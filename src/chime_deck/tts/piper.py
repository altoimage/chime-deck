"""The ``piper`` engine: local neural speech, no network and no subprocess.

This is the optional local synthesiser (design 04 §4, D14). ``piper-tts`` is an
optional extra (``uv sync --extra piper``), so an install that does not want it
pays neither disk nor memory for it, and :class:`PiperProbe` reports the exact
install command when it is missing.

**Seam arithmetic.** One render is bounded by
:data:`~chime_deck.tts.ha_tts.TTS_RENDER_TOTAL_TIMEOUT_S` (7.5 s), imported
from :mod:`chime_deck.tts.ha_tts` so every engine in the package shares one
render budget. A press then costs 1.0 s probe + 0.5 s template + 7.5 s render +
0.5 s publish = 9.5 s against
:data:`~chime_deck.mqtt.bridge.DEFAULT_SEAM_TIMEOUT_S` (10.0 s): 0.5 s of
headroom, which is why neither this number nor the seam may grow.

**Lazy load.** A ``medium`` voice costs roughly 380 MB of **RSS** once loaded
(design 04 §4) — that is the resident cost of the loaded model, not the size of
the model file — and it is loaded on the first press, never at startup, so an
install that never presses pays nothing for it:
:meth:`PiperEngine.warmup` only imports the module, on a background thread, and
the model is read inside the first render's worker.

**D22 is a MemTotal gate.** :meth:`PiperProbe.memory` compares
:data:`DEFAULT_PIPER_MIN_RAM_MB` (or the ``tts.piper.min_ram_mb`` override) with
``MemTotal`` only. ``MemAvailable`` is read and reported, but never checked, so
the verdict does not move as other processes come and go.

**Latch policy, not retry policy.** :data:`MAX_CONSECUTIVE_TIMEOUTS` consecutive
timeouts stop this engine for the rest of the run. A timed-out press fails
outright — it is not retried — and only the *next* press falls through to
another engine, because the latch is checked before anything else. The latch is
permanent for the process: a timed-out worker is stuck in native code this
process cannot reclaim, so the honest answer is "restart the doorbell", not
"try again with a fresh thread". Only Piper's *own* deadline counts towards the
latch: a cancellation from the press-wide deadline (HSD-96k6) discards the
temporary file and propagates, but it is not a Piper timeout and never moves the
counter.

**The import probe is never on the press path.** :meth:`PiperProbe.installed`
only asks :func:`importlib.util.find_spec` for metadata, and the real import runs
once, on a dedicated single-worker thread (``piper-probe``), because a cold
import of ``onnxruntime`` takes seconds and must not block the event loop or
extend a press. A press that arrives mid-import is answered "still loading".

**A missing ``/proc/meminfo`` fails open, with one WARNING.** A container may not
mount ``/proc``; refusing to speak because a check could not run would be worse
than the out-of-memory kill that check guards against.

**Format.** Piper writes 16-bit PCM WAV: ``synthesize_wav`` sets the channel
count, sample width and rate on the file handed to it, which is exactly the
``wav`` container the cache path and libmpv expect.

**Deliberately unmapped options.** ``options.speed`` and ``options.language`` are
ignored here: ``piper-tts`` 1.8.0 exposes ``SynthesisConfig(length_scale=...)``
and a per-voice speaker id, so a future ``tts.piper.speed`` is a one-line
``SynthesisConfig.length_scale`` mapping and a future speaker setting is a
``SynthesisConfig.speaker_id`` mapping — an engine limitation it is not, the
ticket's scope is.

**Verified API** (``piper-tts`` 1.8.0): ``PiperVoice.load(model_path)`` is a
``staticmethod`` returning a ``PiperVoice``, its ``config_path`` defaults to
``model_path + ".json"``, and one render is
``with wave.open(str(tmp), "wb") as wav: voice.synthesize_wav(text, wav)``. The
wheel bundles its own espeak-ng phoneme data, so no system binary and no
``apt install`` is needed: the only host requirements are the wheel and the RAM.

**Two corrections to design 04 §4 (D22).** Its "aarch64 wheels only" is an
erratum — ``piper-tts`` 1.8.0 publishes x86_64 wheels as well — and its advice to
fetch a voice "or the HTTP equivalent" is what
:mod:`chime_deck.tts.voices` does: a plain HTTPS GET from
``huggingface.co/rhasspy/piper-voices`` at the pinned ``v1.0.0`` tag.

Owning ticket: HSD-p50k; :func:`voice_id_problem` was promoted to public by
HSD-5de8 so the renderer validates a caller-supplied voice with the same rule.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import re
import time
import wave
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Final, Protocol, TypeAlias, cast
from uuid import uuid4

from ..store import SETTINGS
from .engines import Availability, TtsOptions, validate_rendered_audio
from .errors import TtsRenderError, clean_detail
from .ha_tts import TTS_RENDER_TOTAL_TIMEOUT_S

if TYPE_CHECKING:
    from .speech import SettingsReader

__all__ = [
    "DEFAULT_PIPER_MIN_RAM_MB",
    "MAX_CONCURRENT_RENDERS",
    "MAX_CONSECUTIVE_TIMEOUTS",
    "MAX_VOICE_ID_CHARS",
    "MEMINFO_PATH",
    "PIPER_EXTRA_REASON",
    "PIPER_IMPORT_TIMEOUT_S",
    "PIPER_MIN_RAM_SETTING",
    "PROBE_THREAD_PREFIX",
    "RENDER_THREAD_PREFIX",
    "VOICE_ID_PATTERN",
    "MemoryVerdict",
    "ModuleFinder",
    "PiperEngine",
    "PiperProbe",
    "PiperVoicePort",
    "read_meminfo",
    "voice_id_problem",
]

_LOGGER = logging.getLogger(__name__)

MEMINFO_PATH: Final = Path("/proc/meminfo")
"""Where Linux reports the machine's memory; read once per process."""

DEFAULT_PIPER_MIN_RAM_MB: Final = 2048
"""MemTotal below which local Piper is refused (D22); the registry default too."""

PIPER_MIN_RAM_SETTING: Final = "tts.piper.min_ram_mb"
"""Registered setting that overrides :data:`DEFAULT_PIPER_MIN_RAM_MB`."""

PIPER_VOICE_SETTING: Final = "tts.piper.voice"
"""Registered setting that names the voice model, used when the engine has none."""

PIPER_EXTRA_REASON: Final = (
    "piper-tts is not installed; run `uv sync --extra piper` and restart the doorbell"
)
"""Availability sentence for an install without the optional extra."""

PIPER_IMPORT_TIMEOUT_S: Final = 5.0
"""Seconds a press waits for the background import before answering "still loading"."""

MAX_CONSECUTIVE_TIMEOUTS: Final = 2
"""Timed-out renders in a row after which this engine latches itself unavailable."""

MAX_CONCURRENT_RENDERS: Final = 2
"""Renders that may be pending at once: one on the worker, one queued behind it."""

VOICE_ID_PATTERN: Final = re.compile(
    r"^[a-z]{2,3}_[A-Za-z]{2,4}-[A-Za-z0-9_]{1,32}-(?:x_low|low|medium|high)$"
)
"""Shape of a Piper voice id, e.g. ``en_GB-alan-low``: it becomes a file name."""

VOICE_ONNX_SUFFIX: Final = ".onnx"
"""Suffix of a voice model inside the voices directory."""

VOICE_CONFIG_SUFFIX: Final = ".onnx.json"
"""Suffix of a voice model's config, which piper derives from the model path."""

MAX_VOICE_ID_CHARS: Final = 64
"""Hard cap on a voice id; the pattern allows 48, the cap keeps the message honest."""

PROBE_THREAD_PREFIX: Final = "piper-probe"
"""Thread name of the one background import, so a shutdown test can find it."""

RENDER_THREAD_PREFIX: Final = "piper-render"
"""Thread name of the one render worker, so a shutdown test can find it."""

_LOADING_REASON: Final = (
    "local piper is still loading — press the button again in a moment; if this keeps happening, "
    "check the doorbell log"
)
"""Answer for a press that arrives while the background import is still running."""

_LATCH_MESSAGE: Final = (
    "local Piper has timed out {count} times in a row and is stopped for this run — check the "
    "doorbell log and restart the service; ha_tts and espeak-ng still work"
)
"""Sentence latched into availability after :data:`MAX_CONSECUTIVE_TIMEOUTS` timeouts."""


class PiperVoicePort(Protocol):
    """The one method the engine calls on a loaded piper voice."""

    def synthesize_wav(self, text: str, wav_file: wave.Wave_write) -> None:
        """Synthesise *text* and write a complete WAV into *wav_file*.

        :param text: phrase to speak.
        :param wav_file: open WAV writer; the voice sets its parameters.
        """
        ...


class PiperVoiceFactory(Protocol):
    """The piper entry point the engine uses to load one voice model."""

    @staticmethod
    def load(model_path: Path) -> PiperVoicePort:
        """Load the voice at *model_path*, reading its ``.onnx.json`` beside it.

        :param model_path: path of the ``.onnx`` model file.
        """
        ...


ModuleImporter: TypeAlias = Callable[[str], ModuleType]
"""Imports one module by name; injectable so a test never imports real piper."""


def _import_module(name: str) -> ModuleType:
    """Import *name*; the default :data:`ModuleImporter` of a probe.

    :param name: module name, always ``"piper"`` in practice.
    """
    return importlib.import_module(name)


ModuleFinder: TypeAlias = Callable[[str], object | None]
"""Finds one module without importing it; injectable so a test controls the answer."""


def _find(name: str) -> object | None:
    """Find *name* using the import finders; the default :data:`ModuleFinder`.

    :param name: module name, always ``"piper"`` in practice.
    """
    return importlib.util.find_spec(name)


def _kilobytes_to_mb(raw: str) -> int | None:
    """Turn one ``/proc/meminfo`` value into whole megabytes.

    :param raw: value as text, in kB (``"2048000"``).
    """
    try:
        kilobytes = int(raw)
    except ValueError:
        return None
    if kilobytes < 0:
        return None
    return kilobytes // 1024


def read_meminfo(path: Path) -> tuple[int | None, int | None]:
    """Return ``(MemTotal_mb, MemAvailable_mb)`` read from a meminfo file.

    Never raises: a missing, unreadable or malformed file answers
    ``(None, None)``, which the caller treats as "cannot check", not as "too
    little memory". Both values are required, because a kernel that reports one
    of them is a kernel whose numbers this engine will not trust.

    :param path: meminfo-style file, normally :data:`MEMINFO_PATH`.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (None, None)
    total: int | None = None
    available: int | None = None
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if not fields:
            continue
        megabytes = _kilobytes_to_mb(fields[0])
        if megabytes is None:
            continue
        if name.strip() == "MemTotal":
            total = megabytes
        elif name.strip() == "MemAvailable":
            available = megabytes
    if total is None or available is None:
        return (None, None)
    return (total, available)


@dataclass(frozen=True, slots=True)
class MemoryVerdict:
    """Whether this machine has enough RAM for local Piper (D22).

    Invariant: :attr:`ok` is ``False`` only when a number was actually read and
    came out below :attr:`threshold_mb`; an unreadable meminfo file leaves
    :attr:`ok` ``True`` with no :attr:`reason`.
    """

    total_mb: int | None
    available_mb: int | None
    threshold_mb: int
    ok: bool
    reason: str | None


class PiperProbe:
    """Answer the three questions local Piper has to pass before it may speak.

    The questions are asked in a fixed order, cheapest and most decisive first:
    is there enough RAM (D22), is the optional extra installed, has the import
    finished, and does the configured voice model exist on disk. Construction is
    inert: nothing is imported, no file is read and no thread is started until a
    caller asks a question. A voice that is not on disk yet can still be asked
    about: :meth:`installable` answers the environment half alone, so a download
    button never has to pretend a model exists to find out whether one could.
    """

    def __init__(
        self,
        settings: SettingsReader,
        *,
        importer: ModuleImporter | None = None,
        finder: ModuleFinder | None = None,
        meminfo_path: Path = MEMINFO_PATH,
    ) -> None:
        """Store the settings port and the test seams; probe nothing yet.

        :param settings: settings reader, normally ``ConfigStore``.
        :param importer: module importer used by :meth:`prime`; injectable so a
            test never imports the real piper.
        :param finder: module finder used by :meth:`installed`; injectable so a
            test never depends on whether this host has the extra.
        :param meminfo_path: meminfo file read once by :meth:`memory`.
        """
        self._settings = settings
        self._importer: ModuleImporter = importer if importer is not None else _import_module
        self._finder: ModuleFinder = finder if finder is not None else _find
        self._meminfo_path = meminfo_path
        self._meminfo: tuple[int | None, int | None] | None = None
        self._installed: bool | None = None
        self._task: asyncio.Task[ModuleType] | None = None
        self._module: ModuleType | None = None
        self._executor: ThreadPoolExecutor | None = None

    async def memory(self) -> MemoryVerdict:
        """Return the D22 memory verdict, re-reading the setting on every call.

        The threshold is read per call so an override applies without a
        restart; ``/proc/meminfo`` is read once per probe and cached, because it
        is static for the process and the press path must not touch the disk.
        An unreadable file is logged once and answers "cannot tell" — available.
        """
        threshold_mb = await self._min_ram_mb()
        total_mb, available_mb = self._meminfo_once()
        if total_mb is None:
            return MemoryVerdict(
                total_mb=None,
                available_mb=None,
                threshold_mb=threshold_mb,
                ok=True,
                reason=None,
            )
        ok = total_mb >= threshold_mb
        return MemoryVerdict(
            total_mb=total_mb,
            available_mb=available_mb,
            threshold_mb=threshold_mb,
            ok=ok,
            reason=None if ok else _ram_reason(total_mb, threshold_mb),
        )

    def installed(self) -> bool:
        """Return whether the ``piper`` module can be found, without importing it.

        :func:`importlib.util.find_spec` only reads the finders' metadata, so
        this costs a directory listing and never loads ``onnxruntime``. The
        answer is memoised: the packages on disk do not change under a running
        process.
        """
        if self._installed is None:
            try:
                self._installed = self._finder("piper") is not None
            except (ImportError, ValueError):
                # A broken or half-installed package must not break a press.
                self._installed = False
        return self._installed

    async def prime(self) -> bool:
        """Import ``piper`` once, off the event loop, and report whether it landed.

        An install without the extra returns ``False`` immediately, before any
        task or thread exists: the common piper-free install must pay neither a
        thread nor a doomed import (D14), and a package that is not on disk
        cannot be imported.

        Single-flight: every call after the first waits on the same task, so the
        importer runs at most once per probe. The import itself runs on a
        dedicated single-worker thread and is shielded from the deadline, so a
        timeout answers ``False`` without cancelling an import that is still
        making progress — a late success is still recorded by :meth:`module`.
        """
        if not self.installed():
            return False
        task = self._task
        if task is None and self._module is None:
            task = asyncio.ensure_future(self._import())
            task.add_done_callback(self._remember)
            self._task = task
        if self._module is not None:
            return True
        if task is None:
            return False
        try:
            await asyncio.wait_for(asyncio.shield(task), PIPER_IMPORT_TIMEOUT_S)
        except TimeoutError:
            _LOGGER.warning(
                "the piper import did not finish within %g s; it continues in the background",
                PIPER_IMPORT_TIMEOUT_S,
            )
            return False
        except Exception as exc:
            _LOGGER.debug("the piper import failed: %s", type(exc).__name__)
            return False
        return task.result() is not None

    def module(self) -> ModuleType | None:
        """Return the imported ``piper`` module, or ``None`` when it is not loaded."""
        return self._module

    def import_reason(self) -> str | None:
        """Return ``None`` when piper is loaded, otherwise why it is not.

        A missing package and a ``ModuleNotFoundError`` both answer
        :data:`PIPER_EXTRA_REASON`, because both are fixed by installing the
        extra. A probe that was never primed answers "still loading" rather than
        waiting: availability must never import, so the only honest answer is
        that the import has not finished. Any other failure names its exception
        type. Nothing is latched here, so a timeout never becomes permanent.
        """
        if self._module is not None:
            return None
        if not self.installed():
            return PIPER_EXTRA_REASON
        task = self._task
        if task is None or not task.done():
            return _LOADING_REASON
        if task.cancelled():
            return _LOADING_REASON
        failure = task.exception()
        if failure is None:
            return _LOADING_REASON
        if isinstance(failure, ModuleNotFoundError):
            return PIPER_EXTRA_REASON
        return (
            f"the piper module could not be imported ({type(failure).__name__}) — check the "
            "doorbell log and restart the service"
        )

    async def voice(self) -> str:
        """Return the configured ``tts.piper.voice``, falling back to its default.

        The registry default is used when the stored value is not a string, so a
        corrupt setting can never become a file name.
        """
        value = await self._settings.get_setting(PIPER_VOICE_SETTING)
        if isinstance(value, str):
            return value
        _LOGGER.warning(
            "setting %r is not a string; using the registry default", PIPER_VOICE_SETTING
        )
        default = SETTINGS[PIPER_VOICE_SETTING].default
        if not isinstance(default, str):
            raise TypeError(f"setting {PIPER_VOICE_SETTING!r} does not have a string default")
        return default

    async def installable(self) -> Availability:
        """Return whether a voice model could be installed on this machine.

        The D22 memory gate, the optional extra and the import state — everything
        `availability` checks except the model files themselves, because the point
        of this question is to answer before a model exists.
        """
        try:
            reason = await self._environment_reason()
        except Exception as exc:
            _LOGGER.warning("the piper install check failed unexpectedly", exc_info=True)
            return Availability(False, _unexpected_reason(exc))
        if reason is None:
            return Availability(True)
        return reason

    async def availability(self, voice: str, voices_dir: Path) -> Availability:
        """Return whether local Piper can speak *voice* right now, never raising.

        Order: the memory verdict, the installed extra, the import state, then
        both files of the voice model. The first failure wins, so the reason a
        user sees is the one they can act on first.

        :param voice: voice id, e.g. ``en_GB-alan-low``.
        :param voices_dir: directory the voice models live in.
        """
        try:
            return await self._availability(voice, voices_dir)
        except Exception as exc:
            _LOGGER.warning("the piper availability check failed unexpectedly", exc_info=True)
            return Availability(False, _unexpected_reason(exc))

    async def _environment_reason(self) -> Availability | None:
        """Return why local Piper cannot run here, or ``None`` when it could.

        The three environment checks shared by :meth:`availability` and
        :meth:`installable`: the D22 memory verdict, the installed extra and the
        import state. Keeping them in one place is what stops a download button
        and an engine disagreeing about the same machine.
        """
        verdict = await self.memory()
        if not verdict.ok:
            return Availability(False, verdict.reason)
        if not self.installed():
            return Availability(False, PIPER_EXTRA_REASON)
        reason = self.import_reason()
        if reason is not None:
            return Availability(False, reason)
        return None

    async def _availability(self, voice: str, voices_dir: Path) -> Availability:
        """Answer one availability question, letting a broken store or disk raise.

        :param voice: voice id.
        :param voices_dir: directory the voice models live in.
        """
        blocked = await self._environment_reason()
        if blocked is not None:
            return blocked
        model = voices_dir / f"{voice}{VOICE_ONNX_SUFFIX}"
        if not model.is_file():
            return Availability(False, _missing_voice_reason(voice, model))
        config = voices_dir / f"{voice}{VOICE_CONFIG_SUFFIX}"
        if not config.is_file():
            return Availability(False, _missing_voice_reason(voice, config))
        return Availability(True)

    def close(self) -> None:
        """Shut the import thread down without waiting for it."""
        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    async def _import(self) -> ModuleType:
        """Run the importer on the dedicated thread, once per probe."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._import_executor(), self._importer, "piper")

    def _import_executor(self) -> ThreadPoolExecutor:
        """Return the lazily created import thread, kept for the probe's lifetime."""
        executor = self._executor
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=PROBE_THREAD_PREFIX)
            self._executor = executor
        return executor

    def _remember(self, task: asyncio.Task[ModuleType]) -> None:
        """Record the module of a finished import, however late it finished.

        :param task: the import task that just completed.
        """
        if task.cancelled():
            return
        if task.exception() is not None:
            return
        self._module = task.result()

    def _meminfo_once(self) -> tuple[int | None, int | None]:
        """Return the cached memory numbers, reading the file at most once."""
        if self._meminfo is None:
            self._meminfo = read_meminfo(self._meminfo_path)
            if self._meminfo[0] is None:
                _LOGGER.warning(
                    "cannot read %s; the local Piper memory check is skipped and piper stays "
                    "available",
                    self._meminfo_path,
                )
        return self._meminfo

    async def _min_ram_mb(self) -> int:
        """Return the D22 threshold, falling back to the registry default."""
        value = await self._settings.get_setting(PIPER_MIN_RAM_SETTING)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        _LOGGER.warning(
            "setting %r is not an integer; using the registry default", PIPER_MIN_RAM_SETTING
        )
        default = SETTINGS[PIPER_MIN_RAM_SETTING].default
        if isinstance(default, int) and not isinstance(default, bool):
            return default
        return DEFAULT_PIPER_MIN_RAM_MB


class PiperEngine:
    """Speak locally with one Piper voice, loaded on the first press.

    Construction is inert: no import, no thread and no model read. The single
    worker thread serialises renders, so two overlapping presses queue instead
    of corrupting the voice, and :data:`MAX_CONCURRENT_RENDERS` keeps the queue
    from growing without bound.
    """

    name: Final = "piper"

    def __init__(
        self,
        probe: PiperProbe,
        *,
        voices_dir: Path,
        render_timeout_s: float = TTS_RENDER_TOTAL_TIMEOUT_S,
        executor_factory: Callable[[], Executor] | None = None,
    ) -> None:
        """Create the engine; nothing is imported and no thread is started.

        :param probe: availability probe shared by the engine and the settings
            page; the engine reads the configured voice through it.
        :param voices_dir: directory holding ``<voice>.onnx`` and its config.
        :param render_timeout_s: seconds one render may take before it is
            abandoned and counted towards the timeout latch.
        :param executor_factory: factory for the single worker; ``None`` builds a
            one-thread :class:`~concurrent.futures.ThreadPoolExecutor`.
        """
        self._probe = probe
        self._voices_dir = voices_dir
        self._render_timeout_s = render_timeout_s
        self._executor_factory = executor_factory
        self._executor: Executor | None = None
        self._voice: PiperVoicePort | None = None
        self._voice_path: Path | None = None
        self._pending = 0
        self._consecutive_timeouts = 0
        self._latched: str | None = None
        self._last_synthesis_ms: int | None = None

    @property
    def last_synthesis_ms(self) -> int | None:
        """Milliseconds the last successful synthesis took, or ``None``.

        Last writer wins over the serialised queue: a queued render that started
        earlier can finish later, so this is the most recent *completion*, not
        the press it belonged to.
        """
        return self._last_synthesis_ms

    async def available(self) -> Availability:
        """Return whether local Piper can speak now, without importing anything.

        A latched engine answers its latch sentence immediately, which is what
        stops the fallback chain paying for a wedged worker on every press.

        This method is not time-bounded by itself: it does a settings read, a
        memoised finder check and two ``stat`` calls. The press resolver's
        availability budget and ``engine_status``'s per-engine deadline are what
        bound it, so nothing slow may ever be added here.
        """
        if self._latched is not None:
            return Availability(False, self._latched)
        try:
            voice = await self._probe.voice()
        except Exception as exc:
            _LOGGER.warning("could not read %r for the piper engine", PIPER_VOICE_SETTING)
            return Availability(
                False,
                f"the local Piper check failed unexpectedly ({type(exc).__name__}) — check the "
                "doorbell log",
            )
        return await self._probe.availability(voice, self._voices_dir)

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Synthesise *text* and publish a 16-bit PCM WAV at *destination*.

        The WAV is written to a hidden sibling and moved into place only after it
        was validated, so a reader never sees a partial file and a failed render
        never removes the destination. A timeout counts towards
        :data:`MAX_CONSECUTIVE_TIMEOUTS`; a success resets the counter.

        :param text: phrase to speak.
        :param options: ``voice`` selects the model; the stored setting is used
            only when it is empty, so a cache key built from these options and
            the voice actually loaded cannot disagree.
        :param destination: exact output path, including its extension.
        :raises TtsRenderError: for blank text, a latched engine, an invalid
            voice, a full queue, a timeout or an inaudible result.
        """
        if not text.strip():
            raise TtsRenderError("piper has no text to speak")
        if self._latched is not None:
            raise TtsRenderError(self._latched)
        voice = options.voice or await self._probe.voice()
        _check_voice(voice)
        if self._pending >= MAX_CONCURRENT_RENDERS:
            raise TtsRenderError(
                f"local Piper is already rendering {self._pending} phrases — press the button "
                "again when the doorbell has finished speaking"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(f".{destination.name}.tmp-{uuid4().hex}")
        model_path = self._voices_dir / f"{voice}{VOICE_ONNX_SUFFIX}"
        self._pending += 1
        try:
            elapsed_ms = await self._render_with_deadline(text, model_path, tmp)
        finally:
            self._pending -= 1
        try:
            validate_rendered_audio(tmp, container="wav", label=self.name)
        except TtsRenderError:
            _discard(tmp)
            raise
        os.replace(tmp, destination)
        self._last_synthesis_ms = elapsed_ms
        _LOGGER.info(
            "piper synthesised %d characters with %s in %d ms", len(text), voice, elapsed_ms
        )
        return destination

    async def warmup(self) -> None:
        """Import piper in the background, without loading the voice.

        Loading a voice that costs ~380 MB of RSS for a ``medium`` model
        (design 04 §4) at startup would put every boot on the press budget and
        make a restart during a chime noticeable, so design 04 §4 keeps the
        model read lazy: only the import is primed here, bounded by
        :data:`PIPER_IMPORT_TIMEOUT_S`, and a failure is logged rather than
        raised because an engine that cannot speak must not stop the doorbell.
        """
        try:
            if not await self._probe.prime():
                _LOGGER.info(
                    "the piper extra is not ready (%s); the ha_tts and espeak-ng engines still "
                    "work",
                    self._probe.import_reason() or "unknown reason",
                )
        except Exception as exc:
            _LOGGER.warning("could not warm up the piper engine: %s", exc)

    def close(self) -> None:
        """Shut the render worker and the probe's import thread down."""
        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        self._probe.close()

    async def _render_with_deadline(self, text: str, model_path: Path, tmp: Path) -> int:
        """Run one synthesis under the render deadline and classify its failures.

        :param text: phrase to speak.
        :param model_path: ``.onnx`` model the voice is loaded from.
        :param tmp: hidden sibling that receives the WAV.
        :returns: measured synthesis time in milliseconds.
        :raises TtsRenderError: on a timeout, a load failure or any other fault.
        :raises asyncio.CancelledError: when the caller is cancelled, for example by
            the press-wide deadline; the temporary file is discarded and the latch
            keeps counting only Piper's own timeouts.
        """
        try:
            elapsed_ms = await asyncio.wait_for(
                self._synthesise(text, model_path, tmp), self._render_timeout_s
            )
        except TimeoutError:
            _discard(tmp)
            self._consecutive_timeouts += 1
            if self._consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                self._latched = _LATCH_MESSAGE.format(count=self._consecutive_timeouts)
            raise TtsRenderError(
                f"local Piper did not finish the speech within {self._render_timeout_s:g} s — "
                "check the doorbell log; ha_tts and espeak-ng still work"
            ) from None
        except TtsRenderError:
            _discard(tmp)
            raise
        except Exception as exc:
            _discard(tmp)
            _LOGGER.warning("the piper render failed unexpectedly", exc_info=True)
            raise TtsRenderError(
                f"local Piper could not render the speech ({type(exc).__name__}) — check the "
                "doorbell log"
            ) from None
        except BaseException:
            # A cancellation from the press-wide deadline (§ HSD-96k6) leaves the
            # worker thread running — it cannot be killed — but the temporary file
            # must go, and the timeout latch must keep counting only Piper's own
            # timeouts, because the press deadline is not one.
            _discard(tmp)
            raise
        self._consecutive_timeouts = 0
        return elapsed_ms

    async def _synthesise(self, text: str, model_path: Path, tmp: Path) -> int:
        """Hand one synthesis to the worker thread.

        :param text: phrase to speak.
        :param model_path: ``.onnx`` model the voice is loaded from.
        :param tmp: hidden sibling that receives the WAV.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._render_executor(), self._synthesise_blocking, text, model_path, tmp
        )

    def _synthesise_blocking(self, text: str, model_path: Path, tmp: Path) -> int:
        """Load the voice if the model changed, write the WAV and time it.

        Runs on the worker thread, so it may block for as long as the model
        takes to load; the deadline is enforced by the awaiting coroutine.

        :param text: phrase to speak.
        :param model_path: ``.onnx`` model the voice is loaded from.
        :param tmp: hidden sibling that receives the WAV.
        :returns: measured synthesis time in milliseconds.
        """
        started = time.monotonic()
        voice = self._loaded_voice(model_path)
        with wave.open(str(tmp), "wb") as wav:
            voice.synthesize_wav(text, wav)
        return round((time.monotonic() - started) * 1000)

    def _loaded_voice(self, model_path: Path) -> PiperVoicePort:
        """Return the loaded voice for *model_path*, loading it when it changed.

        :param model_path: ``.onnx`` model to speak with.
        :raises TtsRenderError: if the module is not loaded or the load failed.
        """
        voice = self._voice
        if voice is not None and self._voice_path == model_path:
            return voice
        module = self._probe.module()
        if module is None:
            raise TtsRenderError(
                "local Piper is not loaded yet — press the button again in a moment; if this "
                "keeps happening, check the doorbell log"
            )
        factory = cast("PiperVoiceFactory", module.PiperVoice)
        loaded = factory.load(model_path)
        self._voice = loaded
        self._voice_path = model_path
        return loaded

    def _render_executor(self) -> Executor:
        """Return the lazily created single render worker, kept for the engine."""
        executor = self._executor
        if executor is None:
            factory = self._executor_factory
            if factory is not None:
                executor = factory()
            else:
                executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix=RENDER_THREAD_PREFIX
                )
            self._executor = executor
        return executor


def voice_id_problem(voice: str) -> str | None:
    """Return the whole sentence rejecting *voice*, or ``None`` when it is usable.

    Public because the renderer role validates a caller-supplied voice with the
    same rule: the two implementations cannot disagree about what names a model
    file if there is only one of them.

    :param voice: voice id from the options, a setting or a render request.
    :returns: one actionable sentence naming the fix, or ``None`` if the id is
        a plain model name that cannot leave ``voices_dir``.
    """
    hostile = "/" in voice or "\\" in voice or ".." in voice or "\x00" in voice
    if hostile or len(voice) > MAX_VOICE_ID_CHARS or VOICE_ID_PATTERN.fullmatch(voice) is None:
        return (
            f"the local Piper voice {clean_detail(voice)!r} is not a valid voice id — choose one "
            f"from Settings → Speech ({PIPER_VOICE_SETTING})"
        )
    return None


def _check_voice(voice: str) -> None:
    """Reject a voice id that is not a plain model name.

    :param voice: voice id from the options or the setting.
    :raises TtsRenderError: if the id could name a file outside ``voices_dir``.
    """
    problem = voice_id_problem(voice)
    if problem is not None:
        raise TtsRenderError(problem)


def _unexpected_reason(exc: BaseException) -> str:
    """Build the sentence for a probe check that raised unexpectedly.

    :param exc: whatever the check raised; only its type is named.
    """
    return (
        f"the local Piper check failed unexpectedly ({type(exc).__name__}) — check the doorbell log"
    )


def _ram_reason(total_mb: int, threshold_mb: int) -> str:
    """Build the D22 sentence for a machine with too little RAM.

    :param total_mb: ``MemTotal`` the device reports.
    :param threshold_mb: threshold the check applied.
    """
    return (
        f"local Piper needs about {threshold_mb} MB of RAM and this device reports {total_mb} MB "
        f"in total — set {PIPER_MIN_RAM_SETTING} in Settings if you know better, or use ha_tts or "
        "espeak-ng with a low or x_low voice instead"
    )


def _missing_voice_reason(voice: str, path: Path) -> str:
    """Build the sentence for a voice model that is not on disk.

    :param voice: voice id the press asked for.
    :param path: the file that was not found.
    """
    return (
        f"the Piper voice {clean_detail(voice)!r} is not installed — {path} does not exist; "
        "download it from Settings → Speech, or choose another voice with "
        f"{PIPER_VOICE_SETTING}"
    )


def _discard(path: Path) -> None:
    """Remove a temporary file, tolerating one that was never written.

    :param path: temporary file to remove.
    """
    with suppress(FileNotFoundError):
        path.unlink()
