"""The state→tone vocabulary, its default table and its precedence rules."""

from __future__ import annotations

import pytest

from chime_deck.icons import (
    DEFAULT_STATE_TONES,
    TONE_ALERT,
    TONE_OFF,
    TONE_OK,
    TONE_WARN,
    TONES,
    is_tone,
    state_tone,
)

_DEFAULT_ENTRIES = sorted(DEFAULT_STATE_TONES.items())


@pytest.mark.parametrize(
    ("state", "expected"),
    _DEFAULT_ENTRIES,
    ids=[state for state, _tone in _DEFAULT_ENTRIES],
)
def test_every_default_state_resolves_to_its_tone(state: str, expected: str) -> None:
    """Each shipped default maps to the tone the table records for it.

    :param state: raw state from the default table.
    :param expected: tone the default table gives it.
    """
    assert state_tone(state) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param("running", TONE_WARN, id="running-warns"),
        pytest.param("finished", TONE_OK, id="finished-is-ok"),
        pytest.param("idle", TONE_OFF, id="idle-is-grey"),
        pytest.param("off", TONE_OFF, id="off-is-grey"),
    ],
)
def test_appliance_states_map_as_the_owner_expects(state: str, expected: str) -> None:
    """The ticket's acceptance states resolve to their agreed tones.

    :param state: raw appliance state under test.
    :param expected: expected tone.
    """
    assert state_tone(state) == expected


def test_explicit_override_beats_the_default() -> None:
    """A per-tile entry wins over the built-in tone for the same state."""
    assert state_tone("running", overrides={"running": TONE_ALERT}) == TONE_ALERT


def test_override_for_one_state_leaves_another_alone() -> None:
    """Overriding one state does not disturb the tone of any other state."""
    overrides = {"running": TONE_OK}
    assert state_tone("running", overrides=overrides) == TONE_OK
    assert state_tone("finished", overrides=overrides) == TONE_OK
    assert state_tone("idle", overrides=overrides) == TONE_OFF


def test_override_may_add_a_state_the_defaults_do_not_know() -> None:
    """A user-added state maps exactly as the tile configured it."""
    assert state_tone("soaking", overrides={"soaking": TONE_WARN}) == TONE_WARN
    assert state_tone("soaking") == TONE_OFF


def test_empty_and_absent_overrides_behave_identically() -> None:
    """``None`` overrides and an empty mapping give the same tone."""
    assert state_tone("running", overrides=None) == state_tone("running", overrides={})


@pytest.mark.parametrize(
    "state",
    [
        pytest.param("running", id="mapped-state"),
        pytest.param("weird", id="unmapped-state"),
        pytest.param("finished", id="explicitly-overridden-state"),
    ],
)
def test_colour_by_state_false_is_neutral_in_every_state(state: str) -> None:
    """The opt-out renders neutral even where a tone would otherwise apply.

    :param state: raw state under test.
    """
    overrides = {"finished": TONE_ALERT}
    assert state_tone(state, overrides=overrides, colour_by_state=False) == TONE_OFF


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
def test_never_mapped_states_stay_off_even_with_an_override(state: str) -> None:
    """An absence of state cannot be coloured by a stored override.

    :param state: raw special state under test.
    """
    assert state_tone(state, overrides={state: TONE_OK}) == TONE_OFF


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
    ],
)
def test_no_state_has_no_tone(state: str | None) -> None:
    """A missing, empty or blank state is not coloured at all.

    :param state: raw state under test.
    """
    assert state_tone(state) is None


def test_unmapped_state_is_grey() -> None:
    """A state the defaults do not know falls back to grey, keeping its word."""
    assert state_tone("sulking") == TONE_OFF


def test_unmapped_state_can_be_coloured_by_an_explicit_mapping() -> None:
    """Grey is a fallback, not a lock: an explicit entry still wins."""
    assert state_tone("sulking", overrides={"sulking": TONE_WARN}) == TONE_WARN


@pytest.mark.parametrize("tone", TONES)
def test_is_tone_accepts_the_vocabulary(tone: str) -> None:
    """Every shipped tone is accepted by the validator.

    :param tone: one of the four tones.
    """
    assert is_tone(tone) is True


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("green", id="colour-name"),
        pytest.param("", id="empty-string"),
        pytest.param(None, id="none"),
        pytest.param(1, id="integer"),
    ],
)
def test_is_tone_rejects_anything_else(value: object) -> None:
    """Only the four tones pass; a colour name, blank value or number does not.

    :param value: candidate value under test.
    """
    assert is_tone(value) is False
