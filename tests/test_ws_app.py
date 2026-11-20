"""``/ws`` and ``/api/health`` over real loopback sockets.

The app runs on a uvicorn server inside this test's event loop, on a socket the
test binds itself, so the port is known and collisions are impossible. Nothing
here touches the network beyond ``127.0.0.1``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from ha_ws_fakes import TOKEN, FakeHaFactory
from log_isolation import preserve_log_state
from websockets.asyncio.client import ClientConnection, connect

from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig
from chime_deck.health import HealthStatus
from chime_deck.store import ConfigStore, EntityIn

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test.

    The application lifespan applies the stored ``system.log_level`` to the root
    logger, so the level has to be put back or later tests inherit it.
    ``preserve_log_state`` only closes the handlers this test added, so the ones
    uvicorn owns are left alone.
    """
    with preserve_log_state():
        yield


_START_TIMEOUT = 10.0
_RECV_TIMEOUT = 3.0


def _build_app(tmp_path: Path) -> tuple[FastAPI, AppState]:
    """Build an app with an empty database and no ``web/dist``.

    :param tmp_path: pytest temporary directory.
    """
    config = ServerConfig(
        data_dir=tmp_path, static_dir=tmp_path / "no-such-dist", seed_demo_sounds=False
    )
    app = create_app(config)
    return app, cast(AppState, app.state.doorbell)


@asynccontextmanager
async def _serving(app: FastAPI) -> AsyncIterator[int]:
    """Serve *app* on an ephemeral loopback port and yield that port.

    :param app: application to serve.
    """
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = cast(int, sock.getsockname()[1])

    config = uvicorn.Config(
        app,
        log_level="warning",
        log_config=None,
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        deadline = loop.time() + _START_TIMEOUT
        while not server.started:
            if task.done():
                raise RuntimeError(f"uvicorn exited before starting: {task.exception()!r}")
            if loop.time() > deadline:
                raise TimeoutError(f"uvicorn did not start within {_START_TIMEOUT} s")
            await asyncio.sleep(0.01)
        yield port
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=_START_TIMEOUT)
        sock.close()


def _decode(raw: str | bytes) -> dict[str, object]:
    """Decode one websocket frame into an envelope.

    :param raw: frame received from the websocket.
    """
    return cast(dict[str, object], json.loads(raw))


def _data(message: dict[str, object]) -> dict[str, object]:
    """Return the ``data`` payload of one envelope.

    :param message: decoded ``{"type": ..., "data": ...}`` message.
    """
    return cast(dict[str, object], message["data"])


async def test_health_over_real_socket(tmp_path: Path) -> None:
    """``/api/health`` answers valid JSON on a real port.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with (
        _serving(app) as port,
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as client,
    ):
        response = await client.get("/api/health")

    assert response.status_code == 200
    payload = cast(dict[str, object], response.json())
    assert set(payload) == {"status", "version", "uptime_s"}
    assert payload["status"] == "ok"
    assert isinstance(payload["uptime_s"], float)


async def test_websocket_snapshot_then_live_messages(tmp_path: Path) -> None:
    """The first frame is a snapshot; live state and config follow it.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with (
        _serving(app) as port,
        connect(f"ws://127.0.0.1:{port}/ws", open_timeout=5, close_timeout=1) as ws,
    ):
        snapshot = _decode(await asyncio.wait_for(ws.recv(), _RECV_TIMEOUT))

        assert snapshot["type"] == "snapshot"
        data = _data(snapshot)
        assert set(data) == {"entities", "buttons", "health", "config_version"}
        assert data["entities"] == []
        assert data["buttons"] == []
        # The lifespan now starts the injected offline bridge and speaker, so the
        # default install reports reconnecting (the fake broker never sends a
        # CONNACK) and ready, not the old idle not_configured/disabled pair. A
        # default install stores no ha.token either, so no Home Assistant client
        # is built and ``ha`` stays not_configured.
        assert data["health"] == {
            "ha": "not_configured",
            "mqtt": "reconnecting",
            "audio": "ready",
        }
        assert data["config_version"] == 0
        assert state.ha_websocket is None

        state.hub.notify_entity_state(
            entity_id="lock.front_door",
            state="unlocked",
            attributes={"battery_level": 91},
            last_changed="2026-01-01T00:00:00Z",
        )
        entity_message = _decode(await asyncio.wait_for(ws.recv(), _RECV_TIMEOUT))

        assert entity_message["type"] == "entity_state"
        assert _data(entity_message) == {
            "entity_id": "lock.front_door",
            "state": "unlocked",
            "attributes": {"battery_level": 91},
            "last_changed": "2026-01-01T00:00:00Z",
            "selected": False,
            "displayed_state": "Unlocked",
            "unit_of_measurement": None,
            "tone": "alert",
            "attribute_hints": [],
        }

        await state.store.set_setting("ui.theme", "light")
        config_message = _decode(await asyncio.wait_for(ws.recv(), _RECV_TIMEOUT))

        assert config_message == {"type": "config_changed", "data": {"what": "settings"}}
        assert state.hub.config_version == 1


async def _wait_for_health(websocket: ClientConnection, status: HealthStatus) -> None:
    """Read frames, bounded, until one reports *status* for ``ha``.

    :param websocket: connected ``/ws`` socket.
    :param status: status the ``health`` frame must report.
    :raises AssertionError: if no such frame arrives within 20 frames.
    """
    for _attempt in range(20):
        message = _decode(await asyncio.wait_for(websocket.recv(), _RECV_TIMEOUT))
        if message["type"] == "health" and _data(message).get("ha") == status.value:
            return
    raise AssertionError(f"no health frame reported {status.value!r} within 20 frames")


async def test_health_frames_report_a_tile_added_and_the_last_tile_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/ws`` reports ``connected`` for a new tile and ``no_entities`` after its delete.

    The client is built by the lifespan from the stored settings (only its socket
    is fake) because the health frame reads the app's own registry, which an
    injected client does not write through.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that installs the fake connection factory.
    """
    seed_config = ServerConfig(
        data_dir=tmp_path, static_dir=tmp_path / "no-such-dist", seed_demo_sounds=False
    )
    async with ConfigStore(seed_config.database_path) as seed:
        await seed.set_setting("ha.base_url", "http://homeassistant.local:8123")
        await seed.set_setting("ha.token", TOKEN)
    factory = FakeHaFactory()
    monkeypatch.setattr("chime_deck.ha.websocket.default_connection_factory", factory)
    app, state = _build_app(tmp_path)

    async with (
        _serving(app) as port,
        connect(f"ws://127.0.0.1:{port}/ws", open_timeout=5, close_timeout=1) as ws,
    ):
        snapshot = _decode(await asyncio.wait_for(ws.recv(), _RECV_TIMEOUT))
        assert snapshot["type"] == "snapshot"

        await state.store.upsert_entity(EntityIn(entity_id="lock.front_door"))
        await _wait_for_health(ws, HealthStatus.CONNECTED)

        stored = (await state.store.list_entities())[0]
        await state.store.delete_entity(stored.id)
        await _wait_for_health(ws, HealthStatus.NO_ENTITIES)


async def test_each_client_gets_its_own_snapshot(tmp_path: Path) -> None:
    """A second connection is independent of the first.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _serving(app) as port:
        url = f"ws://127.0.0.1:{port}/ws"
        async with (
            connect(url, open_timeout=5, close_timeout=1) as first,
            connect(url, open_timeout=5, close_timeout=1) as second,
        ):
            first_snapshot = _decode(await asyncio.wait_for(first.recv(), _RECV_TIMEOUT))
            second_snapshot = _decode(await asyncio.wait_for(second.recv(), _RECV_TIMEOUT))

            assert first_snapshot["type"] == "snapshot"
            assert second_snapshot["type"] == "snapshot"
            assert _data(first_snapshot)["config_version"] == 0
            assert _data(second_snapshot)["config_version"] == 0

            await state.store.set_setting("ui.theme", "light")

            # Both clients see the same change on their own queue.
            for websocket in (first, second):
                message = _decode(await asyncio.wait_for(websocket.recv(), _RECV_TIMEOUT))
                assert message == {"type": "config_changed", "data": {"what": "settings"}}


async def test_selection_toggle_reaches_two_connected_clients(tmp_path: Path) -> None:
    """``POST /api/tiles/{id}/selection`` frames out to both sockets, and the snapshot agrees.

    The tile is created over HTTP before any socket connects, so no
    ``config_changed`` frame races the snapshots; the frame search still tolerates
    one, because a background flush may deliver anything at any time.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _serving(app) as port:
        base_url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
            created = await client.post("/api/entities", json={"entity_id": "lock.front_door"})
            assert created.status_code == 201, created.text
            tile_id = cast(dict[str, object], created.json())["id"]

        url = f"ws://127.0.0.1:{port}/ws"
        async with (
            connect(url, open_timeout=5, close_timeout=1) as first,
            connect(url, open_timeout=5, close_timeout=1) as second,
        ):
            for websocket in (first, second):
                snapshot = _decode(await asyncio.wait_for(websocket.recv(), _RECV_TIMEOUT))
                assert snapshot["type"] == "snapshot"
                entities = cast("list[dict[str, object]]", _data(snapshot)["entities"])
                assert [row["selected"] for row in entities] == [False]

            async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
                toggled = await client.post(f"/api/tiles/{tile_id}/selection")
            assert toggled.status_code == 200
            assert cast(dict[str, object], toggled.json())["selected"] is True

            for websocket in (first, second):
                for _attempt in range(8):
                    message = _decode(await asyncio.wait_for(websocket.recv(), _RECV_TIMEOUT))
                    if message["type"] == "entity_state":
                        break
                else:
                    raise AssertionError("no entity_state frame arrived")
                assert _data(message) == {"entity_id": "lock.front_door", "selected": True}

            async with connect(url, open_timeout=5, close_timeout=1) as third:
                snapshot = _decode(await asyncio.wait_for(third.recv(), _RECV_TIMEOUT))
                entities = cast("list[dict[str, object]]", _data(snapshot)["entities"])
                assert [row["selected"] for row in entities] == [True]
