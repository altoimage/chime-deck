"""Fully offline tests for the pre-rename entity purge (HSD-l84l).

The stack is real: a :class:`~chime_deck.store.store.ConfigStore` on a
temporary SQLite file, the real :class:`~chime_deck.selection.SelectionService`,
the real :class:`~chime_deck.mqtt.bridge.MqttBridge` wired to
:class:`~mqtt_fakes.FakeBroker`, and the real
:class:`~chime_deck.mqtt.discovery.MqttDiscovery` between them — wired through
the same seams :func:`~chime_deck.app.create_app` installs. Nothing here touches
a broker, a socket, the network or a real timer beyond the bounded
:func:`~mqtt_fakes.wait_until` poll and the injected
:class:`~mqtt_fakes.RecordingSleep`/:class:`~mqtt_fakes.FixedRandom`.

The pre-rename topics are spelled as **literals** here on purpose: the test pins
the grammar the old build published instead of re-deriving it with the code under
test, so a change to the current templates or to the translation table cannot
silently agree with itself. This module is one of the four places the old
literals are allowed to appear (see :mod:`chime_deck.mqtt.legacy`).

Owning ticket: HSD-l84l.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
import pytest_asyncio
from mqtt_fakes import FakeBroker, FakeClient, FixedRandom, RecordingSleep, wait_until

from chime_deck.mqtt import (
    DISCOVERY_PUBLISH_QOS,
    SELECTION_STATE_QOS,
    MqttBridge,
    MqttDiscovery,
    MqttSettings,
    legacy_topic,
)
from chime_deck.mqtt.legacy import legacy_identifier
from chime_deck.selection import SelectionService
from chime_deck.store import ButtonIn, ConfigStore, EntityIn, SoundIn

pytestmark = pytest.mark.asyncio

LOGGER_NAME: Final = "chime_deck.mqtt.discovery"
"""Logger the discovery service logs under, filtered on for the summary assertions."""

CONFIG_PREFIX: Final = "homeassistant"
"""Discovery prefix of the default test settings."""

INSTANCE: Final = "default"
"""Instance id of the default test settings."""

HOST: Final = "broker.test"
"""Broker host used by the fake settings object."""

FRONT_DOOR: Final = "front-door"
"""Slug of the enabled button."""

SIDE_DOOR: Final = "side-door"
"""Slug of the DISABLED button: its old entity is still a ghost worth purging."""

LOCK: Final = "lock.front_door"
"""Entity id of the tile the pre-HSD-jw0x rule names ``lock_front_door``."""

DRYER_NODERED: Final = "sensor.dryer_nodered_d90a5b74cba48067"
"""Entity id of the tile HSD-jw0x names "Dryer" — the ticket's headline example."""

LEGACY_BUTTON_CONFIG_TOPIC: Final = "homeassistant/button/super_doorbell_default_front-door/config"
"""Pre-rename discovery topic of the enabled button."""

LEGACY_SIDE_DOOR_CONFIG_TOPIC: Final = (
    "homeassistant/button/super_doorbell_default_side-door/config"
)
"""Pre-rename discovery topic of the now-disabled button."""

LEGACY_LOCK_CONFIG_TOPIC: Final = (
    "homeassistant/binary_sensor/super_doorbell_default_front_door/config"
)
"""Pre-rename discovery topic of "Front Door" under its display-name slug."""

LEGACY_DRYER_CONFIG_TOPIC: Final = "homeassistant/binary_sensor/super_doorbell_default_dryer/config"
"""Pre-rename discovery topic of "Dryer" under its display-name slug."""

LEGACY_LOCK_ENTITY_ID_CONFIG_TOPIC: Final = (
    "homeassistant/binary_sensor/super_doorbell_default_lock_front_door/config"
)
"""Pre-rename discovery topic of "Front Door" under its PRE-HSD-jw0x entity-id slug."""

LEGACY_DRYER_ENTITY_ID_CONFIG_TOPIC: Final = (
    "homeassistant/binary_sensor/"
    "super_doorbell_default_sensor_dryer_nodered_d90a5b74cba48067/config"
)
"""Pre-rename discovery topic of "Dryer" under its PRE-HSD-jw0x entity-id slug."""

LEGACY_LOCK_SWITCH_CONFIG_TOPIC: Final = (
    "homeassistant/switch/super_doorbell_default_front_door/config"
)
"""Pre-rename ``switch`` config of "Front Door" under its display-name slug.

A pre-D34 build never published this family, but the purge plans it anyway: the
shared translation cannot know which entity type the old build announced, so the
``switch`` half is the harmless no-op that makes the purge future-proof for a
later rename.
"""

LEGACY_DRYER_SWITCH_CONFIG_TOPIC: Final = "homeassistant/switch/super_doorbell_default_dryer/config"
"""Pre-rename ``switch`` config of "Dryer" under its display-name slug."""

LEGACY_LOCK_ENTITY_ID_SWITCH_CONFIG_TOPIC: Final = (
    "homeassistant/switch/super_doorbell_default_lock_front_door/config"
)
"""Pre-rename ``switch`` config of "Front Door" under its entity-id slug."""

LEGACY_DRYER_ENTITY_ID_SWITCH_CONFIG_TOPIC: Final = (
    "homeassistant/switch/super_doorbell_default_sensor_dryer_nodered_d90a5b74cba48067/config"
)
"""Pre-rename ``switch`` config of "Dryer" under its entity-id slug."""

LEGACY_LOCK_STATE_TOPIC: Final = "super_doorbell/default/selection/front_door"
"""Retained pre-rename state topic of "Front Door" under its display-name slug."""

LEGACY_DRYER_STATE_TOPIC: Final = "super_doorbell/default/selection/dryer"
"""Retained pre-rename state topic of "Dryer" under its display-name slug."""

LEGACY_LOCK_ENTITY_ID_STATE_TOPIC: Final = "super_doorbell/default/selection/lock_front_door"
"""Retained pre-rename state topic of "Front Door" under its entity-id slug."""

LEGACY_DRYER_ENTITY_ID_STATE_TOPIC: Final = (
    "super_doorbell/default/selection/sensor_dryer_nodered_d90a5b74cba48067"
)
"""Retained pre-rename state topic of "Dryer" under its entity-id slug."""

LEGACY_AVAILABILITY_TOPIC: Final = "super_doorbell/default/availability"
"""Retained pre-rename availability topic."""

LEGACY_COMMAND_TOPIC: Final = "super_doorbell/default/button/front-door/set"
"""Pre-rename command topic: never retained, so never blanked."""

SEEDED_CONFIG_TOPICS: Final[tuple[str, ...]] = (
    LEGACY_BUTTON_CONFIG_TOPIC,
    LEGACY_SIDE_DOOR_CONFIG_TOPIC,
    LEGACY_LOCK_CONFIG_TOPIC,
    LEGACY_DRYER_CONFIG_TOPIC,
    LEGACY_LOCK_ENTITY_ID_CONFIG_TOPIC,
)
"""The non-retained pre-rename discovery topics the fake broker is seeded with."""

SEEDED_STATE_TOPICS: Final[tuple[str, ...]] = (
    LEGACY_LOCK_STATE_TOPIC,
    LEGACY_DRYER_STATE_TOPIC,
    LEGACY_LOCK_ENTITY_ID_STATE_TOPIC,
    LEGACY_AVAILABILITY_TOPIC,
)
"""The retained pre-rename state/availability topics the fake broker is seeded with."""

LEGACY_BLANKED_TOPICS: Final[frozenset[str]] = frozenset(
    (
        *SEEDED_CONFIG_TOPICS,
        LEGACY_DRYER_ENTITY_ID_CONFIG_TOPIC,
        LEGACY_LOCK_SWITCH_CONFIG_TOPIC,
        LEGACY_DRYER_SWITCH_CONFIG_TOPIC,
        LEGACY_LOCK_ENTITY_ID_SWITCH_CONFIG_TOPIC,
        LEGACY_DRYER_ENTITY_ID_SWITCH_CONFIG_TOPIC,
        *SEEDED_STATE_TOPICS,
        LEGACY_DRYER_ENTITY_ID_STATE_TOPIC,
    )
)
"""Every topic one purge blanks: the seeded ghosts plus the harmless no-ops.

The extras are the pre-HSD-jw0x entity-id slugs of the tile with the
integration-specific entity id, which the pre-rename build published under
``..._sensor_dryer_nodered_d90a5b74cba48067``, and the pre-rename ``switch``
config family. They are not seeded because the purge cannot know whether the old
build published them; blanking them anyway is the harmless-no-op half of the
design. The ``switch`` family is a no-op for a pre-D34 build (which published a
``binary_sensor``) but is what a later rename will have to retract.
"""


class RecordingPress:
    """A press handler that records nothing: these tests never press a button."""

    async def press(self, slug: str, *, source: str = "mqtt") -> object:
        """Refuse nothing and play nothing.

        :param slug: the button slug that was pressed.
        :param source: press-source tag.
        """
        return None


@dataclass
class Harness:
    """The real stack under test, wired the way the application wires it."""

    bridge: MqttBridge
    discovery: MqttDiscovery
    broker: FakeBroker
    store: ConfigStore
    selection: SelectionService
    client_index: int = 0
    """Position of this bridge's client in ``broker.clients``, fixed at build time."""

    @property
    def client(self) -> FakeClient:
        """Return the client this harness's own bridge built."""
        return self.broker.clients[self.client_index]


def _settings() -> MqttSettings:
    """Build validated MQTT settings for the test service."""
    return MqttSettings(host=HOST, discovery_prefix=CONFIG_PREFIX, instance_id=INSTANCE)


def _build(
    *,
    store: ConfigStore,
    selection: SelectionService,
    broker: FakeBroker,
    server_host: str = "127.0.0.1",
    server_port: int = 8080,
) -> Harness:
    """Build the bridge and the discovery service wired to each other.

    :param store: connected store the service reads buttons and entities from.
    :param selection: real selection service the state publishes read.
    :param broker: the pre-seeded fake broker the bridge connects to.
    :param server_host: resolved bind host for the configuration URL.
    :param server_port: resolved HTTP port for the configuration URL.
    """
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
        press=RecordingPress(),
        bus=store.events,
        server_host=server_host,
        server_port=server_port,
        entities=store,
        selection=selection,
    )
    bridge.set_on_birth(discovery.on_birth)
    bridge.set_on_message(discovery.on_message)
    selection.set_on_change(
        lambda entity_id, selected: discovery.on_selection_changed(entity_id, selected)
    )
    return Harness(
        bridge=bridge,
        discovery=discovery,
        broker=broker,
        store=store,
        selection=selection,
        client_index=len(broker.clients),
    )


def _seed_broker(broker: FakeBroker) -> None:
    """Seed the fake broker with what the pre-rename build left behind.

    The config payloads are stored directly, because discovery is not retained and
    a pre-rename build never put them in the retained store — they are the ghost
    configs a broker restart cannot even resurrect. The state and availability
    values are published through :meth:`FakeBroker.publish_from_test`, so they
    really enter the retained store the way the old build's retained publishes did.

    :param broker: the fake broker to seed.
    """
    for topic in SEEDED_CONFIG_TOPICS:
        broker.retained[topic] = b'{"name": "old"}'
    broker.publish_from_test(LEGACY_LOCK_STATE_TOPIC, b"on")
    broker.publish_from_test(LEGACY_DRYER_STATE_TOPIC, b"off")
    broker.publish_from_test(LEGACY_LOCK_ENTITY_ID_STATE_TOPIC, b"on")
    broker.publish_from_test(LEGACY_AVAILABILITY_TOPIC, b"online")


@asynccontextmanager
async def _open_harness(db_path: Path) -> AsyncIterator[Harness]:
    """Open a temporary store with the two buttons and two tiles, pre-seeded broker.

    :param db_path: temporary database file path.
    """
    broker = FakeBroker()
    _seed_broker(broker)
    async with ConfigStore(db_path) as store:
        sound = await store.create_sound(
            SoundIn(name="Ding dong", source_type="file", path="ding-dong.mp3")
        )
        await store.upsert_button(
            ButtonIn(
                name="Front Door",
                action_type="sound",
                sound_id=sound.id,
                slug=FRONT_DOOR,
                enabled=True,
            )
        )
        await store.upsert_button(
            ButtonIn(
                name="Side Door",
                action_type="sound",
                sound_id=sound.id,
                slug=SIDE_DOOR,
                enabled=False,
            )
        )
        await store.upsert_entity(EntityIn(entity_id=LOCK, display_name="Front Door"))
        await store.upsert_entity(EntityIn(entity_id=DRYER_NODERED, display_name="Dryer"))
        yield _build(store=store, selection=SelectionService(), broker=broker)


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    """Yield the real stack under test, over a pre-seeded broker and fresh store.

    :param tmp_path: pytest temporary directory.
    """
    async with _open_harness(tmp_path / "doorbell.db") as built:
        yield built


async def _connect(harness: Harness) -> None:
    """Start both halves, accept the CONNACK and wait for the purge to finish.

    The wait is on the last pre-rename blank of the birth seam — the availability
    topic is blanked after every discovery config and every selection state — so
    an assertion can never race the purge it is about.

    :param harness: the stack under test.
    """
    await harness.bridge.start()
    await harness.discovery.start()
    harness.broker.connect(harness.client)
    await wait_until(lambda: _published(harness, LEGACY_AVAILABILITY_TOPIC) != [])


def _published(harness: Harness, topic: str) -> list[tuple[str, object, object, object]]:
    """Return every publish the client made to *topic*, in order.

    :param harness: the stack under test.
    :param topic: topic to filter on.
    """
    return [
        (published_topic, payload, qos, retain)
        for published_topic, payload, qos, retain in harness.client.published
        if published_topic == topic
    ]


def _blanked_since(harness: Harness, offset: int) -> list[tuple[str, object, object]]:
    """Return the empty payloads published after *offset*, as ``(topic, qos, retain)``.

    :param harness: the stack under test.
    :param offset: number of publishes already recorded before the window.
    """
    return [
        (topic, qos, retain)
        for topic, payload, qos, retain in harness.client.published[offset:]
        if payload == ""
    ]


async def test_a_fresh_process_blanks_every_pre_rename_topic(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """A redeploy with no bookkeeping blanks every pre-rename topic, retained or not.

    The broker holds what the old build published; this process has never seen any
    of it. Because every identifier this build constructs is translatable to its
    pre-rename form, the first connect leaves no ghost: each retained value is an
    empty payload and each non-retained config is blanked with the right retain
    flag. The one operator-facing line names the instance and the count, while the
    per-topic detail stays at DEBUG because the purge runs on every connect.

    :param harness: the stack under test.
    :param caplog: pytest log capture.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    await _connect(harness)

    for topic in SEEDED_CONFIG_TOPICS:
        assert _published(harness, topic)[-1] == (topic, "", DISCOVERY_PUBLISH_QOS, False)
    for topic in SEEDED_STATE_TOPICS:
        assert _published(harness, topic)[-1] == (topic, "", SELECTION_STATE_QOS, True)

    # Every topic the old build genuinely retained is empty now. The seeded
    # config entries are still there, and correctly so: a discovery blank is
    # published non-retained (that is the only removal signal Home Assistant has
    # for a config, design 02 §4), and a non-retained publish must not clear
    # retained broker state.
    for topic in SEEDED_STATE_TOPICS:
        assert harness.broker.retained[topic] == b""
    # The only non-empty retained values left are this build's own live topics
    # (plus the seeded config stand-ins, which no publish could have cleared).
    live_retained = {topic for topic, payload in harness.broker.retained.items() if payload}
    assert live_retained - set(SEEDED_CONFIG_TOPICS) == {
        "chime_deck/default/availability",
        "chime_deck/default/selection/dryer",
        "chime_deck/default/selection/front_door",
    }

    infos = [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno == logging.INFO
    ]
    assert any(
        "instance default" in message
        and "15 blanked" in message
        and "10 discovery configs" in message
        and "5 retained states" in message
        for message in infos
    ), infos
    debugs = [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno == logging.DEBUG
    ]
    for topic in LEGACY_BLANKED_TOPICS:
        assert any(topic in message for message in debugs), topic


async def test_the_purge_is_idempotent(harness: Harness) -> None:
    """Running the purge twice blanks the same set twice and raises nothing.

    A purge leaves nothing behind to remember, so there is no state a second run
    could miss or re-derive differently; the empty payloads are simply published
    again and the retained values stay empty.

    :param harness: the stack under test.
    """
    await _connect(harness)
    first_offset = len(harness.client.published)
    first_report = await harness.discovery.purge_legacy_entities()
    first = _blanked_since(harness, first_offset)
    second_offset = len(harness.client.published)
    second_report = await harness.discovery.purge_legacy_entities()
    second = _blanked_since(harness, second_offset)

    assert first == second
    assert {topic for topic, _, _ in first} == set(LEGACY_BLANKED_TOPICS)
    assert first_report.purged == second_report.purged == tuple(sorted(LEGACY_BLANKED_TOPICS))
    assert first_report.failed == second_report.failed == ()
    for topic in SEEDED_STATE_TOPICS:
        assert harness.broker.retained[topic] == b""


async def test_the_purge_leaves_the_new_topics_alone(harness: Harness) -> None:
    """The purge blanks exactly the pre-rename topics and no current one.

    This is the guard against an over-broad substitution: a translation that
    mangled a ``chime_deck`` identifier, or a blank published to the topic this
    build is live on, would show up here as an extra topic.

    :param harness: the stack under test.
    """
    await _connect(harness)
    offset = len(harness.client.published)
    await harness.discovery.purge_legacy_entities()
    blanked = {topic for topic, _, _ in _blanked_since(harness, offset)}

    assert blanked == set(LEGACY_BLANKED_TOPICS)
    assert not any(topic.startswith("chime_deck/") for topic in blanked)
    assert not any("/chime_deck_" in topic for topic in blanked)


async def test_the_translation_layer_is_exact() -> None:
    """The translation is the whole pre-rename grammar, and refuses a no-op.

    The unique-id-in-the-middle case is the one that matters most: a discovery
    topic carries the token inside a path level, not at the start, so only a
    whole-identifier substitution reconstructs it. A second application is a
    no-op, and a no-op is exactly what the layer refuses rather than returns.
    """
    assert (
        legacy_identifier("chime_deck/default/selection/front_door")
        == "super_doorbell/default/selection/front_door"
    )
    assert (
        legacy_identifier("homeassistant/binary_sensor/chime_deck_default_front_door/config")
        == "homeassistant/binary_sensor/super_doorbell_default_front_door/config"
    )
    assert legacy_identifier("chime-deck-default") == "super-doorbell-default"
    with pytest.raises(ValueError):
        legacy_identifier("homeassistant/status")
    with pytest.raises(ValueError):
        legacy_identifier("super_doorbell/default/availability")
    assert legacy_topic("chime_deck/default/availability") == "super_doorbell/default/availability"
    with pytest.raises(ValueError):
        legacy_topic(legacy_topic("chime_deck/default/availability"))


async def test_a_button_command_topic_is_not_blanked(harness: Harness) -> None:
    """Old command topics are left alone: they are never retained, so never stale.

    The documented decision, pinned: nothing is stored on a command topic for the
    old build to leave behind, so blanking one would only publish noise.

    :param harness: the stack under test.
    """
    await _connect(harness)

    assert _published(harness, LEGACY_COMMAND_TOPIC) == []
    assert LEGACY_COMMAND_TOPIC not in harness.broker.retained


async def test_a_disabled_buttons_old_entity_is_still_purged(harness: Harness) -> None:
    """A button that is disabled now still has the entity the old build announced.

    Disabling a button retracts its entity in this build, but the old build's
    entity is on the broker and stays a ghost unless the purge plans every button
    with a usable slug, enabled or not.

    :param harness: the stack under test.
    """
    await _connect(harness)
    buttons = {button.slug: button for button in await harness.store.list_buttons()}
    assert buttons[SIDE_DOOR].enabled is False

    assert _published(harness, LEGACY_SIDE_DOOR_CONFIG_TOPIC)[-1] == (
        LEGACY_SIDE_DOOR_CONFIG_TOPIC,
        "",
        DISCOVERY_PUBLISH_QOS,
        False,
    )
