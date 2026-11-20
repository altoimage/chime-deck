"""Paho-shaped in-memory fakes shared by the MQTT test modules.

:class:`FakeBroker` is an in-memory broker with a topic router of its own, and
its :class:`FakeClient` is paho-shaped: it records every call as an ordered event
tuple in one shared log, ``connect``, ``connect_with`` and ``drop`` fire the
CONNACK and DISCONNECT callbacks the way paho's network thread does, and
``publish_from_test`` really routes a message through the subscriptions the
client recorded, so "a publish to ``<prefix>/status`` reaches the subscription"
is proven rather than assumed. The birth jitter is driven by
:class:`RecordingSleep`/:class:`GatedSleep` and :class:`FixedRandom`, so no test
waits on a real timer for it; the only real-clock waits are the bounded
:func:`wait_until` poll and one bounded daemon-thread poll.

Everything here is fully offline: no socket, no broker, no network and no
credential is involved anywhere, and every wait is bounded.

It lives in a module of its own because two test modules need the same fakes —
``tests/test_mqtt_bridge.py`` and the discovery tests HSD-c3si adds — and one
test module importing private names out of another is worse than sharing them
here.

Owning ticket: HSD-kslf; extracted for HSD-c3si.
"""

from __future__ import annotations

import asyncio
import random
import threading
from collections.abc import Callable
from typing import Final, cast

from paho.mqtt.client import ConnectFlags, DisconnectFlags
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from chime_deck.mqtt import (
    ConnectHandler,
    DisconnectHandler,
    MessageHandler,
    MqttClient,
)
from chime_deck.store.settings_registry import SETTINGS

__all__ = [
    "GATE_TIMEOUT_S",
    "WAIT_TIMEOUT_S",
    "FakeBroker",
    "FakeClient",
    "FakeMessage",
    "FakePublishResult",
    "FakeReader",
    "FixedRandom",
    "GatedSleep",
    "NeverCalledFactory",
    "RecordingPahoClient",
    "RecordingSleep",
    "as_bytes",
    "count",
    "wait_until",
]

GATE_TIMEOUT_S: Final = 10.0
"""Upper bound for the fake client's deliberately wedged ``loop_stop``."""

WAIT_TIMEOUT_S: Final = 5.0
"""Upper bound for every helper poll; no test sleeps indefinitely."""

_POLL_INTERVAL_S: Final = 0.005
"""Seconds between predicate checks inside :func:`wait_until`."""


async def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = WAIT_TIMEOUT_S,
) -> None:
    """Poll *predicate* until it is true, bounded by *timeout*.

    :param predicate: condition to wait for.
    :param timeout: seconds after which the test fails.
    :raises AssertionError: if the condition never became true.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
    raise AssertionError("the condition did not become true before the deadline")


def as_bytes(payload: str | bytes | None) -> bytes:
    """Return *payload* the way a broker would store it.

    :param payload: whatever a caller published; ``None`` is the empty payload.
    """
    if payload is None:
        return b""
    if isinstance(payload, str):
        return payload.encode()
    return payload


def count(events: list[tuple[object, ...]], event: tuple[object, ...]) -> int:
    """Return how often *event* occurs in the shared ordered event log.

    :param events: the broker's ordered event log.
    :param event: the exact event tuple to count.
    """
    return events.count(event)


class FakeMessage:
    """An incoming message that satisfies the :class:`MqttMessage` port."""

    def __init__(self, topic: str, payload: bytes, *, qos: int = 0, retain: bool = False) -> None:
        """Store one delivered message.

        :param topic: topic the message arrived on.
        :param payload: payload bytes as paho would hand them over.
        :param qos: delivery guarantee the broker used.
        :param retain: whether the message was retained.
        """
        self.topic = topic
        self.payload = payload
        self.qos = qos
        self.retain = retain


class FakePublishResult:
    """A publish result that records the flush timeout it was given."""

    def __init__(self, *, published: bool = True, raises: bool = False) -> None:
        """Create a result for a message paho accepted or queued.

        :param published: whether the message is already on the wire.
        :param raises: whether ``is_published()`` fails the way real paho does.
        """
        self.published = published
        self.raises = raises
        self.timeouts: list[float | None] = []

    def wait_for_publish(self, timeout: float | None = None) -> None:
        """Record the timeout instead of waiting for an acknowledgement.

        :param timeout: seconds allowed; ``None`` would wait for ever.
        """
        self.timeouts.append(timeout)

    def is_published(self) -> bool:
        """Return whether the message is on the wire, or fail as real paho can.

        :raises ValueError: when *raises* was set, mirroring paho's own refusal to
            answer outside its network thread.
        """
        if self.raises:
            raise ValueError("queue full")
        return self.published


class FakeClient:
    """A paho-shaped client that records every call into the broker's event log."""

    connect_timeout: float = 5.0
    """Settable like paho's own property; the settings probe lowers it per attempt."""

    _connack_pending: bool = False
    """Whether ``connect`` asked for a CONNACK that ``loop`` has not delivered yet."""

    def __init__(
        self,
        broker: FakeBroker,
        *,
        client_id: str,
        clean_session: bool,
        on_connect: ConnectHandler,
        on_disconnect: DisconnectHandler,
        on_message: MessageHandler,
    ) -> None:
        """Create an unconnected client that records into *broker*.

        :param broker: the fake broker that owns the shared event log.
        :param client_id: client id the factory was asked for.
        :param clean_session: clean-session flag the factory was asked for.
        :param on_connect: handler paho would call after a CONNACK.
        :param on_disconnect: handler paho would call after a disconnect.
        :param on_message: handler paho would call for every delivered message.
        """
        self.broker = broker
        self.client_id = client_id
        self.clean_session = clean_session
        self.credentials: tuple[str, str | None] | None = None
        """Credentials handed to paho, or ``None`` when the client stayed anonymous."""
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_message = on_message
        self.connected = False
        self.disconnected = False
        self.subscriptions: dict[str, int] = {}
        self.published: list[tuple[str, str | bytes | None, int, bool]] = []
        self.publish_results: list[FakePublishResult] = []
        self.is_published_raises = False
        self.subscribe_raises_for: set[str] = set()
        self.publish_raises_for: set[str] = set()
        self.disconnect_raises = False
        self.unsubscribe_raises = False

    def _record(self, *event: object) -> None:
        """Append one ordered event to the broker's shared log.

        :param event: the event tuple, starting with the method name.
        """
        self.broker.events.append(event)

    def username_pw_set(self, username: str, password: str | None = None) -> None:
        """Record the credentials handed to paho.

        :param username: username from the settings.
        :param password: password from the settings, or ``None``.
        """
        self._record("username_pw_set", username, password)
        self.credentials = (username, password)

    def tls_set(self) -> None:
        """Record that TLS was enabled."""
        self._record("tls_set")

    def will_set(
        self,
        topic: str,
        payload: str | bytes | None = None,
        qos: int = 0,
        retain: bool = False,
    ) -> None:
        """Record the last-will registration.

        :param topic: availability topic the will is published to.
        :param payload: will payload.
        :param qos: will delivery guarantee.
        :param retain: whether the will is retained.
        """
        self._record("will_set", topic, payload, qos, retain)

    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 120) -> None:
        """Record the reconnect backoff bounds.

        :param min_delay: first reconnect delay.
        :param max_delay: upper bound for the delay.
        """
        self._record("reconnect_delay_set", min_delay, max_delay)

    def connect(self, host: str, port: int = 1883, keepalive: int = 60) -> None:
        """Record the blocking connect and queue the CONNACK ``loop`` will fire.

        :param host: broker host.
        :param port: broker port.
        :param keepalive: seconds between pings.
        :raises BaseException: the broker's ``connect_raises``, when a test set one.
        """
        self._record("connect", host, port, keepalive)
        if self.broker.connect_raises is not None:
            raise self.broker.connect_raises
        self._connack_pending = True

    def loop(self, timeout: float = 1.0) -> None:
        """Record one network-loop iteration and deliver the queued CONNACK.

        :param timeout: seconds a real paho call may block; unused here.
        """
        self._record("loop")
        if not self._connack_pending or self.broker.connack_never:
            return
        self._connack_pending = False
        self.broker._fire_connect(self, self.broker.connack_name_for(self))

    def connect_async(self, host: str, port: int = 1883, keepalive: int = 60) -> None:
        """Record the non-blocking connect attempt.

        :param host: broker host.
        :param port: broker port.
        :param keepalive: seconds between pings.
        """
        self._record("connect_async", host, port, keepalive)

    def loop_start(self) -> None:
        """Record that paho's network thread was started."""
        self._record("loop_start")

    def loop_stop(self) -> None:
        """Record that paho's network thread was stopped, or block on the test gate."""
        gate = self.broker.loop_stop_gate
        if gate is not None:
            # Bounded on purpose: a wedged loop_stop must not hang the suite.
            gate.wait(GATE_TIMEOUT_S)
        self._record("loop_stop")

    def disconnect(self) -> None:
        """Record the clean disconnect and mark the client disconnected.

        :raises RuntimeError: when *disconnect_raises* was set, so a test can prove
            that the ``loop_stop`` in the shutdown worker's ``finally`` still runs.
        """
        self._record("disconnect")
        self.connected = False
        self.disconnected = True
        if self.disconnect_raises:
            raise RuntimeError("disconnect failed")

    def subscribe(self, topic: str, qos: int = 0) -> None:
        """Record a subscription and remember it for the topic router.

        :param topic: topic filter being subscribed to.
        :param qos: requested delivery guarantee.
        :raises ValueError: if *topic* is registered as one paho rejects.
        """
        self._record("subscribe", topic, qos)
        if topic in self.subscribe_raises_for:
            raise ValueError("bad filter")
        self.subscriptions[topic] = qos

    def unsubscribe(self, topic: str) -> None:
        """Record an unsubscribe and drop it from the topic router.

        The record and the pop both happen before the optional failure, so a test
        can prove that the bridge logs a raising ``unsubscribe`` instead of
        letting it escape.

        :param topic: topic filter being unsubscribed from.
        :raises ValueError: when *unsubscribe_raises* was set, the way real paho
            refuses a bad filter.
        """
        self._record("unsubscribe", topic)
        self.subscriptions.pop(topic, None)
        if self.unsubscribe_raises:
            raise ValueError("bad filter")

    def publish(
        self,
        topic: str,
        payload: str | bytes | None = None,
        qos: int = 0,
        retain: bool = False,
    ) -> FakePublishResult:
        """Record one publish and return a result shaped like paho's.

        A topic registered in *publish_raises_for* raises before anything is
        recorded: neither the shared event log nor ``published`` sees the failed
        call, so a test can prove that a broker-side failure leaves no trace in
        the fake itself.

        :param topic: topic to publish to.
        :param payload: payload to publish.
        :param qos: delivery guarantee.
        :param retain: whether the broker keeps the payload.
        :raises ValueError: when *topic* is registered as one paho refuses.
        """
        if topic in self.publish_raises_for:
            raise ValueError("publish failed")
        self._record("publish", topic, payload, qos, retain)
        self.published.append((topic, payload, qos, retain))
        if retain:
            self.broker.retained[topic] = as_bytes(payload)
        result = FakePublishResult(published=self.connected, raises=self.is_published_raises)
        self.publish_results.append(result)
        return result


class FakeBroker:
    """An in-memory broker: shared event log, client factory and topic router."""

    def __init__(self) -> None:
        """Create a broker with no clients and an empty retained store."""
        self.events: list[tuple[object, ...]] = []
        self.clients: list[FakeClient] = []
        self.factory_calls: list[dict[str, object]] = []
        self.retained: dict[str, bytes] = {}
        self.undelivered: list[tuple[str, bytes]] = []
        self.loop_stop_gate: threading.Event | None = None
        self.connect_raises: BaseException | None = None
        """Exception a ``FakeClient.connect`` raises instead of connecting."""
        self.connack_name: str = "Success"
        """paho reason-code name the CONNACK fired by ``loop`` carries."""
        self.connack_never: bool = False
        """Whether ``loop`` must never fire the queued CONNACK."""
        self.required_credentials: tuple[str, str | None] | None = None
        """When set, a client that did not present exactly these credentials is refused.

        This is what makes "a broker that rejects anonymous clients" testable: the
        refusal travels the real path — the CONNACK the broker fires — rather than
        being asserted about a recorded method call. ``None`` (the default) accepts
        every client, exactly as before.
        """

    def factory(
        self,
        *,
        client_id: str,
        clean_session: bool,
        on_connect: ConnectHandler,
        on_disconnect: DisconnectHandler,
        on_message: MessageHandler,
    ) -> FakeClient:
        """Build one fake client, recording the keyword arguments it was given.

        :param client_id: stable client id, derived from the instance id.
        :param clean_session: always ``True`` from the bridge.
        :param on_connect: handler for a CONNACK.
        :param on_disconnect: handler for a disconnect.
        :param on_message: handler for every delivered message.
        """
        self.factory_calls.append({"client_id": client_id, "clean_session": clean_session})
        client = FakeClient(
            self,
            client_id=client_id,
            clean_session=clean_session,
            on_connect=on_connect,
            on_disconnect=on_disconnect,
            on_message=on_message,
        )
        self.clients.append(client)
        return client

    def connect(self, client: FakeClient) -> None:
        """Mark *client* connected and fire a successful CONNACK.

        :param client: client that just connected.
        """
        self._fire_connect(client, self.connack_name_for(client))

    def connack_name_for(self, client: FakeClient) -> str:
        """Return the CONNACK name *client* earns on this broker.

        A broker with :attr:`required_credentials` set refuses a client that did
        not present them, so the refusal is the CONNACK the client really sees.

        :param client: client asking for a CONNACK.
        """
        if (
            self.required_credentials is not None
            and client.credentials != self.required_credentials
        ):
            return "Bad user name or password"
        return self.connack_name

    def connect_with(self, client: FakeClient, name: str) -> None:
        """Mark *client* connected and fire a CONNACK with the named reason code.

        :param client: client that received the CONNACK.
        :param name: paho reason-code name, e.g. ``Bad user name or password``.
        """
        self._fire_connect(client, name)

    def _fire_connect(self, client: FakeClient, name: str) -> None:
        """Fire the connect callback the way paho's network thread would.

        :param client: client whose callback is fired.
        :param name: paho reason-code name for the CONNACK.
        """
        client.connected = True
        code = ReasonCode(PacketTypes.CONNACK, name)
        client.on_connect(client, None, ConnectFlags(False), code, None)

    def drop(self, client: FakeClient, name: str = "Unspecified error") -> None:
        """Mark *client* disconnected and fire the disconnect callback.

        :param client: client that lost its connection.
        :param name: paho reason-code name for the DISCONNECT.
        """
        client.connected = False
        code = ReasonCode(PacketTypes.DISCONNECT, name)
        client.on_disconnect(client, None, DisconnectFlags(False), code, None)

    def deliver(self, topic: str, payload: bytes | bytearray) -> None:
        """Route one message through every matching subscription.

        A ``bytearray`` payload is passed through untouched so a test can prove
        the bridge normalises it; only the ``undelivered`` record is normalised.

        :param topic: topic the message was published to.
        :param payload: payload bytes.
        """
        delivered = False
        for client in self.clients:
            if not client.connected:
                continue
            if not any(self._topic_matches(pattern, topic) for pattern in client.subscriptions):
                continue
            delivered = True
            client.on_message(client, None, FakeMessage(topic, cast("bytes", payload)))
        if not delivered:
            self.undelivered.append((topic, bytes(payload)))

    def publish_from_test(self, topic: str, payload: bytes | bytearray) -> None:
        """Publish as another client on the broker: retained store plus routing.

        :param topic: topic to publish to.
        :param payload: payload bytes.
        """
        self.retained[topic] = bytes(payload)
        self.deliver(topic, payload)

    @staticmethod
    def _topic_matches(pattern: str, topic: str) -> bool:
        """Return whether one subscription *pattern* selects *topic*.

        Exact levels match themselves, ``+`` matches exactly one level and ``#``
        matches every remaining level.

        :param pattern: subscription topic filter, possibly containing wildcards.
        :param topic: concrete topic a message was published to.
        """
        levels = pattern.split("/")
        topic_levels = topic.split("/")
        for index, level in enumerate(levels):
            if level == "#":
                return True
            if index >= len(topic_levels):
                return False
            if level != "+" and level != topic_levels[index]:
                return False
        return len(levels) == len(topic_levels)


class RecordingPahoClient:
    """Stand-in for ``paho.mqtt.client.Client`` that records its construction."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Record the positional and keyword arguments paho was built with.

        :param args: positional arguments, the callback API version first.
        :param kwargs: keyword arguments, the client id and flags.
        """
        self.args = args
        self.kwargs = kwargs
        self.on_connect: object = None
        self.on_disconnect: object = None
        self.on_message: object = None


class FixedRandom(random.Random):
    """A ``random.Random`` whose ``uniform`` always returns the same value."""

    def __init__(self, value: float) -> None:
        """Seed nothing and remember the fixed value.

        :param value: the value ``uniform`` returns.
        """
        super().__init__()
        self._value = value

    def uniform(self, a: float, b: float) -> float:
        """Return the fixed value instead of a sample.

        :param a: lower bound the bridge asks for; unused.
        :param b: upper bound the bridge asks for; unused.
        """
        return self._value


class RecordingSleep:
    """An injected ``sleep`` that records every delay and returns at once."""

    def __init__(self) -> None:
        """Create a sleep that has recorded no delay yet."""
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        """Record one requested delay instead of waiting for it.

        :param delay: jitter the bridge asked for.
        """
        self.delays.append(delay)


class GatedSleep:
    """An injected ``sleep`` that parks the bridge until the test releases it.

    ``entered`` proves the bridge is inside the jitter sleep, and ``release`` lets
    the test decide exactly when that sleep finishes — so a test can send ``stop``
    through the seam the sleep would otherwise hide.
    """

    def __init__(self) -> None:
        """Create a sleep that has not been entered and is not released."""
        self.delays: list[float] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        """Record the delay, signal *entered* and wait for *release*.

        :param delay: jitter the bridge asked for.
        """
        self.delays.append(delay)
        self.entered.set()
        await self.release.wait()


class FakeReader:
    """A settings store that answers from a dictionary."""

    def __init__(self, values: dict[str, str | int | bool | object]) -> None:
        """Store the values to answer with.

        :param values: every ``mqtt.*`` setting, keyed by setting name.
        """
        self.values = values
        self.keys: list[str] = []

    async def get_setting(self, key: str, default: object = None) -> object:
        """Return the stored value of *key*, its registry default, or *default*.

        The shape mirrors ``ConfigStore.get_setting`` exactly: a stored value wins,
        then the registry default when *default* is ``None``, then *default*. The
        registry fallback is what makes the fake faithful now that
        ``load_mqtt_settings`` reads a key (``device.name``) a test's mapping rarely
        carries: the real store answers its default, so the fake must too. Every
        key read is still recorded so a test can assert which settings were
        consulted.

        :param key: registered setting key.
        :param default: value returned when *key* has neither a stored value nor a
            registry default.
        """
        self.keys.append(key)
        if key in self.values:
            return self.values[key]
        spec = SETTINGS.get(key)
        if spec is not None and default is None:
            return spec.default
        return default


class NeverCalledFactory:
    """A client factory that fails the test if the bridge calls it."""

    def __init__(self) -> None:
        """Create a factory that has not been called yet."""
        self.calls = 0

    def __call__(
        self,
        *,
        client_id: str,
        clean_session: bool,
        on_connect: ConnectHandler,
        on_disconnect: DisconnectHandler,
        on_message: MessageHandler,
    ) -> MqttClient:
        """Fail: an unconfigured bridge must not build a client.

        :param client_id: client id the bridge asked for.
        :param clean_session: clean-session flag the bridge asked for.
        :param on_connect: handler for a CONNACK.
        :param on_disconnect: handler for a disconnect.
        :param on_message: handler for every delivered message.
        """
        self.calls += 1
        raise AssertionError(f"the factory must not be called for {client_id=} {clean_session=}")
