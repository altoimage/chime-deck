"""``ffprobe`` reads back what ``ffmpeg`` encoded, in every shipped format.

Coverage (d) of design 07 §2: duration and format extraction against known
fixtures. These run on the dev host too when ``ffmpeg`` is present, which is
intended -- they are cheap and entirely offline.

Owning ticket: HSD-0mmx.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from audio_fixtures import FORMAT_CODECS, generate_audio_fixtures, probe

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason=(
            "ffmpeg/ffprobe not installed; run ./scripts/test-integration.sh (container) "
            "or ./scripts/test-integration.sh --board (real aarch64)"
        ),
    ),
]

_DURATION_MIN_S = 0.40
_DURATION_MAX_S = 0.80

_FORMAT_NAMES = {
    "wav": "wav",
    "mp3": "mp3",
    "ogg": "ogg",
    "flac": "flac",
}
"""The ``format_name`` ffprobe must report for each extension (ogg says ``ogg``).

Matched exactly, not as a substring: a substring match would still pass if the
container changed under an extension.
"""


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Encode the four tones once for the whole module.

    :param tmp_path_factory: pytest temporary directory factory.
    :return: fixture extension to the encoded path.
    """
    return generate_audio_fixtures(tmp_path_factory.mktemp("ffprobe-fixtures"))


@pytest.mark.parametrize("extension", sorted(FORMAT_CODECS))
def test_ffprobe_reads_the_encoded_stream(extension: str, fixtures: dict[str, Path]) -> None:
    """``ffprobe`` reports a plausible duration and the forced stream layout.

    :param extension: fixture format under test.
    :param fixtures: the module-wide encoded fixtures.
    """
    result = probe(fixtures[extension])

    assert _DURATION_MIN_S <= result.duration_s <= _DURATION_MAX_S
    assert result.sample_rate == 44100
    assert result.channels == 1
    assert result.format_name == _FORMAT_NAMES[extension]
