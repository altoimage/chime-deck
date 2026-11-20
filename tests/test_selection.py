"""Runtime tile selection (D29): the volatile set and its change callback.

Pure and synchronous: no store, no loop, no socket, which is the point — the
selection is runtime state that is never persisted (HSD-u6k5).
"""

from __future__ import annotations

from chime_deck.selection import (
    NO_SELECTION,
    NoSelection,
    SelectionLookup,
    SelectionService,
)


def test_toggle_flips_both_ways_and_returns_the_new_value() -> None:
    """``toggle`` reports the state it just produced, not the one it replaced."""
    selection = SelectionService()

    assert selection.is_selected("lock.front_door") is False
    assert selection.toggle("lock.front_door") is True
    assert selection.is_selected("lock.front_door") is True
    assert selection.toggle("lock.front_door") is False
    assert selection.is_selected("lock.front_door") is False


def test_two_entity_ids_are_independent() -> None:
    """Selecting one tile says nothing about any other tile."""
    selection = SelectionService()

    selection.toggle("lock.front_door")

    assert selection.is_selected("lock.front_door") is True
    assert selection.is_selected("light.hall") is False
    assert selection.selected_ids() == {"lock.front_door"}


def test_selection_is_keyed_on_entity_id_not_position() -> None:
    """Reordering the tile list changes nothing: the keys are entity ids."""
    selection = SelectionService()
    order = ["lock.front_door", "light.hall", "sensor.kitchen"]
    selection.toggle(order[0])
    selection.toggle(order[2])

    order.reverse()

    assert selection.selected_ids() == {"lock.front_door", "sensor.kitchen"}
    assert [selection.is_selected(entity_id) for entity_id in order] == [True, False, True]


def test_discard_drops_exactly_one() -> None:
    """``discard`` removes one key and reports whether it did."""
    selection = SelectionService()
    selection.toggle("lock.front_door")
    selection.toggle("light.hall")

    assert selection.discard("lock.front_door") is True
    assert selection.selected_ids() == {"light.hall"}
    assert selection.discard("lock.front_door") is False
    assert selection.selected_ids() == {"light.hall"}


def test_clear_empties_the_set() -> None:
    """``clear`` is the startup path: nothing survives it."""
    selection = SelectionService()
    selection.toggle("lock.front_door")
    selection.toggle("light.hall")

    selection.clear()

    assert selection.selected_ids() == frozenset()
    assert selection.is_selected("lock.front_door") is False


def test_on_change_receives_the_entity_id_and_the_new_value() -> None:
    """The seam HSD-fro0 subscribes to is told about both directions."""
    calls: list[tuple[str, bool]] = []
    selection = SelectionService(
        on_change=lambda entity_id, value: calls.append((entity_id, value))
    )

    selection.toggle("lock.front_door")
    selection.toggle("lock.front_door")

    assert calls == [("lock.front_door", True), ("lock.front_door", False)]


def test_discard_and_clear_do_not_notify() -> None:
    """Deletion and startup are not toggles, so the callback stays silent."""
    calls: list[tuple[str, bool]] = []
    selection = SelectionService(
        on_change=lambda entity_id, value: calls.append((entity_id, value))
    )
    selection.toggle("lock.front_door")
    calls.clear()

    selection.discard("lock.front_door")
    selection.clear()

    assert calls == []


def test_set_on_change_none_silences_the_callback() -> None:
    """The hub can be detached again, like ``HealthRegistry.set_on_change(None)``."""
    calls: list[tuple[str, bool]] = []
    selection = SelectionService(
        on_change=lambda entity_id, value: calls.append((entity_id, value))
    )
    selection.toggle("lock.front_door")

    selection.set_on_change(None)
    selection.toggle("lock.front_door")

    assert calls == [("lock.front_door", True)]


def test_selected_ids_is_a_snapshot() -> None:
    """The copy a caller holds is unaffected by later toggles."""
    selection = SelectionService()
    selection.toggle("lock.front_door")

    snapshot = selection.selected_ids()
    selection.toggle("light.hall")

    assert snapshot == frozenset({"lock.front_door"})
    assert selection.selected_ids() == {"lock.front_door", "light.hall"}


def test_the_default_seam_selects_nothing() -> None:
    """``NO_SELECTION`` satisfies the Protocol and always answers ``False``."""
    assert isinstance(NO_SELECTION, SelectionLookup)
    assert isinstance(NoSelection(), SelectionLookup)
    assert NO_SELECTION.is_selected("lock.front_door") is False


def test_the_service_satisfies_the_lookup_contract() -> None:
    """The hub is handed the real service through the ``is_selected`` member alone."""
    selection: SelectionLookup = SelectionService()

    assert isinstance(selection, SelectionLookup)
    assert selection.is_selected("lock.front_door") is False
