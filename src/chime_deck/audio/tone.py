"""A generated, audible verification tone — no binary asset, no network, no dependency.

The tone exists so a "Play test tone" can prove the speaker actually makes a
sound: it is generated here with the standard library (:mod:`wave`,
:mod:`array`, :mod:`math`), so nothing is committed to the repository and
nothing is downloaded. Two calls produce byte-identical files, which is what
lets a test assert on the exact bytes.

The level arithmetic is recorded verbatim, because it was measured once and
must not be guessed again:

* peak :data:`TONE_AMPLITUDE` (0.30) full scale is **-10.5 dBFS** at unity gain
  (``20 * log10(0.30)`` = -10.46);
* played at the shipped ``audio.volume`` default of 70% that is about
  **-13.6 dBFS** (``20 * log10(0.30 * 0.70)`` = -13.56);
* at mpv volume 40 it is about **-18.4 dBFS** (``20 * log10(0.30 * 0.40)`` =
  -18.42), which is the level the owner verified as audible on the hardware.

0.30 can never clip: the peak stays below full scale (peak < 1.0) at any volume
up to and including 100, so the probe cannot distort the speaker it is testing.

Owning ticket: HSD-wpm0.
"""

from __future__ import annotations

import io
import math
import wave
from array import array
from pathlib import Path
from typing import Final

__all__ = [
    "TONE_AMPLITUDE",
    "TONE_DURATION_S",
    "TONE_FADE_S",
    "TONE_FILENAME",
    "TONE_FREQUENCY_HZ",
    "TONE_SAMPLE_RATE",
    "tone_wav_bytes",
    "write_test_tone",
]

TONE_FREQUENCY_HZ: Final = 440.0
"""Pitch of the tone: concert A, the reference pitch every speaker reproduces."""

TONE_DURATION_S: Final = 0.4
"""Length of the tone in seconds — long enough to be heard, short enough to bound a probe."""

TONE_AMPLITUDE: Final = 0.30
"""Peak amplitude as a fraction of full scale; see the module docstring for the dBFS table."""

TONE_SAMPLE_RATE: Final = 48_000
"""Sample rate in Hz: the rate every modern sound server accepts without resampling."""

TONE_FADE_S: Final = 0.015
"""Linear fade in and out in seconds, so the tone cannot click at either end."""

TONE_FILENAME: Final = "test-tone.wav"
"""Name :func:`write_test_tone` gives the generated file."""

_TONE_CHANNELS: Final = 1
"""The tone is mono: one speaker, one sample per frame."""

_TONE_SAMPLE_WIDTH_BYTES: Final = 2
"""16-bit PCM: two bytes per sample, signed little-endian."""

_FULL_SCALE: Final = 32767
"""Largest positive value of a signed 16-bit sample."""


def tone_wav_bytes(
    *,
    frequency_hz: float = TONE_FREQUENCY_HZ,
    duration_s: float = TONE_DURATION_S,
    amplitude: float = TONE_AMPLITUDE,
    sample_rate: int = TONE_SAMPLE_RATE,
    fade_s: float = TONE_FADE_S,
) -> bytes:
    """Return one complete 16-bit PCM mono WAV holding the verification tone.

    The signal is a sine at *frequency_hz* with ``round(duration_s *
    sample_rate)`` frames, multiplied by a linear fade in and out of *fade_s* so
    it starts and ends at silence. Exactly one frame reaches the peak
    ``round(amplitude * 32767)``, so the level the module docstring records is
    the level the file really holds.

    ``wave`` writes the sample bytes as they are and :class:`array` is
    little-endian on every platform this project ships (x86_64, aarch64), so the
    payload is a normal ``pcm_s16le`` WAV; the test reads it back with
    :mod:`wave` to keep that true.

    :param frequency_hz: pitch of the sine, in Hz.
    :param duration_s: length of the tone, in seconds.
    :param amplitude: peak amplitude as a fraction of full scale (0.0-1.0).
    :param sample_rate: sample rate, in Hz.
    :param fade_s: linear fade in and out, in seconds.
    :return: the bytes of a complete RIFF/WAVE file, deterministic per call.
    """
    frame_count = round(duration_s * sample_rate)
    peak = round(amplitude * _FULL_SCALE)
    # Never more than half the file, so the two fades cannot overlap and the
    # envelope can never go negative.
    fade_frames = min(round(fade_s * sample_rate), frame_count // 2)
    step = 2.0 * math.pi * frequency_hz / sample_rate
    samples: array[int] = array("h")
    for index in range(frame_count):
        envelope = 1.0
        if fade_frames > 0:
            if index < fade_frames:
                envelope = index / fade_frames
            elif index >= frame_count - fade_frames:
                envelope = (frame_count - 1 - index) / fade_frames
        samples.append(round(peak * math.sin(step * index) * envelope))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(_TONE_CHANNELS)
        wav.setsampwidth(_TONE_SAMPLE_WIDTH_BYTES)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())
    return buffer.getvalue()


def write_test_tone(directory: Path) -> Path:
    """Write the verification tone into *directory* and return its path.

    An existing :data:`TONE_FILENAME` in *directory* is **overwritten**: the file
    is generated, never user data, so a stale tone from an earlier run must not
    be played in place of the current one.

    :param directory: directory the tone is written into; created, with its
        parents, when it does not exist.
    :return: path of the written :data:`TONE_FILENAME`.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / TONE_FILENAME
    path.write_bytes(tone_wav_bytes())
    return path
