"""The playback queue worker: one queue, one task, one chime at a time.

This module is the whole playback policy of design 03 §2 in one place. A press
resolves an action and enqueues a :class:`~chime_deck.press.PlayJob`; this
worker takes the jobs in order, awaits each one to completion on the audio
backend, and records what really happened back into the press history.

The rules it enforces:

* **Never overlap.** The consumer awaits ``play()`` — which returns when the
  file has really finished — before starting the next job.
* **Bounded queue.** :data:`DEFAULT_QUEUE_SIZE` pending jobs at most. When the
  queue is full the *newest* job is dropped and logged, and the press is told
  through an :class:`~chime_deck.audio.AudioError`, so a runaway automation
  cannot eat memory or ring for ten minutes.
* **One failing job never stalls the queue.** A missing or corrupt file fails
  that job and the worker moves on.
* **Shutdown is bounded.** Pending jobs are discarded and recorded, the current
  job gets :data:`SHUTDOWN_GRACE_S` to finish, then it is stopped. Every job
  that left the queue ends up recorded, so no press silently disappears.

The worker writes nothing itself: :class:`PlaybackRecorder` is the seam. In
production it is :class:`HubPlaybackRecorder`, which revises the ``queued`` row
the press path already wrote and then pushes the same flip to the UI;
:class:`PressLogRecorder` is its database half, and unit tests use a recorder
with no database at all.

Owning ticket: HSD-kfnd.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from .audio import AudioBackend, AudioError, MissingAudioFileError
from .hub import StateHub
from .press import PlayJob, PressCode, PressOutcome
from .store import ConfigStore

__all__ = [
    "DEFAULT_QUEUE_SIZE",
    "SHUTDOWN_GRACE_S",
    "WAKE_IDLE_S",
    "HubPlaybackRecorder",
    "PlayResult",
    "PlaybackRecorder",
    "PlaybackWorker",
    "PressLogRecorder",
]

DEFAULT_QUEUE_SIZE: Final = 10
"""Pending jobs the worker accepts before the newest one is dropped (design 03 §2)."""

SHUTDOWN_GRACE_S: Final = 2.0
"""How long shutdown lets the current chime finish before stopping it (design 03 §2)."""

WAKE_IDLE_S: Final = 5.0
"""How long a speaker counts as awake after a chime, for the wake lead-in (HSD-0i95).

A sleeping HDMI or SPDIF output takes about a second to become audible again (see
:mod:`chime_deck.audio.mpv_backend`). The owner's ARM run had an intact press
only 4 s after the previous one, so a gap this short means the sink never went to
sleep: the next chime gets no lead-in and therefore no added delay. A longer gap
cannot be known either way, so it pays the configured ``audio.wake_lead_in_ms`` of
silence and is better late than clipped.
"""

_STOP_SETTLE_S: Final = 1.0
"""Bound for the last, best-effort waits during shutdown."""


@dataclass(frozen=True, slots=True)
class PlayResult:
    """What really happened to one job, and how long it took.

    ``pressed_at`` is the timestamp the press path already wrote, not a fresh
    reading: the played/failed flip must carry the same instant as the
    ``queued`` message and the ``press_log`` row so the UI can correlate them.

    ``latency_ms`` and ``duration_ms`` are ``None`` when the job never started
    or never finished: a missing number is honest, a faked ``0`` would look like
    an instant chime in the UI (design 03 §8).

    ``lead_in_ms`` is a fact, not a request: it is the silence the backend
    reported actually prepending, ``None`` when none was asked for or the job
    failed before it could be applied.
    """

    job_id: str
    button_id: int
    button_name: str
    outcome: PressOutcome
    pressed_at: str
    latency_ms: int | None = None
    duration_ms: int | None = None
    lead_in_ms: int | None = None
    code: PressCode | None = None
    detail: str | None = None


class PlaybackRecorder(Protocol):
    """Where a :class:`PlayResult` goes: the press history in production."""

    async def record(self, result: PlayResult) -> None:
        """Record one finished (or discarded) job.

        Implementations must not raise for a missing row; a recorder that blows
        up must never be able to kill the worker.

        :param result: what happened to the job.
        """
        ...


class PressLogRecorder:
    """Write playback results back onto the press log row the press wrote."""

    def __init__(self, store: ConfigStore) -> None:
        """Create a recorder over *store*.

        :param store: configuration store whose ``press_log`` is revised.

        The logger is ``logging.getLogger(__name__)``, created last.
        """
        self._store = store
        self._logger = logging.getLogger(__name__)

    async def record(self, result: PlayResult) -> None:
        """Revise the ``queued`` row of this job with the real outcome.

        :param result: what happened to the job.
        """
        detail = result.detail
        if result.code is not None:
            # The same shape ``PressService._finish`` produces for a failing
            # press, so one row reads consistently whichever half wrote it.
            detail = str(result.code) if detail is None else f"{result.code}: {detail}"
        recorded = await self._store.record_playback(
            job_id=result.job_id,
            outcome=result.outcome.value,
            detail=detail,
            duration_ms=result.duration_ms,
            latency_ms=result.latency_ms,
            note=_lead_in_note(result.lead_in_ms),
        )
        if not recorded:
            self._logger.warning(
                "no queued press log row for job %s (%r): the %s result was not recorded",
                result.job_id,
                result.button_name,
                result.outcome,
            )


class HubPlaybackRecorder:
    """Record the real outcome and push it to the UI in one step.

    This is the ONLY place a played/failed flip is logged and pushed: the row is
    revised first (so a reader that sees the message also sees the row), then the
    hub message goes out with the press's own timestamp, which the UI needs to
    treat the second ``press`` message for one press as a status flip rather than
    a new press.
    """

    def __init__(self, store: ConfigStore, hub: StateHub) -> None:
        """Create a recorder over *store* and *hub*.

        :param store: configuration store whose ``press_log`` is revised.
        :param hub: broadcast hub that receives the played/failed message.
        """
        self._log = PressLogRecorder(store)
        self._hub = hub

    async def record(self, result: PlayResult) -> None:
        """Revise the ``queued`` row, then push the flip to the UI.

        The order is deliberate and asymmetric with
        :meth:`PressService._finish <chime_deck.press.PressService._finish>`,
        which pushes unconditionally: here a history write that raises skips the
        push, because announcing ``played`` for a press the log does not show
        playing would put the UI and the history into disagreement. A write that
        succeeds but matches no queued row only warns (see
        :class:`PressLogRecorder`), so the flip is still announced — the UI must
        tolerate a ``played`` message whose row is missing.

        :param result: what happened to the job.
        """
        await self._log.record(result)
        self._hub.notify_press(
            button_id=result.button_id,
            button_name=result.button_name,
            outcome=result.outcome.value,
            pressed_at=result.pressed_at,
        )


@dataclass(frozen=True, slots=True)
class _QueuedJob:
    """One accepted job plus the moment it was accepted, for latency."""

    job: PlayJob
    accepted_at: float


class PlaybackWorker:
    """Consume :class:`~chime_deck.press.PlayJob` objects one at a time.

    The worker is a :class:`~chime_deck.press.PressDispatcher`, so the press
    path only ever sees "accepted for playback". Everything else — ordering,
    overlap, dropping, durations and shutdown — lives here.
    """

    def __init__(
        self,
        backend: AudioBackend,
        recorder: PlaybackRecorder,
        *,
        maxsize: int = DEFAULT_QUEUE_SIZE,
        shutdown_grace: float = SHUTDOWN_GRACE_S,
        wake_lead_in_ms: int = 0,
        wake_idle_s: float = WAKE_IDLE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the worker; nothing runs until :meth:`start` is awaited.

        :param backend: speaker that plays one file at a time.
        :param recorder: sink for the result of every job that leaves the queue.
        :param maxsize: pending jobs accepted before the newest is dropped.
        :param shutdown_grace: seconds the current chime gets to finish on stop.
        :param wake_lead_in_ms: milliseconds of silence prepended to a chime that
            follows a possible idle period, from ``audio.wake_lead_in_ms``; ``0``
            disables the lead-in.
        :param wake_idle_s: seconds after a chime during which the speaker counts
            as still awake, so the next job asks for no lead-in.
        :param clock: monotonic clock used for latency, duration and the wake gap.

        ``None`` in the queue is the stop sentinel that wakes an idle consumer.
        The logger is ``logging.getLogger(__name__)``, created last.
        """
        self._backend = backend
        self._recorder = recorder
        self._shutdown_grace = shutdown_grace
        self._wake_lead_in_ms = wake_lead_in_ms
        self._wake_idle_s = wake_idle_s
        self._last_play_finished: float | None = None
        self._clock = clock
        self._queue: asyncio.Queue[_QueuedJob | None] = asyncio.Queue(maxsize)
        self._task: asyncio.Task[None] | None = None
        self._current: _QueuedJob | None = None
        self._closed = False
        self._logger = logging.getLogger(__name__)

    @property
    def is_running(self) -> bool:
        """Whether the consumer task exists and has not finished."""
        return self._task is not None and not self._task.done()

    @property
    def pending_count(self) -> int:
        """How many jobs are waiting in the queue right now."""
        return self._queue.qsize()

    @property
    def backend(self) -> AudioBackend:
        """Return the speaker this worker drives (never swapped after construction)."""
        return self._backend

    @property
    def wake_lead_in_ms(self) -> int:
        """Return the configured wake lead-in this worker was built with."""
        return self._wake_lead_in_ms

    async def start(self, *, timeout_s: float | None = None) -> None:
        """Start the audio backend and the consumer task. Idempotent.

        :param timeout_s: budget for constructing the backend, in seconds;
            ``None`` uses the backend's own default.
        :raises AudioError: if the backend cannot be started; the worker is then
            left not started, so a press fails instead of queueing into nothing.
        """
        if self.is_running:
            return
        await self._backend.start(timeout_s=timeout_s)
        # A ``stop()`` that cancelled the consumer mid-chime left its stop
        # sentinel (and possibly jobs) behind. The next consumer would read that
        # sentinel first and exit at once, so every leftover is cleared here.
        # The previous ``stop()`` already recorded its own discarded jobs, so
        # nothing is lost.
        stale = self._drain()
        if stale:
            self._logger.warning(
                "playback worker restart: cleared %d leftover queued chime(s)", len(stale)
            )
        self._closed = False
        self._task = asyncio.create_task(self._consume(), name="playback-worker")
        self._logger.info("playback worker started (queue size %d)", self._queue.maxsize)

    async def dispatch(self, job: PlayJob) -> None:
        """Accept one job for playback, or reject it immediately.

        This never awaits playback: the enqueue path must not play inline, so a
        press returns as soon as its job is in the queue.

        :param job: resolved job from the press service.
        :raises AudioError: if the worker is not running, or the queue is full
            and this newest job had to be dropped.
        """
        if not self.is_running or self._closed:
            raise AudioError("the playback worker is not running, so the chime cannot be queued")
        try:
            self._queue.put_nowait(_QueuedJob(job=job, accepted_at=self._clock()))
        except asyncio.QueueFull as exc:
            self._logger.warning(
                "playback queue is full (%d chime(s) waiting): dropping the newest job %r",
                self._queue.maxsize,
                job.button_name,
            )
            raise AudioError(
                f"queue full: {job.button_name!r} was dropped because "
                f"{self._queue.maxsize} chime(s) were already waiting"
            ) from exc

    async def _consume(self) -> None:
        """Play queued jobs in order until the stop sentinel arrives."""
        while True:
            item = await self._queue.get()
            if item is None:
                return
            self._current = item
            try:
                await self._play(item)
            finally:
                self._current = None

    async def _play(self, item: _QueuedJob) -> None:
        """Play one job and record what happened; never raise for a failed job.

        :param item: queued job and its acceptance time.
        """
        job = item.job
        started = self._clock()
        latency_ms = _elapsed_ms(item.accepted_at, started)
        # Checked before the backend sees it: a file deleted after the press was
        # accepted must fail as ``sound_missing`` with a detail the user can act
        # on, not as a decoder error from a player that could not open it.
        missing = _missing_file_detail(job)
        if missing is not None:
            await self._report(
                PlayResult(
                    job_id=job.job_id,
                    button_id=job.button_id,
                    button_name=job.button_name,
                    outcome=PressOutcome.FAILED,
                    pressed_at=job.pressed_at,
                    latency_ms=latency_ms,
                    code=PressCode.SOUND_MISSING,
                    detail=missing,
                )
            )
            return
        try:
            lead_in = self._lead_in_for_next_play()
            try:
                applied_ms = await self._backend.play(
                    job.path, volume=job.volume, lead_in_ms=lead_in
                )
            finally:
                # A failed chime still drove the output, so a job that follows
                # soon after counts as playing into an awake sink.
                self._last_play_finished = self._clock()
        except asyncio.CancelledError:
            raise
        # One bad file must neither stall the queue nor kill the loop, so the
        # failure is recorded and the consumer moves on (design 03 §7).
        except Exception as exc:
            await self._report(
                PlayResult(
                    job_id=job.job_id,
                    button_id=job.button_id,
                    button_name=job.button_name,
                    outcome=PressOutcome.FAILED,
                    pressed_at=job.pressed_at,
                    latency_ms=latency_ms,
                    code=_code_for(exc),
                    detail=_describe(exc),
                )
            )
            return
        await self._report(
            PlayResult(
                job_id=job.job_id,
                button_id=job.button_id,
                button_name=job.button_name,
                outcome=PressOutcome.PLAYED,
                pressed_at=job.pressed_at,
                latency_ms=latency_ms,
                duration_ms=_elapsed_ms(started, self._clock()),
                lead_in_ms=applied_ms or None,
            )
        )

    def _lead_in_for_next_play(self) -> int:
        """Return the milliseconds of silence the next chime should start with.

        ``0`` when the lead-in is switched off, and ``0`` again when a chime
        finished less than ``wake_idle_s`` ago: a sink that never slept gains
        nothing but delay. Every other case — the first chime of a run included —
        pays the configured lead-in, because a longer gap cannot be known either
        way.

        :returns: the lead-in to hand to :meth:`AudioBackend.play`, in milliseconds.
        """
        if self._wake_lead_in_ms <= 0:
            return 0
        last_finished = self._last_play_finished
        if last_finished is not None and self._clock() - last_finished < self._wake_idle_s:
            return 0
        return self._wake_lead_in_ms

    async def _report(self, result: PlayResult) -> None:
        """Hand one result to the recorder without letting a broken one kill us.

        :param result: what happened to the job.
        """
        try:
            await self._recorder.record(result)
        except Exception:
            self._logger.exception(
                "could not record the playback of %r (job %s)",
                result.button_name,
                result.job_id,
            )

    def _drain(self) -> list[_QueuedJob]:
        """Remove every pending job from the queue and return them in order.

        :returns: jobs that were still waiting, oldest first.
        """
        dropped: list[_QueuedJob] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return dropped
            if item is not None:
                dropped.append(item)

    async def _report_discarded(self, dropped: list[_QueuedJob]) -> None:
        """Record every job that shutdown threw away, bounded by the grace.

        :param dropped: jobs removed from the queue by :meth:`_drain`.
        """
        if not dropped:
            return
        for item in dropped:
            self._logger.warning(
                "shutdown: discarding the queued chime for %r", item.job.button_name
            )
        try:
            async with asyncio.timeout(self._shutdown_grace):
                for item in dropped:
                    job = item.job
                    await self._report(
                        PlayResult(
                            job_id=job.job_id,
                            button_id=job.button_id,
                            button_name=job.button_name,
                            outcome=PressOutcome.FAILED,
                            pressed_at=job.pressed_at,
                            detail="shutdown: discarded before playback",
                        )
                    )
        except TimeoutError:
            self._logger.warning(
                "shutdown: could not record %d discarded chime(s) within %g s",
                len(dropped),
                self._shutdown_grace,
            )

    async def _report_interrupted(self) -> None:
        """Record the in-flight job that shutdown had to stop, if there is one."""
        item = self._current
        if item is None:
            return
        job = item.job
        await self._report(
            PlayResult(
                job_id=job.job_id,
                button_id=job.button_id,
                button_name=job.button_name,
                outcome=PressOutcome.FAILED,
                pressed_at=job.pressed_at,
                detail=f"shutdown: playback stopped after the {self._shutdown_grace:g} s grace",
            )
        )

    def _log_task_failure(self, task: asyncio.Task[None]) -> None:
        """Log the consumer task's exception, so it is never left unretrieved.

        :param task: the consumer task, which has finished.
        """
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._logger.error("playback worker stopped unexpectedly: %r", error)

    async def stop(self) -> None:
        """Stop the worker: discard pending jobs, let the current one finish, stop the backend.

        Every wait is bounded, so shutdown finishes in roughly
        ``2 * shutdown_grace + 3 * _STOP_SETTLE_S`` **plus** the backend stop that
        follows — bounded by the backend itself (2 s for mpv). That covers one
        grace to record the discarded jobs, one grace to let the current chime
        finish, and three settle waits for recording the interrupted job, stopping
        the speaker and waiting for the consumer to exit. Safe to call twice, and
        safe on a worker that was never started.
        """
        task, self._task = self._task, None
        self._closed = True

        dropped = self._drain()
        await self._report_discarded(dropped)

        pending: set[asyncio.Task[None]] = set()
        if task is not None and not task.done():
            # Wake an idle consumer so a job-free queue does not wait out the
            # grace. ``asyncio.wait`` — not ``wait_for`` — because ``wait_for``
            # would cancel the consumer on timeout, killing the current chime
            # outright and leaving its row stuck at ``queued``.
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)
            _, pending = await asyncio.wait({task}, timeout=self._shutdown_grace)

        if pending and task is not None:
            try:
                async with asyncio.timeout(_STOP_SETTLE_S):
                    await self._report_interrupted()
            except TimeoutError:
                self._logger.warning(
                    "shutdown: could not record the interrupted chime within %g s",
                    _STOP_SETTLE_S,
                )
            with contextlib.suppress(Exception):
                async with asyncio.timeout(_STOP_SETTLE_S):
                    await self._backend.stop_playback()
            task.cancel()
            _, pending = await asyncio.wait({task}, timeout=_STOP_SETTLE_S)
            if pending:
                self._logger.error("playback worker did not stop within %g s", _STOP_SETTLE_S)

        if task is not None and task.done():
            self._log_task_failure(task)

        with contextlib.suppress(Exception):
            await self._backend.stop()

        self._logger.info("playback worker stopped (discarded %d queued chime(s))", len(dropped))


def _elapsed_ms(start: float, end: float) -> int:
    """Return the milliseconds between two clock readings.

    :param start: earlier reading.
    :param end: later reading.
    """
    return max(0, round((end - start) * 1000))


def _missing_file_detail(job: PlayJob) -> str | None:
    """Return a readable detail when *job*'s sound-library file is gone, else ``None``.

    Only a sound-library file (``sound_id`` set) is checked here. A speech clip is
    left to the backend on purpose: the clip cache owns its lifecycle, and
    libmpv's own missing-file error is already mapped to
    :attr:`~chime_deck.press.PressCode.SOUND_MISSING` for the same press-log
    code. This check exists so a library file that vanished after the press names
    the sound the user has to re-scan, instead of surfacing a decoder error.

    :param job: resolved job about to be played.
    """
    if job.sound_id is None:
        return None
    try:
        job.path.stat()
    except OSError:
        return f"sound {job.sound_id} for {job.button_name!r} is no longer on disk: {job.path}"
    return None


def _lead_in_note(ms: int | None) -> str | None:
    """Return the press-log note describing a wake lead-in, if there was one.

    :param ms: milliseconds of leading silence the backend reported applying.
    :returns: a note such as ``"wake lead-in 1.5 s"``, or ``None`` when no
        lead-in was asked for or applied, so a normal chime adds no note.
    """
    if not ms:
        return None
    return f"wake lead-in {ms / 1000:g} s"


def _code_for(exc: BaseException) -> PressCode:
    """Return the press-history code for a failed playback.

    The vocabulary is the one the press path already uses, so the history never
    invents a code for a failure the user can already see elsewhere.

    :param exc: exception raised by the audio backend.
    """
    if isinstance(exc, MissingAudioFileError):
        return PressCode.SOUND_MISSING
    if isinstance(exc, AudioError):
        return PressCode.AUDIO_ERROR
    return PressCode.DISPATCH_ERROR


def _describe(exc: BaseException) -> str:
    """Return the user-facing detail of a failed playback.

    :param exc: exception raised by the audio backend.
    """
    if isinstance(exc, AudioError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"
