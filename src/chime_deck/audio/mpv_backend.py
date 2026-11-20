"""libmpv audio backend: one worker thread, end-file completion, typed errors.

``MpvBackend`` is the default :class:`~chime_deck.audio.backend.AudioBackend`.
Every libmpv call — construction, event registration, device reads and writes,
play, stop, terminate — runs on one private daemon thread, because libmpv is not
safe to drive from several threads and must never block the event loop. The
asyncio side submits a callable, awaits its future under a bound, and maps every
failure to a typed error, so no raw ``ctypes``/``SystemError`` text can escape.

``play()`` returns when mpv reports the end-file event for the file it started.
Two mpv options would silently suppress that event (``keep_open`` and the loop
options), so the base option set pins ``keep_open=False`` and the constructor
refuses those keys: otherwise every chime would wait out the whole play timeout.

A sleeping output must not eat the start of a chime. HDMI and SPDIF sinks power
down when they are idle and take about a second to become audible again, so the
first chime after a quiet spell loses its opening syllable. The purpose-built
option for this, ``--audio-wait-open``, is dead: since mpv 0.33.0 its only
consumer (``ao_resume_time``, written in ``player/audio.c``) is never read, and
a 1 s tone with the option at 5 s and at 0 both finish in 0.6 s — mpv does not
wait. ``play()`` therefore prepends real silence through mpv's own filter chain
(``adelay``), which keeps the silence and the content inside ONE audio output
session; a separate silent file would close and reopen the device and put the
sink straight back to sleep. The caller passes ``lead_in_ms`` (only it knows
whether the speaker has been idle) and ``play()`` returns what it actually
applied.

``MpvPlayer``, ``MpvEvent`` and ``MpvEventData`` are public on purpose: they are
the documented seam a test (or another caller) injects a fake through. ``mpv``
itself is imported lazily, at first ``start()``, never at module import time, so
this module is importable on a host without libmpv.

Owning ticket: HSD-h13q.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import CancelledError, Future, InvalidStateError
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol, TypeVar, cast

from .devices import SYSTEM_DEFAULT_DEVICE, AudioDevice, hostapi_of, resolve_device
from .errors import (
    AudioBackendNotStarted,
    AudioBackendUnavailable,
    AudioError,
    AudioOperationError,
    AudioPermissionError,
    MissingAudioFileError,
    NoAudioDeviceError,
    PlaybackError,
    PlaybackInProgressError,
    UnsupportedFormatError,
)

__all__ = [
    "DEFAULT_START_TIMEOUT_S",
    "MpvBackend",
    "MpvEvent",
    "MpvEventData",
    "MpvEventHandler",
    "MpvPlayer",
    "warm_library",
]

_LOGGER = logging.getLogger(__name__)

DEFAULT_START_TIMEOUT_S: Final = 25.0
"""Default budget for one start, in seconds.

Starting a player is not a command: it dlopens libmpv and its shared-object
graph and then initialises the mpv core. The reference panel (4 cores / 967 MB,
also running Chromium) measured 5.1 s for this on a clean boot and blew a 5 s
budget whenever anything else was loading the same cores, so the default is
five times the measured cost. A value that small is what made a transient
failure permanent; the bound still exists, and the pipeline's recovery ladder
(``chime_deck.wiring``) bounds the total.
"""

_STOP_TIMEOUT_S: Final = 2.0
"""Seconds allowed for the shutdown job that stops and terminates mpv."""

_DEFAULT_PLAY_TIMEOUT_S: Final = 300.0
"""Lost end-file backstop; long enough for a 500-character TTS phrase (design 04)."""

_DEFAULT_COMMAND_TIMEOUT_S: Final = 2.0
"""Seconds allowed for one individual libmpv command."""

_MAX_LEAD_IN_MS: Final = 10_000
"""Longest lead-in accepted; more would stop being a doorbell."""

_BASE_MPV_OPTIONS: Final[Mapping[str, object]] = {
    "audio_display": "no",
    "video": "no",
    "keep_open": False,
    "terminal": False,
    "input_terminal": False,
    "config": False,
}
"""Options every player gets; ``keep_open=False`` is load-bearing, see the module docstring."""

_FORBIDDEN_MPV_OPTIONS: Final = frozenset(
    {
        # Every spelling that stops the end-file event from ever arriving.
        "keep_open",
        "keep-open",
        "loop",
        "loop_file",
        "loop-file",
        "loop_list",
        "loop-list",
        "loop_playlist",
        "loop-playlist",
        "pause",
        "paused",
        "image_display_duration",
        "image-display-duration",
    }
)
"""Caller-supplied options that would silence the end-file event; refused in ``__init__``."""

_END_FILE_EOF: Final = 0
_END_FILE_RESTARTED: Final = 1
_END_FILE_ABORTED: Final = 2
_END_FILE_QUIT: Final = 3
_END_FILE_ERROR: Final = 4
_END_FILE_REDIRECT: Final = 5

_MPV_ERROR_NAMES: Final[dict[int, str]] = {
    -1: "event queue full",
    -13: "loading failed",
    -14: "audio output init failed",
    -16: "nothing to play",
    -17: "unknown format",
    -18: "unsupported",
    -20: "generic error",
}

_T = TypeVar("_T")

MpvEventHandler = Callable[["MpvEvent"], None]
"""Signature python-mpv calls on its event thread."""


def _mpv_error_name(code: int) -> str:
    """Describe an mpv error code in words.

    :param code: numeric mpv/libmpv error code.
    :return: a short description, or ``unknown error`` for a code outside the table.
    """
    return _MPV_ERROR_NAMES.get(code, "unknown error")


class MpvEventData(Protocol):
    """The part of python-mpv's event payload this backend reads."""

    @property
    def playlist_entry_id(self) -> int:
        """Playlist entry the event belongs to; ``0`` when there is none."""
        ...

    @property
    def reason(self) -> int:
        """End-file reason; only present on ``end-file`` events."""
        ...

    @property
    def error(self) -> int:
        """mpv error code; only meaningful when ``reason`` is ``4`` (error)."""
        ...


class MpvEvent(Protocol):
    """The part of a python-mpv event this backend reads."""

    @property
    def data(self) -> MpvEventData | None:
        """Event payload; ``None`` for events that carry no data."""
        ...


class MpvPlayer(Protocol):
    """The subset of ``mpv.MPV`` this backend drives, on the worker thread only."""

    @property
    def audio_device_list(self) -> Sequence[Mapping[str, object]]:
        """Audio outputs mpv can see, each with a ``name`` and a ``description``."""
        ...

    @property
    def audio_device(self) -> str:
        """Selected output; the value resolved from a device hint."""
        ...

    @audio_device.setter
    def audio_device(self, name: str) -> None:
        """Select an output by mpv device name."""

    @property
    def volume(self) -> float:
        """Volume as mpv's percentage, 0-100."""
        ...

    @volume.setter
    def volume(self, level: float) -> None:
        """Set the volume percentage."""

    @property
    def af(self) -> str:
        """The audio filter chain, ``""`` when there is none."""
        ...

    @af.setter
    def af(self, graph: str) -> None:
        """Replace the audio filter chain, e.g. ``"lavfi=[adelay=delays=1500:all=1]"``."""

    def event_callback(self, *event_types: str) -> Callable[[MpvEventHandler], MpvEventHandler]:
        """Return a decorator registering a handler for exactly *event_types*."""
        ...

    def play(self, filename: str) -> None:
        """Start playing *filename*; failures arrive as an end-file error, not here."""
        ...

    def stop(self) -> None:
        """Stop the current playback."""
        ...

    def terminate(self) -> None:
        """Destroy the player; must be called exactly once."""
        ...


def _noop() -> None:
    """Do nothing; used when an event handler offers no unregister hook."""


def _clamp_lead_in(ms: int) -> int:
    """Keep a requested wake lead-in inside the range mpv and a doorbell can bear.

    :param ms: milliseconds of leading silence the caller asked for.
    :return: *ms* unchanged when it is within ``0.._MAX_LEAD_IN_MS``, ``0`` when
        it is negative, or :data:`_MAX_LEAD_IN_MS` when it is longer.
    """
    if ms < 0:
        _LOGGER.warning("a negative wake lead-in (%d ms) was requested; playing without one", ms)
        return 0
    if ms > _MAX_LEAD_IN_MS:
        _LOGGER.warning(
            "wake lead-in %d ms is longer than the %d ms maximum; using %d ms",
            ms,
            _MAX_LEAD_IN_MS,
            _MAX_LEAD_IN_MS,
        )
        return _MAX_LEAD_IN_MS
    return ms


def _lead_in_filter(ms: int) -> str:
    """Build the lavfi filter graph that prepends *ms* milliseconds of silence.

    ``all=1`` delays every channel, so a stereo chime wakes both halves of the
    output instead of only the first.

    :param ms: milliseconds of silence; already clamped to ``0.._MAX_LEAD_IN_MS``.
    :return: the mpv ``af`` graph, e.g. ``"lavfi=[adelay=delays=1500:all=1]"``.
    """
    return f"lavfi=[adelay=delays={ms}:all=1]"


def _load_mpv_module() -> ModuleType:
    """Import python-mpv lazily, mapping a missing library to our own error.

    :raises AudioBackendUnavailable: if python-mpv is not installed or libmpv is
        missing (python-mpv raises :class:`OSError` for the latter).
    :return: the imported ``mpv`` module.
    """
    try:
        return importlib.import_module("mpv")
    except (ImportError, OSError) as exc:
        raise AudioBackendUnavailable(
            "python-mpv/libmpv is not available on this host — install the `libmpv2` "
            "package (e.g. `apt install libmpv2`) and restart"
        ) from exc


def _default_player_factory(options: Mapping[str, object]) -> MpvPlayer:
    """Construct the real ``mpv.MPV`` player.

    :param options: merged mpv options; every key is a constructor keyword.
    :raises AudioBackendUnavailable: if python-mpv or libmpv is missing.
    :return: a new player, owned by the calling (worker) thread.
    """
    module = _load_mpv_module()
    factory = cast("Callable[..., MpvPlayer]", module.MPV)
    return factory(**dict(options))


def warm_library() -> None:
    """Import python-mpv so a later start does not pay the cold dlopen.

    ``import mpv`` is where libmpv and its whole shared-object graph are mapped,
    which on slow storage is the bulk of a first start. Calling this early lets
    that I/O overlap the rest of startup instead of running inside the start
    budget.

    Total by construction: a host without libmpv, or without python-mpv, keeps
    the actionable failure for :meth:`MpvBackend.start`, which is where the
    user-visible error belongs.
    """
    try:
        _load_mpv_module()
    except AudioBackendUnavailable as exc:
        _LOGGER.debug("python-mpv/libmpv is not available yet: %s", exc)


@dataclass(frozen=True, slots=True)
class _Outcome:
    """How one playback ended, as reported by the loop-thread event handlers."""

    reason: int
    error: int


@dataclass(slots=True)
class _PendingPlayback:
    """One file being played: what was asked for and the future the end-file resolves.

    ``entry_id`` is the playlist entry the current player assigned to this file,
    learned from the ``start-file`` event; it is what tells a real end-file apart
    from a late one belonging to a file that already finished.
    """

    path: Path
    done: asyncio.Future[_Outcome]
    entry_id: int | None = None


class _Worker:
    """One daemon thread that runs every libmpv call, in submission order.

    A thread of our own rather than a :class:`~concurrent.futures.ThreadPoolExecutor`
    because executor threads are joined at interpreter exit: one wedged libmpv call
    would hang pytest or systemd shutdown. This thread is a daemon and is never
    joined, so a wedged call can only leak the thread.
    """

    def __init__(self) -> None:
        """Start the thread; it lives until :meth:`close` is called."""
        self._queue: queue.Queue[tuple[Callable[[], object], Future[object]] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="mpv-audio", daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[], object]) -> Future[object]:
        """Queue *fn* for the worker thread.

        :param fn: callable that touches libmpv and must not run on the event loop.
        :return: future resolved or failed with whatever *fn* produced.
        """
        future: Future[object] = Future()
        self._queue.put((fn, future))
        return future

    def close(self) -> None:
        """Ask the thread to exit once the already queued jobs are done."""
        self._queue.put(None)

    def _run(self) -> None:
        """Consume jobs until the sentinel arrives; one failing job never kills the thread."""
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, future = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn()
            except Exception as exc:
                _LOGGER.debug("an audio worker job failed", exc_info=True)
                with suppress(CancelledError, InvalidStateError):
                    future.set_exception(exc)
                continue
            with suppress(CancelledError, InvalidStateError):
                future.set_result(result)


def _shutdown_player(player: MpvPlayer, unregisters: tuple[Callable[[], None], ...]) -> None:
    """Detach every handler, stop playback and terminate mpv exactly once.

    Runs on the worker thread. A second ``terminate()`` would pass NULL to libmpv,
    so this is the only place that calls it.

    :param player: the player to release.
    :param unregisters: unregister callables python-mpv returned for our handlers.
    """
    for unregister in unregisters:
        try:
            unregister()
        except Exception:
            _LOGGER.debug("unregistering an mpv event handler failed", exc_info=True)
    try:
        player.stop()
    except Exception:
        _LOGGER.debug("stopping mpv before terminating it failed", exc_info=True)
    player.terminate()


def _mark_default(devices: list[AudioDevice]) -> list[AudioDevice]:
    """Flag exactly one device as the default.

    :param devices: devices in the backend's order.
    :return: the same devices with ``is_default`` set on the ``auto`` sentinel when
        present, otherwise on the first entry.
    """
    if not devices:
        return []
    default_name = devices[0].name
    for device in devices:
        if device.name == SYSTEM_DEFAULT_DEVICE:
            default_name = device.name
            break
    return [
        AudioDevice(
            name=device.name,
            hostapi=device.hostapi,
            is_default=device.name == default_name,
        )
        for device in devices
    ]


def _event_int(event: MpvEvent, name: str) -> int | None:
    """Read an integer field from an mpv event without ever raising.

    python-mpv delivers a different class per event type, and a ``start-file``
    event has no ``reason`` at all; attribute access there raises on python-mpv's
    event thread, which stalls its event pump. Every field is therefore read with
    ``getattr`` and a default.

    :param event: event delivered by python-mpv.
    :param name: field to read from ``event.data``.
    :return: the value when it is an integer, otherwise ``None``.
    """
    data = event.data
    if data is None:
        return None
    value = getattr(data, name, None)
    return value if isinstance(value, int) else None


def _playback_error(path: Path, code: int) -> PlaybackError:
    """Map an mpv end-file error code to the right typed error.

    :param path: file that failed to play.
    :param code: numeric mpv error code from the end-file event.
    :return: the exception the caller should raise.
    """
    name = _mpv_error_name(code)
    if code in (-17, -18):
        return UnsupportedFormatError(path, name)
    if code == -14:
        return AudioPermissionError(
            f"cannot play {path}: the audio output could not be opened "
            f"(mpv error {code}, {name}) — the device may be busy, or the service user "
            "is not in the `audio` group"
        )
    return PlaybackError(f"playback of {path} failed: mpv error {code} ({name})", code=code)


class MpvBackend:
    """Play files through libmpv on a dedicated worker thread.

    Concurrency contract: an instance is bound to the event loop it was started
    on. Its state (``_pending``, ``_playing``, ``_last_resolved_entry_id``) is
    touched only from that loop thread, except for the two event handlers, which
    parse the event on python-mpv's thread and hop over with
    ``call_soon_threadsafe``.

    The claim in :meth:`play` (started check, overlap check, pending install,
    ``playing = True``) and the state flip in :meth:`stop` are each a run of
    statements with no ``await`` in between. Because only one coroutine runs at a
    time, a concurrent ``play()`` either claims first — and is then resolved as
    stopped — or sees ``_stopping`` and raises :class:`AudioBackendNotStarted`;
    it never waits out a deadline. No lock is needed for those critical sections.
    """

    def __init__(
        self,
        *,
        device_hint: str = "",
        player_factory: Callable[[Mapping[str, object]], MpvPlayer] | None = None,
        mpv_options: Mapping[str, object] | None = None,
        play_timeout: float = _DEFAULT_PLAY_TIMEOUT_S,
        command_timeout: float = _DEFAULT_COMMAND_TIMEOUT_S,
    ) -> None:
        """Configure the backend; nothing is imported or constructed yet.

        :param device_hint: name substring from ``audio.device_hint``; blank means
            "use the system default".
        :param player_factory: constructor seam; defaults to the real ``mpv.MPV``,
            imported lazily on :meth:`start`.
        :param mpv_options: extra mpv options, merged over the safe defaults.
        :param play_timeout: backstop for a lost end-file event, in seconds.
        :param command_timeout: bound for one individual libmpv command, in seconds.
        :raises ValueError: if *mpv_options* sets an option that would stop the
            end-file event from arriving.
        """
        forbidden = sorted(key for key in (mpv_options or {}) if key in _FORBIDDEN_MPV_OPTIONS)
        if forbidden:
            rendered = ", ".join(repr(key) for key in forbidden)
            raise ValueError(
                f"mpv_options must not set {rendered}: keep-open, loop and pause stop mpv "
                "from ever sending the end-file event that completes a playback"
            )
        self._device_hint = device_hint.strip()
        self._player_factory = player_factory or _default_player_factory
        self._options: Mapping[str, object] = dict(_BASE_MPV_OPTIONS) | dict(mpv_options or {})
        self._play_timeout = play_timeout
        self._command_timeout = command_timeout
        self._lock = asyncio.Lock()
        self._worker: _Worker | None = None
        self._player: MpvPlayer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unregister_events: tuple[Callable[[], None], ...] = ()
        self._pending: _PendingPlayback | None = None
        self._last_resolved_entry_id: int | None = None
        self._playing = False
        self._stopping = False

    @property
    def playing(self) -> bool:
        """Whether a file is currently playing."""
        return self._playing

    async def start(self, *, timeout_s: float | None = None) -> None:
        """Create the worker thread and the mpv player, then apply the device hint.

        Idempotent: a second call while started does nothing, and a call after
        :meth:`stop` builds a fresh player.

        :param timeout_s: budget for constructing the player, in seconds;
            ``None`` uses :data:`DEFAULT_START_TIMEOUT_S`. Registering the event
            handlers keeps its own, much smaller, command bound.
        :raises AudioBackendUnavailable: if python-mpv/libmpv is missing or the
            player could not be constructed.
        """
        bound = DEFAULT_START_TIMEOUT_S if timeout_s is None else timeout_s
        async with self._lock:
            if self._player is not None:
                return
            worker = _Worker()
            self._worker = worker
            player: MpvPlayer | None = None
            try:
                player = await self._await_worker(
                    worker,
                    "start",
                    self._create_player,
                    timeout=bound,
                )
                unregisters = await self._await_worker(
                    worker,
                    "register mpv event handlers",
                    lambda: self._register_event_handlers(player),
                    timeout=self._command_timeout,
                )
            except AudioError as exc:
                # A half-built player must be terminated on the worker, never dropped
                # for python-mpv's __del__ to terminate on the event-loop thread.
                await self._discard_player(worker, player)
                self._player = None
                self._worker = None
                self._unregister_events = ()
                worker.close()
                if isinstance(exc, AudioBackendUnavailable):
                    raise
                raise AudioBackendUnavailable(
                    f"the audio backend could not be started: {exc}"
                ) from exc
            self._player = player
            self._unregister_events = unregisters
            self._loop = asyncio.get_running_loop()
            # A fresh player numbers its playlist entries from 1 again.
            self._last_resolved_entry_id = None
        await self._apply_device_hint()

    async def _discard_player(self, worker: _Worker, player: MpvPlayer | None) -> None:
        """Terminate a partially built player on the worker thread.

        Called when :meth:`start` fails after construction: dropping the reference
        on the loop thread would let python-mpv's ``__del__`` run the blocking
        ``terminate()`` there, which is exactly what the single-worker rule forbids.

        :param worker: worker thread that owns the player.
        :param player: player to release, or ``None`` when construction itself failed.
        """
        if player is None:
            return
        try:
            await self._await_worker(
                worker,
                "discard player",
                lambda: _shutdown_player(player, ()),
                timeout=_STOP_TIMEOUT_S,
            )
        except AudioError:
            _LOGGER.debug("discarding a half-built mpv player failed", exc_info=True)

    async def stop(self) -> None:
        """Stop playback, unregister events, terminate mpv and end the worker thread.

        A no-op when the backend was never started, and idempotent. An in-flight
        :meth:`play` returns normally; :meth:`start` may run again afterwards.
        """
        async with self._lock:
            if self._player is None:
                return
            self._stopping = True
            pending = self._pending
            self._pending = None
            if pending is not None and not pending.done.done():
                self._note_resolved(pending.entry_id)
                pending.done.set_result(_Outcome(reason=_END_FILE_QUIT, error=0))
            player = self._player
            worker = self._worker
            unregisters = self._unregister_events
            self._player = None
            self._worker = None
            self._unregister_events = ()
            self._loop = None
        # `player` stays referenced by this frame and by the job closure until the worker
        # has terminated it: python-mpv's __del__ calls the blocking terminate(), which
        # must never run on the event-loop thread.
        if worker is None:
            self._stopping = False
            self._playing = False
            return
        try:
            await self._await_worker(
                worker,
                "stop",
                lambda: _shutdown_player(player, unregisters),
                timeout=_STOP_TIMEOUT_S,
            )
        except AudioError:
            _LOGGER.warning("stopping the audio backend did not complete cleanly", exc_info=True)
        finally:
            worker.close()
            self._stopping = False
            self._playing = False
            self._pending = None

    async def play(
        self,
        path: Path,
        *,
        volume: int,
        lead_in_ms: int = 0,
        when_done: Callable[[], None] | None = None,
    ) -> int:
        """Play one file and return when mpv reports it has finished.

        ``lead_in_ms`` is prepended as silence through mpv's ``af`` filter chain
        (see the module docstring), so a sleeping HDMI/SPDIF output wakes on
        silence rather than on the first syllable. A lead-in that cannot be set
        is logged and skipped: a chime must never fail because of one.

        :param path: sound file; must exist and be readable.
        :param volume: volume 0-100; values outside the range are clamped and logged.
        :param lead_in_ms: milliseconds of leading silence, decided by the caller
            because only it knows whether the speaker has been idle; ``0`` asks
            for none.
        :param when_done: optional callback run once, on this loop thread, whether
            playback succeeded or failed; its exceptions are logged, not raised.
        :returns: the milliseconds of leading silence actually applied, ``0`` when
            none was asked for or the graph could not be set.
        :raises AudioBackendNotStarted: if the backend is not started.
        :raises PlaybackInProgressError: if another file is still playing.
        :raises MissingAudioFileError: if *path* is not a file.
        :raises AudioPermissionError: if *path* cannot be read.
        :raises UnsupportedFormatError: if mpv cannot decode *path*.
        :raises PlaybackError: if mpv reports any other playback error.
        :raises AudioOperationError: if mpv never reports the end of the file, or a
            command could not be issued.
        """
        player = self._require_player()
        if self._pending is not None:
            raise PlaybackInProgressError(f"another file is still playing: {self._pending.path}")
        if not path.is_file():
            raise MissingAudioFileError(path)
        if not os.access(path, os.R_OK):
            raise AudioPermissionError(
                f"cannot read {path}: permission denied — check the file's permissions"
            )
        level = max(0, min(100, volume))
        if level != volume:
            _LOGGER.warning("volume %d is outside 0-100; playing at %d", volume, level)
        pending = _PendingPlayback(path=path, done=asyncio.get_running_loop().create_future())
        applied_ms = 0
        self._pending = pending
        self._playing = True
        try:
            await self._command("set volume", lambda: setattr(player, "volume", float(level)))
            if lead_in_ms > 0:
                clamped = _clamp_lead_in(lead_in_ms)
                graph = _lead_in_filter(clamped)
                try:
                    await self._command("set audio filter", lambda: setattr(player, "af", graph))
                except AudioError as exc:
                    _LOGGER.warning(
                        "the wake lead-in could not be set (%s); the chime plays without it", exc
                    )
                else:
                    applied_ms = clamped
            await self._command("play", lambda: player.play(str(path)))
            try:
                async with asyncio.timeout(self._play_timeout):
                    outcome = await pending.done
            except TimeoutError as exc:
                raise AudioOperationError(
                    "play",
                    f"no end-file event within {self._play_timeout:g} s for {path}",
                ) from exc
            if outcome.reason == _END_FILE_ERROR:
                raise _playback_error(path, outcome.error)
        except (AudioError, asyncio.CancelledError):
            await self._quiet_stop(player)
            raise
        finally:
            self._pending = None
            self._playing = False
            if applied_ms:
                await self._clear_lead_in(player)
            if when_done is not None:
                try:
                    when_done()
                except Exception:
                    _LOGGER.warning("when_done callback raised", exc_info=True)
        return applied_ms

    async def _clear_lead_in(self, player: MpvPlayer) -> None:
        """Remove the wake lead-in graph, ignoring failure and cancellation.

        Modelled on :meth:`_quiet_stop` because this runs from the ``finally`` of
        :meth:`play`: an ``await`` there must never turn an interrupt into an
        error, and the next play must find an untouched filter chain.

        :param player: player the graph was set on, on the worker thread when one
            is still there.
        """
        try:
            await self._command("clear audio filter", lambda: setattr(player, "af", ""))
        except asyncio.CancelledError:
            _LOGGER.debug("cancelled while clearing the wake lead-in")
        except AudioError:
            _LOGGER.debug("clearing the wake lead-in did not succeed", exc_info=True)

    async def stop_playback(self) -> None:
        """Stop the current file and stay started and reusable.

        A no-op when nothing is playing; the interrupted :meth:`play` returns
        normally.

        :raises AudioBackendNotStarted: if the backend is not started.
        :raises AudioOperationError: if mpv could not be asked to stop.
        """
        player = self._require_player()
        await self._command("stop playback", player.stop)
        pending = self._pending
        if pending is not None and not pending.done.done():
            self._note_resolved(pending.entry_id)
            pending.done.set_result(_Outcome(reason=_END_FILE_ABORTED, error=0))

    async def set_device(self, hint: str) -> None:
        """Resolve *hint* against the device list and select the result.

        This is the strict, settings-page path: an empty device list is an error
        worth surfacing, while a blank or unmatched hint logs a warning and falls
        back to the system default.

        :param hint: name substring from ``audio.device_hint``.
        :raises AudioBackendNotStarted: if the backend is not started.
        :raises NoAudioDeviceError: if the host reports no audio output at all.
        :raises AudioOperationError: if the device list could not be read or applied.
        """
        devices = await self.list_devices()
        self._device_hint = hint.strip()
        if not devices:
            raise NoAudioDeviceError(
                "no audio output device was found — check that a sound card is present and "
                "that the service user can reach the sound server"
            )
        device = resolve_device(devices, self._device_hint)
        if device is None:
            if self._device_hint == "":
                _LOGGER.warning(
                    "no audio.device_hint is set; using the system default audio device"
                )
            else:
                _LOGGER.warning(
                    "no audio device name matches %r; using the system default audio device",
                    self._device_hint,
                )
            await self._write_device(SYSTEM_DEFAULT_DEVICE)
            return
        await self._write_device(device.name)

    async def list_devices(self) -> list[AudioDevice]:
        """Return the audio outputs mpv can see, exactly one flagged as default.

        :raises AudioBackendNotStarted: if the backend is not started.
        :raises AudioOperationError: if the device list could not be read.
        :return: devices in mpv's order.
        """
        player = self._require_player()
        entries = await self._command("list audio devices", lambda: player.audio_device_list)
        devices: list[AudioDevice] = []
        for entry in entries:
            name = entry.get("name")
            if not isinstance(name, str) or name.strip() == "":
                continue
            devices.append(AudioDevice(name=name, hostapi=hostapi_of(name)))
        return _mark_default(devices)

    def _require_player(self) -> MpvPlayer:
        """Return the started player.

        :raises AudioBackendNotStarted: if the backend is not started or is stopping.
        :return: the player created by :meth:`start`.
        """
        if self._player is None or self._stopping:
            raise AudioBackendNotStarted("the audio backend is not started; call start() first")
        return self._player

    def _require_worker(self) -> _Worker:
        """Return the worker thread.

        :raises AudioBackendNotStarted: if the backend is not started or is stopping.
        :return: the worker created by :meth:`start`.
        """
        if self._worker is None or self._stopping:
            raise AudioBackendNotStarted("the audio backend is not started; call start() first")
        return self._worker

    async def _await_worker(
        self,
        worker: _Worker,
        operation: str,
        fn: Callable[[], _T],
        *,
        timeout: float,
    ) -> _T:
        """Run *fn* on *worker* and await its result under a bound.

        :param worker: worker thread that owns libmpv.
        :param operation: short name used in the error text.
        :param fn: callable touching libmpv; never runs on the event loop.
        :param timeout: seconds to wait before giving up.
        :raises AudioOperationError: if *fn* raised or did not finish in time.
        :return: whatever *fn* returned.
        """
        future = worker.submit(fn)
        try:
            async with asyncio.timeout(timeout):
                result = await asyncio.wrap_future(future)
        except TimeoutError as exc:
            raise AudioOperationError(operation, f"timed out after {timeout:g} s") from exc
        except AudioError:
            raise
        except Exception as exc:
            raise AudioOperationError(operation, f"{type(exc).__name__}: {exc}") from exc
        return cast(_T, result)

    async def _command(self, operation: str, fn: Callable[[], _T]) -> _T:
        """Run one bounded libmpv command on the worker thread.

        :param operation: short name used in the error text.
        :param fn: callable touching libmpv.
        :raises AudioBackendNotStarted: if the backend is not started.
        :raises AudioOperationError: if the command raised or timed out.
        :return: whatever *fn* returned.
        """
        return await self._await_worker(
            self._require_worker(), operation, fn, timeout=self._command_timeout
        )

    def _create_player(self) -> MpvPlayer:
        """Construct the player on the worker thread.

        :raises AudioBackendUnavailable: if the factory could not build a player.
        :return: the new player.
        """
        try:
            return self._player_factory(dict(self._options))
        except AudioBackendUnavailable:
            raise
        except Exception as exc:
            raise AudioBackendUnavailable(
                f"the audio backend could not be constructed: {type(exc).__name__}: {exc}"
            ) from exc

    def _register_event_handlers(self, player: MpvPlayer) -> tuple[Callable[[], None], ...]:
        """Subscribe to ``start-file`` and ``end-file`` on the player.

        Runs on the worker thread. A blanket ``event_callback()`` would hand every
        event to one handler, and a handler written for end-file fields crashes on
        a start-file event — python-mpv turns that into a ``RuntimeWarning`` on its
        event thread and stops pumping events — so the two names are always
        requested explicitly.

        :param player: player to subscribe on.
        :return: one unregister callable per handler, in registration order.
        """
        handlers = (
            player.event_callback("start-file")(self._handle_start_file),
            player.event_callback("end-file")(self._handle_end_file),
        )
        unregisters: list[Callable[[], None]] = []
        for handler in handlers:
            unregister = getattr(handler, "unregister_mpv_events", None)
            unregisters.append(
                cast("Callable[[], None]", unregister) if callable(unregister) else _noop
            )
        return tuple(unregisters)

    async def _apply_device_hint(self) -> None:
        """Apply the stored hint after :meth:`start`, never failing the start.

        A host with no sound card or an unmatched hint must still start: design 03
        §7 shows a banner on the settings page instead of a dead backend, so this
        path only logs.
        """
        try:
            devices = await self.list_devices()
            if not devices:
                _LOGGER.warning("no audio output device was found; mpv will use the system default")
                return
            hint = self._device_hint
            if hint == "":
                _LOGGER.warning(
                    "no audio.device_hint is set; using the system default audio device"
                )
                return
            device = resolve_device(devices, hint)
            if device is None:
                _LOGGER.warning(
                    "no audio device name matches %r; using the system default audio device",
                    hint,
                )
                return
            await self._write_device(device.name)
        except AudioError:
            _LOGGER.warning(
                "could not apply the audio device hint; using the system default audio device",
                exc_info=True,
            )

    async def _write_device(self, name: str) -> None:
        """Select an mpv device by name.

        :param name: mpv device name, or ``auto`` for the system default.
        :raises AudioBackendNotStarted: if the backend is not started.
        :raises AudioOperationError: if the player rejected the name.
        """
        player = self._require_player()
        await self._command("set audio device", lambda: setattr(player, "audio_device", name))

    async def _quiet_stop(self, player: MpvPlayer) -> None:
        """Ask mpv to stop after a failed or cancelled playback, ignoring failure.

        :param player: player to stop, on the worker thread when one is still there.
        """
        worker = self._worker
        if worker is None:
            return
        try:
            await self._await_worker(worker, "stop playback", player.stop, timeout=_STOP_TIMEOUT_S)
        except asyncio.CancelledError:
            _LOGGER.debug("cancelled while stopping playback after a failure")
        except AudioError:
            _LOGGER.debug("stopping playback after a failure did not succeed", exc_info=True)

    def _post(self, callback: Callable[..., None], *args: object) -> None:
        """Hop from python-mpv's event thread to the event loop.

        :param callback: loop-thread method to run.
        :param args: arguments for *callback*.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            _LOGGER.debug("dropping an mpv event for a closed event loop", exc_info=True)

    def _handle_start_file(self, event: MpvEvent) -> None:
        """Record which playlist entry started, then hop to the loop.

        Runs on python-mpv's event thread and must never raise: an exception there
        stops event pumping. It never calls libmpv.

        :param event: ``start-file`` event delivered by python-mpv.
        """
        try:
            entry_id = _event_int(event, "playlist_entry_id")
            if entry_id is None:
                _LOGGER.debug("ignoring a start-file event without a playlist entry id")
                return
            self._post(self._on_start_file, entry_id)
        except Exception:
            _LOGGER.debug("ignoring malformed mpv event", exc_info=True)

    def _handle_end_file(self, event: MpvEvent) -> None:
        """Parse an end-file event and hop to the loop with its three integers.

        Runs on python-mpv's event thread and must never raise, for the same
        reason as :meth:`_handle_start_file`, and never calls libmpv.

        :param event: ``end-file`` event delivered by python-mpv.
        """
        try:
            entry_id = _event_int(event, "playlist_entry_id")
            reason = _event_int(event, "reason")
            if reason is None:
                _LOGGER.debug("ignoring an end-file event without a reason")
                return
            error = _event_int(event, "error")
            self._post(self._on_end_file, entry_id, reason, 0 if error is None else error)
        except Exception:
            _LOGGER.debug("ignoring malformed mpv event", exc_info=True)

    def _note_resolved(self, entry_id: int | None) -> None:
        """Remember the highest playlist entry that has already finished.

        mpv numbers playlist entries monotonically within one core, so anything at
        or below this id belongs to a file that is already over.

        :param entry_id: entry id of the playback that just finished, if known.
        """
        if entry_id is None or entry_id <= 0:
            return
        resolved = self._last_resolved_entry_id
        if resolved is None or entry_id > resolved:
            self._last_resolved_entry_id = entry_id

    def _on_start_file(self, entry_id: int) -> None:
        """Bind the pending playback to the playlist entry mpv started.

        :param entry_id: ``playlist_entry_id`` of the start-file event.
        """
        pending = self._pending
        if pending is None:
            return
        if pending.entry_id is not None:
            if pending.entry_id != entry_id:
                _LOGGER.debug("ignoring a second start-file event for playlist entry %d", entry_id)
            return
        resolved = self._last_resolved_entry_id
        if resolved is not None and entry_id <= resolved:
            _LOGGER.debug("ignoring a start-file event for finished playlist entry %d", entry_id)
            return
        pending.entry_id = entry_id

    def _on_end_file(self, entry_id: int | None, reason: int, error: int) -> None:
        """Finish the pending playback when its own end-file event arrives.

        The event is matched to a playback by playlist entry id, because mpv sends
        ``start-file`` and then ``end-file`` for the same entry, in that order. A
        delayed abort for a file that has already been resolved — or any event
        that arrives before the pending playback's own ``start-file`` — belongs to
        an earlier file and is dropped, so it can never truncate the file that
        replaced it.

        :param entry_id: ``playlist_entry_id`` of the event, if mpv supplied one.
        :param reason: end-file reason code.
        :param error: mpv error code, meaningful for the error reason.
        """
        pending = self._pending
        if entry_id is not None and entry_id > 0:
            resolved = self._last_resolved_entry_id
            if resolved is not None and entry_id <= resolved:
                _LOGGER.debug("ignoring an end-file event for finished playlist entry %d", entry_id)
                return
            if pending is not None and pending.entry_id != entry_id:
                # Its own start-file has not arrived yet (or named a different
                # entry), so this end-file cannot be the playback we are waiting on.
                _LOGGER.debug("ignoring an end-file event for earlier playlist entry %d", entry_id)
                self._note_resolved(entry_id)
                return
        if pending is None:
            return
        if reason in (_END_FILE_RESTARTED, _END_FILE_REDIRECT):
            _LOGGER.debug(
                "mpv restarted the playlist (reason %d); waiting for the real end", reason
            )
            return
        if reason not in (_END_FILE_EOF, _END_FILE_ABORTED, _END_FILE_QUIT, _END_FILE_ERROR):
            _LOGGER.warning(
                "mpv reported an unknown end-file reason %d; treating the file as finished",
                reason,
            )
        if pending.done.done():
            return
        self._note_resolved(pending.entry_id if pending.entry_id is not None else entry_id)
        pending.done.set_result(_Outcome(reason=reason, error=error))
