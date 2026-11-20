"""Real espeak-ng renders a real WAV, but only where the binary exists.

This is the integration tier from design 07: the host suite skips it cleanly,
the container harness or the ARM board runs it. It is fully offline and touches
no audio device.

Owning ticket: HSD-bw8h.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import wave
from contextlib import suppress
from pathlib import Path

import pytest

from chime_deck.tts import EspeakNgEngine, TtsOptions

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        shutil.which("espeak-ng") is None,
        reason=(
            "espeak-ng not installed; run ./scripts/test-integration.sh or verify on the "
            "ARM board (design 07)"
        ),
    ),
]

_FIXED_TEXT = "Testing one two three."
_MIN_DURATION_S = 0.2
_MAX_DURATION_S = 15.0


async def test_espeak_ng_renders_a_playable_wav(tmp_path: Path) -> None:
    """The real engine is available and renders a non-trivial WAV.

    :param tmp_path: pytest temporary directory.
    """
    engine = EspeakNgEngine()

    availability = await engine.available()
    assert availability.ok is True
    destination = tmp_path / "out.wav"

    result = await engine.render(
        _FIXED_TEXT, TtsOptions(voice="en-gb", speed=160), destination=destination
    )

    assert result == destination
    payload = destination.read_bytes()
    assert payload[:4] == b"RIFF"
    assert payload[8:12] == b"WAVE"
    with wave.open(str(destination), "rb") as reader:
        frames = reader.getnframes()
        rate = reader.getframerate()
    assert frames > 0
    assert rate > 0
    assert _MIN_DURATION_S < frames / rate < _MAX_DURATION_S


def _wav_seconds(path: Path) -> float:
    """Return the playing time of a WAV file in seconds.

    :param path: WAV file to measure.
    :return: frames divided by sample rate.
    """
    with wave.open(str(path), "rb") as reader:
        frames = reader.getnframes()
        rate = reader.getframerate()
    assert frames > 0
    assert rate > 0
    return frames / rate


def _assert_valid_wav(path: Path) -> None:
    """Assert the file carries a RIFF/WAVE header and audible duration.

    :param path: WAV file to check.
    """
    payload = path.read_bytes()
    assert payload[:4] == b"RIFF"
    assert payload[8:12] == b"WAVE"
    duration = _wav_seconds(path)
    assert _MIN_DURATION_S < duration < _MAX_DURATION_S


async def test_espeak_ng_treats_hostile_text_as_data(tmp_path: Path) -> None:
    """Shell metacharacters in a phrase are spoken, never interpreted.

    Coverage (b) of design 07 §2. The phrase travels to espeak-ng on stdin only:
    it is handed over verbatim as stdin, ``--stdin`` is on argv, and no fragment of
    the phrase appears in any argv element — so nothing in it could be interpreted
    as shell syntax. The canary the text tries to create never appears.

    The canary sits on a short fixed path under the temp directory rather than in
    ``tmp_path`` because espeak-ng *speaks the path aloud*, and a long ``tmp_path``
    repeated through the phrase ran to 34 s in the container — past the
    plausible-duration bound. The name carries a pid suffix so concurrent runs do
    not collide, and the pre-clean is guarded because a file left by another user
    must not turn into an error. It is unlinked first so a stale file from an
    earlier run cannot make the "no shell interpretation" assertion fail
    spuriously.

    :param tmp_path: pytest temporary directory.
    """
    canary = Path(tempfile.gettempdir()) / f"sd-canary-{os.getpid()}"
    with suppress(PermissionError):
        canary.unlink(missing_ok=True)
    text = f'"; $(touch {canary}); `touch {canary}` && echo pwned\nsecond line'
    engine = EspeakNgEngine()
    options = TtsOptions(voice="en-gb", speed=160)
    destination = tmp_path / "out.wav"

    invocation = engine.build_invocation(text, options, destination)
    result = await engine.render(text, options, destination=destination)

    assert result == destination
    _assert_valid_wav(destination)
    assert not canary.exists()
    assert invocation.stdin == text.encode("utf-8")
    assert "--stdin" in invocation.argv
    for fragment in ("$(", "`", ";", "&&", "touch"):
        assert all(fragment not in argument for argument in invocation.argv)


async def test_espeak_ng_speaks_every_stdin_line(tmp_path: Path) -> None:
    """All lines of a multi-line phrase are spoken, not just the first.

    Without ``--stdin`` espeak-ng reads one line and drops the rest, so this ratio
    is a real regression guard rather than a smoke test.

    :param tmp_path: pytest temporary directory.
    """
    engine = EspeakNgEngine()
    options = TtsOptions(voice="en-gb", speed=160)

    one_line = await engine.render(
        "The front door is open.", options, destination=tmp_path / "one.wav"
    )
    three_lines = await engine.render(
        "The front door is open.\nThe garage is open.\nThe kitchen is cold.",
        options,
        destination=tmp_path / "three.wav",
    )

    _assert_valid_wav(one_line)
    _assert_valid_wav(three_lines)
    ratio = _wav_seconds(three_lines) / _wav_seconds(one_line)
    assert 1.3 <= ratio <= 6.0, f"three lines took {ratio:.2f}x one line"
