"""Tile selection API: the volatile D29 toggle, its broadcast and its absence on disk.

Every test drives the real application through ``httpx.ASGITransport`` against a
temporary data directory, with the offline wiring installed by
``tests/conftest.py``. Selection is runtime state (HSD-u6k5): it is never
persisted, so the last two tests pin the two halves of that promise — a restart
clears it, and the SQLite file keeps the same schema, ``user_version`` and rows
before and after a toggle.

Owning ticket: HSD-u6k5.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state

from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig
from chime_deck.hub import HubClient

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_RECV_TIMEOUT_S = 5.0
"""Bound for one frame out of a hub client's queue."""

_MAX_SKIPPED_FRAMES = 8
"""How many unrelated frames one ``entity_state`` search may step over."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test."""
    with preserve_log_state():
        yield


def _build_app(tmp_path: Path) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory used as the data directory.
    """
    config = ServerConfig(
        data_dir=tmp_path,
        static_dir=tmp_path / "no-dist",
        seed_demo_sounds=False,
    )
    app = create_app(config)
    return app, cast(AppState, app.state.doorbell)


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


def _body(response: httpx.Response) -> dict[str, object]:
    """Return a decoded JSON object body.

    :param response: successful response.
    """
    return cast(dict[str, object], response.json())


def _rows(response: httpx.Response) -> list[dict[str, object]]:
    """Return a decoded JSON array of objects.

    :param response: successful list response.
    """
    return cast("list[dict[str, object]]", response.json())


def _error(response: httpx.Response) -> dict[str, object]:
    """Return the ``error`` object of a failed response.

    :param response: response with the standard error envelope.
    """
    return cast("dict[str, object]", response.json()["error"])


def _detail(response: httpx.Response) -> dict[str, object]:
    """Return the ``error.detail`` object of a failed response.

    :param response: response with the standard error envelope.
    """
    return cast("dict[str, object]", _error(response)["detail"])


async def _create(client: httpx.AsyncClient, entity_id: str) -> dict[str, object]:
    """Create one entity through the API and return its body.

    :param client: client bound to the running app.
    :param entity_id: entity id of the new row.
    """
    response = await client.post("/api/entities", json={"entity_id": entity_id})
    assert response.status_code == 201, response.text
    return _body(response)


def _snapshot_rows(snapshot: dict[str, object]) -> list[dict[str, object]]:
    """Return the entity rows of one ``snapshot`` message.

    :param snapshot: decoded ``snapshot`` message.
    """
    data = cast(dict[str, object], snapshot["data"])
    return cast("list[dict[str, object]]", data["entities"])


def _selection_of(snapshot: dict[str, object]) -> dict[str, bool]:
    """Return ``entity_id -> selected`` from one ``snapshot`` message.

    :param snapshot: decoded ``snapshot`` message.
    """
    return {
        cast(str, row["entity_id"]): cast(bool, row["selected"]) for row in _snapshot_rows(snapshot)
    }


async def _next_frame(client: HubClient, message_type: str) -> dict[str, object]:
    """Return the next decoded frame of *message_type*, stepping over other types.

    A configuration event may still be in flight when a client registers, so the
    search tolerates a bounded number of unrelated frames instead of assuming the
    wanted one is first. Both waits are bounded.

    :param client: client registered with the hub.
    :param message_type: the ``type`` the caller is waiting for.
    """
    for _attempt in range(_MAX_SKIPPED_FRAMES):
        frame = cast(
            dict[str, object],
            json.loads(await asyncio.wait_for(client.queue.get(), _RECV_TIMEOUT_S)),
        )
        if frame["type"] == message_type:
            return frame
    raise AssertionError(f"no {message_type} frame arrived in {_MAX_SKIPPED_FRAMES} frames")


def _frame_data(frame: dict[str, object]) -> dict[str, object]:
    """Return the ``data`` payload of one decoded envelope.

    :param frame: decoded ``{"type": ..., "data": ...}`` frame.
    """
    return cast("dict[str, object]", frame["data"])


@dataclass(frozen=True)
class _Dump:
    """Everything about one closed database file that a toggle must not change."""

    schema: list[tuple[object, ...]]
    user_version: int
    tables: list[tuple[str, list[str], list[tuple[object, ...]]]]


def _dump(database: Path) -> _Dump:
    """Read a closed SQLite file read-only and return everything worth comparing.

    The connection is read-only on purpose: a test that proves nothing is written
    must not be able to write. It is closed before returning and never kept across
    a running app — SQLite has to checkpoint the WAL before a read-only reader sees
    the whole database, which is why every caller dumps while the store is closed.

    :param database: path of the database file.
    """
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        user_version = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
        table_names = [
            cast(str, row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        ]
        tables: list[tuple[str, list[str], list[tuple[object, ...]]]] = []
        for table in sorted(table_names):
            columns = [
                cast(str, row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            ]
            rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            tables.append((table, columns, rows))
    finally:
        connection.close()
    return _Dump(schema=[tuple(row) for row in schema], user_version=user_version, tables=tables)


async def test_toggle_flips_and_appears_in_a_fresh_snapshot(tmp_path: Path) -> None:
    """The toggle answers the new value and a new client's snapshot agrees.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        first = await _create(client, "lock.front_door")
        await _create(client, "lock.back_door")

        response = await client.post(f"/api/tiles/{first['id']}/selection")

        assert response.status_code == 200
        assert _body(response) == {
            "id": first["id"],
            "entity_id": "lock.front_door",
            "selected": True,
        }
        assert _selection_of(await state.hub.snapshot_message()) == {
            "lock.front_door": True,
            "lock.back_door": False,
        }

        again = await client.post(f"/api/tiles/{first['id']}/selection")

        assert again.status_code == 200
        assert _body(again)["selected"] is False
        assert _selection_of(await state.hub.snapshot_message()) == {
            "lock.front_door": False,
            "lock.back_door": False,
        }


async def test_toggle_is_broadcast_to_two_connected_clients(tmp_path: Path) -> None:
    """Both connected clients get the frame, and a later client gets it via the snapshot.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        tile = await _create(client, "lock.front_door")
        connected = (state.hub.register(), state.hub.register())
        try:
            response = await client.post(f"/api/tiles/{tile['id']}/selection")
            assert response.status_code == 200

            await state.hub.flush_once()
            for hub_client in connected:
                frame = await _next_frame(hub_client, "entity_state")
                assert _frame_data(frame) == {"entity_id": "lock.front_door", "selected": True}

            # Nothing is queued for a client that was not connected yet: it learns
            # the truth from its own snapshot instead.
            await state.hub.flush_once()
            late = state.hub.register()
            assert late.queue.empty()
            assert _selection_of(await state.hub.snapshot_message())["lock.front_door"] is True
        finally:
            for hub_client in connected:
                state.hub.unregister(hub_client)


async def test_toggle_unknown_tile_is_404(tmp_path: Path) -> None:
    """An id that names no entity answers the standard 404 and selects nothing.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _create(client, "lock.front_door")

        response = await client.post("/api/tiles/9999/selection")

        assert response.status_code == 404
        assert _error(response)["code"] == "not_found"
        assert _detail(response) == {"id": 9999}
        assert state.selection.selected_ids() == frozenset()


async def test_deleting_an_entity_clears_its_selection(tmp_path: Path) -> None:
    """A deleted tile loses its selection key; the other tile keeps its own.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        doomed = await _create(client, "lock.front_door")
        kept = await _create(client, "lock.back_door")
        for tile in (doomed, kept):
            response = await client.post(f"/api/tiles/{tile['id']}/selection")
            assert response.status_code == 200
            assert _body(response)["selected"] is True

        deleted = await client.delete(f"/api/entities/{doomed['id']}")

        assert deleted.status_code == 200
        assert state.selection.selected_ids() == frozenset({"lock.back_door"})
        assert _selection_of(await state.hub.snapshot_message()) == {"lock.back_door": True}


async def test_reorder_preserves_selection(tmp_path: Path) -> None:
    """Reordering the tiles is a no-op: the set is keyed on ``entity_id``.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        tiles = [
            await _create(client, entity_id)
            for entity_id in ("lock.first", "lock.second", "lock.third")
        ]
        for tile in (tiles[0], tiles[2]):
            response = await client.post(f"/api/tiles/{tile['id']}/selection")
            assert _body(response)["selected"] is True

        reversed_ids = [tile["id"] for tile in reversed(tiles)]
        reordered = await client.post("/api/entities/reorder", json={"ids": reversed_ids})

        assert reordered.status_code == 200
        assert [row["id"] for row in _rows(await client.get("/api/entities"))] == reversed_ids
        assert _selection_of(await state.hub.snapshot_message()) == {
            "lock.third": True,
            "lock.second": False,
            "lock.first": True,
        }


async def test_restart_clears_every_selection(tmp_path: Path) -> None:
    """Selection is volatile by design (D29 §4): a second boot starts unselected.

    The first app runs a full lifespan against a real ``doorbell.db``, selects
    both tiles and shuts down; the second app is built from the same data
    directory, so only the on-disk configuration survives.

    :param tmp_path: pytest temporary directory.
    """
    first_app, first_state = _build_app(tmp_path)
    async with _running(first_app) as client:
        tiles = [
            await _create(client, entity_id) for entity_id in ("lock.front_door", "lock.back_door")
        ]
        for tile in tiles:
            response = await client.post(f"/api/tiles/{tile['id']}/selection")
            assert _body(response)["selected"] is True
        assert first_state.selection.selected_ids() == {"lock.front_door", "lock.back_door"}

    second_app, second_state = _build_app(tmp_path)
    async with _running(second_app) as client:
        assert len(_rows(await client.get("/api/entities"))) == 2
        assert second_state.selection.selected_ids() == frozenset()
        assert _selection_of(await second_state.hub.snapshot_message()) == {
            "lock.front_door": False,
            "lock.back_door": False,
        }


async def test_selection_is_never_written_to_the_database(tmp_path: Path) -> None:
    """Two toggles leave the SQLite file's schema and content untouched.

    The dumps are taken with the store closed: SQLite has to checkpoint the WAL
    for a read-only reader to see the whole database, so the app is shut down for
    each dump and a second app is run for the toggles rather than trying to read
    a live WAL file. The whole file is not compared byte-for-byte, because the WAL
    and the page layout are free to differ without a single row changing.

    :param tmp_path: pytest temporary directory.
    """
    database = tmp_path / "doorbell.db"

    before_app, _before_state = _build_app(tmp_path)
    async with _running(before_app) as client:
        tiles = [
            await _create(client, entity_id) for entity_id in ("lock.front_door", "lock.back_door")
        ]
        tile_ids = [tile["id"] for tile in tiles]

    before = _dump(database)
    assert "entities" in [table for table, _columns, _rows in before.tables]

    toggle_app, toggle_state = _build_app(tmp_path)
    async with _running(toggle_app) as client:
        for tile_id in tile_ids:
            for _twice in range(2):
                response = await client.post(f"/api/tiles/{tile_id}/selection")
                assert response.status_code == 200
        await toggle_state.hub.flush_once()
        assert toggle_state.selection.selected_ids() == frozenset()

    after = _dump(database)

    assert after.schema == before.schema
    assert after.user_version == before.user_version
    assert after.tables == before.tables
    for table, columns, _rows in after.tables:
        assert "select" not in table.lower()
        assert "select" not in " ".join(columns).lower()
