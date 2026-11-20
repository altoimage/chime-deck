"""Entities CRUD API, D16 icon resolution, presentation hints and the live seam.

Every test drives the real application through ``httpx.ASGITransport`` against a
temporary database, with the offline wiring installed by ``tests/conftest.py``: no
socket, broker, sound card or TTS engine is touched. The live-data seam is a
scripted fake, so each D16 chain and each degradation path is deterministic.

Owning ticket: HSD-jwd3.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state

from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig
from chime_deck.entity_info import NO_LIVE_ENTITY_INFO, EntityContext
from chime_deck.store import ConfigEvent, EntityIn
from chime_deck.store.database import Database

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_MISSING_IN_HA_WARNING = (
    "Home Assistant does not know the entity {entity_id!r} — it was saved anyway. "
    "Check the spelling, or make sure the integration that provides it is loaded in Home Assistant."
)
"""The exact soft warning the UI renders verbatim."""

_VANISHED_ICON_WARNING = (
    "the stored icon {icon!r} is no longer a usable file in data/icons/, so a fallback icon "
    "is shown — choose another icon or upload the file again"
)
"""The exact sentence a vanished ``custom:`` file produces."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test."""
    with preserve_log_state():
        yield


class _FakeLiveEntityInfo:
    """A scripted LiveEntityInfo: complete mappings, or None for 'no connection'."""

    def __init__(
        self,
        contexts: Mapping[str, EntityContext] | None = None,
        *,
        delay: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        """Record the scripted answer and the failure modes.

        :param contexts: complete mapping to return, or ``None`` for "no connection".
        :param delay: seconds to sleep before answering, to exercise the timeout.
        :param error: exception to raise instead of answering.
        """
        self._contexts = contexts
        self._delay = delay
        self._error = error
        self._calls: list[tuple[str, ...]] = []

    @property
    def calls(self) -> list[tuple[str, ...]]:
        """Return the ids of every lookup, in call order."""
        return self._calls

    async def contexts_for(self, entity_ids: Sequence[str]) -> Mapping[str, EntityContext] | None:
        """Answer as scripted, recording the requested ids.

        :param entity_ids: entity ids to look up.
        """
        self._calls.append(tuple(entity_ids))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._contexts


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


def _drain(queue: asyncio.Queue[ConfigEvent]) -> list[ConfigEvent]:
    """Return and clear every queued configuration event.

    :param queue: queue subscribed to the store's event bus.
    """
    drained: list[ConfigEvent] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    return drained


def _count_transactions(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count every ``Database.transaction()`` call from now on.

    The store is autocommit and ``connection.commit()`` is never called, so "one
    write" means exactly one outermost ``Database.transaction()``.

    :param monkeypatch: patcher that installs the counting wrapper.
    """
    calls: list[int] = []
    original = Database.transaction

    @asynccontextmanager
    async def counting_transaction(db: Database) -> AsyncIterator[aiosqlite.Connection]:
        calls.append(1)
        async with original(db) as connection:
            yield connection

    monkeypatch.setattr(Database, "transaction", counting_transaction)
    return calls


async def _create(client: httpx.AsyncClient, entity_id: str) -> dict[str, object]:
    """Create one entity through the API and return its body.

    :param client: client bound to the running app.
    :param entity_id: entity id of the new row.
    """
    response = await client.post("/api/entities", json={"entity_id": entity_id})
    assert response.status_code == 201, response.text
    return _body(response)


async def test_crud_round_trip(tmp_path: Path) -> None:
    """Create, list, patch and delete round-trip through the API.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        created = await _create(client, "lock.front_door")
        assert created["icon"] == "mdi:lock"
        assert created["icon_source"] == "fallback"
        assert created["icon_override"] is None
        assert created["tone"] is None
        assert created["state_tones"] is None
        assert created["colour_by_state"] is True
        assert created["warning"] is None

        rows = _rows(await client.get("/api/entities"))
        assert [row["id"] for row in rows] == [created["id"]]

        patched = await client.patch(
            f"/api/entities/{created['id']}",
            json={"icon": "mdi:door", "display_name": " Front Door "},
        )
        assert patched.status_code == 200
        body = _body(patched)
        assert body["icon_override"] == "mdi:door"
        assert body["icon_source"] == "user"
        assert body["display_name"] == "Front Door"

        deleted = await client.delete(f"/api/entities/{created['id']}")
        assert deleted.status_code == 200
        assert _body(deleted) == {
            "deleted": True,
            "id": created["id"],
            "entity_id": "lock.front_door",
        }

        assert _rows(await client.get("/api/entities")) == []


async def test_every_mutation_emits_exactly_one_entity_event(tmp_path: Path) -> None:
    """Create, patch, delete and reorder each publish one ``entities`` event.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        queue = state.store.events.subscribe_queue(maxsize=50)
        created = await _create(client, "lock.front_door")
        events = _drain(queue)
        assert [event.what for event in events] == ["entities"]
        assert events[0].payload["ids"] == (created["id"],)
        assert _drain(queue) == []

        patched = await client.patch(f"/api/entities/{created['id']}", json={"display_name": "X"})
        assert patched.status_code == 200
        events = _drain(queue)
        assert [event.what for event in events] == ["entities"]
        assert events[0].payload["ids"] == (created["id"],)
        assert _drain(queue) == []

        deleted = await client.delete(f"/api/entities/{created['id']}")
        assert deleted.status_code == 200
        events = _drain(queue)
        assert [event.what for event in events] == ["entities"]
        assert events[0].payload["removed_ids"] == (created["id"],)
        assert _drain(queue) == []

        other = await _create(client, "lock.back_door")
        _drain(queue)
        reorder = await client.post("/api/entities/reorder", json={"ids": [other["id"]]})
        assert reorder.status_code == 200
        events = _drain(queue)
        assert [event.what for event in events] == ["entities"]
        assert events[0].payload["ids"] == (other["id"],)
        assert _drain(queue) == []


async def test_reorder_persists_with_one_transaction_and_one_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reordering writes once, emits once, and refuses an unknown id untouched.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that counts ``Database.transaction()`` calls.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        first = (await _create(client, "lock.first"))["id"]
        second = (await _create(client, "lock.second"))["id"]
        third = (await _create(client, "lock.third"))["id"]

        queue = state.store.events.subscribe_queue(maxsize=50)
        _drain(queue)
        calls = _count_transactions(monkeypatch)

        response = await client.post("/api/entities/reorder", json={"ids": [third, first, second]})
        assert response.status_code == 200
        assert _body(response) == {"ids": [third, first, second]}
        assert calls == [1]

        rows = _rows(await client.get("/api/entities"))
        assert [row["id"] for row in rows] == [third, first, second]
        assert [row["sort_order"] for row in rows] == [10, 20, 30]

        events = _drain(queue)
        assert [event.what for event in events] == ["entities"]
        assert events[0].payload["ids"] == (third, first, second)

        missing = await client.post("/api/entities/reorder", json={"ids": [second, 9999]})
        assert missing.status_code == 404
        assert _error(missing)["code"] == "not_found"
        assert _detail(missing) == {"id": 9999}
        assert _rows(await client.get("/api/entities")) == rows
        assert _drain(queue) == []


async def test_duplicate_entity_id_is_a_409_naming_the_field(tmp_path: Path) -> None:
    """A duplicate answers 409 with the field name and never leaks sqlite text.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await _create(client, "lock.front_door")

        duplicate = await client.post("/api/entities", json={"entity_id": "lock.front_door"})
        assert duplicate.status_code == 409
        assert _error(duplicate)["code"] == "conflict"
        assert _detail(duplicate) == {"field": "entity_id", "entity_id": "lock.front_door"}
        assert "already configured" in cast(str, _error(duplicate)["message"])
        assert "UNIQUE constraint" not in duplicate.text

        second = await _create(client, "lock.back_door")
        moved = await client.patch(
            f"/api/entities/{second['id']}", json={"entity_id": "lock.front_door"}
        )
        assert moved.status_code == 409
        assert "another entity" in cast(str, _error(moved)["message"])


@pytest.mark.parametrize(
    "entity_id",
    ["", "front door", "Lock.Front", "lock.", ".x", "lock.front door"],
)
async def test_malformed_entity_ids_are_422s_naming_the_field(
    tmp_path: Path, entity_id: str
) -> None:
    """Every malformed entity id is rejected with ``detail.field == entity_id``.

    :param tmp_path: pytest temporary directory.
    :param entity_id: submitted id that must be rejected.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.post("/api/entities", json={"entity_id": entity_id})
        assert response.status_code == 422
        assert _error(response)["code"] == "validation_error"
        assert _detail(response)["field"] == "entity_id"
        assert _rows(await client.get("/api/entities")) == []


@pytest.mark.parametrize(
    "icon",
    [
        "Door",
        "MDI:door",
        "mdi:",
        "mdi:door bell",
        "custom:",
        "custom:../x.svg",
        "custom:sub/x.svg",
        "custom:missing.svg",
    ],
)
async def test_bad_icons_are_422s_naming_the_field(tmp_path: Path, icon: str) -> None:
    """A save with an unusable icon is a 422 and writes nothing.

    :param tmp_path: pytest temporary directory.
    :param icon: submitted reference that must be rejected.
    """
    (tmp_path / "icons").mkdir()
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.post(
            "/api/entities", json={"entity_id": "lock.front_door", "icon": icon}
        )
        assert response.status_code == 422
        assert _error(response)["code"] == "validation_error"
        assert _detail(response)["field"] == "icon"
        assert _rows(await client.get("/api/entities")) == []


async def test_blank_show_attributes_entry_is_a_422_naming_the_field(tmp_path: Path) -> None:
    """A blank attribute name is rejected rather than silently dropped.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.post(
            "/api/entities",
            json={"entity_id": "lock.front_door", "show_attributes": [""]},
        )
        assert response.status_code == 422
        assert _detail(response)["field"] == "show_attributes"
        assert _rows(await client.get("/api/entities")) == []


async def test_custom_icon_round_trip_and_a_vanished_file(tmp_path: Path) -> None:
    """A custom icon resolves as ``user`` until its file disappears.

    :param tmp_path: pytest temporary directory.
    """
    icons = tmp_path / "icons"
    icons.mkdir()
    (icons / "cat.svg").write_text("<svg/>", encoding="utf-8")
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        created = await client.post(
            "/api/entities", json={"entity_id": "lock.front_door", "icon": "custom:cat.svg"}
        )
        assert created.status_code == 201, created.text
        body = _body(created)
        assert body["icon_source"] == "user"
        assert body["icon"] == "custom:cat.svg"
        assert body["warning"] is None

        (icons / "cat.svg").unlink()
        (row,) = _rows(await client.get("/api/entities"))
        assert row["icon_override"] == "custom:cat.svg"
        assert row["icon_source"] == "fallback"
        assert row["icon"] == "mdi:lock"
        assert row["warning"] == _VANISHED_ICON_WARNING.format(icon="custom:cat.svg")

        # Both warnings can apply at once; the missing-entity sentence comes first.
        state.live_entities = _FakeLiveEntityInfo(
            {"lock.front_door": EntityContext(entity_id="lock.front_door", found=False)}
        )
        (warned,) = _rows(await client.get("/api/entities"))
        assert warned["warning"] == " ".join(
            (
                _MISSING_IN_HA_WARNING.format(entity_id="lock.front_door"),
                _VANISHED_ICON_WARNING.format(icon="custom:cat.svg"),
            )
        )
        state.live_entities = NO_LIVE_ENTITY_INFO

        # An untouched stored icon is never re-validated, so the row stays editable.
        patched = await client.patch(
            f"/api/entities/{body['id']}", json={"display_name": "Still editable"}
        )
        assert patched.status_code == 200
        updated = _body(patched)
        assert updated["display_name"] == "Still editable"
        assert updated["icon_source"] == "fallback"
        assert updated["warning"] == _VANISHED_ICON_WARNING.format(icon="custom:cat.svg")


async def test_the_soft_warnings(tmp_path: Path) -> None:
    """A missing entity warns softly; a present one and no connection stay quiet.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        state.live_entities = _FakeLiveEntityInfo(
            {"lock.front_door": EntityContext(entity_id="lock.front_door", found=False)}
        )
        created = await client.post("/api/entities", json={"entity_id": "lock.front_door"})
        assert created.status_code == 201
        body = _body(created)
        assert body["warning"] == _MISSING_IN_HA_WARNING.format(entity_id="lock.front_door")

        patched = await client.patch(f"/api/entities/{body['id']}", json={"display_name": "X"})
        assert patched.status_code == 200
        assert _body(patched)["warning"] == _MISSING_IN_HA_WARNING.format(
            entity_id="lock.front_door"
        )

        state.live_entities = _FakeLiveEntityInfo(
            {"lock.back_door": EntityContext(entity_id="lock.back_door", found=True)}
        )
        known = await client.post("/api/entities", json={"entity_id": "lock.back_door"})
        assert known.status_code == 201
        assert _body(known)["warning"] is None

        state.live_entities = NO_LIVE_ENTITY_INFO
        silent = await client.post("/api/entities", json={"entity_id": "lock.side_door"})
        assert silent.status_code == 201
        assert _body(silent)["warning"] is None
        assert all(row["warning"] is None for row in _rows(await client.get("/api/entities")))


async def test_a_failing_seam_degrades_to_configuration_only_icons(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A raising seam answers normally and logs exactly one warning.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        caplog.set_level(logging.WARNING, logger="chime_deck")
        state.live_entities = _FakeLiveEntityInfo(error=RuntimeError("no connection"))
        response = await client.post("/api/entities", json={"entity_id": "lock.front_door"})
        assert response.status_code == 201
        body = _body(response)
        assert body["warning"] is None
        assert body["icon"] == "mdi:lock"
        assert body["icon_source"] == "fallback"

    failures = [
        record
        for record in caplog.records
        if record.getMessage().startswith("live entity lookup failed")
    ]
    assert len(failures) == 1
    assert failures[0].levelno == logging.WARNING


async def test_a_slow_seam_times_out_without_a_warning(tmp_path: Path) -> None:
    """A seam slower than ``live_lookup_timeout`` degrades, bounded and silently.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    state.live_lookup_timeout = 0.01

    async with _running(app) as client:
        state.live_entities = _FakeLiveEntityInfo(delay=0.2)
        response = await asyncio.wait_for(
            client.post("/api/entities", json={"entity_id": "lock.front_door"}), timeout=1.0
        )
        assert response.status_code == 201
        body = _body(response)
        assert body["warning"] is None
        assert body["icon_source"] == "fallback"


@pytest.mark.parametrize(
    ("entity_id", "context", "expected_icon", "expected_source"),
    [
        (
            "lock.front_door",
            EntityContext(entity_id="lock.front_door", found=True, attributes={"icon": "mdi:door"}),
            "mdi:door",
            "ha-attribute",
        ),
        (
            "lock.front_door",
            EntityContext(entity_id="lock.front_door", found=True, registry_icon="mdi:garage"),
            "mdi:garage",
            "ha-registry",
        ),
        (
            "cover.garage",
            EntityContext(entity_id="cover.garage", found=True, device_class="garage"),
            "mdi:garage-open",
            "fallback",
        ),
        (
            "lock.front_door",
            EntityContext(entity_id="lock.front_door", found=True),
            "mdi:lock",
            "fallback",
        ),
        (
            "foobar.thing",
            EntityContext(entity_id="foobar.thing", found=True),
            "mdi:help-circle",
            "default",
        ),
    ],
)
async def test_the_chain_reaches_the_payload_from_the_seam(
    tmp_path: Path,
    entity_id: str,
    context: EntityContext,
    expected_icon: str,
    expected_source: str,
) -> None:
    """Each D16 step, from live attribute to the generic default, reaches the body.

    :param tmp_path: pytest temporary directory.
    :param entity_id: entity to create.
    :param context: scripted live data for that entity.
    :param expected_icon: icon the chain must resolve.
    :param expected_source: ``icon_source`` the winning step must report.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        state.live_entities = _FakeLiveEntityInfo({entity_id: context})
        response = await client.post("/api/entities", json={"entity_id": entity_id})
        assert response.status_code == 201
        body = _body(response)
        assert body["icon"] == expected_icon
        assert body["icon_source"] == expected_source


async def test_hints_and_wording(tmp_path: Path) -> None:
    """State wording, the unit and the renderable attributes all reach the body.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    context = EntityContext(
        entity_id="person.alex",
        found=True,
        state="not_home",
        attributes={"unit_of_measurement": "°C", "battery_level": 87},
    )

    async with _running(app) as client:
        state.live_entities = _FakeLiveEntityInfo({"person.alex": context})
        response = await client.post(
            "/api/entities",
            json={"entity_id": "person.alex", "show_attributes": ["battery_level", "missing_one"]},
        )
        assert response.status_code == 201
        body = _body(response)
        assert body["displayed_state"] == "Away"
        assert body["tone"] == "off"
        assert body["state_tones"] is None
        assert body["colour_by_state"] is True
        assert body["unit_of_measurement"] == "°C"
        assert body["attribute_hints"] == [
            {"name": "battery_level", "label": "Battery level", "value": "87"}
        ]


async def test_one_bulk_lookup_per_list_request(tmp_path: Path) -> None:
    """Listing three rows asks the seam once, for all three in stored order.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        first = await _create(client, "lock.first")
        second = await _create(client, "sensor.second")
        third = await _create(client, "light.third")
        fake = _FakeLiveEntityInfo({})
        state.live_entities = fake

        rows = _rows(await client.get("/api/entities"))
        assert [row["id"] for row in rows] == [first["id"], second["id"], third["id"]]
        assert fake.calls == [("lock.first", "sensor.second", "light.third")]


async def test_the_entity_id_rule_guards_api_writes_only(tmp_path: Path) -> None:
    """The store keeps accepting historical ids, and the read path still renders them.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        rejected = await client.post("/api/entities", json={"entity_id": "front door"})
        assert rejected.status_code == 422

        await state.store.upsert_entity(EntityIn(entity_id="front door"))

        (row,) = _rows(await client.get("/api/entities"))
        assert row["entity_id"] == "front door"
        assert row["icon_source"] == "default"
        assert row["icon"] == "mdi:help-circle"


async def test_unknown_ids_are_404s(tmp_path: Path) -> None:
    """PATCH and DELETE of an id that names no entity answer the not-found envelope.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        patched = await client.patch("/api/entities/9999", json={"display_name": "X"})
        assert patched.status_code == 404
        assert _error(patched)["code"] == "not_found"
        assert _detail(patched) == {"id": 9999}

        deleted = await client.delete("/api/entities/9999")
        assert deleted.status_code == 404
        assert _error(deleted)["code"] == "not_found"
        assert _detail(deleted) == {"id": 9999}


async def test_reorder_of_an_empty_list_changes_nothing(tmp_path: Path) -> None:
    """An empty reorder is a no-op that still answers 200 with the submitted ids.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        queue = state.store.events.subscribe_queue(maxsize=50)
        response = await client.post("/api/entities/reorder", json={"ids": []})
        assert response.status_code == 200
        assert _body(response) == {"ids": []}
        assert _drain(queue) == []


async def test_show_attributes_normalises_duplicates_and_whitespace(tmp_path: Path) -> None:
    """Trimmed duplicates collapse in order, and a blank entry is still refused.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        created = await client.post(
            "/api/entities",
            json={"entity_id": "sensor.kitchen", "show_attributes": [" battery ", "battery"]},
        )
        assert created.status_code == 201
        assert _body(created)["show_attributes"] == ["battery"]

        rejected = await client.patch(
            f"/api/entities/{_body(created)['id']}", json={"show_attributes": ["battery", " "]}
        )
        assert rejected.status_code == 422
        assert _detail(rejected)["field"] == "show_attributes"


async def test_state_tones_round_trip_and_the_row_tone_reflects_them(tmp_path: Path) -> None:
    """A submitted mapping is trimmed, stored sorted and decides the live tone.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    context = EntityContext(entity_id="sensor.washer", found=True, state="running", attributes={})

    async with _running(app) as client:
        state.live_entities = _FakeLiveEntityInfo({"sensor.washer": context})
        created = await client.post(
            "/api/entities",
            json={"entity_id": "sensor.washer", "state_tones": {" running ": "alert"}},
        )
        assert created.status_code == 201
        body = _body(created)
        assert body["state_tones"] == {"running": "alert"}
        assert body["tone"] == "alert"
        assert body["displayed_state"] == "Running"

        patched = await client.patch(
            f"/api/entities/{body['id']}", json={"state_tones": {"finished": "alert"}}
        )
        assert patched.status_code == 200
        patched_body = _body(patched)
        assert patched_body["state_tones"] == {"finished": "alert"}
        # ``running`` is back on the built-in default table, so the override no
        # longer applies to the current state.
        assert patched_body["tone"] == "warn"


async def test_patch_with_null_state_tones_clears_every_override(tmp_path: Path) -> None:
    """An explicit ``null`` is a clear for this nullable field.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    context = EntityContext(entity_id="sensor.washer", found=True, state="running", attributes={})

    async with _running(app) as client:
        state.live_entities = _FakeLiveEntityInfo({"sensor.washer": context})
        created = await client.post(
            "/api/entities",
            json={"entity_id": "sensor.washer", "state_tones": {"running": "alert"}},
        )
        identifier = _body(created)["id"]
        assert _body(created)["tone"] == "alert"

        cleared = await client.patch(f"/api/entities/{identifier}", json={"state_tones": None})
        assert cleared.status_code == 200
        body = _body(cleared)
        assert body["state_tones"] is None
        assert body["tone"] == "warn"


async def test_a_blank_state_tone_key_is_a_422_naming_the_field(tmp_path: Path) -> None:
    """A blank state key is refused with a human message, never stored.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        rejected = await client.post(
            "/api/entities", json={"entity_id": "sensor.washer", "state_tones": {" ": "ok"}}
        )
        assert rejected.status_code == 422
        assert _detail(rejected) == {"field": "state_tones"}
        assert (
            _error(rejected)["message"]
            == "state_tones must map a state to a tone — remove the blank state key"
        )


async def test_a_tone_outside_the_vocabulary_is_a_422_naming_it(tmp_path: Path) -> None:
    """An unknown tone is refused and the vocabulary is spelled out.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        rejected = await client.post(
            "/api/entities",
            json={"entity_id": "sensor.washer", "state_tones": {"running": "loud"}},
        )
        assert rejected.status_code == 422
        assert _detail(rejected) == {"field": "state_tones"}
        assert (
            _error(rejected)["message"]
            == "'running' maps to 'loud' — choose one of ok, warn, alert, off"
        )


async def test_colour_by_state_false_makes_the_tone_off(tmp_path: Path) -> None:
    """The opt-out is grey in every state while the word still says the state.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    context = EntityContext(entity_id="sensor.washer", found=True, state="running", attributes={})

    async with _running(app) as client:
        state.live_entities = _FakeLiveEntityInfo({"sensor.washer": context})
        created = await client.post(
            "/api/entities",
            json={"entity_id": "sensor.washer", "colour_by_state": False},
        )
        assert created.status_code == 201
        body = _body(created)
        assert body["colour_by_state"] is False
        assert body["tone"] == "off"
        assert body["displayed_state"] == "Running"

        kept = await client.patch(f"/api/entities/{body['id']}", json={"colour_by_state": None})
        assert kept.status_code == 200
        assert _body(kept)["colour_by_state"] is False


async def test_state_tone_defaults_route_publishes_the_table(tmp_path: Path) -> None:
    """The editor's pre-fill comes from the code, in display and sorted order.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/entities/state-tones")
        assert response.status_code == 200
        body = _body(response)
        assert body["tones"] == ["ok", "warn", "alert", "off"]
        defaults = cast(dict[str, str], body["defaults"])
        assert list(defaults) == sorted(defaults)
        assert defaults["running"] == "warn"
        assert defaults["locked"] == "ok"
        assert defaults["unlocked"] == "alert"
        assert defaults["detected"] == "warn"


async def test_deleting_one_tile_leaves_anothers_tones_intact(tmp_path: Path) -> None:
    """Per-tile overrides survive the deletion of a sibling.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        first = await client.post(
            "/api/entities",
            json={"entity_id": "sensor.first", "state_tones": {"running": "alert"}},
        )
        second = await client.post(
            "/api/entities",
            json={"entity_id": "sensor.second", "state_tones": {"running": "ok"}},
        )
        deleted = await client.delete(f"/api/entities/{_body(first)['id']}")
        assert deleted.status_code == 200

        (row,) = _rows(await client.get("/api/entities"))
        assert row["id"] == _body(second)["id"]
        assert row["state_tones"] == {"running": "ok"}


async def test_explicit_null_clears_a_nullable_field(tmp_path: Path) -> None:
    """``null`` clears a stored value while an omitted field keeps it.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        created = await client.post(
            "/api/entities",
            json={
                "entity_id": "lock.front_door",
                "icon": "mdi:door",
                "display_name": "Front",
                "accent_color": "#ff0000",
            },
        )
        assert created.status_code == 201
        identifier = created.json()["id"]

        cleared = await client.patch(
            f"/api/entities/{identifier}", json={"icon": None, "accent_color": None}
        )
        assert cleared.status_code == 200
        body = _body(cleared)
        assert body["icon_override"] is None
        assert body["icon_source"] == "fallback"
        assert body["accent_color"] is None
        assert body["display_name"] == "Front"

        kept = await client.patch(f"/api/entities/{identifier}", json={"display_name": "Back"})
        assert kept.status_code == 200
        assert _body(kept)["display_name"] == "Back"
