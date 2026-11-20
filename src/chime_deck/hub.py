"""Broadcast hub behind the ``/ws`` websocket endpoint.

The hub is transport-agnostic: it deals in encoded JSON strings and never
imports Starlette or FastAPI, so it can be unit-tested without a socket and
later reused by other transports. It holds one bounded :class:`asyncio.Queue`
per connected client and pushes coalesced messages to every queue at most ten
times a second (design 05 §4). Silence is bounded as well: a heartbeat lets a
client tell a healthy idle socket from a dead one (HSD-0g25).

Frames produced per flush tick:

* ``entity_state`` — last value per ``entity_id`` wins. Every frame also carries
  the tile's ``selected`` boolean, so a plain state delta keeps the selection
  border honest as well (HSD-u6k5).
* ``press`` — every press is sent, in order. "A press is not lost" means
  coalescing never merges presses; it does **not** mean presses survive a full
  queue, which would require unbounded memory.
* ``config_changed`` — one message per ``what``.
* ``health`` — last value wins.
* ``heartbeat`` — one frame per client per :data:`DEFAULT_HEARTBEAT_INTERVAL`
  seconds of silence, reset by any frame delivered to that client, because a
  delivered frame is itself the liveness proof. It is sent from the existing
  flush loop, so it adds no second background task, and an idle flush still
  sends nothing on its own.

An ``entity_state`` frame carries the presentation block a ``snapshot`` row
carries (``displayed_state``, ``unit_of_measurement``, ``tone`` and
``attribute_hints``) from the same
:func:`~chime_deck.icons.presentation_hints` call, so a tile that keeps its
word from the snapshot can never end up describing the previous state while its
colour describes the new one (HSD-7oto). The colour is part of that same block
(HSD-d5x2), so a tile also cannot show a new word in an old colour.

A *selection-only* ``entity_state`` frame (HSD-u6k5, D29) is the one exception:
it carries ``entity_id`` and ``selected`` and nothing else. The hub does not hold
that entity's live state, and running the frame through
:func:`~chime_deck.icons.presentation_hints` with ``state=None`` would emit
``displayed_state: null`` and blank the tile's word — the HSD-7oto bug shape. A
client therefore merges an ``entity_state`` frame by key.

Each message is JSON-encoded once and offered to every client with
drop-oldest semantics, so one slow tablet can neither block the loop nor grow
the process without bound.

Owning ticket: HSD-8gft.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import __version__
from .entity_info import (
    LIVE_LOOKUP_TIMEOUT_S,
    NO_LIVE_ENTITY_INFO,
    LiveEntityInfo,
    live_contexts,
)
from .health import HealthRegistry
from .icons import domain_of, presentation_hints, presentation_payload, resolve_icon
from .selection import NO_SELECTION, SelectionLookup
from .store import ConfigEvent, ConfigEventName, ConfigStore, Entity

__all__ = [
    "DEFAULT_FLUSH_INTERVAL",
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_QUEUE_SIZE",
    "HUB_STOP_TIMEOUT_S",
    "HubClient",
    "StateHub",
]

DEFAULT_FLUSH_INTERVAL: Final = 0.1
"""Seconds between flushes: 10 Hz, the maximum the UI needs (design 05 §4)."""

DEFAULT_HEARTBEAT_INTERVAL: Final = 5.0
"""Seconds of client silence between ``heartbeat`` frames.

The client treats three missed beats as death, so five seconds put the zombie
bound for a wall panel at fifteen seconds: idle sockets stay provably alive and
a dead one is noticed quickly, without reconnect churn on a healthy socket.
"""

DEFAULT_QUEUE_SIZE: Final = 32
"""Messages buffered per client before the oldest is dropped."""

HUB_STOP_TIMEOUT_S: Final = 5.0
"""Seconds :meth:`StateHub.stop` waits for its background tasks.

A task that suppresses cancellation must not be able to hold ``store.close()``
hostage, so shutdown gives up after this bound and says so in the log.
"""


def _loop_time() -> float:
    """Return the event loop clock, so every deadline is on one time base.

    A synchronous caller can register a client before any loop runs, where
    :func:`asyncio.get_running_loop` raises; the loop clock *is* the monotonic
    clock, so the fallback gives the same instants a later loop will read.

    :returns: the current loop-clock instant in seconds.
    """
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        return time.monotonic()


@dataclass(frozen=True, slots=True)
class TileOptions:
    """The per-tile presentation facts the snapshot and the delta must agree on.

    One row's ``show_attributes``, explicit ``state_tones`` overrides and
    ``colour_by_state`` opt-out decide both the word and the colour a tile shows,
    so they travel together: a frame built from a different copy of these facts
    than the snapshot row would let a tile's colour drift from its word
    (HSD-7oto, HSD-d5x2).
    """

    show_attributes: tuple[str, ...]
    state_tones: Mapping[str, str] | None
    colour_by_state: bool


def _tile_options_map(rows: Iterable[Entity]) -> dict[str, TileOptions]:
    """Map every configured entity id to its tile's presentation facts.

    The snapshot and the live ``entity_state`` frames read these options through
    this one function, so the two can never disagree (HSD-7oto). Everything a
    tile needs per row to present a state consistently lives here: the
    attributes it shows, its explicit state→tone overrides and whether it colours
    by state at all.

    :param rows: entity rows from the store.
    :returns: entity id → that tile's presentation facts.
    """
    return {
        row.entity_id: TileOptions(
            show_attributes=tuple(row.show_attributes or ()),
            state_tones=row.state_tones,
            colour_by_state=row.colour_by_state,
        )
        for row in rows
    }


def _entity_state_data(
    entity_id: str,
    entry: Mapping[str, object],
    options: Mapping[str, TileOptions],
    *,
    selected: bool,
) -> dict[str, object]:
    """Build one ``entity_state`` payload from a queued change and the tile options.

    The word, unit, tone and attribute hints come from the very
    :func:`~chime_deck.icons.presentation_hints` call a snapshot row makes,
    so the frame is a faithful mirror of what that row would show now.

    The unit is deliberately *not* suppressed for ``unavailable``: whether an
    unavailable tile keeps its unit is the client's ``tileView()`` rule, and a
    frame that already applied it would leave the client unable to tell an
    absent unit from a suppressed one.

    :param entity_id: Home Assistant entity id the change belongs to.
    :param entry: the queued ``entity_id``/``state``/``attributes``/``last_changed`` map.
    :param options: entity id → that tile's presentation facts; an entity that is
        not configured gets no attributes, no overrides and colours by state.
    :param selected: whether the tile is selected right now, read from the
        selection seam at flush time rather than carried in the queue, so one
        tick's toggles coalesce to the final truth.
    :returns: the queued fields plus the selection flag and the presentation
        block, ready to encode.
    """
    state = entry.get("state")
    attributes = entry.get("attributes")
    tile = options.get(entity_id)
    hints = presentation_hints(
        domain_of(entity_id),
        state=state if isinstance(state, str) else None,
        attributes=attributes if isinstance(attributes, Mapping) else {},
        show_attributes=() if tile is None else tile.show_attributes,
        state_tones=None if tile is None else tile.state_tones,
        colour_by_state=True if tile is None else tile.colour_by_state,
    )
    return {**entry, "selected": selected, **presentation_payload(hints)}


@dataclass(slots=True, eq=False)
class HubClient:
    """One connected client and its bounded outbound queue.

    Equality is identity: two clients with equal queues are still two clients,
    so the hub can keep them in a set.
    """

    queue: asyncio.Queue[str]
    next_heartbeat: float = 0.0
    """Loop-clock instant this client's next heartbeat is due.

    A heartbeat is sent only to a client that has been *silent*, because a
    delivered frame is itself the liveness proof.
    """


class StateHub:
    """Fan out store events and live state to every connected websocket client.

    The hub also reads the tile selection (HSD-u6k5) through the narrow
    :class:`~chime_deck.selection.SelectionLookup` seam, so it can be built
    with no selection at all — the default is
    :data:`~chime_deck.selection.NO_SELECTION`.
    """

    def __init__(
        self,
        store: ConfigStore,
        health: HealthRegistry,
        *,
        version: str = __version__,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        heartbeat_interval: float | None = DEFAULT_HEARTBEAT_INTERVAL,
        icons_dir: Path | None = None,
        selection: SelectionLookup = NO_SELECTION,
        live_entities: LiveEntityInfo = NO_LIVE_ENTITY_INFO,
        live_lookup_timeout: float = LIVE_LOOKUP_TIMEOUT_S,
    ) -> None:
        """Create a hub; nothing runs until :meth:`start` is awaited.

        :param store: configuration store read for snapshots and events.
        :param health: component health registry read for health messages.
        :param version: application version, logged when the hub starts.
        :param flush_interval: seconds between coalesced flushes.
        :param queue_size: maximum messages buffered per client.
        :param heartbeat_interval: seconds of silence after which a client is
            sent a ``heartbeat`` frame; ``None`` disables heartbeats, which is
            the shape a hub used without a socket wants.
        :param icons_dir: custom-icon directory; ``None`` means no user icon
            directory, so every ``custom:`` reference is a miss.
        :param selection: the read seam for tile selection; the default selects
            nothing, so a hub that is handed no selection service still serves
            every frame with ``selected: false``.
        :param live_entities: the live-data seam D16's icon steps ask; the
            default knows nothing. The lifespan installs the real adapter with
            :meth:`set_live_entities` once the websocket client exists, because
            that client cannot exist when the hub is constructed. The default
            keeps a hub standalone and configuration-only — the useful shape for
            a unit test and for an install with no Home Assistant.
        :param live_lookup_timeout: seconds one page's live lookup may take
            before the snapshot degrades to configuration-only rows.
        """
        self._store = store
        self._health = health
        self._version = version
        self._flush_interval = flush_interval
        self._queue_size = queue_size
        self._heartbeat_interval = heartbeat_interval
        self._icons_dir = icons_dir
        self._selection = selection
        self._live_entities = live_entities
        self._live_lookup_timeout = live_lookup_timeout
        self._clients: set[HubClient] = set()
        self._entity_states: dict[str, dict[str, object]] = {}
        # Entity id → the presentation facts its tile uses; ``None`` until the
        # first flush or snapshot reads them, so a tick with no pending delta
        # costs no store read.
        self._tile_options: dict[str, TileOptions] | None = None
        self._presses: list[dict[str, object]] = []
        # Entity ids whose selection changed since the last flush. The *value* is
        # not queued: it is read from the seam at flush time, so a toggle-on and
        # a toggle-off inside one tick coalesce to the final truth.
        self._selection_changes: set[str] = set()
        self._config_changes: dict[str, dict[str, object]] = {}
        self._health_dirty = False
        self._config_version = 0
        self._unsubscribe: Callable[[], None] | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._dropped = 0
        self._logger = logging.getLogger(__name__)

    @property
    def config_version(self) -> int:
        """Return the monotonic counter bumped by every configuration event."""
        return self._config_version

    @property
    def active_task_count(self) -> int:
        """Return how many background tasks the hub is still tracking."""
        return len(self._tasks)

    def register(self) -> HubClient:
        """Create a client with a bounded queue and start delivering to it."""
        client = HubClient(queue=asyncio.Queue(maxsize=self._queue_size))
        client.next_heartbeat = self._heartbeat_deadline(_loop_time())
        self._clients.add(client)
        self._logger.debug("websocket client registered (%d connected)", len(self._clients))
        return client

    def unregister(self, client: HubClient) -> None:
        """Stop delivering messages to *client*.

        :param client: client previously returned by :meth:`register`.
        """
        self._clients.discard(client)
        self._logger.debug("websocket client unregistered (%d connected)", len(self._clients))

    async def start(self) -> None:
        """Subscribe to store events and start the coalescing flush loop."""
        if self._unsubscribe is None:
            self._unsubscribe = self._store.events.subscribe(self._on_config_event)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop(), name="state-hub-flush")
            self._tasks.add(self._flush_task)
        self._logger.debug("state hub started version=%s", self._version)

    async def stop(self) -> None:
        """Unsubscribe and stop every background task. Safe to call twice.

        The wait is bounded by :data:`HUB_STOP_TIMEOUT_S` with
        :func:`asyncio.wait`, not with ``asyncio.timeout``/``wait_for`` around a
        ``gather``: cancelling a gather cannot bound a task that suppresses
        ``CancelledError``, while ``asyncio.wait`` returns the still-pending
        tasks and lets shutdown continue. A task that refuses to stop is
        abandoned, not awaited forever.
        """
        if self._unsubscribe is None and self._flush_task is None:
            return

        unsubscribe = self._unsubscribe
        self._unsubscribe = None
        if unsubscribe is not None:
            unsubscribe()

        self._flush_task = None
        tasks = tuple(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=HUB_STOP_TIMEOUT_S)
            for task in done:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None:
                    self._logger.error("a state hub task stopped with an error: %r", error)
            if pending:
                self._logger.warning(
                    "shutdown: the state hub did not stop within %g s", HUB_STOP_TIMEOUT_S
                )
        self._logger.info("state hub stopped (cancelled %d background task(s))", len(tasks))

    def notify_entity_state(
        self,
        *,
        entity_id: str,
        state: str | None,
        attributes: Mapping[str, object] | None = None,
        last_changed: str | None = None,
    ) -> None:
        """Queue an ``entity_state`` message, replacing any pending one for this entity.

        The frame the hub sends carries the presentation block too — the same
        ``displayed_state``, ``unit_of_measurement``, ``tone`` and
        ``attribute_hints`` a snapshot row carries — so the tile's word and its
        colour move together (HSD-7oto, HSD-d5x2).

        :param entity_id: Home Assistant entity id, e.g. ``lock.front_door``.
        :param state: new state, or ``None`` when unknown.
        :param attributes: attributes shown on the tile.
        :param last_changed: ISO-8601 timestamp of the change.
        """
        self._entity_states[entity_id] = {
            "entity_id": entity_id,
            "state": state,
            "attributes": dict(attributes) if attributes is not None else {},
            "last_changed": last_changed,
        }

    def notify_selection(self, *, entity_id: str) -> None:
        """Queue an ``entity_state`` frame carrying the new selection of one entity.

        The new *value* is deliberately not passed in: :meth:`flush_once` reads
        it from the selection seam, so a toggle-on followed by a toggle-off
        inside one flush tick coalesces to the final truth (the same "last value
        wins" rule ``entity_state`` already has).

        A selection-only frame carries just ``entity_id`` and ``selected``: the
        hub does not hold that entity's live state, and a frame that invented a
        ``state`` would blank the tile's word (HSD-7oto). A client merges an
        ``entity_state`` frame by key, so the tile keeps everything else it has.

        :param entity_id: Home Assistant entity id whose selection changed.
        """
        self._selection_changes.add(entity_id)

    def notify_press(
        self, *, button_id: int, button_name: str, outcome: str, pressed_at: str
    ) -> None:
        """Queue a ``press`` message. Presses are never coalesced away.

        :param button_id: id of the chime button that fired.
        :param button_name: display name of that button.
        :param outcome: result, e.g. ``played`` or ``failed``.
        :param pressed_at: ISO-8601 timestamp of the press.
        """
        self._presses.append(
            {
                "button_id": button_id,
                "button_name": button_name,
                "outcome": outcome,
                "pressed_at": pressed_at,
            }
        )

    def notify_health(self) -> None:
        """Queue a ``health`` message; only the latest registry state is sent."""
        self._health_dirty = True

    def set_live_entities(
        self, live_entities: LiveEntityInfo, *, timeout_s: float | None = None
    ) -> None:
        """Install the live-data seam used by every later snapshot.

        This is the lifespan's one install point: the hub is constructed before
        the Home Assistant websocket client exists, so the client is handed over
        here instead. Calling it twice replaces the previous seam, and calling it
        with the default :data:`~chime_deck.entity_info.NO_LIVE_ENTITY_INFO`
        returns the hub to configuration-only rows.

        :param live_entities: the seam the snapshot asks for live data.
        :param timeout_s: seconds one lookup may take; ``None`` keeps the bound
            the hub was constructed with.
        """
        self._live_entities = live_entities
        if timeout_s is not None:
            self._live_lookup_timeout = timeout_s

    async def snapshot_message(self) -> dict[str, object]:
        """Build the full ``snapshot`` message sent when a client connects.

        Each entity row is design 05 §4's shape plus the presentation keys the
        tile needs (``display_name``, ``icon_source``, ``displayed_state``,
        ``unit_of_measurement``, ``tone`` and ``attribute_hints``); §4's own row
        omits ``display_name`` and has no ``icon_source``.

        The rows carry live data through :attr:`_live_entities`: one bounded
        lookup for the whole page, never one per row, so a page costs at most
        :attr:`_live_lookup_timeout` seconds. When the seam answers ``None``
        (nothing configured, disconnected, or too slow) every row degrades to
        configuration-only — the state, attributes and last-change fields stay
        empty and the icon comes from the stored override or the domain table,
        exactly as the hub behaved before Home Assistant was wired in.

        ``selected`` is read from the selection seam per row (HSD-u6k5): a client
        that connects mid-session therefore sees the current selection without
        waiting for a toggle.

        ``hide_when_unavailable`` is the one presentation flag the store row
        already carried and this snapshot used to omit; HSD-ckhh's UI had to
        work around that, so the flag now travels with every tile.
        """
        rows = await self._store.list_entities()
        # The same map a live ``entity_state`` frame reads, refreshed here for
        # free while the rows are in hand.
        options = self._tile_options = _tile_options_map(rows)
        contexts = await live_contexts(
            self._live_entities,
            [entity.entity_id for entity in rows],
            timeout_s=self._live_lookup_timeout,
        )
        entities: list[dict[str, object]] = []
        for entity in rows:
            context = None if contexts is None else contexts.get(entity.entity_id)
            choice = resolve_icon(entity.icon, entity.entity_id, context, icons_dir=self._icons_dir)
            state = None if context is None else context.state
            # ``dict()``, not the read-only mapping: ``attributes`` is a
            # ``MappingProxyType`` and ``json`` cannot serialise it.
            attributes: dict[str, object] = {} if context is None else dict(context.attributes)
            hints = presentation_hints(
                domain_of(entity.entity_id),
                state=state,
                attributes=attributes,
                show_attributes=options[entity.entity_id].show_attributes,
                state_tones=entity.state_tones,
                colour_by_state=entity.colour_by_state,
            )
            entities.append(
                {
                    "id": entity.id,
                    "entity_id": entity.entity_id,
                    "display_name": entity.display_name,
                    "state": state,
                    "attributes": attributes,
                    "icon": choice.icon,
                    "icon_source": choice.source.value,
                    "accent_color": entity.accent_color,
                    "last_changed": None if context is None else context.last_changed,
                    **presentation_payload(hints),
                    "hide_when_unavailable": entity.hide_when_unavailable,
                    "selected": self._selection.is_selected(entity.entity_id),
                }
            )
        buttons: list[dict[str, object]] = [
            {
                "id": button.id,
                "name": button.name,
                "slug": button.slug,
                "show_on_dashboard": button.show_on_dashboard,
            }
            for button in await self._store.list_buttons()
        ]
        return {
            "type": "snapshot",
            "data": {
                "entities": entities,
                "buttons": buttons,
                "health": self._health.snapshot(),
                "config_version": self._config_version,
            },
        }

    async def next_payload(self, client: HubClient) -> str:
        """Wait for the next encoded message for *client*.

        :param client: client previously returned by :meth:`register`.
        """
        return await client.queue.get()

    async def flush_once(self) -> None:
        """Drain the coalescing buffers and broadcast them now.

        Never sleeps, so tests drive it directly instead of waiting on the tick.
        The tile-options map is read only when a delta is actually pending: a
        tick that carries nothing costs no store read.

        A pending selection for an entity that also has a pending state delta
        rides on that delta's frame, so one change of that entity is one frame. A
        selection with no state delta of its own becomes a minimal
        ``entity_state`` frame instead (see :meth:`notify_selection`).
        """
        entity_states = self._entity_states
        self._entity_states = {}
        presses = self._presses
        self._presses = []
        config_changes = self._config_changes
        self._config_changes = {}
        health_dirty = self._health_dirty
        self._health_dirty = False
        selection_changes = self._selection_changes
        self._selection_changes = set()

        payloads: list[str] = []
        if entity_states:
            options = await self._tile_options_by_entity()
            payloads.extend(
                self._encode(
                    "entity_state",
                    _entity_state_data(
                        entity_id,
                        data,
                        options,
                        selected=self._selection.is_selected(entity_id),
                    ),
                )
                for entity_id, data in entity_states.items()
            )
        # Sorted, so the frame order of a multi-tile tick is deterministic: a set
        # of strings iterates in hash order, which changes between processes.
        payloads.extend(
            self._encode(
                "entity_state",
                {"entity_id": entity_id, "selected": self._selection.is_selected(entity_id)},
            )
            for entity_id in sorted(selection_changes.difference(entity_states))
        )
        payloads.extend(self._encode("press", data) for data in presses)
        payloads.extend(self._encode("config_changed", data) for data in config_changes.values())
        if health_dirty:
            payloads.append(self._encode("health", self._health.message()))

        now = _loop_time()
        for payload in payloads:
            self._broadcast(payload, now)

    def heartbeat_if_due(self, now: float) -> int:
        """Send one ``heartbeat`` frame to every client that has been silent.

        A heartbeat means "nothing to say, still here": a client that received
        any frame has already proved it is alive, so only a client whose
        deadline has passed is served, and serving it resets that deadline.

        :param now: loop-clock instant to compare the deadlines against, so a
            caller drives the schedule exactly instead of sleeping.
        :returns: how many clients were served a heartbeat.
        """
        interval = self._heartbeat_interval
        if interval is None:
            return 0
        due = [client for client in tuple(self._clients) if client.next_heartbeat <= now]
        if not due:
            return 0
        payload = self._encode("heartbeat", {"interval_s": interval})
        for client in due:
            self._offer(client, payload, now)
        return len(due)

    async def _flush_loop(self) -> None:
        """Flush the coalescing buffers every ``flush_interval`` seconds."""
        while True:
            await asyncio.sleep(self._flush_interval)
            await self.flush_once()
            self.heartbeat_if_due(_loop_time())

    async def _tile_options_by_entity(self) -> dict[str, TileOptions]:
        """Return the tile-options map, reading the store at most once per change.

        A tile's presentation facts change only on an entity write, which the hub
        sees as a configuration event, so the map is cached and invalidated
        there rather than re-read on every ten-per-second tick.

        :returns: entity id → that tile's presentation facts. A store that
            cannot be read degrades to an empty map instead of failing the
            flush.
        """
        if self._tile_options is not None:
            return self._tile_options
        try:
            rows = await self._store.list_entities()
        except Exception:
            # Cached, so this warns once rather than on every tick: the live
            # frames carry no presentation block until the next entity change,
            # which is a degraded tile, not a broken hub.
            self._logger.warning(
                "could not read the entity list, so live frames carry no presentation "
                "block until the next entity change",
                exc_info=True,
            )
            self._tile_options = {}
            return self._tile_options
        self._tile_options = _tile_options_map(rows)
        return self._tile_options

    def _on_config_event(self, event: ConfigEvent) -> None:
        """Record a store event as a coalesced ``config_changed`` message.

        The counter lives here rather than in the store schema: the UI only
        needs to know that *something* changed and how many changes it has seen.
        An entity write also invalidates the cached tile-options map, so the next
        flush re-reads it.

        :param event: configuration event published by the store.
        """
        self._config_version += 1
        self._config_changes[event.what] = {"what": event.what}
        if event.name is ConfigEventName.ENTITIES_CHANGED:
            self._tile_options = None

    @staticmethod
    def _encode(message_type: str, data: Mapping[str, object]) -> str:
        """Encode one ``{"type": ..., "data": ...}`` envelope.

        :param message_type: one of the six message types this hub emits: the
            five from design 05 §4 plus the liveness ``heartbeat``.
        :param data: message payload.
        """
        return json.dumps({"type": message_type, "data": data})

    def _heartbeat_deadline(self, now: float) -> float:
        """Return the instant a client idle from *now* needs a heartbeat.

        :param now: loop-clock instant of the frame that resets the deadline.
        :returns: ``now`` plus the interval, or :data:`math.inf` when
            heartbeats are disabled so no comparison is ever true.
        """
        if self._heartbeat_interval is None:
            return math.inf
        return now + self._heartbeat_interval

    def _broadcast(self, payload: str, now: float) -> None:
        """Offer one encoded message to every client, dropping the oldest on overflow.

        :param payload: already-encoded websocket text frame.
        :param now: loop-clock instant the frame is offered at.
        """
        for client in tuple(self._clients):
            self._offer(client, payload, now)

    def _offer(self, client: HubClient, payload: str, now: float) -> None:
        """Offer one encoded message to one client and reset its heartbeat deadline.

        Drop-oldest on overflow, so a slow tablet can neither block the loop nor
        grow the process without bound. Resetting the deadline here is what
        makes a delivered frame the liveness proof: only a client that received
        nothing needs a heartbeat.

        :param client: client the message is for.
        :param payload: already-encoded websocket text frame.
        :param now: loop-clock instant the frame is offered at.
        """
        queue = client.queue
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            self._dropped += 1
            self._logger.debug(
                "dropped oldest message for a slow client (%d dropped total)", self._dropped
            )
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(payload)
        client.next_heartbeat = self._heartbeat_deadline(now)
