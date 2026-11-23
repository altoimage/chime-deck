"""The HSD-vwdy acceptance test: two units on one broker must not fight.

Two complete :class:`~chime_deck.mqtt.bridge.MqttBridge` +
:class:`~chime_deck.mqtt.discovery.MqttDiscovery` pairs share ONE
:class:`~mqtt_fakes.FakeBroker`, each with its own settings, its own store and its
own button -- and both announce the *same* button slug, deliberately, because the
slug is derived from the tile's name and two installs will collide on it.

What the test proves is that the instance id alone separates them: distinct
``unique_id``s, distinct discovery config topics, distinct device identifiers,
distinct retained availability topics, distinct client ids and distinct press
topics, a press delivered on one unit invoking only that unit's handler, and one
unit going offline leaving the other's retained availability alone (HSD-vwdy).

Everything is offline: the broker, the paho clients and the birth jitter are all
fakes from :mod:`mqtt_fakes`, and every await is bounded either by
:func:`~mqtt_fakes.wait_until` or by :func:`asyncio.timeout`. No real timer, no
socket, no network.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

import pytest
import pytest_asyncio
from mqtt_fakes import (
    FakeBroker,
    FixedRandom,
    RecordingSleep,
    wait_until,
)

from chime_deck.mqtt import (
    AVAILABILITY_OFFLINE,
    AVAILABILITY_ONLINE,
    PRESS_PAYLOAD,
    MqttBridge,
    MqttDiscovery,
    MqttSettings,
    command_topic,
    discovery_topic,
    unique_id,
)
from chime_deck.store import Button, ButtonIn, ConfigStore, EventBus, SoundIn

pytestmark = pytest.mark.asyncio

TIMEOUT_S: Final = 5.0
"""Bound for one lifecycle call or one wait; nothing here may hang the suite."""

HOST: Final = "broker.test"
"""Broker host both units are configured with: one broker, two clients."""

SLUG: Final = "front-door"
"""The button slug both units announce: identical on purpose."""

KITCHEN_INSTANCE: Final = "kitchen"
"""Instance id of the first unit."""

HALL_INSTANCE: Final = "hall"
"""Instance id of the second unit."""

KITCHEN_NAME: Final = "Kitchen"
"""Device name of the first unit."""

HALL_NAME: Final = "Hall Doorbell"
"""Device name of the second unit."""


class RecordingPress:
    """A press handler that records the presses routed to it."""

    def __init__(self) -> None:
        """Create a recorder with no calls yet."""
        self.calls: list[str] = []

    async def press(self, slug: str, *, source: str = "mqtt") -> object:
        """Record one press instead of playing anything.

        :param slug: the button slug that was pressed.
        :param source: press-source tag; unused here.
        """
        self.calls.append(slug)
        return None


@dataclass
class Unit:
    """One complete unit: its identity, its two MQTT halves and its press recorder."""

    settings: MqttSettings
    bridge: MqttBridge
    discovery: MqttDiscovery
    press: RecordingPress
    client_index: int
    """Position of this unit's client in ``broker.clients``, fixed at build time."""


@dataclass
class Installed:
    """Two units sharing one broker, with one seeded button per unit."""

    broker: FakeBroker
    kitchen: Unit
    hall: Unit

    @property
    def units(self) -> tuple[Unit, Unit]:
        """Return both units in build order."""
        return (self.kitchen, self.hall)


async def _seed_button(store: ConfigStore) -> Button:
    """Create the one sound button both units announce.

    The slug is explicit because the derived one would be longer, and the same
    for both units on purpose.

    :param store: connected store of one unit.
    """
    sound = await store.create_sound(
        SoundIn(name="Ding dong", source_type="file", path="ding-dong.mp3")
    )
    return await store.upsert_button(
        ButtonIn(name="Front Door", action_type="sound", sound_id=sound.id, slug=SLUG)
    )


def _build_unit(
    broker: FakeBroker,
    *,
    store: ConfigStore,
    instance_id: str,
    device_name: str,
    client_index: int,
) -> Unit:
    """Wire one bridge and one discovery service to *broker*, exactly as the app does.

    :param broker: the broker both units share.
    :param store: this unit's store.
    :param instance_id: pinned instance id of this unit.
    :param device_name: stored ``device.name`` of this unit.
    :param client_index: position this unit's client will take in ``broker.clients``.
    """
    settings = MqttSettings(host=HOST, instance_id=instance_id, device_name=device_name)
    press = RecordingPress()
    bridge = MqttBridge(
        settings=settings,
        client_factory=broker.factory,
        sleep=RecordingSleep(),
        rng=FixedRandom(0.0),
        seam_timeout_s=TIMEOUT_S,
        shutdown_timeout_s=1.0,
    )
    discovery = MqttDiscovery(
        bridge=bridge,
        buttons=store,
        press=press,
        bus=EventBus(),
        server_host="127.0.0.1",
        server_port=8080,
    )
    bridge.set_on_birth(discovery.on_birth)
    bridge.set_on_message(discovery.on_message)
    return Unit(
        settings=settings,
        bridge=bridge,
        discovery=discovery,
        press=press,
        client_index=client_index,
    )


@asynccontextmanager
async def _installed(tmp_path: Path) -> AsyncIterator[Installed]:
    """Build both units over one broker, each on its own database.

    :param tmp_path: pytest temporary directory.
    """
    broker = FakeBroker()
    async with (
        ConfigStore(tmp_path / "kitchen.db") as kitchen_store,
        ConfigStore(tmp_path / "hall.db") as hall_store,
    ):
        await _seed_button(kitchen_store)
        await _seed_button(hall_store)
        yield Installed(
            broker=broker,
            kitchen=_build_unit(
                broker,
                store=kitchen_store,
                instance_id=KITCHEN_INSTANCE,
                device_name=KITCHEN_NAME,
                client_index=0,
            ),
            hall=_build_unit(
                broker,
                store=hall_store,
                instance_id=HALL_INSTANCE,
                device_name=HALL_NAME,
                client_index=1,
            ),
        )


@pytest_asyncio.fixture
async def installed(tmp_path: Path) -> AsyncIterator[Installed]:
    """Yield two units sharing one broker.

    :param tmp_path: pytest temporary directory.
    """
    async with _installed(tmp_path) as built:
        yield built


async def _start(installed: Installed) -> None:
    """Start both units and accept the broker's CONNACKs, bounded.

    :param installed: the two units under test.
    """
    for unit in installed.units:
        async with asyncio.timeout(TIMEOUT_S):
            await unit.bridge.start()
            await unit.discovery.start()
    for unit in installed.units:
        installed.broker.connect(installed.broker.clients[unit.client_index])
    await wait_until(lambda: all(unit.bridge.connected for unit in installed.units))
    await wait_until(
        lambda: all(
            any(
                event[0] == "publish"
                and event[1] == discovery_topic(unit.settings, SLUG)
                and isinstance(event[2], str)
                and event[2] != ""
                for event in installed.broker.events
            )
            for unit in installed.units
        )
    )


async def test_two_units_on_one_broker_have_distinct_identities(installed: Installed) -> None:
    """Every identifier separates the two units although the slug is the same.

    The instance id, not the device name, is the identity root: two units naming
    the same tile must never share a ``unique_id``, a config topic, a device
    identifier, an availability topic, a client id or a press topic (HSD-vwdy).

    :param installed: the two units under test.
    """
    await _start(installed)
    kitchen = installed.kitchen.settings
    hall = installed.hall.settings

    assert unique_id(kitchen, SLUG) != unique_id(hall, SLUG)
    assert discovery_topic(kitchen, SLUG) != discovery_topic(hall, SLUG)
    assert kitchen.client_id != hall.client_id
    assert kitchen.availability_topic != hall.availability_topic
    assert command_topic(kitchen, SLUG) != command_topic(hall, SLUG)

    # Each unit's payload names its own device and claims only its own identifier.
    for unit in installed.units:
        topic = discovery_topic(unit.settings, SLUG)
        payloads = [
            cast("dict[str, object]", json.loads(event[2]))
            for event in installed.broker.events
            # The HSD-bnb0 blank publishes a non-JSON empty payload to the same
            # topic; only a config payload names a device.
            if event[0] == "publish"
            and event[1] == topic
            and isinstance(event[2], str)
            and event[2] != ""
        ]
        assert payloads, f"no discovery payload was published for {unit.settings.instance_id}"
        payload = payloads[-1]
        device = cast("dict[str, object]", payload["device"])
        assert device["name"] == unit.settings.device_name
        assert device["identifiers"] == [f"chime_deck_{unit.settings.instance_id}"]
        assert payload["unique_id"] == unique_id(unit.settings, SLUG)
        assert payload["availability_topic"] == unit.settings.availability_topic

    # Distinct retained availability topics, each carrying its own online payload.
    assert installed.broker.retained[kitchen.availability_topic] == AVAILABILITY_ONLINE.encode()
    assert installed.broker.retained[hall.availability_topic] == AVAILABILITY_ONLINE.encode()


async def test_a_press_on_one_unit_invokes_only_that_units_handler(installed: Installed) -> None:
    """A press on one unit's command topic must not chime the other unit.

    Both units subscribe to their own command topic only, so the same slug on the
    same broker cannot cross-press them (HSD-vwdy).

    :param installed: the two units under test.
    """
    await _start(installed)
    kitchen = installed.kitchen

    installed.broker.deliver(command_topic(kitchen.settings, SLUG), PRESS_PAYLOAD.encode())

    await wait_until(lambda: kitchen.press.calls == [SLUG])
    assert installed.hall.press.calls == []


async def test_one_unit_going_offline_leaves_the_others_availability_alone(
    installed: Installed,
) -> None:
    """Stopping one unit blanks its availability topic and only that one.

    The retained availability is per instance, so Home Assistant sees the stopped
    unit's device go unavailable rather than either unit disappearing or the other
    one being reported offline (HSD-vwdy).

    :param installed: the two units under test.
    """
    await _start(installed)
    kitchen = installed.kitchen
    hall = installed.hall

    async with asyncio.timeout(TIMEOUT_S):
        await kitchen.bridge.stop()

    await wait_until(
        lambda: (
            installed.broker.retained[kitchen.settings.availability_topic]
            == AVAILABILITY_OFFLINE.encode()
        )
    )
    assert (
        installed.broker.retained[hall.settings.availability_topic] == AVAILABILITY_ONLINE.encode()
    )
    assert hall.bridge.connected is True
    assert hall.press.calls == []
