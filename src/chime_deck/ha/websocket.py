"""Reconnecting Home Assistant WebSocket client with a compressed state cache.

Home Assistant pushes entity state over ``/api/websocket`` using the compressed
``subscribe_entities`` format (``a`` for a full add, ``c`` for a change and ``r``
for a removal). This module owns one long-lived connection: it performs the auth
handshake, subscribes to the configured entity ids, keeps a cache of the merged
states, and reconnects with exponential backoff when the socket drops. Every
reconnect re-subscribes and re-seeds the cache, so an entity that disappeared
while the connection was down does not linger.

**Liveness.** ``ping_interval``/``ping_timeout`` on the real socket are the
liveness detector. An idle read therefore never causes a reconnect: the receive
loop simply keeps waiting on the same ``recv`` task, which is what keeps the UI
from flapping while nothing changes on the Home Assistant side.

**Token secrecy.** The token is only ever placed in the ``auth`` frame. A string
built from Home Assistant's own text goes through :meth:`_clean`, which replaces
the literal token first and the process-wide scrubber second, so a rejected token
cannot reach a log line, an error message, a ``repr`` or the health detail.

Owning ticket: HSD-a21l.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final, Protocol, Self, cast

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from ..config import REDACTED, SECRET_SCRUBBER
from ..health import HealthRegistry, HealthStatus
from .client import normalise_base_url
from .errors import (
    HaAuthError,
    HaBadResponseError,
    HaCommandError,
    HaConfigurationError,
    HaConnectionError,
    HaError,
)

__all__ = [
    "DEFAULT_CLOSE_TIMEOUT_S",
    "DEFAULT_COMMAND_TIMEOUT_S",
    "DEFAULT_HANDSHAKE_TIMEOUT_S",
    "DEFAULT_OPEN_TIMEOUT_S",
    "DEFAULT_PING_INTERVAL_S",
    "DEFAULT_PING_TIMEOUT_S",
    "DEFAULT_RECEIVE_TIMEOUT_S",
    "DEFAULT_RECONNECT_INITIAL_S",
    "DEFAULT_RECONNECT_MAX_S",
    "DEFAULT_SHUTDOWN_TIMEOUT_S",
    "ConnectionFactory",
    "HaEntityState",
    "HaWebSocketConnection",
    "HomeAssistantWebSocketClient",
    "default_connection_factory",
    "websocket_url",
]

_LOGGER = logging.getLogger(__name__)

DEFAULT_RECONNECT_INITIAL_S: Final = 1.0
"""First reconnect delay; each failed attempt doubles it."""

DEFAULT_RECONNECT_MAX_S: Final = 30.0
"""Upper bound for the reconnect delay."""

DEFAULT_HANDSHAKE_TIMEOUT_S: Final = 10.0
"""Seconds allowed for the auth handshake and for one subscription answer."""

DEFAULT_RECEIVE_TIMEOUT_S: Final = 60.0
"""Seconds the receive loop waits before re-checking the control event."""

DEFAULT_SHUTDOWN_TIMEOUT_S: Final = 5.0
"""Seconds allowed for cancelling and draining the session tasks."""

DEFAULT_OPEN_TIMEOUT_S: Final = 10.0
"""Seconds allowed for opening the websocket connection."""

DEFAULT_PING_INTERVAL_S: Final = 20.0
"""Seconds between websocket pings; ``None`` is not accepted because it disables liveness."""

DEFAULT_PING_TIMEOUT_S: Final = 20.0
"""Seconds allowed for a pong before the connection is considered dead."""

DEFAULT_CLOSE_TIMEOUT_S: Final = 5.0
"""Seconds allowed for the websocket close handshake."""

DEFAULT_COMMAND_TIMEOUT_S: Final = 2.0
"""Seconds allowed for one one-off command; deliberately below the live-lookup bound.

A command is a side channel to the state subscription, so it must be cheaper than
the 3 s an entity page may spend on live data; without retries, a slow command is
the caller's problem to retry.
"""

_MAX_ATTEMPT: Final = 30
"""Highest doubling exponent, so a wedged counter cannot overflow the delay."""

_DETAIL_CLIP: Final = 200
"""Maximum characters of Home Assistant text echoed into a detail or an error."""

_REDACTED_TOKEN: Final = "***"
"""Replacement for the token in :meth:`HomeAssistantWebSocketClient.__repr__`."""

_HANDSHAKE_UNEXPECTED: Final = "Home Assistant sent an unexpected websocket handshake message"


def websocket_url(base_url: str) -> str:
    """Turn a stored ``ha.base_url`` into the Home Assistant websocket endpoint.

    The base URL is normalised with :func:`~chime_deck.ha.client.normalise_base_url`
    first, so the same validation and error messages apply, and any reverse-proxy
    path prefix is preserved. ``http`` becomes ``ws`` and ``https`` becomes ``wss``.

    :param base_url: the value stored in ``ha.base_url``.
    :raises HaConfigurationError: if the value cannot be an http(s) base URL.
    """
    base = normalise_base_url(base_url)
    scheme, separator, rest = base.partition("://")
    websocket_scheme = "wss" if scheme == "https" else "ws"
    return f"{websocket_scheme}{separator}{rest}/api/websocket"


class HaWebSocketConnection(Protocol):
    """The narrow connection port the session code needs."""

    async def send(self, message: str) -> None:
        """Send one text frame.

        :param message: already-encoded JSON frame.
        """
        ...

    async def recv(self) -> str | bytes:
        """Receive one frame, blocking until one arrives."""
        ...

    async def close(self) -> None:
        """Close the connection."""
        ...


class ConnectionFactory(Protocol):
    """The callable that opens one connection, satisfied by the default factory.

    The result is an async context manager so the socket is closed even when the
    session raises; ``close_timeout`` bounds that close.
    """

    def __call__(
        self,
        url: str,
        *,
        open_timeout: float,
        close_timeout: float,
        ping_interval: float,
        ping_timeout: float,
    ) -> AbstractAsyncContextManager[HaWebSocketConnection]:
        """Open a connection to *url*.

        :param url: the ``ws://`` or ``wss://`` endpoint.
        :param open_timeout: seconds allowed to open the connection.
        :param close_timeout: seconds allowed for the close handshake.
        :param ping_interval: seconds between pings.
        :param ping_timeout: seconds allowed for a pong.
        """
        ...


@asynccontextmanager
async def default_connection_factory(
    url: str,
    *,
    open_timeout: float,
    close_timeout: float,
    ping_interval: float,
    ping_timeout: float,
) -> AsyncIterator[HaWebSocketConnection]:
    """Open a real :mod:`websockets` connection with the configured timeouts.

    ``max_size`` is left at the ``websockets`` default: the compressed state
    stream is small, and a larger limit is not needed for this feature.

    :param url: the ``ws://`` or ``wss://`` endpoint.
    :param open_timeout: seconds allowed to open the connection.
    :param close_timeout: seconds allowed for the close handshake.
    :param ping_interval: seconds between pings.
    :param ping_timeout: seconds allowed for a pong.
    """
    async with connect(
        url,
        open_timeout=open_timeout,
        close_timeout=close_timeout,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
    ) as connection:
        yield cast("HaWebSocketConnection", connection)


@dataclass(frozen=True, slots=True)
class HaEntityState:
    """One entity state after the compressed diffs have been merged.

    ``state`` is ``None`` in the removal callback: the entity no longer exists in
    Home Assistant. ``attributes`` is a read-only copy, so a frozen state object
    cannot hand out a mapping the caller could mutate underneath it.

    Timestamps are ISO-8601 UTC strings, matching
    :class:`~chime_deck.ha.client.HaState`, even though Home Assistant sends
    the compressed form as epoch seconds.
    """

    entity_id: str
    state: str | None
    attributes: Mapping[str, object]
    last_changed: str | None = None
    last_updated: str | None = None

    def __post_init__(self) -> None:
        """Replace *attributes* with a read-only copy."""
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


def _timestamp(value: object) -> str | None:
    """Convert one compressed timestamp to the ISO-8601 form ``HaState`` uses.

    A string is accepted unchanged so a future Home Assistant version that sends
    formatted timestamps keeps working. ``None`` means the field was absent.

    :param value: ``lc``/``lu`` value from a compressed state, epoch seconds or text.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        _LOGGER.warning("ignoring a websocket timestamp that is a boolean")
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            _LOGGER.warning("ignoring a websocket timestamp outside the supported range")
            return None
    _LOGGER.warning("ignoring a websocket timestamp of unexpected type %s", type(value).__name__)
    return None


def _require_positive(name: str, value: float) -> float:
    """Check that a timeout or backoff bound is usable.

    :param name: constructor argument name, named in the message.
    :param value: value to check.
    :raises HaConfigurationError: if *value* is not greater than zero.
    """
    if value <= 0:
        raise HaConfigurationError(f"{name} must be greater than zero; got {value:g}")
    return value


def _normalise_entity_ids(values: Iterable[str]) -> tuple[str, ...]:
    """Strip, validate and de-duplicate entity ids while preserving order.

    :param values: entity ids from the constructor or :meth:`set_entity_ids`.
    :raises HaConfigurationError: if *values* is a bare string, or a value is not a
        non-empty string.
    """
    if isinstance(values, str):
        raise HaConfigurationError(
            "entity_ids must be an iterable of entity ids, not a single string — wrap it in a list"
        )
    normalised: dict[str, None] = {}
    for value in values:
        if not isinstance(value, str):
            raise HaConfigurationError(
                f"entity_ids must contain strings — got {type(value).__name__}"
            )
        entity_id = value.strip()
        if not entity_id:
            raise HaConfigurationError("entity_ids must not contain an empty entity id")
        normalised.setdefault(entity_id, None)
    return tuple(normalised)


class HomeAssistantWebSocketClient:
    """Subscribe to Home Assistant entity states over ``/api/websocket``.

    Build one per settings generation and use it as an async context manager, or
    call :meth:`start` and :meth:`stop`. Reconnects are automatic and bounded by
    :attr:`~chime_deck.ha.websocket.DEFAULT_RECONNECT_MAX_S`; a rejected token
    is terminal and is never retried.

    :attr:`connected` is the freshness signal for the live-data adapter: a read of
    the cache is only meaningful while it is ``True``, because a disconnected
    client's cache is a stale snapshot with no live Home Assistant behind it. It
    is cleared the moment a session ends — before the reconnect backoff starts —
    so a caller can never mistake a reconnecting client for a live one.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        entity_ids: Iterable[str] = (),
        health: HealthRegistry | None = None,
        on_state_change: Callable[[HaEntityState], None] | None = None,
        connect: ConnectionFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        initial_backoff_s: float = DEFAULT_RECONNECT_INITIAL_S,
        max_backoff_s: float = DEFAULT_RECONNECT_MAX_S,
        handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
        command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
        receive_timeout_s: float = DEFAULT_RECEIVE_TIMEOUT_S,
        shutdown_timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S,
        open_timeout_s: float = DEFAULT_OPEN_TIMEOUT_S,
        close_timeout_s: float = DEFAULT_CLOSE_TIMEOUT_S,
        ping_interval_s: float = DEFAULT_PING_INTERVAL_S,
        ping_timeout_s: float = DEFAULT_PING_TIMEOUT_S,
    ) -> None:
        """Validate the settings and prepare a client that opens no socket yet.

        :param base_url: the stored ``ha.base_url``; normalised before use.
        :param token: the stored ``ha.token``; never logged and never re-exposed.
        :param entity_ids: entities to subscribe to; an empty list is "nothing selected".
        :param health: registry written through for every status change.
        :param on_state_change: called after every add, change and removal.
        :param connect: opens one connection; overridden by tests.
        :param sleep: delay used between reconnect attempts; overridden by tests.
        :param initial_backoff_s: first reconnect delay.
        :param max_backoff_s: upper bound for the reconnect delay.
        :param handshake_timeout_s: seconds allowed for the handshake and one result.
        :param command_timeout_s: seconds allowed for one one-off command result.
        :param receive_timeout_s: seconds between control-event checks while idle.
        :param shutdown_timeout_s: seconds allowed to drain the session tasks.
        :param open_timeout_s: seconds allowed to open the socket.
        :param close_timeout_s: seconds allowed for the close handshake.
        :param ping_interval_s: seconds between websocket pings.
        :param ping_timeout_s: seconds allowed for a pong.
        :raises HaConfigurationError: if a setting or a timeout cannot be used.
        """
        self._base_url = normalise_base_url(base_url)
        stripped_token = token.strip()
        if not stripped_token:
            raise HaConfigurationError(
                "no Home Assistant token is configured — add a long-lived access token "
                "to ha.token in Settings"
            )
        self._token = stripped_token
        self._url = websocket_url(self._base_url)
        self._entity_ids = _normalise_entity_ids(entity_ids)
        self._health = health if health is not None else HealthRegistry()
        self._on_state_change = on_state_change
        self._connect: ConnectionFactory = (
            connect if connect is not None else default_connection_factory
        )
        self._sleep = sleep
        self._initial_backoff_s = _require_positive("initial_backoff_s", initial_backoff_s)
        self._max_backoff_s = _require_positive("max_backoff_s", max_backoff_s)
        if self._initial_backoff_s > self._max_backoff_s:
            raise HaConfigurationError(
                "initial_backoff_s must not be greater than max_backoff_s; got "
                f"{initial_backoff_s:g} > {max_backoff_s:g}"
            )
        self._handshake_timeout_s = _require_positive("handshake_timeout_s", handshake_timeout_s)
        self._command_timeout_s = _require_positive("command_timeout_s", command_timeout_s)
        self._receive_timeout_s = _require_positive("receive_timeout_s", receive_timeout_s)
        self._shutdown_timeout_s = _require_positive("shutdown_timeout_s", shutdown_timeout_s)
        self._open_timeout_s = _require_positive("open_timeout_s", open_timeout_s)
        self._close_timeout_s = _require_positive("close_timeout_s", close_timeout_s)
        self._ping_interval_s = _require_positive("ping_interval_s", ping_interval_s)
        self._ping_timeout_s = _require_positive("ping_timeout_s", ping_timeout_s)
        self._states: dict[str, HaEntityState] = {}
        self._control_event = asyncio.Event()
        self._next_id = 0
        self._attempt = 0
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._stopping = False
        self._connected = False
        self._terminal = False
        self._terminal_detail = ""
        self._last_error: HaError | None = None

    def __repr__(self) -> str:
        """Return a representation that cannot contain the token."""
        return (
            f"<{type(self).__name__} url={self._url!r} token={_REDACTED_TOKEN!r} "
            f"entities={len(self._entity_ids)}>"
        )

    async def __aenter__(self) -> Self:
        """Start the client and return it."""
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Stop the client.

        :param exc_info: the exception triple, unused.
        """
        await self.stop()

    @property
    def states(self) -> Mapping[str, HaEntityState]:
        """Return a read-only snapshot of the current cache."""
        return MappingProxyType(dict(self._states))

    @property
    def entity_ids(self) -> tuple[str, ...]:
        """Return the entity ids currently subscribed to."""
        return self._entity_ids

    @property
    def last_error(self) -> HaError | None:
        """Return the last error the supervisor saw, or ``None``."""
        return self._last_error

    @property
    def connected(self) -> bool:
        """Return whether a live subscription is currently keeping the cache up to date."""
        return self._connected

    def get_state(self, entity_id: str) -> HaEntityState | None:
        """Return the cached state of *entity_id*, or ``None``.

        :param entity_id: for example ``"sensor.front_door"``.
        """
        return self._states.get(entity_id)

    def set_on_state_change(self, callback: Callable[[HaEntityState], None] | None) -> None:
        """Install the callback fired after every cache change.

        :param callback: called with the merged state, or ``None`` to clear it.
        """
        self._on_state_change = callback

    def set_entity_ids(self, values: Iterable[str]) -> None:
        """Replace the entity ids and wake a live session so it re-subscribes.

        With no running supervisor this only stores the new list; after a start
        it launches the supervisor. A list that becomes empty closes the socket
        and reports ``no_entities`` instead of subscribing to every entity.

        :param values: new entity ids.
        :raises HaConfigurationError: if a value is not a non-empty string.
        """
        self._entity_ids = _normalise_entity_ids(values)
        self._control_event.set()
        if self._started and not self._stopping and not self._task_active():
            self._launch_supervisor()

    async def command(self, command_type: str, /, **fields: object) -> Mapping[str, object]:
        """Run one authenticated websocket command and return its result object.

        This is the request/response side channel D16's step 3 needs
        (``config/entity_registry/list_for_display``). It opens its **own**
        short-lived authenticated connection: multiplexing onto the supervisor's
        socket is not possible, because :meth:`_session` keeps a live receive task
        that would steal the result frame. It therefore works on a client that was
        never started and with an empty entity list (the CRUD path does not
        subscribe), and it never disturbs a running session.

        It deliberately touches no client-wide state: no health write, no cache
        change, no backoff reset, no terminal flag. Command ids come from the
        shared monotonic counter, which is harmless because ids are scoped to one
        connection and everything here runs on one event loop. A timeout raises
        :class:`HaConnectionError` for consistency with the handshake and
        subscription paths, not :class:`HaTimeoutError`.

        :param command_type: Home Assistant command type, e.g.
            ``"config/entity_registry/list_for_display"``.
        :param fields: extra command fields, sent beside ``id`` and ``type``.
        :raises HaConfigurationError: for a blank type or a reserved field name.
        :raises HaAuthError: when the token was already rejected (no socket opened).
        :raises HaCommandError: when Home Assistant answered ``success: false``.
        :raises HaConnectionError: when the socket failed or the answer timed out.
        :raises HaBadResponseError: when the result is not a JSON object.
        """
        if not command_type.strip():
            raise HaConfigurationError("a Home Assistant command needs a non-empty command type")
        if "id" in fields or "type" in fields:
            raise HaConfigurationError(
                "command fields must not override the websocket envelope's 'id' or 'type'"
            )
        if self._terminal:
            detail = self._terminal_detail or "the token was rejected"
            raise HaAuthError(
                f"the Home Assistant client is already in a terminal auth-failed state — {detail}",
                status_code=401,
                detail=detail,
            )
        try:
            async with self._connect(
                self._url,
                open_timeout=self._open_timeout_s,
                close_timeout=self._close_timeout_s,
                ping_interval=self._ping_interval_s,
                ping_timeout=self._ping_timeout_s,
            ) as connection:
                await self._handshake(connection)
                return await self._exchange(connection, command_type, fields)
        except HaError:
            raise
        except TimeoutError as exc:
            raise HaConnectionError(
                f"the connection to Home Assistant at {self._url} timed out while opening it "
                f"for the {command_type!r} command"
            ) from exc
        except (WebSocketException, OSError) as exc:
            reason = self._clean(str(exc)) or type(exc).__name__
            raise HaConnectionError(
                f"the Home Assistant websocket failed while running the "
                f"{command_type!r} command: {reason}"
            ) from None

    async def start(self) -> None:
        """Start the supervisor. Safe to call twice.

        A rejected token is terminal: after :class:`HaAuthError` the supervisor is
        never resurrected, because retrying with the same token cannot help.
        """
        if self._terminal:
            _LOGGER.warning(
                "the Home Assistant token was rejected — not reconnecting; fix ha.token"
            )
            return
        if self._task_active():
            return
        self._started = True
        self._stopping = False
        self._launch_supervisor()

    async def stop(self) -> None:
        """Stop the supervisor and close the socket. Safe to call twice."""
        started = self._started
        self._started = False
        self._stopping = True
        self._control_event.set()
        task = self._task
        self._task = None
        cancelled = False
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True),
                    timeout=self._shutdown_timeout_s,
                )
            except TimeoutError:
                _LOGGER.warning("the Home Assistant websocket tasks did not stop in time")
            except asyncio.CancelledError:
                # The caller was cancelled while draining. Finish the bookkeeping
                # below and then re-raise: swallowing this would hide the shutdown
                # cancellation from the caller.
                cancelled = True
                _LOGGER.warning("stopping the Home Assistant websocket client was cancelled")
        self._attempt = 0
        self._connected = False
        if started:
            if self._terminal and self._terminal_detail:
                # A rejected token is the actionable reason; keep it instead of the
                # generic stop detail.
                self._set_health(HealthStatus.ERROR, self._terminal_detail)
            else:
                self._set_health(HealthStatus.ERROR, "connection stopped")
        if cancelled:
            raise asyncio.CancelledError

    async def aclose(self) -> None:
        """Stop the client; alias for :meth:`stop`."""
        await self.stop()

    def _task_active(self) -> bool:
        """Return whether the supervisor task exists and has not finished."""
        task = self._task
        return task is not None and not task.done()

    def _is_stopping(self) -> bool:
        """Return whether :meth:`stop` has been called."""
        return self._stopping

    def _launch_supervisor(self) -> None:
        """Create the supervisor task, or report the empty selection when there is none."""
        if self._terminal:
            _LOGGER.warning(
                "the Home Assistant token was rejected — not reconnecting; fix ha.token"
            )
            return
        if not self._entity_ids:
            self._set_health(HealthStatus.NO_ENTITIES, "no tiles selected yet")
            return
        if self._task_active():
            return
        self._task = asyncio.create_task(self._supervise(), name="ha-websocket-supervisor")

    def _set_health(self, status: HealthStatus, detail: str) -> None:
        """Write one health status through the registry.

        :param status: new status for the ``ha`` component.
        :param detail: human-readable reason shown with the header dots.
        """
        self._health.set("ha", status, detail=detail)

    def _clean(self, text: str) -> str:
        """Redact the token, scrub registered secrets and clip to a detail line.

        The literal token is replaced before the pattern rules so the configured
        value is redacted even if no scrubber registration exists yet; the result
        is whitespace-collapsed and clipped afterwards.

        :param text: text from Home Assistant or from an exception.
        """
        redacted = text.replace(self._token, REDACTED)
        return " ".join(SECRET_SCRUBBER.scrub(redacted).split())[:_DETAIL_CLIP]

    def _next_command_id(self) -> int:
        """Return the next monotonic websocket command id."""
        self._next_id += 1
        return self._next_id

    async def _supervise(self) -> None:
        """Connect, run one session, and reconnect with exponential backoff."""
        while True:
            if self._is_stopping():
                return
            if not self._entity_ids:
                self._set_health(HealthStatus.NO_ENTITIES, "no tiles selected yet")
                return
            self._set_health(
                HealthStatus.RECONNECTING,
                f"connecting to Home Assistant at {self._url}",
            )
            try:
                async with self._connect(
                    self._url,
                    open_timeout=self._open_timeout_s,
                    close_timeout=self._close_timeout_s,
                    ping_interval=self._ping_interval_s,
                    ping_timeout=self._ping_timeout_s,
                ) as connection:
                    try:
                        await self._session(connection)
                    finally:
                        # Cleared before the backoff below, so a reconnecting
                        # client never looks live to a cache reader.
                        self._connected = False
            except HaAuthError as exc:
                self._last_error = exc
                self._terminal = True
                self._terminal_detail = f"token rejected: {exc.detail}"
                self._set_health(HealthStatus.ERROR, self._terminal_detail)
                _LOGGER.error("Home Assistant rejected the token: %s", exc.detail)
                return
            except (HaError, WebSocketException, OSError, TimeoutError) as exc:
                if self._is_stopping():
                    return
                detail = self._report_unreachable(exc)
                _LOGGER.warning("Home Assistant websocket lost: %s", detail)
            except Exception as exc:
                # Deliberately not ``BaseException``: a cancelled supervisor must
                # still end, but a bug in the session must never leave the health
                # dots green while nothing is connected.
                if self._is_stopping():
                    return
                detail = self._report_unreachable(exc)
                _LOGGER.warning(
                    "the Home Assistant websocket raised an unexpected error: %s",
                    detail,
                    exc_info=True,
                )
            else:
                if self._is_stopping():
                    return
                if not self._entity_ids:
                    self._set_health(HealthStatus.NO_ENTITIES, "no tiles selected yet")
                    return
                error = HaConnectionError("the Home Assistant websocket connection closed")
                self._last_error = error
                self._set_health(HealthStatus.RECONNECTING, "unreachable: connection closed")
                _LOGGER.warning("Home Assistant websocket connection closed")
            delay = min(
                self._initial_backoff_s * 2 ** min(self._attempt, _MAX_ATTEMPT),
                self._max_backoff_s,
            )
            self._attempt = min(self._attempt + 1, _MAX_ATTEMPT)
            _LOGGER.info("reconnecting in %.1f s", delay)
            await self._sleep(delay)

    def _report_unreachable(self, exc: BaseException) -> str:
        """Record *exc* as the last error and report the connection as unreachable.

        Used by both failure branches of :meth:`_supervise`, so an unexpected
        exception is reported exactly like a transport failure instead of ending
        the supervisor while the health dots still say ``connected``.

        :param exc: the error that ended the session.
        :returns: the cleaned reason, for the log line.
        """
        self._last_error = self._classify(exc)
        detail = self._clean(str(self._last_error)) or type(exc).__name__
        self._set_health(HealthStatus.RECONNECTING, f"unreachable: {detail}")
        return detail

    def _classify(self, exc: BaseException) -> HaError:
        """Map a supervisor-level failure to a typed error.

        :param exc: the error that ended the session.
        """
        if isinstance(exc, HaError):
            return exc
        if isinstance(exc, TimeoutError):
            return HaConnectionError(f"the connection to Home Assistant at {self._url} timed out")
        reason = self._clean(str(exc)) or type(exc).__name__
        return HaConnectionError(f"the websocket to Home Assistant at {self._url} failed: {reason}")

    async def _session(self, connection: HaWebSocketConnection) -> None:
        """Run one authenticated, subscribed session until it ends.

        The control event is the only way the entity list changes while the
        socket is live; a receive timeout alone never reconnects, because the
        websocket pings are the liveness detector.

        :param connection: the open connection.
        """
        self._control_event.clear()
        await self._handshake(connection)
        entity_ids = self._entity_ids
        if not entity_ids:
            return
        subscription_id = await self._subscribe(connection, entity_ids)
        current_ids = entity_ids
        recv_task = asyncio.create_task(connection.recv())
        control_task = asyncio.create_task(self._control_event.wait())
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {recv_task, control_task},
                    timeout=self._receive_timeout_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    continue
                if recv_task in done:
                    message = self._recv_result(recv_task)
                    self._handle_frame(message, subscription_id)
                    recv_task = asyncio.create_task(connection.recv())
                if control_task in done:
                    self._control_event.clear()
                    if self._stopping:
                        return
                    entity_ids = self._entity_ids
                    if not entity_ids:
                        return
                    if entity_ids != current_ids:
                        # The pending receive would otherwise steal the subscription
                        # result, so it is parked while the control exchange runs.
                        recv_task.cancel()
                        try:
                            await recv_task
                        except asyncio.CancelledError:
                            # Only the cancellation of the parked receive is absorbed;
                            # stopping or a caller cancelling the session must still
                            # propagate instead of being eaten by this window.
                            if self._own_cancellation():
                                raise
                        subscription_id = await self._resubscribe(
                            connection, subscription_id, entity_ids
                        )
                        current_ids = entity_ids
                        recv_task = asyncio.create_task(connection.recv())
                    control_task = asyncio.create_task(self._control_event.wait())
        finally:
            recv_task.cancel()
            control_task.cancel()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(recv_task, control_task, return_exceptions=True),
                    timeout=self._shutdown_timeout_s,
                )

    def _own_cancellation(self) -> bool:
        """Return whether the running task itself was cancelled from outside.

        Cancelling the parked receive task and awaiting it raises
        :class:`asyncio.CancelledError` in this coroutine, which is expected and
        must be suppressed. A cancellation delivered to the task running this
        coroutine (``stop()``, or any caller) is not.
        """
        if self._is_stopping():
            return True
        task = asyncio.current_task()
        return task is not None and task.cancelling() > 0

    async def _resubscribe(
        self,
        connection: HaWebSocketConnection,
        subscription_id: int,
        entity_ids: tuple[str, ...],
    ) -> int:
        """Replace the current subscription after the entity list changed.

        The unsubscribe is best effort: if the socket cannot carry it, the next
        subscribe attempt reports the failure and the supervisor reconnects.

        :param connection: the open connection.
        :param subscription_id: id of the subscription to drop.
        :param entity_ids: new, non-empty entity ids.
        """
        with contextlib.suppress(WebSocketException, OSError):
            await connection.send(
                json.dumps(
                    {
                        "id": self._next_command_id(),
                        "type": "unsubscribe_events",
                        "subscription": subscription_id,
                    }
                )
            )
        return await self._subscribe(connection, entity_ids)

    def _recv_result(self, task: asyncio.Task[str | bytes]) -> str | bytes:
        """Return the frame a finished receive task produced, mapping transport errors.

        :param task: the finished ``recv`` task.
        :raises HaConnectionError: if the socket failed or closed.
        """
        try:
            return task.result()
        except WebSocketException as exc:
            reason = self._clean(str(exc)) or type(exc).__name__
            raise HaConnectionError(f"the Home Assistant websocket failed: {reason}") from None
        except OSError as exc:
            reason = self._clean(str(exc)) or type(exc).__name__
            raise HaConnectionError(f"the Home Assistant websocket failed: {reason}") from None

    async def _handshake(self, connection: HaWebSocketConnection) -> None:
        """Perform the auth handshake, bounded in total and per frame.

        A repeated ``auth_required`` re-sends the auth frame; the outer timeout
        bounds that loop. A rejected token is terminal.

        :param connection: the open connection.
        :raises HaAuthError: if Home Assistant rejected the token.
        :raises HaConnectionError: if the handshake could not be completed.
        """
        try:
            async with asyncio.timeout(self._handshake_timeout_s):
                while True:
                    message = await asyncio.wait_for(connection.recv(), self._handshake_timeout_s)
                    payload = self._decode(message)
                    if not isinstance(payload, Mapping):
                        raise HaConnectionError(
                            "Home Assistant sent a websocket frame that is not a JSON "
                            "object during the handshake"
                        )
                    message_type = payload.get("type")
                    if message_type == "auth_required":
                        await connection.send(
                            json.dumps({"type": "auth", "access_token": self._token})
                        )
                        continue
                    if message_type == "auth_ok":
                        return
                    if message_type == "auth_invalid":
                        detail = self._auth_detail(payload.get("message")) or "no reason given"
                        raise HaAuthError(
                            "Home Assistant rejected the websocket token — the long-lived "
                            "token in ha.token is wrong, expired, revoked, or belongs to a "
                            f"user without permission ({detail})",
                            status_code=401,
                            detail=detail,
                        ) from None
                    raise HaConnectionError(
                        f"{_HANDSHAKE_UNEXPECTED} {self._clean(str(message_type))!r}"
                    )
        except TimeoutError as exc:
            raise HaConnectionError(
                "the Home Assistant websocket handshake did not finish within "
                f"{self._handshake_timeout_s:g} s"
            ) from exc

    def _auth_detail(self, raw: object) -> str:
        """Render the ``message`` of an ``auth_invalid`` frame defensively.

        Home Assistant has shipped both a string and an object here, so both are
        accepted; the result is cleaned before it leaves the client.

        :param raw: the ``message`` value of the frame.
        """
        if isinstance(raw, str):
            text = raw
        elif isinstance(raw, Mapping):
            text = " ".join(str(value) for value in raw.values() if value is not None)
        elif raw is None:
            text = ""
        else:
            text = str(raw)
        return self._clean(text)

    async def _subscribe(
        self, connection: HaWebSocketConnection, entity_ids: tuple[str, ...]
    ) -> int:
        """Subscribe to *entity_ids* and wait for the matching result.

        The list is always non-empty: Home Assistant treats an empty
        ``entity_ids`` list as "every entity", which is never what this client
        wants. A successful result clears the cache (the following add burst
        re-seeds it), resets the backoff and reports ``connected``.

        :param connection: the open connection.
        :param entity_ids: non-empty entity ids.
        :raises HaConnectionError: if Home Assistant refused or did not answer.
        """
        subscription_id = self._next_command_id()
        await connection.send(
            json.dumps(
                {
                    "id": subscription_id,
                    "type": "subscribe_entities",
                    "entity_ids": list(entity_ids),
                }
            )
        )
        try:
            async with asyncio.timeout(self._handshake_timeout_s):
                while True:
                    message = await asyncio.wait_for(connection.recv(), self._handshake_timeout_s)
                    payload = self._decode(message)
                    if not isinstance(payload, Mapping):
                        continue
                    if payload.get("type") != "result":
                        _LOGGER.debug(
                            "ignoring websocket frame of type %r while waiting for the "
                            "subscription result",
                            payload.get("type"),
                        )
                        continue
                    if payload.get("id") != subscription_id:
                        _LOGGER.debug("ignoring a result for another subscription id")
                        continue
                    if payload.get("success") is True:
                        self._states.clear()
                        self._attempt = 0
                        self._connected = True
                        self._set_health(
                            HealthStatus.CONNECTED,
                            f"subscribed to {len(entity_ids)} entities",
                        )
                        return subscription_id
                    reason = self._subscribe_error(payload.get("error"))
                    raise HaConnectionError(
                        f"Home Assistant refused the entity subscription ({reason}) — "
                        "check the entity ids in Settings"
                    )
        except TimeoutError as exc:
            raise HaConnectionError(
                "Home Assistant did not answer the entity subscription within "
                f"{self._handshake_timeout_s:g} s"
            ) from exc

    async def _exchange(
        self,
        connection: HaWebSocketConnection,
        command_type: str,
        fields: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Send one command and wait for the matching ``result`` frame.

        Frames that are not results, or that carry another command id, are
        ignored at DEBUG (Home Assistant sends ``ping`` and subscription events on
        the same socket); the wait is bounded by ``command_timeout_s``.

        :param connection: the authenticated connection.
        :param command_type: Home Assistant command type.
        :param fields: extra command fields.
        :raises HaCommandError: when Home Assistant answered ``success: false``.
        :raises HaConnectionError: when the answer timed out.
        :raises HaBadResponseError: when the result is not a JSON object.
        """
        command_id = self._next_command_id()
        payload: dict[str, object] = {"id": command_id, "type": command_type}
        payload.update(fields)
        _LOGGER.debug("sending websocket command %r (id %d)", command_type, command_id)
        await connection.send(json.dumps(payload))
        try:
            async with asyncio.timeout(self._command_timeout_s):
                while True:
                    message = await asyncio.wait_for(connection.recv(), self._command_timeout_s)
                    frame = self._decode(message)
                    if not isinstance(frame, Mapping):
                        continue
                    if frame.get("type") != "result":
                        _LOGGER.debug(
                            "ignoring websocket frame of type %r while waiting for the "
                            "%r command result",
                            frame.get("type"),
                            command_type,
                        )
                        continue
                    if frame.get("id") != command_id:
                        _LOGGER.debug("ignoring a result for another command id")
                        continue
                    if frame.get("success") is True:
                        result = frame.get("result")
                        if not isinstance(result, Mapping):
                            raise HaBadResponseError(
                                f"the {command_type!r} command answered with a result that is "
                                "not a JSON object"
                            )
                        return result
                    code, reason = self._command_error(frame.get("error"))
                    code_label = f"{code}: " if code is not None else ""
                    raise HaCommandError(
                        f"Home Assistant refused the {command_type!r} command "
                        f"({code_label}{reason})",
                        code=code,
                        detail=reason,
                    )
        except TimeoutError as exc:
            raise HaConnectionError(
                f"Home Assistant did not answer the {command_type!r} command within "
                f"{self._command_timeout_s:g} s"
            ) from exc

    def _command_error(self, error: object) -> tuple[str | None, str]:
        """Render a failed command's ``error`` object into a code and a reason.

        The reason reuses :meth:`_subscribe_error`, which already joins Home
        Assistant's own text, redacts the token and the registered secrets, and
        clips the line, so a command failure cannot leak anything a subscription
        failure would not.

        :param error: the ``error`` value of a failed result frame.
        """
        code: str | None = None
        if isinstance(error, Mapping):
            raw_code = error.get("code")
            if isinstance(raw_code, str) and raw_code.strip():
                code = raw_code.strip()
        return code, self._subscribe_error(error)

    def _subscribe_error(self, error: object) -> str:
        """Render a subscription error object or string defensively.

        :param error: the ``error`` value of a failed result frame.
        """
        if isinstance(error, Mapping):
            parts = [
                value.strip()
                for value in error.values()
                if isinstance(value, str) and value.strip()
            ]
            text = ": ".join(parts)
        elif isinstance(error, str):
            text = error
        elif error is None:
            text = ""
        else:
            text = str(error)
        return self._clean(text) or "no reason given"

    def _decode(self, message: str | bytes) -> object:
        """Decode one websocket frame, warning when it is not usable JSON.

        :param message: text or binary frame.
        """
        try:
            text = message.decode("utf-8") if isinstance(message, bytes) else message
            return json.loads(text)
        except (RecursionError, UnicodeDecodeError, ValueError) as exc:
            # A pathologically nested frame exhausts the recursion limit inside the
            # JSON scanner; that is still one bad frame, not a reason to reconnect.
            _LOGGER.warning(
                "ignoring a websocket frame that is not valid JSON: %s", self._clean(str(exc))
            )
            return None

    def _handle_frame(self, message: str | bytes, subscription_id: int) -> None:
        """Handle one frame from the receive loop.

        Only ``event`` frames for the current subscription are processed; pongs,
        results and anything else are ignored at DEBUG.

        :param message: text or binary frame.
        :param subscription_id: id of the live subscription.
        """
        payload = self._decode(message)
        if payload is None:
            return
        if not isinstance(payload, Mapping):
            _LOGGER.warning("ignoring a websocket frame that is not a JSON object")
            return
        if payload.get("type") != "event":
            _LOGGER.debug("ignoring websocket frame of type %r", payload.get("type"))
            return
        if payload.get("id") != subscription_id:
            _LOGGER.debug("ignoring an event for subscription %r", payload.get("id"))
            return
        event = payload.get("event")
        if not isinstance(event, Mapping):
            _LOGGER.warning("ignoring a subscribe_entities event that is not a JSON object")
            return
        self._apply_event(event)

    def _apply_event(self, event: Mapping[str, object]) -> None:
        """Apply the add/change/remove keys of one event, warning on the rest.

        :param event: the ``event`` object of a subscribe_entities frame.
        """
        for key, value in event.items():
            if key == "a":
                self._apply_add(value)
            elif key == "c":
                self._apply_change(value)
            elif key == "r":
                self._apply_remove(value)
            else:
                _LOGGER.warning("ignoring unknown subscribe_entities delta key %r", key)

    def _apply_add(self, value: object) -> None:
        """Seed the cache from an ``a`` container.

        :param value: the value of the ``a`` key.
        """
        if not isinstance(value, Mapping):
            _LOGGER.warning("ignoring a subscribe_entities addition that is not a JSON object")
            return
        for entity_id, data in value.items():
            if not isinstance(entity_id, str) or not isinstance(data, Mapping):
                _LOGGER.warning("ignoring a malformed subscribe_entities addition entry")
                continue
            state = data.get("s")
            attributes = data.get("a", {})
            if not isinstance(state, str):
                _LOGGER.warning("ignoring an addition for %r without a string state", entity_id)
                continue
            if not isinstance(attributes, Mapping):
                _LOGGER.warning("ignoring an addition for %r with malformed attributes", entity_id)
                continue
            last_changed = _timestamp(data.get("lc"))
            raw_updated = data.get("lu")
            last_updated = last_changed if raw_updated is None else _timestamp(raw_updated)
            self._store(
                HaEntityState(
                    entity_id=entity_id,
                    state=state,
                    attributes=attributes,
                    last_changed=last_changed,
                    last_updated=last_updated,
                )
            )

    def _apply_change(self, value: object) -> None:
        """Merge a ``c`` container into the cache.

        :param value: the value of the ``c`` key.
        """
        if not isinstance(value, Mapping):
            _LOGGER.warning("ignoring a subscribe_entities change that is not a JSON object")
            return
        for entity_id, delta in value.items():
            if not isinstance(entity_id, str) or not isinstance(delta, Mapping):
                _LOGGER.warning("ignoring a malformed subscribe_entities change entry")
                continue
            current = self._states.get(entity_id)
            if current is None:
                _LOGGER.warning("ignoring a change for unknown entity %r", entity_id)
                continue
            merged = self._merge_change(current, delta, entity_id)
            if merged is not None:
                self._store(merged)

    def _merge_change(
        self,
        current: HaEntityState,
        delta: Mapping[str, object],
        entity_id: str,
    ) -> HaEntityState | None:
        """Apply one entity's ``+``/``-`` delta to its cached state.

        A change that carries only ``lc`` preserves the cached ``lu`` (and the
        other way round): the compressed format omits the timestamp that did not
        move, so starting from the cached values is the whole rule.

        :param current: cached state of the entity.
        :param delta: the per-entity change object.
        :param entity_id: id the delta belongs to.
        """
        state = current.state
        attributes = dict(current.attributes)
        last_changed = current.last_changed
        last_updated = current.last_updated
        for key in delta:
            if key not in {"+", "-"}:
                _LOGGER.warning(
                    "ignoring unknown subscribe_entities delta key %r for %r", key, entity_id
                )
        addition = delta.get("+")
        if addition is not None:
            if not isinstance(addition, Mapping):
                _LOGGER.warning("ignoring a malformed addition for %r", entity_id)
                return None
            for key, value in addition.items():
                if key == "s":
                    if isinstance(value, str):
                        state = value
                    else:
                        _LOGGER.warning("ignoring a non-string state for %r", entity_id)
                elif key == "lc":
                    parsed = _timestamp(value)
                    if parsed is not None:
                        last_changed = parsed
                elif key == "lu":
                    parsed = _timestamp(value)
                    if parsed is not None:
                        last_updated = parsed
                elif key == "a":
                    if isinstance(value, Mapping):
                        attributes.update(value)
                    else:
                        _LOGGER.warning("ignoring malformed attributes for %r", entity_id)
                elif key == "c":
                    continue
                else:
                    _LOGGER.warning(
                        "ignoring unknown subscribe_entities delta field %r for %r",
                        key,
                        entity_id,
                    )
        removal = delta.get("-")
        if removal is not None:
            if not isinstance(removal, Mapping):
                _LOGGER.warning("ignoring a malformed removal for %r", entity_id)
                return None
            for key, value in removal.items():
                if key != "a":
                    _LOGGER.warning(
                        "ignoring unknown subscribe_entities removal field %r for %r",
                        key,
                        entity_id,
                    )
                    continue
                if not isinstance(value, list):
                    _LOGGER.warning("ignoring a malformed attribute removal for %r", entity_id)
                    continue
                for name in value:
                    if isinstance(name, str):
                        attributes.pop(name, None)
                    else:
                        _LOGGER.warning("ignoring a non-string attribute name for %r", entity_id)
        return HaEntityState(
            entity_id=entity_id,
            state=state,
            attributes=attributes,
            last_changed=last_changed,
            last_updated=last_updated,
        )

    def _apply_remove(self, value: object) -> None:
        """Evict entities named in an ``r`` container and report them as removed.

        :param value: the value of the ``r`` key.
        """
        if not isinstance(value, list):
            _LOGGER.warning("ignoring a subscribe_entities removal that is not a JSON array")
            return
        for entity_id in value:
            if not isinstance(entity_id, str):
                _LOGGER.warning("ignoring a non-string entity id in a removal")
                continue
            old = self._states.pop(entity_id, None)
            if old is None:
                _LOGGER.warning("ignoring a removal for unknown entity %r", entity_id)
                continue
            self._emit(
                HaEntityState(
                    entity_id=entity_id,
                    state=None,
                    attributes={},
                    last_changed=old.last_changed,
                    last_updated=old.last_updated,
                )
            )

    def _store(self, state: HaEntityState) -> None:
        """Cache *state* and notify the callback.

        :param state: merged state to store.
        """
        self._states[state.entity_id] = state
        self._emit(state)

    def _emit(self, state: HaEntityState) -> None:
        """Call the state callback, logging instead of propagating its errors.

        :param state: state to hand to the callback.
        """
        callback = self._on_state_change
        if callback is None:
            return
        try:
            callback(state)
        except Exception:
            _LOGGER.warning(
                "the entity state callback raised for %r", state.entity_id, exc_info=True
            )
