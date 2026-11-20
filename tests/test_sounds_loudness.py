"""The bundled demos are normalised to the documented loudness target.

There are two layers. Two plain unit tests keep the documentation honest with no
external binary: the measurement table must match the directory, and the WAV
demo's peak is re-derived with the stdlib ``wave`` module. One integration test
actually measures every shipped file with ``ffmpeg`` and compares the result
against the same table, so a silently re-encoded or mis-levelled asset fails
here instead of sounding wrong on a doorbell.

Owning ticket: HSD-lcyj.
"""

from __future__ import annotations

import array
import json
import math
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Final

import pytest

from chime_deck.sounds import bundled_demo_sounds_dir
from chime_deck.sounds.library import SUPPORTED_FORMATS

TARGET_INTEGRATED_LUFS: Final = -16.0
TRUE_PEAK_CEILING_DBTP: Final = -1.0
LUFS_TOLERANCE_DB: Final = 1.0
SAMPLE_PEAK_TOLERANCE_DB: Final = 1.0
TRUE_PEAK_TOLERANCE_DB: Final = 0.5
WAV_PEAK_TOLERANCE_DB: Final = 0.5
MEASURE_TIMEOUT_S: Final = 30.0

Measurement = tuple[float, float, float]
"""Documented ``(integrated LUFS, true peak dBTP, sample peak dBFS)``."""

BUNDLED_LOUDNESS: Final[dict[str, Measurement]] = {
    "demo_alert.flac": (-16.05, -12.21, -12.21),
    "demo_chime.ogg": (-16.05, -12.05, -12.06),
    "demo_ding.wav": (-15.95, -12.53, -12.71),
    "demo_dong.mp3": (-15.95, -12.22, -12.22),
    # The doorbell measures -0.94 dBTP against a nominal -1.0 dBTP ceiling. A
    # true-peak measurement is itself approximate, so the ceiling assertion
    # below allows TRUE_PEAK_TOLERANCE_DB (0.5 dB) rather than demanding the
    # sample-exact target.
    "demo_doorbell.ogg": (-16.16, -0.94, -1.04),
}
"""Measured once on the development host with ffmpeg n9.0.2, in sorted order."""

_WAV_NAME: Final = "demo_ding.wav"
_WAV_SAMPLE_WIDTH: Final = 2
_WAV_FULL_SCALE: Final = 32768.0

_SKIP_REASON: Final = (
    "ffmpeg/ffprobe not installed; run ./scripts/test-integration.sh (container) "
    "or verify on the ARM board"
)


def _sounds_dir() -> Path:
    """Return the bundled directory, failing the test when it is absent."""
    directory = bundled_demo_sounds_dir()
    assert directory is not None, "the bundled demo-sound directory is missing"
    return directory


def _audio_names(directory: Path) -> set[str]:
    """Return the names of the audio files the scanner would accept.

    :param directory: bundled demo-sound directory.
    """
    return {
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower().lstrip(".") in SUPPORTED_FORMATS
    }


def test_every_bundled_audio_file_has_a_documented_measurement() -> None:
    """The loudness table and the bundled directory describe the same set."""
    audio = _audio_names(_sounds_dir())
    documented = set(BUNDLED_LOUDNESS)

    undocumented = sorted(audio - documented)
    assert not undocumented, (
        f"bundled demo(s) {undocumented} have no documented loudness measurement; "
        "measure them and add an entry to BUNDLED_LOUDNESS"
    )
    stale = sorted(documented - audio)
    assert not stale, (
        f"BUNDLED_LOUDNESS documents {stale}, which are no longer bundled; "
        "remove those entries or restore the missing demo files"
    )


def test_wav_demo_peak_matches_the_documented_target() -> None:
    """Pure-Python peak analysis agrees with the documented WAV sample peak.

    The WAV demo is checked without ``ffmpeg`` so at least one loudness claim is
    enforced on every host, including a bare unit-test run.
    """
    path = _sounds_dir() / _WAV_NAME
    documented_peak = BUNDLED_LOUDNESS[_WAV_NAME][2]

    with wave.open(str(path), "rb") as handle:
        assert handle.getsampwidth() == _WAV_SAMPLE_WIDTH
        samples = array.array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))

    largest = max(abs(sample) for sample in samples)
    measured_peak = 20 * math.log10(largest / _WAV_FULL_SCALE)

    assert abs(measured_peak - documented_peak) <= WAV_PEAK_TOLERANCE_DB, (
        f"{_WAV_NAME}: measured sample peak {measured_peak:.2f} dBFS "
        f"but the documented peak is {documented_peak:.2f} dBFS"
    )


def _run_ffmpeg(path: Path, filtergraph: str) -> str:
    """Run one bounded ``ffmpeg`` analysis pass and return its stderr.

    :param path: audio file to analyse.
    :param filtergraph: ``-af`` filtergraph to apply.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            filtergraph,
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=MEASURE_TIMEOUT_S,
        check=False,
    )
    assert result.returncode == 0, f"ffmpeg failed on {path.name}: {result.stderr}"
    return result.stderr


def _parse_loudnorm(stderr: str) -> tuple[float, float]:
    """Extract integrated LUFS and true peak from ``loudnorm``'s JSON summary.

    :param stderr: stderr of a ``loudnorm ... print_format=json`` run.
    """
    start = stderr.index("{")
    end = stderr.rindex("}") + 1
    payload = json.loads(stderr[start:end])
    return float(payload["input_i"]), float(payload["input_tp"])


def _parse_sample_peak(stderr: str) -> float:
    """Extract the overall ``Peak level dB`` that ``astats`` printed.

    :param stderr: stderr of an ``astats`` run.
    """
    for line in stderr.splitlines():
        _prefix, separator, value = line.partition("Peak level dB:")
        if separator:
            return float(value.strip())
    raise AssertionError(f"astats printed no 'Peak level dB' line: {stderr}")


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason=_SKIP_REASON,
)
@pytest.mark.parametrize("name", sorted(BUNDLED_LOUDNESS))
def test_bundled_demo_matches_the_documented_measurement(name: str) -> None:
    """Measure one shipped demo and compare it with the documented numbers.

    ``astats`` runs in a separate invocation on purpose: chaining it after
    ``loudnorm`` would measure loudnorm's processed output rather than the file
    itself, which would make the sample-peak assertion meaningless.

    :param name: bundled audio file name.
    """
    path = _sounds_dir() / name
    documented_sample_peak = BUNDLED_LOUDNESS[name][2]

    lufs_text = _run_ffmpeg(path, "loudnorm=I=-16:TP=-1.0:LRA=11:print_format=json")
    measured_lufs, measured_true_peak = _parse_loudnorm(lufs_text)

    peak_text = _run_ffmpeg(path, "astats=measure_perchannel=none:measure_overall=Peak_level")
    measured_sample_peak = _parse_sample_peak(peak_text)

    assert abs(measured_lufs - TARGET_INTEGRATED_LUFS) <= LUFS_TOLERANCE_DB, (
        f"{name}: measured {measured_lufs:.2f} LUFS, target is "
        f"{TARGET_INTEGRATED_LUFS:.1f} LUFS ± {LUFS_TOLERANCE_DB:.1f} dB"
    )
    assert measured_true_peak <= TRUE_PEAK_CEILING_DBTP + TRUE_PEAK_TOLERANCE_DB, (
        f"{name}: measured true peak {measured_true_peak:.2f} dBTP, ceiling is "
        f"{TRUE_PEAK_CEILING_DBTP:.1f} dBTP"
    )
    assert abs(measured_sample_peak - documented_sample_peak) <= SAMPLE_PEAK_TOLERANCE_DB, (
        f"{name}: measured sample peak {measured_sample_peak:.2f} dBFS, documented "
        f"{documented_sample_peak:.2f} dBFS ± {SAMPLE_PEAK_TOLERANCE_DB:.1f} dB"
    )
