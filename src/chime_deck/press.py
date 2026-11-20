"""The press path: one entry point for an MQTT press and a UI test press.

Both kinds of press arrive here (design 02 §6, design 03 §2) and run through the
same steps, in this order: dedupe first, then mute, then quiet hours, then action
resolution, then dispatch. The order matters and is pinned by tests: a press
that is suppressed or that fails still arms the dedupe window, because the
window is measured from the accepted press (design 03 §3). A muted press is a
distinct outcome rather than a quiet-hours one, because a missed alarm must
never be explained as quiet hours (D28).

Every press produces exactly one ``press_log`` row and exactly one hub message
per press *decision*, because every path ends in
:meth:`PressService._finish`: it is the single writer of the row and the single
pusher of the ``queued`` (or terminal) outcome. "Exactly one row per press" is
therefore structural, not a convention. Since HSD-96k6 that also holds for a
press the MQTT seam cancels: the press path carries an absolute press-wide
monotonic deadline (:class:`PressDeadline`) that stays strictly inside the seam,
stops the press before the seam stops it, and re-writes its own row if a
cancellation lands inside that write, so a cancel at the seam's backstop still
leaves exactly one honest row instead of the silence the product used to have
no explanation for.

It is no longer the only caller of :meth:`StateHub.notify_press`. A successful
dispatch is recorded as ``queued``: playback, latency and the real duration
belong to the playback queue worker, which revises that same row to ``played``
or ``failed`` once the sound has really finished (HSD-kfnd) and pushes that flip
from :class:`~chime_deck.playback.HubPlaybackRecorder` — one place per half
of the press, so a press is never logged or announced twice. This service's job
is to decide and enqueue, not to play, and it deliberately does not pretend the
chime has rung.

Two seams keep the pending tickets out of this module:

* :class:`PressDispatcher` is the playback queue worker (HSD-kfnd).
* :class:`SpeechSource` is the TTS engine resolver and fallback chain
  (HSD-bw8h). This module never constructs an engine and never probes one.

Owning ticket: HSD-laoi.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, time
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Final, Protocol
from uuid import uuid4

from .audio import AudioError, MissingAudioFileError
from .hub import StateHub
from .store import (
    SETTINGS,
    Button,
    ConfigStore,
    PressLogIn,
    SoundNotFound,
    normalise_timestamp,
)

if TYPE_CHECKING:
    # Type-only on purpose: ``chime_deck.tts.speech`` imports this module, so
    # a runtime import of ``chime_deck.tts.providers`` here would re-enter
    # the ``chime_deck.tts`` package while ``press`` is still executing.
    from .tts.providers import TtsCost

__all__ = [
    "SOURCE_MQTT",
    "SOURCE_UI_TEST",
    "PlayJob",
    "PressCode",
    "PressDeadline",
    "PressDispatcher",
    "PressOutcome",
    "PressResult",
    "PressService",
    "SpeechClip",
    "SpeechSource",
    "SpeechUnavailable",
    "in_quiet_hours",
    "resolve_volume",
]

SOURCE_MQTT: Final = "mqtt"
"""``press_log.source`` for a press that arrived over MQTT."""

SOURCE_UI_TEST: Final = "ui_test"
"""``press_log.source`` for a press from the UI's test action."""

_MUTED_DETAIL: Final = (
    "the doorbell is muted (audio.muted is on), so nothing was played — unmute to hear chimes again"
)
"""``press_log.detail`` for a press suppressed by ``audio.muted`` (D28).

It names mute explicitly so "why did my alarm not ring" is answerable from the
history row; :meth:`PressService._finish` stores it with no code prefix.
"""

PRESS_DEADLINE_S: Final = 9.0
"""Seconds a press has to finish speaking before it gives up on itself (HSD-96k6).

The MQTT bridge cancels a whole press after
:data:`~chime_deck.mqtt.bridge.DEFAULT_SEAM_TIMEOUT_S` (10 s), and a
cancelled press used to write no ``press_log`` row at all: the doorbell stayed
silent with only a log warning to explain it. So the press stops *itself* first
and records an honest failure. One second below the seam is many orders of
magnitude more than the press-log insert plus the enqueue that still have to be
paid for afterwards (see :data:`PRESS_FINISH_MARGIN_S`).

Deliberate consequence: 9.0 s is below the 9.5 s worst case of a single
templated ``ha_tts`` press, so the **deadline**, not the seam, is what binds;
the press fails with a row that says why instead of vanishing without one.
"""

PRESS_FINISH_MARGIN_S: Final = 1.0
"""Seconds of the 10 s MQTT seam kept for finishing the press after the deadline.

After :data:`PRESS_DEADLINE_S` expires the press still has to write its
``press_log`` row and push its hub message. A whole second is far more than
those cost, which is why the deadline sits at 9.0 s rather than at the seam
itself: the row is written while the seam is still waiting, so the seam never
has to cancel a press that has something left to record.
"""


class PressOutcome(StrEnum):
    """What one press did, as stored in ``press_log.outcome``."""

    # Not produced here: the playback worker (HSD-kfnd) writes it via
    # ``ConfigStore.record_playback`` once the sound has actually finished.
    PLAYED = "played"
    # This service's single success outcome: the job was accepted for playback.
    QUEUED = "queued"
    # Pressed again inside the per-button dedupe window.
    DEDUPED = "deduped"
    # The deliberate "silence this device" state (``audio.muted``, D28). It
    # outranks quiet hours, exemption included, so it is a distinct outcome: a
    # missed alarm must never be reported as quiet hours.
    SUPPRESSED_MUTED = "suppressed_muted"
    # Inside quiet hours, not exempt, and the quiet-hours volume is zero.
    SUPPRESSED_QUIET_HOURS = "suppressed_quiet_hours"
    # Nothing was dispatched; ``detail`` says why.
    FAILED = "failed"


class PressCode(StrEnum):
    """Machine-readable reason attached to a non-success outcome.

    The first two codes are deliberately distinct states and must never collapse:
    ``tts_disabled`` is a deliberate configuration (D27), ``tts_unavailable`` is
    a fault. A user needs to tell "I turned speech off" from "speech is broken".
    """

    # Speech switched off by ``tts.default_engine: none`` (D27): intentional,
    # and there is no fallback engine to try.
    TTS_DISABLED = "tts_disabled"
    # An engine was wanted but none could speak.
    TTS_UNAVAILABLE = "tts_unavailable"
    # The button's sound row is gone or has no playable local file.
    SOUND_MISSING = "sound_missing"
    # The button's own configuration cannot produce audio.
    INVALID_BUTTON = "invalid_button"
    # The audio engine rejected the job.
    AUDIO_ERROR = "audio_error"
    # The dispatcher raised something that is not an audio error.
    DISPATCH_ERROR = "dispatch_error"
    # The slug does not name any button.
    UNKNOWN_BUTTON = "unknown_button"
    # The press was cancelled before it could finish: the MQTT seam's deadline
    # expired or the doorbell is shutting down. Not TTS_UNAVAILABLE — nothing
    # said speech was broken; time ran out. It exists so the row is honest and
    # the silence is explainable, which is the whole point of HSD-96k6.
    PRESS_CANCELLED = "press_cancelled"


@dataclass(frozen=True, slots=True)
class PlayJob:
    """One resolved press handed to the playback worker."""

    # Persisted in ``press_log.job_id`` by :meth:`PressService._finish`, so the
    # playback worker can revise the row the press wrote instead of inserting a
    # second one.
    job_id: str
    button_id: int
    button_name: str
    path: Path
    # Already resolved: override ?? master, clamped, quiet-hours cap applied.
    volume: int
    # The sound-library row this file came from, or ``None`` for a TTS clip. It
    # lets a failed pre-play check name the sound instead of only the path.
    sound_id: int | None
    # The canonical press timestamp: the played/failed flip carries it so the UI
    # can correlate it with the ``queued`` message and the press log row, which
    # all describe the same press.
    pressed_at: str


class PressDispatcher(Protocol):
    """The narrow port the playback queue worker implements (HSD-kfnd)."""

    async def dispatch(self, job: PlayJob) -> None:
        """Accept one resolved job for later playback.

        Returns once the job is accepted; playback ordering, dropping and
        durations belong to the implementation.

        :param job: resolved job to enqueue.
        :raises AudioError: if the job cannot be accepted (queue full, no device).
        """
        ...


@dataclass(frozen=True, slots=True)
class SpeechClip:
    """A playable piece of speech produced by a :class:`SpeechSource`."""

    path: Path
    engine: str
    # e.g. "fell back from ha_tts: unreachable", or a retry note such as
    # "retried on espeak_ng after ha_tts failed transiently: …". A truthy
    # detail that names an *availability condition* (a fallback reason or a
    # template warning) is what flips ``tts`` health to DEGRADED; a detail that
    # names a *render* (a retry note) does not, because it describes one press,
    # not a lasting condition. A billed synthesis reports what it cost through
    # ``cost``, never through ``detail``.
    detail: str | None = None
    # Milliseconds the synthesis of THIS clip took, measured by the engine.
    # ``None`` means no synthesis was measured for it (a cache or bake hit, a
    # sound button, a failed press, or an engine that keeps no measurement) —
    # never "zero". It must never be put in ``detail``: a truthy ``detail``
    # means degraded, and a successful measurement is not a degradation.
    synthesis_ms: int | None = None
    # What this press actually paid, or ``None`` when nothing was synthesised
    # (a cache or bake hit is free). It is a note, never a degradation, so it
    # too must never be put in ``detail``.
    cost: TtsCost | None = None


class SpeechUnavailable(Exception):
    """Raised by a :class:`SpeechSource` when no engine can speak.

    This is not :data:`PressCode.TTS_DISABLED`: speech was wanted, but no engine
    could produce it. The two states are logged with different codes (D27).
    """


class SpeechSource(Protocol):
    """The minimal speech port; the engine resolver and fallback chain are HSD-bw8h's job."""

    async def resolve(
        self, *, button: Button, engine: str, deadline: PressDeadline | None = None
    ) -> SpeechClip:
        """Turn a TTS button into a playable clip.

        ``deadline`` is the press-wide budget: every step that can block is
        bounded by what remains of it, so a press cannot outlive the MQTT seam
        that wraps it. ``None`` means the caller has none — the bake paths and
        direct constructions — and keeps the per-engine budgets exactly as they
        were. It also rules out a second attempt, because a retry that cannot be
        bounded must not be started.

        :param button: the button being pressed; carries ``tts_text``.
        :param engine: effective engine (``button.tts_engine`` ??
            ``tts.default_engine``); never ``"none"``.
        :param deadline: whole-press time budget, or ``None`` when the caller
            has none (the bake paths and direct constructions).
        :raises SpeechUnavailable: if no engine can produce the clip.
        """
        ...


@dataclass(frozen=True, slots=True)
class PressResult:
    """What one press did; returned to the caller and mirrored in the press log."""

    button_id: int | None
    button_name: str
    source: str
    outcome: PressOutcome
    pressed_at: str
    code: PressCode | None = None
    detail: str | None = None
    # Milliseconds this press's own TTS synthesis took, when one was measured.
    synthesis_ms: int | None = None


def _epoch(moment: datetime) -> float:
    """Return *moment* as a POSIX timestamp.

    Both values compared for dedupe always come from the same injected clock, so
    a naive value is interpreted in one consistent timezone and the difference is
    exact. Using ``timestamp()`` rather than ``later - earlier`` also avoids a
    naive/aware ``TypeError`` if a deployment ever changes the clock.

    :param moment: clock reading to convert.
    """
    return moment.timestamp()


def _local_time(now: datetime) -> time:
    """Return the local wall-clock time of *now*.

    An aware value is converted into the host's local time zone; a naive value is
    taken to already be local wall-clock time, which is what lets the tests inject
    ``datetime(2026, 1, 1, 23, 30)`` and stay independent of the host time zone.

    :param now: clock reading to localise.
    """
    if now.tzinfo is None:
        return now.time()
    return now.astimezone().time()


def in_quiet_hours(now: datetime, start: time, end: time) -> bool:
    """Return whether *now* falls inside the daily quiet-hours window.

    Reference implementation from design 03 §4: the window is half-open, so the
    start is inside and the end is outside; ``start == end`` is a zero-length
    window and therefore disabled; ``start > end`` is a window that crosses
    midnight.

    :param now: clock reading to test.
    :param start: first local time inside the window.
    :param end: first local time outside the window.
    """
    if start == end:
        return False
    current = _local_time(now)
    if start < end:
        return start <= current < end
    return current >= start or current < end


def resolve_volume(override: int | None, master: int, *, quiet_cap: int | None = None) -> int:
    """Resolve the volume for one job (design 03 §5).

    Three layers: the button override, else the global volume, clamped to
    ``[0, 100]``, then capped by the quiet-hours volume when one applies.

    :param override: ``button.volume_override``, or ``None`` to use the global volume.
    :param master: ``audio.volume``.
    :param quiet_cap: ``audio.quiet_hours_volume`` when it is above zero and the
        press falls inside quiet hours; ``None`` outside the window.
    """
    effective = master if override is None else override
    effective = max(0, min(100, effective))
    if quiet_cap is not None:
        effective = min(effective, max(0, quiet_cap))
    return effective


def _int_default(key: str) -> int:
    """Return the registered integer default for *key*.

    A missing or mistyped default is a programming error in the registry, not
    user data, and must not be papered over on the press path.

    :param key: setting key to read the default of.
    """
    default = SETTINGS[key].default
    if isinstance(default, bool) or not isinstance(default, int):
        raise TypeError(f"setting {key!r} does not have an integer default")
    return default


def _bool_default(key: str) -> bool:
    """Return the registered boolean default for *key*.

    :param key: setting key to read the default of.
    """
    default = SETTINGS[key].default
    if not isinstance(default, bool):
        raise TypeError(f"setting {key!r} does not have a boolean default")
    return default


def _str_default(key: str) -> str:
    """Return the registered string default for *key*.

    :param key: setting key to read the default of.
    """
    default = SETTINGS[key].default
    if not isinstance(default, str):
        raise TypeError(f"setting {key!r} does not have a string default")
    return default


def _utcnow() -> datetime:
    """Return the current aware UTC time."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class PressDeadline:
    """The absolute, monotonic instant by which a press must have finished speaking.

    This is **one budget for the whole press, not one per engine**. Every step
    that can block — an availability probe, the press-time template render, the
    render itself — is handed the budget that remains, so a chain of slow or
    unavailable engines cannot outlive the sum of its own per-engine budgets.

    The budget must stay strictly inside the MQTT seam
    (:data:`~chime_deck.mqtt.bridge.DEFAULT_SEAM_TIMEOUT_S`), because the
    seam's backstop cancels the whole callback: a press that reaches it writes no
    row. Expiring at :data:`PRESS_DEADLINE_S` instead means the press ends with
    a recorded, explainable row.

    The budget is only about *time*: it says nothing about which engine may
    speak. Engine choice stays the fallback chain's business.
    """

    started: float
    budget: float

    @classmethod
    def after(cls, seconds: float) -> PressDeadline:
        """Start a deadline of *seconds* from now.

        :param seconds: whole-press budget in seconds.
        """
        return cls(started=monotonic(), budget=seconds)

    def remaining(self) -> float:
        """Return the seconds left in the budget; never negative."""
        return max(0.0, self.started + self.budget - monotonic())

    def elapsed(self) -> float:
        """Return the seconds spent since the deadline started; never negative."""
        return max(0.0, monotonic() - self.started)

    def allows(self, seconds: float) -> bool:
        """Return whether *seconds* of further work still fits in the budget.

        :param seconds: work that would have to start now to finish in time.
        """
        return self.remaining() >= seconds


class PressService:
    """Decide what a press should do, record it and hand the job to the queue."""

    def __init__(
        self,
        store: ConfigStore,
        hub: StateHub,
        dispatcher: PressDispatcher,
        *,
        speech: SpeechSource | None = None,
        clock: Callable[[], datetime] | None = None,
        sounds_dir: Path = Path(),
    ) -> None:
        """Create the service; nothing runs until :meth:`press` is awaited.

        :param store: configuration store read for buttons and settings and
            written through :meth:`ConfigStore.log_press`.
        :param hub: broadcast hub that pushes the ``press`` message to the UI.
        :param dispatcher: playback queue worker (HSD-kfnd) that accepts jobs.
        :param speech: speech source (HSD-bw8h). It is optional because that
            ticket owns the engines: a TTS press without one fails as
            ``tts_unavailable`` instead of pretending to speak.
        :param clock: source of the current time; defaults to :func:`_utcnow`,
            which returns aware UTC. In production this must stay aware.
        :param sounds_dir: directory a sound row's relative ``path`` is resolved
            against. ``001_initial.sql`` documents ``sounds.path`` relative to
            ``data/sounds``. The default ``Path()`` keeps the historical
            CWD-relative behaviour for direct constructions, and because
            ``Path(d) / Path(abs) == abs`` a legacy absolute row (imports, old
            tests) still resolves unchanged; no containment check is enforced
            here because it would break those rows (a known limitation, named in
            the ticket note).

        The logger is ``logging.getLogger(__name__)``, created last.
        """
        self._store = store
        self._hub = hub
        self._dispatcher = dispatcher
        self._speech = speech
        self._clock = clock if clock is not None else _utcnow
        self._sounds_dir = sounds_dir
        # In-memory dedupe state, cleared by a restart, which is correct for a
        # chime (design 03 §3).
        self._last_press: dict[int, datetime] = {}
        self._logger = logging.getLogger(__name__)

    async def press(self, slug: str, *, source: str = SOURCE_MQTT) -> PressResult:
        """Run one press from start to finish and report what it did.

        This is the single entry point for both an MQTT press and a UI test
        press. It never raises for anything it decides: a dispatcher failure, a
        broken speech engine, a missing sound, disabled speech and a failed
        press-log write are all reported in the result. Only a broken store can
        propagate, because then there is nowhere to record the outcome.

        A deduped, suppressed or failed press still arms the dedupe window,
        because the window is measured from the accepted press (design 03 §3).

        The whole press runs under one :class:`PressDeadline` — 9.0 s, one second
        inside the MQTT seam — so a press stops *itself* instead of being
        cancelled by the seam: a cancelled press re-raises after writing its own
        ``failed``/``press_cancelled`` row, which is what keeps the silence
        explainable (HSD-96k6).

        :param slug: the button slug that was pressed.
        :param source: ``mqtt`` or ``ui_test``, recorded in the press log.
        """
        now = self._clock()
        pressed_at = normalise_timestamp(now.isoformat())
        deadline = PressDeadline.after(PRESS_DEADLINE_S)

        button = await self._store.get_button_by_slug(slug)
        if button is None:
            # No hub push: StateHub.notify_press requires an int button id.
            return await self._finish(
                button_id=None,
                button_name=slug,
                source=source,
                outcome=PressOutcome.FAILED,
                pressed_at=pressed_at,
                code=PressCode.UNKNOWN_BUTTON,
                message=f"no button has the slug {slug!r}",
            )

        window = await self._dedupe_window(button)
        # Every settings read happens before this check-and-set, so no ``await``
        # sits between reading the stored press time and writing the new one.
        if self._is_duplicate(button.id, now, window):
            return await self._finish(
                button_id=button.id,
                button_name=button.name,
                source=source,
                outcome=PressOutcome.DEDUPED,
                pressed_at=pressed_at,
                message=f"pressed again within the {window} second dedupe window",
            )

        # Mute is checked after dedupe so the window is already armed by the
        # check-and-set above: a muted press arms it exactly like a suppressed
        # quiet-hours press. It has no exemption (D28): muting this device silences
        # every press, ``quiet_hours_exempt`` buttons included.
        if await self._bool_setting("audio.muted"):
            return await self._finish(
                button_id=button.id,
                button_name=button.name,
                source=source,
                outcome=PressOutcome.SUPPRESSED_MUTED,
                pressed_at=pressed_at,
                message=_MUTED_DETAIL,
            )

        quiet_cap: int | None = None
        quiet_label = ""
        if await self._bool_setting("audio.quiet_hours_enabled"):
            start = self._quiet_time(await self._str_setting("audio.quiet_hours_start"))
            end = self._quiet_time(await self._str_setting("audio.quiet_hours_end"))
            if start is not None and end is not None:
                quiet_label = f"quiet hours {start:%H:%M}-{end:%H:%M}"
                if in_quiet_hours(now, start, end) and not button.quiet_hours_exempt:
                    quiet_volume = await self._int_setting("audio.quiet_hours_volume")
                    if quiet_volume <= 0:
                        return await self._finish(
                            button_id=button.id,
                            button_name=button.name,
                            source=source,
                            outcome=PressOutcome.SUPPRESSED_QUIET_HOURS,
                            pressed_at=pressed_at,
                            message=f"{quiet_label}: audio suppressed",
                        )
                    quiet_cap = quiet_volume

        notes: list[str] = []
        sound_name: str | None = None
        synthesis_ms: int | None = None
        path: Path
        if button.action_type == "sound":
            if button.sound_id is None:
                return await self._finish(
                    button_id=button.id,
                    button_name=button.name,
                    source=source,
                    outcome=PressOutcome.FAILED,
                    pressed_at=pressed_at,
                    code=PressCode.INVALID_BUTTON,
                    message="a sound button without a sound_id cannot be played",
                )
            try:
                sound = await self._store.get_sound(button.sound_id)
            except SoundNotFound:
                return await self._finish(
                    button_id=button.id,
                    button_name=button.name,
                    source=source,
                    outcome=PressOutcome.FAILED,
                    pressed_at=pressed_at,
                    code=PressCode.SOUND_MISSING,
                    message=f"sound {button.sound_id} no longer exists",
                )
            if sound.path is None:
                return await self._finish(
                    button_id=button.id,
                    button_name=button.name,
                    source=source,
                    outcome=PressOutcome.FAILED,
                    pressed_at=pressed_at,
                    code=PressCode.SOUND_MISSING,
                    message=f"sound {sound.name!r} has no local file",
                )
            path = self._sounds_dir / Path(sound.path)
            sound_name = sound.name
        elif button.action_type == "tts":
            configured = (button.tts_engine or "").strip()
            if configured == "none":
                # A per-button switch-off is the same deliberate state as the
                # global one (D27): speech is off, so there is nothing to resolve
                # and no fallback to try.
                return await self._finish(
                    button_id=button.id,
                    button_name=button.name,
                    source=source,
                    outcome=PressOutcome.FAILED,
                    pressed_at=pressed_at,
                    code=PressCode.TTS_DISABLED,
                    message=(
                        f"speech is disabled (this button's tts_engine is 'none'), "
                        f"so {button.name!r} cannot speak"
                    ),
                )
            engine = await self._str_setting("tts.default_engine")
            if engine == "none":
                # Speech is switched off on purpose (D27). The speech port must
                # not be touched at all: no engine constructed, no probe, and no
                # fallback, because there is nothing to fall back to.
                return await self._finish(
                    button_id=button.id,
                    button_name=button.name,
                    source=source,
                    outcome=PressOutcome.FAILED,
                    pressed_at=pressed_at,
                    code=PressCode.TTS_DISABLED,
                    message=(
                        f"speech is disabled (tts.default_engine is 'none'), "
                        f"so {button.name!r} cannot speak"
                    ),
                )
            if not (button.tts_text or "").strip():
                return await self._finish(
                    button_id=button.id,
                    button_name=button.name,
                    source=source,
                    outcome=PressOutcome.FAILED,
                    pressed_at=pressed_at,
                    code=PressCode.INVALID_BUTTON,
                    message="a TTS button without text cannot speak",
                )
            if self._speech is None:
                return await self._finish(
                    button_id=button.id,
                    button_name=button.name,
                    source=source,
                    outcome=PressOutcome.FAILED,
                    pressed_at=pressed_at,
                    code=PressCode.TTS_UNAVAILABLE,
                    message="no speech source is configured",
                )
            # Precedence: the global setting has already been checked, so a
            # globally disabled speech path has returned above and a button that
            # names an engine cannot re-enable it (D27).
            effective = configured or engine
            try:
                clip = await self._speech.resolve(
                    button=button, engine=effective, deadline=deadline
                )
            except asyncio.CancelledError:
                # A cancelled press used to write no row at all — the worst failure
                # this product has, because the doorbell just stays silent. The
                # seam is the outer backstop (mqtt/bridge.py's wait_for), so a
                # cancellation here is that backstop firing or a shutdown; either
                # way the row must exist and must claim nothing that did not happen.
                await self._finish(
                    button_id=button.id,
                    button_name=button.name,
                    source=source,
                    outcome=PressOutcome.FAILED,
                    pressed_at=pressed_at,
                    code=PressCode.PRESS_CANCELLED,
                    message=(
                        f"the press was cancelled after {deadline.elapsed():.1f} s before it "
                        "finished — nothing was played"
                    ),
                )
                raise
            except SpeechUnavailable as exc:
                return await self._finish(
                    button_id=button.id,
                    button_name=button.name,
                    source=source,
                    outcome=PressOutcome.FAILED,
                    pressed_at=pressed_at,
                    code=PressCode.TTS_UNAVAILABLE,
                    message=str(exc),
                )
            # Same reasoning as the dispatcher net below: design 03 §7 requires
            # that silence is always explainable, so a broken engine library must
            # still produce exactly one failed row. BaseException (cancellation,
            # KeyboardInterrupt) is deliberately not caught.
            except Exception as exc:
                self._logger.exception("speech resolution failed for button %s", button.slug)
                return await self._finish(
                    button_id=button.id,
                    button_name=button.name,
                    source=source,
                    outcome=PressOutcome.FAILED,
                    pressed_at=pressed_at,
                    code=PressCode.TTS_UNAVAILABLE,
                    message=f"{type(exc).__name__}: {exc}",
                )
            path = clip.path
            synthesis_ms = clip.synthesis_ms
            notes.append(f"engine {clip.engine}")
            # ``clip.detail`` stays exactly as it is: it is the DEGRADED trigger
            # in ``speech.py``, and a successful billed synthesis is not a
            # degraded component. The cost is a note in the press row instead.
            if clip.detail is not None:
                notes.append(clip.detail)
            if clip.cost is not None:
                notes.append(clip.cost.note())
        else:
            return await self._finish(
                button_id=button.id,
                button_name=button.name,
                source=source,
                outcome=PressOutcome.FAILED,
                pressed_at=pressed_at,
                code=PressCode.INVALID_BUTTON,
                message=f"unknown action type {button.action_type!r}",
            )

        volume = resolve_volume(
            button.volume_override,
            await self._int_setting("audio.volume"),
            quiet_cap=quiet_cap,
        )
        if quiet_cap is not None:
            notes.append(f"{quiet_label}: volume limited to {volume}")

        job = PlayJob(
            job_id=uuid4().hex,
            button_id=button.id,
            button_name=button.name,
            path=path,
            volume=volume,
            sound_id=button.sound_id if button.action_type == "sound" else None,
            pressed_at=pressed_at,
        )
        try:
            await self._dispatcher.dispatch(job)
        except MissingAudioFileError as exc:
            return await self._finish(
                button_id=button.id,
                button_name=button.name,
                source=source,
                outcome=PressOutcome.FAILED,
                pressed_at=pressed_at,
                code=PressCode.SOUND_MISSING,
                message=str(exc),
                sound_name=sound_name,
            )
        except AudioError as exc:
            return await self._finish(
                button_id=button.id,
                button_name=button.name,
                source=source,
                outcome=PressOutcome.FAILED,
                pressed_at=pressed_at,
                code=PressCode.AUDIO_ERROR,
                message=str(exc),
                sound_name=sound_name,
            )
        # Catching Exception here is deliberate and is what design 03 §7 asks
        # for: silence must always be explainable, so an unexpected dispatcher
        # failure is recorded as ``dispatch_error`` rather than escaping and
        # leaving the user with no row and no message. Cancellation and
        # KeyboardInterrupt are BaseException, so they still propagate.
        except Exception as exc:
            self._logger.exception("press dispatch failed for button %s", button.slug)
            return await self._finish(
                button_id=button.id,
                button_name=button.name,
                source=source,
                outcome=PressOutcome.FAILED,
                pressed_at=pressed_at,
                code=PressCode.DISPATCH_ERROR,
                message=f"{type(exc).__name__}: {exc}",
                sound_name=sound_name,
            )
        return await self._finish(
            button_id=button.id,
            button_name=button.name,
            source=source,
            outcome=PressOutcome.QUEUED,
            pressed_at=pressed_at,
            message="; ".join(notes) or None,
            sound_name=sound_name,
            job_id=job.job_id,
            synthesis_ms=synthesis_ms,
        )

    async def _finish(
        self,
        *,
        button_id: int | None,
        button_name: str,
        source: str,
        outcome: PressOutcome,
        pressed_at: str,
        code: PressCode | None = None,
        message: str | None = None,
        sound_name: str | None = None,
        job_id: str | None = None,
        synthesis_ms: int | None = None,
    ) -> PressResult:
        """Record the outcome, tell the UI and return the result.

        This is the only writer of ``press_log`` rows, so "exactly one row per
        press" is structural. A press must never raise because its history write
        failed, so that failure is logged and the result is still returned. A
        cancellation that lands inside the write does not lose the row either:
        :meth:`_log_press` writes it again, so a cancelled press still leaves
        exactly one honest row (HSD-96k6).

        :param button_id: id of the button, or ``None`` for an unknown slug.
        :param button_name: display name, or the slug when no button matched.
        :param source: ``mqtt`` or ``ui_test``.
        :param outcome: what the press did.
        :param pressed_at: canonical UTC timestamp of the press.
        :param code: machine-readable reason for a non-success outcome.
        :param message: human-readable detail; stored after the code.
        :param sound_name: sound played, when the press resolved to one.
        :param job_id: id of the dispatched job, so the playback worker can
            revise this row; ``None`` for every outcome that dispatched nothing.
        :param synthesis_ms: milliseconds the press's own TTS synthesis took;
            ``None`` (the default for every non-TTS outcome) means nothing was
            measured, never that it was zero.
        """
        if code is None:
            detail = message
        elif message is None:
            detail = str(code)
        else:
            detail = f"{code}: {message}"

        entry = PressLogIn(
            button_id=button_id,
            button_name=button_name,
            source=source,
            outcome=str(outcome),
            pressed_at=pressed_at,
            detail=detail,
            sound_name=sound_name,
            job_id=job_id,
            synthesis_ms=synthesis_ms,
        )
        await self._log_press(entry)

        if button_id is not None:
            self._hub.notify_press(
                button_id=button_id,
                button_name=button_name,
                outcome=str(outcome),
                pressed_at=pressed_at,
            )

        self._logger.debug(
            "press of %r from %s: %s%s",
            button_name,
            source,
            outcome,
            f" ({detail})" if detail is not None else "",
        )
        return PressResult(
            button_id=button_id,
            button_name=button_name,
            source=source,
            outcome=outcome,
            pressed_at=pressed_at,
            code=code,
            detail=detail,
            synthesis_ms=synthesis_ms,
        )

    async def _log_press(self, entry: PressLogIn) -> None:
        """Write *entry* to the press log, surviving a cancellation during the write.

        The write runs **inline**, in the caller's task. That is a HSD-96k6
        requirement rather than a style choice: the store takes one write lock per
        transaction, so whoever submits first wins, and the press's row has to be
        the row the playback worker then revises. Moving the write into a task
        lets the worker's ``played`` update overtake the press's insert, which
        leaves the flip unapplied and the ``played`` message ahead of the
        ``queued`` one — the ordering ``tests/test_press_pipeline.py`` pins.

        The price is that a cancellation can land *inside* the write. A cancelled
        write transaction rolls back, so the row is written again here, and the
        existence check closes the one window that leaves: a cancellation that
        arrives after the store's ``COMMIT`` ran but before the writing task was
        resumed. A failed write is logged, never raised, because a press must not
        fail merely because its history could not be recorded.

        :param entry: the row to write.
        """
        try:
            await self._store.log_press(entry)
        except asyncio.CancelledError:
            with suppress(BaseException):
                if not await self._press_row_exists(entry):
                    await self._store.log_press(entry)
            raise
        except Exception:
            self._logger.exception(
                "could not record the press of %r in the press log", entry.button_name
            )

    async def _press_row_exists(self, entry: PressLogIn) -> bool:
        """Return whether *entry* is already in the press log.

        Only used on the cancellation path, to tell "the write rolled back" from
        "the write committed and the cancellation arrived before this task
        resumed". One press of one button never shares a canonical ``pressed_at``
        with another, so the pair identifies the row.

        :param entry: the row that may or may not have been written.
        """
        for row in await self._store.recent_presses():
            if row.button_id == entry.button_id and row.pressed_at == entry.pressed_at:
                return True
        return False

    async def _dedupe_window(self, button: Button) -> int:
        """Return the dedupe window in seconds that applies to *button*.

        Design 03 §3's pseudocode says ``button.dedupe_seconds or default``,
        which would turn an explicit ``0`` into the default and make it
        impossible to disable dedupe for one button. This deliberately tests for
        ``None`` instead, so ``0`` really disables the window.

        :param button: button being pressed.
        """
        if button.dedupe_seconds is not None:
            return button.dedupe_seconds
        return await self._int_setting("buttons.default_dedupe_seconds")

    def _is_duplicate(self, button_id: int, now: datetime, window: int) -> bool:
        """Return whether this press falls inside *button_id*'s dedupe window.

        The window is measured from the last *accepted* press, per button, so
        different buttons never interfere. This contains no ``await`` on purpose:
        two presses handled in the same event-loop tick therefore cannot both be
        accepted, because check and set happen without yielding.

        :param button_id: id of the button being pressed.
        :param now: clock reading for this press.
        :param window: dedupe window in seconds; ``0`` or less disables it.
        """
        if window <= 0:
            return False
        previous = self._last_press.get(button_id)
        if previous is not None and _epoch(now) - _epoch(previous) < window:
            return True
        self._last_press[button_id] = now
        return False

    def _quiet_time(self, value: str) -> time | None:
        """Parse a quiet-hours boundary, ignoring a malformed one for this press.

        :param value: stored ``HH:MM`` setting value.
        """
        try:
            return time.fromisoformat(value)
        except ValueError:
            self._logger.warning(
                "quiet hours time %r is not HH:MM; quiet hours are ignored for this press", value
            )
            return None

    async def _int_setting(self, key: str) -> int:
        """Return an integer setting, falling back to its default on a bad value.

        :param key: setting key to read.
        """
        value = await self._store.get_setting(key)
        if isinstance(value, bool) or not isinstance(value, int):
            self._logger.warning("setting %r is not an integer; using the registry default", key)
            return _int_default(key)
        return value

    async def _bool_setting(self, key: str) -> bool:
        """Return a boolean setting, falling back to its default on a bad value.

        :param key: setting key to read.
        """
        value = await self._store.get_setting(key)
        if not isinstance(value, bool):
            self._logger.warning("setting %r is not a boolean; using the registry default", key)
            return _bool_default(key)
        return value

    async def _str_setting(self, key: str) -> str:
        """Return a string setting, falling back to its default on a bad value.

        :param key: setting key to read.
        """
        value = await self._store.get_setting(key)
        if not isinstance(value, str):
            self._logger.warning("setting %r is not a string; using the registry default", key)
            return _str_default(key)
        return value
