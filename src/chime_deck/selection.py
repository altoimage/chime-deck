"""Runtime tile-selection state (D29): one volatile set of entity ids.

A tile is *selected* when the operator taps it, and the conditional alarms and
automations that read the selection act on it (D29). The selection is **volatile
by design** (D29 §4): a power blip has to *disable* the conditional alarms rather
than leave one armed forever, so the set lives in memory only and every restart
starts with nothing selected.

**It is never persisted.** There is no SQLite column, no migration and no
settings key for it: a configuration export or import cannot carry a selection
across, the store knows nothing about it, and the store never emits an event for
it. Nothing in this module imports a database module, on purpose.

The set is keyed on the Home Assistant ``entity_id`` string
(``lock.front_door``), never on a row id and never on a list position, so
reordering the tiles is a no-op for the selection.

:meth:`SelectionService.set_on_change` is the seam the MQTT publisher
subscribes to: it receives ``(entity_id, selected)`` after every successful
change, so the broker announcement and the websocket frame come from one place
and can never disagree.

**Two writers, one state (D34).** A panel tap flips a tile through
:meth:`SelectionService.toggle`; an inbound Home Assistant command sets an
explicit value through :meth:`SelectionService.set`. Both write the same set and
both leave through the one ``on_change`` callback, so there is no second path to
the broker or to the hub.

The hub reads this state through :class:`SelectionLookup` only, so a hub can be
built with no selection service at all (:data:`NO_SELECTION`) — the shape a unit
test and a build with no selection wants. A consumer that also *writes* asks for
:class:`SelectionWriter` instead, which is the same seam plus the setter.

Owning ticket: HSD-u6k5; the write seam was added by D34 (HSD-8a5f).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "NO_SELECTION",
    "NoSelection",
    "SelectionLookup",
    "SelectionService",
    "SelectionWriter",
]


@runtime_checkable
class SelectionLookup(Protocol):
    """The read seam for tile selection, shaped like the live-data seam.

    The websocket hub asks exactly one question, so it can be handed the real
    :class:`SelectionService`, :data:`NO_SELECTION`, or a test fake. ``False``
    for an unknown id is the contract: an unselected tile is never an error.
    """

    def is_selected(self, entity_id: str) -> bool:
        """Return whether *entity_id* is selected right now.

        :param entity_id: Home Assistant entity id, e.g. ``lock.front_door``.
        """
        ...


@runtime_checkable
class SelectionWriter(SelectionLookup, Protocol):
    """The write seam D34 needs: a command sets a VALUE, it does not flip one.

    An inbound Home Assistant command names the value it wants (``turn_on``,
    ``turn_off``), never "the other one", so the single write method takes the
    value: a command delivered twice must be a no-op rather than a toggle back,
    and a command that repeats the current value must produce no change at all.
    That is why this is :meth:`SelectionService.set` and not ``toggle``.

    The protocol extends the read seam because a caller that may write almost
    always reads first ("is this already the value?"), and one seam with both
    halves is what lets every consumer accept the real service,
    :data:`NO_SELECTION`, or a test fake without an ``isinstance`` guard of its
    own.
    """

    def set(self, entity_id: str, selected: bool) -> bool:
        """Force *entity_id* to *selected* and return the value in effect.

        :param entity_id: Home Assistant entity id, e.g. ``lock.front_door``.
        :param selected: the value to force, not a value to flip to its opposite.
        :returns: the value now in effect for *entity_id*.
        """
        ...


@dataclass(frozen=True, slots=True)
class NoSelection:
    """Default lookup: nothing is ever selected, and nothing can become selected."""

    def is_selected(self, entity_id: str) -> bool:
        """Return ``False``: this build tracks no selection at all.

        :param entity_id: Home Assistant entity id; ignored.
        """
        return False

    def set(self, entity_id: str, selected: bool) -> bool:
        """Return ``False``: a build with no selection service cannot be commanded.

        There is nowhere to store the value and no callback to tell, so the write
        is refused rather than silently accepted. A caller can tell the refusal
        from an accepted write by reading the return value, which is how the MQTT
        command handler decides to republish the (unchanged) state.

        :param entity_id: Home Assistant entity id; ignored.
        :param selected: the requested value; ignored.
        """
        return False


NO_SELECTION: Final = NoSelection()
"""The default seam: a hub built without a selection service selects nothing."""


class SelectionService:
    """Own the volatile selection set and tell one callback about every change.

    The service is the only writer of the set, so the response, the websocket
    frame and the MQTT announcement can never disagree. An optional
    ``on_change`` callback lets the hub react without this module knowing that a
    websocket exists, exactly like
    :class:`~chime_deck.health.HealthRegistry`.

    It is also the single place a change is *notified* from: :meth:`toggle` and
    :meth:`set` differ only in how they compute the value, never in what they do
    with it.
    """

    def __init__(self, *, on_change: Callable[[str, bool], None] | None = None) -> None:
        """Create an empty selection: every tile starts unselected.

        :param on_change: called with ``(entity_id, selected)`` after every
            successful :meth:`toggle` or :meth:`set`.
        """
        self._selected: set[str] = set()
        self._on_change = on_change

    def set_on_change(self, on_change: Callable[[str, bool], None] | None) -> None:
        """Install the change callback after construction.

        :param on_change: called with ``(entity_id, selected)`` after every
            successful :meth:`toggle` or :meth:`set`; ``None`` silences it.
        """
        self._on_change = on_change

    def is_selected(self, entity_id: str) -> bool:
        """Return whether *entity_id* is selected.

        :param entity_id: Home Assistant entity id, e.g. ``lock.front_door``.
        """
        return entity_id in self._selected

    def toggle(self, entity_id: str) -> bool:
        """Flip the selection of *entity_id*, notify, and return the new value.

        The panel's write: a tap always flips, and a tap always notifies, so this
        is :meth:`set` of the flipped value.

        :param entity_id: Home Assistant entity id, e.g. ``lock.front_door``.
        :returns: ``True`` when the entity is selected now, ``False`` when this
            call deselected it.
        """
        return self.set(entity_id, entity_id not in self._selected)

    def set(self, entity_id: str, selected: bool) -> bool:
        """Force the selection of *entity_id*, notify once, and return the value.

        The write D34 needs for an inbound Home Assistant command. A command
        carries the value it wants, so this is a set and not a :meth:`toggle`:
        two deliveries of the same ``on`` must leave the tile on, and a command
        that repeats the current value must change nothing.

        A value already in effect therefore does **not** call ``on_change``. The
        callback is the one point that produces the hub frame and the broker
        publish, so an idempotent confirm has to produce neither — otherwise a
        repeat command would cost a websocket frame and a second retained state
        publish for a value that did not move.

        :param entity_id: Home Assistant entity id, e.g. ``lock.front_door``.
        :param selected: the value to force.
        :returns: the value now in effect, which is *selected* whenever this
            service accepted the write.
        """
        if self.is_selected(entity_id) == selected:
            return selected
        if selected:
            self._selected.add(entity_id)
        else:
            self._selected.discard(entity_id)
        if self._on_change is not None:
            self._on_change(entity_id, selected)
        return selected

    def discard(self, entity_id: str) -> bool:
        """Drop *entity_id* from the selection, without notifying.

        Deletion is not a toggle: the row this id named is gone, so nothing has
        to be announced for it beyond the store's own configuration event.

        :param entity_id: Home Assistant entity id that was deleted.
        :returns: ``True`` when this call dropped it, ``False`` for an id that
            was not selected — so discarding an unknown key is a no-op.
        """
        if entity_id not in self._selected:
            return False
        self._selected.discard(entity_id)
        return True

    def clear(self) -> None:
        """Empty the selection, without notifying.

        The lifespan calls this once at startup, before the hub can deliver
        anything: a selection never survives a restart, so nobody may observe a
        stale value and no broadcast goes to nobody.
        """
        self._selected.clear()

    def selected_ids(self) -> frozenset[str]:
        """Return every selected entity id as an immutable copy.

        :returns: the selected entity ids, unaffected by later toggles.
        """
        return frozenset(self._selected)
