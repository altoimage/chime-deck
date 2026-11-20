"""The generated verification tone: a real 16-bit PCM WAV, checked entirely offline.

Nothing here needs a sound card or a system binary. The tone is read back with
:mod:`wave`, so "the speaker makes a sound" starts from a file that really is a
1-channel, 48 kHz, 16-bit PCM WAV with the documented peak and length — not from
the assumption that it is.

Owning ticket: HSD-wpm0.
"""

from __future__ import annotations

import io
import math
import wave
from array import array
from itertools import pairwise
from pathlib import Path

import pytest

from chime_deck.audio import (
    TONE_AMPLITUDE,
    TONE_DURATION_S,
    TONE_FADE_S,
    TONE_FILENAME,
    TONE_FREQUENCY_HZ,
    TONE_SAMPLE_RATE,
    tone_wav_bytes,
    write_test_tone,
)

_EXPECTED_FRAMES = round(TONE_DURATION_S * TONE_SAMPLE_RATE)
_EXPECTED_PEAK = round(TONE_AMPLITUDE * 32767)
_FADE_FRAMES = round(TONE_FADE_S * TONE_SAMPLE_RATE)


def _params(data: bytes) -> dict[str, int]:
    """Return the WAV header fields of *data* as a plain mapping.

    :param data: bytes of one complete WAV file.
    """
    with wave.open(io.BytesIO(data), "rb") as wav:
        return {
            "channels": wav.getnchannels(),
            "sample_width": wav.getsampwidth(),
            "framerate": wav.getframerate(),
            "nframes": wav.getnframes(),
        }


def _samples(data: bytes) -> list[int]:
    """Return every sample of *data* as a signed 16-bit integer.

    :param data: bytes of one complete WAV file.
    """
    with wave.open(io.BytesIO(data), "rb") as wav:
        frames = wav.readframes(wav.getnframes())
    values: array[int] = array("h")
    values.frombytes(frames)
    return list(values)


def test_the_tone_is_a_mono_48khz_pcm_wav() -> None:
    """The header is the documented mono 48 kHz 16-bit PCM shape and length."""
    params = _params(tone_wav_bytes())

    assert params["channels"] == 1
    assert params["sample_width"] == 2
    assert params["framerate"] == TONE_SAMPLE_RATE == 48_000
    assert params["nframes"] == _EXPECTED_FRAMES == 19_200
    assert params["nframes"] / params["framerate"] == pytest.approx(TONE_DURATION_S)


def test_the_peak_sample_is_the_documented_amplitude() -> None:
    """One sample reaches ``round(0.30 * 32767)`` full scale, so the level is real."""
    values = _samples(tone_wav_bytes())

    peak = max(abs(value) for value in values)

    assert peak == pytest.approx(_EXPECTED_PEAK, abs=1)
    assert _EXPECTED_PEAK == 9830


def test_the_tone_fades_in_and_out_and_hits_its_peak_in_between() -> None:
    """Both ends start and finish at silence, while the middle carries the sine."""
    values = _samples(tone_wav_bytes())

    assert values[0] == 0
    assert abs(values[-1]) <= 1

    peak_index = max(range(len(values)), key=lambda index: abs(values[index]))
    assert _FADE_FRAMES < peak_index < _EXPECTED_FRAMES - _FADE_FRAMES
    assert abs(values[peak_index]) == pytest.approx(_EXPECTED_PEAK, abs=1)


def test_the_rms_is_a_sine_at_the_documented_amplitude() -> None:
    """The power matches ``0.30 / sqrt(2)`` within 5%, so this is a real sine."""
    values = _samples(tone_wav_bytes())

    rms = math.sqrt(sum(value * value for value in values) / len(values)) / 32767

    assert rms == pytest.approx(TONE_AMPLITUDE / math.sqrt(2), rel=0.05)


def test_two_calls_are_byte_identical() -> None:
    """The generator is deterministic, so a byte comparison is a valid assertion."""
    assert tone_wav_bytes() == tone_wav_bytes()


def test_the_frequency_is_a_440_hz_sine() -> None:
    """The zero crossings of the unfaded middle come at the documented 440 Hz."""
    values = _samples(tone_wav_bytes())
    middle = values[_FADE_FRAMES : _EXPECTED_FRAMES - _FADE_FRAMES]

    crossings = sum(1 for first, second in pairwise(middle) if (first < 0) != (second < 0))

    expected = 2 * TONE_FREQUENCY_HZ * (len(middle) / TONE_SAMPLE_RATE)
    assert crossings == pytest.approx(expected, rel=0.02)


def test_write_test_tone_writes_the_named_file(tmp_path: Path) -> None:
    """The tone is generated, not committed, and lands where the probe expects it.

    :param tmp_path: pytest temporary directory standing in for the run directory.
    """
    directory = tmp_path / "nested" / "run"

    path = write_test_tone(directory)

    assert path == directory / TONE_FILENAME
    assert path.is_file()
    assert path.read_bytes() == tone_wav_bytes()


def test_write_test_tone_overwrites_an_existing_file(tmp_path: Path) -> None:
    """A stale tone is replaced, never played in place of the current one.

    :param tmp_path: pytest temporary directory.
    """
    path = tmp_path / TONE_FILENAME
    path.write_bytes(b"stale")

    assert write_test_tone(tmp_path) == path
    assert path.read_bytes() == tone_wav_bytes()


def test_the_documented_dbfs_levels_hold() -> None:
    """The module docstring's level table is arithmetically true, not folklore."""
    peak_dbfs = 20 * math.log10(TONE_AMPLITUDE)
    at_default_volume = 20 * math.log10(TONE_AMPLITUDE * 0.70)
    at_mpv_volume_40 = 20 * math.log10(TONE_AMPLITUDE * 0.40)

    assert peak_dbfs == pytest.approx(-10.46, abs=0.05)
    assert at_default_volume == pytest.approx(-13.6, abs=0.1)
    assert at_mpv_volume_40 == pytest.approx(-18.4, abs=0.1)
    assert TONE_AMPLITUDE < 1.0
