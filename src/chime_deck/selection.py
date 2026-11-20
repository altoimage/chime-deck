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

:meth:`SelectionService.set_on_change` is the seam the future MQTT publisher
(HSD-fro0) subscribes to: it receives ``(entity_id, selected)`` after every
successful toggle, so the broker announcement and the websocket frame come from
one place. The hub reads this state through :class:`SelectionLookup` only, so a
hub can be built with no selection service at all (:data:`NO_SELECTION`) — the
shape a unit test and a build with no selection wants.

Owning ticket: HSD-u6k5.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

__all__ = ["NO_SELECTION", "NoSelection", "SelectionLookup", "SelectionService"]


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


@dataclass(frozen=True, slots=True)
class NoSelection:
    """Default lookup: nothing is ever selected, and nothing can become selected."""

    def is_selected(self, entity_id: str) -> bool:
        """Return ``False``: this build tracks no selection at all.

        :param entity_id: Home Assistant entity id; ignored.
        """
        return False


NO_SELECTION: Final = NoSelection()
"""The default seam: a hub built without a selection service selects nothing."""


class SelectionService:
    """Own the volatile selection set and tell one callback about every toggle.

    The service is the only writer of the set, so the response, the websocket
    frame and the future MQTT announcement can never disagree. An optional
    ``on_change`` callback lets the hub react without this module knowing that a
    websocket exists, exactly like
    :class:`~chime_deck.health.HealthRegistry`.
    """

    def __init__(self, *, on_change: Callable[[str, bool], None] | None = None) -> None:
        """Create an empty selection: every tile starts unselected.

        :param on_change: called with ``(entity_id, selected)`` after every
            successful :meth:`toggle`.
        """
        self._selected: set[str] = set()
        self._on_change = on_change

    def set_on_change(self, on_change: Callable[[str, bool], None] | None) -> None:
        """Install the change callback after construction.

        :param on_change: called with ``(entity_id, selected)`` after every
            successful :meth:`toggle`; ``None`` silences it.
        """
        self._on_change = on_change

    def is_selected(self, entity_id: str) -> bool:
        """Return whether *entity_id* is selected.

        :param entity_id: Home Assistant entity id, e.g. ``lock.front_door``.
        """
        return entity_id in self._selected

    def toggle(self, entity_id: str) -> bool:
        """Flip the selection of *entity_id*, notify, and return the new value.

        :param entity_id: Home Assistant entity id, e.g. ``lock.front_door``.
        :returns: ``True`` when the entity is selected now, ``False`` when this
            call deselected it.
        """
        selected = entity_id not in self._selected
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
