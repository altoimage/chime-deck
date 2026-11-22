"""Fully offline tests for the MQTT selection sensors (design 02 §9, D29).

The stack is real: a :class:`~chime_deck.store.store.ConfigStore` on a
temporary SQLite file, the real
:class:`~chime_deck.selection.SelectionService`, the real
:class:`~chime_deck.mqtt.bridge.MqttBridge` wired to
:class:`~mqtt_fakes.FakeBroker`, and the real
:class:`~chime_deck.mqtt.discovery.MqttDiscovery` between them — wired through
the same ``set_on_change`` seam :func:`~chime_deck.app.create_app` installs.
Nothing here touches a broker, a socket, the network or a real timer beyond the
bounded :func:`~mqtt_fakes.wait_until` poll and the injected
:class:`~mqtt_fakes.RecordingSleep`/:class:`~mqtt_fakes.FixedRandom`.

The store emits its configuration event after the commit, inside the awaiting
call, so an ``upsert_entity``/``delete_entity`` reaches the discovery handler
synchronously: the lifecycle tests need no poll between the write and the
assertion.

Owning ticket: HSD-fro0.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

import pytest
import pytest_asyncio
from mqtt_fakes import FakeBroker, FakeClient, FixedRandom, RecordingSleep, wait_until

from chime_deck.mqtt import (
    DEFAULT_READ_TIMEOUT_S,
    DEVICE_KEY_ORDER,
    DISCOVERY_PUBLISH_QOS,
    SELECTION_KEY_ORDER,
    SELECTION_STATE_QOS,
    MqttBridge,
    MqttDiscovery,
    MqttSettings,
    assign_selection_slugs,
    build_discovery_payload,
    build_selection_payload,
    configuration_url,
    encode_payload,
    selection_default_entity_id,
    selection_discovery_topic,
    selection_entity_id_slug,
    selection_slug,
    selection_state_topic,
    selection_unique_id,
)
from chime_deck.mqtt.discovery import selection_read_only_discovery_topic
from chime_deck.selection import SelectionService
from chime_deck.store import Button, ConfigStore, Entity, EntityIn

pytestmark = pytest.mark.asyncio

GOLDEN_DIR: Final = Path(__file__).parent / "golden"
"""Directory holding the pretty-printed selection payload fixtures."""

GOLDEN_VERSION: Final = "9.9.9"
"""Fixed ``sw_version`` injected into the golden comparison."""

CONFIG_PREFIX: Final = "homeassistant"
"""Discovery prefix of the default test settings."""

INSTANCE: Final = "default"
"""Instance id of the default test settings."""

HOST: Final = "broker.test"
"""Broker host used by the fake settings object."""

AVAILABILITY: Final = "chime_deck/default/availability"
"""Retained availability topic of the default instance."""

LOCK: Final = "lock.front_door"
"""Entity id of the tile the selection tests toggle."""

LOCK_SLUG: Final = "front_door"
"""Selection slug derived from :data:`LOCK`'s display name, "Front Door" (HSD-jw0x)."""

LEGACY_LOCK_SLUG: Final = "lock_front_door"
"""The PRE-HSD-jw0x slug of :data:`LOCK`: what it is called on an upgraded broker."""

DRYER: Final = "sensor.dryer"
"""Entity id of the second, never-selected tile."""

DRYER_SLUG: Final = "sensor_dryer"
"""Selection slug of :data:`DRYER`, which has NO display name in the fixture.

Deliberately left at the entity-id fallback: this tile is the standing proof that a
nameless tile still gets its pre-HSD-jw0x slug.
"""

DRYER_NODERED: Final = "sensor.dryer_nodered_d90a5b74cba48067"
"""Entity id of the tile HSD-jw0x names "Dryer" — the ticket's headline example."""

CONFIG_TOPIC: Final = "homeassistant/switch/chime_deck_default_front_door/config"
"""Discovery topic of the first tile's writable selection switch (D34)."""

DRYER_CONFIG_TOPIC: Final = "homeassistant/switch/chime_deck_default_sensor_dryer/config"
"""Discovery topic of the second tile's selection switch."""

LEGACY_CONFIG_TOPIC: Final = "homeassistant/switch/chime_deck_default_lock_front_door/config"
"""Discovery topic the first tile had BEFORE HSD-jw0x, purged on upgrade."""

NAMED_DRYER_CONFIG_TOPIC: Final = "homeassistant/switch/chime_deck_default_dryer/config"
"""Discovery topic of a tile named "Dryer"."""

SECOND_DRYER_CONFIG_TOPIC: Final = "homeassistant/switch/chime_deck_default_dryer_2/config"
"""Discovery topic of the second of two tiles named "Dryer"."""

WASHER_CONFIG_TOPIC: Final = "homeassistant/switch/chime_deck_default_washing_machine/config"
"""Discovery topic of a tile named "Washing Machine"."""

RENAMED_CONFIG_TOPIC: Final = "homeassistant/switch/chime_deck_default_front_door_lock/config"
"""Discovery topic after "Front Door" is renamed "Front Door Lock"."""

READ_ONLY_CONFIG_TOPIC: Final = "homeassistant/binary_sensor/chime_deck_default_front_door/config"
"""PRE-D34 read-only config of the first tile, blanked before the switch is announced."""

READ_ONLY_DRYER_CONFIG_TOPIC: Final = (
    "homeassistant/binary_sensor/chime_deck_default_sensor_dryer/config"
)
"""PRE-D34 read-only config of the second tile."""

READ_ONLY_LEGACY_CONFIG_TOPIC: Final = (
    "homeassistant/binary_sensor/chime_deck_default_lock_front_door/config"
)
"""PRE-D34 read-only config the first tile had BEFORE HSD-jw0x."""

READ_ONLY_RENAMED_CONFIG_TOPIC: Final = (
    "homeassistant/binary_sensor/chime_deck_default_front_door_lock/config"
)
"""PRE-D34 read-only config after "Front Door" is renamed "Front Door Lock"."""

STATE_TOPIC: Final = "chime_deck/default/selection/front_door"
"""Retained state topic of the first tile's selection sensor."""

DRYER_STATE_TOPIC: Final = "chime_deck/default/selection/sensor_dryer"
"""Retained state topic of the second tile's selection sensor."""

LEGACY_STATE_TOPIC: Final = "chime_deck/default/selection/lock_front_door"
"""Retained state topic the first tile had BEFORE HSD-jw0x."""

RENAMED_STATE_TOPIC: Final = "chime_deck/default/selection/front_door_lock"
"""Retained state topic after "Front Door" is renamed "Front Door Lock"."""

NEVER_ANNOUNCED_STATE_TOPIC: Final = "chime_deck/default/selection/sensor_never_announced"
"""State topic a toggle of a never-announced tile falls back to."""


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


def _settings(
    *,
    discovery_prefix: str = CONFIG_PREFIX,
    instance_id: str = INSTANCE,
) -> MqttSettings:
    """Build validated MQTT settings for one test service.

    :param discovery_prefix: Home Assistant discovery prefix.
    :param instance_id: instance id used in the topics and the unique id.
    """
    return MqttSettings(host=HOST, discovery_prefix=discovery_prefix, instance_id=instance_id)


def _entity(
    *, entity_id: str = LOCK, display_name: str | None = "Front Door", id: int = 1
) -> Entity:
    """Build one immutable entity row without touching the database.

    :param entity_id: Home Assistant entity id of the tile.
    :param display_name: stored display name, or ``None`` for "not set".
    :param id: row id; the collision tests need several distinct tiles.
    """
    return Entity(
        id=id,
        entity_id=entity_id,
        display_name=display_name,
        icon=None,
        accent_color=None,
        show_attributes=None,
        state_tones=None,
        hide_when_unavailable=False,
        colour_by_state=True,
        sort_order=10,
        created_at="2026-01-01T00:00:00.000000Z",
    )


def _button() -> Button:
    """Build one immutable button row, only used for the shared-device comparison."""
    return Button(
        id=1,
        slug="front-door",
        name="Front Door Chime",
        icon="mdi:doorbell",
        action_type="sound",
        sound_id=None,
        tts_text=None,
        tts_engine=None,
        baked_sha=None,
        baked_format=None,
        baked_at=None,
        baked_error=None,
        volume_override=None,
        dedupe_seconds=None,
        show_on_dashboard=False,
        quiet_hours_exempt=False,
        enabled=True,
        sort_order=10,
        created_at="2026-01-01T00:00:00.000000Z",
        updated_at="2026-01-01T00:00:00.000000Z",
    )


def _selection_payload_for(
    host: str, port: int, *, sw_version: str = GOLDEN_VERSION
) -> dict[str, object]:
    """Build the golden tile's payload for one bind.

    :param host: resolved bind host.
    :param port: resolved HTTP port.
    :param sw_version: version string for the device block.
    """
    entity = _entity()
    return build_selection_payload(
        entity,
        slug=selection_slug(entity),
        settings=_settings(),
        availability_topic=AVAILABILITY,
        configuration_url=configuration_url(host, port),
        sw_version=sw_version,
    )


def _named_dryer_payload_for(
    host: str, port: int, *, sw_version: str = GOLDEN_VERSION
) -> dict[str, object]:
    """Build the named-"Dryer" payload for one bind (the HSD-jw0x golden).

    :param host: resolved bind host.
    :param port: resolved HTTP port.
    :param sw_version: version string for the device block.
    """
    dryer = _entity(entity_id=DRYER_NODERED, display_name="Dryer", id=2)
    return build_selection_payload(
        dryer,
        slug=selection_slug(dryer),
        settings=_settings(),
        availability_topic=AVAILABILITY,
        configuration_url=configuration_url(host, port),
        sw_version=sw_version,
    )


def _device(payload: Mapping[str, object]) -> Mapping[str, object]:
    """Return the ``device`` block of a discovery payload.

    :param payload: discovery payload.
    """
    return cast("Mapping[str, object]", payload["device"])


def _golden(name: str) -> tuple[str, object]:
    """Read one golden file and parse it with the version placeholder replaced.

    :param name: golden file name inside :data:`GOLDEN_DIR`.
    """
    text = (GOLDEN_DIR / name).read_text(encoding="utf-8")
    assert "@VERSION@" in text, "the golden file lost its version placeholder"
    return text, json.loads(text.replace("@VERSION@", GOLDEN_VERSION))


def _build(
    *,
    store: ConfigStore,
    selection: SelectionService,
    broker: FakeBroker | None = None,
    server_host: str = "127.0.0.1",
    server_port: int = 8080,
    read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
) -> Harness:
    """Build the bridge and the discovery service wired to each other.

    The ``set_on_change`` callback is the same one the application installs, so a
    toggle through the real :class:`SelectionService` reaches the service exactly
    as a tap on the panel would.

    :param store: connected store the service reads buttons and entities from.
    :param selection: real selection service the state publishes read.
    :param broker: broker to reuse, so a restart is observable on one broker.
    :param server_host: resolved bind host for the configuration URL.
    :param server_port: resolved HTTP port for the configuration URL.
    :param read_timeout_s: bound for one discovery store read.
    """
    broker = FakeBroker() if broker is None else broker
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
        read_timeout_s=read_timeout_s,
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


@asynccontextmanager
async def _open_harness(
    db_path: Path,
    *,
    selection: SelectionService | None = None,
    broker: FakeBroker | None = None,
    server_host: str = "127.0.0.1",
    server_port: int = 8080,
) -> AsyncIterator[Harness]:
    """Open a temporary store with two tiles and yield the wired harness.

    :param db_path: temporary database file path.
    :param selection: selection service to use; a fresh empty one when omitted.
    :param broker: broker to reuse, for the simulated-restart test.
    :param server_host: resolved bind host for the configuration URL.
    :param server_port: resolved HTTP port for the configuration URL.
    """
    async with ConfigStore(db_path) as store:
        await store.upsert_entity(EntityIn(entity_id=LOCK, display_name="Front Door"))
        await store.upsert_entity(EntityIn(entity_id=DRYER))
        yield _build(
            store=store,
            selection=SelectionService() if selection is None else selection,
            broker=broker,
            server_host=server_host,
            server_port=server_port,
        )


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    """Yield the real stack under test, with its own temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with _open_harness(tmp_path / "doorbell.db") as built:
        yield built


async def _connect(harness: Harness) -> None:
    """Start both halves and accept the broker's CONNACK, bounded.

    :param harness: the stack under test.
    """
    await harness.bridge.start()
    await harness.discovery.start()
    harness.broker.connect(harness.client)
    await wait_until(lambda: harness.bridge.connected)


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


def _last_payload(harness: Harness, topic: str) -> str:
    """Return the last payload published to *topic*.

    :param harness: the stack under test.
    :param topic: topic to read.
    """
    published = _published(harness, topic)
    assert published, f"nothing was published to {topic}"
    payload = published[-1][1]
    assert isinstance(payload, str)
    return payload


def _parsed(harness: Harness, topic: str) -> dict[str, object]:
    """Return the last payload published to *topic*, parsed as JSON.

    :param harness: the stack under test.
    :param topic: topic to read.
    """
    parsed = json.loads(_last_payload(harness, topic))
    assert isinstance(parsed, dict)
    return cast("dict[str, object]", parsed)


async def _require_first_tile(store: ConfigStore) -> Entity:
    """Return the stored ``lock.front_door`` tile, failing when it is missing.

    :param store: connected store.
    """
    for entity in await store.list_entities():
        if entity.entity_id == LOCK:
            return entity
    raise AssertionError(f"the store has no {LOCK} tile")


# ----------------------------------------------------------------- golden files


async def test_the_selection_payload_matches_the_golden_file() -> None:
    """The loopback payload is exactly the pinned JSON, in design 02 §9's key order."""
    text, golden = _golden("discovery_selection_loopback.json")
    payload = _selection_payload_for("127.0.0.1", 8080)
    assert payload == golden
    assert tuple(payload) == SELECTION_KEY_ORDER
    assert tuple(_device(payload)) == DEVICE_KEY_ORDER[:5]
    assert tuple(cast("Mapping[str, object]", json.loads(text))) == SELECTION_KEY_ORDER
    assert json.loads(encode_payload(payload)) == payload


async def test_the_lan_payload_matches_the_golden_file() -> None:
    """The LAN payload adds the configuration URL and nothing else."""
    text, golden = _golden("discovery_selection_lan.json")
    payload = _selection_payload_for("192.168.0.50", 8080)
    assert payload == golden
    assert tuple(payload) == SELECTION_KEY_ORDER
    assert tuple(_device(payload)) == DEVICE_KEY_ORDER
    assert _device(payload)["configuration_url"] == "http://192.168.0.50:8080/settings"
    assert tuple(cast("Mapping[str, object]", json.loads(text))) == SELECTION_KEY_ORDER
    assert json.loads(encode_payload(payload)) == payload


async def test_the_selection_payload_is_a_writable_switch(harness: Harness) -> None:
    """D34's entity is a writable ``switch``: it carries a command topic and both states.

    The inverted premise of the pre-D34 test: the payload used to be a read-only
    ``binary_sensor`` with no command topic. Now Home Assistant can write the
    value, so ``command_topic``, ``state_on``/``state_off`` and
    ``payload_on``/``payload_off`` must all be present, the preferred entity id
    must be a ``switch``, and the fields §9 deliberately omits (a device class,
    ``qos``, ``retain``, an entity category) must stay absent.

    :param harness: the stack under test.
    """
    payload = _selection_payload_for("127.0.0.1", 8080)
    assert payload["command_topic"] == "chime_deck/default/selection/front_door/set"
    assert payload["state_on"] == "on"
    assert payload["state_off"] == "off"
    assert payload["payload_on"] == "on"
    assert payload["payload_off"] == "off"
    assert payload["default_entity_id"] == "switch.chime_deck_front_door"
    for absent in ("device_class", "qos", "retain", "entity_category"):
        assert absent not in payload
    assert tuple(payload) == SELECTION_KEY_ORDER

    await _connect(harness)
    published = _parsed(harness, CONFIG_TOPIC)
    assert published["command_topic"] == "chime_deck/default/selection/front_door/set"
    assert published["state_on"] == "on"
    assert published["state_off"] == "off"
    assert published["payload_on"] == "on"
    assert published["payload_off"] == "off"
    assert published["default_entity_id"] == "switch.chime_deck_front_door"
    for absent in ("device_class", "qos", "retain", "entity_category"):
        assert absent not in published
    assert tuple(published) == SELECTION_KEY_ORDER


async def test_the_device_block_and_availability_match_the_buttons() -> None:
    """Both entity types share one device block and one availability source."""
    button_payload = build_discovery_payload(
        _button(),
        settings=_settings(),
        availability_topic=AVAILABILITY,
        sw_version=GOLDEN_VERSION,
    )
    selection_payload = _selection_payload_for("127.0.0.1", 8080)
    assert _device(selection_payload) == _device(button_payload)
    assert selection_payload["availability_topic"] == button_payload["availability_topic"]
    assert selection_payload["payload_available"] == button_payload["payload_available"]
    assert selection_payload["payload_not_available"] == button_payload["payload_not_available"]


async def test_the_dryer_payload_matches_the_golden_file() -> None:
    """The ticket's headline tile is pinned: ``sensor.dryer_nodered_…`` named "Dryer".

    HSD-jw0x's acceptance example: a tile whose Home Assistant entity id is an ugly
    integration-specific id is announced as plain ``dryer``.
    """
    text, golden = _golden("discovery_selection_dryer.json")
    payload = _named_dryer_payload_for("127.0.0.1", 8080)
    assert payload == golden
    assert tuple(payload) == SELECTION_KEY_ORDER
    assert tuple(cast("Mapping[str, object]", json.loads(text))) == SELECTION_KEY_ORDER
    assert payload["name"] == "Dryer selected"
    assert payload["default_entity_id"] == "switch.chime_deck_dryer"
    assert payload["state_topic"] == "chime_deck/default/selection/dryer"
    assert json.loads(encode_payload(payload)) == payload


async def test_the_entity_slug_is_a_valid_home_assistant_object_id() -> None:
    """The slug agrees with the entity id Home Assistant actually registers.

    The slug is derived from the tile's *display name* (HSD-jw0x) and mapped into
    Home Assistant's object-id grammar, where a dash is forbidden: a dash slug
    would make the published ``default_entity_id`` disagree with the entity id Home
    Assistant registers, because it silently slugifies an invalid one.
    """
    slug = selection_slug(_entity())
    assert slug == LOCK_SLUG == "front_door"
    assert "-" not in slug
    assert selection_unique_id(_settings(), slug) == "chime_deck_default_front_door_selected"
    assert selection_state_topic(_settings(), slug) == STATE_TOPIC
    assert selection_default_entity_id(slug) == "switch.chime_deck_front_door"
    assert selection_discovery_topic(_settings(), slug) == CONFIG_TOPIC
    assert "-" not in selection_default_entity_id(slug).split(".", 1)[1]

    # A tile with no display name keeps the pre-HSD-jw0x entity-id slug.
    assert selection_slug(_entity(entity_id=DRYER, display_name=None)) == DRYER_SLUG
    # A display name with no usable character cannot be derived at all, so the
    # entity-id slug is the fallback, never an empty slug.
    assert selection_slug(_entity(display_name="!!!")) == LEGACY_LOCK_SLUG
    assert selection_entity_id_slug(LOCK) == LEGACY_LOCK_SLUG


# -------------------------------------------------------------------- lifecycle


async def test_start_publishes_the_config_unretained_and_the_state_retained(
    harness: Harness,
) -> None:
    """The config is non-retained at discovery QoS; the state is retained at its own.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await wait_until(
        lambda: (
            _published(harness, STATE_TOPIC) != [] and _published(harness, DRYER_STATE_TOPIC) != []
        )
    )

    config = _published(harness, CONFIG_TOPIC)
    assert config, f"nothing was published to {CONFIG_TOPIC}"
    assert config[-1][2] == DISCOVERY_PUBLISH_QOS
    assert config[-1][3] is False

    states = _published(harness, STATE_TOPIC)
    assert states[-1] == (STATE_TOPIC, "off", SELECTION_STATE_QOS, True)
    assert _published(harness, DRYER_STATE_TOPIC)[-1] == (
        DRYER_STATE_TOPIC,
        "off",
        SELECTION_STATE_QOS,
        True,
    )


async def test_start_blanks_the_read_only_config_with_a_retained_empty_payload(
    harness: Harness,
) -> None:
    """The pre-D34 ``binary_sensor`` config is removed with a RETAINED empty payload.

    D34 replaced the read-only entity with a writable switch, and the old config a
    pre-D34 build left on the broker must not survive beside it. A non-retained
    blank removes only the live Home Assistant entity; the retained form is what
    also clears the broker's stored copy, so every blank takes the retained flag
    at the discovery QoS.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await wait_until(lambda: _published(harness, STATE_TOPIC) != [])

    blanked = _published(harness, READ_ONLY_CONFIG_TOPIC)
    assert blanked, f"nothing was published to {READ_ONLY_CONFIG_TOPIC}"
    assert all(
        entry == (READ_ONLY_CONFIG_TOPIC, "", DISCOVERY_PUBLISH_QOS, True) for entry in blanked
    )


async def test_the_read_only_blank_precedes_the_switch_config(harness: Harness) -> None:
    """The dead read-only entity is blanked BEFORE its switch is announced.

    Home Assistant must never be able to register a read-only entity under the
    slug after the writable one has appeared: an upgrade that published in the
    other order could leave the dead entity beside the switch until a restart.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await wait_until(lambda: _published(harness, CONFIG_TOPIC) != [])

    published = harness.client.published
    read_only_index = published.index((READ_ONLY_CONFIG_TOPIC, "", DISCOVERY_PUBLISH_QOS, True))
    config_index = next(index for index, entry in enumerate(published) if entry[0] == CONFIG_TOPIC)
    assert read_only_index < config_index


async def test_retract_all_blanks_the_read_only_config_too(harness: Harness) -> None:
    """The settings handoff leaves no read-only ghost behind either.

    ``retract_all`` runs before the old bridge stops, so its blanks are the last
    chance to remove every identifier of a settings generation. A switch config
    blank alone would leave a pre-D34 ``binary_sensor`` registered forever, so the
    read-only config is blanked here with the same retained empty payload.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await wait_until(lambda: _published(harness, STATE_TOPIC) != [])

    await harness.discovery.retract_all()

    assert _published(harness, READ_ONLY_CONFIG_TOPIC)[-1] == (
        READ_ONLY_CONFIG_TOPIC,
        "",
        DISCOVERY_PUBLISH_QOS,
        True,
    )


async def test_a_home_assistant_birth_republishes_the_config_and_the_state(
    harness: Harness,
) -> None:
    """A ``homeassistant/status == online`` birth republishes config AND state.

    This is the third config lifecycle trigger beside add and edit, and it is the
    same ``on_birth`` seam as the CONNACK: the bridge consumes
    ``homeassistant/status`` itself. The birth jitter is
    :class:`RecordingSleep`, so no real timer is involved.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await wait_until(lambda: _published(harness, STATE_TOPIC) != [])
    assert harness.selection.toggle(LOCK) is True
    assert harness.broker.retained[STATE_TOPIC] == b"on"

    mark = len(harness.client.published)
    harness.broker.publish_from_test("homeassistant/status", b"online")

    def _config_after_birth() -> list[tuple[str, object, object, object]]:
        return [
            entry
            for entry in harness.client.published[mark:]
            if entry[0] == CONFIG_TOPIC and entry[3] is False
        ]

    def _republished_after_birth() -> bool:
        after = harness.client.published[mark:]
        state_on = (STATE_TOPIC, "on", SELECTION_STATE_QOS, True) in after
        return _config_after_birth() != [] and state_on

    await wait_until(_republished_after_birth)

    after = harness.client.published[mark:]
    birth_config = _config_after_birth()
    assert birth_config, f"the birth republished no config to {CONFIG_TOPIC}"
    parsed = json.loads(cast("str", birth_config[-1][1]))
    assert isinstance(parsed, dict)
    assert tuple(parsed) == SELECTION_KEY_ORDER
    assert (STATE_TOPIC, "on", SELECTION_STATE_QOS, True) in after
    assert harness.broker.retained[STATE_TOPIC] == b"on"


async def test_toggling_publishes_the_state_retained(harness: Harness) -> None:
    """One toggle publishes the retained state only, never the config again.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await wait_until(lambda: _published(harness, STATE_TOPIC) != [])
    config_before = len(_published(harness, CONFIG_TOPIC))

    assert harness.selection.toggle(LOCK) is True
    assert _published(harness, STATE_TOPIC)[-1] == (STATE_TOPIC, "on", SELECTION_STATE_QOS, True)
    assert harness.broker.retained[STATE_TOPIC] == b"on"
    assert len(_published(harness, CONFIG_TOPIC)) == config_before

    assert harness.selection.toggle(LOCK) is False
    assert _published(harness, STATE_TOPIC)[-1] == (STATE_TOPIC, "off", SELECTION_STATE_QOS, True)
    assert harness.broker.retained[STATE_TOPIC] == b"off"
    assert len(_published(harness, CONFIG_TOPIC)) == config_before


async def test_a_restart_republishes_every_state_as_off(tmp_path: Path) -> None:
    """A restart starts with nothing selected and overwrites the retained ``on``.

    The second generation has a FRESH selection service, exactly as the lifespan's
    ``clear()`` leaves it, and shares the broker so the retained value is visible.

    :param tmp_path: pytest temporary directory.
    """
    async with _open_harness(tmp_path / "restart.db") as first:
        await _connect(first)
        await wait_until(lambda: _published(first, STATE_TOPIC) != [])
        assert first.selection.toggle(LOCK) is True
        assert first.broker.retained[STATE_TOPIC] == b"on"

        # Stop the first generation before building the second: the store bus must
        # not be double-subscribed while both are alive, and the broker's retained
        # dict is the fake's own, so it survives the handoff.
        await first.discovery.stop()
        await first.bridge.stop()

        second = _build(store=first.store, selection=SelectionService(), broker=first.broker)
        await _connect(second)
        await wait_until(
            lambda: (
                _published(second, STATE_TOPIC) != []
                and _published(second, DRYER_STATE_TOPIC) != []
            )
        )

        states = _published(second, STATE_TOPIC)
        assert states[-1] == (STATE_TOPIC, "off", SELECTION_STATE_QOS, True)
        assert second.broker.retained[STATE_TOPIC] == b"off"
        assert _published(second, DRYER_STATE_TOPIC)[-1] == (
            DRYER_STATE_TOPIC,
            "off",
            SELECTION_STATE_QOS,
            True,
        )


async def test_a_reconnect_republishes_the_selected_state_as_on(harness: Harness) -> None:
    """A reconnect keeps the selection and republishes every state, not just ``off``.

    This is the seam the bridge runs on every CONNACK, reconnects included: a
    toggle-only publisher would leave the broker's retained value stale.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await wait_until(lambda: _published(harness, STATE_TOPIC) != [])
    assert harness.selection.toggle(LOCK) is True
    assert harness.broker.retained[STATE_TOPIC] == b"on"

    mark = len(harness.client.published)
    harness.broker.drop(harness.client)
    await wait_until(lambda: harness.bridge.connected is False)
    harness.broker.connect(harness.client)

    def _republished_after_mark() -> bool:
        after = harness.client.published[mark:]
        return (STATE_TOPIC, "on", SELECTION_STATE_QOS, True) in after and (
            DRYER_STATE_TOPIC,
            "off",
            SELECTION_STATE_QOS,
            True,
        ) in after

    # Wait on the property the test asserts: the CONNACK republishes availability
    # before the awaited store read that leads to the selection republish, so
    # waiting for "something was published" would race two aiosqlite round trips.
    await wait_until(_republished_after_mark)
    after = harness.client.published[mark:]
    assert (STATE_TOPIC, "on", SELECTION_STATE_QOS, True) in after
    assert (DRYER_STATE_TOPIC, "off", SELECTION_STATE_QOS, True) in after
    assert harness.broker.retained[STATE_TOPIC] == b"on"


async def test_deleting_a_tile_publishes_an_empty_payload(harness: Harness) -> None:
    """A deleted tile loses its entity and its retained state value.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await wait_until(lambda: _published(harness, STATE_TOPIC) != [])
    tile = await _require_first_tile(harness.store)
    assert harness.selection.toggle(LOCK) is True
    assert harness.broker.retained[STATE_TOPIC] == b"on"

    await harness.store.delete_entity(tile.id)

    assert _published(harness, CONFIG_TOPIC)[-1] == (CONFIG_TOPIC, "", DISCOVERY_PUBLISH_QOS, False)
    assert _published(harness, READ_ONLY_CONFIG_TOPIC)[-1] == (
        READ_ONLY_CONFIG_TOPIC,
        "",
        DISCOVERY_PUBLISH_QOS,
        True,
    )
    assert _published(harness, STATE_TOPIC)[-1] == (STATE_TOPIC, "", SELECTION_STATE_QOS, True)
    assert harness.broker.retained[STATE_TOPIC] == b""
    # Only the deleted slug is retracted: the other tile keeps its entity and state.
    assert all(payload != "" for _, payload, _, _ in _published(harness, DRYER_CONFIG_TOPIC))
    assert harness.broker.retained[DRYER_STATE_TOPIC] == b"off"


async def test_the_purge_lands_on_the_topics_the_display_name_derives(
    harness: Harness,
) -> None:
    """The retraction lands on the topics the tile's display name derives.

    The neighbouring purge test pins the two halves of D29 with hand-written
    topic constants; this one builds them the way the application does — the
    display name "Front Door" through :func:`~chime_deck.store.derive_slug`
    (``front-door``) and then into Home Assistant's object-id grammar
    (``front_door``) — so a change to the derivation cannot silently move the
    retraction off the topics the sensor was announced on.

    :param harness: the stack under test.
    """
    tile = await _require_first_tile(harness.store)
    slug = selection_slug(tile)
    config_topic = selection_discovery_topic(_settings(), slug)
    state_topic = selection_state_topic(_settings(), slug)

    read_only_config_topic = selection_read_only_discovery_topic(_settings(), slug)
    assert tile.display_name == "Front Door"
    assert slug == "front_door"
    assert config_topic == f"homeassistant/switch/chime_deck_default_{slug}/config"
    assert read_only_config_topic == f"homeassistant/binary_sensor/chime_deck_default_{slug}/config"
    assert state_topic == f"chime_deck/default/selection/{slug}"

    await _connect(harness)
    await wait_until(lambda: _published(harness, config_topic) != [])

    assert harness.selection.toggle(LOCK) is True
    assert harness.broker.retained[state_topic] == b"on"

    await harness.store.delete_entity(tile.id)

    assert _published(harness, config_topic)[-1] == (config_topic, "", DISCOVERY_PUBLISH_QOS, False)
    assert _published(harness, read_only_config_topic)[-1] == (
        read_only_config_topic,
        "",
        DISCOVERY_PUBLISH_QOS,
        True,
    )
    assert _published(harness, state_topic)[-1] == (state_topic, "", SELECTION_STATE_QOS, True)
    assert harness.broker.retained[state_topic] == b""


async def test_retract_all_removes_every_selection_switch(harness: Harness) -> None:
    """The settings handoff retracts both switches and their read-only ghosts.

    ``retract_all`` is the ``_teardown_mqtt(retract=True)`` path: it must purge the
    selection switches as well as the buttons, blanking the PRE-D34 read-only
    config a stale Home Assistant might still hold, and the cleared bookkeeping
    must keep a repeated call from publishing anything.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await wait_until(
        lambda: (
            _published(harness, STATE_TOPIC) != [] and _published(harness, DRYER_STATE_TOPIC) != []
        )
    )

    await harness.discovery.retract_all()

    for config_topic, read_only_topic, state_topic in (
        (CONFIG_TOPIC, READ_ONLY_CONFIG_TOPIC, STATE_TOPIC),
        (DRYER_CONFIG_TOPIC, READ_ONLY_DRYER_CONFIG_TOPIC, DRYER_STATE_TOPIC),
    ):
        assert _published(harness, config_topic)[-1] == (
            config_topic,
            "",
            DISCOVERY_PUBLISH_QOS,
            False,
        )
        assert _published(harness, read_only_topic)[-1] == (
            read_only_topic,
            "",
            DISCOVERY_PUBLISH_QOS,
            True,
        )
        assert _published(harness, state_topic)[-1] == (
            state_topic,
            "",
            SELECTION_STATE_QOS,
            True,
        )
        assert harness.broker.retained[state_topic] == b""

    count = len(harness.client.published)
    await harness.discovery.retract_all()
    assert len(harness.client.published) == count


async def test_editing_a_tile_republishes_its_config(harness: Harness) -> None:
    """A rename moves the entity to its new slug and blanks the old topics.

    HSD-jw0x's acceptance criterion: renaming "Front Door" to "Front Door Lock"
    re-announces the sensor at ``front_door_lock`` and publishes the empty payload
    to the OLD ``front_door`` discovery topic and an empty retained value to the OLD
    ``front_door`` state topic, so Home Assistant keeps no ghost.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await wait_until(lambda: _published(harness, CONFIG_TOPIC) != [])
    tile = await _require_first_tile(harness.store)
    dryer_config_before = len(_published(harness, DRYER_CONFIG_TOPIC))

    await harness.store.upsert_entity(
        EntityIn(id=tile.id, entity_id=LOCK, display_name="Front Door Lock")
    )
    assert _parsed(harness, RENAMED_CONFIG_TOPIC)["name"] == "Front Door Lock selected"
    assert _published(harness, CONFIG_TOPIC)[-1] == (
        CONFIG_TOPIC,
        "",
        DISCOVERY_PUBLISH_QOS,
        False,
    )
    assert _published(harness, READ_ONLY_RENAMED_CONFIG_TOPIC)[-1] == (
        READ_ONLY_RENAMED_CONFIG_TOPIC,
        "",
        DISCOVERY_PUBLISH_QOS,
        True,
    )
    assert _published(harness, READ_ONLY_CONFIG_TOPIC)[-1] == (
        READ_ONLY_CONFIG_TOPIC,
        "",
        DISCOVERY_PUBLISH_QOS,
        True,
    )
    assert _published(harness, STATE_TOPIC)[-1] == (STATE_TOPIC, "", SELECTION_STATE_QOS, True)
    assert harness.broker.retained[STATE_TOPIC] == b""
    # The resync is a full listing, so the untouched tile is re-announced too;
    # that is idempotent and it is what makes a deletion retractable at all.
    assert len(_published(harness, DRYER_CONFIG_TOPIC)) == dryer_config_before + 1

    await harness.store.upsert_entity(EntityIn(entity_id="light.kitchen"))
    kitchen_topic = "homeassistant/switch/chime_deck_default_light_kitchen/config"
    kitchen_state = "chime_deck/default/selection/light_kitchen"
    assert _parsed(harness, kitchen_topic)["name"] == "light.kitchen selected"
    assert _published(harness, kitchen_state)[-1] == (
        kitchen_state,
        "off",
        SELECTION_STATE_QOS,
        True,
    )


async def test_a_tile_named_dryer_is_announced_under_its_display_name(tmp_path: Path) -> None:
    """The ticket's headline example, through the whole real stack.

    A tile whose entity id is an integration-specific
    ``sensor.dryer_nodered_d90a5b74cba48067`` is announced as ``dryer`` because that
    is what the owner called it, and "Washing Machine" becomes ``washing_machine``.
    The seam this pins is the naming source: display name, not entity id.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "named.db") as store:
        await store.upsert_entity(EntityIn(entity_id=DRYER_NODERED, display_name="Dryer"))
        await store.upsert_entity(
            EntityIn(entity_id="sensor.washer_0a1b", display_name="Washing Machine")
        )
        harness = _build(store=store, selection=SelectionService())
        await _connect(harness)
        await wait_until(
            lambda: (
                _published(harness, NAMED_DRYER_CONFIG_TOPIC) != []
                and _published(harness, WASHER_CONFIG_TOPIC) != []
            )
        )

        dryer = _parsed(harness, NAMED_DRYER_CONFIG_TOPIC)
        assert dryer["name"] == "Dryer selected"
        assert dryer["default_entity_id"] == "switch.chime_deck_dryer"
        assert dryer["state_topic"] == "chime_deck/default/selection/dryer"

        washer = _parsed(harness, WASHER_CONFIG_TOPIC)
        assert washer["name"] == "Washing Machine selected"
        assert washer["default_entity_id"] == "switch.chime_deck_washing_machine"
        assert washer["state_topic"] == "chime_deck/default/selection/washing_machine"


async def test_a_redeploy_purges_the_pre_upgrade_topics(tmp_path: Path) -> None:
    """A restarted process with no bookkeeping still blanks the old entity-id topics.

    This is the redeploy case: the broker is pre-seeded as if the previous version
    had published ``..._lock_front_door/config`` and a retained ``on`` on
    ``.../selection/lock_front_door``, and this process is never told that slug. The
    old slug is still computable from the entity id, so the first republish purges
    both topics — otherwise the ghost discovery entity and its retained ``on`` would
    outlive the upgrade forever.

    :param tmp_path: pytest temporary directory.
    """
    broker = FakeBroker()
    broker.retained[LEGACY_CONFIG_TOPIC] = b'{"name": "Front Door selected"}'
    broker.publish_from_test(LEGACY_STATE_TOPIC, b"on")
    assert broker.retained[LEGACY_STATE_TOPIC] == b"on"

    async with ConfigStore(tmp_path / "redeploy.db") as store:
        await store.upsert_entity(EntityIn(entity_id=LOCK, display_name="Front Door"))
        harness = _build(store=store, selection=SelectionService(), broker=broker)
        assert harness.discovery.published == {}
        await _connect(harness)
        await wait_until(lambda: _published(harness, LEGACY_STATE_TOPIC) != [])

        assert _published(harness, LEGACY_CONFIG_TOPIC)[-1] == (
            LEGACY_CONFIG_TOPIC,
            "",
            DISCOVERY_PUBLISH_QOS,
            False,
        )
        assert _published(harness, READ_ONLY_LEGACY_CONFIG_TOPIC)[-1] == (
            READ_ONLY_LEGACY_CONFIG_TOPIC,
            "",
            DISCOVERY_PUBLISH_QOS,
            True,
        )
        assert _published(harness, LEGACY_STATE_TOPIC)[-1] == (
            LEGACY_STATE_TOPIC,
            "",
            SELECTION_STATE_QOS,
            True,
        )
        assert broker.retained[LEGACY_STATE_TOPIC] == b""
        # The tile itself is announced under the new name in the same pass.
        assert _parsed(harness, CONFIG_TOPIC)["default_entity_id"] == (
            "switch.chime_deck_front_door"
        )


async def test_colliding_display_names_get_distinct_deterministic_slugs(tmp_path: Path) -> None:
    """Two tiles named "Dryer" are announced as ``dryer`` and ``dryer_2``.

    End to end, through the real store and broker: both tiles get an entity, neither
    is silently skipped (the old entity-id rule could not collide at all, and the
    old collision handling announced the first and dropped the second), and the
    mapping is identical when the same listing is assigned twice and when it is
    reversed, because it depends only on the set of ``(entity_id, base_slug)`` pairs.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "collision.db") as store:
        await store.upsert_entity(EntityIn(entity_id="sensor.dryer_b", display_name="Dryer"))
        await store.upsert_entity(EntityIn(entity_id="sensor.dryer_a", display_name="Dryer"))
        harness = _build(store=store, selection=SelectionService())
        await _connect(harness)
        await wait_until(
            lambda: (
                _published(harness, NAMED_DRYER_CONFIG_TOPIC) != []
                and _published(harness, SECOND_DRYER_CONFIG_TOPIC) != []
            )
        )

        first = _parsed(harness, NAMED_DRYER_CONFIG_TOPIC)
        second = _parsed(harness, SECOND_DRYER_CONFIG_TOPIC)
        assert first["default_entity_id"] == "switch.chime_deck_dryer"
        assert second["default_entity_id"] == "switch.chime_deck_dryer_2"
        assert first["unique_id"] != second["unique_id"]
        assert first["state_topic"] == "chime_deck/default/selection/dryer"
        assert second["state_topic"] == "chime_deck/default/selection/dryer_2"

        listed = await store.list_entities()
        mapping = assign_selection_slugs(listed)
        assert mapping == assign_selection_slugs(listed)
        assert assign_selection_slugs(list(reversed(listed))) == mapping
        assert set(mapping) == {"dryer", "dryer_2"}
        assert mapping["dryer"].entity_id == "sensor.dryer_a"
        assert mapping["dryer_2"].entity_id == "sensor.dryer_b"


async def test_a_naturally_named_suffix_keeps_its_bare_slug() -> None:
    """A naturally named suffix keeps its bare slug, pushing the duplicate past it.

    Tiles "Dryer", "Dryer" and "Dryer 2" map to ``dryer``, ``dryer_3`` and
    ``dryer_2``: the naturally named tile claims ``dryer_2`` in the first pass, so
    the duplicate is pushed to ``dryer_3`` rather than stealing it.
    """
    mapping = assign_selection_slugs(
        [
            _entity(entity_id="sensor.dryer_b", display_name="Dryer", id=2),
            _entity(entity_id="sensor.dryer_2", display_name="Dryer 2", id=3),
            _entity(entity_id="sensor.dryer_a", display_name="Dryer", id=4),
        ]
    )
    assert {slug: entity.entity_id for slug, entity in mapping.items()} == {
        "dryer": "sensor.dryer_a",
        "dryer_2": "sensor.dryer_2",
        "dryer_3": "sensor.dryer_b",
    }


async def test_a_toggle_after_a_rename_publishes_to_the_new_state_topic(
    harness: Harness,
) -> None:
    """The reverse map is dropped by value, so a post-rename toggle is not stranded.

    The rename publishes the new slug *before* the old one is pruned, so the entity's
    mapping already points at ``front_door_lock`` when ``front_door`` is retracted.
    Dropping the reverse entry by entity id instead of by value would delete that
    mapping and send this toggle to the dead ``front_door`` topic.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await wait_until(lambda: _published(harness, STATE_TOPIC) != [])
    tile = await _require_first_tile(harness.store)

    await harness.store.upsert_entity(
        EntityIn(id=tile.id, entity_id=LOCK, display_name="Front Door Lock")
    )
    await wait_until(lambda: _published(harness, RENAMED_STATE_TOPIC) != [])
    assert harness.broker.retained[STATE_TOPIC] == b""
    marked = len(_published(harness, STATE_TOPIC))

    assert harness.selection.toggle(LOCK) is True

    assert _published(harness, RENAMED_STATE_TOPIC)[-1] == (
        RENAMED_STATE_TOPIC,
        "on",
        SELECTION_STATE_QOS,
        True,
    )
    assert harness.broker.retained[RENAMED_STATE_TOPIC] == b"on"
    assert len(_published(harness, STATE_TOPIC)) == marked
    assert harness.broker.retained[STATE_TOPIC] == b""


async def test_a_toggle_for_a_never_announced_tile_falls_back_to_its_entity_id(
    harness: Harness,
) -> None:
    """A toggle that outruns the first republish uses the entity-id slug deliberately.

    That is the topic an entity announced under the pre-HSD-jw0x rule is still
    listening on, so the operator's visible entity updates instead of nothing.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await wait_until(lambda: _published(harness, STATE_TOPIC) != [])

    assert harness.selection.toggle("sensor.never_announced") is True

    assert _published(harness, NEVER_ANNOUNCED_STATE_TOPIC)[-1] == (
        NEVER_ANNOUNCED_STATE_TOPIC,
        "on",
        SELECTION_STATE_QOS,
        True,
    )


@pytest.mark.parametrize("display_name", [None, "", "   ", "!!!"])
async def test_an_unusable_display_name_never_yields_an_empty_slug(
    display_name: str | None,
) -> None:
    """Absent, blank and punctuation-only names all fall back to the entity id.

    An empty slug would be an invalid topic and an invalid Home Assistant object id,
    so the fallback chain must never end in one.

    :param display_name: the stored display name under test.
    """
    slug = selection_slug(_entity(display_name=display_name))
    assert slug == LEGACY_LOCK_SLUG
    assert slug != ""
