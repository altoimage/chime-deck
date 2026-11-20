"""The local speech engine, voice-model and pre-rendered-clip endpoints.

Six routes live here: ``GET /api/tts/engines`` lists the engines a press can
try, ``GET /api/tts/voices`` lists the curated voice catalogue next to what is
installed, ``POST /api/tts/voices/{voice_id}/download`` starts installing one
curated model, ``GET /api/tts/voices/downloads/{job_id}`` reports that job's
progress, ``POST /api/tts/bake_all`` pre-renders every static phrase that is
missing or stale, and ``GET /api/tts/bake_usage`` reports how much disk the
bakes and the ephemeral cache occupy. They sit **outside** the buttons router:
nothing here uploads or presses a chime button, and a download or a bake is a
settings action.

**The design 05 §3 erratum.** §3 lists ``GET /api/tts/engines`` but neither
``/api/tts/voices`` nor the download routes, and it describes the engines answer
as a bare list. The route answers an envelope instead —
``{"disabled", "engines", "warning"}`` — because an empty list cannot explain
itself, exactly like ``/api/ha/tts_engines``: "speech is switched off" and "the
engine list did not answer" need different fixes and the settings page must show
one without inventing it.

**The name distinction.** ``tts.ha.engine_id`` defaults to the Home Assistant
*entity* ``"tts.piper"``, which is never the local engine named ``"piper"``.
:class:`chime_deck.tts.engines.TtsOptions` says the same about its
``engine_id`` field: a Home Assistant entity id is not a local engine name, so
this route reports the local names while the settings page keeps the entity in
its own dropdown.

**Reasons pass through unchanged.** Every ``reason`` and ``warning`` string is
the engine's own whole actionable sentence (RAM, missing extra, missing model),
already built by :mod:`chime_deck.tts.piper` and
:mod:`chime_deck.tts.speech`; adding advice here would only duplicate it.

**Bounded degradation.** The engines route never answers ``500``: a missing
resolver, the deliberate off state (D27) and a hung status call are each a
``200`` with an empty ``engines`` list and a sentence. The voices route degrades
the same way for a missing manager; a refused *action*, in contrast, is an
actionable error with a status and a code:

=========================================  =====  ===========================
condition                                  status  envelope code
=========================================  =====  ===========================
malformed or hostile voice id              422     ``validation_error``
well-formed but uncurated voice id         422     ``validation_error``
speech disabled (D27), RAM gate, no extra  422     ``voice_unavailable``
download already running for that voice    409     ``conflict``
unknown job id                             404     ``not_found``
a download larger than the cap             413     ``voice_too_large``
corrupt or republished model               502     ``download_failed``
a start whose transfer fails               502     ``download_failed``
no speech source prepared (bake all)       503     ``engine_unavailable``
speech disabled (D27) (bake all)           422     ``invalid_setting``
=========================================  =====  ===========================

**The two bake routes.** They are the durable side of design 04 §2.1 (D21):
``bake_all`` is the settings page's "pre-render everything now" button, and
``bake_usage`` is the read-only figure beside it. ``bake_all`` is synchronous and
its worst case is honest arithmetic — (number of TTS buttons) times (the engine's own
per-render bound: espeak-ng 10 s, ``remote_tts`` 20 s, ``ha_tts`` its client
timeout) — so a panel that calls it shows progress rather than pretending the
call is instant. One button's failure never fails the batch: it is reported in
``failed`` with its reason and the rest are still attempted. ``bake_usage`` never
fails: it answers ``200`` whether or not a resolver exists, and a missing
directory is ``{files: 0, bytes: 0}``.

Owning ticket: HSD-p50k; the two bake routes were added by HSD-dzek.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, NoReturn

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..tts import (
    Availability,
    InstalledVoice,
    SuggestedVoice,
    VoiceError,
    VoiceErrorCode,
    baked_root,
    clean_detail,
    directory_usage,
)
from .errors import ApiError, ErrorCode

if TYPE_CHECKING:
    # ``app.py`` imports ``.api`` at module level, so a runtime import of
    # ``..app`` here would close the cycle (the same pattern as ``api/sounds.py``).
    from ..app import AppState

__all__ = [
    "TTS_ENGINES_ROUTE_TIMEOUT_S",
    "TTS_VOICES_ROUTE_TIMEOUT_S",
    "BakeAllResponse",
    "BakeFailureResponse",
    "BakeUsageResponse",
    "DiskUsageResponse",
    "TtsEngineStatusResponse",
    "TtsEnginesResponse",
    "TtsVoiceResponse",
    "TtsVoicesResponse",
    "VoiceDownloadStartResponse",
    "VoiceDownloadStatusResponse",
    "build_tts_router",
]

_LOGGER = logging.getLogger(__name__)

TTS_ENGINES_ROUTE_TIMEOUT_S: Final = 5.0
"""Seconds the whole engines route may take before it reports a timeout.

The engine resolver bounds each probe itself; this outer deadline is what makes
a wedged resolver harmless to the settings page.
"""

TTS_VOICES_ROUTE_TIMEOUT_S: Final = 5.0
"""Seconds the whole voices route's install check may take.

Hashing the installed models runs off the event loop; this bounds the
environment probe that decides whether a download may start.
"""

_SPEECH_NOT_PREPARED: Final = "speech could not be prepared at startup — check the doorbell log"
"""Warning when no speech resolver exists in this process."""

_SPEECH_OFF_ENGINES: Final = (
    "speech is switched off (tts.default_engine is 'none') — enable speech in Settings"
)
"""Warning for the deliberate off state (D27); no engine exists, so the list is empty."""

_SPEECH_OFF_DOWNLOAD: Final = (
    "speech is switched off (tts.default_engine is 'none'); enable speech in Settings before "
    "downloading a voice model"
)
"""The D27 sentence, used as both the warning and the unavailable reason."""

_ENGINES_TIMEOUT: Final = (
    "the speech engine list did not answer within {seconds:g} s — check the doorbell log"
)
"""Warning when the engine status call outlives the route budget."""

_VOICES_NOT_WIRED: Final = (
    "speech voice management is not wired in this process — check the doorbell log"
)
"""Warning when no :class:`~chime_deck.tts.voices.VoiceManager` exists."""

_DOWNLOADS_NOT_WIRED: Final = (
    "speech voice downloads are not wired in this process — check the doorbell log"
)
"""Error message when no download registry exists."""

_GATE_TIMEOUT: Final = (
    "the voice install check did not answer within {seconds:g} s — check the doorbell log"
)
"""Reason used when the install gate outlives the route budget."""

_UNKNOWN_JOB: Final = "no voice download has that id — it may have been forgotten after a restart"
"""Message for a job id the registry does not retain."""

_ERROR_MAP: Final[Mapping[VoiceErrorCode, tuple[int, ErrorCode]]] = MappingProxyType(
    {
        VoiceErrorCode.INVALID_VOICE: (422, ErrorCode.VALIDATION_ERROR),
        VoiceErrorCode.VOICE_NOT_SUGGESTED: (422, ErrorCode.VALIDATION_ERROR),
        VoiceErrorCode.VOICE_UNAVAILABLE: (422, ErrorCode.VOICE_UNAVAILABLE),
        VoiceErrorCode.VOICE_TOO_LARGE: (413, ErrorCode.VOICE_TOO_LARGE),
        VoiceErrorCode.VOICE_CORRUPT: (502, ErrorCode.DOWNLOAD_FAILED),
        VoiceErrorCode.DOWNLOAD_FAILED: (502, ErrorCode.DOWNLOAD_FAILED),
    }
)
"""Status and envelope code for each voice error; unknown codes fail as a download."""

_ALREADY_RUNNING_REASON: Final = "already_running"
"""``detail`` marker for a voice whose download is still in flight."""

_BAKE_SPEECH_OFF: Final = (
    "speech is switched off (tts.default_engine is 'none') — enable speech in Settings "
    "before pre-rendering phrases (D27 has no fallback to walk)"
)
"""The explicit D27 sentence both bake routes answer when speech is off."""


class TtsEngineStatusResponse(BaseModel):
    """One local speech engine, in the shape the settings page lists it."""

    name: str
    available: bool
    reason: str | None = None
    last_synthesis_ms: int | None = None


class TtsEnginesResponse(BaseModel):
    """The engine list plus the one sentence explaining why it may be empty."""

    disabled: bool
    engines: list[TtsEngineStatusResponse]
    warning: str | None = None


class TtsVoiceResponse(BaseModel):
    """One voice model, curated or installed, in the shape the settings page uses.

    ``language``, ``quality``, ``sample_rate_hz`` and ``num_speakers`` are
    nullable because a model installed by hand has no curated metadata: an
    unknown voice is reported honestly rather than given a made-up quality.
    ``size_mb`` is measured in **MiB** and is ``0.0`` when the file could not be
    measured.
    """

    id: str
    language: str | None
    quality: str | None
    size_mb: float
    sample_rate_hz: int | None
    num_speakers: int | None
    licence: str | None
    licence_url: str | None
    installed: bool
    problem: str | None = None


class TtsVoicesResponse(BaseModel):
    """The curated catalogue and what is installed, plus the download readiness.

    ``disabled`` is the D27 off state; ``can_download`` is the D22 memory gate,
    the optional extra and the import state together, and ``unavailable_reason``
    is the whole sentence explaining a refusal. ``memory_warning`` is reported
    separately so the page can show a low-RAM sentence even when a download is
    refused for a different reason.
    """

    disabled: bool
    can_download: bool
    unavailable_reason: str | None = None
    memory_warning: str | None = None
    suggested: list[TtsVoiceResponse]
    installed: list[TtsVoiceResponse]
    warning: str | None = None


class VoiceDownloadStartResponse(BaseModel):
    """The first snapshot of a download job, returned with ``202``."""

    job_id: str
    voice: str
    state: str


class VoiceDownloadStatusResponse(BaseModel):
    """One download job's current progress and, once terminal, its outcome."""

    job_id: str
    voice: str
    state: str
    bytes_done: int
    bytes_total: int | None = None
    error: str | None = None
    detail: dict[str, object] = Field(default_factory=dict)
    started_at: str
    finished_at: str | None = None


class BakeFailureResponse(BaseModel):
    """One button whose phrase could not be pre-rendered, and why.

    ``button`` is the database id; ``error`` is the engine's or the store's own
    whole sentence, so the settings page shows it unchanged.
    """

    button: int
    error: str


class BakeAllResponse(BaseModel):
    """What one ``POST /api/tts/bake_all`` run did, button id by id.

    ``skipped_dynamic`` are phrases that are never baked by design (design 04
    §2.2), ``skipped_current`` are phrases whose artefact was already up to date,
    and ``failed`` is the honest report that keeps one broken button from hiding
    the rest.
    """

    baked: list[int]
    skipped_dynamic: list[int]
    skipped_current: list[int]
    failed: list[BakeFailureResponse]


class DiskUsageResponse(BaseModel):
    """One directory's footprint: how many files it holds and how many bytes."""

    files: int
    bytes: int


class BakeUsageResponse(BaseModel):
    """Disk occupied by the durable bakes and, for comparison, the cache."""

    baked: DiskUsageResponse
    cache: DiskUsageResponse


def _size_mb(size: int | None) -> float:
    """Turn a byte count into MiB, rounded to one decimal.

    :param size: model size in bytes, or ``None`` when it could not be measured.
    """
    if size is None:
        return 0.0
    return round(size / (1024 * 1024), 1)


def _suggested_row(
    voice: SuggestedVoice, installed: Mapping[str, InstalledVoice]
) -> TtsVoiceResponse:
    """Describe one curated catalogue row, joined with what is already on disk.

    :param voice: curated row from :data:`~chime_deck.tts.voices.SUGGESTED_VOICES`.
    :param installed: models found on disk, keyed by voice id.
    """
    on_disk = installed.get(voice.id)
    return TtsVoiceResponse(
        id=voice.id,
        language=voice.language,
        quality=voice.quality,
        size_mb=voice.size_mb,
        sample_rate_hz=voice.sample_rate_hz,
        num_speakers=voice.num_speakers,
        licence=voice.licence,
        licence_url=voice.licence_url,
        installed=on_disk is not None,
        problem=None if on_disk is None else on_disk.problem,
    )


def _installed_row(
    voice: InstalledVoice, curated: Mapping[str, SuggestedVoice]
) -> TtsVoiceResponse:
    """Describe one model on disk, enriched with curated metadata when it is known.

    An unknown, hand-installed voice keeps ``None`` for every curated field and
    is still listed, because it is still loadable by the engine.

    :param voice: model found on disk.
    :param curated: catalogue rows, keyed by voice id.
    """
    known = curated.get(voice.id)
    return TtsVoiceResponse(
        id=voice.id,
        language=None if known is None else known.language,
        quality=None if known is None else known.quality,
        size_mb=_size_mb(voice.bytes),
        sample_rate_hz=None if known is None else known.sample_rate_hz,
        num_speakers=None if known is None else known.num_speakers,
        licence=voice.licence,
        licence_url=None if known is None else known.licence_url,
        installed=True,
        problem=voice.problem,
    )


def _raise_for_voice_error(exc: VoiceError) -> NoReturn:
    """Translate one voice error into the API's status, code and message.

    The engine already built a whole actionable sentence, so the message travels
    unchanged; only the status and the envelope code are chosen here.

    :param exc: error raised by the voice manager or the registry.
    :raises ApiError: always.
    """
    if exc.detail.get("reason") == _ALREADY_RUNNING_REASON:
        raise ApiError(409, ErrorCode.CONFLICT, str(exc), dict(exc.detail)) from None
    status, code = _ERROR_MAP.get(exc.code, (502, ErrorCode.DOWNLOAD_FAILED))
    raise ApiError(status, code, str(exc), dict(exc.detail)) from None


def build_tts_router(state: AppState) -> APIRouter:
    """Build the ``/api/tts`` router bound to *state*.

    Handlers are closures over *state*, following the house style in
    :mod:`chime_deck.app`: no handler annotates ``AppState``, so FastAPI
    never has to resolve a forward reference at import time.

    :param state: live application state providing the speech resolver, the
        voice manager and the download registry.
    """
    router = APIRouter(prefix="/api/tts", tags=["tts"])

    @router.get("/engines")
    async def list_engines() -> TtsEnginesResponse:
        """List the local engines a press can try, in fallback-chain order.

        Never fails: a resolver that was not prepared, the deliberate off state
        (D27) and a status call that outlives the route budget all answer ``200``
        with an empty list and a sentence the settings page can show unchanged.
        """
        speech = state.speech
        if speech is None:
            return TtsEnginesResponse(disabled=False, engines=[], warning=_SPEECH_NOT_PREPARED)
        if speech.is_disabled:
            return TtsEnginesResponse(disabled=True, engines=[], warning=_SPEECH_OFF_ENGINES)
        try:
            rows = await asyncio.wait_for(speech.engine_status(), TTS_ENGINES_ROUTE_TIMEOUT_S)
        except TimeoutError:
            return TtsEnginesResponse(
                disabled=False,
                engines=[],
                warning=_ENGINES_TIMEOUT.format(seconds=TTS_ENGINES_ROUTE_TIMEOUT_S),
            )
        except Exception as exc:
            _LOGGER.warning("listing the speech engines failed", exc_info=True)
            return TtsEnginesResponse(
                disabled=False,
                engines=[],
                warning=clean_detail(str(exc)) or type(exc).__name__,
            )
        return TtsEnginesResponse(
            disabled=False,
            engines=[
                TtsEngineStatusResponse(
                    name=row.name,
                    available=row.availability.ok,
                    reason=None if row.availability.ok else row.availability.reason,
                    last_synthesis_ms=row.last_synthesis_ms,
                )
                for row in rows
            ],
        )

    @router.get("/voices")
    async def list_voices() -> TtsVoicesResponse:
        """List the curated catalogue and every model installed on disk.

        Never fails: an absent manager answers an empty list with a sentence,
        and a hung or broken install gate becomes ``can_download=false`` with
        the reason instead of a ``500``. The models are hashed off the event
        loop, because a synchronous hash of a 60 MB file would stall a press.
        """
        manager = state.voices
        if manager is None:
            return TtsVoicesResponse(
                disabled=False,
                can_download=False,
                suggested=[],
                installed=[],
                warning=_VOICES_NOT_WIRED,
            )
        installed_rows = await asyncio.to_thread(manager.installed)
        try:
            gate = await asyncio.wait_for(manager.gate(), TTS_VOICES_ROUTE_TIMEOUT_S)
        except TimeoutError:
            gate = Availability(False, _GATE_TIMEOUT.format(seconds=TTS_VOICES_ROUTE_TIMEOUT_S))
        except Exception as exc:
            _LOGGER.warning("the voice install check failed", exc_info=True)
            gate = Availability(False, clean_detail(str(exc)) or type(exc).__name__)
        try:
            memory_warning = await manager.memory_warning()
        except Exception:
            _LOGGER.warning("the Piper memory check failed", exc_info=True)
            memory_warning = None
        suggested = manager.suggested()
        curated = {voice.id: voice for voice in suggested}
        on_disk = {voice.id: voice for voice in installed_rows}
        speech = state.speech
        disabled = speech is not None and speech.is_disabled
        return TtsVoicesResponse(
            disabled=disabled,
            can_download=False if disabled else gate.ok,
            unavailable_reason=(
                _SPEECH_OFF_DOWNLOAD if disabled else (None if gate.ok else gate.reason)
            ),
            memory_warning=memory_warning,
            suggested=[_suggested_row(voice, on_disk) for voice in suggested],
            installed=[_installed_row(voice, curated) for voice in installed_rows],
            warning=_SPEECH_OFF_DOWNLOAD if disabled else None,
        )

    @router.post("/voices/{voice_id}/download", status_code=202)
    async def start_download(voice_id: str) -> VoiceDownloadStartResponse:
        """Start installing one curated voice model and return its job id.

        The registry validates the id, asks the same environment gate the engine
        asks, and refuses a second job for a voice that is already downloading.

        :param voice_id: curated voice id from the settings page.
        :raises ApiError: if speech is off, the registry is not wired, or the
            manager refuses the download.
        """
        speech = state.speech
        if speech is not None and speech.is_disabled:
            raise ApiError(422, ErrorCode.VOICE_UNAVAILABLE, _SPEECH_OFF_DOWNLOAD)
        registry = state.voice_downloads
        if registry is None:
            raise ApiError(500, ErrorCode.INTERNAL_ERROR, _DOWNLOADS_NOT_WIRED)
        try:
            job = await asyncio.wait_for(registry.start(voice_id), TTS_VOICES_ROUTE_TIMEOUT_S)
        except TimeoutError:
            raise ApiError(
                422,
                ErrorCode.VOICE_UNAVAILABLE,
                _GATE_TIMEOUT.format(seconds=TTS_VOICES_ROUTE_TIMEOUT_S),
            ) from None
        except VoiceError as exc:
            _raise_for_voice_error(exc)
        return VoiceDownloadStartResponse(
            job_id=job.job_id,
            voice=job.voice,
            state=job.state.value,
        )

    @router.get("/voices/downloads/{job_id}")
    async def download_status(job_id: str) -> VoiceDownloadStatusResponse:
        """Return one download job's latest snapshot.

        :param job_id: id returned by the start route.
        :raises ApiError: if the registry is not wired or the id is unknown.
        """
        registry = state.voice_downloads
        if registry is None:
            raise ApiError(500, ErrorCode.INTERNAL_ERROR, _DOWNLOADS_NOT_WIRED)
        job = registry.status(job_id)
        if job is None:
            raise ApiError(404, ErrorCode.NOT_FOUND, _UNKNOWN_JOB)
        return VoiceDownloadStatusResponse(
            job_id=job.job_id,
            voice=job.voice,
            state=job.state.value,
            bytes_done=job.bytes_done,
            bytes_total=job.bytes_total,
            error=job.error,
            detail=dict(job.detail),
            started_at=job.started_at,
            finished_at=job.finished_at,
        )

    @router.post("/bake_all")
    async def bake_all() -> BakeAllResponse:
        """Pre-render every static phrase that is missing or stale.

        This is the settings page's "pre-render everything now" action, and it
        is **synchronous**: the request answers when the batch does. Its worst
        case is (number of TTS buttons) times (the engine's own per-render bound —
        espeak-ng 10 s, ``remote_tts`` 20 s, ``ha_tts`` its client timeout), so a
        panel shows progress instead of pretending the call is instant.

        One button's failure never fails the batch: it is reported in ``failed``
        with its own sentence, and the remaining buttons are still attempted.
        A dynamic phrase is reported as skipped, because it is unbakeable by
        design (design 04 §2.2).

        :raises ApiError: if speech is not prepared or is switched off (D27);
            in the off state there is no fallback chain to walk.
        """
        speech = state.speech
        if speech is None:
            raise ApiError(503, ErrorCode.ENGINE_UNAVAILABLE, _SPEECH_NOT_PREPARED)
        if speech.is_disabled or await state.store.get_setting("tts.default_engine") == "none":
            raise ApiError(
                422,
                ErrorCode.INVALID_SETTING,
                _BAKE_SPEECH_OFF,
                {"field": "tts.default_engine", "setting": "tts.default_engine"},
            )
        batch = await speech.bake_all(await state.store.list_buttons())
        return BakeAllResponse(
            baked=list(batch.baked),
            skipped_dynamic=list(batch.skipped_dynamic),
            skipped_current=list(batch.skipped_current),
            failed=[
                BakeFailureResponse(button=failure.button_id, error=failure.error)
                for failure in batch.failed
            ],
        )

    @router.get("/bake_usage")
    async def bake_usage() -> BakeUsageResponse:
        """Report how much disk the bakes and the ephemeral cache occupy.

        Read-only and cheap — a few hundred files — so the settings page may poll
        it. Always answers ``200``: with a resolver the figures come from the one
        object that owns both directories, and without one they are measured
        directly, so an install whose speech never started still gets a real
        answer. A directory that does not exist counts as ``0`` files, ``0``
        bytes.
        """
        speech = state.speech
        if speech is not None:
            usage = await speech.bake_usage()
            baked = DiskUsageResponse(files=usage.baked_files, bytes=usage.baked_bytes)
            cache = DiskUsageResponse(files=usage.cache_files, bytes=usage.cache_bytes)
            return BakeUsageResponse(baked=baked, cache=cache)
        baked_files, baked_bytes = directory_usage(baked_root(state.config.data_dir))
        cache_files, cache_bytes = directory_usage(state.config.data_dir / "tts-cache")
        return BakeUsageResponse(
            baked=DiskUsageResponse(files=baked_files, bytes=baked_bytes),
            cache=DiskUsageResponse(files=cache_files, bytes=cache_bytes),
        )

    return router
