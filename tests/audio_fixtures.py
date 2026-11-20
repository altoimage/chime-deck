"""Audio fixtures generated at test time, shared by the integration tier.

The fixtures are tiny sine tones encoded by ``ffmpeg`` in a temporary directory
and thrown away afterwards. Nothing here is committed: design 07 §4 rule 5 says
committed audio must be CC0 with its licence recorded, and a generated half
second of 440 Hz needs neither.

Every fixture is forced to 44.1 kHz mono so the stream assertions hold across
ffmpeg versions, and every subprocess is bounded, so a broken ``ffmpeg`` cannot
hang the suite.

Owning ticket: HSD-0mmx.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "FIXTURE_SECONDS",
    "FORMAT_CODECS",
    "ProbeResult",
    "generate_audio_fixtures",
    "probe",
]

FORMAT_CODECS: Final[Mapping[str, str]] = {
    "wav": "pcm_s16le",
    "mp3": "libmp3lame",
    "ogg": "libvorbis",
    "flac": "flac",
}
"""Fixture extension to the ffmpeg encoder used for it."""

FIXTURE_SECONDS: Final = 0.5
"""Length of every generated tone; the probes allow 0.40-0.80 s of slop."""

_SAMPLE_RATE: Final = 44100
_CHANNELS: Final = 1


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The audio-stream facts ``ffprobe`` reports for one file."""

    format_name: str
    duration_s: float
    sample_rate: int
    channels: int


def generate_audio_fixtures(
    directory: Path,
    *,
    seconds: float = FIXTURE_SECONDS,
    timeout_s: float = 30.0,
) -> dict[str, Path]:
    """Encode one tone per format in :data:`FORMAT_CODECS`.

    :param directory: existing directory to write into; pytest's ``tmp_path`` or
        a ``tmp_path_factory`` directory.
    :param seconds: tone length in seconds.
    :param timeout_s: bound for each ``ffmpeg`` invocation.
    :return: fixture extension to the path written.
    :raises AssertionError: if ``ffmpeg`` failed to encode one format.
    """
    fixtures: dict[str, Path] = {}
    for extension, codec in FORMAT_CODECS.items():
        destination = directory / f"tone.{extension}"
        completed = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={seconds}",
                "-ar",
                str(_SAMPLE_RATE),
                "-ac",
                str(_CHANNELS),
                "-c:a",
                codec,
                str(destination),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            raise AssertionError(f"ffmpeg could not encode {extension}: {completed.stderr.strip()}")
        fixtures[extension] = destination
    return fixtures


def probe(path: Path, *, timeout_s: float = 20.0) -> ProbeResult:
    """Read the first audio stream and the container format of *path*.

    The output is ``key=value`` lines with no wrapper, which is parsed directly:
    JSON would need casts to satisfy mypy ``strict`` for no benefit here.

    :param path: audio file to inspect.
    :param timeout_s: bound for the ``ffprobe`` invocation.
    :return: the parsed stream and format facts.
    :raises AssertionError: if ``ffprobe`` failed or did not report a field.
    """
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels:format=format_name,duration",
            "-of",
            "default=noprint_wrappers=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise AssertionError(f"ffprobe could not read {path.name}: {completed.stderr.strip()}")

    fields: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()

    return ProbeResult(
        format_name=_field(fields, "format_name", path),
        duration_s=float(_field(fields, "duration", path)),
        sample_rate=int(_field(fields, "sample_rate", path)),
        channels=int(_field(fields, "channels", path)),
    )


def _field(fields: Mapping[str, str], key: str, path: Path) -> str:
    """Return one parsed ``ffprobe`` field, or fail naming the file.

    :param fields: parsed ``key=value`` output.
    :param key: field to look up.
    :param path: file that was probed, for the message.
    :return: the raw field value.
    :raises AssertionError: if the field is missing or empty.
    """
    value = fields.get(key, "")
    if not value:
        raise AssertionError(f"ffprobe reported no {key} for {path.name}: {dict(fields)}")
    return value
