"""The ``ha_tts`` engine: render, validation, availability, secrecy and wiring.

Every request is answered by ``httpx.MockTransport`` and every file lands in
``tmp_path``, so no test resolves a name, opens a socket, runs ``espeak-ng`` or
touches a real Home Assistant. The token tests prove the long-lived token travels
only in the ``Authorization`` header of the ``POST``: never on the
unauthenticated proxy ``GET``, never in a message, never in a log record and
never in the engine's own state.

Owning ticket: HSD-npt0.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import traceback
import wave
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Final, cast

import httpx
import pytest
import pytest_asyncio
from log_isolation import preserve_log_state
from test_press import _Clock, _FakeDispatcher, _seed_tts, _service
from test_tts_speech import FakeEngine

from chime_deck.config import SECRET_SCRUBBER
from chime_deck.health import HealthRegistry, HealthStatus
from chime_deck.hub import StateHub
from chime_deck.mqtt.bridge import DEFAULT_SEAM_TIMEOUT_S
from chime_deck.press import PressCode, PressOutcome, SpeechUnavailable
from chime_deck.sounds.library import SUPPORTED_FORMATS
from chime_deck.store import SETTINGS, ConfigStore
from chime_deck.tts import (
    FALLBACK_CHAIN,
    Availability,
    HaTtsEngine,
    TtsCache,
    TtsOptions,
    TtsRenderError,
    TtsSpeechSource,
    build_speech_source,
    sniff_container,
)
from chime_deck.tts.ha_tts import (
    AVAILABILITY_POSITIVE_TTL_S,
    AVAILABILITY_TIMEOUT_S,
    MAX_AUDIO_BYTES,
    PRESS_MARGIN_S,
    TTS_DOWNLOAD_TIMEOUT_S,
    TTS_GET_URL_TIMEOUT_S,
    TTS_RENDER_TOTAL_TIMEOUT_S,
)

pytestmark = pytest.mark.asyncio

TOKEN: Final = "secret-long-lived-token-abc123"
"""Any occurrence of this string in a message, a log record or the engine is a failure."""

BASE_URL: Final = "http://ha.test:8123"

POST_PATH: Final = "/api/tts_get_url"
"""Path of the authenticated URL request; the download path varies per test."""

_OUT: Final = "out.wav"
"""Destination file name used by almost every render test."""

_AUDIBLE_FRAMES: Final = 800
"""Sample frames of the default audio answer: 0.1 s at 8 kHz, above the audible floor."""

_SLOW_S: Final = 0.2
"""Sleep of the deliberately slow handlers; far longer than the budgets under test."""

_NEVER: Final = 3600.0
"""Hub flush interval used by the fixture: the background tick never fires."""

_OPTIONS: Final = TtsOptions(engine_id="tts.piper", language="en_GB", preferred_format="wav")

_ENTITY_PIPER: Final = {
    "entity_id": "tts.piper",
    "state": "unknown",
    "attributes": {"friendly_name": "Piper", "language": "en_GB"},
}
"""A real ``/api/states/<entity>`` answer for the configured TTS entity."""

_Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]
"""One scripted transport answer: awaited with the request it answers."""

_Item = httpx.Response | Exception | _Handler
"""What a :class:`Scripted` route can hold."""

_SNIFF_SAMPLES: Final[dict[str, bytes]] = {
    "wav": b"RIFF\x00\x00\x00\x00WAVEfmt ",
    "mp3": b"ID3\x04\x00\x00\x00\x00\x00\x00\x00",
    "ogg": b"OggS\x00\x00\x00\x00\x00\x00\x00\x00",
    "flac": b"fLaC\x00\x00\x00\x00\x00\x00\x00\x00",
}
"""One leading-bytes sample per supported container, used to pin the token set."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test."""
    with preserve_log_state():
        yield


class _Settings:
    """A settings reader backed by a dictionary, as ``ConfigStore`` would be."""

    def __init__(self, values: Mapping[str, object] | None = None) -> None:
        """Create the reader.

        :param values: overrides keyed by setting name; the Home Assistant
            defaults are present unless a test overrides them.
        """
        defaults: dict[str, object] = {
            "ha.base_url": BASE_URL,
            "ha.token": TOKEN,
            "tts.ha.engine_id": "tts.piper",
            "tts.ha.language": "en_GB",
        }
        if values is not None:
            defaults.update(values)
        self._values = defaults

    async def get_setting(self, key: str) -> object:
        """Return the stored value of *key*, or ``""`` when it is unset.

        :param key: registered setting key.
        """
        return self._values.get(key, "")


class _ManualClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        """Start at a fixed origin."""
        self.now: float = 0.0

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now


class Scripted:
    """Answer ``(method, path)`` routes and record every request it receives."""

    def __init__(
        self,
        routes: Mapping[tuple[str, str], _Item] | None = None,
        *,
        default: _Item | None = None,
    ) -> None:
        """Create the handler.

        :param routes: answers keyed by ``(METHOD, url.path)``; an unknown key
            falls back to *default*.
        :param default: answer for an unscripted route; ``None`` answers ``404``.
        """
        self.routes = dict(routes) if routes is not None else {}
        self.default = default
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record *request* and answer it with the scripted item for its route.

        :param request: the request httpx is about to send.
        """
        self.requests.append(request)
        item = self.routes.get((request.method.upper(), request.url.path), self.default)
        if item is None:
            return httpx.Response(404, text="unexpected")
        if isinstance(item, httpx.Response):
            return item
        if isinstance(item, Exception):
            raise item
        return await item(request)

    def paths(self) -> list[str]:
        """Return the path of every recorded request, in order."""
        return [request.url.path for request in self.requests]


def _json(payload: object, status: int = 200) -> httpx.Response:
    """Build an unattached JSON answer.

    :param payload: decoded JSON to serialise into the body.
    :param status: status code of the answer.
    """
    return httpx.Response(status, json=payload)


def _wav(frames: int, rate: int = 8000) -> bytes:
    """Build a silent mono 16-bit WAV with *frames* sample frames.

    :param frames: number of sample frames, which decide the duration.
    :param rate: sample rate in hertz.
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def _audio(
    body: bytes | None = None,
    content_type: str | None = "audio/wav",
    status: int = 200,
) -> httpx.Response:
    """Build an unattached audio answer.

    The default body is a real, audible WAV rather than a bare RIFF header: the
    engine measures the WAV before publishing it, so a header-only body would be
    rejected as inaudible. Tests that want a rejection pass *body* explicitly.

    :param body: response body; ``None`` uses :func:`_wav` at a playable length.
    :param content_type: media type, or ``None`` to send no ``Content-Type``.
    :param status: status code of the answer.
    """
    headers = {"content-type": content_type} if content_type is not None else {}
    return httpx.Response(
        status,
        headers=headers,
        content=_wav(_AUDIBLE_FRAMES) if body is None else body,
    )


def _chunked_audio(chunks: list[bytes], content_type: str = "audio/wav") -> _Handler:
    """Answer with a chunked body that declares no ``Content-Length``.

    A server that omits the length is exactly the case the running byte count
    exists for, so the cap cannot be enforced by the header pre-check alone.

    :param chunks: body chunks yielded in order.
    :param content_type: media type of the answer.
    """

    async def answer(request: httpx.Request) -> httpx.Response:
        """Build one streamed answer for *request*.

        :param request: the request being answered.
        """

        async def body() -> AsyncIterator[bytes]:
            """Yield the configured chunks one at a time."""
            for chunk in chunks:
                yield chunk

        return httpx.Response(200, headers={"content-type": content_type}, content=body())

    return answer


def _replies(build: Callable[[], httpx.Response]) -> _Handler:
    """Return a handler that builds a fresh answer for every request.

    A single :class:`httpx.Response` can only be read once, so a route that must
    answer more than one request uses this factory instead.

    :param build: constructs the answer for one request.
    """

    async def respond(request: httpx.Request) -> httpx.Response:
        """Answer *request* with a newly built response.

        :param request: the request being answered.
        """
        return build()

    return respond


def _client(
    handler: Scripted,
    *,
    headers: Mapping[str, str] | None = None,
    auth: httpx.Auth | tuple[str, str] | None = None,
) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` wired to *handler*.

    :param handler: the transport handler that answers every request.
    :param headers: client-wide headers, merged into requests the client builds.
    :param auth: client-wide auth, used by the request-building methods.
    """
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers=headers,
        auth=auth,
    )


def _engine(
    handler: Scripted,
    settings: _Settings | None = None,
    *,
    raw: httpx.AsyncClient | None = None,
    max_bytes: int = MAX_AUDIO_BYTES,
    total_timeout_s: float = TTS_RENDER_TOTAL_TIMEOUT_S,
    download_timeout_s: float = TTS_DOWNLOAD_TIMEOUT_S,
    availability_timeout_s: float = AVAILABILITY_TIMEOUT_S,
    positive_ttl_s: float = AVAILABILITY_POSITIVE_TTL_S,
    clock: Callable[[], float] | None = None,
) -> tuple[HaTtsEngine, httpx.AsyncClient, Scripted]:
    """Build an engine over a mock-transport client and return both.

    :param handler: the transport handler that answers every request.
    :param settings: settings reader; the Home Assistant defaults when omitted.
    :param raw: a client to borrow instead of building one over *handler*.
    :param max_bytes: largest speech file the engine accepts.
    :param total_timeout_s: end-to-end render budget.
    :param download_timeout_s: download-leg budget.
    :param availability_timeout_s: availability probe budget.
    :param positive_ttl_s: how long an "available" verdict is memoised.
    :param clock: monotonic clock for the memo; a real one when omitted.
    """
    client = raw if raw is not None else _client(handler)
    engine = HaTtsEngine(
        settings if settings is not None else _Settings(),
        client=client,
        max_bytes=max_bytes,
        total_timeout_s=total_timeout_s,
        download_timeout_s=download_timeout_s,
        availability_timeout_s=availability_timeout_s,
        positive_ttl_s=positive_ttl_s,
        clock=clock,
    )
    return engine, client, handler


def _engine_state_strings(value: object, seen: set[int] | None = None) -> list[str]:
    """Collect every string the engine itself holds, stopping at borrowed objects.

    The settings reader is the user's storage and the HTTP client (with its
    scripted transport) is a borrowed collaborator; a token legitimately lives
    in both. The promise under test is narrower and stronger: the engine copies
    no secret into its own fields, so neither boundary is crossed.

    :param value: object to walk; normally ``vars(engine)``.
    :param seen: object ids already walked, so a cycle cannot loop forever.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, (httpx.AsyncClient, _Settings)):
        return []
    visited = seen if seen is not None else set()
    if id(value) in visited:
        return []
    visited.add(id(value))
    if isinstance(value, Mapping):
        found: list[str] = []
        for key, item in value.items():
            found.extend(_engine_state_strings(key, visited))
            found.extend(_engine_state_strings(item, visited))
        return found
    if isinstance(value, (list, tuple, set, frozenset)):
        found = []
        for item in value:
            found.extend(_engine_state_strings(item, visited))
        return found
    state = getattr(value, "__dict__", None)
    if isinstance(state, dict):
        return _engine_state_strings(cast("dict[str, object]", state), visited)
    return []


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


@pytest_asyncio.fixture
async def hub(store: ConfigStore) -> AsyncIterator[StateHub]:
    """Yield a real hub that is never started, so only explicit flushes deliver.

    :param store: connected configuration store.
    """
    instance = StateHub(store, HealthRegistry(), flush_interval=_NEVER)
    yield instance
    await instance.stop()


async def _seed_ha(store: ConfigStore) -> None:
    """Store the Home Assistant settings the default ``ha_tts`` engine needs.

    :param store: connected configuration store.
    """
    await store.set_setting("ha.base_url", BASE_URL)
    await store.set_setting("ha.token", TOKEN)
    await store.set_setting("tts.ha.engine_id", "tts.piper")
    await store.set_setting("tts.ha.language", "en_GB")


# --------------------------------------------------------------------------- render


async def test_render_downloads_the_relative_proxy_path(tmp_path: Path) -> None:
    """The ``path`` is preferred, fetched unauthenticated and published intact.

    :param tmp_path: pytest temporary directory.
    """
    body = _wav(_AUDIBLE_FRAMES)
    handler = Scripted(
        {
            ("POST", POST_PATH): _json(
                {"url": "http://cdn.test/x.wav", "path": "/api/tts_proxy/local.wav"}
            ),
            ("GET", "/api/tts_proxy/local.wav"): _audio(body=body),
        }
    )
    engine, raw, handler = _engine(handler)
    try:
        destination = tmp_path / _OUT
        result = await engine.render("Someone is at the door", _OPTIONS, destination=destination)
    finally:
        await raw.aclose()

    assert result == destination
    assert destination.read_bytes() == body
    post = handler.requests[0]
    assert post.method == "POST"
    payload = cast("dict[str, object]", json.loads(post.content))
    assert payload["engine_id"] == "tts.piper"
    assert payload["message"] == "Someone is at the door"
    assert payload["language"] == "en_GB"
    assert payload["options"] == {"preferred_format": "wav"}
    get = handler.requests[1]
    assert get.method == "GET"
    assert str(get.url) == BASE_URL + "/api/tts_proxy/local.wav"
    assert "authorization" not in get.headers
    assert "cookie" not in get.headers


async def test_the_relative_path_wins_over_a_disagreeing_absolute_url(tmp_path: Path) -> None:
    """The download goes to the configured host, not the answer's absolute URL.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted(
        {
            ("POST", POST_PATH): _json(
                {"url": "http://cdn.test/x.wav", "path": "/api/tts_proxy/local.wav"}
            ),
            ("GET", "/api/tts_proxy/local.wav"): _audio(),
        }
    )
    engine, raw, handler = _engine(handler)
    try:
        await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()

    assert handler.paths() == [POST_PATH, "/api/tts_proxy/local.wav"]
    visited = handler.requests[1]
    assert str(visited.url) == BASE_URL + "/api/tts_proxy/local.wav"


async def test_a_missing_path_falls_back_to_a_same_host_absolute_url(tmp_path: Path) -> None:
    """With no ``path``, a same-host absolute ``url`` is fetched instead.

    :param tmp_path: pytest temporary directory.
    """
    absolute = BASE_URL + "/api/tts_proxy/abs.wav"
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": absolute, "path": None}),
            ("GET", "/api/tts_proxy/abs.wav"): _audio(),
        }
    )
    engine, raw, handler = _engine(handler)
    try:
        await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()

    assert str(handler.requests[1].url) == absolute


async def test_an_answer_without_any_address_is_refused(tmp_path: Path) -> None:
    """Neither a ``path`` nor a ``url`` means there is nothing to download.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted({("POST", POST_PATH): _json({"url": "", "path": None})})
    engine, raw, handler = _engine(handler)
    try:
        with pytest.raises(TtsRenderError, match="no address"):
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()

    assert len(handler.requests) == 1


async def test_an_off_host_absolute_url_is_refused(tmp_path: Path) -> None:
    """An absolute URL on another host is refused before any download.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted(
        {("POST", POST_PATH): _json({"url": "http://evil.test/x.wav", "path": None})}
    )
    engine, raw, handler = _engine(handler)
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()

    assert "evil.test" in str(excinfo.value)
    assert len(handler.requests) == 1


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/a.wav",
        "http://evil/a.wav",
        "/api/../admin/x",
        "/admin/x",
        "evil.com/x",
    ],
)
async def test_a_malicious_proxy_path_is_refused(tmp_path: Path, path: str) -> None:
    """A path that is not a plain ``/api/tts_proxy/...`` path never gets a GET.

    :param tmp_path: pytest temporary directory.
    :param path: hostile ``path`` value returned by Home Assistant.
    """
    handler = Scripted({("POST", POST_PATH): _json({"url": "", "path": path})})
    engine, raw, handler = _engine(handler)
    try:
        with pytest.raises(TtsRenderError):
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()

    assert len(handler.requests) == 1
    assert all(request.method == "POST" for request in handler.requests)


async def test_a_400_carries_home_assistants_own_message(tmp_path: Path) -> None:
    """A refused synthesis is reported with the status and HA's explanation.

    A ``400`` means the request has to change first, so it is never retried.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted(
        {("POST", POST_PATH): _json({"message": "engine tts.piper is not loaded"}, status=400)}
    )
    engine, raw, _handler = _engine(handler)
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()

    message = str(excinfo.value)
    assert "HTTP 400" in message
    assert "engine tts.piper is not loaded" in message
    assert excinfo.value.transient is False


@pytest.mark.parametrize(
    ("status", "transient"),
    [
        (500, True),
        (503, True),
        (408, True),
        (429, True),
        (401, False),
        (403, False),
        (404, False),
    ],
)
async def test_a_status_answer_is_classified_for_a_retry(
    tmp_path: Path, status: int, transient: bool
) -> None:
    """A server-side or rate-limit status is transient; an auth or name one is not.

    The rule is not re-derived here: :mod:`chime_deck.ha.errors` already says
    ``408``/``429``/``5xx`` are retryable while ``401``/``403``/``404`` are not,
    and the engine only carries that verdict across the seam in
    :attr:`~chime_deck.tts.errors.TtsRenderError.transient`.

    :param tmp_path: pytest temporary directory.
    :param status: HTTP status Home Assistant answers the URL request with.
    :param transient: expected classification of the failure.
    """
    handler = Scripted({("POST", POST_PATH): _json({"message": "nope"}, status=status)})
    engine, raw, _handler = _engine(handler)
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render("hello", _OPTIONS, destination=destination)
    finally:
        await raw.aclose()

    assert excinfo.value.transient is transient
    assert not destination.exists()


async def test_an_unreachable_home_assistant_says_cannot_connect(tmp_path: Path) -> None:
    """A connect failure becomes the "cannot connect" sentence.

    A refused connection is a transport failure, so a second attempt could
    succeed and the error says so.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted({("POST", POST_PATH): httpx.ConnectError("boom")})
    engine, raw, _handler = _engine(handler)
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()

    assert "cannot connect" in str(excinfo.value)
    assert excinfo.value.transient is True


async def test_a_read_timeout_on_the_download_is_transient(tmp_path: Path) -> None:
    """A stalled download is a transport failure, so one retry could succeed.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): httpx.ReadTimeout("read timed out"),
        }
    )
    engine, raw, _handler = _engine(handler)
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render("hello", _OPTIONS, destination=destination)
    finally:
        await raw.aclose()

    assert "stopped sending the speech audio" in str(excinfo.value)
    assert excinfo.value.transient is True
    assert not destination.exists()


async def test_an_html_answer_is_not_published(tmp_path: Path) -> None:
    """A ``text/html`` answer is rejected and leaves no file or temp behind.

    :param tmp_path: pytest temporary directory.
    """
    html = httpx.Response(
        200,
        text="<!doctype html><html><body>error</body></html>",
        headers={"content-type": "text/html"},
    )
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): html,
        }
    )
    engine, raw, _handler = _engine(handler)
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError, match="instead of audio"):
            await engine.render("hello", _OPTIONS, destination=destination)
    finally:
        await raw.aclose()

    assert not destination.exists()
    assert list(tmp_path.glob(f".{_OUT}.tmp*")) == []


async def test_an_html_body_behind_an_audio_content_type_is_not_published(tmp_path: Path) -> None:
    """A magic-byte sniff is mandatory; a lying ``audio/`` type is not trusted.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): _audio(
                body=b"<!doctype html><html>nope</html>", content_type="audio/mpeg"
            ),
        }
    )
    engine, raw, _handler = _engine(handler)
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError, match="instead of audio"):
            await engine.render("hello", _OPTIONS, destination=destination)
    finally:
        await raw.aclose()

    assert not destination.exists()


async def test_an_oversized_answer_is_abandoned(tmp_path: Path) -> None:
    """A body past ``max_bytes`` is abandoned and leaves nothing behind.

    :param tmp_path: pytest temporary directory.
    """
    oversized = b"RIFF" + (4088).to_bytes(4, "little") + b"WAVE" + b"\x00" * 4084
    assert len(oversized) == 4096
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): _audio(body=oversized),
        }
    )
    engine, raw, _handler = _engine(handler, max_bytes=1024)
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError, match="abandoned"):
            await engine.render("hello", _OPTIONS, destination=destination)
    finally:
        await raw.aclose()

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_an_oversized_answer_without_a_content_length_is_abandoned(
    tmp_path: Path,
) -> None:
    """The running byte count catches a chunked answer that declares no length.

    The header pre-check cannot fire here, so only the streaming cap can stop the
    download.

    :param tmp_path: pytest temporary directory.
    """
    chunks = [b"\x00" * 600 for _ in range(4)]
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): _chunked_audio(chunks),
        }
    )
    engine, raw, _handler = _engine(handler, max_bytes=1024)
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError, match="abandoned"):
            await engine.render("hello", _OPTIONS, destination=destination)
    finally:
        await raw.aclose()

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_an_empty_answer_is_not_published(tmp_path: Path) -> None:
    """A zero-byte answer is reported as an empty speech file.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): _audio(body=b""),
        }
    )
    engine, raw, _handler = _engine(handler)
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError, match="empty speech file"):
            await engine.render("hello", _OPTIONS, destination=destination)
    finally:
        await raw.aclose()

    assert not destination.exists()


async def test_a_zero_frame_wav_is_not_published(tmp_path: Path) -> None:
    """A syntactically valid but silent WAV is rejected as inaudible.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): _audio(body=_wav(0)),
        }
    )
    engine, raw, _handler = _engine(handler)
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError, match="no audible audio"):
            await engine.render("hello", _OPTIONS, destination=destination)
    finally:
        await raw.aclose()

    assert not destination.exists()


async def test_a_redirect_on_the_download_is_refused(tmp_path: Path) -> None:
    """The unauthenticated GET refuses a redirect instead of following it.

    :param tmp_path: pytest temporary directory.
    """
    redirect = httpx.Response(302, headers={"location": "http://evil.test/x.wav"})
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): redirect,
        }
    )
    engine, raw, handler = _engine(handler)
    try:
        with pytest.raises(TtsRenderError, match="redirected"):
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()

    hosts = {request.url.host for request in handler.requests}
    assert hosts == {"ha.test"}
    assert len(handler.requests) == 2


async def test_a_redirect_on_the_url_request_is_refused(tmp_path: Path) -> None:
    """A redirect answer to ``tts_get_url`` is refused by the typed client.

    :param tmp_path: pytest temporary directory.
    """
    redirect = httpx.Response(302, headers={"location": "http://elsewhere.test/api"})
    handler = Scripted({("POST", POST_PATH): redirect})
    engine, raw, _handler = _engine(handler)
    try:
        with pytest.raises(TtsRenderError, match="redirect"):
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()


async def test_mp3_bytes_on_a_wav_path_are_published_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The real container is logged, but the requested suffix and bytes are kept.

    :param tmp_path: pytest temporary directory.
    :param caplog: pytest log capture fixture.
    """
    body = b"ID3" + b"\x00" * 200
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): _audio(body=body, content_type="audio/mpeg"),
        }
    )
    engine, raw, _handler = _engine(handler)
    destination = tmp_path / _OUT
    try:
        with caplog.at_level(logging.WARNING, logger="chime_deck.tts.ha_tts"):
            result = await engine.render("hello", _OPTIONS, destination=destination)
    finally:
        await raw.aclose()

    assert result == destination
    assert destination.read_bytes() == body
    records = [
        record.getMessage() for record in caplog.records if record.name == "chime_deck.tts.ha_tts"
    ]
    assert any("mp3" in message and "wav" in message for message in records)


async def test_a_generic_content_type_is_accepted_when_the_bytes_are_audio(
    tmp_path: Path,
) -> None:
    """``application/octet-stream`` with RIFF bytes is accepted on the sniff alone.

    :param tmp_path: pytest temporary directory.
    """
    body = _wav(_AUDIBLE_FRAMES)
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): _audio(
                body=body, content_type="application/octet-stream"
            ),
        }
    )
    engine, raw, _handler = _engine(handler)
    destination = tmp_path / _OUT
    try:
        await engine.render("hello", _OPTIONS, destination=destination)
    finally:
        await raw.aclose()

    assert destination.read_bytes() == body


async def test_a_missing_content_type_does_not_excuse_an_html_body(tmp_path: Path) -> None:
    """With no content type at all, a non-audio body is still rejected.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): _audio(
                body=b"<!doctype html><html>nope</html>", content_type=None
            ),
        }
    )
    engine, raw, _handler = _engine(handler)
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError, match="instead of audio"):
            await engine.render("hello", _OPTIONS, destination=destination)
    finally:
        await raw.aclose()

    assert not destination.exists()


async def test_a_failing_temp_write_is_a_disk_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write failure names the disk, not the transport.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that makes the temp write fail.
    """

    def no_space(path: Path) -> object:
        """Fail the temp open the way a full disk does.

        :param path: file that would have been opened.
        """
        raise OSError("ENOSPC: no space left on device")

    monkeypatch.setattr("chime_deck.tts.ha_tts._open_temp", no_space)
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): _audio(),
        }
    )
    engine, raw, _handler = _engine(handler)
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()

    message = str(excinfo.value)
    assert "ENOSPC" in message
    assert "cannot connect" not in message


async def test_a_failing_publish_escapes_as_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publish failure is an ``OSError``, which the resolver catches next door.

    :func:`~chime_deck.tts.ha_tts.HaTtsEngine.render` deliberately documents
    that the atomic move is outside its ``TtsRenderError`` net so the resolver
    can tell "Home Assistant said no" from "the disk said no"; the message still
    names the disk.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that makes the publish fail.
    """

    def no_space(tmp: Path, destination: Path) -> None:
        """Fail the atomic move the way a full disk does.

        :param tmp: the validated temporary file.
        :param destination: exact output path.
        """
        raise OSError("ENOSPC: no space left on device")

    monkeypatch.setattr("chime_deck.tts.ha_tts._publish", no_space)
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): _audio(),
        }
    )
    engine, raw, _handler = _engine(handler)
    try:
        with pytest.raises(OSError, match="ENOSPC"):
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()


async def test_an_exception_group_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport-only exception group becomes one typed render error.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that raises the group from the resolver.
    """

    async def group(*args: object, **kwargs: object) -> tuple[str, str]:
        """Raise a transport exception group.

        :param args: ignored positional arguments.
        :param kwargs: ignored keyword arguments.
        """
        raise ExceptionGroup("x", [ValueError("bad"), OSError("net")])

    handler = Scripted()
    engine, raw, _handler = _engine(handler)
    monkeypatch.setattr(engine, "_resolve_url", group)
    try:
        with pytest.raises(TtsRenderError):
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()


async def test_a_cancellation_group_escapes_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A group carrying a cancellation is not swallowed and keeps its leaves.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that raises the base-exception group.
    """

    async def group(*args: object, **kwargs: object) -> tuple[str, str]:
        """Raise a base-exception group that contains a cancellation.

        :param args: ignored positional arguments.
        :param kwargs: ignored keyword arguments.
        """
        raise BaseExceptionGroup("x", [ValueError("bad"), asyncio.CancelledError()])

    handler = Scripted()
    engine, raw, _handler = _engine(handler)
    monkeypatch.setattr(engine, "_resolve_url", group)
    try:
        with pytest.raises(BaseExceptionGroup) as excinfo:
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()

    assert str(excinfo.value) == "x (2 sub-exceptions)"
    leaves = excinfo.value.exceptions
    assert isinstance(leaves[0], ValueError)
    assert str(leaves[0]) == "bad"
    assert isinstance(leaves[1], asyncio.CancelledError)


async def test_the_total_deadline_bounds_a_slow_download(tmp_path: Path) -> None:
    """The end-to-end budget names the injected total, not the download leg.

    :param tmp_path: pytest temporary directory.
    """

    async def slow(request: httpx.Request) -> httpx.Response:
        """Wait past the total budget before answering.

        :param request: the download request.
        """
        await asyncio.sleep(_SLOW_S)
        return _audio()

    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): slow,
        }
    )
    engine, raw, _handler = _engine(handler, total_timeout_s=0.05)
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render("hello", _OPTIONS, destination=destination)
    finally:
        await raw.aclose()

    assert "0.05" in str(excinfo.value)
    assert not destination.exists()


async def test_the_download_leg_has_its_own_deadline(tmp_path: Path) -> None:
    """The download budget names ``download_timeout_s`` when it fires first.

    :param tmp_path: pytest temporary directory.
    """

    async def slow(request: httpx.Request) -> httpx.Response:
        """Wait past the download budget before answering.

        :param request: the download request.
        """
        await asyncio.sleep(_SLOW_S)
        return _audio()

    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): slow,
        }
    )
    engine, raw, _handler = _engine(handler, download_timeout_s=0.02, total_timeout_s=1.0)
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()

    assert "0.02" in str(excinfo.value)


async def test_warmup_opens_nothing() -> None:
    """Construction and warmup are inert: no request is made."""
    handler = Scripted()
    engine, raw, handler = _engine(handler)
    try:
        await engine.warmup()
    finally:
        await raw.aclose()

    assert handler.requests == []


async def test_blank_input_is_refused_before_any_request(tmp_path: Path) -> None:
    """Blank text and a blank sub-engine id are rejected without a request.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted()
    engine, raw, handler = _engine(handler)
    try:
        with pytest.raises(TtsRenderError, match="no text to speak"):
            await engine.render("   ", _OPTIONS, destination=tmp_path / _OUT)
        with pytest.raises(TtsRenderError, match=r"tts\.ha\.engine_id"):
            await engine.render(
                "hello",
                TtsOptions(engine_id="   ", language="en_GB"),
                destination=tmp_path / _OUT,
            )
    finally:
        await raw.aclose()

    assert handler.requests == []


# --------------------------------------------------------------------------- secrecy


class _CookieAuth(httpx.Auth):
    """Add a session cookie, as a client holding a cookie jar would."""

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """Attach the cookie to *request*.

        :param request: request built by the client.
        """
        request.headers["Cookie"] = "session=" + TOKEN
        yield request


async def test_the_download_carries_no_credentials(tmp_path: Path) -> None:
    """The proxy GET carries no client header, cookie or auth of any kind.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted(
        {
            ("POST", POST_PATH): _json({"url": "", "path": "/api/tts_proxy/x.wav"}),
            ("GET", "/api/tts_proxy/x.wav"): _audio(),
        }
    )
    raw = _client(
        handler,
        headers={"Authorization": "Bearer " + TOKEN, "X-Keep": "1"},
        auth=_CookieAuth(),
    )
    engine, raw, handler = _engine(handler, raw=raw)
    try:
        await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()

    get = handler.requests[-1]
    assert get.method == "GET"
    assert "authorization" not in get.headers
    assert "x-keep" not in get.headers
    assert "cookie" not in get.headers
    assert TOKEN not in str(get.url)
    post = handler.requests[0]
    assert post.headers["authorization"] == "Bearer " + TOKEN


async def test_a_secret_echoed_by_home_assistant_never_leaks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A 400 that echoes the token cannot put it in a message, a log or the engine.

    :param tmp_path: pytest temporary directory.
    :param caplog: pytest log capture fixture.
    """

    async def bad_request(request: httpx.Request) -> httpx.Response:
        """Answer the URL request with Home Assistant's 400 message, token included.

        :param request: the URL request being answered.
        """
        return _json({"message": f"the token {TOKEN} is not loaded"}, status=400)

    handler = Scripted({("POST", POST_PATH): bad_request})
    engine, raw, _handler = _engine(handler)
    SECRET_SCRUBBER.set_secrets([TOKEN])
    try:
        with (
            caplog.at_level(logging.DEBUG, logger="chime_deck"),
            pytest.raises(TtsRenderError) as excinfo,
        ):
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
    finally:
        await raw.aclose()

    assert TOKEN not in str(excinfo.value)
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
    state = " ".join(_engine_state_strings(vars(engine)))
    assert TOKEN not in state


# --------------------------------------------------------------------------- availability


async def test_a_missing_token_reports_the_setting_without_a_request() -> None:
    """No token is answered from the settings, not from Home Assistant."""
    engine, raw, handler = _engine(Scripted(), _Settings({"ha.token": ""}))
    try:
        verdict = await engine.available()
    finally:
        await raw.aclose()

    assert isinstance(verdict, Availability)
    assert verdict.ok is False
    assert verdict.reason is not None
    assert "ha.token" in verdict.reason
    assert handler.requests == []


async def test_a_missing_engine_id_reports_the_setting_without_a_request() -> None:
    """No sub-engine id is answered from the settings, not from Home Assistant."""
    engine, raw, handler = _engine(Scripted(), _Settings({"tts.ha.engine_id": ""}))
    try:
        verdict = await engine.available()
    finally:
        await raw.aclose()

    assert verdict.ok is False
    assert verdict.reason is not None
    assert "tts.ha.engine_id" in verdict.reason
    assert handler.requests == []


async def test_a_rejected_token_is_reported_as_such() -> None:
    """A 401 is passed through as the client's "rejected the token" sentence."""
    handler = Scripted({("GET", "/api/states/tts.piper"): httpx.Response(401, text="unauthorized")})
    engine, raw, _handler = _engine(handler)
    try:
        verdict = await engine.available()
    finally:
        await raw.aclose()

    assert verdict.ok is False
    assert verdict.reason is not None
    assert "rejected the token" in verdict.reason


async def test_an_unreachable_host_is_reported_as_cannot_connect() -> None:
    """A connect failure is passed through as the client's sentence."""
    handler = Scripted({("GET", "/api/states/tts.piper"): httpx.ConnectError("boom")})
    engine, raw, _handler = _engine(handler)
    try:
        verdict = await engine.available()
    finally:
        await raw.aclose()

    assert verdict.ok is False
    assert verdict.reason is not None
    assert "cannot connect" in verdict.reason


async def test_a_missing_entity_names_the_engine_and_the_fix() -> None:
    """A 404 for the configured entity explains that the entity was renamed."""
    handler = Scripted({("GET", "/api/states/tts.piper"): httpx.Response(404, text="not found")})
    engine, raw, _handler = _engine(handler)
    try:
        verdict = await engine.available()
    finally:
        await raw.aclose()

    assert verdict.ok is False
    assert verdict.reason is not None
    assert "tts.piper" in verdict.reason
    assert "renamed" in verdict.reason


async def test_the_probe_deadline_is_reported() -> None:
    """A probe slower than ``availability_timeout_s`` reports that budget."""

    async def slow(request: httpx.Request) -> httpx.Response:
        """Wait far past the probe budget.

        :param request: the state request.
        """
        await asyncio.sleep(_SLOW_S)
        return _json(_ENTITY_PIPER)

    handler = Scripted({("GET", "/api/states/tts.piper"): slow})
    engine, raw, _handler = _engine(handler, availability_timeout_s=0.01)
    try:
        verdict = await engine.available()
    finally:
        await raw.aclose()

    assert verdict.ok is False
    assert verdict.reason is not None
    assert "0.01 s" in verdict.reason


async def test_an_available_verdict_is_memoised_until_its_ttl_expires() -> None:
    """Two probes inside the TTL cost one request; an expired one costs another."""
    clock = _ManualClock()
    handler = Scripted({("GET", "/api/states/tts.piper"): _replies(lambda: _json(_ENTITY_PIPER))})
    engine, raw, handler = _engine(handler, clock=clock, positive_ttl_s=AVAILABILITY_POSITIVE_TTL_S)
    try:
        first = await engine.available()
        second = await engine.available()
        assert len(handler.requests) == 1
        clock.now += AVAILABILITY_POSITIVE_TTL_S + 1.0
        third = await engine.available()
    finally:
        await raw.aclose()

    assert first.ok is True
    assert second.ok is True
    assert third.ok is True
    assert len(handler.requests) == 2


async def test_changing_the_engine_id_bypasses_the_memo() -> None:
    """The memo is keyed by the setting, so a new engine id is probed at once."""
    settings = _Settings()
    cloud_entity = {
        "entity_id": "tts.cloud",
        "state": "unknown",
        "attributes": {"friendly_name": "Cloud"},
    }
    handler = Scripted(
        {
            ("GET", "/api/states/tts.piper"): _replies(lambda: _json(_ENTITY_PIPER)),
            ("GET", "/api/states/tts.cloud"): _replies(lambda: _json(cloud_entity)),
        }
    )
    engine, raw, handler = _engine(handler, settings=settings)
    try:
        first = await engine.available()
        second = await engine.available()
        assert len(handler.requests) == 1
        settings._values["tts.ha.engine_id"] = "tts.cloud"
        third = await engine.available()
    finally:
        await raw.aclose()

    assert first.ok is True
    assert second.ok is True
    assert third.ok is True
    assert len(handler.requests) == 2


async def test_a_render_failure_forces_the_next_probe(tmp_path: Path) -> None:
    """A failed render clears the memo, so a broken HA is re-probed at once.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted(
        {
            ("GET", "/api/states/tts.piper"): _replies(lambda: _json(_ENTITY_PIPER)),
            ("POST", POST_PATH): _replies(
                lambda: _json({"message": "the engine exploded"}, status=400)
            ),
        }
    )
    engine, raw, handler = _engine(handler)
    try:
        assert (await engine.available()).ok is True
        with pytest.raises(TtsRenderError):
            await engine.render("hello", _OPTIONS, destination=tmp_path / _OUT)
        assert (await engine.available()).ok is True
    finally:
        await raw.aclose()

    probes = [
        request for request in handler.requests if request.url.path == "/api/states/tts.piper"
    ]
    assert len(probes) == 2


async def test_an_unexpected_failure_is_a_verdict_not_an_exception() -> None:
    """A ``RuntimeError`` from the transport is reported, never raised."""
    handler = Scripted({("GET", "/api/states/tts.piper"): RuntimeError("boom")})
    engine, raw, _handler = _engine(handler)
    try:
        verdict = await engine.available()
    finally:
        await raw.aclose()

    assert verdict.ok is False
    assert verdict.reason is not None
    assert "RuntimeError" in verdict.reason


# --------------------------------------------------------------------------- registration


async def test_build_speech_source_registers_ha_tts_first(
    tmp_path: Path, store: ConfigStore
) -> None:
    """The default engine list resolves a TTS button through ``ha_tts``.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    await _seed_ha(store)
    handler = Scripted(
        {
            ("GET", "/api/states/tts.piper"): _replies(lambda: _json(_ENTITY_PIPER)),
            ("POST", POST_PATH): _replies(
                lambda: _json({"url": "", "path": "/api/tts_proxy/local.wav"})
            ),
            ("GET", "/api/tts_proxy/local.wav"): _replies(_audio),
        }
    )
    raw = _client(handler)
    try:
        source = await build_speech_source(store, data_dir=tmp_path / "data", ha_client=raw)
        clip = await source.resolve(button=button, engine="ha_tts")
    finally:
        await raw.aclose()

    assert clip.engine == "ha_tts"
    assert clip.path.is_file()
    assert ("GET", "/api/states/tts.piper") in handler.routes
    assert [request.method for request in handler.requests] == ["GET", "POST", "GET"]


async def test_an_empty_token_falls_back_to_the_local_engine(
    tmp_path: Path, store: ConfigStore
) -> None:
    """With no token the resolver records the fallback and speaks locally.

    ``espeak_ng`` is replaced by a fake renderer because the real binary is a host
    dependency this suite must not need.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    button = await _seed_tts(store)
    handler = Scripted()
    raw = _client(handler)
    ha = HaTtsEngine(_Settings({"ha.token": ""}), client=raw)
    try:
        source = await build_speech_source(
            store,
            data_dir=tmp_path / "data",
            engines=[ha, FakeEngine("espeak_ng")],
            ha_client=raw,
        )
        clip = await source.resolve(button=button, engine="ha_tts")
    finally:
        await raw.aclose()

    assert clip.engine == "espeak_ng"
    assert clip.detail is not None
    assert clip.detail.startswith("fell back from ha_tts:")
    assert "ha.token" in clip.detail
    assert handler.requests == []


async def test_the_seam_budget_bounds_one_ha_tts_engine() -> None:
    """The nested budgets fit the press seam with the publish margin to spare.

    This bounds **one** ``ha_tts`` engine only: a fallback chain that renders
    twice spends two of these budgets, which is what HSD-p50k has to weigh.
    ``ha_tts`` is still what a press uses by default, even though D24 puts
    ``remote_tts`` first in the chain.
    """
    assert (
        AVAILABILITY_TIMEOUT_S + TTS_RENDER_TOTAL_TIMEOUT_S + PRESS_MARGIN_S
        < DEFAULT_SEAM_TIMEOUT_S
    )
    assert TTS_GET_URL_TIMEOUT_S + TTS_DOWNLOAD_TIMEOUT_S < TTS_RENDER_TOTAL_TIMEOUT_S
    assert FALLBACK_CHAIN[0] == "remote_tts"
    assert HaTtsEngine.name in FALLBACK_CHAIN
    assert SETTINGS["tts.default_engine"].default == HaTtsEngine.name


async def test_sniff_container_tokens_match_the_supported_formats() -> None:
    """The sniffed tokens are exactly the containers the library can play."""
    tokens = {sniff_container(head) for head in _SNIFF_SAMPLES.values()}
    assert tokens == set(SUPPORTED_FORMATS)
    for expected, head in _SNIFF_SAMPLES.items():
        assert sniff_container(head) == expected
    assert sniff_container(b"\xff\xfb" + b"\x00" * 10) == "mp3"
    assert sniff_container(b"\xff\xf1" + b"\x00" * 10) == "mp3"
    assert sniff_container(b"\xff\x1f" + b"\x00" * 10) is None


async def test_a_failed_render_is_one_explainable_press_row(
    store: ConfigStore,
    hub: StateHub,
    tmp_path: Path,
) -> None:
    """A 400 during a press logs one failed row without a Home Assistant traceback.

    The health component is deliberately not written by this failure path: the
    press log is the only surface HSD-npt0 gave a render failure (HSD-p50k owns a
    press-wide health verdict), so the test documents that fail-only surface.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param tmp_path: pytest temporary directory.
    """
    button = await _seed_tts(store)
    message = "engine tts.piper is not loaded"

    async def bad_request(request: httpx.Request) -> httpx.Response:
        """Refuse the synthesis with Home Assistant's own explanation.

        :param request: the URL request being answered.
        """
        return _json({"message": f"{message}; token {TOKEN}"}, status=400)

    handler = Scripted(
        {
            ("GET", "/api/states/tts.piper"): _replies(lambda: _json(_ENTITY_PIPER)),
            ("POST", POST_PATH): bad_request,
        }
    )
    raw = _client(handler)
    health = HealthRegistry()
    SECRET_SCRUBBER.set_secrets([TOKEN])
    try:
        source = TtsSpeechSource(
            store,
            (HaTtsEngine(_Settings(), client=raw),),
            TtsCache(tmp_path / "cache"),
            health=health,
        )
        dispatcher = _FakeDispatcher()
        clock = _Clock(datetime(2026, 1, 1, 12, 0))
        service = _service(store, hub, dispatcher, clock, speech=source)
        result = await service.press(button.slug)
        rows = await store.recent_presses()
        with pytest.raises(SpeechUnavailable) as excinfo:
            await source.resolve(button=button, engine="ha_tts")
    finally:
        await raw.aclose()

    assert result.outcome is PressOutcome.FAILED
    assert result.code is PressCode.TTS_UNAVAILABLE
    assert len(rows) == 1
    detail = rows[0].detail
    assert detail is not None
    assert "could not render speech:" in detail
    assert message in detail
    assert "HaBadRequestError:" not in detail
    assert TOKEN not in detail
    trace = "".join(traceback.format_exception(excinfo.value))
    assert "httpx" not in trace
    assert "HaError" not in trace
    assert health.get("tts") is HealthStatus.NOT_CONFIGURED


async def test_the_disabled_state_never_reaches_home_assistant(
    tmp_path: Path, store: ConfigStore
) -> None:
    """``tts.default_engine: none`` answers from the disabled resolver, offline.

    :param tmp_path: pytest temporary directory.
    :param store: connected configuration store.
    """
    handler = Scripted()
    raw = _client(handler)
    await store.set_setting("tts.default_engine", "none")
    try:
        source = await build_speech_source(store, data_dir=tmp_path / "data", ha_client=raw)
        button = await _seed_tts(store)
        with pytest.raises(SpeechUnavailable, match="disabled"):
            await source.resolve(button=button, engine="ha_tts")
    finally:
        await raw.aclose()

    assert handler.requests == []
