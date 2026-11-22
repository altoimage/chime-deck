"""Fully offline tests for Home Assistant's write half of the selection switches (D34).

The stack is real: a :class:`~chime_deck.store.store.ConfigStore` on a temporary
SQLite file, the real :class:`~chime_deck.selection.SelectionService`, the real
:class:`~chime_deck.mqtt.bridge.MqttBridge` wired to
:class:`~mqtt_fakes.FakeBroker`, and the real
:class:`~chime_deck.mqtt.discovery.MqttDiscovery` between them — wired through
the same ``set_on_change`` seam :func:`~chime_deck.app.create_app` installs. The
convergence test additionally drives the real :class:`~chime_deck.hub.StateHub`,
so the websocket frame and the retained MQTT publish are proven to come from the
one change callback rather than from parallel implementations.

The load-bearing claim is *convergence*: a panel tap and an inbound Home
Assistant command are indistinguishable — same service call, same hub frame,
same retained publish — and every refusal (a retained replay, an unknown slug, a
malformed payload) is answered with a corrective republish instead of silence.

Nothing here touches a broker, a socket, the network or a real timer beyond the
bounded :func:`~mqtt_fakes.wait_until` poll and the injected
:class:`~mqtt_fakes.RecordingSleep`/:class:`~mqtt_fakes.FixedRandom`.

Owning ticket: HSD-8a5f (D34).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

import pytest
import pytest_asyncio
from mqtt_fakes import FakeBroker, FakeClient, FixedRandom, RecordingSleep, count, wait_until

from chime_deck.health import HealthRegistry
from chime_deck.hub import HubClient, StateHub
from chime_deck.mqtt import (
    DEFAULT_READ_TIMEOUT_S,
    DISCOVERY_PUBLISH_QOS,
    SELECTION_STATE_QOS,
    MqttBridge,
    MqttDiscovery,
    MqttSettings,
    selection_state_topic,
)
from chime_deck.mqtt.discovery import (
    SELECTION_COMMAND_QOS,
    selection_command_filter,
    selection_command_topic,
    selection_read_only_discovery_topic,
)
from chime_deck.selection import SelectionService
from chime_deck.store import ButtonIn, ConfigStore, Entity, EntityIn, SoundIn

pytestmark = pytest.mark.asyncio

LOGGER_NAME: Final = "chime_deck.mqtt.discovery"
"""Logger the discovery service logs under, filtered on by the refusal assertions."""

CONFIG_PREFIX: Final = "homeassistant"
"""Discovery prefix of the default test settings."""

INSTANCE: Final = "default"
"""Instance id of the default test settings."""

HOST: Final = "broker.test"
"""Broker host used by the fake settings object."""

AVAILABILITY: Final = "chime_deck/default/availability"
"""Retained availability topic of the default instance."""

LOCK: Final = "lock.front_door"
"""Entity id of the tile every command test writes to."""

SLUG: Final = "front_door"
"""Selection slug derived from "Front Door"."""

FILTER: Final = "chime_deck/default/selection/+/set"
"""The one wildcard filter covering every tile's command topic (D34)."""

COMMAND_TOPIC: Final = "chime_deck/default/selection/front_door/set"
"""Command topic of :data:`LOCK`'s switch."""

STATE_TOPIC: Final = "chime_deck/default/selection/front_door"
"""Retained state topic of :data:`LOCK`'s switch."""

GHOST_COMMAND_TOPIC: Final = "chime_deck/default/selection/ghost/set"
"""Command topic of a slug no displayed tile has."""

GHOST_STATE_TOPIC: Final = "chime_deck/default/selection/ghost"
"""State topic answered when a command names a slug this process does not know."""

PRESS_TOPIC: Final = "chime_deck/default/button/front-door/set"
"""Command topic of the chime button the press-path test creates."""

PRESS_BUTTON_SLUG: Final = "front-door"
"""Slug of the chime button the press-path test creates."""

MALFORMED_PAYLOADS: Final[tuple[bytes, ...]] = (b"", b"ON", b"on ", b"\x00on", b"1")
"""Every payload Home Assistant could plausibly send that is not exactly on/off."""

_COMMAND_QOS: Final = 1
"""The QoS the command subscription must carry, pinned so a change is visible."""

_NEVER: Final = 3600.0
"""Flush interval far longer than any test, so only ``flush_once`` flushes."""


class RecordingPress:
    """A press handler that records every press instead of playing anything."""

    def __init__(self) -> None:
        """Create a press double that has seen no press yet."""
        self.calls: list[tuple[str, str]] = []

    async def press(self, slug: str, *, source: str = "mqtt") -> object:
        """Record one press and return nothing.

        :param slug: the button slug that was pressed.
        :param source: press-source tag.
        :returns: ``None``: this double plays nothing.
        """
        self.calls.append((slug, source))
        return None


@dataclass
class Harness:
    """The real stack under test, wired the way the application wires it."""

    bridge: MqttBridge
    discovery: MqttDiscovery
    broker: FakeBroker
    store: ConfigStore
    selection: SelectionService
    press: RecordingPress
    client_index: int = 0
    """Position of this bridge's client in ``broker.clients``, fixed at build time."""

    @property
    def client(self) -> FakeClient:
        """Return the client this harness's own bridge built.

        :returns: the fake client at this harness's fixed position.
        """
        return self.broker.clients[self.client_index]


def _settings() -> MqttSettings:
    """Build validated MQTT settings for one test service.

    :returns: settings for the default instance and discovery prefix.
    """
    return MqttSettings(host=HOST, discovery_prefix=CONFIG_PREFIX, instance_id=INSTANCE)


def _build(
    *,
    store: ConfigStore,
    selection: SelectionService,
    press: RecordingPress | None = None,
    broker: FakeBroker | None = None,
    hub: StateHub | None = None,
) -> Harness:
    """Build the bridge and discovery service wired to each other and to the hub.

    The ``set_on_change`` callback is exactly the application's: the hub is told
    *which* entity changed and re-reads the value at flush time, while the MQTT
    publisher is told the new value as well. One callback is why the frame and
    the publish cannot disagree.

    :param store: connected store the service reads buttons and entities from.
    :param selection: real selection service both writers share.
    :param press: press double; a fresh recording one when omitted.
    :param broker: broker to reuse, so a restart is observable on one broker.
    :param hub: state hub to notify as well, for the convergence proof.
    :returns: the wired harness, with the bridge's client index fixed.
    """
    broker = FakeBroker() if broker is None else broker
    press_double = RecordingPress() if press is None else press
    bridge = MqttBridge(
        settings=_settings(),
        client_factory=broker.factory,
        sleep=RecordingSleep(),
        rng=FixedRandom(0.0),
        seam_timeout_s=5.0,
        shutdown_timeout_s=1.0,
    )
    discovery = MqttDiscovery(
        bridge=bridge,
        buttons=store,
        press=press_double,
        bus=store.events,
        server_host="127.0.0.1",
        server_port=8080,
        read_timeout_s=DEFAULT_READ_TIMEOUT_S,
        entities=store,
        selection=selection,
    )
    bridge.set_on_birth(discovery.on_birth)
    bridge.set_on_message(discovery.on_message)

    def _on_selection_change(entity_id: str, selected: bool) -> None:
        """Forward one change to the hub and the MQTT publisher, as the app does.

        :param entity_id: Home Assistant entity id whose selection changed.
        :param selected: the new value.
        """
        if hub is not None:
            hub.notify_selection(entity_id=entity_id)
        discovery.on_selection_changed(entity_id, selected)

    selection.set_on_change(_on_selection_change)
    return Harness(
        bridge=bridge,
        discovery=discovery,
        broker=broker,
        store=store,
        selection=selection,
        press=press_double,
        client_index=len(broker.clients),
    )


@asynccontextmanager
async def _open_harness(
    db_path: Path,
    *,
    press: RecordingPress | None = None,
    broker: FakeBroker | None = None,
) -> AsyncIterator[Harness]:
    """Open a temporary store with one tile and yield the wired harness.

    :param db_path: temporary database file path.
    :param press: press double shared with the harness.
    :param broker: broker to reuse, for a simulated restart.
    """
    async with ConfigStore(db_path) as store:
        await store.upsert_entity(EntityIn(entity_id=LOCK, display_name="Front Door"))
        yield _build(store=store, selection=SelectionService(), press=press, broker=broker)


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    """Yield the real stack under test, with its own temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with _open_harness(tmp_path / "doorbell.db") as built:
        yield built


async def _connect(harness: Harness) -> None:
    """Start both halves, accept the broker's CONNACK and wait for a settled republish.

    The wait is on the second state publish because the bridge's CONNACK runs the
    birth seam after flushing the buffered start republish; waiting on the first
    would race the second and make a "one publish per command" assertion flaky.

    :param harness: the stack under test.
    """
    await harness.bridge.start()
    await harness.discovery.start()
    harness.broker.connect(harness.client)
    await wait_until(lambda: harness.bridge.connected)
    await wait_until(lambda: len(_published(harness, STATE_TOPIC)) >= 2)


def _published(harness: Harness, topic: str) -> list[tuple[str, object, object, object]]:
    """Return every publish the client made to *topic*, in order.

    :param harness: the stack under test.
    :param topic: topic to filter on.
    :returns: one ``(topic, payload, qos, retain)`` tuple per publish, in order.
    """
    return [
        (published_topic, payload, qos, retain)
        for published_topic, payload, qos, retain in harness.client.published
        if published_topic == topic
    ]


def _last_publish(harness: Harness, topic: str) -> tuple[str, object, object, object]:
    """Return the last publish recorded for *topic*, failing when there is none.

    :param harness: the stack under test.
    :param topic: topic to read.
    :returns: the final ``(topic, payload, qos, retain)`` tuple published there.
    """
    published = _published(harness, topic)
    assert published, f"nothing was published to {topic}"
    return published[-1]


def _install_recorder(harness: Harness, *, hub: StateHub | None = None) -> list[tuple[str, bool]]:
    """Replace the change callback with a recorder that also forwards.

    Recording *and* forwarding is what proves the callback fired exactly once
    *and* that the normal consequences still happened.

    :param harness: the stack under test.
    :param hub: hub to notify as well, when the test drives one.
    :returns: the recording list the installed callback appends to.
    """
    calls: list[tuple[str, bool]] = []

    def _record(entity_id: str, selected: bool) -> None:
        """Record one change and forward it to the normal consumers.

        :param entity_id: Home Assistant entity id whose selection changed.
        :param selected: the new value.
        """
        calls.append((entity_id, selected))
        if hub is not None:
            hub.notify_selection(entity_id=entity_id)
        harness.discovery.on_selection_changed(entity_id, selected)

    harness.selection.set_on_change(_record)
    return calls


async def _require_first_tile(store: ConfigStore) -> Entity:
    """Return the stored ``lock.front_door`` tile, failing when it is missing.

    :param store: connected store.
    :returns: the stored ``lock.front_door`` row.
    """
    for entity in await store.list_entities():
        if entity.entity_id == LOCK:
            return entity
    raise AssertionError(f"the store has no {LOCK} tile")


def _drain(client: HubClient) -> list[dict[str, object]]:
    """Return and decode every message currently queued for *client*.

    :param client: client registered with the hub.
    :returns: the decoded messages, in queue order.
    """
    messages: list[dict[str, object]] = []
    while not client.queue.empty():
        raw = client.queue.get_nowait()
        messages.append(cast("dict[str, object]", json.loads(raw)))
    return messages


def _info_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return every INFO line the discovery service logged.

    :param caplog: pytest log capture.
    :returns: the INFO messages the discovery service logged.
    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno == logging.INFO
    ]


# ------------------------------------------------------------------ subscription


async def test_the_command_filter_is_subscribed_once_at_qos_one_and_removed_on_stop(
    harness: Harness,
) -> None:
    """One wildcard subscription serves every tile, at-least-once, and stop drops it.

    At-least-once is the point (D29 §4): a missed ``off`` would leave a tile
    armed. One filter rather than one per tile is what lets a command for a
    slug this process has never heard of still arrive and be answered. And the
    recorded filter must be *popped* on stop, or ``clean_session`` would replay it
    on the next connect for the life of the bridge.

    :param harness: the stack under test.
    """
    await _connect(harness)

    assert selection_command_filter(_settings()) == FILTER
    assert selection_command_topic(_settings(), SLUG) == COMMAND_TOPIC
    assert count(harness.broker.events, ("subscribe", FILTER, _COMMAND_QOS)) == 1
    assert harness.bridge.subscriptions[FILTER] == SELECTION_COMMAND_QOS == _COMMAND_QOS

    await harness.discovery.stop()

    assert FILTER not in harness.bridge.subscriptions
    assert ("unsubscribe", FILTER) in harness.broker.events


# -------------------------------------------------------------------- commands


async def test_an_on_command_selects_the_tile_once(harness: Harness) -> None:
    """``on`` writes the service, fires one change, and publishes one retained ``on``.

    The retained publish comes from the single ``on_change`` callback, not from
    the command handler, so a broker packet cannot exist without the hub frame
    and vice versa.

    :param harness: the stack under test.
    """
    await _connect(harness)
    calls = _install_recorder(harness)
    mark = len(_published(harness, STATE_TOPIC))

    harness.broker.publish_from_test(COMMAND_TOPIC, b"on")
    await wait_until(lambda: harness.selection.is_selected(LOCK))

    assert calls == [(LOCK, True)]
    assert _published(harness, STATE_TOPIC)[mark:] == [
        (STATE_TOPIC, "on", SELECTION_STATE_QOS, True)
    ]
    assert harness.broker.retained[STATE_TOPIC] == b"on"


async def test_an_off_command_clears_a_selected_tile_once(harness: Harness) -> None:
    """``off`` after a panel tap deselects, fires one change, publishes one retained ``off``.

    :param harness: the stack under test.
    """
    await _connect(harness)
    assert harness.selection.toggle(LOCK) is True
    calls = _install_recorder(harness)
    mark = len(_published(harness, STATE_TOPIC))

    harness.broker.publish_from_test(COMMAND_TOPIC, b"off")
    await wait_until(lambda: harness.selection.is_selected(LOCK) is False)

    assert calls == [(LOCK, False)]
    assert _published(harness, STATE_TOPIC)[mark:] == [
        (STATE_TOPIC, "off", SELECTION_STATE_QOS, True)
    ]
    assert harness.broker.retained[STATE_TOPIC] == b"off"


async def test_a_home_assistant_command_and_a_panel_tap_are_indistinguishable(
    tmp_path: Path,
) -> None:
    """The convergence proof: both writers produce the same frame and the same publish.

    A panel tap calls ``selection.toggle`` (the one line ``POST
    /api/tiles/{id}/selection`` performs) and a Home Assistant command calls
    ``selection.set`` through the same service. The real hub is registered, so
    this test proves the websocket frame and the retained MQTT value come from
    one callback rather than two implementations that can drift.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "converge.db") as store:
        await store.upsert_entity(EntityIn(entity_id=LOCK, display_name="Front Door"))
        selection = SelectionService()
        hub = StateHub(
            store, HealthRegistry(), queue_size=8, flush_interval=_NEVER, selection=selection
        )
        harness = _build(store=store, selection=selection, hub=hub)
        await hub.start()
        client = hub.register()
        try:
            await _connect(harness)
            _drain(client)

            harness.broker.publish_from_test(COMMAND_TOPIC, b"on")
            await wait_until(lambda: selection.is_selected(LOCK))
            await hub.flush_once()
            command_frame = _drain(client)
            command_publish = _last_publish(harness, STATE_TOPIC)

            assert command_frame == [
                {"type": "entity_state", "data": {"entity_id": LOCK, "selected": True}}
            ]
            assert command_publish == (STATE_TOPIC, "on", SELECTION_STATE_QOS, True)

            # Reset through the panel write, drain its frame, then tap again.
            assert selection.toggle(LOCK) is False
            await hub.flush_once()
            _drain(client)

            mark = len(_published(harness, STATE_TOPIC))
            assert selection.toggle(LOCK) is True
            await hub.flush_once()
            tap_frame = _drain(client)
            tap_publish = _published(harness, STATE_TOPIC)[mark:]

            assert tap_frame == command_frame
            assert tap_publish == [command_publish]
            assert harness.broker.retained[STATE_TOPIC] == b"on"
        finally:
            await hub.stop()


async def test_a_repeated_command_confirms_without_a_second_change(tmp_path: Path) -> None:
    """A second ``on`` is an idempotent confirm: no change, one corrective publish.

    Home Assistant is optimistic: it has already drawn the switch on, so the
    confirm has to answer once so a lost first publish is repaired. But the value
    did not move, so the hub must get no frame at all.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "idempotent.db") as store:
        await store.upsert_entity(EntityIn(entity_id=LOCK, display_name="Front Door"))
        selection = SelectionService()
        hub = StateHub(
            store, HealthRegistry(), queue_size=8, flush_interval=_NEVER, selection=selection
        )
        harness = _build(store=store, selection=selection, hub=hub)
        await hub.start()
        client = hub.register()
        try:
            await _connect(harness)
            calls = _install_recorder(harness, hub=hub)
            harness.broker.publish_from_test(COMMAND_TOPIC, b"on")
            await wait_until(lambda: selection.is_selected(LOCK))
            calls.clear()
            await hub.flush_once()
            _drain(client)

            mark = len(_published(harness, STATE_TOPIC))
            harness.broker.publish_from_test(COMMAND_TOPIC, b"on")
            await wait_until(lambda: len(_published(harness, STATE_TOPIC)) == mark + 1)

            assert calls == []
            await hub.flush_once()
            assert _drain(client) == []
            assert _published(harness, STATE_TOPIC)[mark:] == [
                (STATE_TOPIC, "on", SELECTION_STATE_QOS, True)
            ]
        finally:
            await hub.stop()


# -------------------------------------------------------------------- refusals


async def test_a_retained_command_is_refused_and_corrected(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """A replayed retained command must never re-arm a tile after a reconnect.

    The broker sets RETAIN on a delivery it stored; acting on it would replay an
    old intent (``on`` from days ago) as if it were live. The refusal is answered
    with the current state so Home Assistant's optimistic switch snaps back.

    :param harness: the stack under test.
    :param caplog: pytest log capture.
    """
    await _connect(harness)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    mark = len(_published(harness, STATE_TOPIC))

    await harness.discovery.on_message(COMMAND_TOPIC, b"on", retain=True)

    assert harness.selection.is_selected(LOCK) is False
    assert _published(harness, STATE_TOPIC)[mark:] == [
        (STATE_TOPIC, "off", SELECTION_STATE_QOS, True)
    ]
    messages = _info_messages(caplog)
    assert any("retained" in message and COMMAND_TOPIC in message for message in messages), messages


async def test_a_retained_command_through_the_broker_is_refused(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """The same refusal end to end: the fake broker's replay flag reaches the handler.

    :param harness: the stack under test.
    :param caplog: pytest log capture.
    """
    await _connect(harness)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    mark = len(_published(harness, STATE_TOPIC))

    harness.broker.publish_from_test(COMMAND_TOPIC, b"on", retain=True)
    await wait_until(lambda: len(_published(harness, STATE_TOPIC)) > mark)

    assert harness.selection.is_selected(LOCK) is False
    assert _last_publish(harness, STATE_TOPIC) == (
        STATE_TOPIC,
        "off",
        SELECTION_STATE_QOS,
        True,
    )
    assert any(
        "retained" in message and COMMAND_TOPIC in message for message in _info_messages(caplog)
    )


async def test_a_command_for_an_unknown_slug_is_refused(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """A stale switch HA has not dropped is answered, never ignored and never fatal.

    Home Assistant keeps publishing to a switch's last known command topic after
    the tile is gone, so the wildcard subscription has to accept the slug and
    answer ``off`` (an unknown id is unselected by contract).

    :param harness: the stack under test.
    :param caplog: pytest log capture.
    """
    await _connect(harness)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    harness.broker.publish_from_test(GHOST_COMMAND_TOPIC, b"on")
    await wait_until(lambda: _published(harness, GHOST_STATE_TOPIC) != [])

    assert _last_publish(harness, GHOST_STATE_TOPIC) == (
        GHOST_STATE_TOPIC,
        "off",
        SELECTION_STATE_QOS,
        True,
    )
    assert any(
        "no displayed tile has that selection slug" in message and GHOST_COMMAND_TOPIC in message
        for message in _info_messages(caplog)
    )


async def test_a_command_for_a_deleted_tile_is_refused(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """Deleting a displayed tile retracts its slug, so a later command is a refusal.

    :param harness: the stack under test.
    :param caplog: pytest log capture.
    """
    await _connect(harness)
    assert harness.selection.toggle(LOCK) is True
    tile = await _require_first_tile(harness.store)
    await harness.store.delete_entity(tile.id)
    await wait_until(lambda: _published(harness, STATE_TOPIC)[-1][1] == "")
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    mark = len(_published(harness, STATE_TOPIC))

    harness.broker.publish_from_test(COMMAND_TOPIC, b"on")
    await wait_until(lambda: len(_published(harness, STATE_TOPIC)) > mark)

    assert _last_publish(harness, STATE_TOPIC) == (
        STATE_TOPIC,
        "off",
        SELECTION_STATE_QOS,
        True,
    )
    assert any(
        "no displayed tile has that selection slug" in message and COMMAND_TOPIC in message
        for message in _info_messages(caplog)
    )


@pytest.mark.parametrize("payload", MALFORMED_PAYLOADS)
async def test_a_malformed_command_is_refused(
    harness: Harness, caplog: pytest.LogCaptureFixture, payload: bytes
) -> None:
    """Only the exact bytes ``b"on"`` and ``b"off"`` set a tile; everything else is answered.

    The comparison is bytes equality, never a decoded string: ``ON``, a trailing
    space and a NUL-prefixed payload are all refused, and each refusal is
    corrected instead of raising.

    :param harness: the stack under test.
    :param caplog: pytest log capture.
    :param payload: payload that must be refused.
    """
    await _connect(harness)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    mark = len(_published(harness, STATE_TOPIC))

    harness.broker.publish_from_test(COMMAND_TOPIC, payload)
    await wait_until(lambda: len(_published(harness, STATE_TOPIC)) > mark)

    assert harness.selection.is_selected(LOCK) is False
    assert _last_publish(harness, STATE_TOPIC) == (
        STATE_TOPIC,
        "off",
        SELECTION_STATE_QOS,
        True,
    )
    assert any(
        "malformed" in message and COMMAND_TOPIC in message for message in _info_messages(caplog)
    )


# -------------------------------------------------------------- press untouched


async def test_a_press_still_presses_and_is_never_a_selection_command(
    harness: Harness,
) -> None:
    """The press half is unchanged, and a press payload on a selection topic is malformed.

    ``b"press"`` on a *button* topic must still reach the press service exactly
    once, while ``b"press"``/``b"ON"`` on a *selection* command topic must be
    refused as malformed — never routed to the press service, and never treated
    as a selection write.

    :param harness: the stack under test.
    """
    sound = await harness.store.create_sound(
        SoundIn(name="Ding dong", source_type="file", path="ding-dong.mp3")
    )
    await harness.store.upsert_button(
        ButtonIn(
            name="Front Door",
            action_type="sound",
            sound_id=sound.id,
            slug=PRESS_BUTTON_SLUG,
            enabled=True,
        )
    )
    await _connect(harness)
    await wait_until(lambda: PRESS_TOPIC in harness.bridge.subscriptions)

    harness.broker.publish_from_test(PRESS_TOPIC, b"PRESS")
    await wait_until(lambda: harness.press.calls != [])
    assert harness.press.calls == [(PRESS_BUTTON_SLUG, "mqtt")]

    harness.press.calls.clear()
    for payload in (b"press", b"ON"):
        mark = len(_published(harness, STATE_TOPIC))
        harness.broker.publish_from_test(COMMAND_TOPIC, payload)
        await wait_until(lambda mark=mark: len(_published(harness, STATE_TOPIC)) > mark)
    assert harness.press.calls == []
    assert harness.selection.is_selected(LOCK) is False


# ------------------------------------------------------------- retraction purge


async def test_a_renamed_tile_purges_its_switch_and_read_only_configs(harness: Harness) -> None:
    """A rename blanks the switch config AND the PRE-D34 read-only config with the right flags.

    A switch config blank is non-retained (Home Assistant's removal signal) while
    the read-only blank is retained (it also clears a broker copy a pre-D34 build
    left). Both must land on the old slug, or the ghost outlives the rename.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await wait_until(lambda: _published(harness, STATE_TOPIC) != [])
    tile = await _require_first_tile(harness.store)
    read_only_topic = selection_read_only_discovery_topic(_settings(), SLUG)
    switch_topic = "homeassistant/switch/chime_deck_default_front_door/config"

    assert selection_state_topic(_settings(), SLUG) == STATE_TOPIC
    assert read_only_topic == "homeassistant/binary_sensor/chime_deck_default_front_door/config"

    await harness.store.upsert_entity(
        EntityIn(id=tile.id, entity_id=LOCK, display_name="Front Door Lock")
    )

    assert _last_publish(harness, switch_topic) == (
        switch_topic,
        "",
        DISCOVERY_PUBLISH_QOS,
        False,
    )
    assert _last_publish(harness, read_only_topic) == (
        read_only_topic,
        "",
        DISCOVERY_PUBLISH_QOS,
        True,
    )
    assert harness.broker.retained[STATE_TOPIC] == b""
