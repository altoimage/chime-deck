"""An offline Home Assistant websocket peer, factory and client helper.

:class:`FakeHaPeer` is a complete :class:`~chime_deck.ha.HaWebSocketConnection`:
it answers the auth handshake and the entity subscription the way Home Assistant
does, records every frame the client sends, and lets a test push frames (a state
event, a command result) at any moment. :class:`FakeHaFactory` is the
:class:`~chime_deck.ha.ConnectionFactory` that hands those peers out, and
:func:`make_client` builds a **real**
:class:`~chime_deck.ha.HomeAssistantWebSocketClient` against it.

Everything here is offline and bounded: no socket is opened, and the helpers a
test uses to wait (``mqtt_fakes.wait_until`` / ``asyncio.wait_for``) belong to the
test, not to these fakes.

It lives in a module of its own because the lifespan, shutdown and API tests all
need the same peers, and one test module importing private names out of another
is worse than sharing them here.

Owning ticket: HSD-qr3k.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Final, cast

from chime_deck.ha import HaWebSocketConnection, HomeAssistantWebSocketClient

__all__ = ["TOKEN", "FakeHaFactory", "FakeHaPeer", "make_client"]

TOKEN: Final = "long-lived-token-0123456789abcdef"
"""The stored ``ha.token`` the fake peers accept; no test may log it."""

BASE_URL: Final = "http://ha.test:8123"
"""The base URL :func:`make_client` normalises into a websocket endpoint."""


class FakeHaPeer:
    """An in-memory Home Assistant peer driven frame by frame.

    The peer starts by asking the client to authenticate, answers ``auth`` with
    ``auth_ok``, answers ``subscribe_entities`` with a successful result and
    answers every other command from the scripted *results* map. Everything it
    answers, and everything a test pushes, travels through one
    :class:`asyncio.Queue` that :meth:`recv` awaits.
    """

    def __init__(self, *, results: Mapping[str, object] | None = None) -> None:
        """Create a peer that is waiting for the client's auth frame.

        :param results: command type to the ``result`` payload answered with;
            an unscripted command is answered with an empty object.
        """
        self.sent: list[dict[str, object]] = []
        """Every frame the client sent, decoded, in order."""

        self.closed = False
        """Whether the client's context manager closed this peer."""

        self._results = dict(results) if results is not None else {}
        self._subscription_id: int | None = None
        self._queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        # Home Assistant speaks first: the client's handshake waits for this
        # frame before it sends the token.
        self._queue.put_nowait({"type": "auth_required"})

    async def send(self, message: str) -> None:
        """Record one frame and answer it the way Home Assistant would.

        :param message: already-encoded JSON frame from the client.
        """
        frame = cast("dict[str, object]", json.loads(message))
        self.sent.append(frame)
        frame_type = frame.get("type")
        if frame_type == "auth":
            self._queue.put_nowait({"type": "auth_ok"})
        elif frame_type == "subscribe_entities":
            self._subscription_id = cast("int", frame.get("id"))
            self._queue.put_nowait(
                {
                    "id": self._subscription_id,
                    "type": "result",
                    "success": True,
                    "result": None,
                }
            )
        else:
            self._queue.put_nowait(
                {
                    "id": frame.get("id"),
                    "type": "result",
                    "success": True,
                    "result": self._results.get(cast(str, frame_type), {}),
                }
            )

    async def recv(self) -> str | bytes:
        """Return the next queued frame as JSON text.

        Cancellation propagates: the client parks and cancels its receive task
        when it re-subscribes, and that must stay a real cancellation.
        """
        return json.dumps(await self._queue.get())

    async def close(self) -> None:
        """Mark the peer closed; there is no close handshake to run."""
        self.closed = True

    def push(self, frame: Mapping[str, object]) -> None:
        """Queue one frame verbatim.

        :param frame: frame to send to the client.
        """
        self._queue.put_nowait(dict(frame))

    def push_states(self, states: Mapping[str, Mapping[str, object]]) -> None:
        """Queue one state event for the current subscription.

        :param states: ``entity_id`` to the compressed state dict, typically
            ``{"s": state, "a": attributes, "lc": epoch_seconds}``; it is placed
            in the event's ``a`` (add) container.
        :raises RuntimeError: if the client has not subscribed yet, because a
            state event without a subscription id is not something Home
            Assistant sends.
        """
        if self._subscription_id is None:
            raise RuntimeError("the client has not sent a subscribe_entities frame yet")
        self.push(
            {
                "id": self._subscription_id,
                "type": "event",
                "event": {"a": {entity_id: dict(values) for entity_id, values in states.items()}},
            }
        )

    def subscribed_ids(self) -> list[list[str]]:
        """Return the ``entity_ids`` of every subscription, in order."""
        return [
            cast("list[str]", frame.get("entity_ids"))
            for frame in self.sent
            if frame.get("type") == "subscribe_entities"
        ]

    def commands(self) -> list[str]:
        """Return the ``type`` of every frame that is neither auth nor a subscribe."""
        ignored = {"auth", "subscribe_entities"}
        return [
            cast("str", frame.get("type"))
            for frame in self.sent
            if frame.get("type") not in ignored
        ]


class _PeerContext:
    """Async context manager that yields one fake peer and closes it on exit."""

    def __init__(self, peer: FakeHaPeer) -> None:
        """Wrap *peer*.

        :param peer: the connection the client gets.
        """
        self._peer = peer

    async def __aenter__(self) -> HaWebSocketConnection:
        """Yield the peer."""
        return self._peer

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the peer.

        :param exc_info: the exception triple, unused.
        """
        await self._peer.close()


class FakeHaFactory:
    """The ``ConnectionFactory`` that hands out fresh fake peers.

    A peer factory can be supplied to script the peers themselves (a command
    result, say); without one every connection attempt gets a plain
    :class:`FakeHaPeer`.
    """

    def __init__(self, peer_factory: Callable[[], FakeHaPeer] | None = None) -> None:
        """Create a factory that has opened nothing yet.

        :param peer_factory: builds each peer; ``None`` builds :class:`FakeHaPeer`.
        """
        self.calls: list[str] = []
        """The URL of every connection attempt, in order."""

        self.connections: list[FakeHaPeer] = []
        """Every peer handed out, in order."""

        self._peer_factory = peer_factory if peer_factory is not None else FakeHaPeer

    def __call__(
        self,
        url: str,
        *,
        open_timeout: float,
        close_timeout: float,
        ping_interval: float,
        ping_timeout: float,
    ) -> _PeerContext:
        """Record the attempt and return the next peer's context manager.

        :param url: websocket endpoint the client asked for.
        :param open_timeout: open timeout the client passed, unused.
        :param close_timeout: close timeout the client passed, unused.
        :param ping_interval: ping interval the client passed, unused.
        :param ping_timeout: ping timeout the client passed, unused.
        """
        self.calls.append(url)
        peer = self._peer_factory()
        self.connections.append(peer)
        return _PeerContext(peer)


def make_client(
    factory: FakeHaFactory,
    *,
    token: str = TOKEN,
    entity_ids: tuple[str, ...] = (),
) -> HomeAssistantWebSocketClient:
    """Build a real websocket client whose connections go to *factory*.

    :param factory: the scripted connection factory.
    :param token: stored token the client authenticates with.
    :param entity_ids: initial subscription; the lifespan sets it afterwards.
    """
    return HomeAssistantWebSocketClient(
        base_url=BASE_URL,
        token=token,
        entity_ids=entity_ids,
        connect=factory,
    )
