"""Fully offline tests for the Home Assistant WebSocket state client.

Every connection is an in-memory fake that speaks the compressed
``subscribe_entities`` format, so the tests prove the handshake, the diff merge,
the reconnect policy and token secrecy without a socket. One ``integration``-marked
test exercises a real loopback websocket when a local port can be bound.
Owning ticket: HSD-a21l.
"""

from __future__ import annotations

import asyncio
import json
import logging
import operator
import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Final, cast

import pytest
from log_isolation import preserve_log_state
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosedError

from chime_deck.config import REDACTED, SECRET_SCRUBBER
from chime_deck.ha import (
    DEFAULT_RECONNECT_INITIAL_S,
    DEFAULT_RECONNECT_MAX_S,
    ConnectionFactory,
    HaAuthError,
    HaConfigurationError,
    HaConnectionError,
    HaEntityState,
    HaWebSocketConnection,
    HomeAssistantWebSocketClient,
    websocket_url,
)
from chime_deck.health import HealthRegistry, HealthStatus

pytestmark = pytest.mark.asyncio

TOKEN: Final = "secret-long-lived-token-abc123"
"""Any occurrence of this string outside the auth frame is a failure."""

BASE_URL: Final = "http://ha.test:8123"
FRONT_DOOR: Final = "sensor.front_door"
GARAGE: Final = "cover.garage"
LOGGER_NAME: Final = "chime_deck.ha.websocket"

_WAIT_TIMEOUT_S: Final = 5.0
"""Upper bound for every helper poll; no test sleeps indefinitely."""

_POLL_INTERVAL_S: Final = 0.005
"""Seconds between predicate checks inside :func:`_wait_until`."""

_LC_FRONT: Final = 1_714_557_600.0
_LC_GARAGE: Final = 1_714_550_000.0

_DEEP_NESTING: Final = 10_000
"""Bracket depth that exhausts the JSON scanner's recursion limit in every interpreter."""


def _iso(epoch: float) -> str:
    """Render an epoch second the way the client renders compressed timestamps.

    :param epoch: seconds since the Unix epoch, UTC.
    """
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _assert_token_absent(text: str) -> None:
    """Fail if the long-lived token appears in *text*.

    :param text: any message, repr or traceback that leaves the client.
    :raises AssertionError: if the token is in *text*.
    """
    if TOKEN in text:
        raise AssertionError("the Home Assistant token appears in text that leaves the client")


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = _WAIT_TIMEOUT_S,
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


def _state_is(
    client: HomeAssistantWebSocketClient,
    entity_id: str,
    state: str | None,
) -> bool:
    """Return whether *entity_id* is cached with exactly *state*.

    :param client: client under test.
    :param entity_id: entity to look up.
    :param state: expected state value.
    """
    cached = client.get_state(entity_id)
    return cached is not None and cached.state == state


def _cached(client: HomeAssistantWebSocketClient, entity_id: str) -> HaEntityState:
    """Return the cached state, failing when the entity is missing.

    :param client: client under test.
    :param entity_id: entity to look up.
    """
    cached = client.get_state(entity_id)
    if cached is None:
        raise AssertionError(f"{entity_id} is not cached")
    return cached


def _transitions(records: list[HealthStatus]) -> list[HealthStatus]:
    """Collapse consecutive duplicate statuses so transitions are easy to assert.

    :param records: every status the registry reported, in order.
    """
    transitions: list[HealthStatus] = []
    for status in records:
        if not transitions or transitions[-1] != status:
            transitions.append(status)
    return transitions


async def _instant_sleep(_delay: float) -> None:
    """Reconnect immediately, so a reconnect test needs no real delay.

    :param _delay: the delay the supervisor requested; ignored.
    """
    return None


def _recording_sleep(
    delays: list[float],
    *,
    block_after: int | None = None,
) -> Callable[[float], Awaitable[None]]:
    """Build a sleep that records every delay and can block the last one.

    The blocked branch waits on a gate that is never set, so it is only released
    by cancelling the supervisor — which is exactly what ``client.stop()`` does.
    The bounded wait is a safety net so a test cannot hang on the sleep itself.

    :param delays: list the requested delays are appended to.
    :param block_after: when set, the task blocks once this many delays were
        recorded, so a test can stop the client mid-backoff.
    """
    gate = asyncio.Event()

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if block_after is None or len(delays) < block_after:
            return
        try:
            await asyncio.wait_for(gate.wait(), timeout=_WAIT_TIMEOUT_S)
        except TimeoutError:
            # The gate is never set: the bound expiring is not a test failure, and
            # returning normally keeps the wait from leaking an unretrieved error.
            return

    return sleep


def _record_health() -> tuple[HealthRegistry, list[HealthStatus]]:
    """Build a registry that records every reported status."""
    registry = HealthRegistry()
    records: list[HealthStatus] = []
    registry.set_on_change(lambda: records.append(registry.get("ha")))
    return registry, records


class FakeConnection:
    """An in-memory Home Assistant websocket peer driven frame by frame.

    The peer answers the handshake and the subscription automatically and
    records every frame the client sends, so tests assert on the wire protocol
    without a socket.
    """

    def __init__(
        self,
        *,
        auth: str = "auth_ok",
        auth_message: str | Mapping[str, object] | None = None,
        after_subscribe: Iterable[Mapping[str, object]] = (),
        subscribe_success: bool = True,
        subscribe_error: Mapping[str, object] | str | None = None,
    ) -> None:
        """Create a peer that starts by asking the client to authenticate.

        :param auth: ``auth_ok`` or ``auth_invalid``.
        :param auth_message: ``message`` field sent with ``auth_invalid``.
        :param after_subscribe: event payloads injected after a successful subscribe.
        :param subscribe_success: whether the subscription result succeeds.
        :param subscribe_error: ``error`` field of a failed subscription result.
        """
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self._auth = auth
        self._auth_message = auth_message
        self._after_subscribe = list(after_subscribe)
        self._subscribe_success = subscribe_success
        self._subscribe_error = subscribe_error
        self._subscription_id = 0
        self._queue: asyncio.Queue[str | bytes | Exception] = asyncio.Queue()
        self._queue.put_nowait(json.dumps({"type": "auth_required"}))

    async def send(self, message: str) -> None:
        """Record one frame and answer it the way Home Assistant would.

        :param message: already-encoded JSON frame.
        """
        frame = cast("dict[str, object]", json.loads(message))
        self.sent.append(frame)
        frame_type = frame.get("type")
        if frame_type == "auth":
            self._queue.put_nowait(self._auth_reply())
        elif frame_type == "subscribe_entities":
            self._subscription_id = cast("int", frame.get("id"))
            self._queue.put_nowait(self._subscribe_reply())
            for event in self._after_subscribe:
                self.push_event(event)
            # The burst answers the first subscription; a later resubscribe gets
            # only the frames the test pushes itself.
            self._after_subscribe.clear()
        elif frame_type == "unsubscribe_events":
            self._queue.put_nowait(
                json.dumps({"id": frame.get("id"), "type": "result", "success": True})
            )

    async def recv(self) -> str | bytes:
        """Return the next scripted frame, raising a scripted exception in place."""
        item = await self._queue.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        """Mark the connection closed; the fake has no close handshake."""
        self.closed = True

    def push_event(self, event: Mapping[str, object]) -> None:
        """Queue one ``event`` frame for the current subscription.

        :param event: the compressed add/change/remove payload.
        """
        self.push_frame({"id": self._subscription_id, "type": "event", "event": event})

    def push_frame(self, frame: Mapping[str, object]) -> None:
        """Queue one JSON frame verbatim.

        :param frame: frame to send to the client.
        """
        self._queue.put_nowait(json.dumps(frame))

    def push_text(self, text: str) -> None:
        """Queue one raw text frame.

        :param text: frame contents, possibly invalid JSON.
        """
        self._queue.put_nowait(text)

    def push_error(self, error: Exception) -> None:
        """Make the next receive raise *error*.

        :param error: transport failure to inject.
        """
        self._queue.put_nowait(error)

    def frames_of_type(self, frame_type: str) -> list[dict[str, object]]:
        """Return every recorded frame of *frame_type*.

        :param frame_type: value of the frame's ``type`` field.
        """
        return [frame for frame in self.sent if frame.get("type") == frame_type]

    def _auth_reply(self) -> str:
        """Return the frame Home Assistant sends after the auth frame."""
        if self._auth == "auth_ok":
            return json.dumps({"type": "auth_ok"})
        payload: dict[str, object] = {"type": "auth_invalid"}
        if self._auth_message is not None:
            payload["message"] = self._auth_message
        return json.dumps(payload)

    def _subscribe_reply(self) -> str:
        """Return the result frame for the current subscription."""
        payload: dict[str, object] = {
            "id": self._subscription_id,
            "type": "result",
            "success": self._subscribe_success,
        }
        if self._subscribe_success:
            payload["result"] = None
        elif self._subscribe_error is not None:
            payload["error"] = self._subscribe_error
        return json.dumps(payload)


class _ConnectionContext:
    """Async context manager that yields one fake connection."""

    def __init__(self, connection: FakeConnection) -> None:
        """Wrap *connection*.

        :param connection: peer returned to the client.
        """
        self._connection = connection

    async def __aenter__(self) -> HaWebSocketConnection:
        """Yield the fake connection."""
        return self._connection

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the fake connection.

        :param exc_info: the exception triple, unused.
        """
        await self._connection.close()


class _RaisingContext:
    """Async context manager that fails while opening the connection."""

    def __init__(self, error: Exception) -> None:
        """Store the failure to raise.

        :param error: exception raised by ``__aenter__``.
        """
        self._error = error

    async def __aenter__(self) -> HaWebSocketConnection:
        """Raise the scripted failure."""
        raise self._error

    async def __aexit__(self, *exc_info: object) -> None:
        """Return without doing anything.

        :param exc_info: the exception triple, unused.
        """
        return None


class FakeFactory:
    """Replay a scripted list of sessions and record every connection attempt."""

    def __init__(self, *sessions: FakeConnection | Exception) -> None:
        """Create the factory from the scripted sessions.

        :param sessions: fake connections, or exceptions raised while opening.
        """
        self.sessions = list(sessions)
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        url: str,
        *,
        open_timeout: float,
        close_timeout: float,
        ping_interval: float,
        ping_timeout: float,
    ) -> AbstractAsyncContextManager[HaWebSocketConnection]:
        """Record the attempt and return the next scripted session.

        :param url: endpoint the client asked for.
        :param open_timeout: open timeout the client passed.
        :param close_timeout: close timeout the client passed.
        :param ping_interval: ping interval the client passed.
        :param ping_timeout: ping timeout the client passed.
        """
        self.calls.append(
            {
                "url": url,
                "open_timeout": open_timeout,
                "close_timeout": close_timeout,
                "ping_interval": ping_interval,
                "ping_timeout": ping_timeout,
            }
        )
        if not self.sessions:
            raise AssertionError("the client opened more connections than the test scripted")
        session = self.sessions.pop(0)
        if isinstance(session, Exception):
            return _RaisingContext(session)
        return _ConnectionContext(session)


class FailingFactory:
    """Fail every connection attempt and count the attempts."""

    def __init__(self, error: Exception | None = None) -> None:
        """Create a factory that always raises.

        :param error: failure raised while opening; a connection error by default.
        """
        self.calls = 0
        self._error = error if error is not None else HaConnectionError("connection refused")

    def __call__(
        self,
        url: str,
        *,
        open_timeout: float,
        close_timeout: float,
        ping_interval: float,
        ping_timeout: float,
    ) -> AbstractAsyncContextManager[HaWebSocketConnection]:
        """Count the attempt and return a context that raises.

        :param url: endpoint the client asked for; unused.
        :param open_timeout: open timeout the client passed; unused.
        :param close_timeout: close timeout the client passed; unused.
        :param ping_interval: ping interval the client passed; unused.
        :param ping_timeout: ping timeout the client passed; unused.
        """
        self.calls += 1
        return _RaisingContext(self._error)


def make_client(
    factory: ConnectionFactory,
    *,
    entity_ids: Iterable[str] = (FRONT_DOOR,),
    health: HealthRegistry | None = None,
    on_state_change: Callable[[HaEntityState], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    initial_backoff_s: float = DEFAULT_RECONNECT_INITIAL_S,
    max_backoff_s: float = DEFAULT_RECONNECT_MAX_S,
) -> HomeAssistantWebSocketClient:
    """Build a client wired to a fake factory.

    :param factory: opens one fake connection.
    :param entity_ids: entities to subscribe to.
    :param health: registry to report through.
    :param on_state_change: callback for cached state changes.
    :param sleep: reconnect delay used by the supervisor.
    :param initial_backoff_s: first reconnect delay.
    :param max_backoff_s: upper bound for the reconnect delay.
    """
    return HomeAssistantWebSocketClient(
        base_url=BASE_URL,
        token=TOKEN,
        entity_ids=entity_ids,
        health=health,
        on_state_change=on_state_change,
        connect=factory,
        sleep=sleep,
        initial_backoff_s=initial_backoff_s,
        max_backoff_s=max_backoff_s,
    )


_INITIAL_BURST: Final[Mapping[str, object]] = {
    "a": {
        FRONT_DOOR: {
            "s": "open",
            "a": {"friendly_name": "Front Door", "battery": 91},
            "c": {"id": "01HZX", "parent_id": None, "user_id": None},
            "lc": _LC_FRONT,
            "lu": _LC_FRONT + 30.0,
        },
        GARAGE: {"s": "closed", "a": {}, "c": None, "lc": _LC_GARAGE, "lu": _LC_GARAGE},
    }
}
"""A real initial add burst for two entities, including the ignored ``c`` field."""


# ------------------------------------------------------------------------- endpoint


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (BASE_URL, "ws://ha.test:8123/api/websocket"),
        ("https://ha.test/x", "wss://ha.test/x/api/websocket"),
        ("http://ha.test:8123/api", "ws://ha.test:8123/api/websocket"),
        ("https://ha.test:8123/", "wss://ha.test:8123/api/websocket"),
        ("HTTPS://HA.Test:8123", "wss://HA.Test:8123/api/websocket"),
    ],
)
async def test_websocket_url_maps_the_scheme_and_keeps_the_prefix(
    base_url: str, expected: str
) -> None:
    """``http`` maps to ``ws``, ``https`` to ``wss``, and ``/api/websocket`` is appended."""
    assert websocket_url(base_url) == expected


@pytest.mark.parametrize(
    "base_url",
    ["", "   ", "ftp://ha.test", "ws://ha.test:8123", "http://ha.test:0", "http://ha.test/x?y=1"],
)
async def test_websocket_url_rejects_unusable_values(base_url: str) -> None:
    """The normalisation errors from the REST client apply unchanged."""
    with pytest.raises(HaConfigurationError):
        websocket_url(base_url)


# ------------------------------------------------------------------- diff handling


async def test_initial_burst_changed_delta_and_removal() -> None:
    """An add seeds the cache, a change merges, and a removal evicts and reports it."""
    connection = FakeConnection(after_subscribe=[_INITIAL_BURST])
    changes: list[HaEntityState] = []
    client = make_client(FakeFactory(connection), on_state_change=changes.append)
    try:
        await client.start()
        await _wait_until(lambda: client.get_state(GARAGE) is not None)
        front_door = _cached(client, FRONT_DOOR)
        assert front_door.state == "open"
        assert dict(front_door.attributes) == {"friendly_name": "Front Door", "battery": 91}
        assert front_door.last_changed == _iso(_LC_FRONT)
        assert front_door.last_updated == _iso(_LC_FRONT + 30.0)
        assert {change.entity_id for change in changes} == {FRONT_DOOR, GARAGE}

        connection.push_event(
            {
                "c": {
                    FRONT_DOOR: {
                        "+": {"s": "closed", "a": {"last_seen": "now"}},
                        "-": {"a": ["battery"]},
                    }
                }
            }
        )
        await _wait_until(lambda: _state_is(client, FRONT_DOOR, "closed"))
        merged = _cached(client, FRONT_DOOR)
        assert dict(merged.attributes) == {"friendly_name": "Front Door", "last_seen": "now"}
        assert merged.last_changed == _iso(_LC_FRONT)
        assert merged.last_updated == _iso(_LC_FRONT + 30.0)

        connection.push_event({"r": [GARAGE]})
        await _wait_until(lambda: client.get_state(GARAGE) is None)
        removed = changes[-1]
        assert removed.entity_id == GARAGE
        assert removed.state is None
        assert dict(removed.attributes) == {}
        assert removed.last_changed == _iso(_LC_GARAGE)
        assert removed.last_updated == _iso(_LC_GARAGE)
    finally:
        await client.stop()


async def test_disconnect_reconnects_and_reseeds_the_cache() -> None:
    """A mid-stream drop reconnects and the fresh burst removes an offline entity."""
    stale = {
        "a": {
            FRONT_DOOR: {"s": "open", "a": {}, "lc": _LC_FRONT},
            GARAGE: {"s": "closed", "a": {}, "lc": _LC_GARAGE},
        }
    }
    fresh = {
        "a": {
            FRONT_DOOR: {
                "s": "closed",
                "a": {"fresh": True},
                "lc": _LC_FRONT + 100.0,
                "lu": _LC_FRONT + 100.0,
            }
        }
    }
    first = FakeConnection(after_subscribe=[stale])
    second = FakeConnection(after_subscribe=[fresh])
    factory = FakeFactory(first, second)
    delays: list[float] = []
    health, _ = _record_health()
    client = make_client(
        factory, health=health, sleep=_recording_sleep(delays), initial_backoff_s=0.01
    )
    try:
        await client.start()
        await _wait_until(lambda: client.get_state(GARAGE) is not None)
        first.push_error(ConnectionClosedError(None, None))
        await _wait_until(
            lambda: (
                len(factory.calls) >= 2 and first.closed and _state_is(client, FRONT_DOOR, "closed")
            )
        )
        assert client.get_state(GARAGE) is None
        assert _cached(client, FRONT_DOOR).last_changed == _iso(_LC_FRONT + 100.0)
        assert delays == [0.01]
        assert health.get("ha") is HealthStatus.CONNECTED
    finally:
        await client.stop()


async def test_malformed_frames_warn_and_do_not_stop_the_stream(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every malformed or unknown delta is a warning; a valid event afterwards still applies."""
    connection = FakeConnection(after_subscribe=[_INITIAL_BURST])
    client = make_client(FakeFactory(connection))
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    try:
        await client.start()
        await _wait_until(lambda: client.get_state(GARAGE) is not None)
        connection.push_text(json.dumps({"id": 1, "type": "event", "event": []}))
        connection.push_event({"x": 1})
        connection.push_event({"c": []})
        connection.push_event({"c": {"sensor.unknown": {"+": {"s": "on"}}}})
        connection.push_event({"c": {FRONT_DOOR: {"+": {"s": 7}}}})
        connection.push_event({"a": {GARAGE: {"s": "on", "a": []}}})
        connection.push_event({"r": "nope"})
        connection.push_event({"r": ["sensor.unknown"]})
        connection.push_text("{not json")
        connection.push_text("[1, 2]")
        connection.push_event({"c": {FRONT_DOOR: {"+": {"s": "sentinel"}}}})
        await _wait_until(lambda: _state_is(client, FRONT_DOOR, "sentinel"))
        assert _cached(client, GARAGE).state == "closed"
        warnings = [
            record
            for record in caplog.records
            if record.name == LOGGER_NAME and record.levelno >= logging.WARNING
        ]
        assert len(warnings) >= 8
        assert connection.closed is False
    finally:
        await client.stop()


async def test_a_pathologically_nested_frame_is_only_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A frame that exhausts the JSON scanner's recursion is one bad frame, not a drop."""
    connection = FakeConnection(after_subscribe=[_INITIAL_BURST])
    client = make_client(FakeFactory(connection))
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    try:
        await client.start()
        await _wait_until(lambda: client.get_state(GARAGE) is not None)
        connection.push_text("[" * _DEEP_NESTING + "]" * _DEEP_NESTING)
        connection.push_event({"c": {FRONT_DOOR: {"+": {"s": "sentinel"}}}})
        await _wait_until(lambda: _state_is(client, FRONT_DOOR, "sentinel"))
        warnings = [
            record
            for record in caplog.records
            if record.name == LOGGER_NAME and record.levelno >= logging.WARNING
        ]
        assert len(warnings) == 1
        assert connection.closed is False
    finally:
        await asyncio.wait_for(client.stop(), timeout=1.0)


async def test_merge_keeps_the_timestamp_that_did_not_move() -> None:
    """``lu`` defaults to ``lc`` on add, and a change that carries one keeps the other."""
    lc = 1_714_557_600.0
    connection = FakeConnection(
        after_subscribe=[{"a": {FRONT_DOOR: {"s": "open", "a": {"x": 1}, "lc": lc}}}]
    )
    client = make_client(FakeFactory(connection))
    try:
        await client.start()
        await _wait_until(lambda: client.get_state(FRONT_DOOR) is not None)
        added = _cached(client, FRONT_DOOR)
        assert added.last_changed == _iso(lc)
        assert added.last_updated == _iso(lc)

        connection.push_event({"c": {FRONT_DOOR: {"+": {"lc": lc + 10.0}}}})
        await _wait_until(lambda: _cached(client, FRONT_DOOR).last_changed == _iso(lc + 10.0))
        only_changed = _cached(client, FRONT_DOOR)
        assert only_changed.last_updated == _iso(lc)

        connection.push_event({"c": {FRONT_DOOR: {"+": {"lu": lc + 20.0}}}})
        await _wait_until(lambda: _cached(client, FRONT_DOOR).last_updated == _iso(lc + 20.0))
        only_updated = _cached(client, FRONT_DOOR)
        assert only_changed.last_changed == _iso(lc + 10.0)
        assert only_updated.last_changed == _iso(lc + 10.0)
    finally:
        await client.stop()


# ------------------------------------------------------------------------ health


async def test_health_transitions_are_observable() -> None:
    """The registry sees reconnecting, connected, reconnecting, connected, error in order."""
    first = FakeConnection(after_subscribe=[_INITIAL_BURST])
    second = FakeConnection(after_subscribe=[_INITIAL_BURST])
    factory = FakeFactory(first, second)
    health, records = _record_health()
    client = make_client(factory, health=health, sleep=_instant_sleep)
    try:
        await client.start()
        await _wait_until(lambda: health.get("ha") is HealthStatus.CONNECTED)
        first.push_error(ConnectionClosedError(None, None))
        await _wait_until(
            lambda: len(factory.calls) >= 2 and health.get("ha") is HealthStatus.CONNECTED
        )
    finally:
        await client.stop()
    assert _transitions(records) == [
        HealthStatus.RECONNECTING,
        HealthStatus.CONNECTED,
        HealthStatus.RECONNECTING,
        HealthStatus.CONNECTED,
        HealthStatus.ERROR,
    ]
    assert health.message()["detail"] == "connection stopped"


async def test_auth_invalid_is_terminal_and_the_token_never_leaks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected token is a terminal error, called once, never retried and never logged."""
    auth_message = {"code": "invalid_auth", "message": f"Invalid access token {TOKEN}"}
    connection = FakeConnection(auth="auth_invalid", auth_message=auth_message)
    factory = FakeFactory(connection)
    health, _ = _record_health()
    delays: list[float] = []
    client = make_client(factory, health=health, sleep=_recording_sleep(delays))
    with preserve_log_state():
        SECRET_SCRUBBER.set_secrets([TOKEN])
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
        try:
            await client.start()
            await _wait_until(lambda: health.get("ha") is HealthStatus.ERROR)
            detail = health.message().get("detail") or ""
            assert "token rejected" in detail
            assert TOKEN not in detail
            assert REDACTED in detail
            assert delays == []
            assert len(factory.calls) == 1

            error = client.last_error
            assert isinstance(error, HaAuthError)
            assert error.status_code == 401
            assert REDACTED in error.detail
            assert TOKEN not in str(error)
            assert TOKEN not in repr(error)
            assert TOKEN not in "".join(traceback.format_exception(error))
            _assert_token_absent(repr(client))

            await client.start()
            assert len(factory.calls) == 1
            client.set_entity_ids([GARAGE])
            assert len(factory.calls) == 1
            assert health.get("ha") is HealthStatus.ERROR
        finally:
            await client.stop()
    for record in caplog.records:
        _assert_token_absent(record.getMessage())


async def test_stop_after_auth_invalid_keeps_the_token_rejected_detail() -> None:
    """Stopping after a terminal rejection keeps the actionable reason, not "connection stopped"."""
    connection = FakeConnection(auth="auth_invalid", auth_message={"code": "invalid_auth"})
    health, _ = _record_health()
    client = make_client(FakeFactory(connection), health=health, sleep=_instant_sleep)
    await client.start()
    await _wait_until(lambda: health.get("ha") is HealthStatus.ERROR)
    assert "token rejected" in (health.message().get("detail") or "")

    await asyncio.wait_for(client.stop(), timeout=1.0)
    assert health.get("ha") is HealthStatus.ERROR
    detail = health.message().get("detail") or ""
    assert "token rejected" in detail
    assert "connection stopped" not in detail


@pytest.mark.parametrize(
    "message",
    [f"Invalid token {TOKEN}", {"code": "invalid_auth", "message": f"Invalid token {TOKEN}"}],
)
async def test_auth_invalid_message_accepts_text_and_objects(message: object) -> None:
    """Home Assistant has shipped both a string and an object as the auth message."""
    connection = FakeConnection(
        auth="auth_invalid",
        auth_message=cast("str | Mapping[str, object]", message),
    )
    client = make_client(FakeFactory(connection))
    try:
        await client.start()
        await _wait_until(lambda: isinstance(client.last_error, HaAuthError))
        error = client.last_error
        assert isinstance(error, HaAuthError)
        assert TOKEN not in error.detail
        assert REDACTED in error.detail
    finally:
        await client.stop()


# ---------------------------------------------------------------------- reconnect


async def test_backoff_sequence_and_prompt_stop() -> None:
    """The delays are 1, 2, 4, 8, 16, 30, 30 and a stop during the sleep returns quickly."""
    delays: list[float] = []
    health, _ = _record_health()
    client = make_client(
        FailingFactory(), health=health, sleep=_recording_sleep(delays, block_after=7)
    )
    await client.start()
    await _wait_until(lambda: len(delays) >= 7)
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    started_at = time.monotonic()
    await asyncio.wait_for(client.stop(), timeout=1.0)
    assert time.monotonic() - started_at < 0.5
    assert health.get("ha") is HealthStatus.ERROR


async def test_backoff_resets_after_a_successful_subscription() -> None:
    """Every successful subscribe resets the attempt counter to zero."""
    first = FakeConnection(after_subscribe=[_INITIAL_BURST])
    third = FakeConnection(after_subscribe=[_INITIAL_BURST])
    factory = FakeFactory(first, HaConnectionError("boom"), third, HaConnectionError("boom"))
    delays: list[float] = []
    client = make_client(factory, sleep=_recording_sleep(delays, block_after=4))
    try:
        await client.start()
        await _wait_until(
            lambda: len(factory.calls) >= 1 and bool(first.frames_of_type("subscribe_entities"))
        )
        first.push_error(ConnectionClosedError(None, None))
        await _wait_until(
            lambda: len(factory.calls) >= 3 and bool(third.frames_of_type("subscribe_entities"))
        )
        third.push_error(ConnectionClosedError(None, None))
        await _wait_until(lambda: len(delays) >= 4)
        assert delays == [DEFAULT_RECONNECT_INITIAL_S, 2.0, DEFAULT_RECONNECT_INITIAL_S, 2.0]
    finally:
        await asyncio.wait_for(client.stop(), timeout=1.0)


async def test_an_unexpected_error_reconnects_instead_of_leaving_health_connected() -> None:
    """A non-transport exception in ``recv`` reports unreachable, backs off and reconnects."""
    first = FakeConnection(after_subscribe=[_INITIAL_BURST])
    second = FakeConnection(after_subscribe=[_INITIAL_BURST])
    factory = FakeFactory(first, second)
    health = HealthRegistry()
    records: list[HealthStatus] = []
    details: list[str] = []

    def record() -> None:
        """Record the status and detail of every health write."""
        records.append(health.get("ha"))
        details.append(health.message().get("detail") or "")

    health.set_on_change(record)
    delays: list[float] = []
    client = make_client(
        factory, health=health, sleep=_recording_sleep(delays), initial_backoff_s=0.01
    )
    try:
        await client.start()
        await _wait_until(lambda: health.get("ha") is HealthStatus.CONNECTED)

        first.push_error(RuntimeError("boom"))
        await _wait_until(
            lambda: len(factory.calls) >= 2 and health.get("ha") is HealthStatus.CONNECTED
        )
        await _wait_until(lambda: _state_is(client, FRONT_DOOR, "open"))
        assert delays == [0.01]
        error = client.last_error
        assert isinstance(error, HaConnectionError)
        assert "boom" in str(error)
        _assert_token_absent(str(error))
    finally:
        await asyncio.wait_for(client.stop(), timeout=1.0)
    assert _transitions(records) == [
        HealthStatus.RECONNECTING,
        HealthStatus.CONNECTED,
        HealthStatus.RECONNECTING,
        HealthStatus.CONNECTED,
        HealthStatus.ERROR,
    ]
    assert any(detail.startswith("unreachable: ") and "boom" in detail for detail in details), (
        details
    )
    for detail in details:
        _assert_token_absent(detail)


async def test_stop_propagates_the_cancellation_of_its_caller() -> None:
    """A caller cancelled inside ``stop()`` sees ``CancelledError``, never a swallowed one."""
    release = asyncio.Event()
    delays: list[float] = []
    health, _ = _record_health()

    async def sleep(delay: float) -> None:
        """Keep the supervisor parked so ``stop()`` is still draining when cancelled.

        The first cancellation belongs to ``stop()`` and is absorbed, so the test
        also cancels the caller inside the same drain window. The bounded waits
        guarantee the test cannot hang on the sleep itself.

        :param delay: the delay the supervisor requested.
        """
        delays.append(delay)
        for _attempt in range(2):
            try:
                await asyncio.wait_for(release.wait(), timeout=_WAIT_TIMEOUT_S)
                return
            except TimeoutError:
                return
            except asyncio.CancelledError:
                continue

    client = make_client(FailingFactory(), health=health, sleep=sleep)
    try:
        await client.start()
        await _wait_until(lambda: len(delays) >= 1)

        stop_task = asyncio.create_task(client.stop())
        await asyncio.sleep(0.02)
        assert not stop_task.done()
        stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop_task, timeout=_WAIT_TIMEOUT_S)
        assert stop_task.cancelled()
        assert health.get("ha") is HealthStatus.ERROR
        assert health.message().get("detail") == "connection stopped"
    finally:
        await asyncio.wait_for(client.stop(), timeout=_WAIT_TIMEOUT_S)
        await asyncio.sleep(0.02)


async def test_stop_during_a_real_backoff_returns_promptly() -> None:
    """A real 30 s sleep is cancelled by stop without waiting for it."""
    factory = FailingFactory()
    health, _ = _record_health()
    client = make_client(
        factory, health=health, initial_backoff_s=30.0, max_backoff_s=DEFAULT_RECONNECT_MAX_S
    )
    try:
        await client.start()
        await _wait_until(lambda: (health.message().get("detail") or "").startswith("unreachable"))
        started_at = time.monotonic()
        await asyncio.wait_for(client.stop(), timeout=1.0)
        assert time.monotonic() - started_at < 0.5
        assert health.get("ha") is HealthStatus.ERROR
    finally:
        await client.stop()


# ---------------------------------------------------------------- entity selection


async def test_entity_list_change_resubscribes_on_the_same_socket() -> None:
    """A new list sends one unsubscribe and one fresh subscribe, and clears the cache."""
    connection = FakeConnection(
        after_subscribe=[{"a": {FRONT_DOOR: {"s": "open", "a": {}, "lc": _LC_FRONT}}}]
    )
    factory = FakeFactory(connection)
    health, _ = _record_health()
    client = make_client(factory, entity_ids=[FRONT_DOOR], health=health)
    try:
        await client.start()
        await _wait_until(lambda: client.get_state(FRONT_DOOR) is not None)

        client.set_entity_ids([GARAGE])
        await _wait_until(lambda: len(connection.frames_of_type("subscribe_entities")) >= 2)
        connection.push_event({"a": {GARAGE: {"s": "closed", "a": {}, "lc": _LC_GARAGE}}})
        await _wait_until(lambda: _state_is(client, GARAGE, "closed"))
        assert client.get_state(FRONT_DOOR) is None
        assert health.get("ha") is HealthStatus.CONNECTED
        assert len(connection.frames_of_type("unsubscribe_events")) == 1
        subscriptions = connection.frames_of_type("subscribe_entities")
        assert cast("list[object]", subscriptions[-1].get("entity_ids")) == [GARAGE]
        assert len(factory.calls) == 1

        client.set_entity_ids([])
        await _wait_until(lambda: health.get("ha") is HealthStatus.NO_ENTITIES)
        assert connection.closed is True
        for frame in connection.frames_of_type("subscribe_entities"):
            assert cast("list[object]", frame.get("entity_ids"))
    finally:
        await client.stop()


async def test_a_bare_entity_id_string_is_rejected() -> None:
    """A bare string iterates characters, so both entry points must refuse it."""
    with pytest.raises(HaConfigurationError, match="wrap it in a list"):
        HomeAssistantWebSocketClient(
            base_url=BASE_URL,
            token=TOKEN,
            entity_ids=cast("Iterable[str]", FRONT_DOOR),
        )

    client = make_client(FailingFactory())
    with pytest.raises(HaConfigurationError, match="wrap it in a list"):
        client.set_entity_ids(cast("Iterable[str]", "sensor.x"))
    assert client.entity_ids == (FRONT_DOOR,)


async def test_states_mapping_is_read_only_and_a_snapshot() -> None:
    """``states`` is a read-only copy that later updates do not rewrite."""
    connection = FakeConnection(after_subscribe=[_INITIAL_BURST])
    client = make_client(FakeFactory(connection), entity_ids=[FRONT_DOOR, GARAGE])
    try:
        await client.start()
        await _wait_until(lambda: client.get_state(GARAGE) is not None)
        snapshot = client.states
        assert set(snapshot) == {FRONT_DOOR, GARAGE}
        mapping = cast("dict[str, HaEntityState]", snapshot)
        with pytest.raises(TypeError):
            operator.setitem(mapping, "sensor.new", snapshot[FRONT_DOOR])

        connection.push_event({"c": {FRONT_DOOR: {"+": {"s": "locked"}}}})
        await _wait_until(lambda: _state_is(client, FRONT_DOOR, "locked"))
        assert snapshot[FRONT_DOOR].state == "open"
        assert client.states[FRONT_DOOR].state == "locked"
    finally:
        await client.stop()


async def test_empty_entity_list_reports_no_entities_and_starts_on_demand() -> None:
    """An empty start opens no socket; a later non-empty list starts the supervisor.

    The empty selection reports ``no_entities``, not ``not_configured``: the
    address and token are stored, only the tile list is empty.
    """
    connection = FakeConnection(
        after_subscribe=[{"a": {FRONT_DOOR: {"s": "open", "a": {}, "lc": _LC_FRONT}}}]
    )
    factory = FakeFactory(connection)
    health, _ = _record_health()
    client = make_client(factory, entity_ids=[], health=health)
    try:
        await client.start()
        assert health.get("ha") is HealthStatus.NO_ENTITIES
        assert factory.calls == []
        assert connection.sent == []

        client.set_entity_ids([FRONT_DOOR])
        await _wait_until(lambda: _state_is(client, FRONT_DOOR, "open"))
        assert len(factory.calls) == 1
    finally:
        await client.stop()


async def test_an_empty_selection_reports_no_entities_with_an_honest_detail() -> None:
    """A configured client with no tiles says so instead of claiming no configuration.

    The header detail names what is actually missing, so a user who has stored an
    address and a token does not read "no entities selected" as "not configured".
    """
    health, _ = _record_health()
    client = make_client(FakeFactory(FakeConnection()), entity_ids=[], health=health)
    try:
        await client.start()
        assert health.get("ha") is HealthStatus.NO_ENTITIES
        assert health.message()["detail"] == "no tiles selected yet"
    finally:
        await client.stop()


# ------------------------------------------------------------------- real socket


@asynccontextmanager
async def _loopback_server() -> AsyncIterator[int]:
    """Serve a tiny fake Home Assistant on an ephemeral loopback port.

    :raises OSError: if no local port can be bound.
    """
    subscribed = asyncio.Event()

    async def handler(websocket: ServerConnection) -> None:
        """Answer the handshake and one subscription, then wait for the close.

        :param websocket: the accepted client connection.
        """
        await websocket.send(json.dumps({"type": "auth_required"}))
        auth = cast("dict[str, object]", json.loads(await websocket.recv()))
        assert auth["type"] == "auth"
        await websocket.send(json.dumps({"type": "auth_ok"}))
        subscribe = cast("dict[str, object]", json.loads(await websocket.recv()))
        assert subscribe["type"] == "subscribe_entities"
        await websocket.send(
            json.dumps({"id": subscribe["id"], "type": "result", "success": True, "result": None})
        )
        await websocket.send(
            json.dumps(
                {
                    "id": subscribe["id"],
                    "type": "event",
                    "event": {"a": {FRONT_DOOR: {"s": "open", "a": {}, "lc": _LC_FRONT}}},
                }
            )
        )
        subscribed.set()
        try:
            await asyncio.wait_for(websocket.wait_closed(), timeout=_WAIT_TIMEOUT_S)
        except TimeoutError:
            # A client that never closes must not keep the fake server handler
            # alive past the test that started it.
            return

    server = serve(handler, "127.0.0.1", 0)
    async with server:
        sockets = server.sockets
        assert sockets
        port = cast("int", sockets[0].getsockname()[1])
        yield port
    assert subscribed.is_set()


@pytest.mark.integration
async def test_real_websocket_connection_over_a_local_socket() -> None:
    """The default factory talks to a real loopback websocket server."""
    try:
        async with _loopback_server() as port:
            health, _ = _record_health()
            client = HomeAssistantWebSocketClient(
                base_url=f"http://127.0.0.1:{port}",
                token=TOKEN,
                entity_ids=[FRONT_DOOR],
                health=health,
            )
            try:
                await client.start()
                await _wait_until(lambda: _state_is(client, FRONT_DOOR, "open"))
                assert health.get("ha") is HealthStatus.CONNECTED
            finally:
                await asyncio.wait_for(client.stop(), timeout=5.0)
    except OSError as exc:
        pytest.skip(f"a local websocket port could not be bound: {exc}")
