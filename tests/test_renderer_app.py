"""The renderer role's app factory: the token gate, its routes and the bind log.

Fully offline: the engines are fakes, the store is a real temporary SQLite database
in ``tmp_path`` and every bind is logged rather than opened. The renderer is the
one process in this project that accepts arbitrary text from the network, so
these tests pin the three app-level facts that make that safe — it refuses to
start without a token, it serves only the two routes it means to, and it says out
loud when it is reachable beyond loopback.

Owning ticket: HSD-5de8.
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
import wave
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state

from chime_deck import __version__
from chime_deck.app import create_app
from chime_deck.config import ServerConfig
from chime_deck.renderer import (
    RENDER_QUEUE_MAX,
    RENDER_RATE_LIMIT_PER_MINUTE,
    RENDER_SPOOL_MAX_AGE_S,
    RendererTokenMissing,
    build_renderer_engines,
    create_renderer_app,
    fetch_renderer_token,
    sweep_spool,
)
from chime_deck.store import ConfigStore
from chime_deck.tts import Availability, PiperEngine, TtsOptions

pytestmark = pytest.mark.asyncio

TOKEN = "renderer-token-0123456789abcdef"
"""Bearer token the renderer under test is configured with."""

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_RENDER_ROUTE = "/api/tts/render"
"""The renderer's one action route."""

_HEALTH_ROUTE = "/api/health"
"""The renderer's token-gated health probe."""


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


class FakeEngine:
    """A renderer engine that writes a real WAV and records how often it is used."""

    name = "piper"

    def __init__(self) -> None:
        """Create an idle engine that has not been probed, rendered or warmed up."""
        self.available_calls = 0
        self.renders = 0
        self.warmups = 0

    async def available(self) -> Availability:
        """Report available and record the probe."""
        self.available_calls += 1
        return Availability(True)

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Write a real WAV at *destination* and record the render.

        :param text: phrase to speak.
        :param options: options for this render.
        :param destination: exact output path.
        """
        self.renders += 1
        destination.write_bytes(_wav())
        return destination

    async def warmup(self) -> None:
        """Record the warmup."""
        self.warmups += 1


class _NoReadSettings:
    """A settings reader that fails the test if the app factory reads anything."""

    async def get_setting(self, key: str) -> object:
        """Fail: building the renderer engines must read no setting.

        :param key: setting key that must not be read.
        """
        raise AssertionError(f"building the renderer engines must not read {key!r}")


def _config(tmp_path: Path, *, host: str = "127.0.0.1", port: int = 8090) -> ServerConfig:
    """Build a renderer configuration under *tmp_path*.

    :param tmp_path: pytest temporary directory.
    :param host: bind address the log line describes.
    :param port: bind port the log line describes.
    """
    return ServerConfig(
        data_dir=tmp_path,
        static_dir=tmp_path / "no-dist",
        host=host,
        port=port,
    )


def _renderer_app(
    tmp_path: Path,
    *,
    token: str = TOKEN,
    engine: FakeEngine | None = None,
) -> FastAPI:
    """Build the renderer app on one fake engine and a temporary spool.

    :param tmp_path: pytest temporary directory.
    :param token: configured bearer token; ``""`` exercises the refusal.
    :param engine: engine to host; ``None`` builds a fresh :class:`FakeEngine`.
    """
    return create_renderer_app(
        _config(tmp_path),
        token=token,
        engines=(engine if engine is not None else FakeEngine(),),
        spool_dir=tmp_path / "spool",
    )


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


def _error_code(response: httpx.Response) -> str:
    """Return the standard error envelope's stable code.

    :param response: failed response.
    """
    body = cast("dict[str, object]", response.json())
    error = cast("dict[str, object]", body["error"])
    return cast(str, error["code"])


async def test_the_lifespan_refuses_an_empty_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A renderer with no token refuses to serve, names the database and never echoes a value.

    :param tmp_path: pytest temporary directory.
    :param caplog: log capture fixture.
    """
    config = _config(tmp_path)
    app = create_renderer_app(
        config,
        token="",
        engines=(FakeEngine(),),
        spool_dir=tmp_path / "spool",
    )

    with caplog.at_level(logging.ERROR), pytest.raises(RendererTokenMissing) as exc_info:
        async with app.router.lifespan_context(app):
            raise AssertionError("an empty token must not serve a single request")

    message = str(exc_info.value)
    assert str(config.database_path) in message
    assert "tts.renderer.token" in message
    # No submitted or configured value may appear, so the message can only name
    # the setting; a token-looking credential would be a leak.
    assert re.search(r"Bearer\s+\S{8,}", message) is None
    errors = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and str(config.database_path) in record.getMessage()
    ]
    assert len(errors) == 1
    assert "tts.renderer.token" in errors[0].getMessage()


async def test_the_lifespan_refuses_a_whitespace_token(tmp_path: Path) -> None:
    """A token made only of spaces is stripped away, so the renderer refuses to serve.

    This pins ``_token_bytes(token.strip())``: without the strip the configured
    value would be three spaces, the lifespan would start, and the endpoint would
    be guarded by a token nobody meant to set.

    :param tmp_path: pytest temporary directory.
    """
    config = _config(tmp_path)
    app = create_renderer_app(
        config,
        token="   ",
        engines=(FakeEngine(),),
        spool_dir=tmp_path / "spool",
    )

    with pytest.raises(RendererTokenMissing):
        async with app.router.lifespan_context(app):
            raise AssertionError("a whitespace token must not serve a single request")


async def test_the_lifespan_sweeps_the_spool_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One sweep runs at startup, against the configured spool directory.

    A renderer killed mid-render leaves its spool file behind, so the sweep is
    the only thing that bounds the directory's growth; patching the module's
    ``sweep_spool`` is what proves the lifespan calls it rather than something
    that merely looks like it.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that records the sweep instead of performing it.
    """
    calls: list[tuple[Path, dict[str, object]]] = []

    def _record(directory: Path, **options: object) -> int:
        """Record the sweep the lifespan asked for.

        :param directory: spool directory the lifespan swept.
        :param options: keyword arguments the lifespan passed.
        """
        calls.append((directory, dict(options)))
        return 0

    monkeypatch.setattr("chime_deck.renderer.sweep_spool", _record)
    spool = tmp_path / "render-spool"
    app = create_renderer_app(
        _config(tmp_path),
        token=TOKEN,
        engines=(FakeEngine(),),
        spool_dir=spool,
    )

    async with app.router.lifespan_context(app):
        pass

    assert [directory for directory, _options in calls] == [spool]
    assert calls[0][1]["max_age_s"] == RENDER_SPOOL_MAX_AGE_S


async def test_the_renderer_serves_only_its_token_gated_routes(tmp_path: Path) -> None:
    """A token opens the health probe; documentation, the SPA and the action route stay shut.

    :param tmp_path: pytest temporary directory.
    """
    app = _renderer_app(tmp_path)

    async with _running(app) as client:
        health = await client.get(_HEALTH_ROUTE, headers=_auth())
        rejected = await client.get(_HEALTH_ROUTE, headers=_auth("wrong-token"))
        docs = await client.get("/docs")
        redoc = await client.get("/redoc")
        openapi = await client.get("/openapi.json")
        root = await client.get("/")
        wrong_method = await client.get(_RENDER_ROUTE, headers=_auth())

    assert health.status_code == 200
    body = cast("dict[str, object]", health.json())
    assert set(body) == {"status", "role", "version", "uptime_s"}
    assert body["status"] == "ok"
    assert body["role"] == "renderer"
    assert body["version"] == __version__
    assert isinstance(body["uptime_s"], float)
    assert rejected.status_code == 401
    assert docs.status_code == 404
    assert redoc.status_code == 404
    assert openapi.status_code == 404
    assert root.status_code == 404
    assert wrong_method.status_code == 405
    assert _error_code(wrong_method) == "method_not_allowed"


async def test_the_renderer_policy_values_are_pinned() -> None:
    """The ticket's bounded request queue (max 4) and the chosen rate limit are fixed facts.

    Both numbers are policy rather than tuning knobs: the queue is the ticket's
    "bounded request queue (max 4)", and 30 renders per minute per address is the
    chosen rate limit. A change to either must fail a test, not slip through a
    review of a constant nobody re-read.
    """
    assert RENDER_QUEUE_MAX == 4
    assert RENDER_RATE_LIMIT_PER_MINUTE == 30


async def test_the_main_app_has_no_render_route(tmp_path: Path) -> None:
    """D20's unauthenticated doorbell and D24's token-gated renderer are different processes.

    The main app registers no render handler at all: a POST reaches only the
    GET-only SPA catch-all (``405``) and a GET is refused by the reserved-API
    guard (``404``). Neither answer can be the renderer's ``200`` or ``401``,
    which is the fact this pins.

    :param tmp_path: pytest temporary directory.
    """
    app = create_app(_config(tmp_path))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=_HTTP_TIMEOUT_S
    ) as client:
        posted = await client.post(
            _RENDER_ROUTE, headers=_auth(), json={"text": "Hello", "engine": "piper"}
        )
        fetched = await client.get(_RENDER_ROUTE, headers=_auth())

    assert posted.status_code == 405
    assert _error_code(posted) == "method_not_allowed"
    assert fetched.status_code == 404
    assert _error_code(fetched) == "not_found"


async def test_a_loopback_bind_is_logged_as_local_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A loopback renderer says so once, at INFO, and names the token's database.

    :param tmp_path: pytest temporary directory.
    :param caplog: log capture fixture.
    """
    config = _config(tmp_path, host="127.0.0.1", port=8090)
    app = create_renderer_app(
        config,
        token=TOKEN,
        engines=(FakeEngine(),),
        spool_dir=tmp_path / "spool",
    )

    with caplog.at_level(logging.INFO):
        async with app.router.lifespan_context(app):
            pass

    records = [record for record in caplog.records if "loopback only" in record.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert str(config.database_path) in records[0].getMessage()


async def test_a_lan_bind_is_logged_as_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Serving the LAN warns once, naming the token setting, the CPU cost and port forwarding.

    :param tmp_path: pytest temporary directory.
    :param caplog: log capture fixture.
    """
    config = _config(tmp_path, host="0.0.0.0", port=8090)
    app = create_renderer_app(
        config,
        token=TOKEN,
        engines=(FakeEngine(),),
        spool_dir=tmp_path / "spool",
    )

    with caplog.at_level(logging.WARNING):
        async with app.router.lifespan_context(app):
            pass

    records = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "renderer: listening" in record.getMessage()
    ]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "tts.renderer.token" in message
    assert "arbitrary text" in message
    assert "port-forwarded" in message


async def test_build_renderer_engines_builds_one_inert_piper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production renderer hosts exactly one engine, and building it imports and reads nothing.

    Nothing is warmed up either, so a renderer start costs no import and no
    ``/proc/meminfo`` read until a request actually needs them.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that makes every import, memory read and warmup fail.
    """

    def _forbidden_import(name: str) -> object:
        raise AssertionError(f"building the renderer engines must not import {name!r}")

    def _forbidden_meminfo(*args: object, **kwargs: object) -> object:
        raise AssertionError("building the renderer engines must not read memory")

    async def _forbidden_warmup(self: PiperEngine) -> None:
        raise AssertionError("building the renderer engines must not warm up")

    monkeypatch.setattr("chime_deck.tts.piper._import_module", _forbidden_import)
    monkeypatch.setattr("chime_deck.tts.piper.read_meminfo", _forbidden_meminfo)
    monkeypatch.setattr(PiperEngine, "warmup", _forbidden_warmup)

    engines = build_renderer_engines(_config(tmp_path), _NoReadSettings())

    assert len(engines) == 1
    assert engines[0].name == "piper"
    assert isinstance(engines[0], PiperEngine)


async def test_fetch_renderer_token_refuses_an_empty_token(tmp_path: Path) -> None:
    """An unset ``tts.renderer.token`` is the same fatal refusal as the lifespan's.

    :param tmp_path: pytest temporary directory.
    """
    config = _config(tmp_path)

    with pytest.raises(RendererTokenMissing) as exc_info:
        await fetch_renderer_token(config)

    message = str(exc_info.value)
    assert str(config.database_path) in message
    assert "tts.renderer.token" in message


async def test_fetch_renderer_token_returns_the_stored_value_stripped(tmp_path: Path) -> None:
    """The stored token is read from the database and surrounding whitespace is dropped.

    :param tmp_path: pytest temporary directory.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as store:
        await store.set_setting("tts.renderer.token", f"  {TOKEN}  ")

    assert await fetch_renderer_token(config) == TOKEN


async def test_sweep_spool_removes_only_stale_files(tmp_path: Path) -> None:
    """A file older than the age is removed, a fresh one is kept, and the count is returned.

    :param tmp_path: pytest temporary directory.
    """
    spool = tmp_path / "spool"
    spool.mkdir()
    stale = spool / ".render-stale.wav"
    fresh = spool / ".render-fresh.wav"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"new")
    aged = time.time() - 100
    os.utime(stale, (aged, aged))

    removed = sweep_spool(spool, max_age_s=10.0)

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()


async def test_sweep_spool_tolerates_a_missing_directory(tmp_path: Path) -> None:
    """A missing spool directory is zero removals rather than a startup failure.

    :param tmp_path: pytest temporary directory.
    """
    assert sweep_spool(tmp_path / "absent") == 0
