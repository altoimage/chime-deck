"""Fully offline tests for the MQTT discovery service.

The stack is real: a :class:`~chime_deck.store.store.ConfigStore` on a
temporary SQLite file, the real :class:`~chime_deck.mqtt.bridge.MqttBridge`
wired to :class:`~mqtt_fakes.FakeBroker`, and the real
:class:`~chime_deck.mqtt.discovery.MqttDiscovery` between them. Nothing here
touches a broker, a socket, the network or a real timer beyond the bounded
:func:`~mqtt_fakes.wait_until` poll and the deliberately short read bound of the
stalled-read test.

The store's :meth:`~chime_deck.store.database.Database._publish` runs *after*
the commit, so a button event reaches the discovery handler synchronously inside
``await store.upsert_button(...)``; the lifecycle tests therefore need no poll
between the write and the assertion.

Owning ticket: HSD-c3si.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

import pytest
import pytest_asyncio
from mqtt_fakes import (
    FakeBroker,
    FakeClient,
    FixedRandom,
    RecordingSleep,
    count,
    wait_until,
)

import chime_deck
from chime_deck import press as press_module
from chime_deck.mqtt import (
    AVAILABILITY_ONLINE,
    DEFAULT_BUTTON_ICON,
    DEFAULT_DEVICE_NAME,
    DEFAULT_READ_TIMEOUT_S,
    DEVICE_KEY_ORDER,
    DISCOVERY_KEY_ORDER,
    DISCOVERY_PUBLISH_QOS,
    MQTT_PRESS_SOURCE,
    PRESS_QOS,
    ButtonReader,
    MqttBridge,
    MqttConfigurationError,
    MqttDiscovery,
    MqttSettings,
    build_discovery_payload,
    build_selection_payload,
    button_default_entity_id,
    button_icon,
    command_topic,
    configuration_url,
    discovery_topic,
    encode_payload,
    is_mqtt_safe_icon,
    selection_slug,
    selection_state_topic,
)
from chime_deck.store import (
    Button,
    ButtonIn,
    ConfigStore,
    Entity,
    EventBus,
    EventHandler,
    Sound,
    SoundIn,
    button_event,
    settings_event,
    sound_event,
)

pytestmark = pytest.mark.asyncio

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
"""Repository root, used by the import-isolation subprocess and the ast check."""

GOLDEN_DIR: Final = Path(__file__).parent / "golden"
"""Directory holding the pretty-printed discovery payload fixtures."""

GOLDEN_VERSION: Final = "9.9.9"
"""Fixed ``sw_version`` injected into the golden comparison."""

LOGGER_NAME: Final = "chime_deck.mqtt.discovery"
"""Logger the discovery service writes through."""

CONFIG_PREFIX: Final = "homeassistant"
"""Discovery prefix of the default test settings."""

INSTANCE: Final = "default"
"""Instance id of the default test settings."""

SLUG: Final = "front-door"
"""Slug of the button every lifecycle test creates."""

HOST: Final = "broker.test"
"""Broker host used by every fake settings object."""

AVAILABILITY: Final = "chime_deck/default/availability"
"""Retained availability topic of the default instance."""

STATUS_TOPIC: Final = "homeassistant/status"
"""Home Assistant's own birth and will topic."""

CONFIG_TOPIC: Final = "homeassistant/button/chime_deck_default_front-door/config"
"""Discovery topic of the default test button."""

COMMAND_TOPIC: Final = "chime_deck/default/button/front-door/set"
"""Command topic of the default test button."""

STALL_S: Final = 5.0
"""How long a stalling reader sleeps; the caller's bound cancels it far sooner."""

_MALFORMED_PAYLOADS: Final[tuple[bytes, ...]] = (
    b"",
    b"press",
    b"Press",
    b"PRESS\n",
    b"PRESS ",
    b"PRESS\x00",
    b"\xff\xfe\x00garbage",
    b"PRESS!",
)
"""Every payload that must not press a button: strict bytes equality only."""

_UNUSABLE_BINDS: Final[tuple[tuple[str, int], ...]] = (
    ("", 8080),
    ("   ", 8080),
    ("localhost", 8080),
    ("LOCALHOST", 8080),
    (" 127.0.0.1 ", 8080),
    ("127.0.0.1", 8080),
    ("127.5.5.5", 8080),
    ("::1", 8080),
    ("[::1]", 8080),
    ("0.0.0.0", 8080),
    (" 0.0.0.0 ", 8080),
    ("::", 8080),
    ("[::]", 8080),
    ("*", 8080),
    (" *", 8080),
    ("1.2.3.4/x", 8080),
    ("a b", 8080),
    ("2001:db8::5", 8080),
    ("192.168.0.50", 0),
    ("192.168.0.50", 65536),
)
"""Host/port pairs whose ``configuration_url`` must be omitted entirely."""


class RecordingPress:
    """A :class:`PressHandler` that records the presses it was given."""

    def __init__(self) -> None:
        """Create a recorder with no calls yet."""
        self.calls: list[tuple[str, str]] = []

    async def press(self, slug: str, *, source: str = "mqtt") -> object:
        """Record one press instead of playing anything.

        :param slug: the button slug that was pressed.
        :param source: press-source tag.
        """
        self.calls.append((slug, source))
        return None


class CountingBus(EventBus):
    """An :class:`EventBus` that records every handler subscription it receives."""

    def __init__(self) -> None:
        """Create a bus that has recorded no handler yet."""
        super().__init__()
        self.handlers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """Record one handler, then delegate to the real bus.

        :param handler: sync or async callable receiving each event.
        """
        self.handlers.append(handler)
        return super().subscribe(handler)


class StubButtons:
    """A :class:`ButtonReader` whose rows, stalls and failures the test controls."""

    def __init__(self, buttons: list[Button], *, listed: bool = True) -> None:
        """Store the rows the stub answers with.

        :param buttons: rows available by slug.
        :param listed: whether :meth:`list_buttons` returns the rows; a test can
            hide a row to prove that a single-slug read still publishes it.
        """
        self.buttons = list(buttons)
        self.listed = listed
        self.stalled: set[str] = set()
        self.failing: set[str] = set()

    async def list_buttons(self) -> list[Button]:
        """Return the visible rows, or none when the test hid them."""
        return list(self.buttons) if self.listed else []

    async def get_button_by_slug(self, slug: str) -> Button | None:
        """Return one row, stall when asked, or fail when asked.

        A stalled read sleeps :data:`STALL_S`; the discovery service's own
        ``read_timeout_s`` cancels it long before that, so the test is bounded.

        :param slug: button slug to read.
        """
        if slug in self.stalled:
            await asyncio.sleep(STALL_S)
        if slug in self.failing:
            raise RuntimeError(f"the store failed while reading {slug}")
        for button in self.buttons:
            if button.slug == slug:
                return button
        return None


@dataclass
class Harness:
    """The real stack under test, wired exactly the way HSD-dtpo wires it."""

    bridge: MqttBridge
    discovery: MqttDiscovery
    broker: FakeBroker
    store: ConfigStore
    press: RecordingPress

    @property
    def client(self) -> FakeClient:
        """Return the first client the bridge built."""
        return self.broker.clients[0]


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


def _button(
    *,
    slug: str = SLUG,
    name: str = "Front Door Chime",
    icon: str | None = "mdi:doorbell",
    enabled: bool = True,
) -> Button:
    """Build one immutable button row without touching the database.

    :param slug: button slug.
    :param name: button display name.
    :param icon: icon, or ``None`` for "not set".
    :param enabled: whether the button is enabled.
    """
    return Button(
        id=1,
        slug=slug,
        name=name,
        icon=icon,
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
        enabled=enabled,
        sort_order=10,
        created_at="2026-01-01T00:00:00.000000Z",
        updated_at="2026-01-01T00:00:00.000000Z",
    )


def _tile() -> Entity:
    """Build one immutable entity row without touching the database.

    Only the shared ``device`` block is under test here, so the tile's own
    fields are the defaults a selection payload would carry anyway.
    """
    return Entity(
        id=1,
        entity_id="lock.front_door",
        display_name="Front Door",
        icon=None,
        accent_color=None,
        show_attributes=None,
        state_tones=None,
        hide_when_unavailable=False,
        colour_by_state=True,
        sort_order=10,
        created_at="2026-01-01T00:00:00.000000Z",
    )


def _without_device_name(payload: Mapping[str, object]) -> dict[str, object]:
    """Return *payload* with the device block's ``name`` removed.

    Two payloads that agree on everything except this one field are what a rename
    must produce: the display name has no identity (HSD-vwdy).

    :param payload: discovery or selection payload.
    """
    device = dict(_device(payload))
    device.pop("name")
    return {**payload, "device": device}


def _payload_for(host: str, port: int, *, sw_version: str = GOLDEN_VERSION) -> dict[str, object]:
    """Build the golden button's discovery payload for one bind.

    :param host: resolved bind host.
    :param port: resolved HTTP port.
    :param sw_version: version string for the device block.
    """
    return build_discovery_payload(
        _button(),
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


def _serialised(button: Button) -> str:
    """Return the payload the service publishes for *button* with default settings.

    :param button: button to serialise.
    """
    return encode_payload(
        build_discovery_payload(button, settings=_settings(), availability_topic=AVAILABILITY)
    )


async def _ensure_sound(store: ConfigStore) -> Sound:
    """Return the store's first sound, creating one when the store is empty.

    :param store: connected store.
    """
    sounds = await store.list_sounds()
    if sounds:
        return sounds[0]
    return await store.create_sound(
        SoundIn(name="Ding dong", source_type="file", path="ding-dong.mp3")
    )


async def _create_button(
    store: ConfigStore,
    *,
    name: str = "Front Door Chime",
    slug: str = SLUG,
    icon: str | None = "mdi:doorbell",
    enabled: bool = True,
) -> Button:
    """Create the test button in *store* and return the saved row.

    :param store: connected store.
    :param name: button display name.
    :param slug: explicit slug, because the derived one would be longer.
    :param icon: icon, or ``None``.
    :param enabled: whether the button starts enabled.
    """
    sound = await _ensure_sound(store)
    return await store.upsert_button(
        ButtonIn(
            name=name,
            action_type="sound",
            sound_id=sound.id,
            slug=slug,
            icon=icon,
            enabled=enabled,
        )
    )


async def _set_enabled(store: ConfigStore, button: Button, *, enabled: bool) -> Button:
    """Update *button* with a new enabled flag and return the saved row.

    :param store: connected store.
    :param button: row to update.
    :param enabled: the new flag.
    """
    return await store.upsert_button(
        ButtonIn(
            id=button.id,
            name=button.name,
            action_type=button.action_type,
            sound_id=button.sound_id,
            slug=button.slug,
            icon=button.icon,
            enabled=enabled,
        )
    )


async def _rename(store: ConfigStore, button: Button, slug: str) -> Button:
    """Rename *button* to *slug* and return the saved row.

    :param store: connected store.
    :param button: row to update.
    :param slug: the new slug.
    """
    return await store.upsert_button(
        ButtonIn(
            id=button.id,
            name=button.name,
            action_type=button.action_type,
            sound_id=button.sound_id,
            slug=slug,
            icon=button.icon,
            enabled=button.enabled,
        )
    )


def _build(
    *,
    store: ConfigStore,
    bus: EventBus,
    press: RecordingPress,
    buttons: ButtonReader | None = None,
    settings: MqttSettings | None = None,
    server_host: str = "127.0.0.1",
    server_port: int = 8080,
    read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
) -> Harness:
    """Build the bridge and the discovery service wired to each other.

    :param store: connected store the service reads buttons from.
    :param bus: event bus the service subscribes to.
    :param press: press recorder the service routes presses through.
    :param buttons: reader override, for the stubbed-reader tests.
    :param settings: MQTT settings, defaulting to the standard test settings.
    :param server_host: resolved bind host for the configuration URL.
    :param server_port: resolved HTTP port for the configuration URL.
    :param read_timeout_s: bound for one discovery store read.
    """
    broker = FakeBroker()
    bridge = MqttBridge(
        settings=_settings() if settings is None else settings,
        client_factory=broker.factory,
        sleep=RecordingSleep(),
        rng=FixedRandom(0.0),
        seam_timeout_s=5.0,
        shutdown_timeout_s=1.0,
    )
    discovery = MqttDiscovery(
        bridge=bridge,
        buttons=store if buttons is None else buttons,
        press=press,
        bus=bus,
        server_host=server_host,
        server_port=server_port,
        read_timeout_s=read_timeout_s,
    )
    bridge.set_on_birth(discovery.on_birth)
    bridge.set_on_message(discovery.on_message)
    return Harness(bridge=bridge, discovery=discovery, broker=broker, store=store, press=press)


@asynccontextmanager
async def _open_harness(
    db_path: Path,
    *,
    buttons: ButtonReader | None = None,
    settings: MqttSettings | None = None,
    bus: EventBus | None = None,
    server_host: str = "127.0.0.1",
    server_port: int = 8080,
    read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    press: RecordingPress | None = None,
) -> AsyncIterator[Harness]:
    """Open a temporary store and yield a fully wired harness over it.

    :param db_path: temporary database file path.
    :param buttons: reader override, for the stubbed-reader tests.
    :param settings: MQTT settings, defaulting to the standard test settings.
    :param bus: event bus override, so a test can observe the subscriptions.
    :param server_host: resolved bind host for the configuration URL.
    :param server_port: resolved HTTP port for the configuration URL.
    :param read_timeout_s: bound for one discovery store read.
    :param press: press recorder; a fresh one when omitted.
    """
    bus = EventBus() if bus is None else bus
    async with ConfigStore(db_path, events=bus) as store:
        yield _build(
            store=store,
            bus=bus,
            press=RecordingPress() if press is None else press,
            buttons=buttons,
            settings=settings,
            server_host=server_host,
            server_port=server_port,
            read_timeout_s=read_timeout_s,
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


def _published(harness: Harness, topic: str) -> list[tuple[object, object, object, object]]:
    """Return every publish the client made to *topic*, in order.

    :param harness: the stack under test.
    :param topic: topic to filter on.
    """
    return [
        (published_topic, payload, qos, retain)
        for published_topic, payload, qos, retain in harness.client.published
        if published_topic == topic
    ]


def _payloads(harness: Harness, topic: str) -> list[str]:
    """Return every payload published to *topic*, in order.

    :param harness: the stack under test.
    :param topic: topic to filter on.
    """
    payloads: list[str] = []
    for published in _published(harness, topic):
        payload = published[1]
        assert isinstance(payload, str)
        payloads.append(payload)
    return payloads


def _parsed(harness: Harness, topic: str) -> dict[str, object]:
    """Return the last payload published to *topic*, parsed as JSON.

    :param harness: the stack under test.
    :param topic: topic to read.
    """
    payloads = _payloads(harness, topic)
    assert payloads, f"nothing was published to {topic}"
    parsed = json.loads(payloads[-1])
    assert isinstance(parsed, dict)
    return cast("dict[str, object]", parsed)


def _publish_failures(caplog: pytest.LogCaptureFixture, topic: str) -> list[logging.LogRecord]:
    """Return the warning-or-higher records that name one failed publish.

    :param caplog: pytest log capture.
    :param topic: topic whose failure message is looked for.
    """
    return [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and topic in record.getMessage()
    ]


def _golden(name: str) -> tuple[str, object]:
    """Read one golden file and parse it with the version placeholder replaced.

    :param name: golden file name inside :data:`GOLDEN_DIR`.
    """
    text = (GOLDEN_DIR / name).read_text(encoding="utf-8")
    assert "@VERSION@" in text, "the golden file lost its version placeholder"
    return text, json.loads(text.replace("@VERSION@", GOLDEN_VERSION))


# ----------------------------------------------------------------- golden files


async def test_loopback_payload_matches_the_golden_file() -> None:
    """The loopback payload is exactly the pinned JSON, in the pinned key order."""
    text, golden = _golden("discovery_loopback.json")
    payload = _payload_for("127.0.0.1", 8080)
    assert payload == golden
    assert tuple(payload) == DISCOVERY_KEY_ORDER
    assert tuple(_device(payload)) == DEVICE_KEY_ORDER[:5]
    assert tuple(cast("Mapping[str, object]", json.loads(text))) == DISCOVERY_KEY_ORDER
    assert json.loads(encode_payload(payload)) == payload


async def test_lan_payload_matches_the_golden_file() -> None:
    """The LAN payload is exactly the pinned JSON, including the configuration URL."""
    text, golden = _golden("discovery_lan.json")
    payload = _payload_for("192.168.0.50", 8080)
    assert payload == golden
    assert tuple(payload) == DISCOVERY_KEY_ORDER
    assert tuple(_device(payload)) == DEVICE_KEY_ORDER
    assert _device(payload)["configuration_url"] == "http://192.168.0.50:8080/settings"
    assert tuple(cast("Mapping[str, object]", json.loads(text))) == DISCOVERY_KEY_ORDER
    assert json.loads(encode_payload(payload)) == payload


async def test_the_device_block_publishes_the_configured_device_name() -> None:
    """Both builders take the ``device`` block's name from the settings.

    Two settings that differ only in ``device_name`` must produce identical
    payloads apart from that one field: the name changes no topic, no
    ``unique_id`` and no device identifier, which is what lets a rename keep the
    Home Assistant entities in place (HSD-vwdy).
    """
    default_settings = _settings()
    named_settings = MqttSettings(
        host=HOST,
        discovery_prefix=CONFIG_PREFIX,
        instance_id=INSTANCE,
        device_name="Hall Doorbell",
    )
    button = _button()
    tile = _tile()
    button_payloads = [
        build_discovery_payload(button, settings=settings, availability_topic=AVAILABILITY)
        for settings in (default_settings, named_settings)
    ]
    selection_payloads = [
        build_selection_payload(
            tile,
            slug=selection_slug(tile),
            settings=settings,
            availability_topic=AVAILABILITY,
        )
        for settings in (default_settings, named_settings)
    ]

    for plain, custom in (button_payloads, selection_payloads):
        assert _device(plain)["name"] == DEFAULT_DEVICE_NAME
        assert _device(custom)["name"] == "Hall Doorbell"
        assert _without_device_name(custom) == _without_device_name(plain)

    # Spelled out for the properties the ticket names, so a failure says which.
    plain_button, custom_button = button_payloads
    assert custom_button["unique_id"] == plain_button["unique_id"]
    assert custom_button["command_topic"] == plain_button["command_topic"]
    assert custom_button["command_topic"] == command_topic(named_settings, button.slug)
    assert discovery_topic(named_settings, button.slug) == discovery_topic(
        default_settings, button.slug
    )
    assert _device(custom_button)["identifiers"] == _device(plain_button)["identifiers"]

    plain_selection, custom_selection = selection_payloads
    assert custom_selection["unique_id"] == plain_selection["unique_id"]
    assert custom_selection["state_topic"] == plain_selection["state_topic"]
    assert custom_selection["state_topic"] == selection_state_topic(
        named_settings, selection_slug(tile)
    )


async def test_the_default_sw_version_is_the_package_version() -> None:
    """A payload built without ``sw_version`` reports the installed version."""
    payload = build_discovery_payload(
        _button(), settings=_settings(), availability_topic=AVAILABILITY
    )
    assert _device(payload)["sw_version"] == chime_deck.__version__


# ------------------------------------------------------------------------- icons


async def test_an_unset_icon_publishes_the_default_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A button with no icon gets the default quietly.

    :param caplog: pytest log capture.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    assert button_icon(_button(icon=None)) == DEFAULT_BUTTON_ICON
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("icon", ["", "   "])
async def test_a_blank_icon_publishes_the_default_without_warning(
    caplog: pytest.LogCaptureFixture, icon: str
) -> None:
    """A blank icon is the same supported state as an absent one, not a warning.

    :param caplog: pytest log capture.
    :param icon: blank icon under test.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    assert button_icon(_button(icon=icon)) == DEFAULT_BUTTON_ICON
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("icon", [None, "", "   "])
async def test_a_blank_icon_is_mqtt_safe(icon: str | None) -> None:
    """An absent icon is a supported state, published as the default (HSD-po1g).

    :param icon: absent or blank icon under test.
    """
    assert is_mqtt_safe_icon(icon) is True


@pytest.mark.parametrize("icon", ["mdi:doorbell", "mdi:bell-ring-outline", "mdi:doorbell-1"])
async def test_an_mdi_icon_is_mqtt_safe(icon: str) -> None:
    """The only form Home Assistant accepts on a button passes the check.

    :param icon: valid `mdi:` icon under test.
    """
    assert is_mqtt_safe_icon(icon) is True


@pytest.mark.parametrize(
    "icon",
    ["Front Door", "mdi: front door", "mdi:", "::", "mdi:door bell", " doorbell", "custom:cat.svg"],
)
async def test_an_icon_outside_the_mdi_grammar_is_not_mqtt_safe(icon: str) -> None:
    """Anything but an `mdi:` name is refused before it can reach a payload.

    :param icon: unusable icon under test.
    """
    assert is_mqtt_safe_icon(icon) is False


@pytest.mark.parametrize(
    "icon", ["Front Door", "mdi: front door", "mdi:", "::", "mdi:door bell", " doorbell"]
)
async def test_a_malformed_icon_falls_back_with_a_warning(
    caplog: pytest.LogCaptureFixture, icon: str
) -> None:
    """An icon Home Assistant would reject is replaced, with one warning naming it.

    :param caplog: pytest log capture.
    :param icon: malformed icon under test.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    assert button_icon(_button(icon=icon)) == DEFAULT_BUTTON_ICON
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert SLUG in warnings[0].getMessage()
    assert repr(icon) in warnings[0].getMessage()


@pytest.mark.parametrize("icon", ["mdi:doorbell", "mdi:bell-ring-outline", "mdi:doorbell-1"])
async def test_a_valid_icon_is_kept(caplog: pytest.LogCaptureFixture, icon: str) -> None:
    """A valid icon is published unchanged, with no warning.

    :param caplog: pytest log capture.
    :param icon: valid icon under test.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    assert button_icon(_button(icon=icon)) == icon
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


async def test_the_default_icon_is_itself_valid(caplog: pytest.LogCaptureFixture) -> None:
    """The fallback icon passes the same validation, so it can never be rejected.

    :param caplog: pytest log capture.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    assert button_icon(_button(icon=DEFAULT_BUTTON_ICON)) == DEFAULT_BUTTON_ICON
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


# --------------------------------------------------------------- configuration


@pytest.mark.parametrize(("host", "port"), _UNUSABLE_BINDS)
async def test_configuration_url_is_omitted_for_unusable_binds(host: str, port: int) -> None:
    """An unreachable or malformed bind publishes no configuration URL at all.

    :param host: bind host under test.
    :param port: HTTP port under test.
    """
    assert configuration_url(host, port) is None


@pytest.mark.parametrize(
    "host",
    ["192.168.0.50", "10.0.0.5", "doorbell.local", " 192.168.0.50 ", "Doorbell.Local"],
)
async def test_configuration_url_is_built_for_a_reachable_bind(host: str) -> None:
    """A LAN bind publishes exactly the URL the UI listens on.

    :param host: bind host under test.
    """
    assert configuration_url(host, 8080) == f"http://{host.strip().lower()}:8080/settings"


async def test_a_loopback_bind_omits_the_key_and_a_lan_bind_includes_it(
    tmp_path: Path,
) -> None:
    """The ``device`` block omits ``configuration_url`` entirely for a loopback bind.

    :param tmp_path: pytest temporary directory.
    """
    async with _open_harness(tmp_path / "loopback.db", server_host="127.0.0.1") as loopback:
        await _connect(loopback)
        assert loopback.discovery.configuration_url is None
        await _create_button(loopback.store)
        assert "configuration_url" not in _device(_parsed(loopback, CONFIG_TOPIC))

    async with _open_harness(tmp_path / "lan.db", server_host="192.168.0.50") as lan:
        await _connect(lan)
        assert lan.discovery.configuration_url == "http://192.168.0.50:8080/settings"
        await _create_button(lan.store)
        assert (
            _device(_parsed(lan, CONFIG_TOPIC))["configuration_url"]
            == "http://192.168.0.50:8080/settings"
        )


# --------------------------------------------------------------------- connect


async def test_connect_publishes_availability_before_discovery(harness: Harness) -> None:
    """Availability ``online`` precedes every non-retained discovery publish.

    :param harness: the stack under test.
    """
    await _create_button(harness.store)
    await _connect(harness)
    await wait_until(lambda: _payloads(harness, CONFIG_TOPIC) != [])
    publishes = [
        (index, event) for index, event in enumerate(harness.broker.events) if event[0] == "publish"
    ]
    first_index, first = publishes[0]
    assert first[1] == AVAILABILITY
    assert first[2] == AVAILABILITY_ONLINE
    for index, event in publishes:
        topic = event[1]
        assert isinstance(topic, str)
        if topic.endswith("/config"):
            assert index > first_index
            assert event[3] == DISCOVERY_PUBLISH_QOS
            assert event[4] is False


async def test_topics_are_exactly_the_designed_ones(harness: Harness) -> None:
    """The published topic, payload, QoS, retain flag and subscription are exact.

    :param harness: the stack under test.
    """
    await _connect(harness)
    created = await _create_button(harness.store)
    assert (
        "publish",
        CONFIG_TOPIC,
        _serialised(created),
        DISCOVERY_PUBLISH_QOS,
        False,
    ) in harness.broker.events
    assert ("subscribe", COMMAND_TOPIC, PRESS_QOS) in harness.broker.events
    assert harness.bridge.subscriptions[COMMAND_TOPIC] == PRESS_QOS
    assert _parsed(harness, CONFIG_TOPIC)["qos"] == PRESS_QOS
    # Design 02 §2 pins 0 for both the payload field and the command subscription;
    # our own non-retained config publish is deliberately at-least-once (1).
    assert PRESS_QOS == 0
    assert DISCOVERY_PUBLISH_QOS == 1


async def test_prefix_and_instance_come_from_the_settings(tmp_path: Path) -> None:
    """A custom prefix and instance id are substituted without assuming depth.

    :param tmp_path: pytest temporary directory.
    """
    settings = _settings(discovery_prefix="homeassistant/v2", instance_id="rig")
    config_topic = "homeassistant/v2/button/chime_deck_rig_front-door/config"
    press_topic = "chime_deck/rig/button/front-door/set"
    async with _open_harness(tmp_path / "custom.db", settings=settings) as harness:
        await _connect(harness)
        await _create_button(harness.store)
        assert ("subscribe", press_topic, PRESS_QOS) in harness.broker.events
        expected = encode_payload(
            build_discovery_payload(
                await _require_button(harness.store, SLUG),
                settings=settings,
                availability_topic="chime_deck/rig/availability",
            )
        )
        assert (
            "publish",
            config_topic,
            expected,
            DISCOVERY_PUBLISH_QOS,
            False,
        ) in harness.broker.events
        assert harness.discovery.discovery_prefix == "homeassistant/v2"
        assert harness.discovery.instance_id == "rig"


# ----------------------------------------------------------------------- start


async def test_start_republishes_and_subscribes_only_once(tmp_path: Path) -> None:
    """A discovery service started after the bridge connected still publishes.

    :meth:`MqttDiscovery.on_birth` already republishes on connect, but it cannot
    help a service that is started *after* that connect: every button created in
    that window would stay invisible unless :meth:`MqttDiscovery.start` itself
    republishes. A second ``start()`` must also not subscribe the handler again.

    :param tmp_path: pytest temporary directory.
    """
    bus = CountingBus()
    async with _open_harness(tmp_path / "late.db", bus=bus) as harness:
        await harness.bridge.start()
        harness.broker.connect(harness.client)
        await wait_until(lambda: harness.bridge.connected)
        assert _published(harness, CONFIG_TOPIC) == []
        button = await _create_button(harness.store)
        assert _published(harness, CONFIG_TOPIC) == []
        await harness.discovery.start()
        # A republish is blank-then-announce (HSD-bnb0), not a bare config.
        assert _payloads(harness, CONFIG_TOPIC) == ["", _serialised(button)]
        await harness.discovery.start()
        # One subscriber now dispatches both ``config.buttons_changed`` and
        # ``config.entities_changed``, so the count still proves a second
        # ``start()`` added no handler.
        assert bus.handlers == [harness.discovery.on_config_changed]


# ------------------------------------------------------------------- lifecycle


async def _require_button(store: ConfigStore, slug: str) -> Button:
    """Return the stored button with *slug*, failing when it is missing.

    :param store: connected store.
    :param slug: button slug to look up.
    """
    button = await store.get_button_by_slug(slug)
    assert button is not None
    return button


async def test_creating_a_button_publishes_its_discovery(harness: Harness) -> None:
    """Creating a button publishes its full payload, once.

    :param harness: the stack under test.
    """
    await _connect(harness)
    created = await _create_button(harness.store)
    assert _payloads(harness, CONFIG_TOPIC) == [_serialised(created)]
    assert _parsed(harness, CONFIG_TOPIC)["name"] == created.name


async def test_renaming_a_button_retracts_the_old_topic_then_publishes_the_new_one(
    harness: Harness,
) -> None:
    """A rename retracts the old entity first, then publishes the new one.

    :param harness: the stack under test.
    """
    await _connect(harness)
    created = await _create_button(harness.store)
    renamed = await _rename(harness.store, created, "front-bell")
    new_config = "homeassistant/button/chime_deck_default_front-bell/config"
    new_command = "chime_deck/default/button/front-bell/set"
    retract_event = ("publish", CONFIG_TOPIC, "", DISCOVERY_PUBLISH_QOS, False)
    publish_event = ("publish", new_config, _serialised(renamed), DISCOVERY_PUBLISH_QOS, False)
    assert retract_event in harness.broker.events
    assert publish_event in harness.broker.events
    assert harness.broker.events.index(retract_event) < harness.broker.events.index(publish_event)
    assert COMMAND_TOPIC not in harness.bridge.subscriptions
    assert COMMAND_TOPIC not in harness.client.subscriptions
    assert harness.bridge.subscriptions[new_command] == PRESS_QOS
    assert dict(harness.discovery.command_topics) == {new_command: "front-bell"}


async def test_disabling_a_button_publishes_an_empty_payload(harness: Harness) -> None:
    """Disabling keeps the row but removes the entity and the press route.

    :param harness: the stack under test.
    """
    await _connect(harness)
    created = await _create_button(harness.store)
    await _set_enabled(harness.store, created, enabled=False)
    assert ("publish", CONFIG_TOPIC, "", DISCOVERY_PUBLISH_QOS, False) in harness.broker.events
    assert await harness.store.get_button_by_slug(SLUG) is not None
    assert COMMAND_TOPIC not in harness.discovery.command_topics
    assert COMMAND_TOPIC not in harness.bridge.subscriptions


async def test_deleting_a_button_publishes_an_empty_payload(harness: Harness) -> None:
    """Deleting removes the entity and the press route.

    :param harness: the stack under test.
    """
    await _connect(harness)
    created = await _create_button(harness.store)
    await harness.store.delete_button(created.id)
    assert ("publish", CONFIG_TOPIC, "", DISCOVERY_PUBLISH_QOS, False) in harness.broker.events
    assert await harness.store.get_button_by_slug(SLUG) is None
    assert COMMAND_TOPIC not in harness.discovery.command_topics
    assert COMMAND_TOPIC not in harness.client.subscriptions


async def test_re_enabling_a_button_republishes_its_payload(harness: Harness) -> None:
    """The cache must not swallow the republish that follows a re-enable.

    :param harness: the stack under test.
    """
    await _connect(harness)
    created = await _create_button(harness.store)
    disabled = await _set_enabled(harness.store, created, enabled=False)
    enabled = await _set_enabled(harness.store, disabled, enabled=True)
    payloads = _payloads(harness, CONFIG_TOPIC)
    assert payloads == [_serialised(created), "", _serialised(enabled)]
    assert ("subscribe", COMMAND_TOPIC, PRESS_QOS) in harness.broker.events


async def test_an_unchanged_button_event_publishes_nothing(harness: Harness) -> None:
    """An event whose payload did not change publishes nothing new.

    :param harness: the stack under test.
    """
    await _connect(harness)
    created = await _create_button(harness.store)
    before = len(_published(harness, CONFIG_TOPIC))
    await harness.store.upsert_button(
        ButtonIn(
            id=created.id,
            name=created.name,
            action_type=created.action_type,
            sound_id=created.sound_id,
            slug=created.slug,
            icon=created.icon,
        )
    )
    assert len(_published(harness, CONFIG_TOPIC)) == before


async def test_a_birth_republishes_even_when_nothing_changed(harness: Harness) -> None:
    """A reconnect and an HA birth each republish the identical payload.

    Every republish is blank-then-announce (HSD-bnb0), so each birth adds two
    publishes to the button's discovery topic, never fewer.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await _create_button(harness.store)
    assert len(_published(harness, CONFIG_TOPIC)) == 1
    harness.broker.drop(harness.client)
    await wait_until(lambda: not harness.bridge.connected)
    harness.broker.connect(harness.client)
    await wait_until(lambda: len(_published(harness, CONFIG_TOPIC)) == 3)
    harness.broker.publish_from_test(STATUS_TOPIC, b"online")
    await wait_until(lambda: len(_published(harness, CONFIG_TOPIC)) == 5)


async def test_a_button_created_while_disconnected_is_published_on_connect(
    harness: Harness,
) -> None:
    """The bridge's buffer flush delivers a create made while the broker was down.

    :param harness: the stack under test.
    """
    await harness.bridge.start()
    await harness.discovery.start()
    created = await _create_button(harness.store)
    assert _published(harness, CONFIG_TOPIC) == []
    harness.broker.connect(harness.client)
    await wait_until(lambda: _payloads(harness, CONFIG_TOPIC) != [])
    assert _payloads(harness, CONFIG_TOPIC)[0] == _serialised(created)
    _, _, qos, retain = _published(harness, CONFIG_TOPIC)[0]
    assert qos == DISCOVERY_PUBLISH_QOS
    assert retain is False


async def test_a_delete_while_disconnected_is_flushed_then_everything_is_republished(
    harness: Harness,
) -> None:
    """A delete made while the broker was down reaches the wire on connect.

    :param harness: the stack under test.
    """
    await _connect(harness)
    created = await _create_button(harness.store)
    harness.broker.drop(harness.client)
    await wait_until(lambda: not harness.bridge.connected)
    published_before = len(_published(harness, CONFIG_TOPIC))
    await harness.store.delete_button(created.id)
    assert len(_published(harness, CONFIG_TOPIC)) == published_before
    harness.broker.connect(harness.client)
    await wait_until(
        lambda: ("publish", CONFIG_TOPIC, "", DISCOVERY_PUBLISH_QOS, False) in harness.broker.events
    )
    assert harness.discovery.published[SLUG] is None


async def test_a_birth_retracts_a_tracked_but_missing_button(tmp_path: Path) -> None:
    """A row that vanished while we believed it published is retracted on birth.

    :param tmp_path: pytest temporary directory.
    """
    reader = StubButtons([_button()])
    async with _open_harness(tmp_path / "missing.db", buttons=reader) as harness:
        await _connect(harness)
        assert _payloads(harness, CONFIG_TOPIC) != []
        reader.buttons.clear()
        await harness.discovery.on_birth()
        assert _payloads(harness, CONFIG_TOPIC)[-1] == ""
        subscribe_events = [
            event
            for event in harness.broker.events
            if event[0] == "subscribe" and event[1] == COMMAND_TOPIC
        ]
        assert len(subscribe_events) == 1
        assert COMMAND_TOPIC not in harness.discovery.command_topics


async def test_an_unsafe_rename_retracts_the_old_topic(harness: Harness) -> None:
    """A rename to a slug MQTT cannot carry retracts the old entity and stops.

    :param harness: the stack under test.
    """
    await _connect(harness)
    created = await _create_button(harness.store)
    await _rename(harness.store, created, "front.door")
    new_config = "homeassistant/button/chime_deck_default_front.door/config"
    new_command = "chime_deck/default/button/front.door/set"
    assert [payload for payload in _payloads(harness, CONFIG_TOPIC) if payload == ""] == [""]
    assert _payloads(harness, new_config) == []
    assert not [
        event
        for event in harness.broker.events
        if event[0] == "subscribe" and event[1] == new_command
    ]
    assert dict(harness.discovery.command_topics) == {}
    assert COMMAND_TOPIC not in harness.client.subscriptions


async def test_a_birth_skips_a_button_whose_slug_is_unusable(harness: Harness) -> None:
    """A birth publishes the usable buttons and skips the unusable slug entirely.

    The second, usable button proves the skip is per-button rather than the whole
    republish being skipped.

    :param harness: the stack under test.
    """
    await _connect(harness)
    created = await _create_button(harness.store)
    await _rename(harness.store, created, "front.door")
    back = await _create_button(harness.store, name="Back Door", slug="back-door")
    back_config = "homeassistant/button/chime_deck_default_back-door/config"
    unsafe_config = "homeassistant/button/chime_deck_default_front.door/config"
    unsafe_command = "chime_deck/default/button/front.door/set"
    published_before = len(_payloads(harness, back_config))
    await harness.discovery.on_birth()
    # Blank-then-announce: the usable button is still there, under its pinned id.
    assert _payloads(harness, back_config)[published_before:] == ["", _serialised(back)]
    assert _payloads(harness, unsafe_config) == []
    assert not [
        event
        for event in harness.broker.events
        if event[0] == "subscribe" and event[1] == unsafe_command
    ]


async def test_a_rename_whose_new_row_read_stalls_does_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A stalled read leaves both the old and the new entity exactly as they were.

    :param tmp_path: pytest temporary directory.
    :param caplog: pytest log capture.
    """
    reader = StubButtons([_button()])
    reader.stalled.add("front-bell")
    async with _open_harness(tmp_path / "stall.db", buttons=reader, read_timeout_s=0.05) as harness:
        await _connect(harness)
        publishes_before = len(harness.client.published)
        unsubscribes_before = len(
            [event for event in harness.broker.events if event[0] == "unsubscribe"]
        )
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        await harness.discovery.on_buttons_changed(
            button_event(slugs=("front-bell",), renamed=(("front-door", "front-bell"),))
        )
        assert len(harness.client.published) == publishes_before
        assert (
            len([event for event in harness.broker.events if event[0] == "unsubscribe"])
            == unsubscribes_before
        )
        warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "front-bell" in warnings[0].getMessage()
        assert COMMAND_TOPIC in harness.discovery.command_topics


async def test_an_event_without_slugs_is_a_noop(harness: Harness) -> None:
    """A button event carrying ids but no slugs publishes nothing.

    :param harness: the stack under test.
    """
    await _connect(harness)
    config_before = [
        event
        for event in harness.broker.events
        if event[0] == "publish" and isinstance(event[1], str) and event[1].endswith("/config")
    ]
    await harness.discovery.on_buttons_changed(button_event(ids=(1,)))
    config_after = [
        event
        for event in harness.broker.events
        if event[0] == "publish" and isinstance(event[1], str) and event[1].endswith("/config")
    ]
    assert config_after == config_before


async def test_a_non_button_event_is_ignored(harness: Harness) -> None:
    """A foreign event's ``removed_slugs`` never retracts a button.

    Only ``config.buttons_changed`` may retract: a sounds event can carry a slug
    that collides with a button's, and honouring it would blast a live Home
    Assistant entity away for a change that has nothing to do with buttons.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await _create_button(harness.store)
    publishes = len(_published(harness, CONFIG_TOPIC))
    unsubscribes = count(harness.broker.events, ("unsubscribe", COMMAND_TOPIC))
    # The PRE-rename purge (HSD-l84l) blanks the old availability topic during the
    # connect, so "retracted nothing" is measured against that baseline rather than
    # against zero: the event must add no blank of its own.
    blanks = [
        (topic, payload) for topic, payload, _, _ in harness.client.published if payload == ""
    ]
    await harness.discovery.on_buttons_changed(sound_event(removed_slugs=(SLUG,)))
    await harness.discovery.on_buttons_changed(settings_event(keys=("tts.engine",)))
    assert len(_published(harness, CONFIG_TOPIC)) == publishes
    after = [(topic, payload) for topic, payload, _, _ in harness.client.published if payload == ""]
    assert after == blanks
    assert count(harness.broker.events, ("unsubscribe", COMMAND_TOPIC)) == unsubscribes
    assert dict(harness.discovery.command_topics) == {COMMAND_TOPIC: SLUG}


async def test_a_failing_slug_does_not_swallow_the_rest_of_the_event(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One failing row read is logged, and the next slug of the event still runs.

    :param tmp_path: pytest temporary directory.
    :param caplog: pytest log capture.
    """
    reader = StubButtons([_button()], listed=False)
    reader.failing.add("boom")
    async with _open_harness(tmp_path / "failing.db", buttons=reader) as harness:
        await _connect(harness)
        assert _published(harness, CONFIG_TOPIC) == []
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        await harness.discovery.on_buttons_changed(button_event(slugs=("boom", SLUG)))
        assert _parsed(harness, CONFIG_TOPIC)["name"] == "Front Door Chime"
        failures = [record for record in caplog.records if record.levelno == logging.ERROR]
        assert len(failures) == 1
        assert "boom" in failures[0].getMessage()


# ------------------------------------------------------------------- press path


async def test_presses_route_to_the_press_service(harness: Harness) -> None:
    """A ``PRESS`` on a routed command topic presses the matching slug.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await _create_button(harness.store)
    harness.broker.publish_from_test(COMMAND_TOPIC, b"PRESS")
    await wait_until(lambda: harness.press.calls != [])
    assert harness.press.calls == [(SLUG, "mqtt")]
    assert MQTT_PRESS_SOURCE == press_module.SOURCE_MQTT


@pytest.mark.parametrize("payload", _MALFORMED_PAYLOADS)
async def test_malformed_payloads_are_ignored(harness: Harness, payload: bytes) -> None:
    """Only the exact bytes ``PRESS`` press a button.

    The payload is handed to the service directly so the assertion cannot race
    the bridge's seam; the routed path is proven by the test above.

    :param harness: the stack under test.
    :param payload: payload that must be ignored.
    """
    await _connect(harness)
    await _create_button(harness.store)
    await harness.discovery.on_message(COMMAND_TOPIC, payload)
    assert harness.press.calls == []


async def test_a_message_on_an_unregistered_topic_is_ignored_at_debug(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """A message on a topic that is not routed is ignored, and logged at DEBUG.

    :param harness: the stack under test.
    :param caplog: pytest log capture.
    """
    await _connect(harness)
    unknown = "chime_deck/default/button/unknown/set"
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    await harness.discovery.on_message(unknown, b"PRESS")
    assert harness.press.calls == []
    debug_records = [
        record
        for record in caplog.records
        if record.levelno == logging.DEBUG and unknown in record.getMessage()
    ]
    assert len(debug_records) == 1
    assert "unknown topic" in debug_records[0].getMessage()


async def test_a_pruned_topic_does_not_chime(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """A broker-side filter that survived a prune still cannot press a button.

    :param harness: the stack under test.
    :param caplog: pytest log capture.
    """
    await _connect(harness)
    created = await _create_button(harness.store)
    await _set_enabled(harness.store, created, enabled=False)
    harness.client.subscriptions[COMMAND_TOPIC] = PRESS_QOS
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    harness.broker.deliver(COMMAND_TOPIC, b"PRESS")
    await wait_until(
        lambda: any(
            record.levelno == logging.DEBUG and "unknown topic" in record.getMessage()
            for record in caplog.records
        )
    )
    assert harness.press.calls == []


# ------------------------------------------------------------- lifecycle safety


async def test_no_lifecycle_step_ever_publishes_to_a_command_topic(harness: Harness) -> None:
    """No lifecycle step publishes to ``/set``; discovery publishes stay non-retained.

    Availability publishes keep their own ``retain=True``/``qos=1`` contract and
    are deliberately not asserted here, so this test can never force them to
    change.

    :param harness: the stack under test.
    """
    await _connect(harness)
    created = await _create_button(harness.store)
    renamed = await _rename(harness.store, created, "front-bell")
    disabled = await _set_enabled(harness.store, renamed, enabled=False)
    await _set_enabled(harness.store, disabled, enabled=True)
    await harness.store.delete_button(disabled.id)
    availability_before = len(_published(harness, AVAILABILITY))
    harness.broker.publish_from_test(STATUS_TOPIC, b"online")
    await wait_until(lambda: len(_published(harness, AVAILABILITY)) > availability_before)
    await harness.discovery.stop()
    await harness.bridge.stop()
    for event in harness.broker.events:
        if event[0] != "publish":
            continue
        topic = event[1]
        assert isinstance(topic, str)
        assert not topic.endswith("/set")
        if topic.startswith(f"{CONFIG_PREFIX}/button/") and topic.endswith("/config"):
            assert event[3] == DISCOVERY_PUBLISH_QOS
            assert event[4] is False


async def test_retract_all_removes_every_entity_and_route(harness: Harness) -> None:
    """The settings-generation handoff retracts every published entity, twice safely.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await _create_button(harness.store)
    await harness.discovery.retract_all()
    await harness.discovery.retract_all()
    assert [payload for payload in _payloads(harness, CONFIG_TOPIC) if payload == ""] == [""]
    assert dict(harness.discovery.command_topics) == {}
    assert harness.discovery.published[SLUG] is None
    assert COMMAND_TOPIC not in harness.bridge.subscriptions


async def test_a_failing_publish_does_not_break_republish_all(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """A broker-side failure on one payload still publishes the other one.

    ``MqttBridge.publish`` is the layer that contains a raising ``client.publish``
    (paho can raise from its own network thread), so the failure is one WARNING
    per failing publish naming the topic — and a republish publishes twice to a
    button's topic (the HSD-bnb0 blank and the announcement), hence two — and
    nothing escapes
    :meth:`~chime_deck.mqtt.discovery.MqttDiscovery.on_birth`.

    :param harness: the stack under test.
    :param caplog: pytest log capture.
    """
    await _connect(harness)
    await _create_button(harness.store)
    back = await _create_button(harness.store, name="Back Door", slug="back-door")
    back_config = "homeassistant/button/chime_deck_default_back-door/config"
    harness.client.publish_raises_for.add(CONFIG_TOPIC)
    caplog.set_level(logging.WARNING)
    await harness.discovery.on_birth()
    assert _payloads(harness, back_config)[-1] == _serialised(back)
    assert len(_publish_failures(caplog, CONFIG_TOPIC)) == 2


async def test_retract_all_survives_a_failing_publish(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed empty publish still drops the route and the publish-diff entry.

    The bookkeeping after the publish must not be skipped by a broker-side
    failure: a topic left in the router would be re-SUBSCRIBEd on every reconnect
    for the life of the bridge.

    :param harness: the stack under test.
    :param caplog: pytest log capture.
    """
    await _connect(harness)
    await _create_button(harness.store)
    harness.client.publish_raises_for.add(CONFIG_TOPIC)
    caplog.set_level(logging.WARNING)
    await harness.discovery.retract_all()
    assert COMMAND_TOPIC not in harness.bridge.subscriptions
    assert dict(harness.discovery.command_topics) == {}
    assert harness.discovery.published[SLUG] is None
    assert len(_publish_failures(caplog, CONFIG_TOPIC)) == 1


async def test_a_non_positive_read_timeout_is_refused(harness: Harness) -> None:
    """A read bound that could never fire is rejected at construction.

    :param harness: the stack under test, supplying the collaborators.
    """
    with pytest.raises(MqttConfigurationError):
        MqttDiscovery(
            bridge=harness.bridge,
            buttons=harness.store,
            press=harness.press,
            bus=EventBus(),
            server_host="127.0.0.1",
            server_port=8080,
            read_timeout_s=0.0,
        )


async def test_stop_publishes_nothing_to_discovery_topics(harness: Harness) -> None:
    """Stopping unsubscribes every command topic and publishes no discovery change.

    :param harness: the stack under test.
    """
    await _connect(harness)
    await _create_button(harness.store)
    before = len(_published(harness, CONFIG_TOPIC))
    await harness.discovery.stop()
    assert len(_published(harness, CONFIG_TOPIC)) == before
    assert COMMAND_TOPIC not in harness.bridge.subscriptions
    assert dict(harness.discovery.command_topics) == {}
    assert dict(harness.discovery.published) == {}
    await harness.bridge.stop()


# ----------------------------------------------------- button entity ids (HSD-bnb0)

_HOME_ASSISTANT_ENTITY_ID_SEPARATORS: Final = re.compile(r"[^a-z0-9]+")
"""Runs of characters Home Assistant replaces with ``_`` in an entity id part."""


def _home_assistant_slug(value: str) -> str:
    """Slug *value* the way Home Assistant composes a derived entity id part.

    :param value: device or entity display name.
    """
    return _HOME_ASSISTANT_ENTITY_ID_SEPARATORS.sub("_", value.lower()).strip("_")


def _device_name_entity_id(config: Mapping[str, object]) -> str:
    """Return the entity id Home Assistant derives when none is pinned.

    Home Assistant prefixes the device name, so device "ChimeDeck" plus button
    "Front Door Chime" is ``button.chimedeck_front_door_chime`` — the second
    spelling this ticket removes from one device page.

    :param config: one discovery config payload carrying no ``default_entity_id``.
    """
    device = config.get("device")
    device_name = ""
    if isinstance(device, Mapping):
        name = device.get("name")
        if isinstance(name, str):
            device_name = name
    return f"button.{_home_assistant_slug(device_name)}_{_home_assistant_slug(str(config['name']))}"


class HomeAssistantButtonRegistry:
    """A tiny in-test model of Home Assistant's MQTT entity discovery.

    It models the documented semantics this ticket relies on:

    * a config payload registers ``unique_id -> entity id``, where the entity id
      is ``default_entity_id`` when the payload carries one, otherwise Home
      Assistant's device-name-derived ``<device name> <entity name>`` slug form;
    * an empty payload deletes the entity registered on that discovery topic and
      tombstones its registry entry (an empty payload on a topic that never
      carried an entity is a no-op);
    * a config for a tombstoned ``unique_id`` restores the entry and takes the
      new ``default_entity_id``, while a config for a *live* ``unique_id`` never
      changes its entity id — an entity id is chosen once, at first
      registration, and a config update cannot move it.

    The third rule is the load-bearing one: it is why the pre-seeded migration
    test fails when the blank before the announcement is dropped. A model that
    let a config rename a live entry could not see that bug.
    """

    def __init__(self) -> None:
        """Create a registry with no entity and no tombstone."""
        self.entities: dict[str, str] = {}
        """Live entities as ``unique_id -> entity id``."""
        self.tombstones: dict[str, str] = {}
        """Removed registry entries as ``unique_id -> the entity id they held``."""
        self.topics: dict[str, str] = {}
        """Discovery topics as ``topic -> unique_id``, the way Home Assistant maps a blank."""

    def apply(self, topic: str, payload: str) -> None:
        """Apply one discovery publish, in publish order.

        :param topic: discovery topic the payload arrived on.
        :param payload: config payload, or ``""`` for a removal.
        """
        if payload == "":
            self._remove(topic)
        else:
            self._register(topic, payload)

    def button_entity_ids(self) -> list[str]:
        """Return every live button entity id, sorted for a stable assertion."""
        return sorted(
            entity_id for entity_id in self.entities.values() if entity_id.startswith("button.")
        )

    def _remove(self, topic: str) -> None:
        """Delete the entity on *topic* and tombstone its registry entry.

        :param topic: discovery topic the empty payload arrived on.
        """
        unique = self.topics.pop(topic, None)
        if unique is None:
            return
        entity_id = self.entities.pop(unique, None)
        if entity_id is not None:
            self.tombstones[unique] = entity_id

    def _register(self, topic: str, payload: str) -> None:
        """Register or restore the entity one config payload describes.

        :param topic: discovery topic the payload arrived on.
        :param payload: JSON config payload.
        """
        config: dict[str, object] = json.loads(payload)
        unique = str(config["unique_id"])
        self.topics[topic] = unique
        if unique in self.entities:
            return
        pinned = config.get("default_entity_id")
        if unique in self.tombstones:
            self.tombstones.pop(unique)
            assert isinstance(pinned, str), "a restored entity must take the new pinned id"
            self.entities[unique] = pinned
            return
        self.entities[unique] = (
            pinned if isinstance(pinned, str) else _device_name_entity_id(config)
        )


def _button_publishes(harness: Harness) -> list[tuple[str, str]]:
    """Return every recorded button discovery publish, in order.

    Only topics under ``<prefix>/button/`` are button discovery: availability,
    selection and legacy-purge publishes are not.

    :param harness: the stack under test.
    """
    publishes: list[tuple[str, str]] = []
    for topic, payload, _, _ in harness.client.published:
        if topic.startswith(f"{CONFIG_PREFIX}/button/"):
            assert isinstance(payload, str)
            publishes.append((topic, payload))
    return publishes


def _replay_button_publishes(
    harness: Harness, registry: HomeAssistantButtonRegistry, *, since: int = 0
) -> int:
    """Replay the recorded button discovery publishes from *since* through *registry*.

    :param harness: the stack under test.
    :param registry: model to apply the publishes to.
    :param since: how many button publishes were already replayed.
    :returns: the new count, to pass as *since* on the next call.
    """
    publishes = _button_publishes(harness)
    for topic, payload in publishes[since:]:
        registry.apply(topic, payload)
    return len(publishes)


async def test_the_button_payload_pins_its_entity_id() -> None:
    """The button payload pins ``button.chime_deck_<slug>`` in the designed position."""
    payload = build_discovery_payload(
        _button(), settings=_settings(), availability_topic=AVAILABILITY
    )
    assert payload["default_entity_id"] == "button.chime_deck_front_door"
    assert tuple(payload)[:3] == ("name", "unique_id", "default_entity_id")
    assert tuple(payload) == DISCOVERY_KEY_ORDER


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("front-door", "button.chime_deck_front_door"),
        ("front.door", "button.chime_deck_front_door"),
        ("dryer_done", "button.chime_deck_dryer_done"),
    ],
)
async def test_a_button_slug_is_mapped_into_the_object_id_grammar(slug: str, expected: str) -> None:
    """The stored slug is mapped into Home Assistant's object-id grammar.

    The stored slug keeps a dash (:func:`chime_deck.store.derive_slug`), but an
    entity id may not contain one, so the dash becomes ``_``. A legacy row whose
    slug still carries a dot maps to the same id, and a plain underscore slug is
    unchanged — one rule, three spellings of the same button.

    :param slug: stored button slug under test.
    :param expected: the entity id it must publish.
    """
    entity_id = button_default_entity_id(slug)
    assert entity_id == expected
    assert "-" not in entity_id.split(".", 1)[1]


async def test_the_pinned_entity_id_ignores_the_device_name_and_the_instance_id() -> None:
    """Only the slug names the entity: a device or instance rename cannot move it (D32).

    ``device.name`` labels the device page and ``mqtt.instance_id`` roots every
    topic, and both may change; the pinned id is slug-only, so Home Assistant
    keeps the entity an automation already references. That is the whole reason
    the value is pinned rather than derived from the device name.
    """
    default_settings = _settings()
    other_settings = MqttSettings(
        host=HOST,
        discovery_prefix=CONFIG_PREFIX,
        instance_id="rig",
        device_name="Hall Doorbell",
    )
    button = _button()
    default_payload = build_discovery_payload(
        button, settings=default_settings, availability_topic=AVAILABILITY
    )
    other_payload = build_discovery_payload(
        button, settings=other_settings, availability_topic=AVAILABILITY
    )
    assert default_payload["default_entity_id"] == "button.chime_deck_front_door"
    assert other_payload["default_entity_id"] == default_payload["default_entity_id"]
    # The identity root really did change, so the stability above is the pin and
    # not two identical settings.
    assert other_payload["unique_id"] != default_payload["unique_id"]
    assert discovery_topic(other_settings, button.slug) != discovery_topic(
        default_settings, button.slug
    )


async def test_the_button_config_is_blanked_before_it_is_announced(harness: Harness) -> None:
    """A republish ends with the empty config immediately followed by the announcement.

    The pair is asserted, not membership: an announcement without the preceding
    blank leaves Home Assistant holding the old entity id. Both publishes are
    non-retained and at :data:`DISCOVERY_PUBLISH_QOS`, the contract every
    discovery publish of ours follows.

    :param harness: the stack under test.
    """
    await _connect(harness)
    created = await _create_button(harness.store)
    await harness.discovery.on_birth()
    assert _published(harness, CONFIG_TOPIC)[-2:] == [
        (CONFIG_TOPIC, "", DISCOVERY_PUBLISH_QOS, False),
        (CONFIG_TOPIC, _serialised(created), DISCOVERY_PUBLISH_QOS, False),
    ]


async def test_a_pre_seeded_button_entity_converges_to_the_pinned_id(harness: Harness) -> None:
    """An entity already registered under the old id moves to the pinned one.

    This is the upgrade path. The previous build's config carries no
    ``default_entity_id``, so Home Assistant registered the button by device
    name: ``button.chimedeck_front_door_chime``. The current build blanks the
    same discovery topic and then announces the pinned id; Home Assistant
    restores the tombstoned entry with that id because an entity id is chosen at
    first registration and a config update alone can never move a live one.
    Dropping the blank breaks this test, which is why the model refuses to rename
    a live entry.

    :param harness: the stack under test.
    """
    registry = HomeAssistantButtonRegistry()
    created = await _create_button(harness.store)
    previous_payload = dict(
        build_discovery_payload(created, settings=_settings(), availability_topic=AVAILABILITY)
    )
    previous_payload.pop("default_entity_id")
    previous = encode_payload(previous_payload)
    # Seeded on the broker and fed to the model: discovery is non-retained, so a
    # running Home Assistant is the only holder of the previous config.
    harness.broker.publish_from_test(CONFIG_TOPIC, previous.encode())
    registry.apply(CONFIG_TOPIC, previous)
    assert registry.button_entity_ids() == ["button.chimedeck_front_door_chime"]

    await _connect(harness)
    await wait_until(lambda: _payloads(harness, CONFIG_TOPIC) != [])
    _replay_button_publishes(harness, registry)
    assert registry.button_entity_ids() == ["button.chime_deck_front_door"]
    assert "button.chimedeck_front_door_chime" not in registry.entities.values()
    assert list(registry.topics) == [CONFIG_TOPIC]
    assert registry.tombstones == {}


async def test_a_deleted_button_leaves_no_entity_behind(harness: Harness) -> None:
    """A delete publishes the blank on the unchanged discovery topic.

    The discovery topic is derived from ``unique_id``, which does not move on a
    delete, so the empty payload lands exactly where the entity is registered,
    and the registry holds a tombstone rather than a ghost.

    :param harness: the stack under test.
    """
    registry = HomeAssistantButtonRegistry()
    await _connect(harness)
    created = await _create_button(harness.store)
    replayed = _replay_button_publishes(harness, registry)
    assert registry.button_entity_ids() == ["button.chime_deck_front_door"]
    await harness.store.delete_button(created.id)
    assert _payloads(harness, CONFIG_TOPIC)[-1] == ""
    _replay_button_publishes(harness, registry, since=replayed)
    assert registry.button_entity_ids() == []
    assert len(registry.tombstones) == 1


async def test_a_renamed_button_purges_the_new_topic_before_announcing(harness: Harness) -> None:
    """A rename blanks the old topic, then the new one, then announces.

    A rename moves the pinned entity id with the slug, so the new topic is
    blanked before it is announced and the old topic is retracted: nothing is
    left under either id, and the entity ends up at the new pinned id only.

    :param harness: the stack under test.
    """
    registry = HomeAssistantButtonRegistry()
    await _connect(harness)
    created = await _create_button(harness.store)
    replayed = _replay_button_publishes(harness, registry)
    assert registry.button_entity_ids() == ["button.chime_deck_front_door"]

    renamed = await _rename(harness.store, created, "side-door")
    new_config = "homeassistant/button/chime_deck_default_side-door/config"
    assert _payloads(harness, CONFIG_TOPIC)[-1] == ""
    assert _payloads(harness, new_config)[-2:] == ["", _serialised(renamed)]
    _replay_button_publishes(harness, registry, since=replayed)
    assert registry.button_entity_ids() == ["button.chime_deck_side_door"]
    assert list(registry.topics) == [new_config]


async def test_a_blank_lost_while_disconnected_converges_on_the_next_birth(
    harness: Harness,
) -> None:
    """The birth seam repairs the one coalescer in this path: the offline buffer.

    The service is started before the CONNACK, so every publish of the first
    republish is buffered. The buffer is keyed by topic and keeps only the last
    payload per topic, so the blank is genuinely coalesced away and only the
    config survives. The bridge flushes the buffer and then invokes ``on_birth``
    on every connect, so the blank-then-announce pair arrives from the birth
    seam and Home Assistant converges with no timer of ours.

    :param harness: the stack under test.
    """
    registry = HomeAssistantButtonRegistry()
    await harness.bridge.start()
    created = await _create_button(harness.store)
    await harness.discovery.start()
    # Nothing has reached the wire yet: only the config survived the buffer.
    assert _published(harness, CONFIG_TOPIC) == []

    harness.broker.connect(harness.client)
    await wait_until(
        lambda: _payloads(harness, CONFIG_TOPIC) == [_serialised(created), "", _serialised(created)]
    )
    _replay_button_publishes(harness, registry)
    assert registry.button_entity_ids() == ["button.chime_deck_front_door"]


# ------------------------------------------------------------ import isolation


def _store_imports_outside_type_checking(tree: ast.Module) -> list[ast.ImportFrom]:
    """Return module-level ``..store`` imports that sit outside ``TYPE_CHECKING``.

    Only module-level statements (and the bodies of module-level ``if`` blocks)
    are inspected: the runtime ``ConfigEventName`` import lives inside
    :meth:`MqttDiscovery.on_buttons_changed`, so it is no module-level dependency
    of ``discovery.py``.

    :param tree: parsed module.
    """
    found: list[ast.ImportFrom] = []

    def visit(body: list[ast.stmt], *, type_checking: bool) -> None:
        """Walk one statement list, tracking the ``TYPE_CHECKING`` guard.

        :param body: statements to inspect.
        :param type_checking: whether the list sits inside ``if TYPE_CHECKING:``.
        """
        for node in body:
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                is_store = node.level >= 2 and module.split(".")[0] == "store"
                if is_store and not type_checking:
                    found.append(node)
            elif isinstance(node, ast.If):
                guarded = isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
                visit(node.body, type_checking=type_checking or guarded)
                visit(node.orelse, type_checking=type_checking)

    visit(tree.body, type_checking=False)
    return found


async def test_the_service_imports_no_store_models_at_runtime() -> None:
    """Importing the package must not drag in the web stack, and only types come from store.

    The DB layer itself is not asserted: the ``mqtt -> config -> store`` import
    edge predates this ticket and removing it is not this service's job. What is
    asserted is that the discovery module declares no *module-level* dependency
    on the store models or events package beyond the function-local ``StrEnum``.
    """
    code = (
        "import sys; import chime_deck.mqtt; "
        "assert not {'fastapi','uvicorn','httpx','websockets'} & set(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        timeout=60,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    tree = ast.parse((REPO_ROOT / "src/chime_deck/mqtt/discovery.py").read_text("utf-8"))
    assert _store_imports_outside_type_checking(tree) == []
    runtime_events_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "store.events"
    ]
    assert runtime_events_imports, "the enum import must still exist somewhere"


async def test_the_golden_directory_files_are_key_ordered() -> None:
    """Both goldens still parse and carry the designed number of keys."""
    _, loopback = _golden("discovery_loopback.json")
    _, lan = _golden("discovery_lan.json")
    loopback_payload = cast("Mapping[str, object]", loopback)
    lan_payload = cast("Mapping[str, object]", lan)
    assert tuple(loopback_payload) == DISCOVERY_KEY_ORDER
    assert tuple(lan_payload) == DISCOVERY_KEY_ORDER
    assert len(_device(loopback_payload)) == 5
    assert len(_device(lan_payload)) == 6
    assert tuple(_device(loopback_payload)) == DEVICE_KEY_ORDER[:5]
    assert tuple(_device(lan_payload)) == DEVICE_KEY_ORDER
