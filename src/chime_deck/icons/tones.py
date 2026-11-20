"""The state→tone vocabulary and the default table that seeds it.

A tile's colour is a claim about the state, so the vocabulary is deliberately
tiny and fixed: ``ok`` (green), ``warn`` (amber), ``alert`` (red) and ``off``
(grey). No helper here invents a fifth colour, because a new shade is a design
decision, not something one entity's configuration should be able to make.

The default table lives in this module rather than in a data migration: a
release can improve which states read as ``warn`` or ``ok`` and every installed
tile follows on upgrade, with no migration and no rewrite of rows a user never
touched. A per-tile mapping is therefore *overrides only* — an explicit entry
always wins over the default, which is what lets one tile disagree with the
built-in table without freezing the rest of it.

``unavailable`` and ``unknown`` are never mapped. A tone is a claim about a
state, and "unavailable" is the absence of one rather than a state that happens
to be grey, so the two are special-cased before any lookup and cannot be
remapped into a colour that would assert something the device does not know.

Owning ticket: HSD-d5x2.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

__all__ = [
    "DEFAULT_STATE_TONES",
    "NEVER_MAPPED",
    "TONES",
    "TONE_ALERT",
    "TONE_OFF",
    "TONE_OK",
    "TONE_WARN",
    "is_tone",
    "state_tone",
]

TONE_OK: Final = "ok"
TONE_WARN: Final = "warn"
TONE_ALERT: Final = "alert"
TONE_OFF: Final = "off"

TONES: Final[tuple[str, ...]] = (TONE_OK, TONE_WARN, TONE_ALERT, TONE_OFF)
"""The ordered vocabulary, mirrored by the tile editor's segmented control."""

NEVER_MAPPED: Final[frozenset[str]] = frozenset({"unavailable", "unknown"})
"""States that describe the absence of a state and so carry no tone claim."""

DEFAULT_STATE_TONES: Final[dict[str, str]] = {
    # The owner's appliance vocabulary: a machine that is working is worth an
    # amber glance, a finished cycle is the good news, and a machine at rest is
    # grey. Adding a new default state later is a single line here.
    "running": TONE_WARN,
    "washing": TONE_WARN,
    "drying": TONE_WARN,
    "rinsing": TONE_WARN,
    "spinning": TONE_WARN,
    "pre-wash": TONE_WARN,
    "heating": TONE_WARN,
    "finished": TONE_OK,
    "complete": TONE_OK,
    "done": TONE_OK,
    "idle": TONE_OFF,
    "standby": TONE_OFF,
    "off": TONE_OFF,
    # The states the deleted frontend table used to carry, kept so no installed
    # tile loses its tone when that table goes away.
    "locked": TONE_OK,
    "closed": TONE_OK,
    "unlocked": TONE_ALERT,
    "jammed": TONE_ALERT,
    "open": TONE_WARN,
    "on": TONE_WARN,
    "locking": TONE_WARN,
    "unlocking": TONE_WARN,
    "opening": TONE_WARN,
    "closing": TONE_WARN,
    "detected": TONE_WARN,
}
"""Built-in state→tone table, applied below any explicit per-tile override."""


def is_tone(value: object) -> bool:
    """Return whether *value* is one of the four tones.

    The import path uses this to reject a stored tone instead of writing a
    colour the UI cannot render.

    :param value: candidate tone, of any type a decoded document may hold.
    """
    return isinstance(value, str) and value in TONES


def state_tone(
    state: str | None,
    *,
    overrides: Mapping[str, str] | None = None,
    colour_by_state: bool = True,
) -> str | None:
    """Return the tone one raw state should be drawn in.

    The precedence is **explicit → default → grey/raw**:

    1. no state at all — ``None``, empty or whitespace-only — gives ``None``,
       because there is nothing to colour and the caller's tile shows
       "Unknown";
    2. ``unavailable`` and ``unknown`` give ``off``, checked before any lookup
       so a stored override can never colour an absence of state;
    3. ``colour_by_state`` false gives ``off``, the per-tile opt-out where the
       tile is neutral in every state and its word carries the state instead;
    4. an exact key in *overrides* gives its tone — keys are stored verbatim,
       case-sensitively, and are never normalised, so what a user typed is what
       is looked up;
    5. :data:`DEFAULT_STATE_TONES` gives the built-in tone for the state;
    6. anything else gives ``off``, the grey of an unmapped state whose word
       stays the raw title-cased state.

    :param state: raw Home Assistant state string, or ``None`` when none is
        known.
    :param overrides: explicit per-tile state→tone entries, or ``None`` for
        none; ``None`` and an empty mapping behave identically.
    :param colour_by_state: false when the tile opted out of state colouring.
    """
    if state is None or not state.strip():
        return None
    if state in NEVER_MAPPED:
        return TONE_OFF
    if not colour_by_state:
        return TONE_OFF
    if overrides is not None:
        explicit = overrides.get(state)
        if explicit is not None:
            return explicit
    default = DEFAULT_STATE_TONES.get(state)
    if default is not None:
        return default
    return TONE_OFF
