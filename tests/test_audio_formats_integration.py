"""Real libmpv decodes every shipped format and reaches end-of-file.

Coverage (c) of design 07 §2: mp3 / ogg / flac / wav fixtures all play to null.
Playback uses ``--ao=null``, so libmpv runs the full decode and playback pipeline
(device resolution, codec setup, end-file callback) with no sound device. "It
plays" therefore means "it decoded and played to a null sink", never "it was
heard" (design 07 §3).

The skip conditions are collection-time, so nothing is encoded -- and no real
libmpv is loaded -- unless the harness asked for it.

Owning ticket: HSD-0mmx.
"""

from __future__ import annotations

import asyncio
import locale
import os
import shutil
from pathlib import Path
from typing import Final

import pytest
from audio_fixtures import FORMAT_CODECS, generate_audio_fixtures

from chime_deck.audio import AudioBackendUnavailable, MpvBackend

_HARNESS_HINT = (
    "run ./scripts/test-integration.sh (container) or "
    "./scripts/test-integration.sh --board (real aarch64)"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("CHIMEDECK_TEST_REAL_MPV") != "1",
        reason=f"real libmpv is opt-in; {_HARNESS_HINT}",
    ),
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None,
        reason=f"ffmpeg is needed to generate the audio fixtures; {_HARNESS_HINT}",
    ),
]

_AUDIO_OUTPUT: Final = os.environ.get("MPV_AO", "null")
"""mpv audio output; the harness sets ``MPV_AO=null``, which needs no device."""

_AWAIT_TIMEOUT_S: Final = 20.0
_PLAY_TIMEOUT_S: Final = 30.0


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Encode the four tones once for the whole module.

    :param tmp_path_factory: pytest temporary directory factory.
    :return: fixture extension to the encoded path.
    """
    return generate_audio_fixtures(tmp_path_factory.mktemp("audio-format-fixtures"))


@pytest.mark.parametrize("extension", sorted(FORMAT_CODECS))
async def test_real_libmpv_plays_every_format(extension: str, fixtures: dict[str, Path]) -> None:
    """Each format decodes through libmpv and fires the end-file callback once.

    ``locale.setlocale(locale.LC_NUMERIC)`` is captured and restored because
    ``_default_player_factory`` imports python-mpv lazily inside ``start()``, and
    python-mpv pins the process-wide ``LC_NUMERIC`` to ``C``. This module sorts
    before ``test_tts_*`` and ``test_ws_app``, and with one process plus
    ``filterwarnings = ["error"]`` that bleed-through is a real failure mode, not
    a theoretical one.

    :param extension: fixture format under test.
    :param fixtures: the module-wide encoded fixtures.
    """
    recorded: list[str] = []
    saved_numeric_locale = locale.setlocale(locale.LC_NUMERIC)
    backend = MpvBackend(play_timeout=_PLAY_TIMEOUT_S, mpv_options={"ao": _AUDIO_OUTPUT})
    try:
        try:
            await asyncio.wait_for(backend.start(), timeout=_AWAIT_TIMEOUT_S)
        except AudioBackendUnavailable as exc:
            pytest.skip(f"libmpv is not available on this host ({exc}); {_HARNESS_HINT}")
        # The awaited play() is load-bearing: it raises on ``_END_FILE_ERROR``, and
        # ``when_done`` fires on failure too, so ``recorded == ["done"]`` alone would
        # not prove a successful decode. Do not "simplify" this call away.
        await asyncio.wait_for(
            backend.play(
                fixtures[extension],
                volume=70,
                when_done=lambda: recorded.append("done"),
            ),
            timeout=_AWAIT_TIMEOUT_S,
        )
        assert backend.playing is False
        assert recorded == ["done"]
    finally:
        await asyncio.wait_for(backend.stop(), timeout=_AWAIT_TIMEOUT_S)
        locale.setlocale(locale.LC_NUMERIC, saved_numeric_locale)
