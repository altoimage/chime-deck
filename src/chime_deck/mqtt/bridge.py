"""The MQTT bridge: one paho connection, its availability topic and its health.

This module owns the *connection* only: the client id, the credentials and TLS
settings, the last-will message, the retained availability topic, the
subscription to the Home Assistant status topic, the health status, the bridge
between paho's network thread and the asyncio event loop, and the reconnect
behaviour. The *content* sent over that connection belongs elsewhere: the
full-republish seam (``on_birth``) is what HSD-c3si fills with the discovery
payloads, and command-topic routing is HSD-dtpo's job.

**Never block a callback.** paho calls ``on_connect``, ``on_disconnect`` and
``on_message`` on its own network thread. None of those callbacks may call
``future.result()``, ``asyncio.run()``, ``Thread.join()`` or ``loop_stop()``:
a blocking callback stalls paho's network loop, and a blocking ``loop_stop()``
silently skips the join. Every callback here only builds a coroutine and hands
it to the event loop through :meth:`MqttBridge._submit`.

**Why the birth seam can fire twice per connection.** Home Assistant publishes
its status topic *retained*, so after a reconnect the broker redelivers the
retained ``online`` in addition to the fresh connect callback. The republish
seam therefore legitimately runs about twice per connection, which is harmless
because design 02 §5 requires discovery republishing to be idempotent. It is
written down here so the next reader does not file it as a bug.

**The subscription seam.** :meth:`MqttBridge.subscribe` and
:meth:`MqttBridge.unsubscribe` are the only supported way to change the topic
filters, because the recorded topics are what the bridge replays on every
connect. :meth:`MqttBridge.set_on_birth` and
:meth:`MqttBridge.set_on_message` install the content callbacks after
construction, which is how the discovery service and the bridge are wired to
each other.

Owning ticket: HSD-kslf; the unsubscribe and callback-installation seams were
added by HSD-c3si.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import random
import re
import threading
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from concurrent.futures import InvalidStateError
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Protocol, Self, cast

from ..config import REDACTED, SECRET_SCRUBBER
from ..health import HealthRegistry, HealthStatus
from .errors import MqttConfigurationError

if TYPE_CHECKING:
    from paho.mqtt.client import Client, ConnectFlags, DisconnectFlags, MQTTMessage
    from paho.mqtt.enums import CallbackAPIVersion
    from paho.mqtt.properties import Properties
    from paho.mqtt.reasoncodes import ReasonCode

__all__ = [
    "AVAILABILITY_OFFLINE",
    "AVAILABILITY_ONLINE",
    "AVAILABILITY_TOPIC_TEMPLATE",
    "CLIENT_ID_PREFIX",
    "DEFAULT_BIRTH_JITTER_S",
    "DEFAULT_DEVICE_NAME",
    "DEFAULT_KEEPALIVE_S",
    "DEFAULT_PUBLISH_FLUSH_TIMEOUT_S",
    "DEFAULT_RECONNECT_MAX_DELAY_S",
    "DEFAULT_RECONNECT_MIN_DELAY_S",
    "DEFAULT_SEAM_TIMEOUT_S",
    "DEFAULT_SHUTDOWN_TIMEOUT_S",
    "DEFAULT_STATUS_QOS",
    "MAX_BUFFERED_TOPICS",
    "BirthCallback",
    "ClientFactory",
    "ConnectHandler",
    "DisconnectHandler",
    "MessageCallback",
    "MessageHandler",
    "MqttBridge",
    "MqttClient",
    "MqttMessage",
    "MqttSettings",
    "MqttSettingsReader",
    "PublishResult",
    "default_client_factory",
    "differs_beyond_display_name",
    "load_mqtt_settings",
]

_LOGGER = logging.getLogger(__name__)

CLIENT_ID_PREFIX: Final = "chime-deck-"
"""Prefix of the MQTT client id; the instance id is appended to it."""

AVAILABILITY_ONLINE: Final = "online"
"""Availability payload published on connect."""

AVAILABILITY_OFFLINE: Final = "offline"
"""Availability payload published as the last will and on a clean shutdown."""

AVAILABILITY_TOPIC_TEMPLATE: Final = "chime_deck/{instance}/availability"
"""Availability topic; the instance id is substituted for ``{instance}``."""

DEFAULT_KEEPALIVE_S: Final = 30
"""Seconds between MQTT pings; paho drops the connection after 1.5 times this."""

DEFAULT_RECONNECT_MIN_DELAY_S: Final = 1
"""First reconnect delay handed to paho's ``reconnect_delay_set``."""

DEFAULT_RECONNECT_MAX_DELAY_S: Final = 60
"""Upper bound for the reconnect delay handed to paho."""

DEFAULT_BIRTH_JITTER_S: Final = 2.0
"""Longest random delay before republishing, so a fleet does not answer at once."""

DEFAULT_STATUS_QOS: Final = 1
"""QoS of the availability topic and of the Home Assistant status subscription."""

DEFAULT_SHUTDOWN_TIMEOUT_S: Final = 5.0
"""Seconds allowed for the retained ``offline`` publish, ``disconnect`` and ``loop_stop``."""

DEFAULT_PUBLISH_FLUSH_TIMEOUT_S: Final = 1.0
"""Seconds allowed for the retained ``offline`` publish to reach paho's outgoing queue."""

DEFAULT_SEAM_TIMEOUT_S: Final = 10.0
"""Seconds allowed for one ``on_birth`` or ``on_message`` callback to finish."""

MAX_BUFFERED_TOPICS: Final = 256
"""Topics whose latest state is buffered while the broker is unreachable."""

_SHUTDOWN_POLL_S: Final = 0.01
"""Seconds between checks of the shutdown future; small so tests do not crawl."""

_DETAIL_CLIP: Final = 200
"""Maximum characters of broker text echoed into a health detail."""

_AUTH_FAILURE_CODES: Final[tuple[int, ...]] = (0x86, 0x87)
"""CONNACK/connect reason codes for a bad username or password."""

_PERMANENT_FAILURE_CODES: Final[tuple[int, ...]] = (0x84, 0x85)
"""Reason codes that retrying cannot fix: unsupported version and client id rejected."""

_INSTANCE_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
"""Instance ids go into the topic tree and the client id, so only safe characters."""

_MAX_INSTANCE_ID_LENGTH: Final = 64
"""Longest accepted ``mqtt.instance_id``, matching the settings registry limit."""

DEFAULT_DEVICE_NAME: Final = "ChimeDeck"
"""The device name used when ``device.name`` was never written.

``SETTINGS["device.name"].default`` and
:data:`chime_deck.mqtt.discovery.DEVICE_NAME` hold the same value, and a test pins
all three together: the settings registry must not import this module (the
registry is the store's leaf), so the value is necessarily duplicated, and a
silent drift would rename the Home Assistant device for every unnamed unit.
Same anti-drift pattern as :data:`chime_deck.mqtt.discovery.MQTT_PRESS_SOURCE`.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class MqttSettings:
    """The ``mqtt.*`` settings and the device name a bridge needs, validated once.

    An unconfigured broker is a legal, fully supported state: a blank ``host``
    is accepted and everything else is left unvalidated, because "not
    configured" must never be blocked by an unrelated bad port.

    ``device_name`` is the one field that is not an ``mqtt.*`` setting. It carries
    no identity — see :func:`differs_beyond_display_name` — and is here because
    the discovery ``device`` block is built from these settings.
    """

    host: str
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    discovery_prefix: str = "homeassistant"
    instance_id: str = "default"
    device_name: str = DEFAULT_DEVICE_NAME

    def __post_init__(self) -> None:
        """Validate and normalise the settings.

        :raises MqttConfigurationError: if a configured setting cannot be used.
        """
        if not self.host.strip():
            return
        if isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise MqttConfigurationError(
                "mqtt.port must be a whole number between 1 and 65535 — set it to the port "
                "the broker listens on, normally 1883 or 8883"
            )
        prefix = self.discovery_prefix.strip().rstrip("/")
        if not prefix:
            raise MqttConfigurationError(
                "mqtt.discovery_prefix must not be empty — set it to the discovery prefix "
                "Home Assistant uses, normally homeassistant"
            )
        if "+" in prefix or "#" in prefix:
            raise MqttConfigurationError(
                "mqtt.discovery_prefix must not contain the MQTT wildcards + or # — use a "
                "plain topic prefix such as homeassistant"
            )
        if prefix.startswith("/"):
            raise MqttConfigurationError(
                "mqtt.discovery_prefix must not start with / — use a relative topic prefix "
                "such as homeassistant"
            )
        object.__setattr__(self, "discovery_prefix", prefix)
        if len(self.instance_id) > _MAX_INSTANCE_ID_LENGTH:
            raise MqttConfigurationError(
                "mqtt.instance_id must be at most 64 characters — shorten it in Settings"
            )
        if _INSTANCE_ID_PATTERN.fullmatch(self.instance_id) is None:
            raise MqttConfigurationError(
                "mqtt.instance_id must start with a letter or digit and contain only "
                "letters, digits, dots, underscores and dashes — it is used in the MQTT "
                "topic tree and the client id, so /, + and # are not allowed"
            )
        device_name = self.device_name.strip()
        if not device_name:
            raise MqttConfigurationError(
                "device.name must not be empty — give this unit a name in Settings; it is "
                "the name the Home Assistant device page shows"
            )
        if len(device_name) > _MAX_INSTANCE_ID_LENGTH:
            raise MqttConfigurationError(
                "device.name must be at most 64 characters — shorten it in Settings"
            )
        object.__setattr__(self, "device_name", device_name)

    def __repr__(self) -> str:
        """Return a representation that cannot contain the password."""
        return (
            f"MqttSettings(host={self.host!r}, port={self.port!r}, username={self.username!r}, "
            f"password={REDACTED!r}, tls={self.tls!r}, "
            f"discovery_prefix={self.discovery_prefix!r}, instance_id={self.instance_id!r}, "
            f"device_name={self.device_name!r})"
        )

    @property
    def client_id(self) -> str:
        """Return the stable MQTT client id for this instance."""
        return f"{CLIENT_ID_PREFIX}{self.instance_id}"

    @property
    def availability_topic(self) -> str:
        """Return the retained availability topic for this instance."""
        return AVAILABILITY_TOPIC_TEMPLATE.format(instance=self.instance_id)

    @property
    def status_topic(self) -> str:
        """Return Home Assistant's own status topic."""
        return f"{self.discovery_prefix}/status"

    @property
    def is_configured(self) -> bool:
        """Return whether a broker address has been stored."""
        return bool(self.host.strip())


class MqttSettingsReader(Protocol):
    """The narrow settings port satisfied structurally by ``ConfigStore``.

    Declared here rather than imported from :mod:`chime_deck.store` so the
    bridge depends on a shape instead of on the store. That does not make the
    import graph free, and saying it did would be a lie: ``mqtt.bridge`` imports
    :mod:`chime_deck.config` for :data:`~chime_deck.config.REDACTED` and
    the secret scrubber, and ``config`` imports the store package at module level
    for ``SECRET_SETTING_KEYS`` and ``default_db_path``, so ``aiosqlite`` *is*
    importable through this package. What this package keeps avoiding is a runtime
    dependency on the store's models and bus: those stay ``TYPE_CHECKING``-only,
    so describing or testing a bridge never needs a database.
    """

    async def get_setting(self, key: str) -> object:
        """Return the stored value of *key*, or its registry default.

        :param key: registered setting key.
        """
        ...


def _stored_str(value: object, key: str) -> str:
    """Return one stored setting as stripped text.

    :param value: whatever the settings store returned.
    :param key: setting key, named in the error message.
    :raises MqttConfigurationError: if the value is not text.
    """
    if not isinstance(value, str):
        raise MqttConfigurationError(
            f"{key} must be stored as text — fix it in Settings; the stored value is never "
            "shown here because it may be a secret"
        )
    return value.strip()


def _stored_int(value: object, key: str) -> int:
    """Return one stored setting as a whole number.

    ``True`` is rejected on purpose: ``bool`` is an ``int`` subclass, and a
    stored flag in the port setting would silently mean port 1.

    :param value: whatever the settings store returned.
    :param key: setting key, named in the error message.
    :raises MqttConfigurationError: if the value is not a whole number.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise MqttConfigurationError(
            f"{key} must be stored as a whole number — fix it in Settings; the stored value "
            "is never shown here because it may be a secret"
        )
    return value


def _stored_bool(value: object, key: str) -> bool:
    """Return one stored setting as a flag.

    :param value: whatever the settings store returned.
    :param key: setting key, named in the error message.
    :raises MqttConfigurationError: if the value is not a flag.
    """
    if not isinstance(value, bool):
        raise MqttConfigurationError(
            f"{key} must be stored as on or off — fix it in Settings; the stored value is "
            "never shown here because it may be a secret"
        )
    return value


async def load_mqtt_settings(reader: MqttSettingsReader) -> MqttSettings:
    """Read every ``mqtt.*`` setting and the device name, and build the validated settings.

    The values are type-checked here rather than trusted, because a database
    written by an older build can hold anything. No error message repeats a
    stored value: ``mqtt.password`` must not appear in a log line even when the
    password itself is the setting of the wrong type.

    :param reader: the settings store.
    :return: the validated settings.
    :raises MqttConfigurationError: if a stored value cannot be used.
    """
    host = _stored_str(await reader.get_setting("mqtt.host"), "mqtt.host")
    port = _stored_int(await reader.get_setting("mqtt.port"), "mqtt.port")
    username = _stored_str(await reader.get_setting("mqtt.username"), "mqtt.username")
    password = _stored_str(await reader.get_setting("mqtt.password"), "mqtt.password")
    tls = _stored_bool(await reader.get_setting("mqtt.tls"), "mqtt.tls")
    discovery_prefix = _stored_str(
        await reader.get_setting("mqtt.discovery_prefix"), "mqtt.discovery_prefix"
    )
    instance_id = _stored_str(await reader.get_setting("mqtt.instance_id"), "mqtt.instance_id")
    device_name = _stored_str(await reader.get_setting("device.name"), "device.name")
    return MqttSettings(
        host=host,
        port=port,
        username=username,
        password=password,
        tls=tls,
        discovery_prefix=discovery_prefix,
        instance_id=instance_id,
        device_name=device_name,
    )


def differs_beyond_display_name(a: MqttSettings, b: MqttSettings) -> bool:
    """Return whether two settings differ in anything other than the device name.

    The display name reaches only the discovery ``device`` block's ``name`` and the
    browser title: it appears in no topic, no ``unique_id``, no device identifier
    and not in the client id, so a rename alone must not retract the published
    entities — that would delete and re-create entities whose identity never
    changed, splitting history and breaking automations (HSD-vwdy).

    The comparison substitutes the default name into both sides with
    :func:`dataclasses.replace` so the set of identity-bearing fields is never
    re-typed here: a field added to :class:`MqttSettings` is compared
    automatically, which is what keeps this helper from silently going stale.

    :param a: the settings of the running generation.
    :param b: the settings just read from the store.
    """
    return replace(a, device_name=DEFAULT_DEVICE_NAME) != replace(
        b, device_name=DEFAULT_DEVICE_NAME
    )


class MqttMessage(Protocol):
    """The narrow incoming-message port the bridge needs from paho."""

    topic: str
    payload: bytes
    qos: int
    retain: bool


class PublishResult(Protocol):
    """The narrow publish-result port the bridge needs from paho."""

    def wait_for_publish(self, timeout: float | None = None) -> None:
        """Block until the message is acknowledged, or the timeout expires.

        :param timeout: seconds allowed; ``None`` waits for ever, which the
            bridge never asks for.
        """
        ...


class MqttClient(Protocol):
    """The narrow paho client port this package uses.

    Every mutating method declares ``-> object`` on purpose: paho returns
    ``MQTTErrorCode``, ``None`` or a tuple from these calls, and a narrower
    declared return type would stop a real :class:`paho.mqtt.client.Client` from
    matching this protocol under mypy strict.
    """

    connect_timeout: float
    """Seconds the synchronous ``connect`` may spend on DNS, TCP and TLS.

    A settable float property on paho's client (5.0 by default); the settings
    probe lowers it to what is left of its own budget.
    """

    def username_pw_set(self, username: str, password: str | None = None) -> object:
        """Set the credentials used at CONNECT."""
        ...

    def tls_set(self) -> object:
        """Enable TLS with the system trust store."""
        ...

    def will_set(
        self,
        topic: str,
        payload: str | bytes | None = None,
        qos: int = 0,
        retain: bool = False,
    ) -> object:
        """Register the last-will message."""
        ...

    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 120) -> object:
        """Set the reconnect backoff bounds."""
        ...

    def connect_async(self, host: str, port: int = 1883, keepalive: int = 60) -> object:
        """Start connecting without blocking the caller."""
        ...

    def connect(self, host: str, port: int = 1883, keepalive: int = 60) -> object:
        """Connect synchronously: DNS, TCP, TLS and the CONNECT packet.

        paho reports a failure here by raising, and a CONNACK is only processed
        later by :meth:`loop`, which is exactly what the settings probe needs.
        """
        ...

    def loop(self, timeout: float = 1.0) -> object:
        """Run one iteration of paho's network loop, processing a CONNACK.

        :param timeout: seconds this call may block waiting for the socket.
        """
        ...

    def loop_start(self) -> object:
        """Start paho's network thread."""
        ...

    def loop_stop(self) -> object:
        """Stop paho's network thread and join it."""
        ...

    def disconnect(self) -> object:
        """Send DISCONNECT and close the socket."""
        ...

    def subscribe(self, topic: str, qos: int = 0) -> object:
        """Subscribe to one topic."""
        ...

    def unsubscribe(self, topic: str) -> object:
        """Remove one topic filter, so the broker stops routing it."""
        ...

    def publish(
        self,
        topic: str,
        payload: str | bytes | None = None,
        qos: int = 0,
        retain: bool = False,
    ) -> PublishResult:
        """Queue one message for publication."""
        ...


# The first two parameters are typed ``object`` so paho's own handlers (which pass
# its ``Client`` and ``Any``) are accepted without a cast: a callable that takes
# ``object`` is assignable wherever a callable taking ``Client`` is expected.
ConnectHandler = Callable[
    ["object", "object", "ConnectFlags", "ReasonCode", "Properties | None"], None
]
DisconnectHandler = Callable[
    ["object", "object", "DisconnectFlags", "ReasonCode", "Properties | None"], None
]
MessageHandler = Callable[["object", "object", "MqttMessage"], None]

BirthCallback = Callable[[], Awaitable[None]]
"""Called whenever the discovery payloads should be republished (HSD-c3si fills this)."""

MessageCallback = Callable[[str, bytes, bool], Awaitable[None]]
"""Called with every message on a topic this bridge subscribed to (HSD-dtpo fills this).

The third argument is the broker's **RETAIN flag for that delivery**, not the
publisher's intent: ``True`` means the broker is replaying a message it stored,
which a consumer needs to know because a replayed command is a replay of an *old*
intent (the tile was asked to turn on maybe days ago), not a live request. The
subscription cannot see that distinction any other way — the flag lives on the
message, and :class:`chime_deck.mqtt.bridge.MqttMessage` already carries it.
"""


class ClientFactory(Protocol):
    """The callable that builds one client, satisfied by :func:`default_client_factory`.

    The handlers are passed *to the factory* rather than assigned through
    :class:`MqttClient`: paho declares its callback attributes as
    ``Optional[...]`` properties, and assigning through a Protocol would fight
    mypy strict for no benefit.
    """

    def __call__(
        self,
        *,
        client_id: str,
        clean_session: bool,
        on_connect: ConnectHandler,
        on_disconnect: DisconnectHandler,
        on_message: MessageHandler,
    ) -> MqttClient:
        """Build a client with the three handlers already installed.

        :param client_id: stable client id, derived from the instance id.
        :param clean_session: always ``True``: a reconnect drops broker state.
        :param on_connect: connect callback, called on paho's network thread.
        :param on_disconnect: disconnect callback, called on paho's network thread.
        :param on_message: message callback, called on paho's network thread.
        """
        ...


def default_client_factory(
    *,
    client_id: str,
    clean_session: bool,
    on_connect: ConnectHandler,
    on_disconnect: DisconnectHandler,
    on_message: MessageHandler,
) -> MqttClient:
    """Build a real :class:`paho.mqtt.client.Client` with callback API v2.

    paho is imported *inside* this function so importing the package — which the
    settings UI and the test suite both do — never pays for paho's module import,
    and a runtime without paho can still describe a bridge.

    ``clean_session`` is always passed explicitly by the caller, and
    ``reconnect_on_failure`` is deliberately left alone: paho's default ``True``
    is what makes ``connect_async`` recover on its own after a broker restart.

    :param client_id: stable client id, derived from the instance id.
    :param clean_session: always ``True``: a reconnect drops broker state.
    :param on_connect: connect callback, called on paho's network thread.
    :param on_disconnect: disconnect callback, called on paho's network thread.
    :param on_message: message callback, called on paho's network thread.
    :return: the new client, not yet connected.
    """
    from paho.mqtt.client import Client
    from paho.mqtt.enums import CallbackAPIVersion

    # Callback API v2 is the paho 2.x API: the v1 handlers take a different
    # number of arguments and would not match the handler aliases above.
    version: CallbackAPIVersion = CallbackAPIVersion.VERSION2
    client: Client = Client(version, client_id=client_id, clean_session=clean_session)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    # One narrow cast: paho declares ``MQTTMessage.payload`` as ``bytes | bytearray``,
    # which is wider than the ``bytes`` our port promises, so mypy refuses the plain
    # assignment even though the handler accepts everything paho passes it.
    client.on_message = cast("Callable[[Client, Any, MQTTMessage], None]", on_message)
    return client


def _require_positive(name: str, value: float) -> float:
    """Check that a timeout or backoff bound is usable.

    :param name: constructor argument name, named in the message.
    :param value: value to check.
    :return: the same value, for chaining.
    :raises MqttConfigurationError: if *value* is not greater than zero.
    """
    if value <= 0:
        raise MqttConfigurationError(f"{name} must be greater than zero; got {value:g}")
    return value


def _payload_bytes(payload: object) -> bytes:
    """Normalise one MQTT payload to bytes without clipping it.

    :param payload: whatever the client delivered.
    :return: the payload as bytes; ``None`` becomes the empty payload.
    """
    if payload is None:
        return b""
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8", errors="replace")
    if isinstance(payload, bool):
        # bool is an int subclass; keep the text form a user would expect.
        return b"1" if payload else b"0"
    if isinstance(payload, (int, float)):
        return str(payload).encode("ascii")
    _LOGGER.debug("ignoring an MQTT payload of unexpected type %s", type(payload).__name__)
    return b""


@dataclass(frozen=True, slots=True)
class _PendingPublish:
    """The latest state buffered for one topic while the broker is unreachable."""

    payload: str | bytes
    qos: int
    retain: bool


class _ShutdownWorker:
    """One daemon thread that runs the blocking paho shutdown for a bridge.

    A thread of our own rather than a
    :class:`~concurrent.futures.ThreadPoolExecutor`, for the reason
    :class:`chime_deck.audio.mpv_backend._Worker` already documents:
    executor threads are joined at interpreter exit, so a shutdown left running
    there would hang pytest or systemd shutdown. This thread is a daemon and is
    never joined, so a wedged call can only leak this thread. paho's network
    thread is itself a daemon, so a ``loop_stop()`` that never finishes can only
    leak that thread too — it can never block the interpreter from exiting.
    """

    def __init__(self, shutdown: Callable[[], None]) -> None:
        """Prepare the thread without starting it.

        :param shutdown: blocking work to run on the thread.
        """
        self._shutdown = shutdown
        self._done: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._thread = threading.Thread(target=self._run, name="mqtt-shutdown", daemon=True)

    def start(self) -> concurrent.futures.Future[None]:
        """Start the thread and return the future it resolves.

        :return: future resolved, or failed, once the shutdown callable returns.
        """
        self._thread.start()
        return self._done

    def _run(self) -> None:
        """Run the shutdown once; nothing escapes this thread."""
        try:
            self._shutdown()
        except Exception as exc:
            _LOGGER.warning("the MQTT shutdown worker failed", exc_info=True)
            self._resolve_error(exc)
            return
        except BaseException as exc:
            # ``BaseException`` rather than ``Exception``: a KeyboardInterrupt or a
            # SystemExit arriving here must not print a thread traceback at exit.
            _LOGGER.warning("the MQTT shutdown worker was interrupted")
            self._resolve_error(exc)
            return
        with suppress(asyncio.CancelledError, InvalidStateError):
            self._done.set_result(None)

    def _resolve_error(self, exc: BaseException) -> None:
        """Fail the shutdown future, tolerating a cancelled or finished one.

        :param exc: whatever the shutdown body raised.
        """
        with suppress(asyncio.CancelledError, InvalidStateError):
            self._done.set_exception(exc)


class MqttBridge:
    """Own one MQTT connection: availability, status subscription, health, reconnect.

    Build one per settings generation and use it as an async context manager, or
    call :meth:`start` and :meth:`stop`. Neither raises: a broker that is down at
    startup, or wedged at shutdown, must never take the application with it.
    """

    def __init__(
        self,
        *,
        settings: MqttSettings,
        health: HealthRegistry | None = None,
        on_birth: BirthCallback | None = None,
        on_message: MessageCallback | None = None,
        client_factory: ClientFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        jitter_max_s: float = DEFAULT_BIRTH_JITTER_S,
        keepalive_s: int = DEFAULT_KEEPALIVE_S,
        reconnect_min_delay_s: int = DEFAULT_RECONNECT_MIN_DELAY_S,
        reconnect_max_delay_s: int = DEFAULT_RECONNECT_MAX_DELAY_S,
        shutdown_timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S,
        seam_timeout_s: float = DEFAULT_SEAM_TIMEOUT_S,
        publish_flush_timeout_s: float = DEFAULT_PUBLISH_FLUSH_TIMEOUT_S,
    ) -> None:
        """Validate the knobs and prepare a bridge that has opened no socket yet.

        :param settings: validated ``mqtt.*`` settings.
        :param health: registry written through for every status change.
        :param on_birth: republish hook, called on connect and on an HA birth message.
        :param on_message: handler for every other subscribed topic.
        :param client_factory: builds the client; overridden by tests.
        :param sleep: delay used for the birth jitter; overridden by tests.
        :param rng: randomness source for the jitter; overridden by tests.
        :param jitter_max_s: longest jitter before a republish; ``0`` disables it.
        :param keepalive_s: seconds between MQTT pings.
        :param reconnect_min_delay_s: first reconnect delay.
        :param reconnect_max_delay_s: upper bound for the reconnect delay.
        :param shutdown_timeout_s: seconds allowed for the paho shutdown.
        :param seam_timeout_s: seconds allowed for one seam callback.
        :param publish_flush_timeout_s: seconds allowed for the ``offline`` flush.
        :raises MqttConfigurationError: if a knob cannot be used.
        """
        self._settings = settings
        self._health = health if health is not None else HealthRegistry()
        self._on_birth = on_birth
        # The paho callback below is a method named ``_on_message``, so the seam that
        # receives the routed payloads is stored under a name of its own.
        self._message_callback = on_message
        self._client_factory: ClientFactory = (
            client_factory if client_factory is not None else default_client_factory
        )
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()
        if jitter_max_s < 0:
            raise MqttConfigurationError(
                f"jitter_max_s must be zero or greater; got {jitter_max_s:g}"
            )
        self._jitter_max_s = jitter_max_s
        self._keepalive_s = keepalive_s
        _require_positive("keepalive_s", keepalive_s)
        self._reconnect_min_delay_s = reconnect_min_delay_s
        self._reconnect_max_delay_s = reconnect_max_delay_s
        _require_positive("reconnect_min_delay_s", reconnect_min_delay_s)
        _require_positive("reconnect_max_delay_s", reconnect_max_delay_s)
        if reconnect_min_delay_s > reconnect_max_delay_s:
            raise MqttConfigurationError(
                "reconnect_min_delay_s must not be greater than reconnect_max_delay_s; got "
                f"{reconnect_min_delay_s} > {reconnect_max_delay_s}"
            )
        self._shutdown_timeout_s = _require_positive("shutdown_timeout_s", shutdown_timeout_s)
        self._seam_timeout_s = _require_positive("seam_timeout_s", seam_timeout_s)
        self._publish_flush_timeout_s = _require_positive(
            "publish_flush_timeout_s", publish_flush_timeout_s
        )
        self._started = False
        self._client_built = False
        self._stopping = False
        self._connected = False
        self._publish_failures = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: MqttClient | None = None
        self._pending: set[concurrent.futures.Future[None]] = set()
        self._pending_lock = threading.Lock()
        self._buffered: dict[str, _PendingPublish] = {}
        # The status subscription is part of the connection, so it is seeded here
        # and replayed on every connect.
        self._subscriptions: dict[str, int] = {settings.status_topic: DEFAULT_STATUS_QOS}

    def __repr__(self) -> str:
        """Return a representation with neither the password nor the username."""
        return (
            f"<{type(self).__name__} client_id={self._settings.client_id!r} "
            f"host={self._settings.host!r} port={self._settings.port!r} "
            f"connected={self._connected}>"
        )

    async def __aenter__(self) -> Self:
        """Start the bridge and return it."""
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Stop the bridge.

        :param exc_info: the exception triple, unused.
        """
        await self.stop()

    @property
    def connected(self) -> bool:
        """Return whether paho reports a live broker connection."""
        return self._connected

    @property
    def client_id(self) -> str:
        """Return the MQTT client id this bridge uses."""
        return self._settings.client_id

    @property
    def availability_topic(self) -> str:
        """Return the retained availability topic this bridge publishes to."""
        return self._settings.availability_topic

    @property
    def status_topic(self) -> str:
        """Return the Home Assistant status topic this bridge subscribes to."""
        return self._settings.status_topic

    @property
    def publish_failures(self) -> int:
        """Return how many publishes the MQTT client has refused so far.

        Only the calls where the client itself raised are counted, because those
        are the payloads that provably never reached the broker. It is a
        monotonically increasing process counter, not a per-call result: a caller
        that wants to know what one operation lost takes the difference of two
        snapshots (HSD-bq76 does exactly that).
        """
        return self._publish_failures

    @property
    def settings(self) -> MqttSettings:
        """Return the validated settings this bridge was built from."""
        return self._settings

    @property
    def subscriptions(self) -> Mapping[str, int]:
        """Return a read-only snapshot of the topics replayed on every connect."""
        return MappingProxyType(dict(self._subscriptions))

    async def start(self) -> None:
        """Create the client, connect asynchronously and start paho's thread.

        Safe to call again after :meth:`stop`: the buffered state and the
        connection flag are reset, while the recorded subscriptions are kept so
        they can be replayed. A second start while the first client is still
        alive is a no-op, because building a second client would leave the first
        one's network thread running. No exception escapes: a broker that is down
        at startup leaves the bridge in ``reconnecting`` and the application keeps
        serving.
        """
        if self._client is not None:
            _LOGGER.debug("the MQTT bridge is already started; ignoring a second start")
            return
        self._started = True
        self._client_built = False
        self._stopping = False
        self._connected = False
        self._buffered.clear()
        settings = self._settings
        if not settings.is_configured:
            self._health.set(
                "mqtt",
                HealthStatus.NOT_CONFIGURED,
                "MQTT is not configured — set mqtt.host to the broker address in Settings "
                "to enable the chime buttons",
            )
            _LOGGER.info("MQTT is not configured; the bridge stays idle")
            return
        try:
            self._loop = asyncio.get_running_loop()
            client = self._client_factory(
                client_id=settings.client_id,
                clean_session=True,
                on_connect=self._on_connect,
                on_disconnect=self._on_disconnect,
                on_message=self._on_message,
            )
            self._client = client
            self._client_built = True
            if settings.username:
                # The password is only ever handed to paho together with a
                # username; with no username it is not passed anywhere at all.
                client.username_pw_set(settings.username, settings.password or None)
            if settings.tls:
                client.tls_set()
            client.will_set(
                settings.availability_topic,
                AVAILABILITY_OFFLINE,
                qos=DEFAULT_STATUS_QOS,
                retain=True,
            )
            client.reconnect_delay_set(self._reconnect_min_delay_s, self._reconnect_max_delay_s)
            client.connect_async(settings.host, settings.port, keepalive=self._keepalive_s)
            client.loop_start()
        except Exception as exc:
            _LOGGER.exception("starting the MQTT bridge failed")
            self._health.set(
                "mqtt", HealthStatus.ERROR, self._clean(str(exc)) or type(exc).__name__
            )
            self._client = None
            # The client never became usable, so a later stop() must not report
            # "disabled" over this actionable error.
            self._client_built = False
            return
        self._health.set(
            "mqtt",
            HealthStatus.RECONNECTING,
            f"connecting to the MQTT broker at {settings.host}:{settings.port}",
        )

    async def stop(self) -> None:
        """Stop the client and drain the in-flight callbacks. Safe to call twice.

        The retained ``offline`` publish, ``disconnect()`` and ``loop_stop()`` all
        run on a bridge-owned daemon thread, because ``loop_stop()`` joins paho's
        network thread and has no timeout of its own. No exception escapes.
        """
        if self._stopping:
            return
        self._stopping = True
        try:
            client = self._client
            if client is not None:
                await self._stop_client(client)
            self._client = None
            self._loop = None
            self._connected = False
            self._buffered.clear()
            await self._drain_pending()
            if self._started and self._client_built and self._settings.is_configured:
                # An unconfigured bridge keeps reporting "not configured": that is
                # the actionable state, and "disabled" would hide it. A client that
                # never became usable keeps its start-up status too: "disabled"
                # would overwrite the actionable "error" a failed start reported.
                self._health.set("mqtt", HealthStatus.DISABLED, "the MQTT bridge is stopped")
        except Exception:
            _LOGGER.exception("stopping the MQTT bridge failed")

    def publish(
        self, topic: str, payload: str | bytes, *, qos: int = 0, retain: bool = False
    ) -> None:
        """Publish the desired state of *topic*, or buffer it until connected.

        This is design 02 §5's "buffer at most the latest desired state per
        button" implemented once, at the bridge level: the buffer holds the
        latest *state* of a topic, never a fire-and-forget command, so a broker
        outage loses events but not the desired state. Call it from the event
        loop only.

        :param topic: topic to publish to.
        :param payload: payload to publish.
        :param qos: delivery guarantee for the publish.
        :param retain: whether the broker keeps the payload for new subscribers.
        """
        client = self._client
        if self._connected and client is not None:
            try:
                client.publish(topic, payload, qos=qos, retain=retain)
            except Exception:
                self._publish_failures += 1
                _LOGGER.warning("publishing to %s failed", topic, exc_info=True)
            return
        if topic not in self._buffered and len(self._buffered) >= MAX_BUFFERED_TOPICS:
            oldest = next(iter(self._buffered))
            del self._buffered[oldest]
            _LOGGER.warning(
                "the MQTT buffer is full; dropping the buffered state for %s to make room for %s",
                oldest,
                topic,
            )
        # A dict keeps insertion order, so an existing topic keeps its position and
        # the flush order is the order the topics were first desired.
        self._buffered[topic] = _PendingPublish(payload=payload, qos=qos, retain=retain)

    def subscribe(self, topic: str, qos: int = 0) -> None:
        """Record a subscription and make it now when connected.

        Because the client connects with ``clean_session=True``, every reconnect
        drops the subscriptions on the broker side, so the recorded topics are
        replayed on every connect. This is the seam HSD-c3si registers its command
        topics through.

        :param topic: topic to subscribe to.
        :param qos: delivery guarantee for the subscription.
        """
        self._subscriptions[topic] = qos
        if not self._connected or self._client is None:
            return
        self._subscribe_now(topic, qos)

    def unsubscribe(self, topic: str) -> None:
        """Forget a recorded subscription and remove it from the live client.

        The recorded pop is the load-bearing half. Because the client connects
        with ``clean_session=True``, every reconnect replays the recorded
        subscriptions, so a topic that is not popped here is re-SUBSCRIBEd on every
        reconnect for the life of the bridge. That is the subscription leak this
        method exists to close: design 02 §5 says a deleted or renamed button must
        stop routing presses, and forgetting only the live client would bring the
        filter back on the next connect.

        The pop happens even while stopping, because :meth:`start` deliberately
        keeps the recorded subscriptions across a stop/start cycle, so a skipped
        pop would be replayed on the next start.

        A filter this bridge never recorded is a quiet no-op: there is nothing a
        reconnect could replay, and telling the broker to drop a filter it was
        never asked for would only add noise.

        The Home Assistant status topic is refused: the bridge owns that
        subscription (it is seeded in ``__init__``), and removing it would silently
        kill the birth handling that republishes discovery — and it would come back
        on the next connect anyway.

        :param topic: topic filter to stop receiving from.
        """
        if topic == self._settings.status_topic:
            _LOGGER.debug(
                "refusing to unsubscribe from the Home Assistant status topic %s: the bridge "
                "owns that subscription",
                topic,
            )
            return
        recorded = topic in self._subscriptions
        self._subscriptions.pop(topic, None)
        if not recorded:
            _LOGGER.debug("ignoring an unsubscribe for the unknown topic %s", topic)
            return
        if self._is_stopping():
            return
        self._unsubscribe_now(topic)

    def set_on_birth(self, on_birth: BirthCallback | None) -> None:
        """Install the full-republish seam after construction.

        The discovery service needs the bridge and the bridge needs the discovery
        service's callbacks, so the callbacks are installed once both exist rather
        than passed to the constructor. A callback installed before :meth:`start`
        is used by the first CONNACK; one installed later is used by the next
        connect or the next Home Assistant birth message.

        A call while stopping is ignored, so a callback cannot be resurrected after
        the shutdown has already published the retained ``offline``.

        :param on_birth: callback that republishes the discovery payloads, or
            ``None`` to remove it.
        """
        if self._is_stopping():
            _LOGGER.debug("ignoring set_on_birth: the MQTT bridge is stopping")
            return
        self._on_birth = on_birth

    def set_on_message(self, on_message: MessageCallback | None) -> None:
        """Install the command-topic seam after construction.

        The same wiring cycle as :meth:`set_on_birth`: the bridge and the service
        that routes presses are built from each other, so the callback is
        installed afterwards. A call while stopping is ignored, so a handler
        cannot come back to life after the shutdown has published the retained
        ``offline``.

        :param on_message: callback receiving the topic, the normalised payload and
            the broker's RETAIN flag for every message on a subscribed, non-status
            topic, or ``None`` to remove it.
        """
        if self._is_stopping():
            _LOGGER.debug("ignoring set_on_message: the MQTT bridge is stopping")
            return
        self._message_callback = on_message

    def _subscribe_now(self, topic: str, qos: int) -> None:
        """Subscribe on the live client, logging instead of raising.

        The one guard shared by :meth:`subscribe` and the connect-time replay, so
        a topic paho rejects (``ValueError`` for a bad filter or QoS) can neither
        abort the replay nor stop the discovery republish that follows it. The
        recorded topic is replayed on the next connect, so nothing is lost.

        :param topic: topic filter to subscribe to.
        :param qos: delivery guarantee for the subscription.
        """
        client = self._client
        if client is None:
            return
        try:
            client.subscribe(topic, qos=qos)
        except Exception:
            _LOGGER.warning("subscribing to %s failed", topic, exc_info=True)

    def _unsubscribe_now(self, topic: str) -> None:
        """Unsubscribe on the live client, logging instead of raising.

        Doing nothing when no client exists is intentional: ``clean_session=True``
        means the broker already dropped every subscription with the previous
        connection, so only the recorded pop in :meth:`unsubscribe` matters.

        :param topic: topic filter to unsubscribe from.
        """
        client = self._client
        if client is None:
            return
        try:
            client.unsubscribe(topic)
        except Exception:
            _LOGGER.warning("unsubscribing from %s failed", topic, exc_info=True)

    async def _stop_client(self, client: MqttClient) -> None:
        """Run the blocking shutdown on a daemon thread and wait for it, bounded.

        ``asyncio.wrap_future`` is deliberately not used: it installs a
        cross-thread callback in the loop and cancels the concurrent future when
        the wait is cancelled, which would abort a shutdown already in progress.

        :param client: the client to shut down.
        """
        future = _ShutdownWorker(partial(self._shutdown_client, client)).start()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._shutdown_timeout_s
        # The real asyncio.sleep: this poll must not use the injected jitter sleep.
        while not future.done():
            if loop.time() >= deadline:
                _LOGGER.warning(
                    "the MQTT client did not stop within %g s — leaving its thread to finish "
                    "on its own",
                    self._shutdown_timeout_s,
                )
                return
            await asyncio.sleep(_SHUTDOWN_POLL_S)

    def _shutdown_client(self, client: MqttClient) -> None:
        """Publish ``offline``, disconnect and join paho's thread. Runs on the worker.

        The order matters. The broker does not publish the last will on a clean
        disconnect, so this explicit retained ``offline`` is the only thing that
        stops Home Assistant showing the device as available. paho writes its
        queued packets FIFO, so the PUBLISH precedes the DISCONNECT packet, and
        ``loop_stop()`` joins paho's network thread — which has no timeout of its
        own, hence the outer daemon-thread budget in :meth:`_stop_client`.

        :param client: the client to shut down.
        """
        if self._connected:
            try:
                info = client.publish(
                    self._settings.availability_topic,
                    AVAILABILITY_OFFLINE,
                    qos=DEFAULT_STATUS_QOS,
                    retain=True,
                )
                info.wait_for_publish(timeout=self._publish_flush_timeout_s)
            except (ValueError, RuntimeError):
                # A plain return does not prove delivery, so claim nothing here.
                _LOGGER.warning("flushing the retained MQTT offline availability failed")
        try:
            client.disconnect()
        finally:
            # In a ``finally`` on purpose: a raising ``disconnect()`` must not leave
            # paho's network thread reconnecting for the life of the process.
            client.loop_stop()

    async def _drain_pending(self) -> None:
        """Wait, bounded, for the callbacks already handed to the loop."""
        with self._pending_lock:
            pending = tuple(self._pending)
        if not pending:
            return
        await asyncio.to_thread(concurrent.futures.wait, pending, timeout=self._shutdown_timeout_s)

    def _submit(self, coro: Coroutine[Any, Any, None]) -> None:
        """Hand one coroutine from paho's network thread to the event loop.

        A callback that arrives after shutdown is a silent no-op — the coroutine
        is closed rather than dropped, because an un-awaited coroutine raises
        ``RuntimeWarning`` and ``filterwarnings = ["error"]`` turns that into a
        failure.

        :param coro: coroutine built by a callback.
        """
        loop = self._loop
        if self._stopping or loop is None or loop.is_closed():
            coro.close()
            return
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            _LOGGER.debug("dropping an MQTT event for a closed event loop", exc_info=True)
            coro.close()
            return
        with self._pending_lock:
            self._pending.add(future)
        future.add_done_callback(self._forget)

    def _forget(self, future: concurrent.futures.Future[None]) -> None:
        """Consume the outcome of one bridge task and forget it.

        Runs on the event loop thread. It never calls ``result()`` in a way that
        re-raises, so a failing handler can never surface as an unhandled
        exception in paho's thread or as a warning pytest would fail on.

        :param future: the finished task.
        """
        with self._pending_lock:
            self._pending.discard(future)
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            _LOGGER.warning(
                "an MQTT bridge task failed: %s", self._clean(str(error)) or type(error).__name__
            )

    def _on_connect(
        self,
        _client: object,
        _userdata: object,
        _flags: ConnectFlags,
        reason_code: ReasonCode,
        _properties: Properties | None,
    ) -> None:
        """Accept paho's connect callback and hop to the event loop.

        :param _client: paho's client instance, unused.
        :param _userdata: paho's userdata, unused.
        :param _flags: the CONNACK flags, unused.
        :param reason_code: the CONNACK reason code.
        :param _properties: the CONNACK properties, unused.
        """
        self._submit(self._handle_connect(reason_code))

    def _on_disconnect(
        self,
        _client: object,
        _userdata: object,
        _flags: DisconnectFlags,
        reason_code: ReasonCode,
        _properties: Properties | None,
    ) -> None:
        """Accept paho's disconnect callback and hop to the event loop.

        :param _client: paho's client instance, unused.
        :param _userdata: paho's userdata, unused.
        :param _flags: the disconnect flags, unused.
        :param reason_code: the disconnect reason code.
        :param _properties: the disconnect properties, unused.
        """
        self._submit(self._handle_disconnect(reason_code))

    def _on_message(self, _client: object, _userdata: object, message: MqttMessage) -> None:
        """Accept paho's message callback and hop to the event loop.

        The RETAIN flag rides along with the topic and the payload: it describes
        *this delivery* (a broker replay) and is gone once the message object is,
        so the handler that has to refuse a replayed command cannot recover it
        later.

        :param _client: paho's client instance, unused.
        :param _userdata: paho's userdata, unused.
        :param message: the incoming message.
        """
        self._submit(self._handle_message(message.topic, message.payload, message.retain))

    async def _handle_connect(self, reason_code: ReasonCode) -> None:
        """Publish availability, replay the subscriptions and ask for a republish.

        A rejected connection is reported and then dropped: retrying is paho's
        job, and publishing availability or subscribing over a connection that
        does not exist would only log noise.

        :param reason_code: the CONNACK reason code.
        """
        if self._stopping:
            # A CONNACK that was already in flight when stop() began must not
            # resurrect the connection after the shutdown has published offline.
            return
        settings = self._settings
        if reason_code.is_failure or reason_code.value != 0:
            self._connected = False
            # A tuple and ``==``, never ``in``: paho's ReasonCode defines __eq__ but
            # no __hash__, so set membership would raise TypeError.
            if any(reason_code == code for code in _AUTH_FAILURE_CODES):
                self._health.set(
                    "mqtt",
                    HealthStatus.ERROR,
                    f"the broker at {settings.host}:{settings.port} rejected the credentials "
                    "— check mqtt.username and mqtt.password",
                )
                _LOGGER.warning(
                    "the MQTT broker rejected the credentials — check mqtt.username and "
                    "mqtt.password"
                )
            elif any(reason_code == code for code in _PERMANENT_FAILURE_CODES):
                self._health.set(
                    "mqtt",
                    HealthStatus.ERROR,
                    "the broker rejected the connection permanently "
                    f"({self._clean(str(reason_code))}) — check mqtt.instance_id and the "
                    "broker configuration",
                )
                _LOGGER.warning("the MQTT broker rejected the connection permanently")
            else:
                self._health.set(
                    "mqtt",
                    HealthStatus.RECONNECTING,
                    f"the broker at {settings.host}:{settings.port} refused the connection "
                    f"({self._clean(str(reason_code))}) — paho keeps retrying every "
                    f"{self._reconnect_min_delay_s} to {self._reconnect_max_delay_s} s",
                )
                _LOGGER.warning("the MQTT broker refused the connection")
            return
        self._connected = True
        client = self._client
        if client is None:
            return
        # The guarded publish, not the raw client call: an availability topic paho
        # rejects must not abort the subscriptions and the republish below it.
        self._publish_availability(AVAILABILITY_ONLINE)
        # clean_session=True means the broker dropped every subscription with the
        # previous connection, so the replay below is mandatory, not a nicety.
        for topic, qos in self._subscriptions.items():
            self._subscribe_now(topic, qos)
        self._health.set(
            "mqtt",
            HealthStatus.CONNECTED,
            f"connected to the MQTT broker at {settings.host}:{settings.port}",
        )
        for topic, pending in self._buffered.items():
            try:
                info = client.publish(
                    topic, pending.payload, qos=pending.qos, retain=pending.retain
                )
                # Inside the ``try``: real paho raises ``ValueError``/``RuntimeError``
                # from ``is_published()``, and one poisoned entry must not abort the
                # flush — nor the republish that follows it.
                is_published = getattr(info, "is_published", None)
                if callable(is_published) and not is_published():
                    _LOGGER.debug("the buffered MQTT state for %s is still queued", topic)
            except Exception:
                _LOGGER.warning("replaying the buffered MQTT state for %s failed", topic)
                continue
        # Unconditional: nothing above may keep the birth seam from running, or
        # HSD-c3si's discovery republish would never reach Home Assistant.
        self._buffered.clear()
        await self._invoke_birth("connected to the broker")

    async def _handle_disconnect(self, reason_code: ReasonCode) -> None:
        """Record the lost connection; paho's own backoff does the reconnecting.

        :param reason_code: the disconnect reason code.
        """
        self._connected = False
        if self._stopping:
            _LOGGER.debug("the MQTT client disconnected while the bridge was stopping")
            return
        settings = self._settings
        self._health.set(
            "mqtt",
            HealthStatus.RECONNECTING,
            f"lost the connection to the MQTT broker at {settings.host}:{settings.port} "
            f"({self._clean(str(reason_code))}) — paho keeps retrying every "
            f"{self._reconnect_min_delay_s} to {self._reconnect_max_delay_s} s",
        )
        _LOGGER.warning("lost the connection to the MQTT broker")

    async def _handle_message(self, topic: str, payload: object, retain: bool) -> None:
        """Route one incoming message. Never raises.

        On a Home Assistant birth message the discovery payloads go out first and
        the availability last (design 02 §3): Home Assistant drops availability
        that arrives before the config payload, and because discovery is not
        retained (design 02 §4) the republish is what recreates the entities.

        :param topic: topic the message arrived on.
        :param payload: raw payload, normalised here.
        :param retain: the broker's RETAIN flag for this delivery, passed on to the
            message seam untouched.
        """
        if self._stopping:
            # A birth message racing with stop() would republish availability after
            # the shutdown already published the retained offline state.
            return
        data = _payload_bytes(payload)
        if topic == self._settings.status_topic:
            if data == b"online":
                await self._sleep(self._rng.uniform(0.0, self._jitter_max_s))
                # Re-checked after the jitter: stop() may have published the retained
                # offline while this message was waiting for its delay.
                if self._is_stopping():
                    _LOGGER.debug("dropping a Home Assistant birth message: the bridge is stopping")
                    return
                await self._invoke_birth("Home Assistant asked for a republish")
                # And again right before publishing: the retained ``online`` would
                # otherwise land after the shutdown's retained ``offline`` and leave
                # Home Assistant showing a powered-off device as available.
                if self._is_stopping():
                    _LOGGER.debug(
                        "dropping a Home Assistant birth message before publishing availability"
                    )
                    return
                self._publish_availability(AVAILABILITY_ONLINE)
            elif data == b"offline":
                _LOGGER.debug("Home Assistant announced it is going away; nothing to do")
            else:
                _LOGGER.debug("ignoring an unknown status payload on %s", topic)
            return
        if self._message_callback is None:
            _LOGGER.debug("ignoring an MQTT message on %s: no handler is installed", topic)
            return
        await self._invoke_message(topic, data, retain)

    def _is_stopping(self) -> bool:
        """Return whether :meth:`stop` has already begun.

        A method rather than a plain attribute test because mypy narrows an
        attribute checked once for the rest of a block, while these guards exist
        precisely because an ``await`` between two checks can flip the flag.
        """
        return self._stopping

    def _publish_availability(self, payload: str) -> None:
        """Publish one retained availability payload, or log why it did not go out.

        :param payload: :data:`AVAILABILITY_ONLINE` or :data:`AVAILABILITY_OFFLINE`.
        """
        client = self._client
        if client is None:
            return
        try:
            client.publish(
                self._settings.availability_topic,
                payload,
                qos=DEFAULT_STATUS_QOS,
                retain=True,
            )
        except Exception:
            _LOGGER.warning("publishing the MQTT availability state failed", exc_info=True)

    async def _invoke_birth(self, reason: str) -> None:
        """Run the full-republish seam, bounded and never fatal.

        The seam is what HSD-c3si fills with the discovery payloads.

        :param reason: why the republish was asked for, named in a warning.
        """
        if self._on_birth is None:
            _LOGGER.debug("no MQTT republish callback is installed (%s)", reason)
            return
        try:
            await asyncio.wait_for(self._on_birth(), timeout=self._seam_timeout_s)
        except TimeoutError:
            _LOGGER.warning(
                "the MQTT republish callback did not finish within %g s (%s)",
                self._seam_timeout_s,
                reason,
            )
        except Exception:
            _LOGGER.warning("the MQTT republish callback failed (%s)", reason, exc_info=True)

    async def _invoke_message(self, topic: str, payload: bytes, retain: bool) -> None:
        """Run the message seam, bounded and never fatal.

        :param topic: topic the message arrived on.
        :param payload: normalised payload.
        :param retain: the broker's RETAIN flag for this delivery.
        """
        on_message = self._message_callback
        if on_message is None:
            return
        try:
            await asyncio.wait_for(on_message(topic, payload, retain), timeout=self._seam_timeout_s)
        except TimeoutError:
            _LOGGER.warning(
                "the MQTT message callback did not finish within %g s for %s",
                self._seam_timeout_s,
                topic,
            )
        except Exception:
            _LOGGER.warning("the MQTT message callback failed for %s", topic, exc_info=True)

    def _clean(self, text: str) -> str:
        """Redact the password, scrub registered secrets and clip to a detail line.

        The literal password is replaced before the pattern rules so the
        configured value is redacted even if no scrubber registration exists yet;
        the result is whitespace-collapsed and clipped afterwards.

        :param text: text from the broker or from an exception.
        """
        redacted = text
        if self._settings.password:
            redacted = redacted.replace(self._settings.password, REDACTED)
        return " ".join(SECRET_SCRUBBER.scrub(redacted).split())[:_DETAIL_CLIP]
