"""Real piper renders a real WAV, but only where it is installed and has the RAM.

Design 07 separates three tiers, and this test is the *measurement* tier: it uses
the real optional extra, the real wheel's own espeak-ng data and a real voice
model. It never talks to the network, because the model is a fixture baked into
the integration image (or rsynced to the ARM board) ahead of the run.

**A deliberate deviation from design 07 §4 rule 3.** That rule says an
integration test must skip when the binary is missing, not fail. Here, when the
tier has *promised* coverage (``CHIMEDECK_TEST_REAL_PIPER=1``, set by the
integration image), a missing piper or fixture model is a :func:`pytest.fail`:
a green-by-skip run would let a ticket claim a measurement that never happened.
Without that variable the test skips with the command that would run it.

**The D22 memory gate.** The board is a measurement target, not a supported
one, so the gate is bypassed there on purpose only with
``CHIMEDECK_TEST_PIPER_FORCE=1``. Below :data:`DEFAULT_PIPER_MIN_RAM_MB`
without it, the test skips and quotes both the threshold and ``MemTotal`` /
``MemAvailable``, because that is the honest answer to "can this machine speak
with local Piper" — the answer the product gives too.

Owning ticket: HSD-p50k.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import resource
import wave
from pathlib import Path
from typing import Final, NoReturn

import pytest

from chime_deck.tts import (
    DEFAULT_PIPER_MIN_RAM_MB,
    PiperEngine,
    PiperProbe,
    TtsOptions,
    read_meminfo,
)
from chime_deck.tts.piper import MEMINFO_PATH, PIPER_MIN_RAM_SETTING, PIPER_VOICE_SETTING

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    # onnxruntime and protobuf are third-party, and `filterwarnings = ["error"]`
    # must not decide whether a real synthesis is possible. The mark is scoped
    # to this module because the warning can surface from the import on another
    # thread.
    pytest.mark.filterwarnings("ignore::DeprecationWarning:onnxruntime.*"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning:google.protobuf.*"),
]

_LOGGER = logging.getLogger(__name__)

_CHIMEDECK_ENV = os.environ.get("CHIMEDECK_TEST_REAL_PIPER") == "1"
"""The integration image promises this tier, so a missing piece is a failure."""

_FORCE = os.environ.get("CHIMEDECK_TEST_PIPER_FORCE") == "1"
"""Measure below the D22 threshold anyway; the board is not a supported target."""

VOICE: Final = "nl_BE-nathalie-x_low"
"""The baked fixture voice: 19.7 MiB, CC0, published at the v1.0.0 tag."""

_FIXED_TEXT: Final = "Testing one two three."
_MIN_DURATION_S: Final = 0.2
_MAX_DURATION_S: Final = 30.0
_RENDER_TIMEOUT_S: Final = 30.0
"""A cold model load plus one short phrase, generously bounded for the board."""

_FIXTURE_DIR: Final = Path(
    os.environ.get(
        "CHIMEDECK_TEST_PIPER_VOICES",
        str(Path(__file__).parent / "fixtures" / "piper-voices"),
    )
)
"""Where the baked model lives; the image overrides this to ``/app/tests/fixtures``."""


class _StubSettings:
    """The two settings :class:`PiperProbe` reads, answered without a store."""

    async def get_setting(self, key: str) -> object:
        """Return the value the probe asks for, or ``None`` for anything else.

        :param key: registered setting key.
        """
        if key == PIPER_MIN_RAM_SETTING:
            return 256
        if key == PIPER_VOICE_SETTING:
            return VOICE
        return None


def _fail_or_skip(reason: str) -> NoReturn:
    """Fail when the tier promised coverage, otherwise skip with the reason.

    :param reason: what is missing and how to provide it.
    """
    if _CHIMEDECK_ENV:
        pytest.fail(f"the integration tier promised real Piper coverage: {reason}")
    pytest.skip(reason)


async def test_real_piper_renders_a_playable_wav(tmp_path: Path) -> None:
    """Synthesise one fixed phrase and check the audio and the measured time.

    :param tmp_path: pytest temporary directory.
    """
    model = _FIXTURE_DIR / f"{VOICE}.onnx"
    config = _FIXTURE_DIR / f"{VOICE}.onnx.json"
    missing: list[str] = []
    if importlib.util.find_spec("piper") is None:
        missing.append("the piper extra is not installed")
    if not model.is_file() or not config.is_file():
        missing.append(f"the fixture voice {VOICE} is missing from {_FIXTURE_DIR}")
    if missing:
        _fail_or_skip(
            f"{'; '.join(missing)}; run ./scripts/test-integration.sh --container or --board"
        )

    total_mb, available_mb = read_meminfo(MEMINFO_PATH)
    if total_mb is not None and total_mb < DEFAULT_PIPER_MIN_RAM_MB and not _FORCE:
        pytest.skip(
            f"local Piper needs {DEFAULT_PIPER_MIN_RAM_MB} MB of RAM but this machine reports "
            f"MemTotal {total_mb} MB and MemAvailable {available_mb} MB; set "
            "CHIMEDECK_TEST_PIPER_FORCE=1 to measure anyway"
        )

    probe = PiperProbe(_StubSettings(), meminfo_path=MEMINFO_PATH)
    engine = PiperEngine(probe, voices_dir=_FIXTURE_DIR, render_timeout_s=_RENDER_TIMEOUT_S)
    destination = tmp_path / "out.wav"
    try:
        await engine.warmup()
        await engine.render(_FIXED_TEXT, TtsOptions(voice=VOICE), destination=destination)

        payload = destination.read_bytes()
        assert payload[:4] == b"RIFF"
        assert payload[8:12] == b"WAVE"
        with wave.open(str(destination), "rb") as reader:
            frames = reader.getnframes()
            rate = reader.getframerate()
        assert frames > 0
        assert rate > 0
        duration = frames / rate
        assert _MIN_DURATION_S < duration < _MAX_DURATION_S

        measured = engine.last_synthesis_ms
        assert measured is not None
        assert measured > 0
        peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        _LOGGER.info(
            "piper synthesised %d characters with %s in %d ms; process peak RSS %d kB "
            "(includes the app baseline; piper synthesises in-process, so this is not "
            "piper's own RSS)",
            len(_FIXED_TEXT),
            VOICE,
            measured,
            peak_kb,
        )
    finally:
        engine.close()
        probe.close()
