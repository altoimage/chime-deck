"""Presentation hints: state wording, tone, units and attribute rendering."""

from __future__ import annotations

import pytest

from chime_deck.icons import (
    AttributeHint,
    humanise_attribute_name,
    presentation_hints,
    presentation_payload,
)


@pytest.mark.parametrize(
    ("domain", "device_class", "state", "expected"),
    [
        ("lock", None, "locked", "Locked"),
        ("lock", None, "unlocked", "Unlocked"),
        ("lock", None, "jammed", "Jammed"),
        ("cover", None, "open", "Open"),
        ("cover", None, "closed", "Closed"),
        ("cover", None, "opening", "Opening"),
        ("cover", None, "closing", "Closing"),
        ("person", None, "home", "Home"),
        ("person", None, "not_home", "Away"),
        ("device_tracker", None, "not_home", "Away"),
        ("binary_sensor", "door", "on", "Open"),
        ("binary_sensor", "door", "off", "Closed"),
        ("binary_sensor", "motion", "on", "Detected"),
        ("binary_sensor", "moisture", "off", "Dry"),
        ("binary_sensor", "connectivity", "on", "Connected"),
        ("binary_sensor", "smoke", "on", "On"),
        ("binary_sensor", "smoke", "off", "Off"),
        ("switch", None, "on", "On"),
        ("switch", None, "off", "Off"),
        ("sensor", None, None, None),
        ("sensor", None, "weird", "Weird"),
        ("sensor", None, "pre-wash", "Pre-wash"),
        ("sensor", None, "running", "Running"),
        ("sensor", None, "not_home", "Not_home"),
        ("sensor", None, "Open", "Open"),
    ],
)
def test_state_wording(
    domain: str, device_class: str | None, state: str | None, expected: str | None
) -> None:
    """Every word is title-cased; unknown tokens are capitalised, not hidden.

    Only the first letter is normalised, so a hyphen, an underscore and an
    already-capitalised word survive untouched.

    :param domain: entity domain.
    :param device_class: device class from the attributes, or ``None``.
    :param state: raw state under test.
    :param expected: expected displayed state.
    """
    attributes: dict[str, object] = {}
    if device_class is not None:
        attributes["device_class"] = device_class

    hints = presentation_hints(domain, state=state, attributes=attributes)

    assert hints.state == state
    assert hints.displayed_state == expected


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        ({"unit_of_measurement": "°C"}, "°C"),
        ({"unit_of_measurement": "  °C  "}, "°C"),
        ({"unit_of_measurement": ""}, None),
        ({"unit_of_measurement": 5}, None),
        ({}, None),
    ],
)
def test_unit_of_measurement(attributes: dict[str, object], expected: str | None) -> None:
    """A usable unit is stripped, and anything else degrades to ``None``.

    :param attributes: state attributes under test.
    :param expected: expected unit of measurement.
    """
    hints = presentation_hints("sensor", state="21", attributes=attributes)

    assert hints.unit_of_measurement == expected


def test_attribute_hint_renders_name_label_and_value() -> None:
    """A requested attribute becomes a labelled, rendered hint."""
    hints = presentation_hints(
        "sensor",
        state="on",
        attributes={"battery_level": 87},
        show_attributes=("battery_level",),
    )

    assert hints.attribute_hints == (AttributeHint("battery_level", "Battery level", "87"),)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (21.5, "21.5"),
        (21.0, "21"),
        (True, "yes"),
        (False, "no"),
        (["a", "b"], '["a", "b"]'),
        ({"b": 2, "a": 1}, '{"a": 1, "b": 2}'),
    ],
)
def test_attribute_value_formatting(value: object, expected: str) -> None:
    """Bools, numbers, strings and containers render deterministically.

    :param value: attribute value under test.
    :param expected: expected rendered text.
    """
    hints = presentation_hints(
        "sensor",
        state="on",
        attributes={"value": value},
        show_attributes=("value",),
    )

    assert hints.attribute_hints[0].value == expected


def test_none_and_missing_attributes_are_omitted() -> None:
    """``None`` values and absent names are skipped rather than defaulted."""
    hints = presentation_hints(
        "sensor",
        state="on",
        attributes={"present": None},
        show_attributes=("present", "missing"),
    )

    assert hints.attribute_hints == ()


def test_empty_show_attributes_gives_no_hints() -> None:
    """No requested attributes means no attribute hints."""
    hints = presentation_hints("sensor", state="on", attributes={"battery_level": 87})

    assert hints.attribute_hints == ()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("unit_of_measurement", "Unit of measurement"),
        ("", ""),
        ("name", "Name"),
        ("a__b", "A b"),
    ],
)
def test_humanise_attribute_name(name: str, expected: str) -> None:
    """Attribute names become sentence-case labels.

    :param name: raw attribute name.
    :param expected: expected label.
    """
    assert humanise_attribute_name(name) == expected


def test_no_live_data_degrades_to_empty_hints() -> None:
    """With no state and no attributes every field degrades instead of guessing."""
    hints = presentation_hints("sensor", state=None, attributes={}, show_attributes=("x",))

    assert hints.state is None
    assert hints.displayed_state is None
    assert hints.unit_of_measurement is None
    assert hints.attribute_hints == ()
    assert hints.tone is None


@pytest.mark.parametrize(
    ("state_tones", "colour_by_state", "state", "expected"),
    [
        (None, True, "running", "warn"),
        ({"running": "alert"}, True, "running", "alert"),
        (None, False, "running", "off"),
        (None, True, "not_a_known_state", "off"),
        ({"unavailable": "alert"}, True, "unavailable", "off"),
        ({"unknown": "ok"}, True, "unknown", "off"),
    ],
)
def test_presentation_hints_carry_the_tone(
    state_tones: dict[str, str] | None,
    colour_by_state: bool,
    state: str,
    expected: str,
) -> None:
    """The tone rides with the word, with explicit → default → grey precedence.

    ``unavailable`` and ``unknown`` are guarded before any lookup, so a hostile
    override cannot colour an absence of state.

    :param state_tones: explicit state→tone overrides, or ``None``.
    :param colour_by_state: whether the tile colours by state.
    :param state: raw state under test.
    :param expected: expected tone.
    """
    hints = presentation_hints(
        "sensor",
        state=state,
        attributes={},
        state_tones=state_tones,
        colour_by_state=colour_by_state,
    )

    assert hints.tone == expected


def test_presentation_payload_is_the_wire_shape_for_a_unit_bearing_state() -> None:
    """A numeric state exports its word, its unit and its rendered hint, in order."""
    hints = presentation_hints(
        "sensor",
        state="21.4",
        attributes={"unit_of_measurement": "°C", "battery_level": 91},
        show_attributes=("battery_level",),
    )

    assert presentation_payload(hints) == {
        "displayed_state": "21.4",
        "unit_of_measurement": "°C",
        "tone": "off",
        "attribute_hints": [
            {"name": "battery_level", "label": "Battery level", "value": "91"},
        ],
    }


def test_presentation_payload_mirrors_an_unavailable_state() -> None:
    """``unavailable`` travels verbatim; suppressing a unit is the client's rule."""
    hints = presentation_hints(
        "sensor",
        state="unavailable",
        attributes={"unit_of_measurement": "°C"},
        show_attributes=("battery_level",),
    )

    assert presentation_payload(hints) == {
        "displayed_state": "Unavailable",
        "unit_of_measurement": "°C",
        "tone": "off",
        "attribute_hints": [],
    }


def test_presentation_payload_has_no_hints_when_the_attributes_carry_none() -> None:
    """Requested names the state does not carry give an empty list, not a default."""
    hints = presentation_hints(
        "lock",
        state="locked",
        attributes={"battery_level": None},
        show_attributes=("battery_level", "missing"),
    )

    assert presentation_payload(hints)["attribute_hints"] == []
