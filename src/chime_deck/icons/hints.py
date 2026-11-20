"""Presentation hints derived from one Home Assistant state.

These helpers turn raw state and attributes into the words a tile can show.
Nothing is invented: a missing value stays missing, and an attribute the state
does not carry is omitted rather than defaulted. That makes the no-live-data
path render configuration-only tiles without pretending to know more.

The word is a word: the state phrasing vocabulary that used to guess sentences
like "the door is open" is gone, and every state now renders as a short label
with only its **first letter** normalised to upper case (``pre-wash`` reads
``Pre-wash`` and ``not_home`` stays ``Not_home``). There is no vocabulary and no
per-word override here, so a hyphen, an underscore and any already-capitalised
word survive untouched and the transform is idempotent.

The colour travels with the word: :class:`PresentationHints` carries the
``tone`` :func:`~chime_deck.icons.tones.state_tone` picks for the same raw
state, so one call decides both what the tile says and what colour it wears.

Owning ticket: HSD-jwd3; the title-casing rule and the tone field were added by
HSD-d5x2.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from .tones import state_tone

__all__ = [
    "AttributeHint",
    "PresentationHints",
    "humanise_attribute_name",
    "presentation_hints",
    "presentation_payload",
]

_STATE_WORDING: Final[dict[str, dict[str, str]]] = {
    "binary_sensor:door": {"on": "open", "off": "closed"},
    "binary_sensor:window": {"on": "open", "off": "closed"},
    "binary_sensor:garage_door": {"on": "open", "off": "closed"},
    "binary_sensor:opening": {"on": "open", "off": "closed"},
    "binary_sensor:motion": {"on": "detected", "off": "clear"},
    "binary_sensor:occupancy": {"on": "detected", "off": "clear"},
    "binary_sensor:presence": {"on": "detected", "off": "clear"},
    "binary_sensor:moisture": {"on": "wet", "off": "dry"},
    "binary_sensor:connectivity": {"on": "connected", "off": "disconnected"},
    "cover": {
        "open": "open",
        "closed": "closed",
        "opening": "opening",
        "closing": "closing",
    },
    "lock": {
        "locked": "locked",
        "unlocked": "unlocked",
        "jammed": "jammed",
        "locking": "locking",
        "unlocking": "unlocking",
    },
    "person": {"home": "home", "not_home": "away"},
    "device_tracker": {"home": "home", "not_home": "away"},
}

_DEFAULT_WORDING: Final[dict[str, str]] = {"on": "on", "off": "off"}


@dataclass(frozen=True, slots=True)
class AttributeHint:
    """One ``show_attributes`` entry resolved to something a tile can render."""

    name: str
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class PresentationHints:
    """How one state should be presented; every field degrades instead of guessing."""

    state: str | None
    displayed_state: str | None
    unit_of_measurement: str | None
    attribute_hints: tuple[AttributeHint, ...]
    tone: str | None
    """The colour :func:`~chime_deck.icons.tones.state_tone` picked, or
    ``None`` when there is no state to colour."""


def humanise_attribute_name(name: str) -> str:
    """Turn ``battery_level`` into ``Battery level``.

    :param name: raw attribute name.
    """
    parts = [part for part in name.split("_") if part]
    if not parts:
        return ""
    head, *tail = parts
    return " ".join([head.capitalize(), *(part.lower() for part in tail)])


def presentation_hints(
    domain: str,
    *,
    state: str | None,
    attributes: Mapping[str, object],
    show_attributes: Iterable[str] = (),
    state_tones: Mapping[str, str] | None = None,
    colour_by_state: bool = True,
) -> PresentationHints:
    """Build the presentation hints for one state.

    Nothing is invented: an absent state gives ``None``, an absent
    ``unit_of_measurement`` gives ``None``, and a ``show_attributes`` name that the
    attributes do not carry is omitted rather than defaulted. The word and the
    tone are decided by the same call, so a tile can never be handed a new word
    with the previous state's colour.

    :param domain: entity domain, e.g. ``"binary_sensor"``.
    :param state: raw state, or ``None`` when no state is known.
    :param attributes: live state attributes.
    :param show_attributes: attribute names the tile wants rendered, in order.
    :param state_tones: explicit per-tile state→tone overrides, or ``None`` for
        none; the built-in default table still applies to every other state.
    :param colour_by_state: false when the tile opted out of state colouring, so
        every state is drawn in the neutral ``off`` tone.
    """
    device_class = _device_class_for(attributes)
    unit = attributes.get("unit_of_measurement")
    unit_of_measurement = unit.strip() if isinstance(unit, str) and unit.strip() else None

    hints: list[AttributeHint] = []
    for name in show_attributes:
        if not name:
            continue
        if name not in attributes:
            continue
        value = attributes[name]
        if value is None:
            continue
        hints.append(
            AttributeHint(
                name=name,
                label=humanise_attribute_name(name),
                value=_format_value(value),
            )
        )

    return PresentationHints(
        state=state,
        displayed_state=_displayed_state(domain, device_class, state),
        unit_of_measurement=unit_of_measurement,
        attribute_hints=tuple(hints),
        tone=state_tone(state, overrides=state_tones, colour_by_state=colour_by_state),
    )


def presentation_payload(hints: PresentationHints) -> dict[str, object]:
    """Reshape presentation hints into the JSON-ready keys a tile consumes.

    This is the one wire shape for those keys: a ``snapshot`` row and an
    ``entity_state`` delta both spread its result, so the word, unit, tone and
    attribute hints a tile reads on connect are produced by exactly the same
    code as the ones it reads on every later change (HSD-7oto). Two shapes for
    the same facts let a tile keep a stale word while its colour moved on.

    The tone rides in this one shape for the same reason (HSD-d5x2): both the
    snapshot row and the ``entity_state`` delta spread it, so a tile's colour
    cannot drift from its word any more than the word can drift from its unit.

    Nothing is decided here, only reshaped: every value, including which
    attribute names are shown at all, comes from :func:`presentation_hints`.

    :param hints: hints built for one state by :func:`presentation_hints`.
    :returns: ``displayed_state``, ``unit_of_measurement``, ``tone`` and the
        ``attribute_hints`` list of ``{"name", "label", "value"}`` objects.
    """
    return {
        "displayed_state": hints.displayed_state,
        "unit_of_measurement": hints.unit_of_measurement,
        "tone": hints.tone,
        "attribute_hints": [
            {"name": hint.name, "label": hint.label, "value": hint.value}
            for hint in hints.attribute_hints
        ],
    }


def _displayed_state(domain: str, device_class: str | None, state: str | None) -> str | None:
    """Return the rendered word for *state*, or ``None`` when no state is known.

    Every branch is title-cased: the state phrasing vocabulary that used to guess
    sentences is gone, so a word is just a word and the only normalisation is its
    first letter (see :func:`_title_case`). A token neither table knows still
    renders, capitalised, rather than being hidden.

    :param domain: entity domain.
    :param device_class: device class from the attributes, or ``None``.
    :param state: raw state, or ``None``.
    """
    if state is None:
        return None
    if device_class is not None:
        class_wording = _STATE_WORDING.get(f"{domain}:{device_class}")
        if class_wording is not None and state in class_wording:
            return _title_case(class_wording[state])
    domain_wording = _STATE_WORDING.get(domain)
    if domain_wording is not None and state in domain_wording:
        return _title_case(domain_wording[state])
    return _title_case(_DEFAULT_WORDING.get(state, state))


def _title_case(word: str | None) -> str | None:
    """Upper-case the first character of *word* and leave the rest untouched.

    A word is a word: there is no vocabulary and no word override here, so
    ``running`` reads ``Running`` while ``pre-wash`` reads ``Pre-wash`` and
    ``not_home`` keeps its underscore. An absent word stays absent, and an
    already-capitalised one is unchanged, which makes the transform idempotent.

    :param word: rendered word, or ``None`` when there is none.
    :returns: the word with its first character upper-cased, or ``None``.
    """
    if word is None:
        return None
    return word[:1].upper() + word[1:]


def _device_class_for(attributes: Mapping[str, object]) -> str | None:
    """Return a usable device class from *attributes*, or ``None``.

    :param attributes: live state attributes.
    """
    value = attributes.get("device_class")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _format_value(value: object) -> str:
    """Render one attribute value as compact text.

    :param value: value from the state attributes.
    """
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)
