"""StateHub: coalescing, drop-oldest backpressure and lifecycle.

Pure asyncio: no sockets and no sleeps. The hub fixture starts the real
subscription and flush task but with a flush interval far longer than any test,
so every assertion is driven by an explicit ``await hub.flush_once()`` instead
of by the wall clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio

from chime_deck.entity_info import EntityContext
from chime_deck.health import HealthRegistry, HealthStatus
from chime_deck.hub import DEFAULT_HEARTBEAT_INTERVAL, HUB_STOP_TIMEOUT_S, HubClient, StateHub
from chime_deck.icons import presentation_hints
from chime_deck.selection import SelectionService
from chime_deck.store import ButtonIn, ConfigStore, Entity, EntityIn

pytestmark = pytest.mark.asyncio

_NEVER = 3600.0
"""Flush interval used by the fixture: the background tick never fires."""

_EMPTY_HEALTH = {"ha": "not_configured", "mqtt": "not_configured", "audio": "disabled"}


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


@pytest.fixture
def health() -> HealthRegistry:
    """Return an empty health registry."""
    return HealthRegistry()


@pytest_asyncio.fixture
async def hub(store: ConfigStore, health: HealthRegistry) -> AsyncIterator[StateHub]:
    """Yield a started hub with two-message queues, stopped on teardown.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    instance = StateHub(store, health, queue_size=2, flush_interval=_NEVER)
    health.set_on_change(instance.notify_health)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


def _drain(client: HubClient) -> list[dict[str, object]]:
    """Return and decode every message currently queued for *client*.

    :param client: client registered with the hub.
    """
    messages: list[dict[str, object]] = []
    while not client.queue.empty():
        raw = client.queue.get_nowait()
        messages.append(cast(dict[str, object], json.loads(raw)))
    return messages


def _data(message: dict[str, object]) -> dict[str, object]:
    """Return the ``data`` payload of one decoded envelope.

    :param message: decoded ``{"type": ..., "data": ...}`` message.
    """
    return cast(dict[str, object], message["data"])


class _FakeSeam:
    """A ``LiveEntityInfo`` that answers from a script and records every call."""

    def __init__(
        self,
        contexts: Mapping[str, EntityContext] | None,
        *,
        delay_s: float = 0.0,
    ) -> None:
        """Create a seam that returns *contexts* (or ``None``) after *delay_s*.

        :param contexts: the answer, or ``None`` for "could not ask".
        :param delay_s: seconds the answer waits, to force the caller's timeout.
        """
        self.calls: list[list[str]] = []
        self._contexts = contexts
        self._delay_s = delay_s

    async def contexts_for(self, entity_ids: Sequence[str]) -> Mapping[str, EntityContext] | None:
        """Record the requested ids and return the scripted answer.

        :param entity_ids: ids the hub asked for.
        """
        self.calls.append(list(entity_ids))
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return self._contexts


def _rows(snapshot: dict[str, object]) -> list[dict[str, object]]:
    """Return the entity rows of one snapshot message.

    :param snapshot: decoded ``snapshot`` message.
    """
    return cast("list[dict[str, object]]", _data(snapshot)["entities"])


async def test_register_creates_a_bounded_queue(hub: StateHub) -> None:
    """Each client gets its queue capped at the configured size.

    :param hub: started hub.
    """
    client = hub.register()

    assert isinstance(client.queue, asyncio.Queue)
    assert client.queue.maxsize == 2
    assert client.queue.empty()


async def test_unregister_stops_delivery(hub: StateHub) -> None:
    """A client removed from the hub receives nothing more.

    :param hub: started hub.
    """
    client = hub.register()
    hub.unregister(client)

    hub.notify_entity_state(entity_id="lock.front_door", state="locked")
    await hub.flush_once()

    assert _drain(client) == []


async def test_snapshot_shape(hub: StateHub, store: ConfigStore) -> None:
    """The snapshot carries the four-key envelope and one configuration-only tile.

    :param hub: started hub.
    :param store: connected configuration store.
    """
    entity = await store.upsert_entity(EntityIn(entity_id="lock.front_door"))
    button = await store.upsert_button(
        ButtonIn(name="Front Door", action_type="tts", tts_text="ding")
    )

    message = await hub.snapshot_message()
    data = _data(message)

    assert message["type"] == "snapshot"
    assert set(data) == {"entities", "buttons", "health", "config_version"}
    assert data["entities"] == [
        {
            "id": entity.id,
            "entity_id": "lock.front_door",
            "display_name": None,
            "state": None,
            "attributes": {},
            "icon": "mdi:lock",
            "icon_source": "fallback",
            "accent_color": None,
            "last_changed": None,
            "displayed_state": None,
            "unit_of_measurement": None,
            "attribute_hints": [],
            "tone": None,
            "hide_when_unavailable": False,
            "selected": False,
        }
    ]
    assert data["buttons"] == [
        {
            "id": button.id,
            "name": "Front Door",
            "slug": "front-door",
            "show_on_dashboard": False,
        }
    ]
    assert data["health"] == _EMPTY_HEALTH
    assert data["config_version"] == hub.config_version


async def test_snapshot_resolves_a_custom_icon_from_the_icon_directory(
    store: ConfigStore, health: HealthRegistry, tmp_path: Path
) -> None:
    """A ``custom:`` file that exists is the user's icon in the snapshot.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    :param tmp_path: pytest temporary directory.
    """
    icons = tmp_path / "icons"
    icons.mkdir()
    (icons / "cat.svg").write_text("<svg/>", encoding="utf-8")
    await store.upsert_entity(EntityIn(entity_id="lock.front_door", icon="custom:cat.svg"))
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER, icons_dir=icons)

    (row,) = cast("list[dict[str, object]]", _data(await hub.snapshot_message())["entities"])

    assert row["icon"] == "custom:cat.svg"
    assert row["icon_source"] == "user"


async def test_snapshot_falls_back_when_a_custom_icon_file_vanished(
    store: ConfigStore, health: HealthRegistry, tmp_path: Path
) -> None:
    """The same stored reference degrades once its file is gone.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    :param tmp_path: pytest temporary directory.
    """
    icons = tmp_path / "icons"
    icons.mkdir()
    await store.upsert_entity(EntityIn(entity_id="lock.front_door", icon="custom:cat.svg"))
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER, icons_dir=icons)

    (row,) = cast("list[dict[str, object]]", _data(await hub.snapshot_message())["entities"])

    assert row["icon"] == "mdi:lock"
    assert row["icon_source"] == "fallback"


async def test_snapshot_renders_a_row_the_api_would_not_accept(
    hub: StateHub, store: ConfigStore
) -> None:
    """A historical entity id is a default icon, never a failed snapshot.

    :param hub: started hub.
    :param store: connected configuration store.
    """
    await store.upsert_entity(EntityIn(entity_id="front door"))

    (row,) = cast("list[dict[str, object]]", _data(await hub.snapshot_message())["entities"])

    assert row["icon"] == "mdi:help-circle"
    assert row["icon_source"] == "default"


async def test_entity_states_coalesce_per_entity_last_wins(hub: StateHub) -> None:
    """Three updates for one entity become one message carrying the last state.

    :param hub: started hub.
    """
    client = hub.register()
    for state in ("locked", "unlocked", "jammed"):
        hub.notify_entity_state(entity_id="lock.front_door", state=state)
    hub.notify_entity_state(entity_id="light.hall", state="on")

    await hub.flush_once()
    messages = _drain(client)

    assert [message["type"] for message in messages] == ["entity_state", "entity_state"]
    assert {
        cast(str, _data(message)["entity_id"]): _data(message)["state"] for message in messages
    } == {
        "lock.front_door": "jammed",
        "light.hall": "on",
    }


async def test_entity_state_carries_attributes_and_timestamp(hub: StateHub) -> None:
    """The complete frame shape survives the queue, presentation block included.

    :param hub: started hub.
    """
    client = hub.register()
    hub.notify_entity_state(
        entity_id="sensor.kitchen",
        state="21.4",
        attributes={"battery_level": 91},
        last_changed="2026-01-01T00:00:00Z",
    )

    await hub.flush_once()
    (message,) = _drain(client)

    assert _data(message) == {
        "entity_id": "sensor.kitchen",
        "state": "21.4",
        "attributes": {"battery_level": 91},
        "last_changed": "2026-01-01T00:00:00Z",
        "selected": False,
        "displayed_state": "21.4",
        "unit_of_measurement": None,
        "attribute_hints": [],
        "tone": "off",
    }


async def test_entity_state_lock_delta_carries_the_new_word(hub: StateHub) -> None:
    """An unlocked lock says ``Unlocked`` on the delta, not just in the snapshot.

    :param hub: started hub.
    """
    client = hub.register()

    hub.notify_entity_state(entity_id="lock.front_door", state="locked", attributes={})
    await hub.flush_once()
    hub.notify_entity_state(entity_id="lock.front_door", state="unlocked", attributes={})
    await hub.flush_once()
    locked, unlocked = _drain(client)

    assert _data(locked)["displayed_state"] == "Locked"
    assert _data(unlocked)["displayed_state"] == "Unlocked"


async def test_entity_state_word_follows_the_device_class(hub: StateHub) -> None:
    """A garage door reads ``Open`` on a delta, as its snapshot row would.

    :param hub: started hub.
    """
    client = hub.register()
    hub.notify_entity_state(
        entity_id="binary_sensor.garage_door",
        state="on",
        attributes={"device_class": "garage_door"},
    )

    await hub.flush_once()
    (message,) = _drain(client)

    assert _data(message)["displayed_state"] == "Open"


async def test_entity_state_keeps_the_unit_on_unavailable(hub: StateHub) -> None:
    """The frame mirrors ``presentation_hints``; hiding a unit is the client's job.

    :param hub: started hub.
    """
    attributes = {"unit_of_measurement": "°C"}
    hints = presentation_hints("sensor", state="unavailable", attributes=attributes)
    client = hub.register()
    hub.notify_entity_state(entity_id="sensor.hall", state="unavailable", attributes=attributes)

    await hub.flush_once()
    (message,) = _drain(client)

    data = _data(message)
    assert data["displayed_state"] == hints.displayed_state == "Unavailable"
    assert data["unit_of_measurement"] == hints.unit_of_measurement == "°C"
    assert data["tone"] == hints.tone == "off"


async def test_entity_state_hints_follow_a_changed_attribute(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """A configured ``show_attributes`` reaches the delta and tracks the value.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    await store.upsert_entity(EntityIn(entity_id="sensor.hall", show_attributes=("battery_level",)))
    hub = StateHub(store, health, queue_size=8, flush_interval=_NEVER)
    client = hub.register()

    hub.notify_entity_state(entity_id="sensor.hall", state="21.4", attributes={"battery_level": 91})
    await hub.flush_once()
    hub.notify_entity_state(entity_id="sensor.hall", state="21.5", attributes={"battery_level": 42})
    await hub.flush_once()
    first, second = _drain(client)

    expected = [{"name": "battery_level", "label": "Battery level", "value": "91"}]
    assert _data(first)["attribute_hints"] == expected
    assert _data(second)["attribute_hints"] == [
        {"name": "battery_level", "label": "Battery level", "value": "42"}
    ]


async def test_entity_state_of_an_unconfigured_entity_has_no_attribute_hints(
    hub: StateHub,
) -> None:
    """Nothing configured means no requested attributes, so no hints are invented.

    :param hub: started hub with an empty store.
    """
    client = hub.register()
    hub.notify_entity_state(
        entity_id="sensor.stray", state="21.4", attributes={"battery_level": 91}
    )

    await hub.flush_once()
    (message,) = _drain(client)

    assert _data(message)["attribute_hints"] == []


async def test_show_attributes_are_read_once_and_reread_after_an_entity_change(
    store: ConfigStore, health: HealthRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The map costs one store read, and an entity change is what invalidates it.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    :param monkeypatch: fixture used to count ``list_entities`` calls.
    """
    await store.upsert_entity(EntityIn(entity_id="sensor.hall", show_attributes=("battery_level",)))
    original = store.list_entities
    calls = 0

    async def counting() -> list[Entity]:
        nonlocal calls
        calls += 1
        return await original()

    monkeypatch.setattr(store, "list_entities", counting)
    hub = StateHub(store, health, queue_size=8, flush_interval=_NEVER)
    client = hub.register()
    await hub.start()
    try:
        hub.notify_entity_state(
            entity_id="sensor.hall", state="21.4", attributes={"battery_level": 91}
        )
        await hub.flush_once()
        hub.notify_entity_state(
            entity_id="sensor.hall", state="21.5", attributes={"battery_level": 42}
        )
        await hub.flush_once()
        assert calls == 1
        assert [_data(message)["attribute_hints"] for message in _drain(client)] == [
            [{"name": "battery_level", "label": "Battery level", "value": "91"}],
            [{"name": "battery_level", "label": "Battery level", "value": "42"}],
        ]

        # The write itself publishes the entity event, so the hub must re-read
        # before it can hint the next entity it has never seen listed.
        await store.upsert_entity(
            EntityIn(entity_id="sensor.porch", show_attributes=("battery_level",))
        )
        hub.notify_entity_state(
            entity_id="sensor.porch", state="on", attributes={"battery_level": 7}
        )
        await hub.flush_once()
        assert calls == 2
        pending = [message for message in _drain(client) if message["type"] == "entity_state"]
        (porch,) = pending
        assert _data(porch)["attribute_hints"] == [
            {"name": "battery_level", "label": "Battery level", "value": "7"}
        ]
    finally:
        await hub.stop()


async def test_a_failing_entity_read_still_delivers_the_frame_and_warns_once(
    store: ConfigStore,
    health: HealthRegistry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A store that cannot list entities degrades the hints, it does not drop frames.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    :param monkeypatch: fixture used to make ``list_entities`` raise.
    :param caplog: captured log records.
    """
    calls = 0

    async def exploding() -> list[Entity]:
        nonlocal calls
        calls += 1
        message = "database is gone"
        raise RuntimeError(message)

    monkeypatch.setattr(store, "list_entities", exploding)
    hub = StateHub(store, health, queue_size=8, flush_interval=_NEVER)
    client = hub.register()

    with caplog.at_level(logging.WARNING, logger="chime_deck.hub"):
        hub.notify_entity_state(
            entity_id="lock.front_door",
            state="unavailable",
            attributes={"unit_of_measurement": "°C"},
        )
        await hub.flush_once()
        hub.notify_entity_state(entity_id="lock.front_door", state="locked", attributes={})
        await hub.flush_once()
    first, second = _drain(client)

    assert _data(first)["displayed_state"] == "Unavailable"
    assert _data(first)["unit_of_measurement"] == "°C"
    assert _data(first)["attribute_hints"] == []
    assert _data(second)["displayed_state"] == "Locked"
    assert _data(second)["attribute_hints"] == []
    assert calls == 1
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "no presentation block until the next entity change" in warnings[0]


async def test_the_snapshot_row_and_the_delta_carry_the_same_tone(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """One state, one word and one colour, whichever frame carries it.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    await store.upsert_entity(EntityIn(entity_id="lock.front_door"))
    seam = _FakeSeam(
        {
            "lock.front_door": EntityContext(
                entity_id="lock.front_door",
                found=True,
                state="unlocked",
                attributes={},
                last_changed="2026-01-01T00:00:00Z",
            )
        }
    )
    hub = StateHub(store, health, queue_size=8, flush_interval=_NEVER, live_entities=seam)
    client = hub.register()

    hub.notify_entity_state(entity_id="lock.front_door", state="unlocked", attributes={})
    await hub.flush_once()
    (delta,) = _drain(client)
    (row,) = _rows(await hub.snapshot_message())

    assert row["displayed_state"] == _data(delta)["displayed_state"] == "Unlocked"
    assert row["tone"] == _data(delta)["tone"] == "alert"


async def test_an_explicit_override_beats_the_default_for_that_tile_only(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """Two tiles in the same state disagree on the colour, not on the word.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    await store.upsert_entity(
        EntityIn(entity_id="sensor.override", state_tones={"running": "alert"})
    )
    await store.upsert_entity(EntityIn(entity_id="sensor.default"))
    hub = StateHub(store, health, queue_size=8, flush_interval=_NEVER)
    client = hub.register()

    hub.notify_entity_state(entity_id="sensor.override", state="running", attributes={})
    hub.notify_entity_state(entity_id="sensor.default", state="running", attributes={})
    await hub.flush_once()
    frames = {cast(str, _data(message)["entity_id"]): _data(message) for message in _drain(client)}

    assert frames["sensor.override"]["tone"] == "alert"
    assert frames["sensor.default"]["tone"] == "warn"
    assert frames["sensor.override"]["displayed_state"] == "Running"
    assert frames["sensor.default"]["displayed_state"] == "Running"


async def test_colour_by_state_false_sends_off_while_the_word_still_moves(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """The opt-out colours nothing, but the word keeps tracking the state.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    await store.upsert_entity(EntityIn(entity_id="lock.front_door", colour_by_state=False))
    hub = StateHub(store, health, queue_size=8, flush_interval=_NEVER)
    client = hub.register()

    hub.notify_entity_state(entity_id="lock.front_door", state="locked", attributes={})
    await hub.flush_once()
    hub.notify_entity_state(entity_id="lock.front_door", state="unlocked", attributes={})
    await hub.flush_once()
    locked, unlocked = _drain(client)

    assert _data(locked)["displayed_state"] == "Locked"
    assert _data(unlocked)["displayed_state"] == "Unlocked"
    assert _data(locked)["tone"] == _data(unlocked)["tone"] == "off"


async def test_presses_are_never_coalesced(store: ConfigStore, health: HealthRegistry) -> None:
    """Every press is delivered, in FIFO order.

    A queue large enough for the burst is used here so the test observes
    coalescing only: drop-oldest with two slots would be the thing under test.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    hub = StateHub(store, health, queue_size=4, flush_interval=_NEVER)
    client = hub.register()
    stamps = ["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"]
    for stamp in stamps:
        hub.notify_press(button_id=1, button_name="Front Door", outcome="played", pressed_at=stamp)

    await hub.flush_once()
    messages = _drain(client)

    assert [message["type"] for message in messages] == ["press", "press", "press"]
    assert [cast(str, _data(message)["pressed_at"]) for message in messages] == stamps


async def test_config_changes_coalesce_per_what(hub: StateHub, store: ConfigStore) -> None:
    """Two settings writes between flushes become one ``config_changed``.

    :param hub: started hub.
    :param store: connected configuration store.
    """
    client = hub.register()

    await store.set_setting("ui.theme", "light")
    await store.set_setting("ui.show_clock", False)

    assert hub.config_version == 2

    await hub.flush_once()
    messages = _drain(client)

    assert messages == [{"type": "config_changed", "data": {"what": "settings"}}]

    await hub.flush_once()
    assert _drain(client) == []

    snapshot = await hub.snapshot_message()
    assert _data(snapshot)["config_version"] == 2


async def test_config_version_counts_entity_writes_too(hub: StateHub, store: ConfigStore) -> None:
    """Any store mutation bumps the counter, and the message names its table.

    :param hub: started hub.
    :param store: connected configuration store.
    """
    client = hub.register()

    await store.upsert_entity(EntityIn(entity_id="lock.front_door"))

    await hub.flush_once()

    assert _drain(client) == [{"type": "config_changed", "data": {"what": "entities"}}]
    assert hub.config_version == 1


async def test_health_is_last_wins_and_only_when_dirty(
    hub: StateHub, health: HealthRegistry
) -> None:
    """Two registry changes between flushes send one health message.

    :param hub: started hub.
    :param health: health registry the hub broadcasts.
    """
    client = hub.register()

    health.set("ha", HealthStatus.CONNECTED)
    health.set("mqtt", HealthStatus.RECONNECTING, detail="broker refused")

    assert _drain(client) == []

    await hub.flush_once()
    messages = _drain(client)

    assert len(messages) == 1
    assert messages[0]["type"] == "health"
    assert _data(messages[0]) == {
        "ha": "connected",
        "mqtt": "reconnecting",
        "audio": "disabled",
        "detail": "broker refused",
    }

    await hub.flush_once()
    assert _drain(client) == []


async def test_flush_with_nothing_pending_is_a_no_op(hub: StateHub) -> None:
    """An idle flush sends nothing at all.

    :param hub: started hub.
    """
    client = hub.register()

    await hub.flush_once()

    assert _drain(client) == []


async def test_heartbeat_frame_contract(hub: StateHub) -> None:
    """The heartbeat frame is exactly the envelope the client parses.

    :param hub: started hub.
    """
    client = hub.register()

    assert hub.heartbeat_if_due(client.next_heartbeat) == 1

    (message,) = _drain(client)
    assert message["type"] == "heartbeat"
    assert set(_data(message)) == {"interval_s"}
    assert _data(message)["interval_s"] == DEFAULT_HEARTBEAT_INTERVAL


async def test_heartbeat_waits_for_silence(hub: StateHub) -> None:
    """Nothing is sent before the deadline; the deadline itself serves one frame.

    :param hub: started hub.
    """
    client = hub.register()

    assert hub.heartbeat_if_due(client.next_heartbeat - 0.001) == 0
    assert _drain(client) == []

    assert hub.heartbeat_if_due(client.next_heartbeat) == 1
    (message,) = _drain(client)
    assert message["type"] == "heartbeat"


async def test_a_delivered_frame_postpones_the_heartbeat(hub: StateHub) -> None:
    """A client that received a frame is not silent, so its deadline moves later.

    :param hub: started hub.
    """
    client = hub.register()
    first_deadline = client.next_heartbeat

    hub.notify_entity_state(entity_id="light.zone_0", state="on")
    await hub.flush_once()

    assert client.next_heartbeat > first_deadline
    assert hub.heartbeat_if_due(first_deadline) == 0
    assert [message["type"] for message in _drain(client)] == ["entity_state"]

    assert hub.heartbeat_if_due(client.next_heartbeat) == 1
    (message,) = _drain(client)
    assert message["type"] == "heartbeat"


async def test_heartbeat_reaches_every_silent_client(hub: StateHub) -> None:
    """One call at the deadline serves both silent clients exactly once.

    :param hub: started hub.
    """
    first = hub.register()
    second = hub.register()
    deadline = max(first.next_heartbeat, second.next_heartbeat)

    assert hub.heartbeat_if_due(deadline) == 2

    for client in (first, second):
        (message,) = _drain(client)
        assert message["type"] == "heartbeat"


async def test_heartbeat_can_be_disabled(store: ConfigStore, health: HealthRegistry) -> None:
    """``heartbeat_interval=None`` never heartbeats and leaves an idle flush silent.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER, heartbeat_interval=None)
    client = hub.register()
    try:
        assert hub.heartbeat_if_due(client.next_heartbeat + 1e9) == 0
        await hub.flush_once()
        assert _drain(client) == []
    finally:
        await hub.stop()


async def test_the_flush_loop_sends_a_heartbeat(store: ConfigStore, health: HealthRegistry) -> None:
    """The existing flush loop, not a second task, is what heartbeats a client.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    hub = StateHub(store, health, queue_size=2, flush_interval=0.01, heartbeat_interval=0.05)
    try:
        await hub.start()
        client = hub.register()
        raw = await asyncio.wait_for(hub.next_payload(client), timeout=2.0)
        assert json.loads(raw)["type"] == "heartbeat"
    finally:
        await hub.stop()


async def test_slow_client_drops_oldest_without_blocking(hub: StateHub) -> None:
    """A client that never drains keeps only the newest messages and blocks nobody.

    :param hub: started hub.
    """
    fast = hub.register()
    slow = hub.register()
    delivered: list[str] = []

    for index in range(5):
        hub.notify_entity_state(entity_id=f"light.zone_{index}", state="on")
        await asyncio.wait_for(hub.flush_once(), timeout=1.0)

        assert slow.queue.qsize() <= 2
        (message,) = _drain(fast)
        delivered.append(cast(str, _data(message)["entity_id"]))

    assert delivered == [f"light.zone_{index}" for index in range(5)]
    # Queue size 2 after five flushes: the two most recent updates survive and
    # the newest is at the tail, which is what "drop-oldest" means.
    assert [_data(message)["entity_id"] for message in _drain(slow)] == [
        "light.zone_3",
        "light.zone_4",
    ]


async def test_queue_of_one_keeps_only_the_newest(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """With a single slot the head of the queue is always the newest message.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    hub = StateHub(store, health, queue_size=1, flush_interval=_NEVER)
    client = hub.register()

    for index in range(3):
        hub.notify_entity_state(entity_id=f"light.zone_{index}", state="on")
        await asyncio.wait_for(hub.flush_once(), timeout=1.0)
        assert client.queue.qsize() == 1

    (message,) = _drain(client)
    assert _data(message)["entity_id"] == "light.zone_2"


async def test_stop_cancels_tasks_is_idempotent_and_unsubscribes(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """Shutdown leaves no task behind, can be repeated, and detaches from the bus.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    current = asyncio.current_task()
    before = asyncio.all_tasks() - {current}
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER)

    await hub.start()
    assert hub.active_task_count == 1

    await hub.stop()
    assert hub.active_task_count == 0
    assert asyncio.all_tasks() - {current} == before

    await hub.stop()
    assert hub.active_task_count == 0

    version = hub.config_version
    await store.set_setting("ui.theme", "light")
    assert hub.config_version == version


async def test_start_is_idempotent(store: ConfigStore, health: HealthRegistry) -> None:
    """Starting twice does not spawn a second flush task.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER)
    try:
        await hub.start()
        await hub.start()
        assert hub.active_task_count == 1
    finally:
        await hub.stop()


async def test_stop_before_start_is_a_no_op(store: ConfigStore, health: HealthRegistry) -> None:
    """Stopping a hub that never started is harmless.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    hub = StateHub(store, health)

    await hub.stop()

    assert hub.active_task_count == 0


async def test_stop_bounds_a_task_that_suppresses_cancellation(
    store: ConfigStore, health: HealthRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    """A task that swallows ``CancelledError`` cannot hold shutdown open.

    ``asyncio.gather`` would wait on it forever; :meth:`StateHub.stop` uses
    :func:`asyncio.wait` with a bound, logs that it gave up and returns.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    :param caplog: pytest log capture fixture.
    """
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER)
    await hub.start()
    release = asyncio.Event()
    swallowing = asyncio.Event()

    async def stubborn() -> None:
        try:
            await asyncio.sleep(_NEVER)
        except asyncio.CancelledError:
            # Signal that cancellation was delivered *and* swallowed, so the test
            # never has to guess whether the task had started yet.
            swallowing.set()
            await release.wait()

    task = asyncio.create_task(stubborn(), name="stubborn-hub-task")
    hub._tasks.add(task)
    started = asyncio.get_running_loop().time()
    stop_task = asyncio.create_task(hub.stop())
    try:
        with caplog.at_level(logging.WARNING, logger="chime_deck.hub"):
            # Deterministic: wait until ``stop`` has actually cancelled the task
            # and the task has swallowed it. On a slow host the previous version
            # could cancel a task that had not started, so it never swallowed the
            # cancellation and ``stop`` returned instantly (aarch64 board).
            await asyncio.wait_for(swallowing.wait(), 2.0)
            assert not stop_task.done(), "stop must be waiting, not returning early"
            await asyncio.wait_for(stop_task, HUB_STOP_TIMEOUT_S + 2)
    finally:
        release.set()
        # The task was cancelled; having swallowed CancelledError to await
        # ``release``, it finishes by *raising* it. Either outcome proves it did
        # not hang, which is the point of the test.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 2.0)
        assert task.done()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed >= HUB_STOP_TIMEOUT_S - 0.5
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "chime_deck.hub" and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "state hub did not stop within" in warnings[0]


async def test_snapshot_carries_live_state_through_the_seam(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """A live context fills the state, attributes, icon and hints of its row.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    await store.upsert_entity(EntityIn(entity_id="sensor.hall", show_attributes=("battery_level",)))
    attributes = {
        "unit_of_measurement": "°C",
        "battery_level": 91,
        "icon": "mdi:thermometer",
    }
    seam = _FakeSeam(
        {
            "sensor.hall": EntityContext(
                entity_id="sensor.hall",
                found=True,
                state="21.4",
                attributes=attributes,
                last_changed="2026-01-01T00:00:00Z",
            )
        }
    )
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER, live_entities=seam)

    (row,) = _rows(await hub.snapshot_message())

    assert seam.calls == [["sensor.hall"]]
    assert row["state"] == "21.4"
    assert row["attributes"] == attributes
    assert row["last_changed"] == "2026-01-01T00:00:00Z"
    assert row["icon"] == "mdi:thermometer"
    assert row["icon_source"] == "ha-attribute"
    assert row["displayed_state"] == "21.4"
    assert row["tone"] == "off"
    assert row["unit_of_measurement"] == "°C"
    assert row["attribute_hints"] == [
        {"name": "battery_level", "label": "Battery level", "value": "91"}
    ]
    assert row["hide_when_unavailable"] is False


async def test_snapshot_keeps_the_stored_hide_when_unavailable_flag(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """The store's ``hide_when_unavailable`` column reaches the row unchanged.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    await store.upsert_entity(EntityIn(entity_id="lock.front_door", hide_when_unavailable=True))
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER)

    (row,) = _rows(await hub.snapshot_message())

    assert row["hide_when_unavailable"] is True


async def test_snapshot_degrades_to_configuration_when_the_seam_cannot_answer(
    store: ConfigStore, health: HealthRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    """``None`` from the seam is a configuration-only row, not a warning or a raise.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    :param caplog: captured log records.
    """
    await store.upsert_entity(EntityIn(entity_id="lock.front_door"))
    hub = StateHub(
        store, health, queue_size=2, flush_interval=_NEVER, live_entities=_FakeSeam(None)
    )

    with caplog.at_level(logging.WARNING, logger="chime_deck"):
        (row,) = _rows(await hub.snapshot_message())

    assert row["state"] is None
    assert row["attributes"] == {}
    assert row["last_changed"] is None
    assert row["icon"] == "mdi:lock"
    assert row["icon_source"] == "fallback"
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


async def test_snapshot_returns_inside_the_bound_when_the_seam_is_slow(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """A seam slower than ``live_lookup_timeout`` degrades instead of stalling the page.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    await store.upsert_entity(EntityIn(entity_id="lock.front_door"))
    hub = StateHub(
        store,
        health,
        queue_size=2,
        flush_interval=_NEVER,
        live_entities=_FakeSeam({}, delay_s=5.0),
        live_lookup_timeout=0.01,
    )

    started = asyncio.get_running_loop().time()
    (row,) = _rows(await hub.snapshot_message())
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.0
    assert row["state"] is None
    assert row["attributes"] == {}
    assert row["icon_source"] == "fallback"


async def test_set_live_entities_replaces_the_installed_seam(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """A second install wins: the setter replaces the seam the hub would ask.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    await store.upsert_entity(EntityIn(entity_id="lock.front_door"))
    first = _FakeSeam(None)
    second = _FakeSeam(
        {"lock.front_door": EntityContext(entity_id="lock.front_door", found=True, state="jammed")}
    )
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER)
    hub.set_live_entities(first)
    hub.set_live_entities(second, timeout_s=0.5)

    (row,) = _rows(await hub.snapshot_message())

    assert first.calls == []
    assert second.calls == [["lock.front_door"]]
    assert row["state"] == "jammed"


async def test_snapshot_rows_carry_the_selection_flag(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """A row reads ``False`` until the service is toggled, then ``True`` (HSD-u6k5).

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    entity = await store.upsert_entity(EntityIn(entity_id="lock.front_door"))
    selection = SelectionService()
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER, selection=selection)

    (before,) = _rows(await hub.snapshot_message())
    selection.toggle(entity.entity_id)
    (after,) = _rows(await hub.snapshot_message())

    assert before["selected"] is False
    assert after["selected"] is True


async def test_an_entity_state_delta_also_carries_the_selection(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """A plain state delta refreshes the border as well as the word.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    selection = SelectionService()
    selection.toggle("lock.front_door")
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER, selection=selection)
    client = hub.register()

    hub.notify_entity_state(entity_id="lock.front_door", state="unlocked", attributes={})
    await hub.flush_once()
    (message,) = _drain(client)

    assert _data(message)["selected"] is True
    assert _data(message)["displayed_state"] == "Unlocked"


async def test_a_selection_only_frame_carries_only_the_entity_and_the_flag(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """No ``state`` and no presentation keys, so the tile's word is never blanked.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    selection = SelectionService()
    selection.toggle("light.hall")
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER, selection=selection)
    client = hub.register()

    hub.notify_selection(entity_id="light.hall")
    await hub.flush_once()
    (message,) = _drain(client)

    assert message["type"] == "entity_state"
    assert set(_data(message)) == {"entity_id", "selected"}
    assert _data(message) == {"entity_id": "light.hall", "selected": True}


async def test_a_selection_toggled_twice_in_one_tick_coalesces_to_the_last_value(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """On then off inside one flush tick is one frame saying ``False``.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    selection = SelectionService()
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER, selection=selection)
    client = hub.register()

    hub.notify_selection(entity_id="lock.front_door")
    selection.toggle("lock.front_door")
    selection.toggle("lock.front_door")
    await hub.flush_once()
    (message,) = _drain(client)

    assert set(_data(message)) == {"entity_id", "selected"}
    assert _data(message)["selected"] is False
    assert selection.is_selected("lock.front_door") is False


async def test_a_selection_and_a_state_change_of_one_entity_are_one_frame(
    store: ConfigStore, health: HealthRegistry
) -> None:
    """The selection rides on the pending delta instead of duplicating it.

    :param store: connected configuration store.
    :param health: health registry the hub broadcasts.
    """
    selection = SelectionService()
    hub = StateHub(store, health, queue_size=2, flush_interval=_NEVER, selection=selection)
    client = hub.register()

    hub.notify_entity_state(entity_id="lock.front_door", state="locked", attributes={})
    hub.notify_selection(entity_id="lock.front_door")
    selection.toggle("lock.front_door")
    await hub.flush_once()
    (message,) = _drain(client)

    assert _data(message)["selected"] is True
    assert _data(message)["state"] == "locked"
    assert _drain(client) == []
