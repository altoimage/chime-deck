"""Fully offline, deterministic tests for the MQTT bridge.

Everything here is driven through :class:`FakeBroker`, an in-memory broker with
its own topic router. Its :class:`FakeClient` is paho-shaped: it records every
call as an ordered event tuple in one shared log, ``connect``, ``connect_with``
and ``drop`` fire the CONNACK and DISCONNECT callbacks the way paho's network
thread does, and ``publish_from_test`` really routes a message through the
subscriptions the client recorded, so "a publish to ``<prefix>/status`` reaches
the subscription" is proven rather than assumed. The birth jitter uses an
injected sleep and a fixed RNG, so nothing waits on a real timer for it; the
only real-clock waits are the bounded poll helper and one bounded daemon-thread
poll. No broker, socket, network or credential is involved anywhere.

The fakes themselves live in ``tests/mqtt_fakes.py``, because two test modules
need them once HSD-c3si's discovery tests exist and a test module must not be
imported for its private helpers.

Owning ticket: HSD-kslf; the unsubscribe and callback-installation tests were
added by HSD-c3si.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, cast

import paho.mqtt.client as paho_client
import pytest
from log_isolation import preserve_log_state
from mqtt_fakes import (
    WAIT_TIMEOUT_S,
    FakeBroker,
    FakeClient,
    FakeMessage,
    FakeReader,
    FixedRandom,
    GatedSleep,
    NeverCalledFactory,
    RecordingPahoClient,
    RecordingSleep,
    count,
    wait_until,
)
from paho.mqtt.client import ConnectFlags, DisconnectFlags
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from chime_deck.config import REDACTED
from chime_deck.health import HealthRegistry, HealthStatus
from chime_deck.mqtt import (
    AVAILABILITY_OFFLINE,
    AVAILABILITY_ONLINE,
    DEFAULT_DEVICE_NAME,
    DEVICE_NAME,
    MAX_BUFFERED_TOPICS,
    BirthCallback,
    ClientFactory,
    ConnectHandler,
    DisconnectHandler,
    MessageCallback,
    MessageHandler,
    MqttBridge,
    MqttClient,
    MqttConfigurationError,
    MqttSettings,
    default_client_factory,
    differs_beyond_display_name,
    load_mqtt_settings,
)
from chime_deck.store import SETTINGS

pytestmark = pytest.mark.asyncio

HOST: Final = "broker.test"
"""Broker host used by every fake settings object."""

INSTANCE: Final = "default"
"""Instance id used by every fake settings object."""

PASSWORD: Final = "s3cret-password-value"
"""Any occurrence of this string outside a credentials call is a failure."""

LOGGER_NAME: Final = "chime_deck.mqtt.bridge"
"""Logger the bridge writes through."""

AVAILABILITY: Final = "chime_deck/default/availability"
"""Retained availability topic of the default instance."""

STATUS: Final = "homeassistant/status"
"""Home Assistant's own birth and will topic."""

JITTER_S: Final = 0.5
"""Value :class:`FixedRandom` returns, inside the 0-2 s birth jitter window."""

TOPIC_A: Final = "chime_deck/default/button/front-door/set"
TOPIC_B: Final = "chime_deck/default/button/back-door/set"
CUSTOM_TOPIC: Final = "chime_deck/custom/set"


@dataclass
class Harness:
    """The bridge under test plus every seam it was wired with."""

    bridge: MqttBridge
    broker: FakeBroker
    sleeper: RecordingSleep | GatedSleep
    rng: FixedRandom
    health: HealthRegistry

    @property
    def client(self) -> FakeClient:
        """Return the first client the bridge built."""
        return self.broker.clients[0]


def _settings(
    *,
    host: str = HOST,
    port: int = 1883,
    username: str = "",
    password: str = "",
    tls: bool = False,
    discovery_prefix: str = "homeassistant",
    instance_id: str = INSTANCE,
) -> MqttSettings:
    """Build validated settings for one bridge.

    :param host: broker address; blank means "not configured".
    :param port: broker port.
    :param username: broker username.
    :param password: broker password.
    :param tls: whether TLS is enabled.
    :param discovery_prefix: Home Assistant discovery prefix.
    :param instance_id: instance id used in the topics and the client id.
    """
    return MqttSettings(
        host=host,
        port=port,
        username=username,
        password=password,
        tls=tls,
        discovery_prefix=discovery_prefix,
        instance_id=instance_id,
    )


def _bridge(
    broker: FakeBroker,
    *,
    settings: MqttSettings | None = None,
    health: HealthRegistry | None = None,
    on_birth: BirthCallback | None = None,
    on_message: MessageCallback | None = None,
    client_factory: ClientFactory | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    shutdown_timeout_s: float = 1.0,
    seam_timeout_s: float = 1.0,
    publish_flush_timeout_s: float = 0.5,
) -> Harness:
    """Build a bridge wired to *broker* and to a recording sleep and fixed RNG.

    :param broker: the fake broker supplying the client factory.
    :param settings: settings to use; default fake settings when omitted.
    :param health: registry to report through; a fresh one when omitted.
    :param on_birth: republish seam, if the test needs one.
    :param on_message: message seam, if the test needs one.
    :param client_factory: factory override; the broker's factory when omitted.
    :param sleep: jitter sleep override; a :class:`RecordingSleep` when omitted.
    :param shutdown_timeout_s: budget for the paho shutdown.
    :param seam_timeout_s: budget for one seam callback.
    :param publish_flush_timeout_s: budget for the retained ``offline`` flush.
    """
    registry = health if health is not None else HealthRegistry()
    sleeper: RecordingSleep | GatedSleep = (
        RecordingSleep() if sleep is None else cast("RecordingSleep | GatedSleep", sleep)
    )
    rng = FixedRandom(JITTER_S)
    bridge = MqttBridge(
        settings=settings if settings is not None else _settings(),
        health=registry,
        on_birth=on_birth,
        on_message=on_message,
        client_factory=client_factory if client_factory is not None else broker.factory,
        sleep=sleeper,
        rng=rng,
        shutdown_timeout_s=shutdown_timeout_s,
        seam_timeout_s=seam_timeout_s,
        publish_flush_timeout_s=publish_flush_timeout_s,
    )
    return Harness(bridge=bridge, broker=broker, sleeper=sleeper, rng=rng, health=registry)


def _birth_seam(broker: FakeBroker) -> BirthCallback:
    """Build a republish seam that records one ``("birth",)`` event.

    :param broker: broker whose shared event log the seam writes to.
    """

    async def on_birth() -> None:
        broker.events.append(("birth",))

    return on_birth


async def _start_connected(harness: Harness) -> None:
    """Start the bridge, accept its CONNACK and wait for a settled connection.

    :param harness: the bridge under test.
    """
    await harness.bridge.start()
    harness.broker.connect(harness.client)
    await wait_until(lambda: harness.health.get("mqtt") is HealthStatus.CONNECTED)


# ------------------------------------------------------------------ connection


async def test_connect_sequence_is_in_order() -> None:
    """The client is configured, connected and announced in the documented order."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await harness.bridge.start()
    assert broker.events == [
        ("will_set", AVAILABILITY, AVAILABILITY_OFFLINE, 1, True),
        ("reconnect_delay_set", 1, 60),
        ("connect_async", HOST, 1883, 30),
        ("loop_start",),
    ]

    broker.connect(harness.client)
    await wait_until(lambda: harness.health.get("mqtt") is HealthStatus.CONNECTED)
    assert broker.events[4:] == [
        ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True),
        ("subscribe", STATUS, 1),
    ]


async def test_will_is_registered_retained_offline_qos_1() -> None:
    """The last will is registered before ``connect_async`` so a crash marks us offline."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await harness.bridge.start()
    assert ("will_set", AVAILABILITY, AVAILABILITY_OFFLINE, 1, True) in broker.events
    will_index = broker.events.index(("will_set", AVAILABILITY, AVAILABILITY_OFFLINE, 1, True))
    connect_index = broker.events.index(("connect_async", HOST, 1883, 30))
    assert will_index < connect_index


async def test_availability_online_is_published_retained_qos_1() -> None:
    """A successful CONNACK publishes ``online`` retained at QoS 1."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await _start_connected(harness)
    assert ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True) in broker.events
    assert broker.retained[AVAILABILITY] == b"online"


# ------------------------------------------------------------- Home Assistant birth


async def test_status_online_triggers_one_republish_after_jitter() -> None:
    """A single birth message republishes once after the jitter, availability last."""
    broker = FakeBroker()
    harness = _bridge(broker, on_birth=_birth_seam(broker))
    await _start_connected(harness)
    # The connect itself asks for a republish; that one is not under test here.
    await wait_until(lambda: ("birth",) in broker.events)
    assert harness.sleeper.delays == []
    broker.events.clear()

    broker.publish_from_test(STATUS, b"online")
    await wait_until(lambda: ("birth",) in broker.events)
    await wait_until(
        lambda: ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True) in broker.events
    )

    assert count(broker.events, ("birth",)) == 1
    assert harness.sleeper.delays == [JITTER_S]
    assert 0.0 <= harness.sleeper.delays[0] <= 2.0
    availability_index = broker.events.index(
        ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True)
    )
    assert availability_index > broker.events.index(("birth",))


async def test_two_status_online_messages_republish_twice() -> None:
    """Every birth message earns its own republish and its own jitter."""
    broker = FakeBroker()
    harness = _bridge(broker, on_birth=_birth_seam(broker))
    await _start_connected(harness)
    await wait_until(lambda: ("birth",) in broker.events)
    broker.events.clear()
    harness.sleeper.delays.clear()

    broker.publish_from_test(STATUS, b"online")
    await wait_until(lambda: count(broker.events, ("birth",)) == 1)
    broker.publish_from_test(STATUS, b"online")
    await wait_until(lambda: count(broker.events, ("birth",)) == 2)

    assert harness.sleeper.delays == [JITTER_S, JITTER_S]


async def test_status_offline_does_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """Home Assistant going away logs at debug and republishes nothing."""
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    broker = FakeBroker()
    harness = _bridge(broker, on_birth=_birth_seam(broker))
    await _start_connected(harness)
    await wait_until(lambda: ("birth",) in broker.events)
    broker.events.clear()
    harness.sleeper.delays.clear()

    broker.publish_from_test(STATUS, b"offline")
    await wait_until(lambda: any("going away" in record.getMessage() for record in caplog.records))

    assert ("birth",) not in broker.events
    assert harness.sleeper.delays == []


@pytest.mark.parametrize("payload", [b"Online", b" online ", b"", b"ONLINE"])
async def test_status_payload_variants_do_not_republish(
    payload: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    """Only an exact ``b"online"`` is a birth; every other payload is ignored.

    :param payload: the status payload that must not trigger a republish.
    :param caplog: captured debug log proving the message was processed.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    broker = FakeBroker()
    harness = _bridge(broker, on_birth=_birth_seam(broker))
    await _start_connected(harness)
    await wait_until(lambda: ("birth",) in broker.events)
    broker.events.clear()

    broker.publish_from_test(STATUS, payload)
    await wait_until(
        lambda: any("ignoring an unknown status payload" in r.getMessage() for r in caplog.records)
    )

    assert ("birth",) not in broker.events
    assert harness.sleeper.delays == []


async def test_birth_message_racing_a_stop_never_republishes_availability() -> None:
    """A birth message still in its jitter when stop() begins publishes nothing.

    The shutdown already published the retained ``offline``, and the broker does
    not publish the will on a clean disconnect, so a late retained ``online``
    would leave Home Assistant showing a powered-off device as available.
    """
    broker = FakeBroker()
    gated = GatedSleep()
    harness = _bridge(broker, on_birth=_birth_seam(broker), sleep=gated)
    await harness.bridge.start()
    broker.connect(harness.client)
    await wait_until(lambda: ("birth",) in broker.events)
    broker.events.clear()

    broker.publish_from_test(STATUS, b"online")
    # The message is now parked inside the birth jitter, before the seam runs.
    await wait_until(lambda: gated.entered.is_set())

    stop_task = asyncio.ensure_future(harness.bridge.stop())
    # Let stop() set ``_stopping`` and reach its first await before the jitter ends.
    await asyncio.sleep(0)
    gated.release.set()
    await asyncio.wait_for(stop_task, timeout=WAIT_TIMEOUT_S)

    assert ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True) not in broker.events
    assert ("birth",) not in broker.events
    assert count(broker.events, ("publish", AVAILABILITY, AVAILABILITY_OFFLINE, 1, True)) == 1


async def test_birth_message_racing_a_stop_inside_the_seam_never_republishes_availability(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A birth message blocked inside its seam when stop() begins publishes nothing.

    The test above stops during the jitter, so it only exercises the first guard.
    This one lets the jitter finish and parks the seam itself, so the check
    immediately before the availability publish is the one being proven: the
    retained ``online`` must not land after the shutdown's retained ``offline``.

    The log assertion is load-bearing. ``stop()`` clears the client before this
    coroutine resumes, so the missing publish alone cannot tell the guard apart
    from ``_publish_availability`` finding no client; only the guard's own debug
    line proves which of the two it was.

    :param caplog: captured debug log proving the guard, not a missing client, dropped it.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    broker = FakeBroker()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def on_birth() -> None:
        nonlocal calls
        broker.events.append(("birth",))
        calls += 1
        if calls == 1:
            # The connect-time republish returns at once, or start-up would block.
            return
        entered.set()
        await release.wait()

    harness = _bridge(broker, on_birth=on_birth)
    await harness.bridge.start()
    broker.connect(harness.client)
    await wait_until(lambda: ("birth",) in broker.events)
    broker.events.clear()

    broker.publish_from_test(STATUS, b"online")
    # The injected sleep is immediate, so the message reaches the seam at once.
    await wait_until(lambda: entered.is_set())

    stop_task = asyncio.ensure_future(harness.bridge.stop())
    # Let stop() set ``_stopping`` and reach its first await before the seam ends.
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(stop_task, timeout=WAIT_TIMEOUT_S)

    assert ("birth",) in broker.events
    assert ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True) not in broker.events
    assert count(broker.events, ("publish", AVAILABILITY, AVAILABILITY_OFFLINE, 1, True)) == 1
    assert any("before publishing availability" in record.getMessage() for record in caplog.records)


# ----------------------------------------------------------------- message routing


async def test_unknown_topic_is_ignored_without_a_handler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unsubscribed topic is never delivered, and a handler-less delivery is debug.

    The first publish is routed by the broker to nobody, so it is only recorded
    as undelivered. The second one reaches the client (the bridge now subscribes
    to it) and the bridge drops it with a debug line, because no message seam is
    installed.

    :param caplog: captured debug log proving the handler-less message was seen.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    broker = FakeBroker()
    harness = _bridge(broker)
    await _start_connected(harness)

    broker.publish_from_test("someone/else/topic", b"payload")
    assert broker.undelivered == [("someone/else/topic", b"payload")]

    harness.bridge.subscribe("someone/else/topic")
    broker.publish_from_test("someone/else/topic", b"payload")
    await wait_until(
        lambda: any("no handler is installed" in record.getMessage() for record in caplog.records)
    )
    assert broker.undelivered == [("someone/else/topic", b"payload")]


async def test_other_topics_are_forwarded_to_the_message_seam() -> None:
    """A subscribed non-status topic reaches the seam once with a ``bytes`` payload."""
    broker = FakeBroker()
    received: list[tuple[str, bytes]] = []

    async def on_message(topic: str, payload: bytes, retain: bool) -> None:
        received.append((topic, payload))

    harness = _bridge(broker, on_message=on_message)
    await _start_connected(harness)

    harness.bridge.subscribe(CUSTOM_TOPIC, qos=1)
    assert ("subscribe", CUSTOM_TOPIC, 1) in broker.events

    broker.publish_from_test(CUSTOM_TOPIC, bytearray(b"\x00PRESS"))
    await wait_until(lambda: bool(received))

    assert received == [(CUSTOM_TOPIC, b"\x00PRESS")]
    assert isinstance(received[0][0], str)
    assert isinstance(received[0][1], bytes)


async def test_the_broker_retain_flag_reaches_the_message_seam() -> None:
    """A broker replay arrives with ``retain=True``; a live publish with ``False``.

    The flag describes *this delivery*, not the publisher's intent, so the bridge
    has to carry it across the paho-to-event-loop hop: a consumer cannot recover
    it from the topic or the payload, and a replayed selection command must be
    refusable as an old intent (D34).
    """
    broker = FakeBroker()
    received: list[tuple[str, bytes, bool]] = []

    async def on_message(topic: str, payload: bytes, retain: bool) -> None:
        received.append((topic, payload, retain))

    harness = _bridge(broker, on_message=on_message)
    await _start_connected(harness)
    harness.bridge.subscribe(CUSTOM_TOPIC, qos=1)

    broker.deliver(CUSTOM_TOPIC, b"on", retain=True)
    await wait_until(lambda: bool(received))
    broker.deliver(CUSTOM_TOPIC, b"off", retain=False)
    await wait_until(lambda: len(received) == 2)

    assert received == [(CUSTOM_TOPIC, b"on", True), (CUSTOM_TOPIC, b"off", False)]


# -------------------------------------------------------------------------- health


async def test_broker_down_at_startup_does_not_raise() -> None:
    """A broker that never answers leaves the bridge reconnecting, not broken."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await harness.bridge.start()
    assert harness.health.get("mqtt") is HealthStatus.RECONNECTING
    detail = harness.health.message().get("detail") or ""
    assert f"{HOST}:1883" in detail
    assert detail
    assert harness.bridge.connected is False


async def test_connection_recovers_after_a_broker_outage() -> None:
    """A dropped connection reports itself and a fresh CONNACK replays the full setup."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await _start_connected(harness)

    broker.drop(harness.client)
    await wait_until(lambda: harness.health.get("mqtt") is HealthStatus.RECONNECTING)
    assert "lost the connection" in (harness.health.message().get("detail") or "")

    broker.connect(harness.client)
    await wait_until(
        lambda: count(broker.events, ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True)) == 2
    )
    await wait_until(lambda: harness.health.get("mqtt") is HealthStatus.CONNECTED)

    assert count(broker.events, ("subscribe", STATUS, 1)) == 2


async def test_duplicate_on_connect_is_safe() -> None:
    """A second CONNACK without a drop repeats the setup, republish included."""
    broker = FakeBroker()
    harness = _bridge(broker, on_birth=_birth_seam(broker))
    await harness.bridge.start()

    broker.connect(harness.client)
    broker.connect(harness.client)
    await wait_until(lambda: count(broker.events, ("birth",)) == 2)

    assert harness.health.get("mqtt") is HealthStatus.CONNECTED
    assert count(broker.events, ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True)) == 2
    assert count(broker.events, ("subscribe", STATUS, 1)) == 2
    # One republish per CONNACK, which is also why the module docstring says the seam
    # legitimately runs more than once for a single connection.
    assert count(broker.events, ("birth",)) == 2


async def test_a_failing_publish_result_does_not_abort_the_flush_or_the_birth(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A publish result that raises is one poisoned entry, not the end of connect.

    Real paho raises ``ValueError``/``RuntimeError`` from ``is_published()`` when it
    is asked outside its network thread, so a single buffered topic must not stop
    the rest of the flush, the connect health or the discovery republish.

    :param caplog: captured log records proving no bridge task failed.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    broker = FakeBroker()
    harness = _bridge(broker, on_birth=_birth_seam(broker))
    await harness.bridge.start()

    harness.bridge.publish(TOPIC_A, "one")
    harness.bridge.publish(TOPIC_B, "two")
    harness.client.is_published_raises = True

    broker.connect(harness.client)
    await wait_until(lambda: ("birth",) in broker.events)

    assert ("publish", TOPIC_A, "one", 0, False) in broker.events
    assert ("publish", TOPIC_B, "two", 0, False) in broker.events
    assert harness.health.get("mqtt") is HealthStatus.CONNECTED
    assert "an MQTT bridge task failed" not in caplog.text


async def test_a_failing_subscribe_during_replay_does_not_abort_the_connect() -> None:
    """A topic paho refuses to subscribe to is logged, not fatal to the connect.

    The *first* replayed filter is the one that fails, so surviving it — and
    still replaying the rest and republishing — is proven rather than assumed.
    """
    broker = FakeBroker()
    harness = _bridge(broker, on_birth=_birth_seam(broker))
    await harness.bridge.start()

    harness.bridge.subscribe("cmd/topic", qos=1)
    # The status topic is replayed first, so this refuses the very first filter.
    harness.client.subscribe_raises_for = {STATUS}

    broker.connect(harness.client)
    await wait_until(lambda: ("birth",) in broker.events)

    assert ("subscribe", "cmd/topic", 1) in broker.events
    assert count(broker.events, ("birth",)) == 1
    assert harness.health.get("mqtt") is HealthStatus.CONNECTED


# ---------------------------------------------------------------- client identity


async def test_client_id_is_stable_across_restarts() -> None:
    """The client id is derived from the instance id, which also names the topics."""
    broker = FakeBroker()
    first = _bridge(broker, settings=_settings())
    second = _bridge(broker, settings=_settings())
    kitchen = _bridge(broker, settings=_settings(instance_id="kitchen"))

    await first.bridge.start()
    await second.bridge.start()
    await kitchen.bridge.start()

    assert [call["client_id"] for call in broker.factory_calls] == [
        "chime-deck-default",
        "chime-deck-default",
        "chime-deck-kitchen",
    ]
    assert all(call["clean_session"] is True for call in broker.factory_calls)
    assert kitchen.bridge.client_id == "chime-deck-kitchen"
    assert kitchen.bridge.availability_topic == "chime_deck/kitchen/availability"


# ------------------------------------------------------------------------ shutdown


async def test_shutdown_publishes_retained_offline_then_stops() -> None:
    """A clean stop flushes a retained ``offline`` before disconnecting and joining."""
    broker = FakeBroker()
    harness = _bridge(broker, shutdown_timeout_s=1.0, publish_flush_timeout_s=0.25)
    await _start_connected(harness)

    await harness.bridge.stop()

    offline = ("publish", AVAILABILITY, AVAILABILITY_OFFLINE, 1, True)
    assert broker.events[-3:] == [offline, ("disconnect",), ("loop_stop",)]
    assert harness.health.get("mqtt") is HealthStatus.DISABLED
    assert harness.bridge.connected is False
    assert harness.client.publish_results[-1].timeouts == [0.25]


async def test_a_failing_disconnect_still_stops_pahos_thread(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising ``disconnect()`` cannot leave paho's network thread reconnecting.

    ``loop_stop()`` sits in a ``finally`` for exactly this case, and the failure
    is reported by the shutdown worker rather than escaping ``stop()``.

    :param caplog: captured log proving the worker reported the failure.
    """
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    broker = FakeBroker()
    harness = _bridge(broker)
    await _start_connected(harness)
    harness.client.disconnect_raises = True

    await harness.bridge.stop()

    assert ("disconnect",) in broker.events
    assert ("loop_stop",) in broker.events
    assert harness.health.get("mqtt") is HealthStatus.DISABLED
    assert any("shutdown worker failed" in record.getMessage() for record in caplog.records)


async def test_shutdown_is_idempotent() -> None:
    """Stopping twice publishes and disconnects exactly once."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await _start_connected(harness)

    await harness.bridge.stop()
    await harness.bridge.stop()

    offline = ("publish", AVAILABILITY, AVAILABILITY_OFFLINE, 1, True)
    assert count(broker.events, offline) == 1
    assert count(broker.events, ("disconnect",)) == 1
    assert count(broker.events, ("loop_stop",)) == 1


async def test_stop_without_start_is_safe() -> None:
    """A never-started bridge builds nothing and keeps reporting not configured."""
    broker = FakeBroker()
    harness = _bridge(broker)

    await harness.bridge.stop()

    assert broker.factory_calls == []
    assert harness.health.get("mqtt") is HealthStatus.NOT_CONFIGURED


async def test_stop_of_an_unconfigured_bridge_keeps_not_configured() -> None:
    """Stopping an unconfigured bridge never downgrades it to disabled."""
    broker = FakeBroker()
    harness = _bridge(broker, settings=_settings(host=""))

    await harness.bridge.start()
    assert broker.factory_calls == []
    assert harness.health.get("mqtt") is HealthStatus.NOT_CONFIGURED

    await harness.bridge.stop()
    assert harness.health.get("mqtt") is HealthStatus.NOT_CONFIGURED


async def test_second_start_is_a_no_op() -> None:
    """A second start while the first client lives does not build another client."""
    broker = FakeBroker()
    harness = _bridge(broker)

    await harness.bridge.start()
    await harness.bridge.start()

    assert len(broker.factory_calls) == 1


async def test_restart_after_stop_builds_a_fresh_client() -> None:
    """After a stop, a start builds a new client whose CONNACK is honoured."""
    broker = FakeBroker()
    harness = _bridge(broker, shutdown_timeout_s=1.0)
    await _start_connected(harness)

    await harness.bridge.stop()
    await harness.bridge.start()

    assert len(broker.factory_calls) == 2
    fresh = broker.clients[1]
    broker.connect(fresh)
    await wait_until(lambda: harness.health.get("mqtt") is HealthStatus.CONNECTED)
    assert (AVAILABILITY, AVAILABILITY_ONLINE, 1, True) in fresh.published


async def test_shutdown_worker_that_hangs_returns_within_budget(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """A wedged ``loop_stop`` cannot hang the stop: it warns and gives up.

    :param caplog: captured warning log proving the budget expired.
    :param capsys: captured streams proving the abandoned thread printed nothing.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    broker = FakeBroker()
    harness = _bridge(broker, shutdown_timeout_s=0.05)
    await _start_connected(harness)

    gate = threading.Event()
    broker.loop_stop_gate = gate
    try:
        await asyncio.wait_for(harness.bridge.stop(), timeout=WAIT_TIMEOUT_S)
        assert any("did not stop within" in record.getMessage() for record in caplog.records)
        assert harness.health.get("mqtt") is HealthStatus.DISABLED
    finally:
        # Release the daemon thread and wait, bounded, for it to finish.
        gate.set()
        await wait_until(lambda: ("loop_stop",) in broker.events)

    captured = capsys.readouterr()
    assert "Exception in thread" not in captured.out + captured.err


# ------------------------------------------------------------------------ secrets


async def test_password_never_appears_in_reprs_logs_or_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The stored password cannot leak through a repr, a log record or an error.

    :param caplog: captured log records for the whole connect/drop/stop cycle.
    """
    with preserve_log_state(), caplog.at_level(logging.DEBUG):
        details: list[str] = []
        health = HealthRegistry()
        health.set_on_change(lambda: details.append(str(health.message().get("detail"))))
        settings = _settings(username="doorbell", password=PASSWORD)
        broker = FakeBroker()
        harness = _bridge(broker, settings=settings, health=health)
        bridge = harness.bridge

        assert REDACTED in repr(settings)
        assert PASSWORD not in repr(settings)
        assert PASSWORD not in repr(bridge)

        await _start_connected(harness)
        broker.drop(harness.client)
        await wait_until(lambda: harness.health.get("mqtt") is HealthStatus.RECONNECTING)
        await bridge.stop()

        assert PASSWORD not in (health.message().get("detail") or "")
        assert details
        assert all(PASSWORD not in detail for detail in details)
        for record in caplog.records:
            assert PASSWORD not in record.getMessage()
            assert PASSWORD not in (record.exc_text or "")

        values: dict[str, object] = {
            "mqtt.host": HOST,
            "mqtt.port": 1883,
            "mqtt.username": "doorbell",
            "mqtt.password": {"nested": PASSWORD},
            "mqtt.tls": False,
            "mqtt.discovery_prefix": "homeassistant",
            "mqtt.instance_id": INSTANCE,
        }
        with pytest.raises(MqttConfigurationError) as caught:
            await load_mqtt_settings(FakeReader(values))
        assert "mqtt.password" in str(caught.value)
        assert PASSWORD not in str(caught.value)


# --------------------------------------------------------------------- settings


async def test_not_configured_when_host_is_empty() -> None:
    """A blank host reports not configured and never builds a client."""
    broker = FakeBroker()
    factory = NeverCalledFactory()
    harness = _bridge(broker, settings=_settings(host=""), client_factory=factory)

    await harness.bridge.start()

    assert factory.calls == 0
    assert harness.health.get("mqtt") is HealthStatus.NOT_CONFIGURED
    assert "mqtt.host" in (harness.health.message().get("detail") or "")


@pytest.mark.parametrize(
    ("username", "password", "tls"),
    [
        ("", "", False),
        ("doorbell", PASSWORD, False),
        ("", PASSWORD, False),
        ("", "", True),
    ],
)
async def test_credentials_and_tls_only_when_configured(
    username: str, password: str, tls: bool
) -> None:
    """Credentials need a username, and TLS is only enabled when asked for.

    :param username: configured username.
    :param password: configured password.
    :param tls: configured TLS flag.
    """
    broker = FakeBroker()
    harness = _bridge(broker, settings=_settings(username=username, password=password, tls=tls))
    await harness.bridge.start()

    if username:
        assert ("username_pw_set", username, password or None) in broker.events
    else:
        assert not any(event[0] == "username_pw_set" for event in broker.events)
    if tls:
        assert ("tls_set",) in broker.events
    else:
        assert ("tls_set",) not in broker.events


# ------------------------------------------------------------------ buffered state


async def test_publish_while_disconnected_coalesces_latest_state() -> None:
    """The buffer keeps the first desire order and the latest payload per topic."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await harness.bridge.start()
    bridge = harness.bridge

    bridge.publish(TOPIC_A, "one")
    bridge.publish(TOPIC_B, "two")
    bridge.publish(TOPIC_B, "latest")

    broker.connect(harness.client)
    await wait_until(lambda: harness.health.get("mqtt") is HealthStatus.CONNECTED)
    await wait_until(lambda: ("publish", TOPIC_B, "latest", 0, False) in broker.events)

    assert broker.events[4:] == [
        ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True),
        ("subscribe", STATUS, 1),
        ("publish", TOPIC_A, "one", 0, False),
        ("publish", TOPIC_B, "latest", 0, False),
    ]

    broker.drop(harness.client)
    await wait_until(lambda: harness.bridge.connected is False)
    broker.connect(harness.client)
    await wait_until(
        lambda: count(broker.events, ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True)) == 2
    )

    # An empty buffer, proven the only way the public API allows: no stale replay.
    assert count(broker.events, ("publish", TOPIC_A, "one", 0, False)) == 1
    assert count(broker.events, ("publish", TOPIC_B, "latest", 0, False)) == 1


async def test_buffer_eviction_drops_the_oldest_topic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full buffer evicts the oldest topic with a warning and flushes the rest.

    :param caplog: captured warning naming the evicted topic.
    """
    broker = FakeBroker()
    harness = _bridge(broker)
    await harness.bridge.start()

    for index in range(MAX_BUFFERED_TOPICS + 1):
        harness.bridge.publish(f"buffer/{index}", "PRESS")

    assert "buffer/0" in caplog.text

    broker.connect(harness.client)
    await wait_until(
        lambda: (
            len(
                [
                    event
                    for event in broker.events
                    if event[0] == "publish"
                    and isinstance(event[1], str)
                    and event[1].startswith("buffer/")
                ]
            )
            == MAX_BUFFERED_TOPICS
        )
    )
    flushed = [
        event for event in broker.events if event[0] == "publish" and event[1] != AVAILABILITY
    ]
    assert len(flushed) == MAX_BUFFERED_TOPICS
    assert not any(event[1] == "buffer/0" for event in flushed)


async def test_subscribe_while_disconnected_is_replayed_on_connect() -> None:
    """A subscription made before the CONNACK is recorded and replayed with the status."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await harness.bridge.start()

    harness.bridge.subscribe("cmd/topic", qos=1)

    assert not any(event[0] == "subscribe" for event in broker.events)
    assert harness.bridge.subscriptions["cmd/topic"] == 1

    broker.connect(harness.client)
    await wait_until(lambda: count(broker.events, ("subscribe", "cmd/topic", 1)) == 1)
    assert ("subscribe", STATUS, 1) in broker.events


# -------------------------------------------------------- subscription removal


async def test_unsubscribe_pops_the_recorded_topic_and_calls_the_client() -> None:
    """An unsubscribe reaches the broker and leaves nothing to replay."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await _start_connected(harness)
    harness.bridge.subscribe(TOPIC_A, qos=1)
    assert ("subscribe", TOPIC_A, 1) in broker.events

    harness.bridge.unsubscribe(TOPIC_A)

    assert TOPIC_A not in harness.bridge.subscriptions
    assert ("unsubscribe", TOPIC_A) in broker.events
    assert TOPIC_A not in harness.client.subscriptions


async def test_unsubscribing_an_unknown_topic_is_a_quiet_no_op() -> None:
    """A filter the bridge never recorded is neither popped nor sent to the broker."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await _start_connected(harness)
    before = dict(harness.bridge.subscriptions)

    harness.bridge.unsubscribe(TOPIC_A)

    assert not any(event[0] == "unsubscribe" for event in broker.events)
    assert dict(harness.bridge.subscriptions) == before


async def test_unsubscribe_refuses_the_status_topic() -> None:
    """The bridge owns the Home Assistant status subscription and keeps it."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await _start_connected(harness)

    harness.bridge.unsubscribe(STATUS)

    assert STATUS in harness.bridge.subscriptions
    assert STATUS in harness.client.subscriptions
    assert not any(event[0] == "unsubscribe" for event in broker.events)


async def test_an_unsubscribed_topic_is_not_replayed_after_a_reconnect() -> None:
    """A popped filter is not re-SUBSCRIBEd by the next clean-session replay."""
    broker = FakeBroker()
    harness = _bridge(broker, on_birth=_birth_seam(broker))
    await harness.bridge.start()
    harness.bridge.subscribe(TOPIC_A)

    broker.connect(harness.client)
    await wait_until(lambda: ("birth",) in broker.events)
    assert count(broker.events, ("subscribe", TOPIC_A, 0)) == 1

    harness.bridge.unsubscribe(TOPIC_A)
    broker.drop(harness.client)
    await wait_until(lambda: harness.bridge.connected is False)
    broker.connect(harness.client)
    # The replay happens on the loop, and the republish is what follows it.
    await wait_until(lambda: count(broker.events, ("birth",)) == 2)

    assert count(broker.events, ("subscribe", TOPIC_A, 0)) == 1
    assert count(broker.events, ("subscribe", STATUS, 1)) == 2


async def test_a_raising_client_unsubscribe_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A client that refuses the unsubscribe is a warning, not an escaping error.

    :param caplog: captured warning naming the topic the client refused.
    """
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    broker = FakeBroker()
    harness = _bridge(broker)
    await _start_connected(harness)
    harness.bridge.subscribe(TOPIC_A)
    harness.client.unsubscribe_raises = True

    harness.bridge.unsubscribe(TOPIC_A)

    assert any("unsubscribing from" in record.getMessage() for record in caplog.records)
    # The pop happened before the client call, so the leak is closed even here.
    assert TOPIC_A not in harness.bridge.subscriptions


# ---------------------------------------------------- callbacks after construction


async def test_a_birth_callback_installed_after_start_is_used_by_the_first_connect() -> None:
    """A seam installed between ``start()`` and the CONNACK still republishes."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await harness.bridge.start()

    harness.bridge.set_on_birth(_birth_seam(broker))

    broker.connect(harness.client)
    await wait_until(lambda: ("birth",) in broker.events)

    assert harness.health.get("mqtt") is HealthStatus.CONNECTED


async def test_a_message_callback_installed_after_start_is_used() -> None:
    """A message seam installed after ``start()`` receives the next delivery."""
    broker = FakeBroker()
    harness = _bridge(broker)
    received: list[tuple[str, bytes]] = []

    async def on_message(topic: str, payload: bytes, retain: bool) -> None:
        received.append((topic, payload))

    await _start_connected(harness)
    harness.bridge.subscribe(CUSTOM_TOPIC, qos=1)
    harness.bridge.set_on_message(on_message)

    broker.publish_from_test(CUSTOM_TOPIC, b"\x00PRESS")
    await wait_until(lambda: bool(received))

    assert received == [(CUSTOM_TOPIC, b"\x00PRESS")]


async def test_setting_a_callback_while_stopping_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A callback installed during the shutdown cannot be resurrected by a restart.

    :param caplog: captured debug log proving the post-restart message was dropped.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    broker = FakeBroker()
    harness = _bridge(broker, on_birth=_birth_seam(broker))
    await _start_connected(harness)
    await wait_until(lambda: ("birth",) in broker.events)

    await harness.bridge.stop()

    async def seam_b() -> None:
        broker.events.append(("birth-b",))

    async def message_seam_b(topic: str, payload: bytes, retain: bool) -> None:
        broker.events.append(("message-b", topic, payload))

    harness.bridge.set_on_birth(seam_b)
    harness.bridge.set_on_message(message_seam_b)

    await harness.bridge.start()
    broker.connect(broker.clients[1])
    await wait_until(lambda: count(broker.events, ("birth",)) == 2)

    harness.bridge.subscribe(CUSTOM_TOPIC)
    broker.publish_from_test(CUSTOM_TOPIC, b"PRESS")
    await wait_until(
        lambda: any("no handler is installed" in record.getMessage() for record in caplog.records)
    )

    assert count(broker.events, ("birth",)) == 2
    assert not any(event[0] == "birth-b" for event in broker.events)
    assert not any(event[0] == "message-b" for event in broker.events)


# ------------------------------------------------------- callbacks after shutdown


async def test_callbacks_after_stop_are_dropped_silently() -> None:
    """Callbacks arriving after stop are dropped without a warning."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await _start_connected(harness)
    await harness.bridge.stop()
    client = harness.client

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        broker.connect(client)
        broker.drop(client)
        broker.deliver(STATUS, b"online")

    assert harness.health.get("mqtt") is HealthStatus.DISABLED


async def test_callbacks_after_the_loop_is_closed_are_dropped() -> None:
    """Callbacks fired after the owning loop is gone neither raise nor warn."""
    broker = FakeBroker()
    harness = _bridge(broker)
    failures: list[Exception] = []

    async def scenario() -> None:
        await _start_connected(harness)
        await harness.bridge.stop()

    def run() -> None:
        try:
            asyncio.run(scenario())
        except Exception as exc:  # reported by the assertion below
            failures.append(exc)

    thread = threading.Thread(target=run, name="mqtt-bridge-loop", daemon=True)
    thread.start()
    await wait_until(lambda: not thread.is_alive())
    assert failures == []

    client = harness.client
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client.on_connect(
            client, None, ConnectFlags(False), ReasonCode(PacketTypes.CONNACK, "Success"), None
        )
        client.on_disconnect(
            client,
            None,
            DisconnectFlags(False),
            ReasonCode(PacketTypes.DISCONNECT, "Unspecified error"),
            None,
        )
        client.on_message(client, None, FakeMessage(STATUS, b"online"))

    messages = [str(warning.message) for warning in caught]
    assert not any("never awaited" in message for message in messages)
    assert not any("Event loop is closed" in message for message in messages)


async def test_callbacks_after_the_loop_closes_without_a_stop_are_dropped() -> None:
    """A crashed loop (no ``stop``) still makes every later callback a silent no-op.

    ``stop`` clears the captured loop, so the closed-loop branch of ``_submit`` is
    only reached when the loop dies without a shutdown — exactly the crash case
    this bridge has to survive without the ``Event loop is closed`` noise that
    ``filterwarnings = ["error"]`` turns into a failure.
    """
    broker = FakeBroker()
    harness = _bridge(broker)
    failures: list[Exception] = []

    async def scenario() -> None:
        await _start_connected(harness)

    def run() -> None:
        try:
            asyncio.run(scenario())
        except Exception as exc:  # reported by the assertion below
            failures.append(exc)

    thread = threading.Thread(target=run, name="mqtt-bridge-crash", daemon=True)
    thread.start()
    await wait_until(lambda: not thread.is_alive())
    assert failures == []

    client = harness.client
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client.on_connect(
            client, None, ConnectFlags(False), ReasonCode(PacketTypes.CONNACK, "Success"), None
        )
        client.on_message(client, None, FakeMessage(STATUS, b"online"))

    messages = [str(warning.message) for warning in caught]
    assert not any("never awaited" in message for message in messages)
    assert not any("Event loop is closed" in message for message in messages)


async def test_callback_from_a_worker_thread_hops_to_the_loop() -> None:
    """A CONNACK arriving on another thread still reaches the bridge's loop."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await harness.bridge.start()
    client = harness.client

    thread = threading.Thread(target=broker.connect, args=(client,), daemon=True)
    thread.start()
    await wait_until(lambda: harness.health.get("mqtt") is HealthStatus.CONNECTED)
    await wait_until(lambda: not thread.is_alive())

    assert ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True) in broker.events
    assert ("subscribe", STATUS, 1) in broker.events


# ------------------------------------------------------------------ seam failures


async def test_birth_seam_failure_is_swallowed(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """A raising republish seam warns and leaves the connection healthy.

    :param caplog: captured warning naming the republish callback.
    :param capsys: captured streams proving no task exception leaked.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    async def on_birth() -> None:
        raise RuntimeError("boom")

    broker = FakeBroker()
    harness = _bridge(broker, on_birth=on_birth)
    await _start_connected(harness)
    await wait_until(lambda: any("republish callback" in r.getMessage() for r in caplog.records))

    assert harness.health.get("mqtt") is HealthStatus.CONNECTED
    captured = capsys.readouterr()
    assert "Task exception was never retrieved" not in caplog.text
    assert "Task exception was never retrieved" not in captured.out + captured.err


async def test_birth_seam_timeout_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    """A republish seam that hangs is cancelled and warned about, not awaited for ever.

    :param caplog: captured warning naming the expired seam budget.
    """
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    async def on_birth() -> None:
        await asyncio.sleep(60)

    broker = FakeBroker()
    harness = _bridge(broker, on_birth=on_birth, seam_timeout_s=0.05)
    await _start_connected(harness)
    await wait_until(
        lambda: any("did not finish within" in record.getMessage() for record in caplog.records)
    )

    assert harness.health.get("mqtt") is HealthStatus.CONNECTED


# ---------------------------------------------------------------- failure reasons


async def test_start_failure_sets_error_without_raising() -> None:
    """A factory that cannot build a client is an error status, not an exception."""

    class ExplodingFactory:
        """A client factory that fails the way a paho-less runtime would."""

        def __call__(
            self,
            *,
            client_id: str,
            clean_session: bool,
            on_connect: ConnectHandler,
            on_disconnect: DisconnectHandler,
            on_message: MessageHandler,
        ) -> MqttClient:
            """Fail every attempt to build a client.

            :param client_id: client id the bridge asked for.
            :param clean_session: clean-session flag the bridge asked for.
            :param on_connect: handler for a CONNACK.
            :param on_disconnect: handler for a disconnect.
            :param on_message: handler for every delivered message.
            """
            raise RuntimeError("no paho here")

    broker = FakeBroker()
    harness = _bridge(broker, client_factory=ExplodingFactory())
    await harness.bridge.start()

    assert harness.health.get("mqtt") is HealthStatus.ERROR
    assert "no paho here" in (harness.health.message().get("detail") or "")
    assert harness.bridge.connected is False

    # A bridge that never built a client keeps the actionable error on stop.
    await harness.bridge.stop()
    assert harness.health.get("mqtt") is HealthStatus.ERROR


async def test_auth_failure_connack_sets_error() -> None:
    """A rejected login names the credential settings and does no setup work."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await harness.bridge.start()

    broker.connect_with(harness.client, "Bad user name or password")
    await wait_until(lambda: harness.health.get("mqtt") is HealthStatus.ERROR)

    detail = harness.health.message().get("detail") or ""
    assert "mqtt.username" in detail
    assert "mqtt.password" in detail
    assert ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True) not in broker.events
    assert ("subscribe", STATUS, 1) not in broker.events


async def test_a_bridge_with_credentials_connects_to_a_broker_that_requires_them() -> None:
    """A bridge that presents the broker's credentials is accepted and set up."""
    broker = FakeBroker()
    broker.required_credentials = ("doorbell", "hunter2")
    harness = _bridge(broker, settings=_settings(username="doorbell", password="hunter2"))
    await harness.bridge.start()

    broker.connect(harness.client)
    await wait_until(lambda: harness.health.get("mqtt") is HealthStatus.CONNECTED)

    assert ("username_pw_set", "doorbell", "hunter2") in broker.events


async def test_an_anonymous_bridge_is_rejected_by_a_broker_that_requires_credentials() -> None:
    """An anonymous bridge sees the broker's CONNACK refusal and does no setup work.

    The refusal is fired through ``broker.connect`` (the CONNACK the fake broker
    sends on its own), so this proves the real path rather than a manual
    ``connect_with`` that bypasses the credential check.
    """
    broker = FakeBroker()
    broker.required_credentials = ("doorbell", "hunter2")
    harness = _bridge(broker)
    await harness.bridge.start()

    broker.connect(harness.client)
    await wait_until(lambda: harness.health.get("mqtt") is HealthStatus.ERROR)

    detail = harness.health.message().get("detail") or ""
    assert "rejected the credentials" in detail
    assert "mqtt.username" in detail
    assert "mqtt.password" in detail
    assert ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True) not in broker.events
    assert ("subscribe", STATUS, 1) not in broker.events


async def test_permanent_failure_connack_sets_error() -> None:
    """A rejected client id names the instance setting and publishes nothing."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await harness.bridge.start()

    broker.connect_with(harness.client, "Client identifier not valid")
    await wait_until(lambda: harness.health.get("mqtt") is HealthStatus.ERROR)

    assert "mqtt.instance_id" in (harness.health.message().get("detail") or "")
    assert ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True) not in broker.events


async def test_other_failure_connack_sets_reconnecting() -> None:
    """A transient refusal keeps paho retrying and reports it as reconnecting."""
    broker = FakeBroker()
    harness = _bridge(broker)
    await harness.bridge.start()

    broker.connect_with(harness.client, "Server unavailable")
    await wait_until(
        lambda: "refused the connection" in (harness.health.message().get("detail") or "")
    )

    assert harness.health.get("mqtt") is HealthStatus.RECONNECTING
    assert "retrying" in (harness.health.message().get("detail") or "")
    assert ("publish", AVAILABILITY, AVAILABILITY_ONLINE, 1, True) not in broker.events


# ------------------------------------------------------------ default paho factory


async def test_default_client_factory_uses_callback_api_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real factory builds a callback-API-v2 client and installs all handlers.

    :param monkeypatch: swaps paho's ``Client`` for a recording stand-in.
    """
    monkeypatch.setattr(paho_client, "Client", RecordingPahoClient)
    calls: list[str] = []

    def on_connect(*_args: object) -> None:
        calls.append("connect")

    def on_disconnect(*_args: object) -> None:
        calls.append("disconnect")

    def on_message(*_args: object) -> None:
        calls.append("message")

    built = cast(
        "RecordingPahoClient",
        default_client_factory(
            client_id="chime-deck-default",
            clean_session=True,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
            on_message=on_message,
        ),
    )

    assert built.args == (CallbackAPIVersion.VERSION2,)
    assert built.kwargs == {"client_id": "chime-deck-default", "clean_session": True}
    assert "reconnect_on_failure" not in built.kwargs
    assert built.on_connect is on_connect
    assert built.on_disconnect is on_disconnect
    assert built.on_message is on_message
    assert calls == []


# ------------------------------------------------------------------- settings I/O


async def test_load_mqtt_settings_reads_every_key() -> None:
    """Every stored ``mqtt.*`` setting and the device name are read and used."""
    reader = FakeReader(
        {
            "mqtt.host": HOST,
            "mqtt.port": 8883,
            "mqtt.username": "doorbell",
            "mqtt.password": PASSWORD,
            "mqtt.tls": True,
            "mqtt.discovery_prefix": "ha2",
            "mqtt.instance_id": "kitchen",
            "device.name": "Hall Doorbell",
        }
    )

    settings = await load_mqtt_settings(reader)

    assert settings.host == HOST
    assert settings.port == 8883
    assert settings.username == "doorbell"
    assert settings.password == PASSWORD
    assert settings.tls is True
    assert settings.discovery_prefix == "ha2"
    assert settings.instance_id == "kitchen"
    # Asserted explicitly, not via the fake's registry fallback: this is the one
    # key that is not an ``mqtt.*`` setting (HSD-vwdy).
    assert settings.device_name == "Hall Doorbell"
    assert settings.client_id == "chime-deck-kitchen"
    assert settings.availability_topic == "chime_deck/kitchen/availability"
    assert settings.status_topic == "ha2/status"


async def test_load_mqtt_settings_defaults_the_device_name_from_the_registry() -> None:
    """A store that never wrote ``device.name`` yields the registry default.

    This is the fresh-install case of HSD-vwdy: ``FakeReader`` answers the
    registry default exactly as ``ConfigStore`` does.
    """
    reader = FakeReader({"mqtt.host": HOST})

    settings = await load_mqtt_settings(reader)

    assert settings.device_name == SETTINGS["device.name"].default
    assert "device.name" in reader.keys


# ---------------------------------------------------------------- device name


async def test_the_default_device_name_cannot_drift_from_the_registry_or_discovery() -> None:
    """The three copies of "ChimeDeck" are pinned to each other.

    The settings registry may not import the bridge (import-cycle rule), so the
    value is necessarily duplicated; this is the test that stops a silent drift
    renaming the Home Assistant device for every unnamed unit (HSD-vwdy).
    """
    assert DEFAULT_DEVICE_NAME == "ChimeDeck"
    assert SETTINGS["device.name"].default == DEFAULT_DEVICE_NAME
    assert DEVICE_NAME == DEFAULT_DEVICE_NAME
    assert MqttSettings(host=HOST).device_name == DEFAULT_DEVICE_NAME


async def test_a_custom_device_name_is_kept_stripped_and_repr_d_safely() -> None:
    """A configured name is stripped once and readable in the repr."""
    settings = MqttSettings(host=HOST, device_name="  Hall Doorbell  ")

    assert settings.device_name == "Hall Doorbell"
    assert "Hall Doorbell" in repr(settings)
    assert PASSWORD not in repr(MqttSettings(host=HOST, password=PASSWORD))


@pytest.mark.parametrize("value", ["", "   ", "n" * 65])
async def test_an_unusable_device_name_is_refused_naming_the_key(value: str) -> None:
    """Blank and over-long names are refused with an actionable message.

    :param value: unusable device name.
    """
    with pytest.raises(MqttConfigurationError) as caught:
        MqttSettings(host=HOST, device_name=value)

    assert "device.name" in str(caught.value)


async def test_an_unconfigured_broker_skips_the_device_name_check() -> None:
    """A blank host accepts everything else unvalidated, as before ``device.name``.

    "Not configured" must never be blocked by an unrelated bad value.
    """
    settings = MqttSettings(host="", device_name="")

    assert settings.device_name == ""
    assert settings.is_configured is False


async def test_a_display_name_change_alone_is_not_an_identity_difference() -> None:
    """Renaming the device must not retract the published entities (HSD-vwdy)."""
    running = MqttSettings(host=HOST, instance_id=INSTANCE, device_name="ChimeDeck")
    renamed = MqttSettings(host=HOST, instance_id=INSTANCE, device_name="Hall Doorbell")

    assert differs_beyond_display_name(running, renamed) is False
    # Both sides substitute the same default, so even two custom names agree.
    assert (
        differs_beyond_display_name(renamed, MqttSettings(host=HOST, instance_id=INSTANCE)) is False
    )


@pytest.mark.parametrize(
    ("host", "discovery_prefix", "instance_id"),
    [
        (HOST, "homeassistant", "kitchen"),
        ("other.test", "homeassistant", INSTANCE),
        (HOST, "ha2", INSTANCE),
    ],
)
async def test_every_other_setting_change_is_an_identity_difference(
    host: str, discovery_prefix: str, instance_id: str
) -> None:
    """A topic, prefix, credential or identity change always keeps the retraction.

    :param host: broker host of the compared settings.
    :param discovery_prefix: discovery prefix of the compared settings.
    :param instance_id: instance id of the compared settings.
    """
    running = MqttSettings(host=HOST)
    changed = MqttSettings(host=host, discovery_prefix=discovery_prefix, instance_id=instance_id)

    assert differs_beyond_display_name(running, changed) is True


async def test_a_rename_that_also_moves_the_identity_still_retracts() -> None:
    """A name change bundled with an identity change keeps today's retraction."""
    running = MqttSettings(host=HOST, instance_id="default")
    moved = MqttSettings(host=HOST, instance_id="hall", device_name="Hall Doorbell")

    assert differs_beyond_display_name(running, moved) is True


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("mqtt.port", "1883"),
        ("mqtt.port", True),
        ("mqtt.tls", "yes"),
        ("mqtt.discovery_prefix", "homeassistant/#"),
        ("mqtt.discovery_prefix", ""),
        ("mqtt.instance_id", "kitchen/1"),
        ("mqtt.instance_id", "k" * 65),
    ],
)
async def test_load_mqtt_settings_rejects_unusable_values(key: str, value: object) -> None:
    """A stored value that cannot be used names its key and never repeats a secret.

    :param key: the setting that holds an unusable value.
    :param value: the stored value itself.
    """
    values: dict[str, object] = {
        "mqtt.host": HOST,
        "mqtt.port": 1883,
        "mqtt.username": "doorbell",
        "mqtt.password": PASSWORD,
        "mqtt.tls": False,
        "mqtt.discovery_prefix": "homeassistant",
        "mqtt.instance_id": INSTANCE,
    }
    values[key] = value

    with pytest.raises(MqttConfigurationError) as caught:
        await load_mqtt_settings(FakeReader(values))

    message = str(caught.value)
    assert key in message
    assert PASSWORD not in message


async def test_status_topic_follows_the_discovery_prefix() -> None:
    """The status subscription and birth handling follow ``mqtt.discovery_prefix``."""
    broker = FakeBroker()
    harness = _bridge(
        broker, settings=_settings(discovery_prefix="ha2"), on_birth=_birth_seam(broker)
    )
    assert harness.bridge.status_topic == "ha2/status"

    await _start_connected(harness)
    assert ("subscribe", "ha2/status", 1) in broker.events
    await wait_until(lambda: ("birth",) in broker.events)
    broker.events.clear()

    # The default prefix is not subscribed, so that status message is routed to nobody.
    broker.publish_from_test(STATUS, b"online")
    assert broker.undelivered == [(STATUS, b"online")]
    assert ("birth",) not in broker.events

    broker.publish_from_test("ha2/status", b"online")
    await wait_until(lambda: count(broker.events, ("birth",)) == 1)
    assert harness.sleeper.delays == [JITTER_S]
