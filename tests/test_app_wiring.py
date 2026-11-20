"""The application wiring: startup, health, the press endpoint and the MQTT generation.

Every test here builds a real application through :func:`create_app`, runs the
real lifespan and speaks to it through ``httpx.ASGITransport``, so no socket is
opened. The devices are the offline ones installed by ``tests/conftest.py``:
:class:`~chime_fakes.RecordingBackend` is the speaker and
:class:`~mqtt_fakes.FakeBroker` supplies the MQTT clients. Tests that need a
non-default device (an unavailable backend) build their own
:class:`~chime_deck.wiring.Wiring` and pass it explicitly.

Everything asynchronous is bounded: every hub read is wrapped in
``asyncio.wait_for`` and every condition the application reaches on its own is
polled with :func:`~mqtt_fakes.wait_until`.

Owning ticket: HSD-dtpo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest
from chime_fakes import RecordingBackend
from fastapi import FastAPI
from log_isolation import preserve_log_state
from mqtt_fakes import FakeBroker, wait_until

from chime_deck.app import AppState, create_app
from chime_deck.audio import AudioBackendUnavailable, MpvBackend
from chime_deck.config import ServerConfig
from chime_deck.health import HealthStatus
from chime_deck.hub import HubClient
from chime_deck.mqtt import (
    MqttSettings,
    command_topic,
    discovery_topic,
    selection_discovery_topic,
    selection_slug,
    selection_state_topic,
    unique_id,
)
from chime_deck.press import SOURCE_UI_TEST, PressService
from chime_deck.store import ButtonIn, ConfigStore, EntityIn, SoundIn
from chime_deck.wiring import Wiring, _UnavailableBackend, resolve_backend

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request; nothing here may hang the suite."""

_PATCH_TIMEOUT_S = 5.0
"""Bound for one settings write, including the MQTT generation rebuild."""

_HUB_TIMEOUT_S = 2.0
"""Bound for one hub message, delivered by the 0.1 s background flush."""

_READY_INSTANCE = "default"
"""Instance id the seeded settings use for the first MQTT generation."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test.

    The application lifespan applies the stored ``system.log_level`` to the root
    logger, so the level has to be put back or later tests inherit it.
    """
    with preserve_log_state():
        yield


@dataclass(frozen=True, slots=True)
class _Seeded:
    """The database rows a test starts from."""

    button_id: int
    slug: str
    sound_path: Path
    discovery_topic: str
    command_topic: str


def _config(tmp_path: Path) -> ServerConfig:
    """Return the server configuration every test in this module uses.

    :param tmp_path: pytest temporary directory, used as the data directory.
    """
    return ServerConfig(
        data_dir=tmp_path, static_dir=tmp_path / "no-such-dist", seed_demo_sounds=False
    )


async def _seed(tmp_path: Path, *, settings: Mapping[str, object] | None = None) -> _Seeded:
    """Write settings and one playable sound button before the app is built.

    The sound file is created (the worker only stats it; the fake backend never
    decodes it), and the rows are written before the lifespan opens the same
    database, which is the only honest way to make ``create_app`` see them.

    :param tmp_path: pytest temporary directory.
    :param settings: settings written before anything starts, keyed by name.
    """
    sound_path = tmp_path / "ding.mp3"
    sound_path.write_bytes(b"an offline test sound")
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        for key, value in (settings or {}).items():
            await store.set_setting(key, value)
        sound = await store.create_sound(
            SoundIn(name="Ding dong", source_type="file", path=str(sound_path))
        )
        button = await store.upsert_button(
            ButtonIn(name="Front Door", action_type="sound", sound_id=sound.id)
        )
    delivered = MqttSettings(host="broker.test", instance_id=_READY_INSTANCE)
    return _Seeded(
        button_id=button.id,
        slug=button.slug,
        sound_path=sound_path,
        discovery_topic=discovery_topic(delivered, button.slug),
        command_topic=command_topic(delivered, button.slug),
    )


@asynccontextmanager
async def _running(
    app: FastAPI, *, raise_app_exceptions: bool = True
) -> AsyncIterator[httpx.AsyncClient]:
    """Run the real lifespan and yield a client bound to the application.

    :param app: application under test.
    :param raise_app_exceptions: whether httpx re-raises an application error
        after the error envelope has been produced.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=_HTTP_TIMEOUT_S
        ) as client:
            yield client


def _state(app: FastAPI) -> AppState:
    """Return the application state attached to *app*.

    :param app: application built by :func:`create_app`.
    """
    return cast(AppState, app.state.doorbell)


async def _next_press(state: AppState, hub_client: HubClient) -> dict[str, object]:
    """Read one ``press`` message from the hub, bounded.

    :param state: running application state holding the hub.
    :param hub_client: client registered on that hub.
    """
    payload = await asyncio.wait_for(state.hub.next_payload(hub_client), timeout=_HUB_TIMEOUT_S)
    return cast(dict[str, object], json.loads(payload))


def _write_raw_setting(database: Path, key: str, value: object) -> None:
    """Write one ``settings`` row with raw SQLite, bypassing the key registry.

    This is how a database written by an older build looks: the value is stored
    even though the current registry would refuse it on a normal write.

    :param database: SQLite file to write.
    :param key: settings key to store.
    :param value: decoded value, stored JSON-encoded like the store does.
    """
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value), "2026-11-13T00:00:00.000000Z"),
        )


def _raw_setting_timestamp(database: Path, key: str) -> str:
    """Return the raw ``updated_at`` text of one stored setting.

    :param database: SQLite file to read.
    :param key: settings key to look up.
    """
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute("SELECT updated_at FROM settings WHERE key = ?", (key,)).fetchone()
    assert row is not None, f"setting {key!r} is missing"
    return cast(str, row[0])


async def test_startup_wires_the_press_path(
    tmp_path: Path, fake_broker: FakeBroker, fake_backend: RecordingBackend
) -> None:
    """The lifespan builds one worker, one shared press service and one generation.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the offline wiring handed to the app.
    :param fake_backend: speaker the offline wiring handed to the app.
    """
    seeded = await _seed(tmp_path)
    app = create_app(_config(tmp_path))
    state = _state(app)

    async with _running(app):
        assert state.health.get("audio") is HealthStatus.READY
        assert fake_backend.started == 1
        assert isinstance(state.chime.press, PressService)
        assert state.chime.playback.is_running
        assert state.chime.mqtt_generations == 1
        assert state.chime.bridge is not None
        discovery = state.chime.discovery
        assert discovery is not None
        assert seeded.command_topic in discovery.command_topics
        assert discovery.command_topics[seeded.command_topic] == seeded.slug
        assert fake_broker.clients


async def test_create_app_wires_the_icon_directory_into_the_hub(tmp_path: Path) -> None:
    """``create_app`` hands the resolved custom-icon directory to the hub.

    :param tmp_path: pytest temporary directory.
    """
    icons = tmp_path / "icons"
    icons.mkdir()
    (icons / "cat.svg").write_text("<svg/>", encoding="utf-8")
    config = ServerConfig(
        data_dir=tmp_path,
        static_dir=tmp_path / "no-such-dist",
        seed_demo_sounds=False,
        icons_dir=icons,
    )
    app = create_app(config)
    state = _state(app)

    async with _running(app):
        await state.store.upsert_entity(
            EntityIn(entity_id="lock.front_door", icon="custom:cat.svg")
        )
        snapshot = await state.hub.snapshot_message()
        data = cast(dict[str, object], snapshot["data"])
        (row,) = cast("list[dict[str, object]]", data["entities"])

        assert row["icon"] == "custom:cat.svg"
        assert row["icon_source"] == "user"


async def test_press_endpoint_answers_with_the_press_result(
    tmp_path: Path, fake_backend: RecordingBackend
) -> None:
    """``POST /api/buttons/{id}/press`` returns the result and logs exactly one row.

    The backend's gate holds the chime in flight, so the ``queued`` state is
    observed before the worker revises it; the played flip is the integration
    test's subject.

    :param tmp_path: pytest temporary directory.
    :param fake_backend: speaker whose gate holds the chime open.
    """
    seeded = await _seed(tmp_path)
    gate = asyncio.Event()
    fake_backend.gate = gate
    app = create_app(_config(tmp_path))
    state = _state(app)

    try:
        async with _running(app) as client:
            hub_client = state.hub.register()
            response = await client.post(f"/api/buttons/{seeded.button_id}/press")

            assert response.status_code == 200
            body = cast(dict[str, object], response.json())
            assert body["button_id"] == seeded.button_id
            assert body["button_name"] == "Front Door"
            assert body["source"] == SOURCE_UI_TEST
            assert body["outcome"] == "queued"
            assert body["code"] is None
            assert body["detail"] is None

            message = await _next_press(state, hub_client)
            assert message["type"] == "press"
            data = cast(dict[str, object], message["data"])
            assert data["outcome"] == "queued"
            assert data["button_id"] == seeded.button_id
            assert data["pressed_at"] == body["pressed_at"]

            entries = await state.store.recent_presses()
            assert len(entries) == 1
            assert entries[0].outcome == "queued"
            assert entries[0].source == SOURCE_UI_TEST
            assert entries[0].pressed_at == body["pressed_at"]
            # The worker has taken the job; its played flip cannot land until
            # the gate opens, so exactly one press message exists for this press.
            assert fake_backend.playing is True
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(state.hub.next_payload(hub_client), timeout=0.2)

            # Released here, not only in the ``finally``: leaving the lifespan
            # with a chime still in flight would make shutdown wait out the
            # worker's 2 s grace for no reason. The ``finally`` stays as the
            # failure path.
            gate.set()
    finally:
        gate.set()


async def test_press_an_unknown_id_is_the_not_found_envelope(tmp_path: Path) -> None:
    """An id no button has answers 404 with the standard envelope.

    :param tmp_path: pytest temporary directory.
    """
    await _seed(tmp_path)
    app = create_app(_config(tmp_path))

    async with _running(app) as client:
        response = await client.post("/api/buttons/999/press")

    assert response.status_code == 404
    body = cast(dict[str, object], response.json())
    assert set(body) == {"error"}
    error = cast(dict[str, object], body["error"])
    assert error["code"] == "not_found"
    assert error["detail"] == {"id": 999}


async def test_press_with_a_non_numeric_id_is_a_validation_error(tmp_path: Path) -> None:
    """A path that is not an id is a request-validation failure, not a 500.

    :param tmp_path: pytest temporary directory.
    """
    await _seed(tmp_path)
    app = create_app(_config(tmp_path))

    async with _running(app) as client:
        response = await client.post("/api/buttons/not-a-number/press")

    assert response.status_code == 422
    body = cast(dict[str, object], response.json())
    error = cast(dict[str, object], body["error"])
    assert error["code"] == "validation_error"
    assert "button_id" in json.dumps(error["detail"])


async def test_a_broken_store_answers_with_the_store_envelope(tmp_path: Path) -> None:
    """A closed connection is a ``store_unavailable`` envelope, not a traceback.

    :param tmp_path: pytest temporary directory.
    """
    seeded = await _seed(tmp_path)
    app = create_app(_config(tmp_path))
    state = _state(app)

    async with _running(app, raise_app_exceptions=False) as client:
        await state.store.close()
        response = await client.post(f"/api/buttons/{seeded.button_id}/press")

    assert response.status_code == 503
    body = cast(dict[str, object], response.json())
    error = cast(dict[str, object], body["error"])
    assert error["code"] == "store_unavailable"


async def test_a_legacy_sounddevice_backend_is_migrated_to_mpv(
    tmp_path: Path,
    fake_backend: RecordingBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pre-migration ``audio.backend = "sounddevice"`` row is rewritten to ``"mpv"``.

    The row is written with raw SQLite, which is how an older build's database
    contains a value the registry now rejects. The repair runs at startup, so the
    settings page and every later reader see ``"mpv"`` instead of a value no
    backend implements — and a press plays through the injected speaker.

    :param tmp_path: pytest temporary directory.
    :param fake_backend: speaker the offline wiring hands to the app.
    :param caplog: captured log records.
    """
    seeded = await _seed(tmp_path)
    _write_raw_setting(tmp_path / "doorbell.db", "audio.backend", "sounddevice")
    caplog.set_level(logging.INFO, logger="chime_deck")
    app = create_app(_config(tmp_path))
    state = _state(app)
    gate = asyncio.Event()
    fake_backend.gate = gate

    try:
        async with _running(app) as client:
            assert await state.store.get_setting("audio.backend") == "mpv"
            assert state.health.get("audio") is HealthStatus.READY
            assert state.chime.playback.is_running is True

            response = await client.post(f"/api/buttons/{seeded.button_id}/press")

            assert response.status_code == 200
            body = cast(dict[str, object], response.json())
            assert body["outcome"] == "queued"
            assert body["code"] is None
            assert body["detail"] is None
            assert fake_backend.playing is True
            gate.set()
    finally:
        gate.set()

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith("audio.backend is set to")
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "'sounddevice'" in message
    assert "'mpv'" in message
    assert warnings[0].exc_info is None


async def test_an_already_valid_audio_backend_is_not_rewritten(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A stored ``"mpv"`` is left exactly as it is: no write, no warning.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    database = tmp_path / "doorbell.db"
    await _seed(tmp_path, settings={"audio.backend": "mpv"})
    before = _raw_setting_timestamp(database, "audio.backend")
    caplog.set_level(logging.INFO, logger="chime_deck")
    app = create_app(_config(tmp_path))

    async with _running(app):
        pass

    assert _raw_setting_timestamp(database, "audio.backend") == before
    assert not [
        record
        for record in caplog.records
        if record.getMessage().startswith("audio.backend is set to")
    ]


async def test_a_backend_that_cannot_start_still_serves_health(
    tmp_path: Path,
    fake_backend: RecordingBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A speaker that will not start leaves the app serving and presses failing.

    The registry's ``detail`` is global and sticky and the MQTT bridge records a
    later one, so the audio detail is captured as it is written.

    :param tmp_path: pytest temporary directory.
    :param fake_backend: speaker told to fail its startup.
    :param monkeypatch: patcher used to observe the health writes.
    """
    seeded = await _seed(tmp_path)
    fake_backend.start_error = AudioBackendUnavailable("libmpv is not installed")
    app = create_app(_config(tmp_path))
    state = _state(app)
    recorded: list[tuple[str, HealthStatus, str | None]] = []
    original = state.health.set

    def capture(component: str, status: HealthStatus, detail: str | None = None) -> None:
        """Record one health write before forwarding it.

        :param component: component name.
        :param status: status being recorded.
        :param detail: actionable detail being recorded.
        """
        recorded.append((component, status, detail))
        original(component, status, detail)

    monkeypatch.setattr(state.health, "set", capture)

    async with _running(app) as client:
        health_response = await client.get("/api/health")
        assert health_response.status_code == 200
        assert state.health.get("audio") is HealthStatus.ERROR
        audio_details = [
            detail
            for component, status, detail in recorded
            if component == "audio" and status is HealthStatus.ERROR
        ]
        # At least one write, not exactly one: the first failure records the
        # detail and the recovery ladder may record another before the test ends.
        assert audio_details
        detail = cast(str, audio_details[0])
        assert "libmpv is not installed" in detail
        assert "check that libmpv is installed" in detail
        assert state.chime.playback.is_running is False

        response = await client.post(f"/api/buttons/{seeded.button_id}/press")
        body = cast(dict[str, object], response.json())
        assert body["outcome"] == "failed"
        assert body["code"] == "audio_error"
        assert fake_backend.played == []


async def test_a_broker_that_never_connects_still_serves_health(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """The application serves while the bridge keeps reconnecting.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker that never fires a CONNACK.
    """
    await _seed(tmp_path)
    app = create_app(_config(tmp_path))
    state = _state(app)

    async with _running(app) as client:
        response = await client.get("/api/health")

        assert response.status_code == 200
        assert state.health.get("mqtt") is HealthStatus.RECONNECTING
        assert fake_broker.clients
        assert state.chime.mqtt_generations == 1


async def test_a_blank_broker_host_builds_no_client(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """``mqtt.host = ""`` is not configured: no client, no socket, one generation.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker that must never be asked for a client.
    """
    await _seed(tmp_path, settings={"mqtt.host": ""})
    app = create_app(_config(tmp_path))
    state = _state(app)

    async with _running(app):
        assert state.health.get("mqtt") is HealthStatus.NOT_CONFIGURED
        assert fake_broker.clients == []
        assert state.chime.mqtt_generations == 1
        assert state.chime.bridge is not None


async def test_an_mqtt_setting_change_rebuilds_exactly_one_generation(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """A settings write retracts through the old bridge and starts exactly one new one.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker both generations route through.
    """
    seeded = await _seed(
        tmp_path,
        settings={"mqtt.host": "broker.test", "mqtt.instance_id": _READY_INSTANCE},
    )
    app = create_app(_config(tmp_path))
    state = _state(app)

    async with _running(app) as client:
        assert len(fake_broker.clients) == 1
        old_client = fake_broker.clients[0]
        fake_broker.connect(old_client)
        await wait_until(lambda: state.chime.bridge is not None and state.chime.bridge.connected)
        await wait_until(
            lambda: any(
                event[0] == "publish" and event[1] == seeded.discovery_topic
                for event in fake_broker.events
            )
        )

        press_before = state.chime.press
        bridge_before = state.chime.bridge
        events_before = len(fake_broker.events)

        response = await asyncio.wait_for(
            client.patch("/api/settings", json={"mqtt.instance_id": "second"}),
            timeout=_PATCH_TIMEOUT_S,
        )

        assert response.status_code == 200
        body = cast(dict[str, object], response.json())
        settings_snapshot = cast(dict[str, object], body["settings"])
        assert settings_snapshot["mqtt.instance_id"] == "second"

        assert state.chime.mqtt_generations == 2
        assert len(fake_broker.clients) == 2
        assert state.chime.press is press_before
        assert state.chime.bridge is not bridge_before

        # The retract payload travelled through the OLD, still-open bridge,
        # before anything was unsubscribed or stopped.
        events = fake_broker.events[events_before:]
        teardown = next(
            index
            for index, event in enumerate(events)
            if event[0] in {"unsubscribe", "disconnect", "loop_stop"}
        )
        retract = next(
            index
            for index, event in enumerate(events)
            if event[0] == "publish" and event[1] == seeded.discovery_topic
        )
        assert events[retract][2] == ""
        assert retract < teardown

        # The new generation really publishes again through its own client.
        new_topic = discovery_topic(
            MqttSettings(host="broker.test", instance_id="second"), seeded.slug
        )
        new_client = fake_broker.clients[1]
        fake_broker.connect(new_client)
        await wait_until(
            lambda: any(
                event[0] == "publish"
                and event[1] == new_topic
                and isinstance(event[2], str)
                and event[2] != ""
                for event in fake_broker.events[events_before:]
            )
        )


def _published_payloads(events: list[tuple[object, ...]], topic: str) -> list[Mapping[str, object]]:
    """Return every non-empty payload published to *topic*, decoded, in order.

    :param events: the broker's ordered event log, or a slice of it.
    :param topic: topic to filter on.
    """
    return [
        cast("Mapping[str, object]", json.loads(cast(str, event[2])))
        for event in events
        if event[0] == "publish" and event[1] == topic and isinstance(event[2], str) and event[2]
    ]


def _empty_publishes(events: list[tuple[object, ...]], topic: str) -> list[tuple[object, ...]]:
    """Return the retraction publishes (empty payloads) sent to *topic*.

    :param events: the broker's ordered event log, or a slice of it.
    :param topic: topic to filter on.
    """
    return [
        event for event in events if event[0] == "publish" and event[1] == topic and event[2] == ""
    ]


async def test_a_device_name_only_change_republishes_without_retracting(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """A rename rebuilds the generation and retracts nothing (HSD-vwdy).

    The display name lives only in the discovery ``device`` block, so the entities
    keep their config topics and unique ids; retracting them on a rename would
    delete and re-create them, splitting their history.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker every generation routes through.
    """
    seeded = await _seed(
        tmp_path,
        settings={"mqtt.host": "broker.test", "mqtt.instance_id": _READY_INSTANCE},
    )
    app = create_app(_config(tmp_path))
    state = _state(app)

    async with _running(app) as client:
        fake_broker.connect(fake_broker.clients[0])
        await wait_until(lambda: state.chime.bridge is not None and state.chime.bridge.connected)
        await wait_until(
            lambda: _published_payloads(fake_broker.events, seeded.discovery_topic) != []
        )
        old_payload = _published_payloads(fake_broker.events, seeded.discovery_topic)[-1]
        events_before = len(fake_broker.events)

        response = await asyncio.wait_for(
            client.patch("/api/settings", json={"device.name": "Hall Doorbell"}),
            timeout=_PATCH_TIMEOUT_S,
        )

        assert response.status_code == 200
        body = cast(dict[str, object], response.json())
        settings_snapshot = cast(dict[str, object], body["settings"])
        assert settings_snapshot["device.name"] == "Hall Doorbell"
        # The pin fires on this same write, so the identity is unchanged.
        assert settings_snapshot["mqtt.instance_id"] == _READY_INSTANCE

        assert state.chime.mqtt_generations == 2
        assert len(fake_broker.clients) == 2

        events = fake_broker.events[events_before:]
        assert _empty_publishes(events, seeded.discovery_topic) == []
        assert [
            event
            for event in events
            if event[0] == "publish"
            and isinstance(event[1], str)
            and event[1].endswith("/config")
            and event[2] == ""
        ] == []

        new_client = fake_broker.clients[1]
        fake_broker.connect(new_client)
        await wait_until(
            lambda: any(
                cast("Mapping[str, object]", payload["device"])["name"] == "Hall Doorbell"
                for payload in _published_payloads(
                    fake_broker.events[events_before:], seeded.discovery_topic
                )
            )
        )
        republished = _published_payloads(
            fake_broker.events[events_before:], seeded.discovery_topic
        )[-1]
    assert cast("Mapping[str, object]", republished["device"])["name"] == "Hall Doorbell"
    # Same entity: only the label moved.
    assert republished["unique_id"] == old_payload["unique_id"]
    assert republished["command_topic"] == old_payload["command_topic"]
    assert (
        cast("Mapping[str, object]", republished["device"])["identifiers"]
        == cast("Mapping[str, object]", old_payload["device"])["identifiers"]
    )
    assert republished["unique_id"] == unique_id(
        MqttSettings(host="broker.test", instance_id=_READY_INSTANCE), seeded.slug
    )


async def test_two_concurrent_mqtt_patches_rebuild_once(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """The reload lock means overlapping writes cannot leave extra bridges behind.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker every generation routes through.
    """
    await _seed(
        tmp_path,
        settings={"mqtt.host": "broker.test", "mqtt.instance_id": _READY_INSTANCE},
    )
    app = create_app(_config(tmp_path))
    state = _state(app)

    async with _running(app) as client:
        fake_broker.connect(fake_broker.clients[0])
        await wait_until(lambda: state.chime.bridge is not None and state.chime.bridge.connected)

        async def patch() -> httpx.Response:
            """Send the same settings write; both must succeed."""
            return await client.patch("/api/settings", json={"mqtt.instance_id": "second"})

        responses = await asyncio.wait_for(
            asyncio.gather(patch(), patch()), timeout=_PATCH_TIMEOUT_S
        )

        assert [response.status_code for response in responses] == [200, 200]
        assert state.chime.mqtt_generations == 2
        assert len(fake_broker.clients) == 2


async def test_unusable_stored_mqtt_settings_keep_the_running_generation(
    tmp_path: Path,
    fake_broker: FakeBroker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stored value ``MqttSettings`` rejects is a warning, never a rebuild.

    ``mqtt.instance_id`` is a plain string in the registry, so ``"bad/id"`` can
    be stored; only ``MqttSettings`` rejects it.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the running generation routes through.
    :param caplog: captured log records.
    """
    seeded = await _seed(
        tmp_path,
        settings={"mqtt.host": "broker.test", "mqtt.instance_id": _READY_INSTANCE},
    )
    app = create_app(_config(tmp_path))
    state = _state(app)
    caplog.set_level(logging.DEBUG, logger="chime_deck")

    async with _running(app):
        fake_broker.connect(fake_broker.clients[0])
        await wait_until(lambda: state.chime.bridge is not None and state.chime.bridge.connected)
        bridge_before = state.chime.bridge
        retracts_before = [
            event for event in fake_broker.events if event[0] == "publish" and event[2] == ""
        ]

        # Stored directly, exactly as an older build could have written it.
        await state.store.set_setting("mqtt.instance_id", "bad/id")

        assert state.chime.bridge is bridge_before
        assert bridge_before is not None
        assert bridge_before.settings.instance_id == _READY_INSTANCE
        assert state.chime.mqtt_generations == 1
        assert len(fake_broker.clients) == 1
        assert [
            event for event in fake_broker.events if event[0] == "publish" and event[2] == ""
        ] == retracts_before
        discovery = state.chime.discovery
        assert discovery is not None
        assert discovery.published.get(seeded.slug)
        assert state.health.get("mqtt") is HealthStatus.ERROR

    warnings = [
        record
        for record in caplog.records
        if "keeping the running MQTT bridge" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert all(record.exc_info is None for record in caplog.records)


async def test_pristine_wiring_really_opens_the_production_devices() -> None:
    """The untouched ``Wiring()`` and ``resolve_backend`` select real devices.

    The autouse conftest fixture hides the production defaults from every app
    this suite builds, so this is the one place that proves
    ``chime_deck.wiring`` still names a real speaker when nobody injects a
    ``Wiring``. Nothing here starts a device. The non-``mpv`` values below can no
    longer be selected or written through settings; they are only reachable from
    a hand-edited or pre-migration database, which the startup repair fixes.
    """
    pristine = Wiring()
    assert pristine.audio_backend is None
    assert pristine.mqtt_client_factory is None
    assert pristine.mqtt_rng is None
    assert pristine.press_clock is None
    assert pristine.mqtt_sleep is asyncio.sleep
    assert pristine.audio_sleep is asyncio.sleep
    assert pristine.audio_clock is time.monotonic

    mpv = resolve_backend("mpv", "usb")
    assert isinstance(mpv, MpvBackend)
    # ``MpvBackend`` keeps the hint private: ``start()`` is the only public way
    # to observe it, and this test must not touch a sound card.
    assert mpv._device_hint == "usb"

    for stored in ("sounddevice", "", None, "pulse"):
        backend = resolve_backend(stored, "usb")
        assert isinstance(backend, _UnavailableBackend)
        assert backend.playing is False
        assert repr(stored) in backend.reason
        assert "audio.backend" in backend.reason


async def test_the_buttons_module_has_no_import_cycle() -> None:
    """``chime_deck.api.buttons`` imports in a fresh interpreter."""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", "import chime_deck.api.buttons"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_the_stored_wake_lead_in_reaches_the_playback_worker(
    tmp_path: Path, fake_broker: FakeBroker, fake_backend: RecordingBackend
) -> None:
    """``audio.wake_lead_in_ms`` is read once at startup and handed to the worker.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the offline wiring hands to the app.
    :param fake_backend: speaker the offline wiring hands to the app.
    """
    await _seed(tmp_path, settings={"audio.wake_lead_in_ms": 2500})
    app = create_app(_config(tmp_path))
    state = _state(app)

    async with _running(app):
        assert state.chime.playback.wake_lead_in_ms == 2500


async def test_a_failing_wake_lead_in_read_uses_the_default_and_warns(
    tmp_path: Path,
    fake_broker: FakeBroker,
    fake_backend: RecordingBackend,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable setting must not stop the worker: it starts with the default.

    The following is the real store with one key's read broken, which is the
    same shape as a store whose settings table cannot be queried.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the offline wiring hands to the app.
    :param fake_backend: speaker the offline wiring hands to the app.
    :param caplog: captured log records.
    :param monkeypatch: patcher used to break one setting read.
    """
    await _seed(tmp_path)
    original = ConfigStore.get_setting

    async def broken_read(self: ConfigStore, name: str) -> object:
        """Raise for the lead-in key and delegate every other read.

        :param self: store the read was issued on.
        :param name: setting key being read.
        """
        if name == "audio.wake_lead_in_ms":
            raise RuntimeError("the settings table is unreadable")
        return await original(self, name)

    monkeypatch.setattr(ConfigStore, "get_setting", broken_read)
    caplog.set_level(logging.WARNING, logger="chime_deck")
    app = create_app(_config(tmp_path))
    state = _state(app)

    async with _running(app):
        assert state.chime.playback.wake_lead_in_ms == 1500

    assert "audio.wake_lead_in_ms could not be read" in caplog.text


async def test_a_tile_selection_reaches_the_broker(tmp_path: Path, fake_broker: FakeBroker) -> None:
    """The app-level seam announces the tile at startup and a tap reaches the broker.

    The selection sensor config is published by the first generation, and
    ``POST /api/tiles/{id}/selection`` publishes the retained ``on`` through the
    real ``SelectionService.set_on_change`` -> ``ChimePipeline.notify_selection``
    path, which is the only place the two consumers meet.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the offline wiring handed to the app.
    """
    await _seed(tmp_path)
    delivered = MqttSettings(host="broker.test", instance_id=_READY_INSTANCE)
    app = create_app(_config(tmp_path))
    state = _state(app)

    async with _running(app) as client:
        tile = await state.store.upsert_entity(
            EntityIn(entity_id="lock.front_door", display_name="Front Door")
        )
        # The slug comes from the tile's display name (HSD-jw0x), so it is computed
        # from the row the store just returned rather than from the entity id.
        slug = selection_slug(tile)
        config_topic = selection_discovery_topic(delivered, slug)
        state_topic = selection_state_topic(delivered, slug)
        fake_broker.connect(fake_broker.clients[0])
        await wait_until(lambda: state.chime.bridge is not None and state.chime.bridge.connected)
        await wait_until(
            lambda: any(
                event[0] == "publish"
                and event[1] == config_topic
                and isinstance(event[2], str)
                and event[2] != ""
                for event in fake_broker.events
            )
        )

        response = await asyncio.wait_for(
            client.post(f"/api/tiles/{tile.id}/selection"), timeout=_HTTP_TIMEOUT_S
        )

        assert response.status_code == 200
        body = cast(dict[str, object], response.json())
        assert body["selected"] is True
        assert ("publish", state_topic, "on", 1, True) in fake_broker.events
        assert fake_broker.retained[state_topic] == b"on"
