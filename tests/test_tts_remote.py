"""The ``remote_tts`` client engine: render, probe, memo, secrecy and wiring.

Fully offline. Every request is answered by ``httpx.MockTransport``, except the
one end-to-end test, which mounts the real renderer app through
``httpx.ASGITransport`` (no socket) with a fake ``piper`` engine that writes real
audio. The engine's settings come from a dict-backed stub standing in for
``ConfigStore``, so nothing here reads ``data/doorbell.db``, a real renderer, a
real token or a real audio device.

The error ladder is covered failure shape by failure shape, and each failure
also asserts the same two file facts: the ``destination`` was never created and
the directory the engine was writing into is empty, so a hidden ``.tmp`` sibling
can never survive a failed or cancelled render.

Owning ticket: HSD-5de8.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import wave
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from log_isolation import preserve_log_state
from test_press import _Clock, _FakeDispatcher, _seed_tts, _service
from test_tts_speech import FakeEngine

from chime_deck.config import ServerConfig
from chime_deck.health import HealthRegistry
from chime_deck.hub import StateHub
from chime_deck.mqtt.bridge import DEFAULT_SEAM_TIMEOUT_S
from chime_deck.press import PressCode, PressOutcome
from chime_deck.renderer import (
    RENDER_CLIENT_HEADROOM_S,
    RENDER_QUEUE_WAIT_S,
    RENDER_SYNTHESIS_TIMEOUT_S,
    create_renderer_app,
)
from chime_deck.store import SETTINGS, Button, ConfigStore
from chime_deck.tts import Availability, TtsCache, TtsOptions, TtsRenderError, TtsSpeechSource
from chime_deck.tts.ha_tts import PRESS_MARGIN_S, TTS_RENDER_TOTAL_TIMEOUT_S
from chime_deck.tts.remote_tts import (
    REMOTE_AVAILABILITY_TIMEOUT_S,
    REMOTE_RENDER_BUDGET_S,
    RemoteTtsEngine,
)
from chime_deck.tts.templating import TEMPLATE_TIMEOUT_S

pytestmark = pytest.mark.asyncio

RENDERER_URL: Final = "http://renderer.test"
"""The renderer address the stub settings point at; never resolved by a test."""

TOKEN: Final = "remote-renderer-token-abc123"
"""Bearer token the client sends; any occurrence in a URL is a failure."""

TEXT: Final = "Someone is at the door"
"""The phrase every render test speaks."""

_OUT: Final = "out.wav"
"""Destination file name used by almost every render test."""

_AUDIBLE_FRAMES: Final = 800
"""Sample frames of the default audio answer: 0.1 s at 8 kHz, above the audible floor."""

_SLOW_S: Final = 0.2
"""Sleep of the deliberately slow handlers; far longer than the budgets under test."""

_WAIT_S: Final = 5.0
"""Bound for one in-test wait, so a hung await fails instead of stalling the suite."""

_HTTP_TIMEOUT_S: Final = 5.0
"""Bound for one mock-transport request."""

_NEVER: Final = 3600.0
"""Hub flush interval used by the fixture: the background tick never fires."""

_CACHE_NAME: Final = re.compile(r"[0-9a-f]{64}\.wav")
"""What the resolver's cache writes: the sha256 cache key plus the WAV suffix."""

_Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]
"""One scripted transport answer: a coroutine function awaiting the request it answers."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test."""
    with preserve_log_state():
        yield


class _Settings:
    """A settings reader backed by a dictionary, as ``ConfigStore`` would be."""

    def __init__(self, values: Mapping[str, object] | None = None) -> None:
        """Create the reader with a configured renderer by default.

        :param values: overrides keyed by setting name; a key that is absent
            answers with its registry default, exactly as the store does.
        """
        self.values: dict[str, object] = {
            "tts.remote_tts.url": RENDERER_URL,
            "tts.remote_tts.token": TOKEN,
        }
        if values is not None:
            self.values.update(values)

    async def get_setting(self, key: str) -> object:
        """Return the stored value of *key*, or its registry default.

        :param key: registered setting key.
        """
        if key in self.values:
            return self.values[key]
        return SETTINGS[key].default


class Recorder:
    """Answer every request through one handler and record each request."""

    def __init__(self, handler: _Handler) -> None:
        """Create the recorder.

        :param handler: the transport answer every request is given.
        """
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record *request* and answer it.

        :param request: the request httpx is about to send.
        """
        self.requests.append(request)
        return await self._handler(request)


class _ClientSpy:
    """A client factory that records the call and refuses to build anything."""

    def __init__(self) -> None:
        """Create an unused factory."""
        self.calls = 0

    def __call__(self) -> httpx.AsyncClient:
        """Record the call, then fail the test that provoked it."""
        self.calls += 1
        raise AssertionError("no HTTP client may be built here")


class FakePiper:
    """A ``piper`` engine hosted by a renderer, writing real, audible audio."""

    name = "piper"

    def __init__(self) -> None:
        """Create an engine that has rendered nothing yet."""
        self.renders = 0
        self.texts: list[str] = []

    async def available(self) -> Availability:
        """Report available; the renderer probes it before every render."""
        return Availability(True)

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Write an audible WAV at *destination* and record the render.

        :param text: phrase to speak.
        :param options: options for this render.
        :param destination: exact output path.
        """
        self.renders += 1
        self.texts.append(text)
        destination.write_bytes(_wav(_AUDIBLE_FRAMES))
        return destination

    async def warmup(self) -> None:
        """Do nothing; there is nothing to prime."""
        return None


def _json(payload: object, status: int = 200) -> httpx.Response:
    """Build a JSON answer in the project's error-envelope-compatible shape.

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


def _audio(body: bytes | None = None) -> httpx.Response:
    """Build an ``audio/wav`` answer.

    The default body is a real, audible WAV rather than a bare RIFF header: the
    engine measures the WAV before publishing it, so a header-only body would be
    rejected as inaudible. Tests that want a rejection pass *body* explicitly.

    :param body: response body; ``None`` uses :func:`_wav` at a playable length.
    """
    return httpx.Response(
        200,
        headers={"content-type": "audio/wav"},
        content=_wav(_AUDIBLE_FRAMES) if body is None else body,
    )


def _replies(build: Callable[[], httpx.Response]) -> _Handler:
    """Return a handler that builds a fresh answer for every request.

    A single :class:`httpx.Response` can only be streamed once, so any route a
    test hits more than once is built per request.

    :param build: constructs the answer for one request.
    """

    async def respond(request: httpx.Request) -> httpx.Response:
        """Answer *request* with a newly built response.

        :param request: the request being answered.
        """
        return build()

    return respond


def _raises(error: Exception) -> _Handler:
    """Return a handler that raises *error* for every request.

    This is how a transport failure is modelled: ``httpx.MockTransport`` sees a
    raising handler exactly as the client sees a refused connection.

    :param error: exception the transport raises.
    """

    async def answer(request: httpx.Request) -> httpx.Response:
        """Raise the configured transport error.

        :param request: the request being answered.
        """
        raise error

    return answer


async def _slow_answer(request: httpx.Request) -> httpx.Response:
    """Answer only after far longer than any budget under test.

    :param request: the request being answered.
    """
    await asyncio.sleep(_SLOW_S)
    return _audio()


def _streamed(chunks: list[bytes]) -> _Handler:
    """Answer with a chunked ``audio/wav`` body that declares no ``Content-Length``.

    A server that omits the length is exactly the case the running byte count
    exists for, so the cap cannot be enforced by the header pre-check alone.

    :param chunks: body chunks yielded in order.
    """

    async def answer(request: httpx.Request) -> httpx.Response:
        """Build one streamed answer for *request*.

        :param request: the request being answered.
        """

        async def body() -> AsyncIterator[bytes]:
            """Yield the configured chunks one at a time."""
            for chunk in chunks:
                yield chunk

        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=body())

    return answer


def _client(handler: _Handler) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` wired to *handler*.

    :param handler: the transport answer every request is given.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=_HTTP_TIMEOUT_S)


def _engine(
    handler: _Handler,
    settings: _Settings | None = None,
    *,
    render_budget_s: float = REMOTE_RENDER_BUDGET_S,
    availability_timeout_s: float = REMOTE_AVAILABILITY_TIMEOUT_S,
) -> tuple[RemoteTtsEngine, httpx.AsyncClient, Recorder]:
    """Build an engine over a mock-transport client and return both.

    :param handler: the transport answer every request is given.
    :param settings: settings reader; a configured renderer when omitted.
    :param render_budget_s: seconds one render attempt may take.
    :param availability_timeout_s: seconds an availability probe may take.
    """
    recorder = Recorder(handler)
    raw = _client(recorder)
    engine = RemoteTtsEngine(
        settings if settings is not None else _Settings(),
        client=raw,
        render_budget_s=render_budget_s,
        availability_timeout_s=availability_timeout_s,
    )
    return engine, raw, recorder


def _button(text: str = TEXT) -> Button:
    """Build an unsaved TTS button, as ``ConfigStore.upsert_button`` returns one.

    :param text: phrase the button speaks.
    """
    stamp = "2026-01-01T00:00:00Z"
    return Button(
        id=1,
        slug="front-door",
        name="Front Door",
        icon=None,
        action_type="tts",
        sound_id=None,
        tts_text=text,
        tts_engine=None,
        baked_sha=None,
        baked_format=None,
        baked_at=None,
        baked_error=None,
        volume_override=None,
        dedupe_seconds=None,
        show_on_dashboard=False,
        quiet_hours_exempt=False,
        enabled=True,
        sort_order=0,
        created_at=stamp,
        updated_at=stamp,
    )


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield a client bound to it.

    :param app: application under test.
    """
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=RENDERER_URL,
            timeout=_HTTP_TIMEOUT_S,
        ) as client,
    ):
        yield client


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


# --------------------------------------------------------------------------- render


async def test_render_posts_the_phrase_and_publishes_the_wav_at_destination(
    tmp_path: Path,
) -> None:
    """A render POSTs the phrase with the bearer token and writes one playable WAV.

    The token travels in the ``Authorization`` header only: it must never reach
    the URL, where a proxy log or a ``systemd`` unit would capture it.

    :param tmp_path: pytest temporary directory.
    """
    body = _wav(_AUDIBLE_FRAMES)
    engine, raw, recorder = _engine(_replies(lambda: _audio(body=body)))
    destination = tmp_path / _OUT
    try:
        result = await engine.render(TEXT, TtsOptions(), destination=destination)
    finally:
        await raw.aclose()

    assert result == destination
    assert destination.read_bytes() == body
    with wave.open(str(destination), "rb") as reader:
        assert reader.getnframes() == _AUDIBLE_FRAMES
    assert list(tmp_path.iterdir()) == [destination]

    assert len(recorder.requests) == 1
    post = recorder.requests[0]
    assert post.method == "POST"
    assert str(post.url) == RENDERER_URL + "/api/tts/render"
    assert post.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(post.url)
    assert json.loads(post.content) == {
        "text": TEXT,
        "engine": "piper",
        "options": {"preferred_format": "wav"},
    }


async def test_an_in_process_renderer_returns_a_wav_the_client_caches(tmp_path: Path) -> None:
    """The renderer's answer reaches the resolver's bake cache as a valid WAV.

    This is the ticket's end-to-end criterion in one process: the real
    ``create_renderer_app`` mounted through ``httpx.ASGITransport`` (never a
    socket), a fake ``piper`` that writes real audio, and the client engine
    publishing into the ``tts-cache`` directory the resolver owns.

    :param tmp_path: pytest temporary directory.
    """
    piper = FakePiper()
    config = ServerConfig(data_dir=tmp_path, static_dir=tmp_path / "no-dist")
    app = create_renderer_app(config, token=TOKEN, engines=(piper,), spool_dir=tmp_path / "spool")
    settings = _Settings()
    cache = TtsCache(tmp_path / "tts-cache")

    async with _running(app) as client:
        source = TtsSpeechSource(
            settings,
            [RemoteTtsEngine(settings, client=client)],
            cache,
            templating=None,
        )
        clip = await source.resolve(button=_button(), engine="remote_tts")

    assert clip.engine == "remote_tts"
    assert clip.detail is None
    assert clip.path.parent == tmp_path / "tts-cache"
    assert _CACHE_NAME.fullmatch(clip.path.name) is not None
    with wave.open(str(clip.path), "rb") as reader:
        assert reader.getnframes() == _AUDIBLE_FRAMES
    assert [entry.name for entry in cache.root.iterdir()] == [clip.path.name]
    assert piper.renders == 1
    assert piper.texts == [TEXT]


async def test_a_rejected_token_names_both_token_settings(tmp_path: Path) -> None:
    """A 401 render and a 401 probe carry the same actionable sentence.

    The sentence must name the client's setting *and* the renderer's, because
    the fix is to make the two values match.

    :param tmp_path: pytest temporary directory.
    """
    envelope = {
        "error": {
            "code": "unauthorized",
            "message": "the renderer needs a bearer token",
            "detail": {},
        }
    }
    engine, raw, _recorder = _engine(_replies(lambda: httpx.Response(401, json=envelope)))
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render(TEXT, TtsOptions(), destination=destination)
        verdict = await engine.available()
    finally:
        await raw.aclose()

    message = str(excinfo.value)
    assert "tts.remote_tts.token" in message
    assert "tts.renderer.token" in message
    assert excinfo.value.transient is False
    assert verdict.ok is False
    assert verdict.reason == message
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_a_renderer_message_that_echoes_the_token_is_redacted(tmp_path: Path) -> None:
    """A renderer envelope that quotes the token is redacted before it becomes a sentence.

    The renderer chooses its own error text, so it may repeat anything it was
    sent; the client therefore has to scrub the token out rather than trust the
    answer not to contain it.

    :param tmp_path: pytest temporary directory.
    """
    envelope = {
        "error": {
            "code": "validation_error",
            "message": f"the phrase {TOKEN} was refused by this renderer",
            "detail": {},
        }
    }
    engine, raw, _recorder = _engine(_replies(lambda: httpx.Response(422, json=envelope)))
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render(TEXT, TtsOptions(), destination=destination)
    finally:
        await raw.aclose()

    message = str(excinfo.value)
    assert "***" in message
    assert TOKEN not in message
    assert excinfo.value.transient is False
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("label", "build"),
    [
        ("connect-error", lambda: _raises(httpx.ConnectError("boom"))),
        ("read-timeout", lambda: _raises(httpx.ReadTimeout("slow"))),
    ],
)
async def test_a_transport_failure_is_transient(
    tmp_path: Path, label: str, build: Callable[[], _Handler]
) -> None:
    """A failure to reach the renderer or to keep reading is worth one retry.

    ``label`` names the transport shape in the test id: a refused connection and
    a read that timed out. Both are classified at this seam, where the failure is
    known, so the resolver never has to re-derive it.

    :param tmp_path: pytest temporary directory.
    :param label: short name of the transport failure; it is only the test id.
    :param build: builds the handler that raises it.
    """
    engine, raw, _recorder = _engine(build())
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render(TEXT, TtsOptions(), destination=destination)
    finally:
        await raw.aclose()

    assert excinfo.value.transient is True
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_a_not_found_answer_is_not_transient(tmp_path: Path) -> None:
    """A ``404`` means the address is wrong, so a retry would fail the same way.

    :param tmp_path: pytest temporary directory.
    """
    engine, raw, _recorder = _engine(_replies(lambda: _json({"status": "missing"}, status=404)))
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render(TEXT, TtsOptions(), destination=destination)
    finally:
        await raw.aclose()

    assert "HTTP 404" in str(excinfo.value)
    assert excinfo.value.transient is False
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("label", "build", "render_budget_s"),
    [
        (
            "html-page",
            lambda: _replies(
                lambda: httpx.Response(
                    200, text="<html>nope</html>", headers={"content-type": "text/html"}
                )
            ),
            REMOTE_RENDER_BUDGET_S,
        ),
        (
            "html-dressed-as-audio",
            lambda: _replies(lambda: _audio(body=b"<!doctype html><html>nope</html>")),
            REMOTE_RENDER_BUDGET_S,
        ),
        ("inaudible", lambda: _replies(lambda: _audio(body=_wav(0))), REMOTE_RENDER_BUDGET_S),
        (
            "refused",
            lambda: _replies(
                lambda: _json(
                    {"error": {"code": "engine_unavailable", "message": "no"}}, status=503
                )
            ),
            REMOTE_RENDER_BUDGET_S,
        ),
        ("timeout", lambda: _slow_answer, 0.01),
    ],
)
async def test_every_failed_render_leaves_no_file_behind(
    tmp_path: Path,
    label: str,
    build: Callable[[], _Handler],
    render_budget_s: float,
) -> None:
    """None of the failure shapes publishes a file or keeps a temp sibling.

    ``label`` names the shape in the test id: a web page, a page dressed as
    audio, an inaudible WAV, a refused render and a render that outlives its
    budget. Every one of them must leave the directory exactly as it was.

    :param tmp_path: pytest temporary directory.
    :param label: short name of the failure shape under test; it is only the
        test id, so the body is identical for every shape.
    :param build: builds the handler that produces that shape.
    :param render_budget_s: render budget the shape is provoked with.
    """
    engine, raw, _recorder = _engine(build(), render_budget_s=render_budget_s)
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError):
            await engine.render(TEXT, TtsOptions(), destination=destination)
    finally:
        await raw.aclose()

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_a_text_html_answer_is_refused_naming_the_content_type(tmp_path: Path) -> None:
    """A web page is never published as speech, and its media type is named.

    :param tmp_path: pytest temporary directory.
    """
    engine, raw, _recorder = _engine(
        _replies(
            lambda: httpx.Response(
                200, text="<html>nope</html>", headers={"content-type": "text/html"}
            )
        )
    )
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render(TEXT, TtsOptions(), destination=destination)
    finally:
        await raw.aclose()

    message = str(excinfo.value)
    assert "text/html" in message
    assert "instead of audio" in message
    assert excinfo.value.transient is False
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_html_bytes_behind_an_audio_content_type_are_refused(tmp_path: Path) -> None:
    """The RIFF magic-byte check catches an error page dressed as ``audio/wav``.

    :param tmp_path: pytest temporary directory.
    """
    engine, raw, _recorder = _engine(
        _replies(lambda: _audio(body=b"<!doctype html><html>nope</html>"))
    )
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render(TEXT, TtsOptions(), destination=destination)
    finally:
        await raw.aclose()

    message = str(excinfo.value)
    assert "not a WAV file" in message
    assert "error page" in message
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_a_header_only_wav_is_refused_as_inaudible(tmp_path: Path) -> None:
    """A valid but silent WAV is rejected, exactly as every local engine does.

    :param tmp_path: pytest temporary directory.
    """
    body = _wav(0)
    assert len(body) == 44
    engine, raw, _recorder = _engine(_replies(lambda: _audio(body=body)))
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError, match="no audible audio"):
            await engine.render(TEXT, TtsOptions(), destination=destination)
    finally:
        await raw.aclose()

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_a_declared_length_over_the_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``Content-Length`` over the cap is refused before a byte is streamed.

    The cap is patched down rather than generating ten megabytes: the check and
    the sentence are what matter, not the size that provokes them.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that shrinks the shared size cap.
    """
    monkeypatch.setattr("chime_deck.tts.remote_tts.MAX_AUDIO_BYTES", 1024)
    engine, raw, _recorder = _engine(
        _replies(
            lambda: httpx.Response(
                200,
                headers={"content-type": "audio/wav", "content-length": "4096"},
                content=_wav(_AUDIBLE_FRAMES),
            )
        )
    )
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError, match="abandoned"):
            await engine.render(TEXT, TtsOptions(), destination=destination)
    finally:
        await raw.aclose()

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_a_lying_body_over_the_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The running byte count stops a chunked body that declares no length.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that shrinks the shared size cap.
    """
    monkeypatch.setattr("chime_deck.tts.remote_tts.MAX_AUDIO_BYTES", 1024)
    chunks = [b"RIFF" + b"\x00" * 596 for _ in range(4)]
    engine, raw, _recorder = _engine(_streamed(chunks))
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError, match="abandoned"):
            await engine.render(TEXT, TtsOptions(), destination=destination)
    finally:
        await raw.aclose()

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_a_cancelled_render_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    """A cancelled render abandons its hidden temp file instead of leaking it.

    The body parks mid-stream, so the temp file is proven to exist before the
    cancellation arrives; the assertion after the gather proves it is gone.

    :param tmp_path: pytest temporary directory.
    """
    streaming = asyncio.Event()
    parked = asyncio.Event()

    async def answer(request: httpx.Request) -> httpx.Response:
        """Answer with a body that starts, then parks forever.

        :param request: the request being answered.
        """

        async def body() -> AsyncIterator[bytes]:
            """Signal that streaming began, then park until cancelled."""
            streaming.set()
            await parked.wait()
            yield b"\x00"

        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=body())

    engine, raw, _recorder = _engine(answer)
    destination = tmp_path / _OUT
    try:
        task = asyncio.create_task(engine.render(TEXT, TtsOptions(), destination=destination))
        try:
            await asyncio.wait_for(streaming.wait(), _WAIT_S)
            assert list(tmp_path.iterdir()) != []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await raw.aclose()

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_a_configured_timeout_longer_than_the_press_budget_is_clamped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The transport ceiling is clamped to the press budget, and said so once.

    ``tts.remote_tts.timeout_s`` is the transport ceiling, not a press budget:
    one press can afford only ``REMOTE_RENDER_BUDGET_S``, so the render is
    bounded there and the INFO record names both numbers.

    :param tmp_path: pytest temporary directory.
    :param caplog: pytest log capture fixture.
    """
    engine, raw, _recorder = _engine(_slow_answer, render_budget_s=0.05)
    destination = tmp_path / _OUT
    try:
        with (
            caplog.at_level(logging.INFO, logger="chime_deck.tts.remote_tts"),
            pytest.raises(TtsRenderError) as excinfo,
        ):
            await engine.render(TEXT, TtsOptions(), destination=destination)
    finally:
        await raw.aclose()

    assert "0.05 s" in str(excinfo.value)
    assert excinfo.value.transient is True
    records = [
        record.getMessage()
        for record in caplog.records
        if record.name == "chime_deck.tts.remote_tts" and record.levelno == logging.INFO
    ]
    assert any("tts.remote_tts.timeout_s is 20 s" in message for message in records)
    assert any("cannot afford" in message for message in records)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("status", "renderer_message", "expected"),
    [
        (429, "too many render requests from this address", "busy"),
        (503, "the render took longer than 6 s and was abandoned", "was abandoned"),
    ],
)
async def test_a_refused_render_says_what_the_user_can_do(
    tmp_path: Path, status: int, renderer_message: str, expected: str
) -> None:
    """A rate-limited or failed render is a sentence a user can act on.

    ``429`` becomes "busy, press again in a moment"; ``503`` carries the
    renderer's own explanation so the user is sent to the right log. Both are
    transient: repeating the same render could succeed.

    :param tmp_path: pytest temporary directory.
    :param status: HTTP status the renderer answers with.
    :param renderer_message: message in the renderer's error envelope.
    :param expected: text the raised sentence must carry.
    """
    envelope = {"error": {"code": "engine_unavailable", "message": renderer_message, "detail": {}}}
    engine, raw, _recorder = _engine(_replies(lambda: httpx.Response(status, json=envelope)))
    destination = tmp_path / _OUT
    try:
        with pytest.raises(TtsRenderError) as excinfo:
            await engine.render(TEXT, TtsOptions(), destination=destination)
    finally:
        await raw.aclose()

    assert expected in str(excinfo.value)
    assert excinfo.value.transient is True
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["timeout", "refused", "unreachable"])
async def test_a_failed_render_makes_the_next_probe_answer_false_without_a_request(
    tmp_path: Path, failure: str
) -> None:
    """A render failure records a negative verdict, so the next press pays nothing.

    This is piper's latch in memo form: the failed render already proved the
    renderer unusable, and a second probe would spend another press's budget.
    ``failure`` names the shape of the failed render — one that outlives its
    budget, one the renderer refuses with ``503``, and one refused at the
    connection — so the rule is pinned for the transport and status halves and
    not only for the timeout.

    :param tmp_path: pytest temporary directory.
    :param failure: which render failure to provoke.
    """

    async def answer(request: httpx.Request) -> httpx.Response:
        """Answer the probe at once and the render with the configured failure.

        :param request: the request being answered.
        """
        if request.method == "GET":
            return _json({"status": "ok"})
        if failure == "timeout":
            return await _slow_answer(request)
        if failure == "refused":
            return _json({"error": {"code": "engine_unavailable", "message": "no"}}, status=503)
        raise httpx.ConnectError("boom")

    engine, raw, recorder = _engine(answer, render_budget_s=0.02)
    try:
        assert (await engine.available()).ok is True
        assert len(recorder.requests) == 1
        with pytest.raises(TtsRenderError):
            await engine.render(TEXT, TtsOptions(), destination=tmp_path / _OUT)
        verdict = await engine.available()
    finally:
        await raw.aclose()

    assert verdict.ok is False
    assert len(recorder.requests) == 2


# --------------------------------------------------------------------------- probe


async def test_available_without_a_url_builds_no_client() -> None:
    """An unconfigured address is answered from the settings, with no client.

    ``client_factory`` is a spy that would fail the test by being called, so an
    empty ``tts.remote_tts.url`` cannot be hiding a socket attempt.
    """
    spy = _ClientSpy()
    engine = RemoteTtsEngine(_Settings({"tts.remote_tts.url": ""}), client_factory=spy)

    verdict = await engine.available()

    assert verdict.ok is False
    assert verdict.reason is not None
    assert "tts.remote_tts.url" in verdict.reason
    assert "unexpectedly" not in verdict.reason
    assert spy.calls == 0


async def test_available_without_a_token_builds_no_client() -> None:
    """An unconfigured token is answered from the settings, with no client."""
    spy = _ClientSpy()
    engine = RemoteTtsEngine(_Settings({"tts.remote_tts.token": ""}), client_factory=spy)

    verdict = await engine.available()

    assert verdict.ok is False
    assert verdict.reason is not None
    assert "tts.remote_tts.token" in verdict.reason
    assert "unexpectedly" not in verdict.reason
    assert spy.calls == 0


async def test_available_reports_an_unreachable_renderer() -> None:
    """A connect failure becomes the "cannot reach" sentence, not a traceback."""
    engine, raw, _recorder = _engine(_raises(httpx.ConnectError("boom")))
    try:
        verdict = await asyncio.wait_for(engine.available(), _WAIT_S)
    finally:
        await raw.aclose()

    assert verdict.ok is False
    assert verdict.reason is not None
    assert "cannot reach" in verdict.reason
    assert RENDERER_URL in verdict.reason


async def test_available_is_bounded_by_its_own_timeout() -> None:
    """A silent renderer is answered inside ``availability_timeout_s``."""
    engine, raw, _recorder = _engine(_slow_answer, availability_timeout_s=0.05)
    try:
        verdict = await asyncio.wait_for(engine.available(), _WAIT_S)
    finally:
        await raw.aclose()

    assert verdict.ok is False
    assert verdict.reason is not None
    assert "0.05 s" in verdict.reason


async def test_available_reports_a_five_hundred() -> None:
    """A ``500`` names the status and points at the renderer role."""
    engine, raw, _recorder = _engine(_replies(lambda: _json({"status": "boom"}, status=500)))
    try:
        verdict = await engine.available()
    finally:
        await raw.aclose()

    assert verdict.ok is False
    assert verdict.reason is not None
    assert "HTTP 500" in verdict.reason
    assert "renderer role" in verdict.reason


async def test_available_reports_a_not_found_and_names_the_role() -> None:
    """A ``404`` on the health path means the host is not running as a renderer."""
    engine, raw, _recorder = _engine(_replies(lambda: _json({"status": "missing"}, status=404)))
    try:
        verdict = await engine.available()
    finally:
        await raw.aclose()

    assert verdict.ok is False
    assert verdict.reason is not None
    assert "HTTP 404" in verdict.reason
    assert "--role renderer" in verdict.reason


async def test_available_probes_the_token_gated_health_path() -> None:
    """A healthy renderer is available, and the probe is a token-gated ``GET``."""
    engine, raw, recorder = _engine(_replies(lambda: _json({"status": "ok"})))
    try:
        verdict = await engine.available()
    finally:
        await raw.aclose()

    assert verdict == Availability(True)
    assert verdict.reason is None
    assert len(recorder.requests) == 1
    probe = recorder.requests[0]
    assert probe.method == "GET"
    assert str(probe.url) == RENDERER_URL + "/api/health"
    assert probe.headers["authorization"] == f"Bearer {TOKEN}"


async def test_a_verdict_is_memoised_and_a_new_url_re_probes() -> None:
    """Two probes cost one request; a changed ``tts.remote_tts.url`` costs another."""
    settings = _Settings()
    engine, raw, recorder = _engine(_replies(lambda: _json({"status": "ok"})), settings=settings)
    try:
        first = await engine.available()
        second = await engine.available()
        assert len(recorder.requests) == 1
        settings.values["tts.remote_tts.url"] = "http://other-renderer.test"
        third = await engine.available()
    finally:
        await raw.aclose()

    assert first.ok is True
    assert second.ok is True
    assert third.ok is True
    assert len(recorder.requests) == 2
    assert str(recorder.requests[1].url) == "http://other-renderer.test/api/health"


async def test_the_availability_memo_key_is_a_hash_and_the_token_is_kept_nowhere(
    tmp_path: Path,
) -> None:
    """The memo key is ``sha256(url\\0token)``, so no attribute holds the token itself.

    Reaching into ``_memo`` is deliberate: the key is the one place the token
    could be stored for a few seconds, and a hex digest is the only acceptable
    shape for it.

    :param tmp_path: pytest temporary directory.
    """
    engine, raw, recorder = _engine(_replies(lambda: _json({"status": "ok"})))
    try:
        verdict = await engine.available()
    finally:
        await raw.aclose()

    assert verdict.ok is True
    assert len(recorder.requests) == 1
    assert TOKEN not in repr(engine.__dict__)
    memo = engine._memo
    assert memo is not None
    assert re.fullmatch(r"[0-9a-f]{64}", memo[0]) is not None


async def test_warmup_and_shutdown_build_no_client() -> None:
    """Construction, warmup and ``aclose`` are inert: the factory never fires."""
    spy = _ClientSpy()
    settings = _Settings()
    engine = RemoteTtsEngine(settings, client_factory=spy)
    await engine.warmup()
    source = TtsSpeechSource(settings, [engine], None)

    await source.aclose()

    assert spy.calls == 0


# --------------------------------------------------------------------------- wiring


async def test_a_press_falls_back_from_an_unreachable_renderer(
    store: ConfigStore, hub: StateHub, tmp_path: Path
) -> None:
    """An unreachable renderer falls back to ``ha_tts`` and the press log says so.

    The press is driven against a real store, so the row under test is the one
    the press path wrote: the fallback is a fact in ``press_log.detail``, not a
    claim in a return value.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param tmp_path: pytest temporary directory.
    """
    await store.set_setting("tts.default_engine", "remote_tts")
    await store.set_setting("tts.remote_tts.url", RENDERER_URL)
    await store.set_setting("tts.remote_tts.token", TOKEN)
    button = await _seed_tts(store)
    client = _client(_raises(httpx.ConnectError("boom")))
    try:
        source = TtsSpeechSource(
            store,
            [RemoteTtsEngine(store, client=client), FakeEngine("ha_tts")],
            TtsCache(tmp_path / "tts-cache"),
        )
        service = _service(
            store,
            hub,
            _FakeDispatcher(),
            _Clock(datetime(2026, 1, 1, 12, 0)),
            speech=source,
        )
        result = await service.press(button.slug)
        rows = await store.recent_presses()
    finally:
        await client.aclose()

    assert result.outcome is PressOutcome.QUEUED
    assert len(rows) == 1
    detail = rows[0].detail
    assert detail is not None
    assert "fell back from remote_tts" in detail
    assert "engine ha_tts" in detail


async def test_the_disabled_engine_never_builds_a_client(
    store: ConfigStore, hub: StateHub, tmp_path: Path
) -> None:
    """``tts.default_engine: none`` (D27) fails the press with no probe and no client.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param tmp_path: pytest temporary directory.
    """
    await store.set_setting("tts.default_engine", "none")
    button = await _seed_tts(store)
    spy = _ClientSpy()
    source = TtsSpeechSource(
        store,
        [RemoteTtsEngine(store, client_factory=spy)],
        TtsCache(tmp_path / "tts-cache"),
    )
    service = _service(
        store,
        hub,
        _FakeDispatcher(),
        _Clock(datetime(2026, 1, 1, 12, 0)),
        speech=source,
    )

    result = await service.press(button.slug)
    rows = await store.recent_presses()

    assert result.outcome is PressOutcome.FAILED
    assert result.code is PressCode.TTS_DISABLED
    assert spy.calls == 0
    assert len(rows) == 1


# --------------------------------------------------------------------------- budgets


async def test_the_remote_budgets_fit_the_press_seam() -> None:
    """The probe and render halves fill the 10 s press seam and fit inside it.

    Each number protects one bound from the design:

    * ``tts.remote_tts.timeout_s`` is the **transport ceiling**, not a press
      budget — it may be configured up to 2 minutes, and the engine clamps it to
      ``REMOTE_RENDER_BUDGET_S`` per press;
    * :data:`REMOTE_AVAILABILITY_TIMEOUT_S` (1.0 s) plus the template budget
      (0.5 s) plus :data:`REMOTE_RENDER_BUDGET_S` (7.5 s) plus
      :data:`PRESS_MARGIN_S` (0.5 s) is 9.5 s, below ``DEFAULT_SEAM_TIMEOUT_S``
      (10.0 s, D24's press seam), so one remote press always answers in time;
    * :data:`REMOTE_RENDER_BUDGET_S` equals ``TTS_RENDER_TOTAL_TIMEOUT_S``, so
      the remote engine is never slower than the local Home Assistant one;
    * the renderer's own halves — :data:`RENDER_QUEUE_WAIT_S` (1.0 s) plus
      :data:`RENDER_SYNTHESIS_TIMEOUT_S` (6.0 s) — stay inside the client's
      budget minus :data:`RENDER_CLIENT_HEADROOM_S` (0.5 s), so a queued or slow
      render always answers with the renderer's real verdict instead of timing
      out against it.
    """
    assert (
        REMOTE_AVAILABILITY_TIMEOUT_S + TEMPLATE_TIMEOUT_S + REMOTE_RENDER_BUDGET_S + PRESS_MARGIN_S
        < DEFAULT_SEAM_TIMEOUT_S
    )
    assert REMOTE_RENDER_BUDGET_S == TTS_RENDER_TOTAL_TIMEOUT_S
    assert (
        RENDER_QUEUE_WAIT_S + RENDER_SYNTHESIS_TIMEOUT_S
        <= REMOTE_RENDER_BUDGET_S - RENDER_CLIENT_HEADROOM_S
    )
