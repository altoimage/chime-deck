"""The playback queue worker: ordering, overlap, dropping, timings and shutdown.

Fully offline. The speaker is a fake that records what it was asked to play and
can be blocked on an :class:`asyncio.Event`, so overlap and shutdown can be
observed without a sound card. There is no libmpv, no real file, no sound
device, no TTS and no network here; every await in this file is bounded, and
the integration tests use the real store, hub and press service against that
fake backend.

Owning ticket: HSD-kfnd.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TypeVar

import pytest

from chime_deck.audio import (
    SYSTEM_DEFAULT_DEVICE,
    AudioBackendNotStarted,
    AudioDevice,
    AudioError,
    MissingAudioFileError,
    PlaybackError,
    PlaybackInProgressError,
)
from chime_deck.health import HealthRegistry
from chime_deck.hub import StateHub
from chime_deck.playback import (
    WAKE_IDLE_S,
    PlaybackRecorder,
    PlaybackWorker,
    PlayResult,
    PressLogRecorder,
    _lead_in_note,
)
from chime_deck.press import (
    PlayJob,
    PressCode,
    PressDispatcher,
    PressOutcome,
    PressService,
)
from chime_deck.store import Button, ButtonIn, ConfigStore, SoundIn

pytestmark = pytest.mark.asyncio

_AWAIT_TIMEOUT_S = 2.0
"""Bound for every ``await`` in this file."""

_NEVER = 3600.0
"""Hub flush interval used by the integration tests: the background tick never fires."""

_T = TypeVar("_T")


class _FakeClock:
    """A deterministic monotonic clock the test advances by hand."""

    def __init__(self, now: float = 0.0) -> None:
        """Start the clock at *now*.

        :param now: initial reading in seconds.
        """
        self.now = now

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        :param seconds: how far to advance, in seconds.
        """
        self.now += seconds


class _FakeBackend:
    """A speaker that records what it played and can be held open by a gate.

    It refuses to overlap two files the way the real backend does, so a worker
    that started a second chime early would fail loudly here.
    """

    def __init__(self, clock: _FakeClock | None = None) -> None:
        """Create a backend that has not been started.

        :param clock: clock advanced by ``durations`` while a file plays.
        """
        self.clock = clock if clock is not None else _FakeClock()
        self.played: list[Path] = []
        self.lead_ins: list[int] = []
        self.playing = False
        self.started = False
        self.start_calls = 0
        self.start_timeouts: list[float | None] = []
        self.stop_calls = 0
        self.stop_playback_calls = 0
        self.device_hints: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.errors: dict[Path, Exception] = {}
        self.durations: dict[Path, float] = {}
        self.gate: asyncio.Event | None = None
        self.on_play: Callable[[Path], Awaitable[None]] | None = None
        self.devices: list[AudioDevice] = [
            AudioDevice(name=SYSTEM_DEFAULT_DEVICE, hostapi="auto", is_default=True)
        ]

    async def start(self, *, timeout_s: float | None = None) -> None:
        """Mark the backend usable.

        :param timeout_s: budget the caller offered for this start, in seconds.
        """
        self.start_calls += 1
        self.start_timeouts.append(timeout_s)
        self.started = True

    async def stop(self) -> None:
        """Mark the backend unusable again."""
        self.stop_calls += 1
        self.started = False

    async def play(
        self,
        path: Path,
        *,
        volume: int,
        lead_in_ms: int = 0,
        when_done: Callable[[], None] | None = None,
    ) -> int:
        """Play *path*, honouring the gate, the configured error and duration.

        :param path: file to "play"; it is never touched.
        :param volume: volume 0-100; accepted and ignored.
        :param lead_in_ms: silence the worker asked to prepend, in milliseconds;
            it is recorded and echoed back as "applied".
        :param when_done: optional callback run once when playback ends.
        :returns: the milliseconds of leading silence this fake reports applying.
        :raises AudioBackendNotStarted: if :meth:`start` was not called.
        :raises PlaybackInProgressError: if a file is already playing.
        :raises Exception: the exception configured for *path* in ``errors``.
        """
        self.lead_ins.append(lead_in_ms)
        if not self.started:
            raise AudioBackendNotStarted("the fake backend was not started")
        if self.playing:
            raise PlaybackInProgressError(f"{path} would overlap the file already playing")
        self.playing = True
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.on_play is not None:
                await self.on_play(path)
            error = self.errors.get(path)
            if error is not None:
                raise error
            self.clock.advance(self.durations.get(path, 0.0))
            self.played.append(path)
        finally:
            self.concurrent -= 1
            self.playing = False
            if when_done is not None:
                when_done()
        return lead_in_ms

    async def stop_playback(self) -> None:
        """Stop the current file and release the gate that holds it open."""
        self.stop_playback_calls += 1
        if self.gate is not None:
            self.gate.set()

    async def set_device(self, hint: str) -> None:
        """Record the requested device hint.

        :param hint: name substring from ``audio.device_hint``.
        """
        self.device_hints.append(hint)

    async def list_devices(self) -> list[AudioDevice]:
        """Return the fake's single default device."""
        return list(self.devices)


class _RecordingRecorder:
    """Collect every :class:`PlayResult`, or raise the configured error."""

    def __init__(self, error: Exception | None = None) -> None:
        """Create a recorder with an optional permanent failure.

        :param error: exception raised by :meth:`record`, or ``None`` to accept.
        """
        self.results: list[PlayResult] = []
        self.error = error

    async def record(self, result: PlayResult) -> None:
        """Store *result* unless an error is configured.

        :param result: what happened to one job.
        """
        if self.error is not None:
            raise self.error
        self.results.append(result)


class _NullRecorder:
    """Discard every result: used where only the backend is under test."""

    async def record(self, result: PlayResult) -> None:
        """Ignore *result*.

        :param result: what happened to one job.
        """


_CONFORMANCE: PressDispatcher = PlaybackWorker(_FakeBackend(), _NullRecorder())
"""Typing pin only: this worker is never started and must not be reused by a test."""

_RECORDER_CONFORMANCE: PlaybackRecorder = _NullRecorder()
"""Typing pin for the recorder seam, as tests/test_audio_backend.py pins its protocol."""


async def _run(awaitable: Awaitable[_T]) -> _T:
    """Await *awaitable* under the file's fixed bound.

    :param awaitable: coroutine or future to await.
    :returns: whatever *awaitable* produced.
    """
    return await asyncio.wait_for(awaitable, timeout=_AWAIT_TIMEOUT_S)


async def _settle() -> None:
    """Let the event loop run the worker's currently ready steps."""
    await asyncio.sleep(0.02)


async def _wait_until(predicate: Callable[[], Awaitable[bool]]) -> None:
    """Poll *predicate* until it is true, with a fixed attempt cap.

    :param predicate: condition another task is expected to satisfy.
    :raises AssertionError: if the condition is still false after 2 s.
    """
    for _ in range(200):
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met within 2 s")


def _counted(backend: _FakeBackend, count: int) -> Callable[[], Awaitable[bool]]:
    """Return a predicate that becomes true once *count* files have played.

    :param backend: fake backend under test.
    :param count: number of finished files to wait for.
    """

    async def predicate() -> bool:
        return len(backend.played) == count

    return predicate


def _recorded(recorder: _RecordingRecorder, count: int) -> Callable[[], Awaitable[bool]]:
    """Return a predicate that becomes true once *count* results were recorded.

    :param recorder: recording recorder under test.
    :param count: number of results to wait for.
    """

    async def predicate() -> bool:
        return len(recorder.results) == count

    return predicate


def _running(worker: PlaybackWorker) -> bool:
    """Return the worker's running flag.

    Reading it through a call keeps mypy from narrowing the property and then
    treating the rest of a test as unreachable.

    :param worker: worker under test.
    """
    return worker.is_running


def _started(backend: _FakeBackend) -> Callable[[], Awaitable[bool]]:
    """Return a predicate that becomes true once the fake backend is playing.

    :param backend: fake backend under test.
    """

    async def predicate() -> bool:
        return backend.playing

    return predicate


def _press_outcome(store: ConfigStore, outcome: str) -> Callable[[], Awaitable[bool]]:
    """Return a predicate that becomes true once the newest press row has *outcome*.

    :param store: connected configuration store.
    :param outcome: outcome to wait for, for example ``"played"``.
    """

    async def predicate() -> bool:
        entries = await store.recent_presses()
        return bool(entries) and entries[0].outcome == outcome

    return predicate


def _job(path: Path, *, job_id: str = "job-1", name: str = "Front Door") -> PlayJob:
    """Build one resolved job for the worker.

    :param path: file to play.
    :param job_id: id the press path would have written to ``press_log``.
    :param name: display name of the button.
    """
    return PlayJob(
        job_id=job_id,
        button_id=1,
        button_name=name,
        path=path,
        volume=70,
        sound_id=None,
        pressed_at="2026-01-01T00:00:00+00:00",
    )


async def _seed_sound(store: ConfigStore, tmp_path: Path, *, name: str = "Front Door") -> Button:
    """Create a file-backed sound and a sound button that plays it, and return it.

    The file is really written, because the worker checks that a sound-library
    file still exists before it asks the backend to play it (HSD-dtpo). A row
    pointing at a path that was never there is not what the sound library
    produces.

    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory used for the sound path.
    :param name: display name of the button, which also decides its slug.
    """
    sound_path = tmp_path / "ding.mp3"
    sound_path.write_bytes(b"ID3")
    sound = await store.create_sound(
        SoundIn(name="Ding dong", source_type="file", path=str(sound_path))
    )
    return await store.upsert_button(ButtonIn(name=name, action_type="sound", sound_id=sound.id))


async def test_jobs_play_in_order_and_dispatch_only_enqueues() -> None:
    """Dispatching accepts the job and returns; the worker plays them in order."""
    backend = _FakeBackend()
    recorder = _RecordingRecorder()
    worker = PlaybackWorker(backend, recorder)
    paths = [Path(f"/sounds/{index}.mp3") for index in range(3)]
    started: list[Path] = []

    async def on_play(path: Path) -> None:
        """Remember that playback began.

        :param path: file the worker is about to play.
        """
        started.append(path)

    backend.on_play = on_play

    await _run(worker.start())
    try:
        # ``dispatch`` never suspends, so no job can start before this block ends.
        async with asyncio.timeout(_AWAIT_TIMEOUT_S):
            for index, path in enumerate(paths):
                await worker.dispatch(_job(path, job_id=f"job-{index}"))
        # Dispatch only enqueues: nothing has started and nothing has rung yet.
        assert started == []
        assert backend.played == []

        await _wait_until(_counted(backend, len(paths)))
        assert started == paths
        assert backend.played == paths
        assert [(result.job_id, result.outcome) for result in recorder.results] == [
            ("job-0", PressOutcome.PLAYED),
            ("job-1", PressOutcome.PLAYED),
            ("job-2", PressOutcome.PLAYED),
        ]
    finally:
        await _run(worker.stop())


async def test_burst_of_jobs_never_overlaps() -> None:
    """A burst of jobs is played one at a time, never two at once."""
    backend = _FakeBackend()
    worker = PlaybackWorker(backend, _RecordingRecorder())
    paths = [Path(f"/sounds/burst-{index}.mp3") for index in range(10)]

    await _run(worker.start())
    try:
        for index, path in enumerate(paths):
            await _run(worker.dispatch(_job(path, job_id=f"burst-{index}")))
        await _settle()
        await _wait_until(_counted(backend, len(paths)))
        assert backend.max_concurrent == 1
        assert backend.played == paths
    finally:
        await _run(worker.stop())


async def test_full_queue_drops_the_newest_job_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The eleventh waiting chime is refused with an ``AudioError`` and a warning.

    :param caplog: pytest log capture.
    """
    backend = _FakeBackend()
    backend.gate = asyncio.Event()
    worker = PlaybackWorker(backend, _RecordingRecorder())
    caplog.set_level(logging.WARNING, logger="chime_deck")

    await _run(worker.start())
    try:
        blocker = Path("/sounds/blocker.mp3")
        await _run(worker.dispatch(_job(blocker, job_id="blocker")))
        await _wait_until(_started(backend))

        accepted = [Path(f"/sounds/accepted-{index}.mp3") for index in range(10)]
        for index, path in enumerate(accepted):
            await _run(worker.dispatch(_job(path, job_id=f"accepted-{index}")))
        assert worker.pending_count == 10

        with pytest.raises(AudioError, match="queue full"):
            await _run(
                worker.dispatch(
                    _job(Path("/sounds/eleventh.mp3"), job_id="dropped", name="Eleventh")
                )
            )

        assert "Eleventh" in caplog.text
        assert "full" in caplog.text

        backend.gate.set()
        await _wait_until(_counted(backend, 11))
        assert backend.played == [blocker, *accepted]
    finally:
        if backend.gate is not None:
            backend.gate.set()
        await _run(worker.stop())


async def test_queue_is_usable_again_after_a_drop() -> None:
    """One dropped chime does not poison the queue: the next job is accepted."""
    backend = _FakeBackend()
    backend.gate = asyncio.Event()
    worker = PlaybackWorker(backend, _RecordingRecorder(), maxsize=1)
    paths = [Path(f"/sounds/queued-{index}.mp3") for index in range(3)]

    await _run(worker.start())
    try:
        await _run(worker.dispatch(_job(paths[0], job_id="job-0")))
        await _wait_until(_started(backend))
        await _run(worker.dispatch(_job(paths[1], job_id="job-1")))
        with pytest.raises(AudioError, match="queue full"):
            await _run(worker.dispatch(_job(paths[2], job_id="job-2")))

        backend.gate.set()
        await _wait_until(_counted(backend, 2))

        await _run(worker.dispatch(_job(paths[2], job_id="job-2")))
        await _wait_until(_counted(backend, 3))
        assert backend.played == paths
    finally:
        if backend.gate is not None:
            backend.gate.set()
        await _run(worker.stop())


async def test_a_failing_job_does_not_stall_the_queue() -> None:
    """A file the backend refuses fails that job only; the next one still plays."""
    backend = _FakeBackend()
    recorder = _RecordingRecorder()
    worker = PlaybackWorker(backend, recorder)
    broken = Path("/sounds/broken.mp3")
    healthy = Path("/sounds/healthy.mp3")
    backend.errors[broken] = PlaybackError(f"cannot play {broken}: broken file")

    await _run(worker.start())
    try:
        await _run(worker.dispatch(_job(broken, job_id="job-1")))
        await _run(worker.dispatch(_job(healthy, job_id="job-2")))
        await _wait_until(_recorded(recorder, 2))

        assert backend.played == [healthy]
        assert [result.outcome for result in recorder.results] == [
            PressOutcome.FAILED,
            PressOutcome.PLAYED,
        ]
        assert recorder.results[0].code is PressCode.AUDIO_ERROR
    finally:
        await _run(worker.stop())


async def test_missing_file_is_recorded_as_sound_missing() -> None:
    """A vanished file keeps the press-history vocabulary: ``sound_missing``."""
    backend = _FakeBackend()
    recorder = _RecordingRecorder()
    worker = PlaybackWorker(backend, recorder)
    missing = Path("/sounds/gone.mp3")
    backend.errors[missing] = MissingAudioFileError(missing)

    await _run(worker.start())
    try:
        await _run(worker.dispatch(_job(missing, job_id="job-1")))
        await _wait_until(_recorded(recorder, 1))
        (result,) = recorder.results
        assert result.outcome is PressOutcome.FAILED
        assert result.code is PressCode.SOUND_MISSING
        assert result.detail is not None
        assert "audio file not found" in result.detail
    finally:
        await _run(worker.stop())


async def test_every_failing_job_is_recorded_in_order() -> None:
    """A queue of broken files still produces one result per job, in order."""
    backend = _FakeBackend()
    recorder = _RecordingRecorder()
    worker = PlaybackWorker(backend, recorder)
    paths = [Path(f"/sounds/broken-{index}.mp3") for index in range(3)]
    for path in paths:
        backend.errors[path] = PlaybackError(f"cannot play {path}: broken file")

    await _run(worker.start())
    try:
        for index, path in enumerate(paths):
            await _run(worker.dispatch(_job(path, job_id=f"job-{index}")))
        await _wait_until(_recorded(recorder, len(paths)))

        assert [result.job_id for result in recorder.results] == ["job-0", "job-1", "job-2"]
        assert all(result.outcome is PressOutcome.FAILED for result in recorder.results)
        assert backend.played == []
    finally:
        await _run(worker.stop())


async def test_latency_and_duration_are_reported_in_milliseconds() -> None:
    """Latency is enqueue-to-first-sound and duration is first-sound-to-end."""
    clock = _FakeClock()
    backend = _FakeBackend(clock)
    recorder = _RecordingRecorder()
    worker = PlaybackWorker(backend, recorder, clock=clock)
    first = Path("/sounds/one.mp3")
    second = Path("/sounds/two.mp3")
    backend.durations[first] = 1.0

    await _run(worker.start())
    try:
        # Both jobs are accepted at t=0, before the worker takes the first one.
        await _run(
            asyncio.gather(
                worker.dispatch(_job(first, job_id="job-1")),
                worker.dispatch(_job(second, job_id="job-2")),
            )
        )
        await _wait_until(_counted(backend, 2))

        results = {result.job_id: result for result in recorder.results}
        assert results["job-1"].latency_ms == 0
        assert results["job-1"].duration_ms == 1000
        assert results["job-2"].latency_ms == 1000
        assert results["job-2"].duration_ms == 0
    finally:
        await _run(worker.stop())


async def test_the_first_chime_of_a_run_pays_the_configured_wake_lead_in() -> None:
    """Nothing is known about the first chime, so it starts with the configured silence."""
    clock = _FakeClock()
    backend = _FakeBackend(clock)
    recorder = _RecordingRecorder()
    worker = PlaybackWorker(backend, recorder, wake_lead_in_ms=1500, clock=clock)

    await _run(worker.start())
    try:
        await _run(worker.dispatch(_job(Path("/sounds/first.mp3"))))
        await _wait_until(_counted(backend, 1))
    finally:
        await _run(worker.stop())

    assert backend.lead_ins == [1500]


async def test_a_chime_soon_after_another_asks_for_no_lead_in() -> None:
    """The regression test: the sink never slept, so a lead-in would only add delay.

    The owner's ARM run had an intact press 4 s after the previous one, which is
    inside :data:`WAKE_IDLE_S`.
    """
    clock = _FakeClock()
    backend = _FakeBackend(clock)
    recorder = _RecordingRecorder()
    worker = PlaybackWorker(backend, recorder, wake_lead_in_ms=1500, clock=clock)
    first = Path("/sounds/first.mp3")
    second = Path("/sounds/second.mp3")
    backend.durations[first] = 4.0

    await _run(worker.start())
    try:
        await _run(worker.dispatch(_job(first, job_id="job-1")))
        await _wait_until(_counted(backend, 1))
        await _run(worker.dispatch(_job(second, job_id="job-2")))
        await _wait_until(_counted(backend, 2))
    finally:
        await _run(worker.stop())

    # The first chime paid the lead-in; the second, 4 s later, paid nothing.
    assert backend.lead_ins == [1500, 0]


async def test_a_chime_after_a_long_idle_gap_pays_the_lead_in_again() -> None:
    """A gap longer than :data:`WAKE_IDLE_S` means the sink may have slept."""
    clock = _FakeClock()
    backend = _FakeBackend(clock)
    recorder = _RecordingRecorder()
    worker = PlaybackWorker(backend, recorder, wake_lead_in_ms=1500, clock=clock)
    first = Path("/sounds/first.mp3")
    second = Path("/sounds/second.mp3")

    await _run(worker.start())
    try:
        await _run(worker.dispatch(_job(first, job_id="job-1")))
        await _wait_until(_counted(backend, 1))
        clock.advance(WAKE_IDLE_S + 1.0)
        await _run(worker.dispatch(_job(second, job_id="job-2")))
        await _wait_until(_counted(backend, 2))
    finally:
        await _run(worker.stop())

    assert backend.lead_ins == [1500, 1500]


async def test_a_played_result_carries_the_lead_in_the_backend_applied() -> None:
    """The press log records what really happened, not what the worker asked for."""
    backend = _FakeBackend()
    recorder = _RecordingRecorder()
    worker = PlaybackWorker(backend, recorder, wake_lead_in_ms=1500)

    await _run(worker.start())
    try:
        await _run(worker.dispatch(_job(Path("/sounds/ding.mp3"))))
        await _wait_until(_recorded(recorder, 1))
    finally:
        await _run(worker.stop())

    (result,) = recorder.results
    assert result.lead_in_ms == 1500


@pytest.mark.parametrize(
    ("applied_ms", "note"),
    [(None, None), (0, None), (1500, "wake lead-in 1.5 s"), (250, "wake lead-in 0.25 s")],
)
async def test_lead_in_note_formats_the_silence_or_suppresses_it(
    applied_ms: int | None, note: str | None
) -> None:
    """Only a real lead-in produces a press-log note, in whole seconds.

    :param applied_ms: milliseconds the backend reported applying.
    :param note: the note the press log must carry, or ``None`` for no note.
    """
    assert _lead_in_note(applied_ms) == note


@pytest.mark.slow
async def test_shutdown_is_bounded_and_records_the_interrupted_job() -> None:
    """Shutdown gives the current chime the grace, then stops it and records it."""
    backend = _FakeBackend()
    backend.gate = asyncio.Event()
    recorder = _RecordingRecorder()
    worker = PlaybackWorker(backend, recorder)

    await _run(worker.start())
    await _run(worker.dispatch(_job(Path("/sounds/playing.mp3"), job_id="playing")))
    await _wait_until(_started(backend))

    started = time.monotonic()
    await asyncio.wait_for(worker.stop(), timeout=3.5)
    elapsed = time.monotonic() - started

    assert 1.9 <= elapsed <= 3.5
    assert backend.stop_playback_calls == 1
    assert _running(worker) is False
    # Exactly one result, compared in full: a job interrupted by shutdown is
    # recorded once, and a second record would be a bug this hides otherwise.
    assert len(recorder.results) == 1
    (interrupted,) = recorder.results
    assert interrupted.job_id == "playing"
    assert interrupted.button_id == 1
    assert interrupted.button_name == "Front Door"
    assert interrupted.outcome is PressOutcome.FAILED
    assert interrupted.latency_ms is None
    assert interrupted.duration_ms is None
    assert interrupted.code is None
    assert interrupted.detail is not None
    assert "grace" in interrupted.detail


async def test_restart_after_a_stopped_chime_still_plays_new_jobs() -> None:
    """A ``stop()`` mid-chime leaves its sentinel behind; the next ``start()`` clears it."""
    backend = _FakeBackend()
    backend.gate = asyncio.Event()
    recorder = _RecordingRecorder()
    worker = PlaybackWorker(backend, recorder, shutdown_grace=0.05)
    new_path = Path("/sounds/after-restart.mp3")

    await _run(worker.start())
    try:
        await _run(worker.dispatch(_job(Path("/sounds/blocked.mp3"), job_id="blocked")))
        await _wait_until(_started(backend))

        # The consumer is inside ``play()``, so the stop sentinel stays queued.
        await _run(worker.stop())
        assert _running(worker) is False
        assert worker.pending_count == 1

        await _run(worker.start())
        assert _running(worker) is True

        backend.gate = None
        await _run(worker.dispatch(_job(new_path, job_id="after-restart")))

        async def recorded_new_job() -> bool:
            return any(
                result.job_id == "after-restart" and result.outcome is PressOutcome.PLAYED
                for result in recorder.results
            )

        await _wait_until(recorded_new_job)
        assert backend.played[-1] == new_path
        assert _running(worker) is True
    finally:
        if backend.gate is not None:
            backend.gate.set()
        await _run(worker.stop())


async def test_a_broken_recorder_never_kills_the_worker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recorder that raises on every call is logged and the queue keeps playing.

    :param caplog: pytest log capture.
    """
    backend = _FakeBackend()
    recorder = _RecordingRecorder(error=RuntimeError("the press log database is locked"))
    worker = PlaybackWorker(backend, recorder)
    paths = [Path("/sounds/one.mp3"), Path("/sounds/two.mp3")]
    caplog.set_level(logging.ERROR, logger="chime_deck")

    await _run(worker.start())
    try:
        for index, path in enumerate(paths):
            await _run(worker.dispatch(_job(path, job_id=f"job-{index}")))
        await _wait_until(_counted(backend, len(paths)))

        assert backend.played == paths
        assert recorder.results == []
        assert _running(worker) is True
        assert "could not record" in caplog.text
    finally:
        await _run(worker.stop())


async def test_shutdown_discards_pending_jobs_and_records_each_once() -> None:
    """Jobs still in the queue are dropped, logged and recorded exactly once each."""
    backend = _FakeBackend()
    backend.gate = asyncio.Event()
    recorder = _RecordingRecorder()
    worker = PlaybackWorker(backend, recorder, shutdown_grace=0.05)

    await _run(worker.start())
    await _run(worker.dispatch(_job(Path("/sounds/playing.mp3"), job_id="playing")))
    await _wait_until(_started(backend))
    queued = [Path(f"/sounds/queued-{index}.mp3") for index in range(3)]
    for index, path in enumerate(queued):
        await _run(worker.dispatch(_job(path, job_id=f"queued-{index}")))
    assert worker.pending_count == 3

    await _run(worker.stop())

    discarded = [
        result
        for result in recorder.results
        if result.detail == "shutdown: discarded before playback"
    ]
    assert [result.job_id for result in discarded] == ["queued-0", "queued-1", "queued-2"]
    assert all(result.outcome is PressOutcome.FAILED for result in discarded)
    assert all(result.latency_ms is None for result in discarded)


async def test_lifecycle_is_guarded_and_idempotent() -> None:
    """Dispatch before start fails; start and stop are both idempotent."""
    backend = _FakeBackend()
    worker = PlaybackWorker(backend, _NullRecorder())

    with pytest.raises(AudioError, match="not running"):
        await _run(worker.dispatch(_job(Path("/sounds/one.mp3"))))

    await _run(worker.start())
    await _run(worker.start())
    assert _running(worker) is True
    assert worker.pending_count == 0
    running = [task for task in asyncio.all_tasks() if task.get_name() == "playback-worker"]
    assert len(running) == 1

    await _run(worker.stop())
    assert _running(worker) is False
    await _run(worker.stop())
    assert _running(worker) is False

    never_started = _FakeBackend()
    idle = PlaybackWorker(never_started, _NullRecorder())
    await _run(idle.stop())
    assert never_started.stop_calls == 1


async def test_stop_leaves_no_task_behind() -> None:
    """Shutdown leaves the event loop exactly as it found it."""
    current = asyncio.current_task()
    before = asyncio.all_tasks() - {current}
    backend = _FakeBackend()
    worker = PlaybackWorker(backend, _NullRecorder())

    await _run(worker.start())
    await _run(worker.dispatch(_job(Path("/sounds/one.mp3"), job_id="job-1")))
    await _wait_until(_counted(backend, 1))
    await _run(worker.stop())

    assert asyncio.all_tasks() - {current} == before


@pytest.mark.integration
async def test_playback_is_recorded_in_the_press_history(tmp_path: Path) -> None:
    """One press leaves exactly one row, revised from ``queued`` to ``played``.

    :param tmp_path: pytest temporary directory used for the database and sound.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        hub = StateHub(store, HealthRegistry(), flush_interval=_NEVER)
        try:
            button = await _seed_sound(store, tmp_path)
            backend = _FakeBackend()
            worker = PlaybackWorker(backend, PressLogRecorder(store))
            service = PressService(store, hub, worker)

            await _run(worker.start())
            try:
                result = await _run(service.press(button.slug))
                assert result.outcome is PressOutcome.QUEUED
                await _wait_until(_press_outcome(store, "played"))
            finally:
                await _run(worker.stop())

            entries = await store.recent_presses()
            assert len(entries) == 1
            entry = entries[0]
            assert entry.outcome == "played"
            assert entry.job_id is not None
            assert entry.latency_ms is not None
            assert entry.duration_ms is not None
            assert entry.button_id == button.id
            assert entry.pressed_at == result.pressed_at
        finally:
            await hub.stop()


@pytest.mark.integration
async def test_a_full_queue_records_one_failed_press(tmp_path: Path) -> None:
    """A press that cannot be queued becomes one ``failed`` row naming the reason.

    :param tmp_path: pytest temporary directory used for the database and sound.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        hub = StateHub(store, HealthRegistry(), flush_interval=_NEVER)
        try:
            button = await _seed_sound(store, tmp_path)
            backend = _FakeBackend()
            backend.gate = asyncio.Event()
            worker = PlaybackWorker(backend, PressLogRecorder(store), maxsize=1)
            service = PressService(store, hub, worker)

            await _run(worker.start())
            try:
                await _run(worker.dispatch(_job(Path("/sounds/blocker.mp3"), job_id="blocker")))
                await _wait_until(_started(backend))
                await _run(worker.dispatch(_job(Path("/sounds/queued.mp3"), job_id="queued")))
                assert worker.pending_count == 1

                result = await _run(service.press(button.slug))

                assert result.outcome is PressOutcome.FAILED
                assert result.code is PressCode.AUDIO_ERROR
                entries = await store.recent_presses()
                assert len(entries) == 1
                entry = entries[0]
                assert entry.outcome == "failed"
                assert entry.detail is not None
                assert "queue full" in entry.detail
                assert button.name in entry.detail
            finally:
                if backend.gate is not None:
                    backend.gate.set()
                await _run(worker.stop())
        finally:
            await hub.stop()


@pytest.mark.integration
async def test_the_press_row_exists_before_playback_starts(tmp_path: Path) -> None:
    """The row the worker revises is written before the chime is allowed to sound.

    :param tmp_path: pytest temporary directory used for the database and sound.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        hub = StateHub(store, HealthRegistry(), flush_interval=_NEVER)
        try:
            button = await _seed_sound(store, tmp_path)
            backend = _FakeBackend()
            release = asyncio.Event()
            observed: list[tuple[str | None, str]] = []

            async def on_play(path: Path) -> None:
                """Wait until the press returned, then inspect the press log.

                :param path: file the worker is about to play.
                """
                await asyncio.wait_for(release.wait(), timeout=_AWAIT_TIMEOUT_S)
                entries = await store.recent_presses()
                assert len(entries) == 1
                observed.append((entries[0].job_id, entries[0].outcome))

            backend.on_play = on_play
            worker = PlaybackWorker(backend, PressLogRecorder(store))
            service = PressService(store, hub, worker)

            await _run(worker.start())
            try:
                result = await _run(service.press(button.slug))
                assert result.outcome is PressOutcome.QUEUED
                entries = await store.recent_presses()
                assert len(entries) == 1
                job_id = entries[0].job_id
                release.set()

                await _wait_until(_counted(backend, 1))
                await _wait_until(_press_outcome(store, "played"))
            finally:
                release.set()
                await _run(worker.stop())

            assert job_id is not None
            assert observed == [(job_id, "queued")]
        finally:
            await hub.stop()


@pytest.mark.integration
async def test_successful_playback_keeps_the_press_detail(tmp_path: Path) -> None:
    """A ``played`` revision must not wipe the note the press path wrote.

    :param tmp_path: pytest temporary directory used for the database and sound.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        hub = StateHub(store, HealthRegistry(), flush_interval=_NEVER)
        try:
            button = await _seed_sound(store, tmp_path)
            await store.set_setting("audio.quiet_hours_enabled", True)
            await store.set_setting("audio.quiet_hours_start", "22:00")
            await store.set_setting("audio.quiet_hours_end", "07:00")
            await store.set_setting("audio.quiet_hours_volume", 30)

            backend = _FakeBackend()
            worker = PlaybackWorker(backend, PressLogRecorder(store))
            late = datetime(2026, 1, 1, 23, 30)
            service = PressService(store, hub, worker, clock=lambda: late)

            await _run(worker.start())
            try:
                result = await _run(service.press(button.slug))
                assert result.outcome is PressOutcome.QUEUED
                assert result.detail is not None
                assert "volume limited" in result.detail
                await _wait_until(_press_outcome(store, "played"))
            finally:
                await _run(worker.stop())

            (entry,) = await store.recent_presses()
            assert entry.outcome == "played"
            assert entry.detail == result.detail
        finally:
            await hub.stop()
