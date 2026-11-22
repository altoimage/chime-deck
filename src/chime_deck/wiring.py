"""The production wiring: the one press path, one audio worker, one MQTT generation.

Owning ticket: HSD-dtpo.

:class:`ChimePipeline` is what the application lifespan starts and stops, and
this module is the ONE place the press path is assembled:

* **One press path.** The MQTT command topic and the UI's test action
  (``POST /api/buttons/{id}/press``) share the single
  :class:`~chime_deck.press.PressService` built here, so dedupe, quiet hours,
  volume, speech and the press log behave identically whichever source pressed.
  A per-generation service would reset the dedupe window, so the same instance is
  injected into every MQTT generation.
* **Non-fatal subsystems.** A missing mpv/libmpv, a broker that is down and a
  broken speech factory each degrade one component: the app still serves, the
  press still produces an honest ``press_log`` row, and health says what is
  wrong. Constructing the pipeline opens nothing; only :meth:`ChimePipeline.start`
  touches a device.
* **What reloads and what does not.** ``mqtt.*`` and ``device.name`` changes
  rebuild the bridge in place, retracting the old discovery payloads through the
  old bridge first — except for a rename, which retracts nothing because the
  display name changes no identifier (HSD-vwdy).
  ``audio.*`` values are re-read where they are used: volume and the quiet
  window per press, ``audio.start_timeout_s`` before every start attempt. A
  running worker keeps the backend it was built with, so a ``device_hint``
  change reaches the speaker on the next start, not through this re-arm. What
  an ``audio.*`` write does do is re-arm the recovery ladder for a speaker that
  never started, so the start is retried at once instead of waiting out a
  backoff step — and that retry really applies a new start budget because the
  budget is re-read before every attempt.
* **A rebuild lengthens a settings write.** The reload runs inside the publish
  that follows the settings transaction, before the HTTP response, so the empty
  payloads travel through the still-open bridge. One ``PATCH /api/settings`` that
  changes ``mqtt.*`` (or ``device.name``) therefore waits for the bridge's own
  bounded stop budget, worst case about 11 s (two 5 s shutdown waits plus the 1 s
  availability flush); it is usually milliseconds.

The whole module is a seam: :func:`default_wiring` is a factory, not a constant,
so a test can replace ``chime_deck.app.default_wiring`` and the application
under test never opens a socket or a sound card.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from .audio import (
    DEFAULT_START_TIMEOUT_S,
    AudioBackend,
    AudioBackendUnavailable,
    AudioDevice,
    CommandSpawner,
    HardwareVolume,
    HardwareVolumeStatus,
    MpvBackend,
    warm_library,
)
from .config import ServerConfig
from .health import HealthRegistry, HealthStatus
from .hub import StateHub
from .mqtt import (
    DEFAULT_PUBLISH_FLUSH_TIMEOUT_S,
    DEFAULT_SHUTDOWN_TIMEOUT_S,
    ClientFactory,
    MqttBridge,
    MqttConfigurationError,
    MqttDiscovery,
    differs_beyond_display_name,
    load_mqtt_settings,
)
from .playback import HubPlaybackRecorder, PlaybackWorker
from .press import PressService, SpeechSource
from .selection import NO_SELECTION, SelectionLookup
from .store import ConfigStore
from .store.events import ConfigEvent, ConfigEventName
from .store.settings_registry import AUDIO_BACKEND_CHOICES

__all__ = [
    "ChimePipeline",
    "Wiring",
    "default_wiring",
    "resolve_backend",
]

_MQTT_KEY_PREFIX: Final = "mqtt."
"""Settings key prefix that makes a stored change rebuild the bridge."""

_DEVICE_NAME_KEY: Final = "device.name"
"""The one non-``mqtt.*`` key whose stored change rebuilds the bridge.

It changes no topic and no unique id, but it does change a discovery payload (the
``device`` block's name, HSD-vwdy), so the generation must be rebuilt to publish
it. It is deliberately not spelled ``mqtt.device_name``: the name belongs to the
unit, not to the broker.
"""

_REBUILD_WORST_CASE_S: Final = 2 * DEFAULT_SHUTDOWN_TIMEOUT_S + DEFAULT_PUBLISH_FLUSH_TIMEOUT_S
"""Longest a settings-generation rebuild can add to one settings write, in seconds."""

_DEFAULT_WAKE_LEAD_IN_MS: Final = 1500
"""Fallback for ``audio.wake_lead_in_ms``; mirrors the registry default."""

_AUDIO_KEY_PREFIX: Final = "audio."
"""Settings key prefix that re-arms a stopped audio start."""

_HARDWARE_VOLUME_KEYS: Final = frozenset(
    {
        "audio.hardware_volume",
        "audio.muted",
        "audio.hardware_control",
        "audio.device_hint",
    }
)
"""The ``audio.*`` keys that change the device's own volume control (cd-qul9).

The first three change what :class:`~chime_deck.audio.HardwareVolume` writes; a
``device_hint`` change moves the output, and the control belongs to the output,
so it re-applies the stored level to the new device as well. Every other
``audio.*`` key keeps its old behaviour: a running speaker is left alone."""

_AUDIO_START_MAX_ATTEMPTS: Final = 5
"""Total start attempts, the startup one included, before the ladder gives up."""

_AUDIO_START_BACKOFF_INITIAL_S: Final = 1.0
"""Delay before the first retry, in seconds; it doubles after every failure."""

_AUDIO_START_BACKOFF_MAX_S: Final = 16.0
"""Cap on one backoff step, in seconds, so a retry is never minutes away."""

_AUDIO_START_RETRY_BUDGET_S: Final = 60.0
"""Longest the whole ladder may run, attempts included, in seconds."""

_AUDIO_TASK_STOP_TIMEOUT_S: Final = 5.0
"""Longest shutdown waits for one audio background task to unwind, in seconds."""


@dataclass(frozen=True, slots=True)
class Wiring:
    """Injectable substitutes for the devices the lifespan would otherwise open.

    Every field is empty in :func:`default_wiring`, so replacing that one object
    makes the whole application offline — no broker, no sound card and no real
    clock — instead of patching five modules.
    """

    #: Speaker to play through; ``None`` builds one from ``audio.backend``.
    audio_backend: AudioBackend | None = None
    #: Seam for the device's own volume commands (``amixer``/``pactl``);
    #: ``None`` uses :data:`~chime_deck.audio.DEFAULT_SPAWN`, and the test suite
    #: injects a recording fake so no test can execute a real mixer.
    hardware_spawner: CommandSpawner | None = None
    #: Builds the paho client; ``None`` means the bridge's own factory.
    mqtt_client_factory: ClientFactory | None = None
    #: Delay used for the MQTT birth jitter; the production value is the real sleep.
    mqtt_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    #: Randomness source for the birth jitter.
    mqtt_rng: random.Random | None = None
    #: Delay between audio start attempts; the production value is the real sleep.
    audio_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    #: Monotonic clock that bounds the audio recovery ladder.
    audio_clock: Callable[[], float] = time.monotonic
    #: Source of the press timestamp; ``None`` means aware UTC.
    press_clock: Callable[[], datetime] | None = None


def default_wiring() -> Wiring:
    """Return the production wiring, which opens the real devices when started.

    This is a factory rather than a module constant on purpose: it is ONE seam a
    test can replace (``chime_deck.app.default_wiring``) to make every
    application it builds fully offline, including the lifespan's own devices.

    :return: a wiring whose every substitute is empty.
    """
    return Wiring()


def resolve_backend(backend_setting: object, device_hint: object) -> AudioBackend:
    """Return the speaker described by the stored ``audio.*`` settings.

    Total on purpose: a stored value this build cannot honour yields a backend
    that fails at :meth:`start`, never an exception here and never a silent
    substitution of mpv. "The setting says sounddevice but we played through mpv
    anyway" would be a lie in the settings page and in the health status.

    A non-``mpv`` value can no longer be selected in the settings page or
    written through it: the registry's ``AUDIO_BACKEND_CHOICES`` rejects it. The
    only way to reach this branch is a hand-edited or pre-migration database,
    and startup repairs that row (see
    :meth:`ChimePipeline._repair_audio_backend`). The branch stays as defence in
    depth so such a row still fails honestly instead of silently playing mpv.

    :param backend_setting: stored ``audio.backend``; ``"mpv"`` is the only
        implemented value, and anything else yields an unavailable backend.
    :param device_hint: stored ``audio.device_hint``; a non-string is ignored.
    :return: an :class:`~chime_deck.audio.MpvBackend` for ``"mpv"``, otherwise
        an unavailable backend whose reason names the stored value.
    """
    if backend_setting == "mpv":
        hint = device_hint if isinstance(device_hint, str) else ""
        return MpvBackend(device_hint=hint)
    reason = (
        f"audio.backend is set to {backend_setting!r}, which this build does not implement — "
        "set audio.backend to 'mpv' to get sound"
    )
    return _UnavailableBackend(reason)


def _attempts_label(count: int) -> str:
    """Return "1 attempt" or "3 attempts" for a log line or a health detail.

    :param count: how many start attempts were made, the boot attempt included.
    :return: the pluralised label.
    """
    return f"{count} attempt{'' if count == 1 else 's'}"


def _audio_failure_detail(exc: BaseException, *, recovery: str) -> str:
    """Return the actionable health detail for a backend that would not start.

    :param exc: what :meth:`AudioBackend.start` raised.
    :param recovery: what happens next, said in the caller's terms (a retry
        ladder still running, or a ladder that gave up).
    :return: the one-line detail stored in the ``audio`` health status.
    """
    reason = str(exc).strip() or type(exc).__name__
    return (
        f"the speaker could not start: {reason} — check that libmpv is installed, that "
        f"audio.backend is 'mpv' and that audio.device_hint names an output; {recovery}"
    )


class _UnavailableBackend:
    """An :class:`~chime_deck.audio.AudioBackend` for a backend this build lacks.

    Every operation that would need a player raises
    :class:`~chime_deck.audio.AudioBackendUnavailable` carrying the reason, so
    the failure surfaces once, with the stored value quoted, instead of as a
    mysterious missing sound.
    """

    def __init__(self, reason: str) -> None:
        """Record why this backend cannot play.

        :param reason: actionable text naming the stored setting value.
        """
        self._reason = reason

    def __repr__(self) -> str:
        """Return a representation carrying the reason."""
        return f"<{type(self).__name__} {self._reason}>"

    @property
    def reason(self) -> str:
        """Return the actionable reason this backend cannot play."""
        return self._reason

    @property
    def playing(self) -> bool:
        """Return whether a file is playing; an unavailable backend never plays."""
        return False

    async def start(self, *, timeout_s: float | None = None) -> None:
        """Refuse to start.

        The budget is accepted and ignored: no length of time makes a backend
        this build does not implement play.

        :param timeout_s: budget the caller offered for the start, in seconds.
        :raises AudioBackendUnavailable: always, with this backend's reason.
        """
        raise AudioBackendUnavailable(self._reason)

    async def stop(self) -> None:
        """Do nothing: there is no player to release."""

    async def play(
        self,
        path: Path,
        *,
        volume: int,
        lead_in_ms: int = 0,
        when_done: Callable[[], None] | None = None,
    ) -> int:
        """Refuse to play.

        :param path: file that would have been played.
        :param volume: volume that would have been used.
        :param lead_in_ms: lead-in that would have been asked for.
        :param when_done: completion callback that is never run.
        :raises AudioBackendUnavailable: always, with this backend's reason.
        """
        raise AudioBackendUnavailable(self._reason)

    async def stop_playback(self) -> None:
        """Do nothing: nothing is playing."""

    async def set_device(self, hint: str) -> None:
        """Refuse to select a device.

        :param hint: device hint that would have been selected.
        :raises AudioBackendUnavailable: always, with this backend's reason.
        """
        raise AudioBackendUnavailable(self._reason)

    async def list_devices(self) -> list[AudioDevice]:
        """Refuse to enumerate devices.

        :raises AudioBackendUnavailable: always, with this backend's reason.
        """
        raise AudioBackendUnavailable(self._reason)


class ChimePipeline:
    """Own the audio worker, the shared press service and the current MQTT generation.

    Construction is free of I/O: the application factory builds one per process
    and the lifespan starts it, so importing or building the app never opens a
    device or a socket.
    """

    def __init__(
        self,
        *,
        store: ConfigStore,
        hub: StateHub,
        health: HealthRegistry,
        config: ServerConfig,
        wiring: Wiring,
        selection: SelectionLookup = NO_SELECTION,
    ) -> None:
        """Prepare the pipeline; nothing runs until :meth:`start` is awaited.

        :param store: configuration store read for settings, buttons and the press log.
        :param hub: broadcast hub the played/failed flip is pushed through.
        :param health: registry written for the ``audio`` and ``mqtt`` components.
        :param config: resolved server configuration; its host and port (never
            the ``server.*`` settings) are published in the discovery payload.
        :param wiring: injectable devices; :func:`default_wiring` in production.
        :param selection: the volatile D29 tile selection the discovery service
            reads to republish the selection sensors' state.

        The logger is ``logging.getLogger(__name__)``, created last.
        """
        self._store = store
        self._hub = hub
        self._health = health
        self._config = config
        self._wiring = wiring
        self._selection = selection
        self._press: PressService | None = None
        self._worker: PlaybackWorker | None = None
        self._bridge: MqttBridge | None = None
        self._discovery: MqttDiscovery | None = None
        self._mqtt_generations = 0
        self._unsubscribe: Callable[[], None] | None = None
        self._audio_recovery: asyncio.Task[None] | None = None
        self._hardware_task: asyncio.Task[None] | None = None
        self._hardware = HardwareVolume(
            store=self._store,
            backend=lambda: self.backend,
            spawner=self._wiring.hardware_spawner,
        )
        # The generation retires a superseded ladder at its next check instead of
        # cancelling one that is mid-attempt, which would drop a half-built player.
        self._audio_generation = 0
        self._audio_warm_up: asyncio.Task[None] | None = None
        # An instance-level lock, not a module global: two pipelines in one
        # process (two apps in one test session) must never serialise each other.
        self._reload_lock = asyncio.Lock()
        self._logger = logging.getLogger(__name__)

    @property
    def press(self) -> PressService:
        """Return the shared press service.

        :raises RuntimeError: if the pipeline has not been started, because a
            press must not be silently accepted into nothing.
        """
        if self._press is None:
            raise RuntimeError(
                "the chime pipeline has not been started, so there is no press service yet"
            )
        return self._press

    @property
    def playback(self) -> PlaybackWorker:
        """Return the playback worker.

        :raises RuntimeError: if the pipeline has not been started.
        """
        if self._worker is None:
            raise RuntimeError(
                "the chime pipeline has not been started, so there is no playback worker yet"
            )
        return self._worker

    @property
    def bridge(self) -> MqttBridge | None:
        """Return the current MQTT bridge, or ``None`` when no generation is live."""
        return self._bridge

    @property
    def backend(self) -> AudioBackend | None:
        """Return the started backend, or ``None`` before :meth:`start` / after :meth:`stop`.

        This never raises: the device-discovery route and the audio probe both
        have to answer honestly in that window ("the audio engine has not
        started yet") instead of a 500. A caller must never construct its own
        backend to fill the gap — a second :class:`~chime_deck.audio.MpvBackend`
        would open a second libmpv player against the same sound card.
        """
        worker = self._worker
        return None if worker is None else worker.backend

    @property
    def discovery(self) -> MqttDiscovery | None:
        """Return the current discovery service, or ``None`` when no generation is live."""
        return self._discovery

    def notify_selection(self, entity_id: str, selected: bool) -> None:
        """Publish one D29 toggle to the current MQTT generation. Never raises.

        The one seam per toggle: both consumers — the websocket hub and the MQTT
        publisher — are fed from
        :meth:`~chime_deck.selection.SelectionService.set_on_change`, so the
        response, the frame and the broker can never disagree. The generation is
        rebuilt on an ``mqtt.*`` or ``device.name`` change, so forwarding through
        this method always reaches the CURRENT discovery service instead of a
        stale reference.

        A toggle is a user tap: a broker hiccup must not fail it, so a generation
        that is not live is a DEBUG no-op and a failing publish is already
        swallowed by :meth:`MqttDiscovery.on_selection_changed`.

        :param entity_id: Home Assistant entity id whose selection changed.
        :param selected: the new value, ``True`` when the tile is selected.
        """
        discovery = self._discovery
        if discovery is None:
            self._logger.debug(
                "no MQTT generation is live; not publishing the selection of %s", entity_id
            )
            return
        discovery.on_selection_changed(entity_id, selected)

    @property
    def mqtt_generations(self) -> int:
        """Return how many MQTT bridges have been started, startup one included."""
        return self._mqtt_generations

    async def start(self, speech: SpeechSource | None) -> None:
        """Start the audio worker, the press path and the first MQTT generation.

        Total by construction: a backend that cannot start, settings that cannot
        be read and a bridge that fails all end in a health status and a WARNING,
        never in an exception that would stop the application from serving. A
        backend that cannot start is retried on a bounded backoff in the
        background, so a transient failure does not become permanent.

        The audio-library warm-up is awaited first, because loading libmpv is the
        bulk of a first start and doing it here keeps that cost out of the start
        budget. The audio-backend repair runs next, before any backend is built
        and before the settings watcher is subscribed, so the write it may
        perform cannot trigger a settings reload of the generation being started.

        :param speech: speech source for TTS buttons (HSD-bw8h), or ``None`` when
            no engine could be prepared; a TTS press then fails ``tts_unavailable``.
        """
        await self._wait_for_warm_up()
        await self._repair_audio_backend()
        self._logger.info("starting the audio playback worker")
        backend = self._wiring.audio_backend
        if backend is None:
            backend = await self._build_backend()
        wake_lead_in_ms = await self._read_wake_lead_in_ms()
        worker = PlaybackWorker(
            backend,
            HubPlaybackRecorder(self._store, self._hub),
            wake_lead_in_ms=wake_lead_in_ms,
        )
        self._worker = worker
        self._logger.info(
            "a chime that follows an idle speaker starts with %d ms of silence",
            wake_lead_in_ms,
        )
        # Built once, shared by MQTT discovery and the UI route: one dedupe
        # window and one press log, whichever source pressed.
        self._press = PressService(
            self._store,
            self._hub,
            worker,
            speech=speech,
            clock=self._wiring.press_clock,
            sounds_dir=self._config.resolved_sounds_dir,
        )
        self._logger.info("the press path is ready (MQTT and the UI test action share it)")
        await self._start_audio(worker)
        await self._start_mqtt()
        self._unsubscribe = self._store.events.subscribe(self._on_settings_changed)
        self._logger.info(
            "watching for settings changes: audio.* re-arms the start retry for a speaker "
            "that is not running, mqtt.* and device.name rebuild the bridge in place"
        )

    async def stop(self) -> None:
        """Stop the settings watch, the MQTT generation and then the audio worker.

        The press service is deliberately kept while the worker stops: a press
        that arrives during shutdown must still write an honest ``failed`` row
        (``audio_error``) rather than answer HTTP 500.

        The audio recovery ladder and the library warm-up are retired first: the
        generation is bumped so a ladder between attempts exits at its next
        check, and what is still waiting is cancelled under a bound before the
        worker stops, so neither can start a backend behind the shutdown.
        """
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()
        await self._teardown_mqtt(retract=False)
        self._audio_generation += 1
        await self._cancel_bounded(self._audio_recovery, what="audio recovery")
        self._audio_recovery = None
        await self._cancel_bounded(self._hardware_task, what="hardware volume")
        self._hardware_task = None
        await self._cancel_bounded(self._audio_warm_up, what="audio warm-up")
        self._audio_warm_up = None
        worker, self._worker = self._worker, None
        if worker is not None:
            await worker.stop()
        self._logger.info("the chime pipeline stopped")

    def warm_up_audio(self) -> None:
        """Load python-mpv in the background so a start does not pay the cold dlopen.

        Idempotent, and never raises: a host without libmpv keeps the actionable
        failure for :meth:`start`, which is where the user-visible error belongs.
        This only arms the task; :meth:`start` awaits it, bounded.
        """
        if self._audio_warm_up is not None:
            return
        self._audio_warm_up = asyncio.create_task(
            asyncio.to_thread(warm_library), name="mpv-warm-up"
        )

    async def _wait_for_warm_up(self) -> None:
        """Await the library warm-up under a bound, then forget it.

        Total by construction: the import has no user-visible failure of its own
        — ``start`` reports a missing libmpv — so a warm-up that fails or
        outlasts the start budget is logged at DEBUG and startup carries on.
        Only a cancellation of the startup itself propagates: that is not the
        warm-up failing.
        """
        task, self._audio_warm_up = self._audio_warm_up, None
        if task is None:
            return
        budget = await self._read_start_timeout_s()
        try:
            async with asyncio.timeout(budget):
                await task
        except Exception as exc:
            self._logger.debug(
                "the audio library warm-up did not finish within %g s (%s: %s); startup carries on",
                budget,
                type(exc).__name__,
                exc,
            )

    async def _cancel_bounded(self, task: asyncio.Task[None] | None, *, what: str) -> None:
        """Cancel one background task and wait a bounded time for it to unwind.

        Cancelling is best effort on purpose: a task stuck in a bounded start is
        released by that start's own budget, so shutdown waits a fixed short
        time and then carries on rather than blocking on a device.

        :param task: task to cancel, or ``None`` when it was never armed.
        :param what: name used in the DEBUG line, e.g. ``"audio recovery"``.
        """
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, timeout=_AUDIO_TASK_STOP_TIMEOUT_S)
        if not task.done():
            self._logger.debug("the %s task did not unwind cleanly before shutdown", what)

    async def _start_audio(self, worker: PlaybackWorker) -> None:
        """Start the speaker once, then keep trying on a bounded backoff.

        The first attempt is awaited: a healthy device is ready before the first
        press, and that attempt is the ladder's first attempt anyway. A failure
        must not hold the UI and the MQTT bridge back — they start in the same
        step — so the remaining attempts run as a background task: the
        application serves, health says ``error``, and every press is recorded
        as ``audio_error`` until the speaker answers.

        :param worker: playback worker whose backend to start.
        """
        try:
            await worker.start(timeout_s=await self._read_start_timeout_s())
        except Exception as exc:
            # No traceback: a missing mpv is an expected deployment state, not a
            # crash, and the health detail already names the fix.
            self._health.set(
                "audio",
                HealthStatus.ERROR,
                _audio_failure_detail(
                    exc,
                    recovery=(
                        "the service keeps retrying in the background for up to "
                        f"{_AUDIO_START_RETRY_BUDGET_S:g} s, and presses are recorded as "
                        "audio_error until the speaker starts"
                    ),
                ),
            )
            self._logger.warning(
                "audio is unavailable, so a press cannot play: %s — retrying in the "
                "background, up to %d more attempts within %g s",
                exc,
                _AUDIO_START_MAX_ATTEMPTS - 1,
                _AUDIO_START_RETRY_BUDGET_S,
            )
            # The inline attempt above was the ladder's first one: number the
            # retries from there so the journal counts the whole run.
            self._arm_audio_recovery(worker, wait_first=True, attempts_so_far=1)
        else:
            self._health.set("audio", HealthStatus.READY)
            self._logger.info("audio playback is ready")
            # The measured bug of cd-qul9: a device whose own control sits at 43%
            # makes a UI 100% audibly half volume. Applying the stored value here
            # is what replaces the OS's arbitrary level at boot. It never raises
            # and a mixer failure changes no health status, because a cosmetic
            # volume problem must not make the doorbell look dead.
            await self._reapply_hardware()

    def _arm_audio_recovery(
        self, worker: PlaybackWorker, *, wait_first: bool, attempts_so_far: int
    ) -> None:
        """Replace the recovery ladder with a fresh one.

        The previous ladder is retired by generation rather than cancelled: a
        cancel landing inside ``start()`` would drop a half-built player on the
        event loop, which is exactly what :class:`MpvBackend` forbids. A retired
        ladder exits at its next check and cannot double-start, because
        :meth:`PlaybackWorker.start` returns at once when the worker already
        runs.

        :param worker: playback worker to start.
        :param wait_first: sleep one backoff step before the first attempt — the
            boot case, where the failure has just happened; ``False`` when the
            caller has a reason to try again immediately, such as a settings
            change that may have fixed the device.
        :param attempts_so_far: start attempts already made by the caller, so the
            log numbers them as a run and not per ladder.
        """
        self._audio_generation += 1
        self._audio_recovery = asyncio.create_task(
            self._recover_audio(worker, wait_first=wait_first, first_attempt=attempts_so_far + 1),
            name="audio-recovery",
        )

    async def _recover_audio(
        self, worker: PlaybackWorker, *, wait_first: bool, first_attempt: int
    ) -> None:
        """Retry the speaker start on a bounded exponential backoff.

        Bounded twice over — at most :data:`_AUDIO_START_MAX_ATTEMPTS` attempts
        and at most :data:`_AUDIO_START_RETRY_BUDGET_S` seconds — so a
        permanently broken device stops being retried, and every attempt costs
        the device one bounded start. The budget is read again before every
        attempt, so a changed ``audio.start_timeout_s`` takes effect without a
        restart. On success health flips back to ``ready`` at INFO, naming how
        many attempts it took; on exhaustion it stays ``error`` with a WARNING
        that says so.

        Attempt numbers continue from the caller's count, so a ladder armed
        after a failed boot attempt reports "attempt 2 of 5" and "recovered after
        2 attempts" rather than pretending the boot attempt never happened.

        :param worker: playback worker to start.
        :param wait_first: sleep one backoff step before the first attempt.
        :param first_attempt: number this ladder gives its first attempt, which
            is the caller's attempt count plus one.
        """
        generation = self._audio_generation
        started = self._wiring.audio_clock()
        delay = _AUDIO_START_BACKOFF_INITIAL_S
        attempts = first_attempt - 1
        last_error: BaseException | None = None
        while attempts < _AUDIO_START_MAX_ATTEMPTS:
            if generation != self._audio_generation:
                return
            if attempts or wait_first:
                # Checked before the wait so a spent budget does not buy one
                # more of them, and again afterwards because the wait may have
                # spent it.
                if self._wiring.audio_clock() - started >= _AUDIO_START_RETRY_BUDGET_S:
                    break
                await self._wiring.audio_sleep(delay)
                delay = min(delay * 2, _AUDIO_START_BACKOFF_MAX_S)
            if self._wiring.audio_clock() - started >= _AUDIO_START_RETRY_BUDGET_S:
                break
            attempts += 1
            try:
                await worker.start(timeout_s=await self._read_start_timeout_s())
            except Exception as exc:
                last_error = exc
                self._logger.warning(
                    "audio start attempt %d of %d failed: %s; retrying in %g s",
                    attempts,
                    _AUDIO_START_MAX_ATTEMPTS,
                    exc,
                    delay,
                )
            else:
                self._health.set("audio", HealthStatus.READY)
                self._logger.info(
                    "audio recovered after %s and %g s",
                    _attempts_label(attempts),
                    self._wiring.audio_clock() - started,
                )
                # A recovered speaker is a (re)started output: re-apply the stored
                # device level, exactly like the boot path, so a DAC that came
                # back at its own default is brought to the app's value.
                await self._reapply_hardware()
                return
        if last_error is None:
            return
        elapsed = self._wiring.audio_clock() - started
        self._health.set(
            "audio",
            HealthStatus.ERROR,
            _audio_failure_detail(
                last_error,
                recovery=(
                    f"the service gave up after {_attempts_label(attempts)} in {elapsed:g} s — "
                    "fix the cause above and restart the service; presses are recorded as "
                    "audio_error until the speaker starts"
                ),
            ),
        )
        self._logger.warning(
            "giving up on the speaker after %s in %g s: %s — presses are recorded "
            "as audio_error until the service is restarted after the cause is fixed",
            _attempts_label(attempts),
            elapsed,
            last_error,
        )

    async def _repair_audio_backend(self) -> None:
        """Write the only implemented backend back over an unsupported stored value.

        ``"sounddevice"`` was once offered by the settings page but no such
        backend ever shipped. The registry now rejects it, but a database written
        by an older build still contains it, and ``get_setting`` returns stored
        values without re-validating. This is a write-back migration rather than
        a read-path alias on purpose: the settings page must not keep saying the
        speaker is ``sounddevice`` while chimes play through mpv, so the stored
        row is corrected once, at startup, and every later reader sees the truth.

        Total by construction: a store failure logs a WARNING and returns, never
        raising out of startup. An already supported value is left untouched (no
        write, no log), so a healthy database gains no settings event and no
        spurious timestamp change.
        """
        try:
            stored = await self._store.get_setting("audio.backend")
        except Exception as exc:
            self._logger.warning(
                "audio.backend could not be read, so it was left unchanged: %s", exc
            )
            return
        if stored in AUDIO_BACKEND_CHOICES:
            return
        replacement = AUDIO_BACKEND_CHOICES[0]
        try:
            await self._store.set_setting("audio.backend", replacement)
        except Exception as exc:
            self._logger.warning(
                "audio.backend is set to %r, which this build does not implement, and "
                "replacing it with %r failed (%s) — set audio.backend to %r in Settings "
                "and restart to get sound",
                stored,
                replacement,
                exc,
                replacement,
            )
            return
        self._logger.warning(
            "audio.backend is set to %r, which this build does not implement — replaced it "
            "with %r; chimes now play through mpv",
            stored,
            replacement,
        )

    async def _build_backend(self) -> AudioBackend:
        """Build the speaker from the stored ``audio.*`` settings.

        A store read that fails must not stop startup, so it yields an
        unavailable backend carrying the reason; the worker then reports
        ``audio_error`` per press and health says why.

        :return: the configured backend, or an unavailable one with a reason.
        """
        try:
            backend_setting = await self._store.get_setting("audio.backend")
            device_hint = await self._store.get_setting("audio.device_hint")
        except Exception as exc:
            reason = (
                f"the audio settings could not be read: {exc} — fix the configuration store "
                "and restart; presses are recorded as audio_error until then"
            )
            self._logger.warning("%s", reason)
            return _UnavailableBackend(reason)
        backend = resolve_backend(backend_setting, device_hint)
        if isinstance(backend, _UnavailableBackend):
            self._logger.warning("%s", backend.reason)
        return backend

    async def _read_wake_lead_in_ms(self) -> int:
        """Read ``audio.wake_lead_in_ms``, falling back to the default on any doubt.

        Total by construction, like :meth:`_build_backend`: a store read that
        fails or a stored value that is not a bare integer logs a WARNING and
        yields :data:`_DEFAULT_WAKE_LEAD_IN_MS`, and a negative value yields
        ``0``. A bad setting must leave the chime playable, not stop the worker.

        ``bool`` counts as a wrong type on purpose: it is an ``int`` to Python,
        but ``True`` milliseconds of silence is a typo, not a configuration.

        :return: milliseconds of silence to prepend after an idle period.
        """
        try:
            value = await self._store.get_setting("audio.wake_lead_in_ms")
        except Exception as exc:
            self._logger.warning(
                "audio.wake_lead_in_ms could not be read (%s); using %d ms",
                exc,
                _DEFAULT_WAKE_LEAD_IN_MS,
            )
            return _DEFAULT_WAKE_LEAD_IN_MS
        if isinstance(value, bool) or not isinstance(value, int):
            self._logger.warning(
                "audio.wake_lead_in_ms is not an integer (%r); using %d ms",
                value,
                _DEFAULT_WAKE_LEAD_IN_MS,
            )
            return _DEFAULT_WAKE_LEAD_IN_MS
        return max(0, value)

    async def _read_start_timeout_s(self) -> float:
        """Read ``audio.start_timeout_s``, falling back to the default on any doubt.

        Total by construction, like :meth:`_read_wake_lead_in_ms`: a store read
        that fails or a stored value that is not a bare integer logs a WARNING
        and yields :data:`DEFAULT_START_TIMEOUT_S`. ``get_setting`` returns a
        stored row without re-validating it, so a hand-edited database can hold
        a value outside the registry's ``10..120`` range; that is clamped with a
        WARNING rather than trusted, because too small makes a start fail on
        this hardware and too large would outlast the ladder that owns it.

        ``bool`` counts as a wrong type on purpose: it is an ``int`` to Python,
        but ``True`` seconds is a typo, not a configuration.

        :return: the budget for one start attempt, in seconds.
        """
        try:
            value = await self._store.get_setting("audio.start_timeout_s")
        except Exception as exc:
            self._logger.warning(
                "audio.start_timeout_s could not be read (%s); using %g s",
                exc,
                DEFAULT_START_TIMEOUT_S,
            )
            return DEFAULT_START_TIMEOUT_S
        if isinstance(value, bool) or not isinstance(value, int):
            self._logger.warning(
                "audio.start_timeout_s is not an integer (%r); using %g s",
                value,
                DEFAULT_START_TIMEOUT_S,
            )
            return DEFAULT_START_TIMEOUT_S
        clamped = min(max(value, 10), 120)
        if clamped != value:
            self._logger.warning(
                "audio.start_timeout_s is %d s, outside the 10-120 s range; using %d s",
                value,
                clamped,
            )
        return float(clamped)

    async def _start_mqtt(self) -> None:
        """Build, wire and start one MQTT generation.

        Never raises: an unusable ``mqtt.*`` configuration, a bridge that cannot
        be built and a discovery publish that fails each leave the application
        serving with an ``mqtt`` health status that says what happened.
        """
        self._logger.info("starting the MQTT bridge")
        bridge: MqttBridge | None = None
        discovery: MqttDiscovery | None = None
        try:
            settings = await load_mqtt_settings(self._store)
            bridge = MqttBridge(
                settings=settings,
                health=self._health,
                client_factory=self._wiring.mqtt_client_factory,
                sleep=self._wiring.mqtt_sleep,
                rng=self._wiring.mqtt_rng,
            )
            # The wiring order is load-bearing: the seams must be installed before
            # start() can deliver a connect, or the first birth republish is lost.
            discovery = MqttDiscovery(
                bridge=bridge,
                buttons=self._store,
                press=self.press,
                bus=self._store.events,
                server_host=self._config.host,
                server_port=self._config.port,
                entities=self._store,
                selection=self._selection,
            )
            bridge.set_on_birth(discovery.on_birth)
            bridge.set_on_message(discovery.on_message)
            await bridge.start()
            await discovery.start()
        except MqttConfigurationError as exc:
            # The message already names the setting and the fix; no traceback.
            self._logger.warning("the MQTT bridge is not configured: %s", exc)
            self._health.set("mqtt", HealthStatus.ERROR, str(exc))
            return
        except Exception as exc:
            self._logger.warning("starting the MQTT bridge failed: %s", exc)
            self._health.set(
                "mqtt",
                HealthStatus.ERROR,
                f"the MQTT bridge could not start: {exc} — check mqtt.host and mqtt.port in "
                "Settings and restart if it persists",
            )
            # A half-started generation must not keep a socket alive behind the
            # back of the pipeline: nothing would ever stop it.
            if discovery is not None:
                with contextlib.suppress(Exception):
                    await discovery.stop()
            if bridge is not None:
                with contextlib.suppress(Exception):
                    await bridge.stop()
            return
        self._bridge = bridge
        self._discovery = discovery
        self._mqtt_generations += 1

    async def _teardown_mqtt(self, *, retract: bool) -> None:
        """Drop the current MQTT generation, retracting its entities if asked.

        The attributes are cleared first so a concurrent reader sees "no
        generation" rather than a bridge that is already stopping.

        :param retract: publish the empty discovery payloads before stopping.
            The reload path passes ``True`` — the payloads must travel through
            the still-open bridge — while shutdown passes ``False``: design 02 §4
            says a clean shutdown publishes nothing, and the bridge's availability
            ``offline`` is the bridge's own job.
        """
        discovery, self._discovery = self._discovery, None
        bridge, self._bridge = self._bridge, None
        if discovery is not None:
            if retract:
                # Never raises by contract, and it must happen before either stop.
                await discovery.retract_all()
            await discovery.stop()
        if bridge is not None:
            await bridge.stop()

    async def _reload_mqtt(self) -> None:
        """Replace the MQTT generation when the settings it consumes changed.

        The equality guard collapses the one-event-per-key burst a single
        ``PATCH /api/settings`` emits, and the lock stops two overlapping patches
        from both observing a difference and both starting a bridge.
        """
        async with self._reload_lock:
            try:
                settings = await load_mqtt_settings(self._store)
            except MqttConfigurationError as exc:
                # Keep the working generation: a half-written configuration must
                # not take the working chime buttons away.
                self._logger.warning(
                    "keeping the running MQTT bridge: the new mqtt.* settings cannot be used: %s",
                    exc,
                )
                self._health.set("mqtt", HealthStatus.ERROR, str(exc))
                return
            if self._bridge is not None and self._bridge.settings == settings:
                return
            self._logger.info(
                "rebuilding the MQTT bridge; the settings write waits for it (worst case %g s)",
                _REBUILD_WORST_CASE_S,
            )
            # Retract only when something beyond the display name differs. The
            # name reaches just the discovery ``device`` block, so retracting on a
            # rename would delete and re-create entities whose identity did not
            # change, splitting their history and breaking automations; the new
            # generation republishes the same config topics with the new name
            # instead (HSD-vwdy). A first start (``None``) always retracts, as
            # before: there is nothing to retract, and the argument is what the
            # teardown contract expects.
            previous = None if self._bridge is None else self._bridge.settings
            retract = previous is None or differs_beyond_display_name(previous, settings)
            await self._teardown_mqtt(retract=retract)
            await self._start_mqtt()

    async def _on_settings_changed(self, event: ConfigEvent) -> None:
        """React to a committed setting change that this pipeline consumes.

        An ``mqtt.*`` or ``device.name`` change reloads the MQTT generation; an
        ``audio.*`` change re-arms the start retry for a speaker that is not
        running (see :meth:`_on_audio_settings_changed`).

        The whole body is guarded because this runs inside
        ``EventBus.publish``, whose own fallback logs a traceback: a handled
        condition must never print one.

        :param event: committed configuration change, from the store's bus.
        """
        try:
            if event.name is not ConfigEventName.SETTINGS_CHANGED:
                return
            # ``event.payload.keys`` is the Mapping method, not the changed keys.
            keys = event.payload.get("keys", ())
            if not isinstance(keys, tuple):
                return
            if any(isinstance(key, str) and key.startswith(_AUDIO_KEY_PREFIX) for key in keys):
                self._on_audio_settings_changed(keys)
            # ``device.name`` is not an ``mqtt.*`` key but it does change a
            # discovery payload, so it rebuilds the generation too.
            if not any(
                isinstance(key, str)
                and (key.startswith(_MQTT_KEY_PREFIX) or key == _DEVICE_NAME_KEY)
                for key in keys
            ):
                return
            await self._reload_mqtt()
        except Exception as exc:
            self._logger.warning(
                "reloading the MQTT bridge after a settings change failed: %s", exc
            )

    def _on_audio_settings_changed(self, keys: Sequence[object]) -> None:
        """Apply the new device level and re-arm a stopped speaker; never block the bus.

        Values are re-read where they are used — volume and the quiet window per
        press, ``audio.start_timeout_s`` before every start attempt — but a
        backend is built once, at start. A healthy worker therefore keeps its
        own backend and its output through the settings write: the documented
        policy, and it must not interrupt a chime.

        The exception is the device's own volume control (cd-qul9). A running
        speaker is exactly where a new ``audio.hardware_volume``, ``audio.muted``,
        ``audio.hardware_control`` or ``audio.device_hint`` has to land, and it
        lands through the mixer rather than through a restart, so those keys
        schedule :meth:`_reapply_hardware` as a tracked task. The schedule is a
        ``create_task`` because this method is called from ``EventBus.publish``,
        which runs inside the settings write: awaiting a mixer there would make
        the HTTP response wait on ``amixer``.

        A speaker that is not running is the other interesting case: health
        already shows audio disconnected, and the owner raises
        ``audio.start_timeout_s``. Re-arming retries at once instead of waiting
        out a backoff step, and the next attempt reads the new budget; the
        hardware level is then applied by that start's success branch.

        :param keys: the committed settings keys named by the event's payload.
        """
        worker = self._worker
        if worker is None:
            self._logger.info(
                "audio settings changed, but the chime pipeline has no playback worker yet"
            )
            return
        if worker.is_running:
            if any(isinstance(key, str) and key in _HARDWARE_VOLUME_KEYS for key in keys):
                self._logger.info(
                    "audio settings changed; applying the stored level to the device's own "
                    "volume control now"
                )
                self._schedule_hardware_reapply()
                return
            self._logger.info(
                "audio settings changed; the running speaker keeps its current output until "
                "the service restarts"
            )
            return
        self._logger.info(
            "audio settings changed while the speaker was not running; retrying the start now"
        )
        # A fresh start: the caller made no attempt of its own, so this ladder's
        # first attempt is numbered 1 and is tried straight away.
        self._arm_audio_recovery(worker, wait_first=False, attempts_so_far=0)

    def _schedule_hardware_reapply(self) -> None:
        """Re-apply the stored device level in a tracked task, replacing any pending one.

        Only one re-apply can be useful at a time: a second settings write while
        the first is still running supersedes it, so the older task is cancelled
        (its command is killed and reaped) instead of piling up behind the mixer
        lock. Cancellation is safe here because :meth:`HardwareVolume.apply`
        holds the lock only for its own bounded commands.
        """
        previous, self._hardware_task = self._hardware_task, None
        if previous is not None and not previous.done():
            previous.cancel()
        self._hardware_task = asyncio.create_task(self._reapply_hardware(), name="hardware-volume")

    async def _reapply_hardware(self) -> None:
        """Apply the stored device level, logging the outcome. Never raises.

        Total by construction: :meth:`HardwareVolume.apply` reports every failure
        as a status, so this only has to log it — and it must not raise, because
        it runs as a background task where an exception would be a warning with a
        traceback for a cosmetic problem.
        """
        status = await self.reapply_hardware()
        if status.available:
            self._logger.info(
                "the device's own volume control is at %s%% (%s)",
                status.percent,
                status.control,
            )
            return
        self._logger.debug("the device's own volume control is unavailable: %s", status.reason)

    async def reapply_hardware(self) -> HardwareVolumeStatus:
        """Set the resolved output's own control to the stored level and read it back.

        Called at startup, after a recovery and on every device change (including
        the test tone's ``set_device``). Never raises: a mixer that is missing,
        wedged or absent from the output is reported in the returned status, so a
        caller may use it on a path where a cosmetic failure must not fail the
        request.
        """
        return await self._hardware.apply()

    async def read_hardware(self) -> HardwareVolumeStatus:
        """Read the resolved output's own control without changing it. Never raises.

        This is the API route's method: a ``GET`` that writes a system mixer is a
        surprise, so the read path is separate from :meth:`reapply_hardware`.
        """
        return await self._hardware.read()
