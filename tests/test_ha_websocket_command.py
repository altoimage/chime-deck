"""Fully offline tests for the one-off Home Assistant websocket command.

The tests prove the command round-trip on its own short-lived connection, its
timeout and transport bounds, its typed failures and its token secrecy without
a socket. Owning ticket: HSD-jwd3.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Final, cast

import pytest

from chime_deck.ha import (
    HaAuthError,
    HaBadResponseError,
    HaCommandError,
    HaConfigurationError,
    HaConnectionError,
    HaWebSocketConnection,
    HomeAssistantWebSocketClient,
)

pytestmark = pytest.mark.asyncio

TOKEN: Final = "secret-long-lived-token-abc123"
"""Any occurrence of this string outside the auth frame is a failure."""

BASE_URL: Final = "http://ha.test:8123"

_LIST_FOR_DISPLAY: Final = "config/entity_registry/list_for_display"


def _assert_token_absent(text: str) -> None:
    """Fail if the long-lived token appears in *text*.

    :param text: any message, repr or frame that leaves the client.
    :raises AssertionError: if the token is in *text*.
    """
    if TOKEN in text:
        raise AssertionError("the Home Assistant token appears in text that leaves the client")


class ScriptedConnection:
    """An in-memory Home Assistant peer that answers auth and scripted commands.

    It records every frame the client sends, answers the handshake, and answers
    one command per scripted ``results``/``errors`` entry. An unscripted command
    gets Home Assistant's own ``unknown_command`` refusal, so a test cannot pass
    on an answer it did not ask for.
    """

    def __init__(
        self,
        *,
        auth: str = "auth_ok",
        results: Mapping[str, object] | None = None,
        errors: Mapping[str, object] | None = None,
        frames_before_result: Iterable[Mapping[str, object]] = (),
        silent: bool = False,
        recv_error: Exception | None = None,
    ) -> None:
        """Create a peer that starts by asking the client to authenticate.

        :param auth: ``auth_ok`` or ``auth_invalid``.
        :param results: successful ``result`` values keyed by command type.
        :param errors: failed ``error`` values keyed by command type.
        :param frames_before_result: frames pushed verbatim before a command result.
        :param silent: when true, record commands without ever answering them.
        :param recv_error: raised from the next receive after a command is sent.
        """
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self._auth = auth
        self._results = dict(results) if results is not None else {}
        self._errors = dict(errors) if errors is not None else {}
        self._frames_before_result = list(frames_before_result)
        self._silent = silent
        self._recv_error = recv_error
        self._queue: asyncio.Queue[str | bytes | Exception] = asyncio.Queue()
        self._queue.put_nowait(json.dumps({"type": "auth_required"}))

    async def send(self, message: str) -> None:
        """Record one frame and answer it the way Home Assistant would.

        :param message: already-encoded JSON frame.
        """
        frame = cast("dict[str, object]", json.loads(message))
        self.sent.append(frame)
        if frame.get("type") == "auth":
            self._queue.put_nowait(self._auth_reply())
            return
        if self._recv_error is not None:
            self._queue.put_nowait(self._recv_error)
            return
        if self._silent:
            return
        for noise in self._frames_before_result:
            self._queue.put_nowait(json.dumps(noise))
        self._queue.put_nowait(self._command_reply(frame))

    async def recv(self) -> str | bytes:
        """Return the next scripted frame, raising a scripted exception in place."""
        item = await self._queue.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        """Mark the connection closed; the fake has no close handshake."""
        self.closed = True

    def _auth_reply(self) -> str:
        """Return the frame Home Assistant sends after the auth frame."""
        if self._auth == "auth_ok":
            return json.dumps({"type": "auth_ok"})
        return json.dumps({"type": "auth_invalid", "message": {"code": "invalid_auth"}})

    def _command_reply(self, frame: Mapping[str, object]) -> str:
        """Return the result frame for one command frame.

        :param frame: the command frame the client sent.
        """
        command_id = frame.get("id")
        command_type = frame.get("type")
        if isinstance(command_type, str) and command_type in self._errors:
            return json.dumps(
                {
                    "id": command_id,
                    "type": "result",
                    "success": False,
                    "error": self._errors[command_type],
                }
            )
        if isinstance(command_type, str) and command_type in self._results:
            return json.dumps(
                {
                    "id": command_id,
                    "type": "result",
                    "success": True,
                    "result": self._results[command_type],
                }
            )
        return json.dumps(
            {
                "id": command_id,
                "type": "result",
                "success": False,
                "error": {"code": "unknown_command", "message": "Unknown command."},
            }
        )


class _ConnectionContext:
    """Async context manager that yields one fake connection."""

    def __init__(self, connection: ScriptedConnection) -> None:
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


class ScriptedFactory:
    """Build a fresh scripted peer per open call and record every attempt."""

    def __init__(self, builder: Callable[[], ScriptedConnection]) -> None:
        """Store the builder and start empty.

        :param builder: zero-argument callable returning a fresh peer.
        """
        self.connections: list[ScriptedConnection] = []
        self.calls: list[dict[str, object]] = []
        self._builder = builder

    def __call__(
        self,
        url: str,
        *,
        open_timeout: float,
        close_timeout: float,
        ping_interval: float,
        ping_timeout: float,
    ) -> AbstractAsyncContextManager[HaWebSocketConnection]:
        """Record the attempt and return a fresh scripted peer.

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
        connection = self._builder()
        self.connections.append(connection)
        return _ConnectionContext(connection)


def _make_factory(
    *,
    auth: str = "auth_ok",
    results: Mapping[str, object] | None = None,
    errors: Mapping[str, object] | None = None,
    frames_before_result: Iterable[Mapping[str, object]] = (),
    silent: bool = False,
    recv_error: Exception | None = None,
) -> ScriptedFactory:
    """Build a factory whose every connection shares the scripted answers.

    :param auth: ``auth_ok`` or ``auth_invalid`` for every peer.
    :param results: successful ``result`` values keyed by command type.
    :param errors: failed ``error`` values keyed by command type.
    :param frames_before_result: frames pushed verbatim before a command result.
    :param silent: when true, record commands without ever answering them.
    :param recv_error: raised from the next receive after a command is sent.
    """
    noise = tuple(frames_before_result)

    def build() -> ScriptedConnection:
        return ScriptedConnection(
            auth=auth,
            results=results,
            errors=errors,
            frames_before_result=noise,
            silent=silent,
            recv_error=recv_error,
        )

    return ScriptedFactory(build)


def _client(
    factory: ScriptedFactory,
    *,
    entity_ids: Iterable[str] = (),
    command_timeout_s: float | None = None,
) -> HomeAssistantWebSocketClient:
    """Build a client that talks to *factory* and nothing else.

    :param factory: connection factory for the test.
    :param entity_ids: entities to subscribe to, if any.
    :param command_timeout_s: override for the one-off command bound.
    """
    if command_timeout_s is None:
        return HomeAssistantWebSocketClient(
            base_url=BASE_URL,
            token=TOKEN,
            entity_ids=entity_ids,
            connect=factory,
        )
    return HomeAssistantWebSocketClient(
        base_url=BASE_URL,
        token=TOKEN,
        entity_ids=entity_ids,
        connect=factory,
        command_timeout_s=command_timeout_s,
    )


async def test_command_round_trip_on_a_never_started_client() -> None:
    """A one-off command authenticates, sends its frame and returns the result."""
    result_mapping = {"entities": [{"entity_id": "sensor.front_door"}]}
    factory = _make_factory(results={_LIST_FOR_DISPLAY: result_mapping})
    client = _client(factory, command_timeout_s=0.5)

    result = await client.command(_LIST_FOR_DISPLAY)

    assert result == result_mapping
    assert len(factory.calls) == 1
    assert factory.calls[0]["url"] == "ws://ha.test:8123/api/websocket"
    connection = factory.connections[0]
    assert [frame.get("type") for frame in connection.sent] == ["auth", _LIST_FOR_DISPLAY]
    assert TOKEN in json.dumps(connection.sent[0])
    assert isinstance(connection.sent[1].get("id"), int)
    assert connection.closed
    for frame in connection.sent[1:]:
        _assert_token_absent(json.dumps(frame))
    _assert_token_absent(repr(client))
    error = client.last_error
    if error is not None:
        _assert_token_absent(str(error))


async def test_command_ids_increment_and_each_command_opens_a_connection() -> None:
    """Two commands take ids 1 and 2 from the shared counter, each on a new socket."""
    factory = _make_factory(results={_LIST_FOR_DISPLAY: {"entities": []}})
    client = _client(factory)

    first = await client.command(_LIST_FOR_DISPLAY)
    second = await client.command(_LIST_FOR_DISPLAY)

    assert first == {"entities": []}
    assert second == {"entities": []}
    assert len(factory.calls) == 2
    assert [connection.sent[-1].get("id") for connection in factory.connections] == [1, 2]


async def test_command_ignores_noise_and_other_command_ids() -> None:
    """A ping and a result for another id are skipped, and the real result is returned."""
    factory = _make_factory(
        results={_LIST_FOR_DISPLAY: {"kept": True}},
        frames_before_result=(
            {"type": "ping"},
            {"id": 999, "type": "result", "success": True, "result": {"wrong": True}},
        ),
    )
    client = _client(factory)

    assert await client.command(_LIST_FOR_DISPLAY) == {"kept": True}


async def test_command_failure_is_typed_and_redacted() -> None:
    """``success: false`` becomes a typed error whose reason cannot leak the token."""
    factory = _make_factory(
        errors={
            "bad/command": {"code": "unknown_command", "message": "Unknown command."},
        }
    )
    client = _client(factory)

    with pytest.raises(HaCommandError) as caught:
        await client.command("bad/command")

    assert caught.value.code == "unknown_command"
    assert "bad/command" in str(caught.value)
    assert "Unknown command." in caught.value.detail
    _assert_token_absent(str(caught.value))

    leaking_factory = _make_factory(
        errors={"bad/command": {"code": "unauthorized", "message": f"token {TOKEN} rejected"}}
    )
    leaking_client = _client(leaking_factory)

    with pytest.raises(HaCommandError) as leaked:
        await leaking_client.command("bad/command")

    _assert_token_absent(str(leaked.value))
    _assert_token_absent(repr(leaked.value))


@pytest.mark.parametrize("error", [{"message": "nope"}, {"code": 7, "message": "nope"}])
async def test_command_error_without_a_string_code_has_no_code(
    error: Mapping[str, object],
) -> None:
    """A missing or non-string ``code`` is reported as ``None``, not coerced."""
    factory = _make_factory(errors={"bad/command": error})
    client = _client(factory)

    with pytest.raises(HaCommandError) as caught:
        await client.command("bad/command")

    assert caught.value.code is None


async def test_command_times_out_within_its_bound() -> None:
    """A silent peer fails fast with the command type and the configured bound named."""
    factory = _make_factory(silent=True)
    client = _client(factory, command_timeout_s=0.05)

    with pytest.raises(HaConnectionError) as caught:
        await client.command("slow/command")

    message = str(caught.value)
    assert "slow/command" in message
    assert "0.05" in message


async def test_command_maps_a_transport_failure() -> None:
    """A socket failure while waiting for the result becomes a connection error."""
    factory = _make_factory(recv_error=OSError("socket gone"))
    client = _client(factory)

    with pytest.raises(HaConnectionError) as caught:
        await client.command("bad/command")

    assert "socket gone" in str(caught.value)


async def test_command_rejects_a_result_that_is_not_an_object() -> None:
    """A result that is a JSON array is a bad response, not a usable answer."""
    factory = _make_factory(results={"bad/command": [1, 2]})
    client = _client(factory)

    with pytest.raises(HaBadResponseError):
        await client.command("bad/command")


async def test_command_on_a_terminal_client_opens_no_socket() -> None:
    """After the token is rejected, a command fails locally without a connection."""
    factory = _make_factory(auth="auth_invalid")
    client = _client(factory, entity_ids=("sensor.x",))

    await client.start()
    try:
        for _ in range(200):
            if client.last_error is not None:
                break
            await asyncio.sleep(0.005)
        assert isinstance(client.last_error, HaAuthError)

        with pytest.raises(HaAuthError):
            await client.command("any/command")

        assert len(factory.calls) == 1
    finally:
        await client.stop()


async def test_command_validates_without_a_socket() -> None:
    """A blank type or a reserved field fails before any connection is opened."""
    factory = _make_factory(results={_LIST_FOR_DISPLAY: {"entities": []}})
    client = _client(factory)

    for command_type in ("", "   "):
        with pytest.raises(HaConfigurationError):
            await client.command(command_type)
    with pytest.raises(HaConfigurationError):
        await client.command("any/command", id=1)
    with pytest.raises(HaConfigurationError):
        await client.command("any/command", type="other")

    assert factory.calls == []
