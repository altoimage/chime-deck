"""Two real listeners on loopback ports, and the doorbell on both (HSD-n6wi).

This is the tier the in-process tests cannot cover: two ``uvicorn.Server``
instances on one event loop, one application, one lifespan, exactly as the CLI
runs them. Everything is bound to ``127.0.0.1`` on port 0 — the ports come from
the kernel — and every wait is bounded by ``asyncio.wait_for``. The teardown
also asserts that neither port still accepts connections afterwards, so a stray
listener cannot survive the test.

The loopback listener is seeded with a password **through its own open API**,
which is what makes the two views comparable: the same running application serves
the dashboard without a credential and redirects it on the admin port.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
import uvicorn

from chime_deck.admin import create_admin_app
from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig
from chime_deck.serving import build_configs

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 10.0
_REQUEST_TIMEOUT_S = 5.0
PASSWORD = "correct horse battery staple"

_INDEX_HTML = (
    '<!doctype html><html lang="en"><body><div id="app"></div>'
    '<script src="/assets/index-abc.js"></script></body></html>'
)


def _bind() -> socket.socket:
    """Return a TCP socket bound to an ephemeral loopback port, not yet listening.

    :raises OSError: if no loopback port can be bound, which is a real failure.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


def _build(tmp_path: Path) -> tuple[uvicorn.Config, uvicorn.Config]:
    """Build the two uvicorn configs the CLI would build.

    :param tmp_path: pytest temporary directory.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    config = ServerConfig(
        data_dir=tmp_path / "data",
        static_dir=dist,
        host="127.0.0.1",
        port=0,
        admin_host="127.0.0.1",
        admin_port=0,
        seed_demo_sounds=False,
    )
    app = create_app(config)
    state = cast(AppState, app.state.doorbell)
    admin_app = create_admin_app(app, gate=state.admin)
    doorbell_config, admin_config = build_configs(config, app, admin_app=admin_app)
    return doorbell_config, admin_config


async def _wait_started(servers: list[uvicorn.Server], tasks: list[asyncio.Task[None]]) -> None:
    """Wait until every server is accepting, bounded, failing fast on an early exit.

    :param servers: the servers that must report ``started``.
    :param tasks: their ``serve()`` tasks, so an early exit is noticed.
    :raises TimeoutError: if they do not start within the budget.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _START_TIMEOUT_S
    while not all(server.started for server in servers):
        for task in tasks:
            if task.done():
                raise RuntimeError(f"uvicorn exited before starting: {task.exception()!r}")
        if loop.time() > deadline:
            raise TimeoutError(f"uvicorn did not start within {_START_TIMEOUT_S} s")
        await asyncio.sleep(0.01)


async def _wait_closed(port: int) -> None:
    """Assert that nothing listens on *port* any more.

    A refused connection is the proof; anything else is a stray listener that
    outlived the test.

    :param port: port that must be closed.
    :raises AssertionError: if the port still accepts connections.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STOP_TIMEOUT_S
    while True:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=1.0
            )
        except (ConnectionError, OSError):
            return
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        if loop.time() > deadline:
            raise AssertionError(f"port {port} is still accepting connections")
        await asyncio.sleep(0.05)


@asynccontextmanager
async def _two_listeners(tmp_path: Path) -> AsyncIterator[tuple[int, int]]:
    """Serve the doorbell and the admin app on two loopback ports.

    :param tmp_path: pytest temporary directory.
    """
    doorbell_config, admin_config = _build(tmp_path)
    configs = [doorbell_config, admin_config]
    sockets = [_bind() for _ in configs]
    ports = [cast(int, sock.getsockname()[1]) for sock in sockets]
    servers = [uvicorn.Server(config) for config in configs]
    tasks = [
        asyncio.create_task(server.serve(sockets=[sock]))
        for server, sock in zip(servers, sockets, strict=True)
    ]
    try:
        await asyncio.wait_for(_wait_started(servers, tasks), timeout=_START_TIMEOUT_S)
        yield ports[0], ports[1]
    finally:
        for server in servers:
            server.should_exit = True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=_STOP_TIMEOUT_S
            )
        try:
            for port in ports:
                await _wait_closed(port)
        finally:
            for sock in sockets:
                sock.close()


async def test_both_listeners_serve_one_application(tmp_path: Path) -> None:
    """The admin port serves settings only; the loopback port serves everything.

    :param tmp_path: pytest temporary directory.
    """
    async with _two_listeners(tmp_path) as (loopback_port, admin_port):
        loopback_url = f"http://127.0.0.1:{loopback_port}"
        admin_url = f"http://127.0.0.1:{admin_port}"

        async with (
            httpx.AsyncClient(base_url=loopback_url, timeout=_REQUEST_TIMEOUT_S) as loopback,
            httpx.AsyncClient(base_url=admin_url, timeout=_REQUEST_TIMEOUT_S) as admin,
        ):
            # The loopback listener is open: the first password needs no credential,
            # which is exactly the fresh-install path the wizard uses.
            seeded = await loopback.post("/api/auth/password", json={"new": PASSWORD})
            assert seeded.status_code == 200

            dashboard = await loopback.get("/")
            loopback_api = await loopback.get("/api/settings")

            root = await admin.get("/")
            shell = await admin.get("/settings")
            gated = await admin.get("/api/settings")

            login = await admin.post("/api/auth/login", json={"password": PASSWORD})
            unlocked = await admin.get("/api/settings")
            stream = await admin.get("/ws")

        assert dashboard.status_code == 200
        assert 'id="app"' in dashboard.text
        assert loopback_api.status_code == 200

        assert root.status_code == 302
        assert root.headers["location"] == "/settings"
        assert shell.status_code == 200
        assert 'id="app"' in shell.text
        assert gated.status_code == 401
        assert login.status_code == 200
        assert unlocked.status_code == 200
        assert unlocked.json()["settings"]["audio.volume"] == 100
        assert stream.status_code == 404
        assert PASSWORD not in unlocked.text
