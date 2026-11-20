"""The renderer's HTTP contract: one token-gated endpoint, bounded in every direction.

Every test drives the real renderer app through ``httpx.ASGITransport`` (never a
live socket), with one fake ``piper`` engine per test so a call count is
observable. The order the route promises is the point of most of these tests:
authentication before the body, the body before an engine, and a bounded queue
around every synthesis. Nothing here reads the network, a real engine or a real
clock.

Owning ticket: HSD-5de8.
"""

from __future__ import annotations

import asyncio
import io
import time
import wave
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state

import chime_deck.tts.templating as templating_module
from chime_deck.api import install_error_handlers
from chime_deck.api.render import (
    PRESERVED_ORTHOGRAPHIC_CONTROLS,
    build_render_router,
)
from chime_deck.auth import bearer_token
from chime_deck.config import ServerConfig
from chime_deck.renderer import (
    RENDER_MAX_BODY_BYTES,
    RENDER_QUEUE_MAX,
    RENDER_QUEUE_WAIT_S,
    RENDER_RATE_LIMIT_PER_MINUTE,
    RENDER_RATE_WINDOW_S,
    RENDER_SYNTHESIS_TIMEOUT_S,
    RendererState,
    RenderQueue,
    RenderRateLimiter,
    create_renderer_app,
)
from chime_deck.tts import Availability, TtsOptions, TtsRenderError

pytestmark = pytest.mark.asyncio

TOKEN = "renderer-token-0123456789abcdef"
"""Bearer token the renderer under test is configured with."""

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_ROUTE = "/api/tts/render"
"""The one action route under test."""

_BODY: dict[str, object] = {"text": "Hello", "engine": "piper"}
"""The smallest well-formed render request."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Keep the root logger and the registered secrets isolated per test."""
    with preserve_log_state():
        yield


def _wav(frames: int = 80, rate: int = 8000) -> bytes:
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


class _Clock:
    """A monotonic clock the test moves by hand, so no rate-limit test sleeps."""

    def __init__(self, now: float) -> None:
        """Start the clock at *now*.

        :param now: initial reading, in seconds.
        """
        self._now = now

    def __call__(self) -> float:
        """Return the current reading."""
        return self._now

    def set(self, now: float) -> None:
        """Move the clock to *now*.

        :param now: new reading, in seconds.
        """
        self._now = now


class FakePiper:
    """A ``piper`` engine whose probe, render and overlap are all observable."""

    name = "piper"

    def __init__(
        self,
        *,
        availability: Availability | None = None,
        error: Exception | None = None,
        payload: bytes | None = None,
        gate: asyncio.Event | None = None,
        started: asyncio.Event | None = None,
        sleep_s: float | None = None,
    ) -> None:
        """Create the engine.

        :param availability: answer to :meth:`available`; available by default.
        :param error: exception raised by :meth:`render`, or ``None`` to succeed.
        :param payload: bytes written to the destination, or ``None`` for a real WAV.
        :param gate: event :meth:`render` parks on once it has started.
        :param started: event :meth:`render` sets before it parks.
        :param sleep_s: seconds :meth:`render` sleeps before writing.
        """
        self._availability = Availability(True) if availability is None else availability
        self._error = error
        self._payload = payload
        self._gate = gate
        self._started = started
        self._sleep_s = sleep_s
        self.available_calls = 0
        self.renders = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.texts: list[str] = []

    async def available(self) -> Availability:
        """Record the probe and report the configured availability."""
        self.available_calls += 1
        return self._availability

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Record the render, then write, park, sleep or raise as configured.

        The overlap counter is what proves the renderer serialises synthesis: two
        concurrent calls would make :attr:`max_concurrent` two.

        :param text: phrase to speak.
        :param options: options for this render.
        :param destination: exact output path.
        """
        self.renders += 1
        self.texts.append(text)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self._started is not None:
                self._started.set()
            if self._gate is not None:
                await self._gate.wait()
            if self._sleep_s is not None:
                await asyncio.sleep(self._sleep_s)
            if self._error is not None:
                raise self._error
            destination.write_bytes(self._payload if self._payload is not None else _wav())
            return destination
        finally:
            self.concurrent -= 1

    async def warmup(self) -> None:
        """Do nothing; there is nothing to prime."""
        return None


def _build_renderer(
    tmp_path: Path,
    engine: FakePiper,
    *,
    clock: Callable[[], float] = time.monotonic,
    rate_limit: int = RENDER_RATE_LIMIT_PER_MINUTE,
    rate_window_s: float = RENDER_RATE_WINDOW_S,
    queue_max: int = RENDER_QUEUE_MAX,
    queue_wait_s: float = RENDER_QUEUE_WAIT_S,
    synthesis_timeout_s: float = RENDER_SYNTHESIS_TIMEOUT_S,
) -> FastAPI:
    """Build the renderer app around one fake engine and a temporary spool.

    :param tmp_path: pytest temporary directory.
    :param engine: engine the app hosts.
    :param clock: monotonic clock for the limiter, uptime and the render timer.
    :param rate_limit: requests allowed per source address per window.
    :param rate_window_s: rate-limit window length in seconds.
    :param queue_max: most renders in flight at once.
    :param queue_wait_s: seconds a request may wait for the synthesis lock.
    :param synthesis_timeout_s: seconds the render itself may take.
    """
    config = ServerConfig(data_dir=tmp_path, static_dir=tmp_path / "no-dist")
    return create_renderer_app(
        config,
        token=TOKEN,
        engines=(engine,),
        spool_dir=tmp_path / "spool",
        clock=clock,
        rate_limit=rate_limit,
        rate_window_s=rate_window_s,
        queue_max=queue_max,
        queue_wait_s=queue_wait_s,
        synthesis_timeout_s=synthesis_timeout_s,
    )


def _bare_router(tmp_path: Path, engine: FakePiper, *, token: bytes) -> FastAPI:
    """Build an app from a hand-built renderer state, with no ``create_renderer_app``.

    Hand-building is what lets a test set a token the factory would refuse to
    serve with (``b""``), so the route's own refusal is what is exercised.

    :param tmp_path: pytest temporary directory.
    :param engine: engine the router hosts under the name ``piper``.
    :param token: already-encoded configured token; ``b""`` is the fail-closed case.
    """
    (tmp_path / "spool").mkdir(parents=True, exist_ok=True)
    state = RendererState(
        token=token,
        engines={engine.name: engine},
        hosted=engine.name,
        spool_dir=tmp_path / "spool",
        limiter=RenderRateLimiter(),
        queue=RenderQueue(),
        queue_wait_s=1.0,
        synthesis_timeout_s=1.0,
        clock=time.monotonic,
        version="test",
        started_monotonic=0.0,
        synthesis_lock=asyncio.Lock(),
    )
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(build_render_router(state))
    return app


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield a client bound to it.

    :param app: application under test.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=_HTTP_TIMEOUT_S
        ) as client:
            yield client


def _auth(token: str = TOKEN) -> dict[str, str]:
    """Return an ``Authorization`` header carrying *token*.

    :param token: bearer token to send.
    """
    return {"Authorization": f"Bearer {token}"}


def _error(response: httpx.Response) -> dict[str, object]:
    """Return the standard error envelope's ``error`` object.

    :param response: failed response.
    """
    body = cast("dict[str, object]", response.json())
    return cast("dict[str, object]", body["error"])


def _detail(response: httpx.Response) -> dict[str, object]:
    """Return the standard error envelope's structured detail object.

    :param response: failed response.
    """
    return cast("dict[str, object]", _error(response)["detail"])


def _state(app: FastAPI) -> RendererState:
    """Return the renderer state the app was built with.

    :param app: renderer application under test.
    """
    return cast(RendererState, app.state.renderer)


async def test_a_valid_request_returns_a_wav_and_clears_the_spool(tmp_path: Path) -> None:
    """A token, a phrase and a hosted engine produce a real WAV and leave no file behind.

    :param tmp_path: pytest temporary directory.
    """
    engine = FakePiper()
    app = _build_renderer(tmp_path, engine)

    async with _running(app) as client:
        response = await client.post(
            _ROUTE,
            headers=_auth(),
            json={"text": "Hello", "engine": "piper", "options": {"preferred_format": "wav"}},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-hsd-engine"] == "piper"
    with wave.open(io.BytesIO(response.content)) as reader:
        assert reader.getnframes() > 0
    assert engine.available_calls == 1
    assert engine.renders == 1
    assert list((tmp_path / "spool").iterdir()) == []


@pytest.mark.parametrize(
    ("label", "headers", "submitted"),
    [
        ("absent", {}, ""),
        ("wrong", {"Authorization": "Bearer wrong-submitted-token"}, "wrong-submitted-token"),
        ("basic", {"Authorization": "Basic abc"}, "abc"),
        ("case", {"Authorization": f"Bearer {TOKEN.upper()}"}, TOKEN.upper()),
    ],
)
async def test_a_rejected_bearer_touches_no_engine(
    tmp_path: Path, label: str, headers: dict[str, str], submitted: str
) -> None:
    """Every wrong credential is a 401 that never probes, never renders and never echoes a value.

    ``label`` names the shape of the failure in the test id; ``submitted`` is the
    credential the response must not repeat, and the configured token must never
    appear either.

    :param tmp_path: pytest temporary directory.
    :param label: short name of the credential shape under test.
    :param headers: request headers carrying the rejected credential.
    :param submitted: credential text that must not be echoed; ``""`` when absent.
    """
    engine = FakePiper()
    app = _build_renderer(tmp_path, engine)

    async with _running(app) as client:
        response = await client.post(_ROUTE, headers=headers, json=_BODY)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert _error(response)["code"] == "unauthorized"
    assert TOKEN not in response.text
    if submitted:
        assert submitted not in response.text
    assert engine.available_calls == 0
    assert engine.renders == 0


async def test_a_wrong_token_is_refused_before_the_body_is_validated(tmp_path: Path) -> None:
    """A 600-character body under a wrong token is still a 401: authentication comes first.

    :param tmp_path: pytest temporary directory.
    """
    engine = FakePiper()
    app = _build_renderer(tmp_path, engine)

    async with _running(app) as client:
        response = await client.post(
            _ROUTE,
            headers={"Authorization": "Bearer not-the-configured-token"},
            json={"text": "a" * 600, "engine": "piper"},
        )

    assert response.status_code == 401
    assert engine.available_calls == 0
    assert engine.renders == 0


async def test_an_empty_configured_token_fails_closed(tmp_path: Path) -> None:
    """An empty configured token refuses every credential, and the refusal is the only reason.

    The router is built by hand over a ``RendererState`` whose token is ``b""``,
    so the fail-closed branch is the sole thing under test. One engine is
    hosted and available, so a single probe or render would be visible. The
    positive control builds the very same hand-built router around the real
    token and gets a ``200``: without it, a router that refused everything would
    pass this test too.

    :param tmp_path: pytest temporary directory.
    """
    # The mechanism behind "fail closed": a credential-less header is ``None``
    # and never ``b""``, so an empty configured token cannot be matched by an
    # empty presented one.
    assert bearer_token(None) is None
    assert bearer_token("Bearer ") is None

    engine = FakePiper()
    app = _bare_router(tmp_path, engine, token=b"")

    async with _running(app) as client:
        absent = await client.post(_ROUTE, json=_BODY)
        empty = await client.post(_ROUTE, headers={"Authorization": "Bearer "}, json=_BODY)
        anything = await client.post(
            _ROUTE, headers={"Authorization": "Bearer anything"}, json=_BODY
        )
        capitalised = await client.post(
            _ROUTE, headers={"Authorization": f"bearer {TOKEN.upper()}"}, json=_BODY
        )

    refused = {
        "absent": absent,
        "empty credential": empty,
        "wrong credential": anything,
        "capitalised token": capitalised,
    }
    for label, response in refused.items():
        assert response.status_code == 401, label
        assert response.headers["www-authenticate"] == "Bearer", label
        assert _error(response)["code"] == "unauthorized", label
        assert TOKEN not in response.text, label
    assert engine.available_calls == 0
    assert engine.renders == 0

    # The positive control: the same hand-built router, the same engine, one real
    # token. Only the configured token differs, so the refusals above are the
    # fail-closed branch and nothing else.
    control_app = _bare_router(tmp_path, engine, token=TOKEN.encode())
    async with _running(control_app) as client:
        control = await client.post(_ROUTE, headers=_auth(), json=_BODY)

    assert control.status_code == 200
    assert control.headers["x-hsd-engine"] == "piper"
    assert engine.available_calls == 1
    assert engine.renders == 1


async def test_a_lone_surrogate_in_a_credential_is_refused_not_raised(tmp_path: Path) -> None:
    """A corrupt credential fails closed: it is encoded and then refused, never raised.

    A lone surrogate cannot travel as a ``str`` header (``httpx`` refuses to
    encode one) and ASGI hands headers to the app decoded as latin-1, so both
    forms a renderer can actually meet are exercised here: the credential helper
    on the surrogate string, and the header's WTF-8 bytes through the app. A
    ``500`` out of the authentication check is the failure this pins against.

    :param tmp_path: pytest temporary directory.
    """
    assert bearer_token("Bearer \ud800") == b"\xed\xa0\x80"

    engine = FakePiper()
    app = _bare_router(tmp_path, engine, token=b"")

    async with _running(app) as client:
        response = await client.post(
            _ROUTE, headers={b"authorization": b"Bearer \xed\xa0\x80"}, json=_BODY
        )

    assert response.status_code == 401
    assert _error(response)["code"] == "unauthorized"
    assert engine.available_calls == 0
    assert engine.renders == 0


@pytest.mark.parametrize(
    ("label", "content"),
    [
        ("over the body cap", b'{"text": "' + b"a" * RENDER_MAX_BODY_BYTES + b'"}'),
        ("not json", b"this is not a JSON body at all"),
    ],
)
async def test_a_wrong_token_is_refused_before_the_body_is_read(
    tmp_path: Path, label: str, content: bytes
) -> None:
    """Authentication precedes the body, so a wrong token is a 401 and never a 422 or a 500.

    The body is hostile in both directions on purpose: one is over the read cap
    and one is not JSON at all, so a ``422`` would mean the renderer validated a
    body for a caller it had not authenticated yet.

    :param tmp_path: pytest temporary directory.
    :param label: short name of the hostile body in the test id.
    :param content: raw request body bytes.
    """
    engine = FakePiper()
    app = _build_renderer(tmp_path, engine)

    async with _running(app) as client:
        response = await client.post(
            _ROUTE,
            headers={
                "Authorization": "Bearer not-the-configured-token",
                "content-type": "application/json",
            },
            content=content,
        )

    assert response.status_code == 401, label
    assert _error(response)["code"] == "unauthorized"
    assert engine.available_calls == 0
    assert engine.renders == 0


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("empty text", {"text": "", "engine": "piper"}),
        ("whitespace text", {"text": "   ", "engine": "piper"}),
        ("too long", {"text": "a" * 501, "engine": "piper"}),
        ("nul", {"text": "a\x00b", "engine": "piper"}),
        ("escape", {"text": "a\x1b[31mb", "engine": "piper"}),
        ("rtl override", {"text": "\u202e", "engine": "piper"}),
        ("bom", {"text": "\ufeff", "engine": "piper"}),
        ("array", []),
        ("text wrong type", {"text": 5, "engine": "piper"}),
        ("engine wrong type", {"engine": 5, "text": "x"}),
        ("options wrong type", {"options": [], "text": "x"}),
        ("ha engine", {"text": "x", "engine": "ha_tts"}),
        ("remote engine", {"text": "x", "engine": "remote_tts"}),
        ("none engine", {"text": "x", "engine": "none"}),
        ("no engine", {"text": "x"}),
        ("empty engine", {"text": "x", "engine": ""}),
        ("voice flag", {"text": "x", "engine": "piper", "options": {"voice": "--help"}}),
        ("voice short flag", {"text": "x", "engine": "piper", "options": {"voice": "-v"}}),
        (
            "voice path",
            {"text": "x", "engine": "piper", "options": {"voice": "../../etc/passwd"}},
        ),
        ("speed low", {"text": "x", "engine": "piper", "options": {"speed": 79}}),
        ("speed text", {"text": "x", "engine": "piper", "options": {"speed": "fast"}}),
        ("speed high", {"text": "x", "engine": "piper", "options": {"speed": 100000}}),
        ("speed bool", {"text": "x", "engine": "piper", "options": {"speed": True}}),
        (
            "format mp3",
            {"text": "x", "engine": "piper", "options": {"preferred_format": "mp3"}},
        ),
        (
            "voice no underscore",
            {"text": "x", "engine": "piper", "options": {"voice": "en-us"}},
        ),
        ("voice plus sign", {"text": "x", "engine": "piper", "options": {"voice": "a+b"}}),
    ],
)
async def test_a_bad_request_is_422_and_touches_no_engine(
    tmp_path: Path, label: str, body: object
) -> None:
    """Every malformed, hostile or unsupported request is a 422 that never reaches an engine.

    ``label`` names the case in the test id; the engine is the same fake ``piper``
    in every case, so a single probe or render would show up as a failure.

    :param tmp_path: pytest temporary directory.
    :param label: short name of the rejected case.
    :param body: decoded JSON body to send.
    """
    engine = FakePiper()
    app = _build_renderer(tmp_path, engine)

    async with _running(app) as client:
        response = await client.post(_ROUTE, headers=_auth(), json=body)

    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"
    assert engine.available_calls == 0
    assert engine.renders == 0


@pytest.mark.parametrize(
    ("label", "content"),
    [
        ("invalid json", b"not json"),
        ("not utf-8", b'{"text": "\xff"}'),
        (
            "over the body cap",
            b'{"text": "' + b"a" * RENDER_MAX_BODY_BYTES + b'"}',
        ),
    ],
)
async def test_a_bad_body_is_422_and_touches_no_engine(
    tmp_path: Path, label: str, content: bytes
) -> None:
    """A body that is not UTF-8 JSON, or is over the read cap, is a 422 that reaches no engine.

    :param tmp_path: pytest temporary directory.
    :param label: short name of the rejected body.
    :param content: raw request body bytes.
    """
    engine = FakePiper()
    app = _build_renderer(tmp_path, engine)

    async with _running(app) as client:
        response = await client.post(
            _ROUTE,
            headers={**_auth(), "content-type": "application/json"},
            content=content,
        )

    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"
    assert engine.available_calls == 0
    assert engine.renders == 0


async def test_a_chunked_body_over_the_cap_is_422_and_touches_no_engine(tmp_path: Path) -> None:
    """A chunked body that declares no length is stopped by the running byte count.

    Without a ``Content-Length`` the header pre-check can refuse nothing, so only
    the count kept while streaming stands between the renderer and a body it
    would otherwise buffer. The body is a *valid* request with one enormous
    ignored field, so a renderer that dropped the count would answer ``200`` and
    render: a body that failed validation anyway could not prove the cap.

    :param tmp_path: pytest temporary directory.
    """
    engine = FakePiper()
    app = _build_renderer(tmp_path, engine)
    padding = b'"padding": "' + b"a" * RENDER_MAX_BODY_BYTES + b'"}'
    body_bytes = b'{"text": "Hello", "engine": "piper", ' + padding
    assert len(body_bytes) > RENDER_MAX_BODY_BYTES
    chunk_size = 4096

    async def body() -> AsyncIterator[bytes]:
        """Yield the request body in chunks, so no length is declared."""
        for start in range(0, len(body_bytes), chunk_size):
            yield body_bytes[start : start + chunk_size]

    async with _running(app) as client:
        response = await client.post(
            _ROUTE,
            headers={**_auth(), "content-type": "application/json"},
            content=body(),
        )

    assert "content-length" not in response.request.headers
    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"
    assert engine.available_calls == 0
    assert engine.renders == 0


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("zwnj", {"text": "door\u200cbell", "engine": "piper"}),
        ("speed zero", {"text": "Hello", "engine": "piper", "options": {"speed": 0}}),
        ("empty voice", {"text": "Hello", "engine": "piper", "options": {"voice": ""}}),
        ("unknown option", {"text": "Hello", "engine": "piper", "options": {"future": 1}}),
        (
            "explicit wav",
            {"text": "Hello", "engine": "piper", "options": {"preferred_format": "wav"}},
        ),
    ],
)
async def test_accepted_edge_cases_still_render(
    tmp_path: Path, label: str, body: dict[str, object]
) -> None:
    """An orthographic joiner, an absent-looking option and a future key are all accepted.

    ``speed: 0`` and ``voice: ""`` mean "not an override", and an unknown option
    is ignored so a newer client cannot break an older renderer.

    :param tmp_path: pytest temporary directory.
    :param label: short name of the accepted case.
    :param body: decoded JSON body to send.
    """
    engine = FakePiper()
    app = _build_renderer(tmp_path, engine)

    async with _running(app) as client:
        response = await client.post(_ROUTE, headers=_auth(), json=body)

    assert response.status_code == 200
    assert engine.renders == 1


@pytest.mark.parametrize("voice", ["en-us", "a+b"])
async def test_a_voice_that_is_no_piper_model_id_is_refused_by_name(
    tmp_path: Path, voice: str
) -> None:
    """A voice past the generic token rule is rejected by the Piper rule, and the sentence says so.

    Both values here are plain option tokens, so only the engine's own
    :func:`~chime_deck.tts.piper.voice_id_problem` can refuse them, and the
    answer names the setting that fixes it.

    :param tmp_path: pytest temporary directory.
    :param voice: voice id that is a valid token but not a Piper model name.
    """
    engine = FakePiper()
    app = _build_renderer(tmp_path, engine)

    async with _running(app) as client:
        response = await client.post(
            _ROUTE,
            headers=_auth(),
            json={"text": "x", "engine": "piper", "options": {"voice": voice}},
        )

    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"
    message = cast("str", _error(response)["message"])
    assert "not a valid voice id" in message
    assert "tts.piper.voice" in message
    assert engine.available_calls == 0
    assert engine.renders == 0


async def test_the_rate_limiter_reclaims_expired_windows_before_evicting_a_live_one() -> None:
    """With the map at its ceiling, expired windows are dropped before any live entry is.

    The limiter is driven directly, with a hand-moved clock, so the ceiling is
    provable without a thousand addresses. If the expired entries were not
    reclaimed first, the insertion of ``d`` would evict the live ``a`` and the
    map would still hold three entries.
    """
    clock = _Clock(10.0)
    limiter = RenderRateLimiter(limit=2, max_keys=3, clock=clock)

    for key in ("a", "b", "c"):
        assert limiter.allow(key) is None
        assert len(limiter._windows) <= 3
    assert list(limiter._windows) == ["a", "b", "c"]

    clock.set(71.0)
    assert limiter.allow("d") is None

    assert list(limiter._windows) == ["d"]


async def test_the_rate_limiter_evicts_the_soonest_closing_entry_at_its_ceiling() -> None:
    """A full map of live windows evicts the one that closes soonest, and never grows.

    Three addresses fill the ceiling with windows ending at 70, 80 and 90 s; the
    fourth arrives before any of them expires, so ``a`` — the soonest to close —
    is the entry that makes room.
    """
    clock = _Clock(10.0)
    limiter = RenderRateLimiter(limit=2, max_keys=3, clock=clock)

    for offset, key in enumerate(("a", "b", "c")):
        clock.set(10.0 + 10.0 * offset)
        assert limiter.allow(key) is None
        assert len(limiter._windows) <= 3

    clock.set(40.0)
    assert limiter.allow("d") is None

    assert list(limiter._windows) == ["b", "c", "d"]
    assert len(limiter._windows) <= 3


async def test_a_third_request_from_one_address_is_rate_limited(tmp_path: Path) -> None:
    """Two requests per window are allowed; the third is 429 and another address still works.

    :param tmp_path: pytest temporary directory.
    """
    engine = FakePiper()
    app = _build_renderer(tmp_path, engine, rate_limit=2, rate_window_s=60.0, clock=lambda: 1000.0)

    async with _running(app) as client:
        first = await client.post(_ROUTE, headers=_auth(), json=_BODY)
        second = await client.post(_ROUTE, headers=_auth(), json=_BODY)
        limited = await client.post(_ROUTE, headers=_auth(), json=_BODY)

        other_transport = httpx.ASGITransport(app=app, client=("10.0.0.9", 1))
        async with httpx.AsyncClient(
            transport=other_transport, base_url="http://test", timeout=_HTTP_TIMEOUT_S
        ) as other:
            still_allowed = await other.post(_ROUTE, headers=_auth(), json=_BODY)

    assert first.status_code == 200
    assert second.status_code == 200
    assert limited.status_code == 429
    assert _error(limited)["code"] == "rate_limited"
    assert _detail(limited)["reason"] == "rate_limited"
    assert int(limited.headers["retry-after"]) >= 1

    assert still_allowed.status_code == 200


async def test_a_forwarded_for_header_cannot_buy_a_fresh_window(tmp_path: Path) -> None:
    """``X-Forwarded-For`` is ignored: four peer addresses in one header still hit the limit.

    The header is client-supplied, so honouring it would let one caller claim a
    fresh identity per request; the third and fourth requests from the same TCP
    peer must therefore be limited even though each claims a new address.

    :param tmp_path: pytest temporary directory.
    """
    engine = FakePiper()
    app = _build_renderer(tmp_path, engine, rate_limit=2, rate_window_s=60.0, clock=lambda: 1000.0)

    async with _running(app) as client:
        responses = [
            await client.post(
                _ROUTE, headers={**_auth(), "x-forwarded-for": f"10.0.0.{n}"}, json=_BODY
            )
            for n in range(1, 5)
        ]

    assert [response.status_code for response in responses] == [200, 200, 429, 429]
    for limited in responses[2:]:
        assert _error(limited)["code"] == "rate_limited"
        assert _detail(limited)["reason"] == "rate_limited"
        assert int(limited.headers["retry-after"]) >= 1


async def test_a_full_queue_is_refused_and_reports_empty(tmp_path: Path) -> None:
    """While one render is parking, a second request is 429 ``queue_full`` and the counter drains.

    :param tmp_path: pytest temporary directory.
    """
    gate = asyncio.Event()
    started = asyncio.Event()
    engine = FakePiper(gate=gate, started=started)
    app = _build_renderer(tmp_path, engine, queue_max=1)
    state = _state(app)

    async with _running(app) as client:
        first = asyncio.create_task(client.post(_ROUTE, headers=_auth(), json=_BODY))
        await asyncio.wait_for(started.wait(), 5)
        second = asyncio.create_task(client.post(_ROUTE, headers=_auth(), json=_BODY))
        second_response = await asyncio.wait_for(second, 5)
        gate.set()
        first_response, _ = await asyncio.wait_for(asyncio.gather(first, second), 5)

    assert second_response.status_code == 429
    assert _error(second_response)["code"] == "queue_full"
    assert _detail(second_response)["reason"] == "queue_full"
    assert "retry-after" in second_response.headers
    assert first_response.status_code == 200
    assert state.queue.pending == 0


async def test_an_unknown_engine_is_422_even_when_the_queue_is_full(tmp_path: Path) -> None:
    """A permanently wrong engine name gets its own 422 while every slot is taken.

    The name cannot become renderable by retrying, so ``429 queue_full`` would be
    a lie about the fix; the lookup therefore runs before a slot is asked for.

    :param tmp_path: pytest temporary directory.
    """
    gate = asyncio.Event()
    started = asyncio.Event()
    engine = FakePiper(gate=gate, started=started)
    app = _build_renderer(tmp_path, engine, queue_max=1)
    state = _state(app)

    async with _running(app) as client:
        first = asyncio.create_task(client.post(_ROUTE, headers=_auth(), json=_BODY))
        await asyncio.wait_for(started.wait(), 5)
        unknown = await client.post(
            _ROUTE, headers=_auth(), json={"text": "Hello", "engine": "ha_tts"}
        )
        gate.set()
        (first_response,) = await asyncio.wait_for(asyncio.gather(first), 5)

    assert unknown.status_code == 422
    assert _error(unknown)["code"] == "validation_error"
    assert "ha_tts" in cast("str", _error(unknown)["message"])
    assert first_response.status_code == 200
    assert state.queue.pending == 0


async def test_a_request_that_waits_too_long_for_the_lock_is_refused(tmp_path: Path) -> None:
    """A lock wait that expires is 429 ``queue_wait`` because nothing was synthesised.

    :param tmp_path: pytest temporary directory.
    """
    gate = asyncio.Event()
    started = asyncio.Event()
    engine = FakePiper(gate=gate, started=started)
    app = _build_renderer(tmp_path, engine, queue_wait_s=0.01)
    state = _state(app)

    async with _running(app) as client:
        first = asyncio.create_task(client.post(_ROUTE, headers=_auth(), json=_BODY))
        await asyncio.wait_for(started.wait(), 5)
        second = asyncio.create_task(client.post(_ROUTE, headers=_auth(), json=_BODY))
        second_response = await asyncio.wait_for(second, 5)
        gate.set()
        first_response, _ = await asyncio.wait_for(asyncio.gather(first, second), 5)

    assert second_response.status_code == 429
    assert _error(second_response)["code"] == "queue_full"
    assert _detail(second_response)["reason"] == "queue_wait"
    assert first_response.status_code == 200
    assert state.queue.pending == 0
    assert list((tmp_path / "spool").iterdir()) == []


async def test_an_unavailable_engine_is_503_with_its_own_sentence(tmp_path: Path) -> None:
    """A hosted engine that cannot speak now answers 503, unchanged, and no render is attempted.

    :param tmp_path: pytest temporary directory.
    """
    sentence = "the piper voice model is missing — download it in Settings → Speech"
    engine = FakePiper(availability=Availability(False, sentence))
    app = _build_renderer(tmp_path, engine)
    state = _state(app)

    async with _running(app) as client:
        response = await client.post(_ROUTE, headers=_auth(), json=_BODY)

    assert response.status_code == 503
    assert _error(response)["code"] == "engine_unavailable"
    assert _error(response)["message"] == sentence
    assert _detail(response)["reason"] == "engine_unavailable"
    assert engine.renders == 0
    assert state.queue.pending == 0
    assert list((tmp_path / "spool").iterdir()) == []


async def test_a_failed_render_keeps_the_engine_sentence(tmp_path: Path) -> None:
    """A ``TtsRenderError`` is a 503 ``engine_failed`` whose message is the engine's own.

    :param tmp_path: pytest temporary directory.
    """
    sentence = "the piper engine produced no audio — check the renderer log"
    engine = FakePiper(error=TtsRenderError(sentence))
    app = _build_renderer(tmp_path, engine)
    state = _state(app)

    async with _running(app) as client:
        response = await client.post(_ROUTE, headers=_auth(), json=_BODY)

    assert response.status_code == 503
    assert _error(response)["code"] == "engine_unavailable"
    assert sentence in cast("str", _error(response)["message"])
    assert _detail(response)["reason"] == "engine_failed"
    assert state.queue.pending == 0
    assert list((tmp_path / "spool").iterdir()) == []


async def test_a_render_that_outlives_its_deadline_is_503_timeout(tmp_path: Path) -> None:
    """A sleeping engine is abandoned at the synthesis deadline and answered 503 ``timeout``.

    :param tmp_path: pytest temporary directory.
    """
    engine = FakePiper(sleep_s=5.0)
    app = _build_renderer(tmp_path, engine, synthesis_timeout_s=0.01)
    state = _state(app)

    async with _running(app) as client:
        response = await client.post(_ROUTE, headers=_auth(), json=_BODY)

    assert response.status_code == 503
    assert _detail(response)["reason"] == "timeout"
    assert state.queue.pending == 0
    assert list((tmp_path / "spool").iterdir()) == []


async def test_a_render_over_the_audio_cap_is_503_too_large(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file over the shared audio cap is refused; the cap is patched down so no 10 MB is written.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that lowers the rendered-file cap.
    """
    monkeypatch.setattr("chime_deck.api.render.MAX_AUDIO_BYTES", 100)
    engine = FakePiper(payload=b"\x00" * 200)
    app = _build_renderer(tmp_path, engine)
    state = _state(app)

    async with _running(app) as client:
        response = await client.post(_ROUTE, headers=_auth(), json=_BODY)

    assert response.status_code == 503
    assert _detail(response)["reason"] == "too_large"
    assert state.queue.pending == 0
    assert list((tmp_path / "spool").iterdir()) == []


async def test_synthesis_is_serialised_across_concurrent_requests(tmp_path: Path) -> None:
    """Three accepted requests render one at a time, so the engine never sees two at once.

    :param tmp_path: pytest temporary directory.
    """
    engine = FakePiper()
    app = _build_renderer(tmp_path, engine, queue_max=4)

    async with _running(app) as client:

        async def post() -> httpx.Response:
            """Send one accepted render request.

            The outer client is what makes the three requests share one loop and
            therefore one synthesis lock.
            """
            return await client.post(_ROUTE, headers=_auth(), json=_BODY)

        responses = await asyncio.wait_for(
            asyncio.gather(post(), post(), post()),
            5,
        )

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert engine.max_concurrent == 1
    assert engine.renders == 3


async def test_the_two_control_character_policies_cannot_drift() -> None:
    """The renderer keeps exactly the joiners templating keeps, pinned by reaching the private set.

    The renderer must accept every character the local policy preserves, and no
    more, so the two policies are compared directly. Accessing the private
    ``_PRESERVED_JOINERS`` is intentional and is the only way to pin the equality.
    """
    assert PRESERVED_ORTHOGRAPHIC_CONTROLS == templating_module._PRESERVED_JOINERS
