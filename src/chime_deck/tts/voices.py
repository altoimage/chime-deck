"""The curated Piper voice catalogue, its downloader and its progress registry.

**What this is.** :data:`SUGGESTED_VOICES` is the short list of voices the
settings page offers, :class:`VoiceManager` downloads and verifies one of them
into ``data/voices``, and :class:`VoiceDownloadRegistry` runs those downloads as
background jobs so a 60 MB transfer never holds a request open.

**Where the facts come from.** Paths, sizes and md5 digests are copied verbatim
from
``https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/voices.json``
(2026-11-14). Licence strings are transcribed by hand from each voice's
``MODEL_CARD`` at the same tag, because ``voices.json`` has no licence field;
the ``MODEL_CARD`` digests recorded in the table are constants used only to
notice a republished model. They are never ``stat()``ed against a downloaded
file, because ``piper.download_voices`` fetches only ``.onnx`` and
``.onnx.json``.

**Downloads are curated-only.** Only a row with a published digest and a
licence can be verified, so an uncurated id is refused rather than accepted
unverified. A model installed by hand is still *listed* by
:meth:`VoiceManager.installed` and still usable by the engine; it simply has no
licence metadata and no verified checksum.

**Redirects are followed here, deliberately.** Every other client in this tree
is ``follow_redirects=False`` and :mod:`chime_deck.ha.client` explains why:
an ``Authorization`` header must not travel cross-origin. These are public,
unauthenticated model artefacts and the host always redirects to its CDN, so
this module owns a ``follow_redirects=True`` client with at most
:data:`REDIRECT_LIMIT` hops and never borrows ``app.state.ha_client`` or the
sounds client.

**Why the streamed HTTP fetch, not the vendor tool.** Design 04 §4 explicitly
allows "the HTTP equivalent" of ``python -m piper.download_voices``, and it is
the only shape that gives checksum verification, byte progress and an atomic
publish: the vendor tool verifies nothing and would leave a truncated file in
place. The checksum is **integrity over HTTPS against the publisher's published
digests, not authenticity** — there is no signature, so it detects a corrupt or
republished file, not a compromised catalogue.

**Deterministic re-runs.** ``--force-redownload`` is safe because every file is
checked against the table's size and digest: a re-run either rewrites exactly
the published bytes or refuses with ``VOICE_CORRUPT``. Nothing is ever
*replaced* on a mismatch, so a good model on disk survives a failed refresh.

**Atomic publish.** The config is moved into place first and the model second
(:func:`os.replace`, both inside the voices directory), so a crash can never
leave a model without its config. Both live in a temporary directory that a
``finally`` removes, so a cancellation cannot leak up to
:data:`MAX_VOICE_BYTES` inside ``data/voices``; a directory left by a killed
process is swept by :meth:`VoiceManager.sweep_stale_temps`.

**Bounded.** The whole transfer is capped by :data:`DOWNLOAD_TIMEOUT_S` and each
file by :data:`MAX_VOICE_BYTES`, enforced on the running byte count so a lying
``Content-Length`` cannot fill the disk.

**D22.** A download is refused below the memory threshold — the same gate the
engine applies, asked through :meth:`VoiceManager.gate` — unless the user
lowers ``tts.piper.min_ram_mb``.

Owning ticket: HSD-p50k; the licence strategy follows D14 and the closed
HSD-2giv. Refresh this table, and ``tests/golden/piper_voices_v1_0_0.json``
together with it, when upstream republishes a voice.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final
from uuid import uuid4

import httpx

from ..store.store import utcnow_iso
from .engines import Availability
from .errors import VoiceError, VoiceErrorCode, clean_detail
from .piper import (
    MAX_VOICE_ID_CHARS,
    VOICE_CONFIG_SUFFIX,
    VOICE_ID_PATTERN,
    VOICE_ONNX_SUFFIX,
    PiperProbe,
)

__all__ = [
    "CHUNK_BYTES",
    "DOWNLOAD_TIMEOUT_S",
    "MAX_JOBS",
    "MAX_VOICE_BYTES",
    "MIN_ONNX_BYTES",
    "REDIRECT_LIMIT",
    "STALE_TEMP_AGE_S",
    "SUGGESTED_VOICES",
    "VOICE_REPO_ROOT",
    "InstalledVoice",
    "SuggestedVoice",
    "VoiceDownloadJob",
    "VoiceDownloadRegistry",
    "VoiceError",
    "VoiceErrorCode",
    "VoiceManager",
    "VoiceState",
    "install_url",
]

_LOGGER = logging.getLogger(__name__)

VOICE_REPO_ROOT: Final = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
"""Pinned tag every catalogue path is resolved against."""

DOWNLOAD_TIMEOUT_S: Final = 300.0
"""Deadline covering both files of one voice, fetched sequentially."""

CHUNK_BYTES: Final = 64 * 1024
"""Bytes requested per stream iteration."""

MAX_VOICE_BYTES: Final = 128 * 1024 * 1024
"""Hard cap on one downloaded file, enforced on the running byte count.

A ``Content-Length`` is only a hint: it rejects an obviously large file early,
but it is never trusted, because a lying server could otherwise make the device
write gigabytes to disk.
"""

MIN_ONNX_BYTES: Final = 1024 * 1024
"""Smallest body that can plausibly be a Piper model rather than an error page."""

REDIRECT_LIMIT: Final = 5
"""Redirect hops followed before the transfer is refused."""

STALE_TEMP_AGE_S: Final = 3600.0
"""A hidden temporary directory older than this is left over from a crash."""

MAX_JOBS: Final = 20
"""Most download jobs the registry keeps, newest terminal ones first."""

QUALITY_ORDER: Final = {"x_low": 0, "low": 1, "medium": 2, "high": 3}
"""Piper quality names ordered from smallest and fastest to largest and best."""

_HTML_HEAD: Final = frozenset({b"<", b"{", b"["})
"""First bytes that mean "an error page was saved as a model", not a model."""


@dataclass(frozen=True, slots=True)
class SuggestedVoice:
    """One curated Piper voice, exactly as the pinned release publishes it.

    :attr:`size_mb` is not stored: it is derived from :attr:`onnx_bytes` and
    measured in **MiB**, because design 04 §4's "~20 MB for low" is really the
    ``x_low`` figure (about 20 MiB) and ``low`` is about 60 MiB.
    """

    id: str
    language: str
    quality: str
    sample_rate_hz: int
    num_speakers: int
    licence: str
    licence_url: str
    file_onnx: str
    onnx_bytes: int
    onnx_md5: str
    file_config: str
    config_bytes: int
    config_md5: str
    file_model_card: str
    model_card_bytes: int
    model_card_md5: str

    @property
    def size_mb(self) -> float:
        """Model size in MiB, rounded to one decimal, for the settings page."""
        return round(self.onnx_bytes / (1024 * 1024), 1)


_VOICE_ROWS: Final[tuple[SuggestedVoice, ...]] = (
    SuggestedVoice(
        id="de_DE-eva_k-x_low",
        language="de_DE",
        quality="x_low",
        sample_rate_hz=16000,
        num_speakers=1,
        licence="See model card",
        licence_url="https://www.caito.de/2019/01/03/the-m-ailabs-speech-dataset/",
        file_onnx="de/de_DE/eva_k/x_low/de_DE-eva_k-x_low.onnx",
        onnx_bytes=20628813,
        onnx_md5="51bfc52a58282c2e4fc01ae66567a708",
        file_config="de/de_DE/eva_k/x_low/de_DE-eva_k-x_low.onnx.json",
        config_bytes=4158,
        config_md5="2034ba7a991ac47d911c98ee4844fc90",
        file_model_card="de/de_DE/eva_k/x_low/MODEL_CARD",
        model_card_bytes=246,
        model_card_md5="02b01f3d47b2798ece347b2c7e94c9e9",
    ),
    SuggestedVoice(
        id="es_ES-carlfm-x_low",
        language="es_ES",
        quality="x_low",
        sample_rate_hz=16000,
        num_speakers=1,
        licence="public domain",
        licence_url="https://github.com/carlfm01/my-speech-datasets",
        file_onnx="es/es_ES/carlfm/x_low/es_ES-carlfm-x_low.onnx",
        onnx_bytes=28130791,
        onnx_md5="4137b5aee01ea6241080fc4dbe59a8ee",
        file_config="es/es_ES/carlfm/x_low/es_ES-carlfm-x_low.onnx.json",
        config_bytes=4159,
        config_md5="f4ab058cc8b5d024f1b123d06049df22",
        file_model_card="es/es_ES/carlfm/x_low/MODEL_CARD",
        model_card_bytes=250,
        model_card_md5="19cb47bbe9e07e8d7937cfd39027d3a9",
    ),
    SuggestedVoice(
        id="nl_BE-nathalie-x_low",
        language="nl_BE",
        quality="x_low",
        sample_rate_hz=16000,
        num_speakers=1,
        licence="CC0",
        licence_url="https://github.com/rhasspy/dataset-voice-nathalie",
        file_onnx="nl/nl_BE/nathalie/x_low/nl_BE-nathalie-x_low.onnx",
        onnx_bytes=20628813,
        onnx_md5="4a00803b60caecad30ea612bcd9f9344",
        file_config="nl/nl_BE/nathalie/x_low/nl_BE-nathalie-x_low.onnx.json",
        config_bytes=4163,
        config_md5="5307e9c8398ab02060611bb9c2e0f137",
        file_model_card="nl/nl_BE/nathalie/x_low/MODEL_CARD",
        model_card_bytes=246,
        model_card_md5="5df62094bde427374223f91f44476392",
    ),
    SuggestedVoice(
        id="en_GB-alan-low",
        language="en_GB",
        quality="low",
        sample_rate_hz=16000,
        num_speakers=1,
        licence="See model card",
        licence_url="https://github.com/MycroftAI/mimic3-voices/blob/master/voices/en_UK/apope_low",
        file_onnx="en/en_GB/alan/low/en_GB-alan-low.onnx",
        onnx_bytes=63104526,
        onnx_md5="2acae8c79395ab109a7572f0afa61fff",
        file_config="en/en_GB/alan/low/en_GB-alan-low.onnx.json",
        config_bytes=4170,
        config_md5="4c0fa2c6763bf49b343cbb4f655a147b",
        file_model_card="en/en_GB/alan/low/MODEL_CARD",
        model_card_bytes=309,
        model_card_md5="b116c3cbdebac99ade9af03807cb9301",
    ),
    SuggestedVoice(
        id="en_GB-southern_english_female-low",
        language="en_GB",
        quality="low",
        sample_rate_hz=16000,
        num_speakers=1,
        licence="CC-BY-SA 4.0 International",
        licence_url="https://creativecommons.org/licenses/by-sa/4.0/",
        file_onnx="en/en_GB/southern_english_female/low/en_GB-southern_english_female-low.onnx",
        onnx_bytes=63104526,
        onnx_md5="596c7ed4d8488cf64e027765dce2dad1",
        file_config=(
            "en/en_GB/southern_english_female/low/en_GB-southern_english_female-low.onnx.json"
        ),
        config_bytes=4189,
        config_md5="7548456e9f2bc4c16bf818be26618ea1",
        file_model_card="en/en_GB/southern_english_female/low/MODEL_CARD",
        model_card_bytes=296,
        model_card_md5="77ac998c8b37842ef98594567f141629",
    ),
    SuggestedVoice(
        id="en_US-kathleen-low",
        language="en_US",
        quality="low",
        sample_rate_hz=16000,
        num_speakers=1,
        licence="CC0",
        licence_url="https://github.com/rhasspy/dataset-voice-kathleen",
        file_onnx="en/en_US/kathleen/low/en_US-kathleen-low.onnx",
        onnx_bytes=63104526,
        onnx_md5="dd1ab131724b1cff76fe388252bec47b",
        file_config="en/en_US/kathleen/low/en_US-kathleen-low.onnx.json",
        config_bytes=4169,
        config_md5="d970eebfe8e9f8515e405659da658f9b",
        file_model_card="en/en_US/kathleen/low/MODEL_CARD",
        model_card_bytes=281,
        model_card_md5="0585e0a798d093c9ee090b99d9c8f68e",
    ),
    SuggestedVoice(
        id="en_US-lessac-low",
        language="en_US",
        quality="low",
        sample_rate_hz=16000,
        num_speakers=1,
        licence="Blizzard 2013 (see model card)",
        licence_url=(
            "https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html"
        ),
        file_onnx="en/en_US/lessac/low/en_US-lessac-low.onnx",
        onnx_bytes=63201294,
        onnx_md5="31883a7506589feadf3c3474fd8ef658",
        file_config="en/en_US/lessac/low/en_US-lessac-low.onnx.json",
        config_bytes=4882,
        config_md5="64876daa19dd042b368cd64fa379a75f",
        file_model_card="en/en_US/lessac/low/MODEL_CARD",
        model_card_bytes=345,
        model_card_md5="999cbf2c337d8fb2f21b0fa2c95e9e85",
    ),
    SuggestedVoice(
        id="en_GB-alba-medium",
        language="en_GB",
        quality="medium",
        sample_rate_hz=22050,
        num_speakers=1,
        licence="CC BY 4.0",
        licence_url="https://creativecommons.org/licenses/by/4.0/",
        file_onnx="en/en_GB/alba/medium/en_GB-alba-medium.onnx",
        onnx_bytes=63201294,
        onnx_md5="c07f313752bb3aba8061041666251654",
        file_config="en/en_GB/alba/medium/en_GB-alba-medium.onnx.json",
        config_bytes=4888,
        config_md5="dbb6f2ede31082710665221417906e13",
        file_model_card="en/en_GB/alba/medium/MODEL_CARD",
        model_card_bytes=324,
        model_card_md5="d5a8716acb311b20e0f28710d0fcc982",
    ),
    SuggestedVoice(
        id="en_GB-cori-medium",
        language="en_GB",
        quality="medium",
        sample_rate_hz=22050,
        num_speakers=1,
        licence="public domain",
        licence_url="https://librivox.org",
        file_onnx="en/en_GB/cori/medium/en_GB-cori-medium.onnx",
        onnx_bytes=63531379,
        onnx_md5="f143307611eccea9d976235d0895f57c",
        file_config="en/en_GB/cori/medium/en_GB-cori-medium.onnx.json",
        config_bytes=4966,
        config_md5="12b1dc45d8919f3475cf296d5f16a4c6",
        file_model_card="en/en_GB/cori/medium/MODEL_CARD",
        model_card_bytes=474,
        model_card_md5="43861aa2d2c143356702fd966b7812c4",
    ),
    SuggestedVoice(
        id="en_GB-northern_english_male-medium",
        language="en_GB",
        quality="medium",
        sample_rate_hz=22050,
        num_speakers=1,
        licence="CC-BY-SA 4.0 International",
        licence_url="https://creativecommons.org/licenses/by-sa/4.0/",
        file_onnx=("en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium.onnx"),
        onnx_bytes=63201294,
        onnx_md5="4c9a9735bfb76ad67c8b31b23d6840a0",
        file_config=(
            "en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium.onnx.json"
        ),
        config_bytes=4847,
        config_md5="0ce3f61a9604616ed475c921fdeedb1a",
        file_model_card="en/en_GB/northern_english_male/medium/MODEL_CARD",
        model_card_bytes=305,
        model_card_md5="8d1b725154c658ead4f068389c319c82",
    ),
    SuggestedVoice(
        id="en_US-joe-medium",
        language="en_US",
        quality="medium",
        sample_rate_hz=22050,
        num_speakers=1,
        licence="CC0",
        licence_url="https://github.com/NabuCasa/voice-datasets",
        file_onnx="en/en_US/joe/medium/en_US-joe-medium.onnx",
        onnx_bytes=63201294,
        onnx_md5="74fd6a4dc39e0aa9dce145d7f5acd4f6",
        file_config="en/en_US/joe/medium/en_US-joe-medium.onnx.json",
        config_bytes=4794,
        config_md5="811036b9c1451545f9495fdc1baa0754",
        file_model_card="en/en_US/joe/medium/MODEL_CARD",
        model_card_bytes=280,
        model_card_md5="7d25cb111aa9699518764a1cb3943af1",
    ),
    SuggestedVoice(
        id="en_US-libritts_r-medium",
        language="en_US",
        quality="medium",
        sample_rate_hz=22050,
        num_speakers=904,
        licence="CC BY 4.0",
        licence_url="http://www.openslr.org/141/",
        file_onnx="en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx",
        onnx_bytes=78580914,
        onnx_md5="bb2c2776cffbfd736c7c497f620c0ca6",
        file_config="en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx.json",
        config_bytes=20123,
        config_md5="c740db18a0dab11b13ce0292ea1196c6",
        file_model_card="en/en_US/libritts_r/medium/MODEL_CARD",
        model_card_bytes=279,
        model_card_md5="ae42a6ca5f3fdc81690d4fe191b6fdee",
    ),
    SuggestedVoice(
        id="en_US-ljspeech-medium",
        language="en_US",
        quality="medium",
        sample_rate_hz=22050,
        num_speakers=1,
        licence="public domain",
        licence_url="https://keithito.com/LJ-Speech-Dataset/",
        file_onnx="en/en_US/ljspeech/medium/en_US-ljspeech-medium.onnx",
        onnx_bytes=63531379,
        onnx_md5="109d552e9dd78d92d1169a7edd6de38d",
        file_config="en/en_US/ljspeech/medium/en_US-ljspeech-medium.onnx.json",
        config_bytes=4972,
        config_md5="0668112b8b3ac5bb4b12c2b1a366776a",
        file_model_card="en/en_US/ljspeech/medium/MODEL_CARD",
        model_card_bytes=517,
        model_card_md5="5ad31d314786587d4f97effb0b716d61",
    ),
)
"""Every row of the pinned release, before it is ordered for the settings page."""

SUGGESTED_VOICES: Final[tuple[SuggestedVoice, ...]] = tuple(
    sorted(_VOICE_ROWS, key=lambda voice: (QUALITY_ORDER[voice.quality], voice.id))
)
"""The curated catalogue, smallest and fastest quality first, then by id."""

_SUGGESTED_BY_ID: Final[Mapping[str, SuggestedVoice]] = MappingProxyType(
    {voice.id: voice for voice in SUGGESTED_VOICES}
)
"""Lookup table for one voice id, so a download never scans the tuple."""


def install_url(voice: SuggestedVoice, file: str) -> str:
    """Return the download URL for one file of *voice*.

    The catalogue path is used verbatim, so the language-family directory
    (``en/en_GB/...``) can never be guessed wrong.

    :param voice: curated row the file belongs to.
    :param file: one of the row's published paths.
    """
    return f"{VOICE_REPO_ROOT}/{file}"


@dataclass(frozen=True, slots=True)
class InstalledVoice:
    """One voice model found on disk, with whatever can be verified about it.

    :attr:`problem` is a whole sentence when the model cannot be used (a missing
    or unparsable config) and ``None`` when the voice is complete. A model that
    is not in the curated table has no licence and no verified checksum, but it
    is still listed and still loadable by the engine.
    """

    id: str
    onnx_path: Path
    config_path: Path | None
    bytes: int | None
    licence: str | None
    licence_known: bool
    checksum_verified: bool
    problem: str | None


class VoiceManager:
    """Download curated voice models into ``voices_dir`` and describe what is there.

    Construction is inert: no directory is created, no client is built and no
    file is read. A client passed in is *borrowed* — the same rule as
    :class:`chime_deck.ha.client.HomeAssistantClient`, which tracks
    ``_owns_client`` — so a download through an injected client leaves it open
    and usable for the next one.
    """

    def __init__(
        self,
        probe: PiperProbe,
        voices_dir: Path,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = DOWNLOAD_TIMEOUT_S,
        max_bytes: int = MAX_VOICE_BYTES,
        chunk_bytes: int = CHUNK_BYTES,
        redirect_limit: int = REDIRECT_LIMIT,
    ) -> None:
        """Create the manager and remember its limits; touch nothing yet.

        :param probe: the environment gate, shared with the engine so a download
            and a press cannot disagree about this machine.
        :param voices_dir: directory voice models live in, normally
            ``data/voices``.
        :param client: borrowed HTTP client; ``None`` creates and closes one per
            download. A borrowed client is never closed here.
        :param timeout_s: deadline covering both files of one voice.
        :param max_bytes: hard cap on one downloaded file.
        :param chunk_bytes: bytes requested per stream iteration.
        :param redirect_limit: redirect hops followed before a transfer is
            refused.
        """
        self._probe = probe
        self._voices_dir = voices_dir
        self._client = client
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._chunk_bytes = chunk_bytes
        self._redirect_limit = redirect_limit

    def suggested(self) -> list[SuggestedVoice]:
        """Return the curated catalogue, as a copy so a caller cannot edit it."""
        return list(SUGGESTED_VOICES)

    def suggested_voice(self, voice_id: str) -> SuggestedVoice | None:
        """Return the curated row for *voice_id*, or ``None`` when it is not curated.

        :param voice_id: voice id from a request or the settings page.
        """
        return _SUGGESTED_BY_ID.get(voice_id)

    def installed(self) -> list[InstalledVoice]:
        """Return every voice model on disk, complete or not, never raising.

        Hidden entries and names that are not a Piper voice id are skipped. A
        model whose config is missing or unusable is reported with its
        :attr:`~InstalledVoice.problem` rather than hidden, and an unreadable
        directory answers an empty list. Checking one model against the table
        reads it once to hash it, so a route should call this off the event
        loop.
        """
        try:
            entries = sorted(self._voices_dir.iterdir())
        except OSError:
            return []
        voices: list[InstalledVoice] = []
        for entry in entries:
            name = entry.name
            if name.startswith(".") or not name.endswith(VOICE_ONNX_SUFFIX):
                continue
            voice_id = name[: -len(VOICE_ONNX_SUFFIX)]
            if VOICE_ID_PATTERN.fullmatch(voice_id) is None:
                continue
            if not entry.is_file():
                continue
            voices.append(self._describe(voice_id, entry))
        return voices

    async def gate(self) -> Availability:
        """Return whether a voice model could be installed on this machine.

        The D22 memory gate, the optional extra and the import state come from
        :meth:`chime_deck.tts.piper.PiperProbe.installable`, so the download
        route and the engine cannot disagree about the same host.
        """
        return await self._probe.installable()

    async def memory_warning(self) -> str | None:
        """Return the D22 sentence when this machine is too small for local Piper.

        Separate from :meth:`gate` because the settings page shows the RAM
        warning even when a download is refused for a different reason.
        """
        verdict = await self._probe.memory()
        return None if verdict.ok else verdict.reason

    async def download(
        self,
        voice_id: str,
        *,
        on_progress: Callable[[int, int | None], None] | None = None,
    ) -> InstalledVoice:
        """Download, verify and publish one curated voice.

        The id is validated before anything is created, the environment gate is
        asked before the network is touched, and both files land in a temporary
        directory that is removed whatever happens. The config is published
        first and the model second, so an interrupted publish can never leave a
        model without its config.

        :param voice_id: curated voice id, e.g. ``en_GB-alan-low``.
        :param on_progress: called with ``(bytes_done, bytes_total)`` as the two
            files stream in; the total spans both files and is always known.
        :raises VoiceError: for an invalid or uncurated id, a failed gate, an
            oversized or truncated body, a checksum mismatch or a config that is
            not a Piper voice config.
        """
        voice = _require_suggested(voice_id)
        gate = await self.gate()
        if not gate.ok:
            raise VoiceError(
                VoiceErrorCode.VOICE_UNAVAILABLE,
                gate.reason or f"the {voice_id} voice cannot be installed on this device",
                {"voice": voice_id, "reason": "unavailable"},
            )
        self._voices_dir.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix=".", suffix=".tmp", dir=self._voices_dir))
        onnx_tmp = temp / f"{voice_id}{VOICE_ONNX_SUFFIX}"
        config_tmp = temp / f"{voice_id}{VOICE_CONFIG_SUFFIX}"
        final_onnx = self._voices_dir / f"{voice_id}{VOICE_ONNX_SUFFIX}"
        final_config = self._voices_dir / f"{voice_id}{VOICE_CONFIG_SUFFIX}"
        try:
            async with asyncio.timeout(self._timeout_s):
                client = self._client
                if client is None:
                    owned = httpx.AsyncClient(
                        follow_redirects=True,
                        max_redirects=self._redirect_limit,
                        timeout=httpx.Timeout(self._timeout_s),
                    )
                    async with owned:
                        await self._fetch_voice(owned, voice, onnx_tmp, config_tmp, on_progress)
                else:
                    await self._fetch_voice(client, voice, onnx_tmp, config_tmp, on_progress)
            _validate_config(config_tmp, voice_id)
            os.replace(config_tmp, final_config)
            os.replace(onnx_tmp, final_onnx)
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            raise _classify_transfer(exc) from None
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        _LOGGER.info("installed the Piper voice %s", voice_id)
        return InstalledVoice(
            id=voice_id,
            onnx_path=final_onnx,
            config_path=final_config,
            bytes=voice.onnx_bytes,
            licence=voice.licence,
            licence_known=True,
            checksum_verified=True,
            problem=None,
        )

    def sweep_stale_temps(self) -> int:
        """Delete temporary directories a killed process left in ``voices_dir``.

        Only names shaped like :meth:`download`'s own temporary directory
        (``.<random>.tmp``) and older than :data:`STALE_TEMP_AGE_S` are removed,
        the same one-hour rule the speech cache applies, so a download running
        right now is never swept. Never raises.

        :returns: number of directories removed.
        """
        now = time.time()
        try:
            entries = list(self._voices_dir.iterdir())
        except OSError:
            return 0
        removed = 0
        for entry in entries:
            name = entry.name
            if not (name.startswith(".") and name.endswith(".tmp")):
                continue
            try:
                if not entry.is_dir():
                    continue
                if now - entry.stat().st_mtime <= STALE_TEMP_AGE_S:
                    continue
            except OSError:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        return removed

    def close(self) -> None:
        """Release the manager's resources; there is nothing to release today.

        Every per-download client is closed by its own ``async with``, so this
        is a no-op kept only so the application has one shutdown entry point to
        call once it owns something.
        """

    async def _fetch_voice(
        self,
        client: httpx.AsyncClient,
        voice: SuggestedVoice,
        onnx_tmp: Path,
        config_tmp: Path,
        on_progress: Callable[[int, int | None], None] | None,
    ) -> None:
        """Stream the model and then its config into the temporary directory.

        :param client: client that sends the requests.
        :param voice: curated row being installed.
        :param onnx_tmp: temporary file that receives the model.
        :param config_tmp: temporary file that receives the config.
        :param on_progress: called with the running total across both files.
        """
        total = voice.onnx_bytes + voice.config_bytes
        fetched = 0

        def report(file_done: int, _file_total: int | None) -> None:
            """Add the finished file's bytes, so one running total spans both.

            :param file_done: bytes of the file being streamed so far.
            :param _file_total: that file's expected size, unused here.
            """
            if on_progress is not None:
                on_progress(fetched + file_done, total)

        await self._stream_file(
            client,
            install_url(voice, voice.file_onnx),
            onnx_tmp,
            expected_bytes=voice.onnx_bytes,
            expected_md5=voice.onnx_md5,
            label=voice.id,
            on_progress=report,
        )
        fetched = voice.onnx_bytes
        await self._stream_file(
            client,
            install_url(voice, voice.file_config),
            config_tmp,
            expected_bytes=voice.config_bytes,
            expected_md5=voice.config_md5,
            label=voice.id,
            on_progress=report,
        )

    async def _stream_file(
        self,
        client: httpx.AsyncClient,
        url: str,
        destination: Path,
        *,
        expected_bytes: int,
        expected_md5: str,
        label: str,
        on_progress: Callable[[int, int | None], None] | None,
    ) -> int:
        """Stream one catalogue file into *destination* and verify it.

        The digest is computed on the bytes as they arrive, so nothing is held
        in memory and a lying ``Content-Length`` cannot fill the disk. The
        redirect budget is enforced here too: ``follow_redirects=True`` is
        passed per request because a borrowed client may have been built with
        the repository default, and a redirect loop is a transfer failure, never
        a corrupt model.

        :param client: client that sends the request.
        :param url: catalogue URL, used verbatim.
        :param destination: file the response is written to.
        :param expected_bytes: size the catalogue publishes for this file.
        :param expected_md5: md5 the catalogue publishes for this file.
        :param label: voice id used in every sentence and detail field.
        :param on_progress: called with the bytes this file has so far and its
            expected size.
        :returns: number of bytes written.
        :raises VoiceError: for a server error, an oversized body, a truncated
            body, a checksum mismatch or a body that is not a voice model.
        """
        digest = hashlib.md5()
        received = 0
        limit_mb = self._max_bytes // (1024 * 1024)
        async with client.stream("GET", url, follow_redirects=True) as response:
            if len(response.history) > self._redirect_limit:
                raise VoiceError(
                    VoiceErrorCode.DOWNLOAD_FAILED,
                    f"the download of the {label} voice was redirected "
                    f"{len(response.history)} times, more than the {self._redirect_limit} hops "
                    "this doorbell follows — check that huggingface.co is reachable and try again",
                    {"voice": label, "file": destination.name, "reason": "too_many_redirects"},
                )
            status = response.status_code
            if status >= 400:
                raise VoiceError(
                    VoiceErrorCode.DOWNLOAD_FAILED,
                    f"the server answered HTTP {status} while fetching the {label} voice — check "
                    "that this device can reach huggingface.co and try again",
                    {"voice": label, "file": destination.name, "status": status},
                )
            declared = _content_length(response.headers.get("content-length"))
            if declared is not None and declared > self._max_bytes:
                raise VoiceError(
                    VoiceErrorCode.VOICE_TOO_LARGE,
                    f"the server says the {label} file is larger than the {limit_mb} MB limit — "
                    "that voice cannot be installed on this device",
                    {
                        "voice": label,
                        "file": destination.name,
                        "bytes_declared": declared,
                        "limit_bytes": self._max_bytes,
                    },
                )
            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes(self._chunk_bytes):
                    received += len(chunk)
                    if received > self._max_bytes:
                        raise VoiceError(
                            VoiceErrorCode.VOICE_TOO_LARGE,
                            f"the {label} file passed the {limit_mb} MB limit while downloading — "
                            "that voice cannot be installed on this device",
                            {
                                "voice": label,
                                "file": destination.name,
                                "bytes_received": received,
                                "limit_bytes": self._max_bytes,
                            },
                        )
                    digest.update(chunk)
                    handle.write(chunk)
                    if on_progress is not None:
                        on_progress(received, expected_bytes)
        if received != expected_bytes:
            raise VoiceError(
                VoiceErrorCode.VOICE_CORRUPT,
                f"the downloaded {destination.name} is {received} bytes but {expected_bytes} "
                "were published — the transfer was cut short; download the voice again",
                {
                    "voice": label,
                    "file": destination.name,
                    "bytes_expected": expected_bytes,
                    "bytes_received": received,
                },
            )
        if received == 0:
            raise VoiceError(
                VoiceErrorCode.VOICE_CORRUPT,
                f"the download of the {label} voice was empty — try again once the device is "
                "back online",
                {"voice": label, "file": destination.name, "bytes_received": 0},
            )
        actual_md5 = digest.hexdigest()
        if actual_md5 != expected_md5:
            raise VoiceError(
                VoiceErrorCode.VOICE_CORRUPT,
                f"the downloaded {destination.name} does not match its published checksum — the "
                "file is corrupt or the model was republished; download the voice again",
                {
                    "voice": label,
                    "file": destination.name,
                    "expected_md5": expected_md5,
                    "actual_md5": actual_md5,
                    "bytes_expected": expected_bytes,
                    "bytes_received": received,
                },
            )
        if destination.name.endswith(VOICE_ONNX_SUFFIX):
            with destination.open("rb") as handle:
                head = handle.read(2)
            if head[:1] in _HTML_HEAD:
                raise VoiceError(
                    VoiceErrorCode.VOICE_CORRUPT,
                    f"the download of the {label} voice is a web page rather than a model — the "
                    "server answered an error page; try again later",
                    {"voice": label, "file": destination.name, "bytes_received": received},
                )
            if received < MIN_ONNX_BYTES:
                raise VoiceError(
                    VoiceErrorCode.VOICE_CORRUPT,
                    f"the downloaded {destination.name} is only {received} bytes, too small to be "
                    "a Piper voice — download the voice again",
                    {
                        "voice": label,
                        "file": destination.name,
                        "bytes_received": received,
                        "min_bytes": MIN_ONNX_BYTES,
                    },
                )
        return received

    def _describe(self, voice_id: str, onnx_path: Path) -> InstalledVoice:
        """Describe one model on disk, verified against the table when it is known.

        :param voice_id: id derived from the model's file name.
        :param onnx_path: the model file that exists.
        """
        config_path = self._voices_dir / f"{voice_id}{VOICE_CONFIG_SUFFIX}"
        reported_config: Path | None
        problem: str | None = None
        if not config_path.is_file():
            reported_config = None
            problem = (
                f"the {voice_id} voice is missing its config file, so piper cannot load it — "
                "download the voice again from Settings → Speech"
            )
        else:
            reported_config = config_path
            try:
                _validate_config(config_path, voice_id)
            except VoiceError as exc:
                problem = str(exc)
        try:
            size: int | None = onnx_path.stat().st_size
        except OSError:
            size = None
        curated = _SUGGESTED_BY_ID.get(voice_id)
        if curated is None:
            return InstalledVoice(
                id=voice_id,
                onnx_path=onnx_path,
                config_path=reported_config,
                bytes=size,
                licence=None,
                licence_known=False,
                checksum_verified=False,
                problem=problem,
            )
        checksum_verified = False
        if size is not None and size == curated.onnx_bytes:
            try:
                digest: str | None = _md5_file(onnx_path)
            except OSError:
                digest = None
            checksum_verified = digest == curated.onnx_md5
        return InstalledVoice(
            id=voice_id,
            onnx_path=onnx_path,
            config_path=reported_config,
            bytes=size,
            licence=curated.licence,
            licence_known=True,
            checksum_verified=checksum_verified,
            problem=problem,
        )


class VoiceState(StrEnum):
    """Lifecycle state of one download job.

    ``failed`` is a transfer failure and ``rejected`` is a refusal or a corrupt
    model; the split matters because the UI tells the user to check the network
    in one case and to pick a real voice in the other.
    """

    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    REJECTED = "rejected"


_REJECTED_CODES: Final[frozenset[VoiceErrorCode]] = frozenset(
    {
        VoiceErrorCode.INVALID_VOICE,
        VoiceErrorCode.VOICE_NOT_SUGGESTED,
        VoiceErrorCode.VOICE_UNAVAILABLE,
        VoiceErrorCode.VOICE_TOO_LARGE,
        VoiceErrorCode.VOICE_CORRUPT,
    }
)
"""Refusals and corrupt models, as opposed to a transfer that never landed."""


def _state_for(code: VoiceErrorCode) -> VoiceState:
    """Return the terminal job state for one refusal code.

    :param code: code carried by the :class:`VoiceError` that ended the job.
    """
    return VoiceState.REJECTED if code in _REJECTED_CODES else VoiceState.FAILED


@dataclass(frozen=True, slots=True)
class VoiceDownloadJob:
    """One download job, as the settings page polls it.

    Snapshots are replaced, never mutated, so a caller may keep one and compare
    it with the next. :attr:`detail` carries the machine-readable facts from the
    :class:`VoiceError` that ended a failed job.
    """

    job_id: str
    voice: str
    state: VoiceState
    bytes_done: int
    bytes_total: int | None
    error: str | None
    detail: Mapping[str, object]
    started_at: str
    finished_at: str | None


class VoiceDownloadRegistry:
    """Run voice downloads as bounded background jobs and keep their progress.

    One job per voice at a time, at most :data:`MAX_JOBS` snapshots retained, and
    every job finishes in one of the terminal states — an unexpected failure is
    recorded, logged and marked ``FAILED`` rather than leaving a silently dead
    task behind.
    """

    def __init__(
        self,
        manager: VoiceManager,
        *,
        max_jobs: int = MAX_JOBS,
        close_grace_s: float = 5.0,
    ) -> None:
        """Create an empty registry; nothing is downloaded until :meth:`start`.

        :param manager: the downloader every job drives.
        :param max_jobs: most job snapshots kept, newest terminal ones first.
        :param close_grace_s: longest :meth:`aclose` waits for cancelled jobs.
        """
        self._manager = manager
        self._max_jobs = max_jobs
        self._close_grace_s = close_grace_s
        self._jobs: dict[str, VoiceDownloadJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self, voice_id: str) -> VoiceDownloadJob:
        """Start one download and return its first snapshot.

        :param voice_id: curated voice id to install.
        :raises VoiceError: for an invalid or uncurated id, a failed environment
            gate, or a job already running for the same voice.
        """
        voice = _require_suggested(voice_id)
        gate = await self._manager.gate()
        if not gate.ok:
            raise VoiceError(
                VoiceErrorCode.VOICE_UNAVAILABLE,
                gate.reason or f"the {voice_id} voice cannot be installed on this device",
                {"voice": voice_id, "reason": "unavailable"},
            )
        if any(
            job.voice == voice_id and job.state is VoiceState.RUNNING for job in self._jobs.values()
        ):
            raise VoiceError(
                VoiceErrorCode.VOICE_UNAVAILABLE,
                f"the {voice_id} voice is already downloading — wait for it to finish",
                {"reason": "already_running", "voice": voice_id},
            )
        job_id = uuid4().hex
        job = VoiceDownloadJob(
            job_id=job_id,
            voice=voice_id,
            state=VoiceState.RUNNING,
            bytes_done=0,
            bytes_total=voice.onnx_bytes + voice.config_bytes,
            error=None,
            detail=MappingProxyType({}),
            started_at=utcnow_iso(),
            finished_at=None,
        )
        self._jobs[job_id] = job
        task = asyncio.create_task(self._run(job_id, voice_id), name=f"voice-download-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda done: self._forget(job_id, done))
        return job

    def status(self, job_id: str) -> VoiceDownloadJob | None:
        """Return the latest snapshot of *job_id*, or ``None`` when it is unknown.

        :param job_id: id returned by :meth:`start`.
        """
        return self._jobs.get(job_id)

    def jobs(self) -> list[VoiceDownloadJob]:
        """Return every retained job, oldest first."""
        return list(self._jobs.values())

    async def aclose(self) -> None:
        """Cancel every running download and mark it failed, bounded and repeatable.

        Cancellation is delivered first and awaited inside
        :attr:`_close_grace_s`, so a stuck transport cannot hold shutdown open.
        The wait is bounded for tasks that honour cancellation; a task that
        suppresses it is abandoned after the grace. A job that was still running
        is marked ``FAILED`` with the sentence the settings page shows after a
        restart. Calling this again is a no-op.
        """
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=self._close_grace_s)
            for task in done:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None:
                    _LOGGER.error("a voice download task stopped with an error: %r", error)
            if pending:
                _LOGGER.warning(
                    "a voice download did not stop within %g s; it stays cancelled",
                    self._close_grace_s,
                )
        for job_id, job in list(self._jobs.items()):
            if job.state is not VoiceState.RUNNING:
                continue
            self._jobs[job_id] = replace(
                job,
                state=VoiceState.FAILED,
                error=(
                    f"the doorbell stopped before the download of the {job.voice} voice finished "
                    "— start it again after the restart"
                ),
                detail=MappingProxyType({"reason": "closed"}),
                finished_at=utcnow_iso(),
            )
        self._trim()

    async def _run(self, job_id: str, voice_id: str) -> None:
        """Drive one download and record its terminal state.

        ``CancelledError`` is a :class:`BaseException`, so it propagates and the
        task ends cancelled; :meth:`aclose` is what turns that into a ``FAILED``
        snapshot.

        :param job_id: id of the job this task reports to.
        :param voice_id: curated voice id being installed.
        """

        def progress(done: int, _total: int | None) -> None:
            """Record a new byte count for the job snapshot.

            :param done: bytes received so far across both files.
            :param _total: expected total, already recorded on the job.
            """
            self._note_progress(job_id, done)

        try:
            installed = await self._manager.download(voice_id, on_progress=progress)
        except VoiceError as exc:
            _LOGGER.info("voice download %s ended %s: %s", voice_id, exc.code, exc)
            self._finish(
                job_id,
                _state_for(exc.code),
                error=str(exc),
                detail=exc.detail,
            )
        except Exception as exc:
            _LOGGER.warning("the voice download of %s failed unexpectedly", voice_id, exc_info=True)
            self._finish(
                job_id,
                VoiceState.FAILED,
                error=(
                    f"the {voice_id} voice download failed unexpectedly "
                    f"({type(exc).__name__}) — check the doorbell log"
                ),
                detail={"reason": "unexpected"},
            )
        else:
            self._finish(
                job_id,
                VoiceState.DONE,
                error=None,
                detail={"bytes": installed.bytes, "path": str(installed.onnx_path)},
            )

    def _note_progress(self, job_id: str, done: int) -> None:
        """Replace a job snapshot with a larger byte count, ignoring a smaller one.

        :param job_id: job being reported on.
        :param done: bytes received so far.
        """
        job = self._jobs.get(job_id)
        if job is None or done <= job.bytes_done:
            return
        self._jobs[job_id] = replace(job, bytes_done=done)

    def _finish(
        self,
        job_id: str,
        state: VoiceState,
        *,
        error: str | None,
        detail: Mapping[str, object],
    ) -> None:
        """Mark a job terminal and trim the retained snapshots.

        :param job_id: job that finished.
        :param state: terminal state to record.
        :param error: whole sentence for the user, or ``None`` on success.
        :param detail: machine-readable facts from the refusal, if any.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return
        self._jobs[job_id] = replace(
            job,
            state=state,
            error=error,
            detail=MappingProxyType(dict(detail)),
            finished_at=utcnow_iso(),
        )
        self._trim()

    def _trim(self) -> None:
        """Drop the oldest terminal jobs until at most :attr:`_max_jobs` remain."""
        excess = len(self._jobs) - self._max_jobs
        if excess <= 0:
            return
        terminal = sorted(
            (
                (job.finished_at or "", job_id)
                for job_id, job in self._jobs.items()
                if job.state is not VoiceState.RUNNING
            ),
        )
        for _finished_at, job_id in terminal[:excess]:
            del self._jobs[job_id]

    def _forget(self, job_id: str, task: asyncio.Task[None]) -> None:
        """Forget a finished task so shutdown only sees running ones.

        :param job_id: job the task drove.
        :param task: the task that just completed.
        """
        if self._tasks.get(job_id) is task:
            del self._tasks[job_id]


def _require_suggested(voice_id: str) -> SuggestedVoice:
    """Return the curated row for *voice_id*, or refuse the download.

    The id is a file name, so it is checked as a plain model name first: a
    separator, ``..``, a NUL or more than
    :data:`MAX_VOICE_ID_CHARS` characters could name a file outside
    ``voices_dir`` and is refused before anything is created. Only then is it
    looked up, because an uncurated id has no digest and no licence to verify
    against.

    :param voice_id: id from a request or the settings page.
    :raises VoiceError: ``INVALID_VOICE`` for a malformed id,
        ``VOICE_NOT_SUGGESTED`` for a well-formed id that is not curated.
    """
    hostile = "/" in voice_id or "\\" in voice_id or ".." in voice_id or "\x00" in voice_id
    too_long = len(voice_id) > MAX_VOICE_ID_CHARS
    if hostile or too_long or VOICE_ID_PATTERN.fullmatch(voice_id) is None:
        raise VoiceError(
            VoiceErrorCode.INVALID_VOICE,
            f"the voice id {clean_detail(voice_id)!r} is not a valid Piper voice id — choose one "
            "from the suggested list in Settings → Speech",
            {"voice": voice_id},
        )
    voice = _SUGGESTED_BY_ID.get(voice_id)
    if voice is None:
        raise VoiceError(
            VoiceErrorCode.VOICE_NOT_SUGGESTED,
            f"the voice {voice_id!r} is not in the curated list this doorbell can verify — choose "
            "one from the suggested list in Settings → Speech",
            {"voice": voice_id},
        )
    return voice


def _classify_transfer(exc: BaseException) -> VoiceError:
    """Map a transfer-time exception onto the domain error.

    One place decides what a failed transfer means, in the house style of
    :func:`chime_deck.sounds.download._classify_transfer_group`: a redirect
    loop and a timeout each get their own actionable sentence, and everything
    else keeps one generic one. Every branch is ``DOWNLOAD_FAILED`` — a transfer
    that never produced a body is not a corrupt model.

    :param exc: what the transfer raised.
    """
    if isinstance(exc, httpx.TooManyRedirects):
        return VoiceError(
            VoiceErrorCode.DOWNLOAD_FAILED,
            "the server redirected the voice download too many times — check that "
            "huggingface.co is reachable and try again",
            {"reason": "too_many_redirects"},
        )
    if isinstance(exc, TimeoutError):
        return VoiceError(
            VoiceErrorCode.DOWNLOAD_FAILED,
            "the voice download timed out — check this device's connection and try again",
            {"reason": "timeout"},
        )
    return VoiceError(
        VoiceErrorCode.DOWNLOAD_FAILED,
        f"the voice download failed ({type(exc).__name__}) — check that this device can reach "
        "huggingface.co and try again",
        {"reason": "transport_error", "error": type(exc).__name__},
    )


def _validate_config(path: Path, voice_id: str) -> None:
    """Require *path* to be a Piper voice config the engine can load.

    The five keys checked here are exactly what ``PiperConfig.from_dict``
    indexes in ``piper-tts`` 1.8.0: ``num_symbols``, ``num_speakers``, an
    ``audio`` mapping with a sample rate, an ``espeak`` mapping with a voice
    name and a non-empty ``phoneme_id_map``. Checking them at download time is
    why a bad sibling becomes a rejected download instead of a ``KeyError``
    inside the synthesis thread at press time.

    :param path: config file to check.
    :param voice_id: voice the config belongs to, used in the sentence.
    :raises VoiceError: ``VOICE_CORRUPT`` if the file cannot be decoded or is
        missing a key the engine indexes.
    """
    sentence = (
        f"the voice's config file is not a Piper voice config — download the {voice_id} voice "
        "again from Settings → Speech"
    )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise VoiceError(
            VoiceErrorCode.VOICE_CORRUPT,
            sentence,
            {"voice": voice_id, "file": path.name, "reason": "not_json"},
        ) from None
    if not isinstance(document, dict):
        raise VoiceError(
            VoiceErrorCode.VOICE_CORRUPT,
            sentence,
            {"voice": voice_id, "file": path.name, "reason": "not_an_object"},
        )
    audio = document.get("audio")
    espeak = document.get("espeak")
    phoneme_id_map = document.get("phoneme_id_map")
    missing: list[str] = []
    if not _is_int(document.get("num_symbols")):
        missing.append("num_symbols")
    if not _is_int(document.get("num_speakers")):
        missing.append("num_speakers")
    if not isinstance(audio, dict) or not _is_int(audio.get("sample_rate")):
        missing.append("audio.sample_rate")
    if not isinstance(espeak, dict) or not _is_str(espeak.get("voice")):
        missing.append("espeak.voice")
    if not isinstance(phoneme_id_map, dict) or not phoneme_id_map:
        missing.append("phoneme_id_map")
    if missing:
        raise VoiceError(
            VoiceErrorCode.VOICE_CORRUPT,
            f"{sentence} ({', '.join(missing)} is missing or malformed)",
            {"voice": voice_id, "file": path.name, "missing": tuple(missing)},
        )


def _is_int(value: object) -> bool:
    """Return whether *value* is a real integer rather than a bool or a string.

    :param value: value from a decoded JSON document.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: object) -> bool:
    """Return whether *value* is a non-empty string.

    :param value: value from a decoded JSON document.
    """
    return isinstance(value, str) and bool(value)


def _md5_file(path: Path) -> str:
    """Return the lower-case md5 hex digest of a file's bytes.

    :param path: file to hash.
    """
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_length(value: str | None) -> int | None:
    """Parse a ``Content-Length`` header, or return ``None`` when unusable.

    :param value: raw header value.
    """
    if value is None:
        return None
    try:
        declared = int(value)
    except ValueError:
        return None
    return declared if declared >= 0 else None
