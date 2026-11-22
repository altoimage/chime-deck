"""An offline audio backend and the wiring helper built on top of it.

:class:`RecordingBackend` is a full :class:`~chime_deck.audio.AudioBackend`:
it records every :meth:`~RecordingBackend.play` call instead of touching a
sound card, it can be told to fail at :meth:`~RecordingBackend.start`, and it
can hold a chime "in flight" on an :class:`asyncio.Event` so a test can observe
the mid-playback state without a race.

:func:`build_offline_wiring` assembles a
:class:`~chime_deck.wiring.Wiring` in which *every* device is a fake. It
always injects :class:`~mqtt_fakes.RecordingSleep` and
:class:`~mqtt_fakes.FixedRandom` on purpose: a test that needs a pinned press
clock should not accidentally leave the real 0-2 s birth jitter in place, which
would make an otherwise deterministic test slow and order-dependent.

Owning ticket: HSD-dtpo.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from audio_hardware_fakes import RecordingSpawner
from mqtt_fakes import FakeBroker, FixedRandom, RecordingSleep

from chime_deck.audio import AudioBackend, AudioDevice, CommandSpawner
from chime_deck.wiring import Wiring

__all__ = ["RecordingBackend", "build_offline_wiring"]


class RecordingBackend:
    """An :class:`~chime_deck.audio.AudioBackend` that records instead of playing.

    ``started`` and ``stopped`` count real transitions, so a second ``start()``
    while playing changes nothing. ``start_error`` is set by a test to make
    :meth:`start` raise, which is how the "missing mpv" deployment state is
    simulated. ``start_failures`` is an ordered queue of exceptions instead: each
    :meth:`start` raises and removes the front one, which is how a test says
    "fail twice, then succeed"; it is consulted before ``start_error``. ``gate``
    is an optional event: when it is set, :meth:`play` records the call and then
    waits for it, holding the chime in flight for as long as the test wants.
    """

    def __init__(self) -> None:
        """Create a backend that has played nothing and started no device."""
        self.played: list[tuple[Path, int]] = []
        self.lead_ins: list[int] = []
        self.hints: list[str] = []
        self.started = 0
        self.stopped = 0
        self.start_timeouts: list[float | None] = []
        self.start_error: Exception | None = None
        self.start_failures: list[Exception] = []
        self.gate: asyncio.Event | None = None
        self._playing = False
        self._started = False
        self._devices = [
            AudioDevice(name="pipewire/offline-output", hostapi="pipewire", is_default=True)
        ]

    @property
    def playing(self) -> bool:
        """Return whether a recorded chime is still in flight."""
        return self._playing

    async def start(self, *, timeout_s: float | None = None) -> None:
        """Record the startup, or raise the configured error.

        Idempotent: a second call while started leaves the counters untouched.

        :param timeout_s: budget the caller offered for this start, in seconds.
        :raises Exception: the front of ``start_failures``, else the configured
            ``start_error``, when a test set one.
        """
        self.start_timeouts.append(timeout_s)
        if self.start_failures:
            raise self.start_failures.pop(0)
        if self.start_error is not None:
            raise self.start_error
        if self._started:
            return
        self._started = True
        self.started += 1

    async def stop(self) -> None:
        """Record the stop; safe to call on a backend that never started."""
        if not self._started:
            return
        self._started = False
        self.stopped += 1
        self._playing = False

    async def play(
        self,
        path: Path,
        *,
        volume: int,
        lead_in_ms: int = 0,
        when_done: Callable[[], None] | None = None,
    ) -> int:
        """Record one playback, optionally waiting on the test's gate.

        The lead-in is echoed back as "applied": this fake has no output device
        to wake, so whatever the caller asked for is what it reports.

        :param path: file that would have been played.
        :param volume: volume that would have been used.
        :param lead_in_ms: silence the worker asked to prepend, in milliseconds.
        :param when_done: completion callback, run exactly once when the recorded
            playback ends.
        :returns: the milliseconds of silence reported as applied.
        """
        self.played.append((path, volume))
        self.lead_ins.append(lead_in_ms)
        self._playing = True
        try:
            if self.gate is not None:
                await self.gate.wait()
        finally:
            self._playing = False
            if when_done is not None:
                with contextlib.suppress(Exception):
                    when_done()
        return lead_in_ms

    async def stop_playback(self) -> None:
        """Stop the in-flight chime by releasing the gate, if there is one."""
        self._playing = False
        if self.gate is not None:
            self.gate.set()

    async def set_device(self, hint: str) -> None:
        """Record a requested device instead of selecting one.

        :param hint: device hint a real player would resolve.
        """
        self.hints.append(hint)

    async def list_devices(self) -> list[AudioDevice]:
        """Return one plausible output without asking the host."""
        return list(self._devices)


def build_offline_wiring(
    *,
    broker: FakeBroker,
    backend: AudioBackend,
    press_clock: Callable[[], datetime] | None = None,
    hardware_spawner: CommandSpawner | None = None,
) -> Wiring:
    """Assemble a wiring whose every device is a fake.

    The sleep and randomness are always injected, so adding a pinned press clock
    can never bring the real birth jitter back by omission.

    :param broker: in-memory broker that supplies the client factory.
    :param backend: speaker the playback worker starts.
    :param press_clock: source of the press timestamp, or ``None`` for aware UTC.
    :param hardware_spawner: fake mixer commands; ``None`` builds a fresh
        :class:`~audio_hardware_fakes.RecordingSpawner`, because a test that
        injects this wiring explicitly bypasses the conftest factory and must
        still never execute a real ``amixer`` (HARD RULE 4).
    :return: the wiring to hand to ``create_app(config, wiring=...)``.
    """
    return Wiring(
        audio_backend=backend,
        hardware_spawner=(RecordingSpawner() if hardware_spawner is None else hardware_spawner),
        mqtt_client_factory=broker.factory,
        mqtt_sleep=RecordingSleep(),
        mqtt_rng=FixedRandom(0.0),
        press_clock=press_clock,
    )
