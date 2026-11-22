"""The press path: dedupe, quiet hours, action resolution and dispatch.

Fully offline and deterministic: the clock is a fake the test advances by hand,
the dispatcher is a list, the speech source is a stub except in the one
end-to-end retry proof, which drives the real resolver through fake engines, and
the hub is real but never started, so every message is delivered by an explicit
bounded ``flush_once()`` rather than by the background tick. No test here needs
real audio, real TTS, a network, MQTT or a Home Assistant instance.

Owning ticket: HSD-laoi.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio

from chime_deck.audio import MissingAudioFileError, PlaybackError
from chime_deck.health import HealthRegistry, HealthStatus
from chime_deck.hub import HubClient, StateHub
from chime_deck.press import (
    PRESS_DEADLINE_S,
    SOURCE_MQTT,
    SOURCE_UI_TEST,
    PlayJob,
    PressCode,
    PressDeadline,
    PressDispatcher,
    PressOutcome,
    PressService,
    SpeechClip,
    SpeechSource,
    SpeechUnavailable,
    in_quiet_hours,
    resolve_volume,
)
from chime_deck.store import Button, ButtonIn, ConfigStore, PressLogIn, SoundIn
from chime_deck.tts import (
    Availability,
    BakeStore,
    TtsCache,
    TtsCost,
    TtsOptions,
    TtsRenderError,
    TtsSpeechSource,
)

pytestmark = pytest.mark.asyncio

_NEVER = 3600.0
"""Hub flush interval used by the fixture: the background tick never fires."""

_QUIET_START = "22:00"
_QUIET_END = "07:00"


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


@pytest_asyncio.fixture
async def hub(store: ConfigStore) -> AsyncIterator[StateHub]:
    """Yield a real hub that is never started, so only explicit flushes deliver.

    :param store: connected configuration store.
    """
    instance = StateHub(store, HealthRegistry(), flush_interval=_NEVER)
    yield instance
    await instance.stop()


@pytest.fixture
def client(hub: StateHub) -> HubClient:
    """Register one UI client so press messages can be observed.

    :param hub: real hub shared with the service.
    """
    return hub.register()


class _Clock:
    """A deterministic clock the test advances by hand."""

    def __init__(self, now: datetime) -> None:
        """Start the clock at *now*.

        :param now: initial reading; naive values are local wall-clock time.
        """
        self.now = now

    def __call__(self) -> datetime:
        """Return the current reading."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        :param seconds: how far to advance.
        """
        self.now += timedelta(seconds=seconds)


class _FakeDispatcher:
    """Record accepted jobs, or raise the configured error."""

    def __init__(self, error: Exception | None = None) -> None:
        """Create a dispatcher that optionally fails every dispatch.

        :param error: exception raised by :meth:`dispatch`, or ``None`` to accept.
        """
        self.jobs: list[PlayJob] = []
        self.error = error

    async def dispatch(self, job: PlayJob) -> None:
        """Accept *job* unless an error is configured.

        :param job: resolved job from the press service.
        """
        if self.error is not None:
            raise self.error
        self.jobs.append(job)


class _FakeSpeech:
    """Return a canned clip, or raise the configured error."""

    def __init__(self, clip: SpeechClip | None = None, error: Exception | None = None) -> None:
        """Create a speech source with one canned result.

        :param clip: clip returned on success.
        :param error: exception raised instead of returning a clip.
        """
        self.calls: list[tuple[Button, str]] = []
        self.deadlines: list[PressDeadline | None] = []
        self.clip = clip
        self.error = error

    async def resolve(
        self, *, button: Button, engine: str, deadline: PressDeadline | None = None
    ) -> SpeechClip:
        """Record the call and return the canned clip.

        :param button: button being pressed.
        :param engine: effective engine the service chose.
        :param deadline: whole-press budget the service created, or ``None``.
        """
        self.calls.append((button, engine))
        self.deadlines.append(deadline)
        if self.error is not None:
            raise self.error
        if self.clip is None:
            raise SpeechUnavailable("no clip is configured for this test")
        return self.clip


_CONFORMANCE: PressDispatcher = _FakeDispatcher()
"""Pin protocol conformance under mypy, as tests/test_audio_backend.py does."""

_SPEECH_CONFORMANCE: SpeechSource = _FakeSpeech()
"""Pin the speech port conformance under mypy."""


def _drain(client: HubClient) -> list[dict[str, object]]:
    """Return and decode every message currently queued for *client*.

    :param client: client registered with the hub.
    """
    messages: list[dict[str, object]] = []
    while not client.queue.empty():
        raw = client.queue.get_nowait()
        messages.append(cast(dict[str, object], json.loads(raw)))
    return messages


async def _settle(hub: StateHub, client: HubClient) -> list[dict[str, object]]:
    """Flush the hub once, bounded, and return what *client* received.

    :param hub: real hub shared with the service.
    :param client: client registered with the hub.
    """
    await asyncio.wait_for(hub.flush_once(), timeout=1.0)
    return _drain(client)


def _data(message: dict[str, object]) -> dict[str, object]:
    """Return the ``data`` payload of one decoded envelope.

    :param message: decoded ``{"type": ..., "data": ...}`` message.
    """
    return cast(dict[str, object], message["data"])


def _presses(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return only the ``press`` messages from one drained batch.

    :param messages: decoded messages drained from a client.
    """
    return [message for message in messages if message["type"] == "press"]


def _service(
    store: ConfigStore,
    hub: StateHub,
    dispatcher: PressDispatcher,
    clock: Callable[[], datetime],
    *,
    speech: SpeechSource | None = None,
    sounds_dir: Path = Path(),
) -> PressService:
    """Build the press service under test.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param dispatcher: fake playback queue.
    :param clock: deterministic clock.
    :param speech: fake speech source, or ``None`` to model "no engines wired".
    :param sounds_dir: directory a relative sound path resolves against;
        ``Path()`` keeps the historical CWD-relative behaviour.
    """
    return PressService(store, hub, dispatcher, speech=speech, clock=clock, sounds_dir=sounds_dir)


async def _seed_sound(
    store: ConfigStore,
    tmp_path: Path,
    *,
    name: str = "Front Door",
    sound_name: str = "Ding dong",
    dedupe_seconds: int | None = None,
    volume_override: int | None = None,
    quiet_hours_exempt: bool = False,
    enabled: bool = True,
) -> Button:
    """Create a file-backed sound and a sound button that plays it, and return it.

    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory used for the sound path.
    :param name: display name of the button, which also decides its slug.
    :param sound_name: display name of the sound.
    :param dedupe_seconds: per-button dedupe override, or ``None`` for the default.
    :param volume_override: per-button volume, or ``None`` for the master volume.
    :param quiet_hours_exempt: whether the button plays during quiet hours.
    :param enabled: whether the button is enabled.
    """
    sound = await store.create_sound(
        SoundIn(name=sound_name, source_type="file", path=str(tmp_path / f"{name}.mp3"))
    )
    return await store.upsert_button(
        ButtonIn(
            name=name,
            action_type="sound",
            sound_id=sound.id,
            dedupe_seconds=dedupe_seconds,
            volume_override=volume_override,
            quiet_hours_exempt=quiet_hours_exempt,
            enabled=enabled,
        )
    )


async def _seed_tts(
    store: ConfigStore,
    *,
    name: str = "Front Door",
    text: str = "Someone is at the door",
    tts_engine: str | None = None,
    dedupe_seconds: int | None = None,
    quiet_hours_exempt: bool = False,
) -> Button:
    """Create a TTS button and return it.

    :param store: connected configuration store.
    :param name: display name of the button, which also decides its slug.
    :param text: phrase to speak.
    :param tts_engine: per-button engine override, or ``None`` for the default.
    :param dedupe_seconds: per-button dedupe override, or ``None`` for the default.
    :param quiet_hours_exempt: whether the button speaks during quiet hours.
    """
    return await store.upsert_button(
        ButtonIn(
            name=name,
            action_type="tts",
            tts_text=text,
            tts_engine=tts_engine,
            dedupe_seconds=dedupe_seconds,
            quiet_hours_exempt=quiet_hours_exempt,
        )
    )


async def _enable_quiet_hours(store: ConfigStore, volume: int = 0) -> None:
    """Turn quiet hours on with the standard 22:00-07:00 window.

    :param store: connected configuration store.
    :param volume: ``audio.quiet_hours_volume``; ``0`` means suppress.
    """
    await store.set_setting("audio.quiet_hours_enabled", True)
    await store.set_setting("audio.quiet_hours_start", _QUIET_START)
    await store.set_setting("audio.quiet_hours_end", _QUIET_END)
    await store.set_setting("audio.quiet_hours_volume", volume)


async def test_sound_press_queues_one_job_and_records_one_row(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A plain sound press dispatches once, logs once and tells the UI once.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    clock = _Clock(datetime(2026, 1, 1, 12, 0))
    service = _service(store, hub, dispatcher, clock)

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert result.code is None
    assert result.source == SOURCE_MQTT
    assert result.pressed_at.endswith("Z")
    assert result.button_id == button.id

    rows = await store.recent_presses()
    assert len(rows) == 1
    assert rows[0].outcome == "queued"
    assert rows[0].source == "mqtt"
    assert rows[0].sound_name == "Ding dong"
    assert rows[0].button_id == button.id
    assert rows[0].detail is None

    assert len(dispatcher.jobs) == 1
    job = dispatcher.jobs[0]
    assert job.button_id == button.id
    assert job.button_name == "Front Door"
    assert job.path == tmp_path / "Front Door.mp3"
    assert job.volume == 100

    messages = await _settle(hub, client)
    presses = _presses(messages)
    assert len(presses) == 1
    assert _data(presses[0])["outcome"] == "queued"
    assert _data(presses[0])["button_id"] == button.id


async def test_relative_sound_path_resolves_against_sounds_dir(
    store: ConfigStore,
    hub: StateHub,
    tmp_path: Path,
) -> None:
    """A relative row path is joined to the configured library directory.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param tmp_path: pytest temporary directory used as the library root.
    """
    sounds_dir = tmp_path / "sounds"
    sound = await store.create_sound(SoundIn(name="Ding", source_type="file", path="ding.wav"))
    button = await store.upsert_button(
        ButtonIn(name="Door", action_type="sound", sound_id=sound.id)
    )
    dispatcher = _FakeDispatcher()
    service = _service(
        store,
        hub,
        dispatcher,
        _Clock(datetime(2026, 1, 1, 12, 0)),
        sounds_dir=sounds_dir,
    )

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert dispatcher.jobs[0].path == sounds_dir / "ding.wav"


async def test_absolute_sound_path_still_wins_over_sounds_dir(
    store: ConfigStore,
    hub: StateHub,
    tmp_path: Path,
) -> None:
    """``Path(dir) / Path(abs)`` is the absolute path, so legacy rows keep working.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param tmp_path: pytest temporary directory.
    """
    absolute = tmp_path / "elsewhere" / "legacy.wav"
    sound = await store.create_sound(SoundIn(name="Legacy", source_type="file", path=str(absolute)))
    button = await store.upsert_button(
        ButtonIn(name="Door", action_type="sound", sound_id=sound.id)
    )
    dispatcher = _FakeDispatcher()
    service = _service(
        store,
        hub,
        dispatcher,
        _Clock(datetime(2026, 1, 1, 12, 0)),
        sounds_dir=tmp_path / "sounds",
    )

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert dispatcher.jobs[0].path == absolute


async def test_without_sounds_dir_a_relative_path_stays_cwd_relative(
    store: ConfigStore,
    hub: StateHub,
) -> None:
    """Constructing the service directly keeps the historical behaviour.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    """
    sound = await store.create_sound(SoundIn(name="Ding", source_type="file", path="ding.wav"))
    button = await store.upsert_button(
        ButtonIn(name="Door", action_type="sound", sound_id=sound.id)
    )
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert dispatcher.jobs[0].path == Path("ding.wav")


async def test_ui_test_source_is_recorded(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A UI test press is logged with its own source, not as an MQTT press.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path)
    service = _service(store, hub, _FakeDispatcher(), _Clock(datetime(2026, 1, 1, 12, 0)))

    result = await service.press(button.slug, source=SOURCE_UI_TEST)

    assert result.source == "ui_test"
    assert result.outcome is PressOutcome.QUEUED
    rows = await store.recent_presses()
    assert len(rows) == 1
    assert rows[0].source == "ui_test"


async def test_volume_override_wins_over_the_master_volume(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A button's own volume overrides ``audio.volume`` on the job.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path, volume_override=40)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)))

    await service.press(button.slug)

    assert len(dispatcher.jobs) == 1
    assert dispatcher.jobs[0].volume == 40


@pytest.mark.parametrize(
    ("override", "master", "quiet_cap", "expected"),
    [
        (None, 70, None, 70),
        (40, 70, None, 40),
        (120, 70, None, 100),
        (-5, 70, None, 0),
        (40, 70, 20, 20),
        (10, 70, 20, 10),
    ],
)
async def test_resolve_volume_clamps_and_caps(
    override: int | None, master: int, quiet_cap: int | None, expected: int
) -> None:
    """The three volume layers resolve exactly as design 03 §5 says.

    :param override: button volume override, or ``None``.
    :param master: global ``audio.volume``.
    :param quiet_cap: quiet-hours cap, or ``None`` outside the window.
    :param expected: resolved volume.
    """
    assert resolve_volume(override, master, quiet_cap=quiet_cap) == expected


async def test_second_press_inside_the_window_is_deduped(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A second press at the same instant is logged but not dispatched again.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    clock = _Clock(datetime(2026, 1, 1, 12, 0))
    service = _service(store, hub, dispatcher, clock)

    first = await service.press(button.slug)
    second = await service.press(button.slug)

    assert first.outcome is PressOutcome.QUEUED
    assert second.outcome is PressOutcome.DEDUPED
    assert len(dispatcher.jobs) == 1
    rows = await store.recent_presses()
    assert len(rows) == 2
    assert rows[0].outcome == "deduped"
    assert rows[0].detail is not None
    assert "3 second dedupe window" in rows[0].detail


async def test_press_after_the_window_expires_is_queued_again(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """Once the default window has elapsed the press is accepted again.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    clock = _Clock(datetime(2026, 1, 1, 12, 0))
    service = _service(store, hub, dispatcher, clock)

    await service.press(button.slug)
    clock.advance(4)
    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert len(dispatcher.jobs) == 2


async def test_zero_dedupe_seconds_disables_the_window(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """An explicit ``0`` really disables dedupe instead of falling back to the default.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path, dedupe_seconds=0)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)))

    first = await service.press(button.slug)
    second = await service.press(button.slug)

    assert first.outcome is PressOutcome.QUEUED
    assert second.outcome is PressOutcome.QUEUED
    assert len(dispatcher.jobs) == 2


async def test_default_window_comes_from_the_setting(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A button with no override uses ``buttons.default_dedupe_seconds``.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    await store.set_setting("buttons.default_dedupe_seconds", 10)
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    clock = _Clock(datetime(2026, 1, 1, 12, 0))
    service = _service(store, hub, dispatcher, clock)

    await service.press(button.slug)
    clock.advance(5)
    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.DEDUPED
    assert len(dispatcher.jobs) == 1


async def test_dedupe_is_per_button(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """Two different buttons pressed at the same instant both play.

    :param store: connected configuration store.
    :param hub: real hub that receives the press messages.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound paths.
    """
    front = await _seed_sound(store, tmp_path)
    back = await _seed_sound(store, tmp_path, name="Back Door", sound_name="Ding")
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)))

    first = await service.press(front.slug)
    second = await service.press(back.slug)

    assert first.outcome is PressOutcome.QUEUED
    assert second.outcome is PressOutcome.QUEUED
    assert len(dispatcher.jobs) == 2


async def test_exempt_button_plays_during_quiet_hours(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A quiet-hours-exempt button plays at the master volume inside the window.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    await _enable_quiet_hours(store, volume=0)
    button = await _seed_sound(store, tmp_path, quiet_hours_exempt=True)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 23, 30)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert len(dispatcher.jobs) == 1
    assert dispatcher.jobs[0].volume == 100


async def test_quiet_hours_with_zero_volume_suppresses_the_press(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """Inside the window with a zero quiet volume nothing is dispatched.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    await _enable_quiet_hours(store, volume=0)
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 23, 30)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.SUPPRESSED_QUIET_HOURS
    assert dispatcher.jobs == []
    rows = await store.recent_presses()
    assert len(rows) == 1
    assert rows[0].outcome == "suppressed_quiet_hours"
    assert rows[0].detail is not None
    assert "quiet hours 22:00-07:00" in rows[0].detail


async def test_muted_press_suppresses_a_normal_button(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """``audio.muted`` alone silences a press, and the row names mute.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    await store.set_setting("audio.muted", True)
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.SUPPRESSED_MUTED
    assert str(result.outcome) == "suppressed_muted"
    assert dispatcher.jobs == []
    rows = await store.recent_presses()
    assert len(rows) == 1
    assert rows[0].outcome == "suppressed_muted"
    assert rows[0].detail is not None
    assert "mute" in rows[0].detail
    assert not rows[0].detail.startswith("suppressed_muted:")

    messages = await _settle(hub, client)
    presses = _presses(messages)
    assert len(presses) == 1
    assert _data(presses[0])["outcome"] == "suppressed_muted"


async def test_muted_press_suppresses_a_quiet_hours_exempt_button(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """Mute outranks the exemption: even an alarm button is silent while muted (D28).

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    await store.set_setting("audio.muted", True)
    await _enable_quiet_hours(store, volume=0)
    button = await _seed_sound(store, tmp_path, quiet_hours_exempt=True)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 23, 30)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.SUPPRESSED_MUTED
    assert dispatcher.jobs == []


async def test_mute_outranks_quiet_hours(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """Inside the window a muted press is named mute, never quiet hours.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    await store.set_setting("audio.muted", True)
    await _enable_quiet_hours(store, volume=0)
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 23, 30)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.SUPPRESSED_MUTED
    assert result.outcome is not PressOutcome.SUPPRESSED_QUIET_HOURS
    assert dispatcher.jobs == []
    rows = await store.recent_presses()
    assert len(rows) == 1
    assert rows[0].detail is not None
    assert "mute" in rows[0].detail
    assert "quiet hours" not in rows[0].detail


async def test_unmuting_restores_muted_and_exempt_presses(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """Unmuting takes effect immediately, with no restart of the service, and the
    exemption works again inside the window.

    :param store: connected configuration store.
    :param hub: real hub that receives the press messages.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound paths.
    """
    await store.set_setting("audio.muted", True)
    await _enable_quiet_hours(store, volume=0)
    normal = await _seed_sound(store, tmp_path, dedupe_seconds=0)
    exempt = await _seed_sound(
        store, tmp_path, name="Alarm", dedupe_seconds=0, quiet_hours_exempt=True
    )
    dispatcher = _FakeDispatcher()
    clock = _Clock(datetime(2026, 1, 1, 23, 30))
    service = _service(store, hub, dispatcher, clock)

    first = await service.press(normal.slug)
    second = await service.press(exempt.slug)

    assert first.outcome is PressOutcome.SUPPRESSED_MUTED
    assert second.outcome is PressOutcome.SUPPRESSED_MUTED
    assert dispatcher.jobs == []

    await store.set_setting("audio.muted", False)

    # Still inside the window: the exempt alarm button rings again at once.
    third = await service.press(exempt.slug)
    # The ordinary button rings again outside the window, which is the only time
    # it may, so the restore is not a promise that quiet hours were bypassed.
    clock.now = datetime(2026, 1, 1, 12, 0)
    fourth = await service.press(normal.slug)

    assert third.outcome is PressOutcome.QUEUED
    assert fourth.outcome is PressOutcome.QUEUED
    assert [job.volume for job in dispatcher.jobs] == [100, 100]


async def test_dedupe_is_evaluated_before_mute(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """Mute sits after dedupe, and a muted press arms the window just the same.

    :param store: connected configuration store.
    :param hub: real hub that receives the press messages.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    await store.set_setting("audio.muted", True)
    button = await _seed_sound(store, tmp_path)
    service = _service(store, hub, _FakeDispatcher(), _Clock(datetime(2026, 1, 1, 12, 0)))

    first = await service.press(button.slug)
    second = await service.press(button.slug)

    assert first.outcome is PressOutcome.SUPPRESSED_MUTED
    assert second.outcome is PressOutcome.DEDUPED


async def test_ui_test_press_does_not_bypass_mute(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """Mute is a property of the device, not of the press source.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    await store.set_setting("audio.muted", True)
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)))

    result = await service.press(button.slug, source=SOURCE_UI_TEST)

    assert result.outcome is PressOutcome.SUPPRESSED_MUTED
    assert dispatcher.jobs == []
    rows = await store.recent_presses()
    assert len(rows) == 1
    assert rows[0].source == "ui_test"


async def test_press_outside_quiet_hours_plays(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A press outside the window plays normally.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    await _enable_quiet_hours(store, volume=0)
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert len(dispatcher.jobs) == 1


async def test_disabled_quiet_hours_are_ignored_even_inside_the_window(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """With ``audio.quiet_hours_enabled`` false the configured window does nothing.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 23, 30)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert len(dispatcher.jobs) == 1


@pytest.mark.parametrize(
    ("master", "quiet_volume", "expected"),
    [(70, 20, 20), (10, 20, 10)],
)
async def test_quiet_hours_volume_caps_the_job_volume(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
    master: int,
    quiet_volume: int,
    expected: int,
) -> None:
    """The quiet-hours volume lowers the job volume and is reported in the detail.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    :param master: ``audio.volume``.
    :param quiet_volume: ``audio.quiet_hours_volume``.
    :param expected: volume the job must carry.
    """
    await store.set_setting("audio.volume", master)
    await _enable_quiet_hours(store, volume=quiet_volume)
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 23, 30)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert len(dispatcher.jobs) == 1
    assert dispatcher.jobs[0].volume == expected
    rows = await store.recent_presses()
    assert rows[0].detail is not None
    assert f"volume limited to {expected}" in rows[0].detail


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 1, 1, 22, 0), True),
        (datetime(2026, 1, 1, 23, 30), True),
        (datetime(2026, 1, 1, 0, 0), True),
        (datetime(2026, 1, 1, 6, 59), True),
        (datetime(2026, 1, 1, 7, 0), False),
        (datetime(2026, 1, 1, 12, 0), False),
    ],
)
async def test_in_quiet_hours_crosses_midnight(moment: datetime, expected: bool) -> None:
    """A 22:00-07:00 window is half-open and spans midnight.

    :param moment: naive local wall-clock time under test.
    :param expected: whether the moment is inside the window.
    """
    assert in_quiet_hours(moment, time(22, 0), time(7, 0)) is expected


@pytest.mark.parametrize(
    "moment",
    [datetime(2026, 1, 1, 22, 0), datetime(2026, 1, 1, 23, 0), datetime(2026, 1, 1, 12, 0)],
)
async def test_zero_length_window_is_disabled(moment: datetime) -> None:
    """``start == end`` is a zero-length window, so nothing is ever quiet.

    :param moment: naive local wall-clock time under test.
    """
    assert in_quiet_hours(moment, time(22, 0), time(22, 0)) is False


async def test_zero_length_window_does_not_suppress_a_press(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A configured window whose start equals its end leaves the press alone.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    await store.set_setting("audio.quiet_hours_enabled", True)
    await store.set_setting("audio.quiet_hours_start", "22:00")
    await store.set_setting("audio.quiet_hours_end", "22:00")
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 22, 30)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert len(dispatcher.jobs) == 1


async def test_audio_error_from_the_dispatcher_is_a_failed_press(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A typed audio failure is reported once, in the row and to the UI.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher(PlaybackError("cannot play chime.mp3: unsupported format"))
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.FAILED
    assert result.code is PressCode.AUDIO_ERROR
    rows = await store.recent_presses()
    assert len(rows) == 1
    assert rows[0].detail is not None
    assert "cannot play chime.mp3: unsupported format" in rows[0].detail
    messages = await _settle(hub, client)
    presses = _presses(messages)
    assert len(presses) == 1
    assert _data(presses[0])["outcome"] == "failed"


async def test_missing_audio_file_is_reported_as_sound_missing(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A file that vanished is ``sound_missing``, not a generic audio error.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path)
    missing = MissingAudioFileError(tmp_path / "chime.mp3")
    service = _service(store, hub, _FakeDispatcher(missing), _Clock(datetime(2026, 1, 1, 12, 0)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.FAILED
    assert result.code is PressCode.SOUND_MISSING
    rows = await store.recent_presses()
    assert len(rows) == 1
    assert rows[0].detail is not None
    assert rows[0].detail.startswith("sound_missing: ")


async def test_unexpected_dispatcher_error_does_not_escape(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A non-audio exception becomes ``dispatch_error`` instead of a traceback.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path)
    service = _service(
        store, hub, _FakeDispatcher(RuntimeError("boom")), _Clock(datetime(2026, 1, 1, 12, 0))
    )

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.FAILED
    assert result.code is PressCode.DISPATCH_ERROR
    rows = await store.recent_presses()
    assert len(rows) == 1
    assert rows[0].detail is not None
    assert "RuntimeError: boom" in rows[0].detail


async def test_tts_disabled_never_consults_the_speech_port(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
) -> None:
    """``tts.default_engine: none`` fails as ``tts_disabled`` without touching any engine.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    """
    button = await _seed_tts(store)
    fake_speech = _FakeSpeech(clip=SpeechClip(path=Path("/tmp/clip.wav"), engine="piper"))
    await store.set_setting("tts.default_engine", "none")
    service = _service(
        store, hub, _FakeDispatcher(), _Clock(datetime(2026, 1, 1, 12, 0)), speech=fake_speech
    )

    result = await service.press(button.slug)

    assert button.action_type == "tts"
    assert button.tts_text
    assert result.outcome is PressOutcome.FAILED
    assert result.code is PressCode.TTS_DISABLED
    assert result.detail is not None
    assert result.detail.startswith("tts_disabled: ")
    assert fake_speech.calls == []
    rows = await store.recent_presses()
    assert len(rows) == 1


async def test_tts_unavailable_never_collapses_into_tts_disabled(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
) -> None:
    """A broken engine and a deliberate "off" are two different, visible states (D27).

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    """
    button = await _seed_tts(store, dedupe_seconds=0)
    fake_speech = _FakeSpeech(
        error=SpeechUnavailable("no engine available: piper is not installed")
    )
    service = _service(
        store, hub, _FakeDispatcher(), _Clock(datetime(2026, 1, 1, 12, 0)), speech=fake_speech
    )

    unavailable = await service.press(button.slug)
    await store.set_setting("tts.default_engine", "none")
    disabled = await service.press(button.slug)

    assert unavailable.outcome is PressOutcome.FAILED
    assert unavailable.code is PressCode.TTS_UNAVAILABLE
    assert unavailable.detail is not None
    assert unavailable.detail.startswith("tts_unavailable: ")
    assert "piper is not installed" in unavailable.detail

    assert disabled.outcome is PressOutcome.FAILED
    assert disabled.code is PressCode.TTS_DISABLED
    assert disabled.detail is not None
    assert disabled.detail.startswith("tts_disabled: ")
    assert unavailable.detail != disabled.detail
    # The two states must stay distinguishable in the press history (D27).
    assert str(unavailable.code) != str(disabled.code)


async def test_tts_happy_path_queues_the_clip(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
) -> None:
    """A resolved clip is queued with the default engine and its fallback noted.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    """
    button = await _seed_tts(store)
    clip = SpeechClip(path=Path("/tmp/clip.wav"), engine="piper", detail="fell back from ha_tts")
    fake_speech = _FakeSpeech(clip=clip)
    dispatcher = _FakeDispatcher()
    service = _service(
        store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)), speech=fake_speech
    )

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert len(dispatcher.jobs) == 1
    assert dispatcher.jobs[0].path == clip.path
    assert len(fake_speech.calls) == 1
    assert fake_speech.calls[0][0].id == button.id
    assert fake_speech.calls[0][1] == "ha_tts"
    rows = await store.recent_presses()
    assert len(rows) == 1
    assert rows[0].detail is not None
    assert "engine piper" in rows[0].detail
    assert "fell back from ha_tts" in rows[0].detail


async def test_tts_without_a_speech_source_is_unavailable(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
) -> None:
    """A service built without a speech source fails as ``tts_unavailable``.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    """
    button = await _seed_tts(store)
    service = _service(store, hub, _FakeDispatcher(), _Clock(datetime(2026, 1, 1, 12, 0)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.FAILED
    assert result.code is PressCode.TTS_UNAVAILABLE


async def test_button_level_tts_none_is_tts_disabled(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
) -> None:
    """A button whose own ``tts_engine`` is ``none`` is deliberately off (D27).

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    """
    button = await _seed_tts(store, tts_engine="none")
    fake_speech = _FakeSpeech(clip=SpeechClip(path=Path("/tmp/clip.wav"), engine="piper"))
    dispatcher = _FakeDispatcher()
    service = _service(
        store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)), speech=fake_speech
    )

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.FAILED
    assert result.code is PressCode.TTS_DISABLED
    assert result.detail is not None
    assert "tts_engine" in result.detail
    assert fake_speech.calls == []
    assert dispatcher.jobs == []
    rows = await store.recent_presses()
    assert len(rows) == 1
    assert rows[0].outcome == "failed"


async def test_globally_disabled_speech_wins_over_a_button_engine(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
) -> None:
    """A button naming an engine cannot re-enable globally disabled speech (D27).

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    """
    button = await _seed_tts(store, tts_engine="piper")
    fake_speech = _FakeSpeech(clip=SpeechClip(path=Path("/tmp/clip.wav"), engine="piper"))
    dispatcher = _FakeDispatcher()
    await store.set_setting("tts.default_engine", "none")
    service = _service(
        store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)), speech=fake_speech
    )

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.FAILED
    assert result.code is PressCode.TTS_DISABLED
    assert fake_speech.calls == []
    assert dispatcher.jobs == []
    rows = await store.recent_presses()
    assert len(rows) == 1


async def test_unexpected_speech_error_does_not_escape(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
) -> None:
    """An engine raising something unexpected still yields one failed row (design 03 §7).

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    """
    button = await _seed_tts(store)
    fake_speech = _FakeSpeech(error=RuntimeError("engine crashed"))
    dispatcher = _FakeDispatcher()
    service = _service(
        store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)), speech=fake_speech
    )

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.FAILED
    assert result.code is PressCode.TTS_UNAVAILABLE
    assert result.detail is not None
    assert "RuntimeError: engine crashed" in result.detail
    assert dispatcher.jobs == []
    rows = await store.recent_presses()
    assert len(rows) == 1


async def test_unknown_slug_is_logged_without_a_hub_message(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
) -> None:
    """An unknown slug is recorded once, with no button id and nothing pushed.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    """
    service = _service(store, hub, _FakeDispatcher(), _Clock(datetime(2026, 1, 1, 12, 0)))

    result = await service.press("no-such-button")

    assert result.outcome is PressOutcome.FAILED
    assert result.code is PressCode.UNKNOWN_BUTTON
    assert result.button_id is None
    rows = await store.recent_presses()
    assert len(rows) == 1
    assert rows[0].button_id is None
    assert rows[0].button_name == "no-such-button"
    messages = await _settle(hub, client)
    assert messages == []


async def test_sound_without_a_local_file_is_sound_missing(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
) -> None:
    """A url-only sound has no local file, so nothing can be dispatched.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    """
    sound = await store.create_sound(
        SoundIn(
            name="Remote",
            source_type="url",
            url="https://example.invalid/chime.mp3",
        )
    )
    button = await store.upsert_button(
        ButtonIn(name="Front Door", action_type="sound", sound_id=sound.id)
    )
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.FAILED
    assert result.code is PressCode.SOUND_MISSING
    assert dispatcher.jobs == []


async def test_every_press_writes_exactly_one_row(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """Six presses with six different outcomes leave exactly six rows.

    :param store: connected configuration store.
    :param hub: real hub that receives the press messages.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    sound_button = await _seed_sound(store, tmp_path)
    tts_button = await _seed_tts(store, name="Back Door", quiet_hours_exempt=True)
    dispatcher = _FakeDispatcher()
    clock = _Clock(datetime(2026, 1, 1, 21, 59, 58))
    service = _service(store, hub, dispatcher, clock)

    queued = await service.press(sound_button.slug)
    deduped = await service.press(sound_button.slug)

    await _enable_quiet_hours(store, volume=0)
    clock.advance(4)
    suppressed = await service.press(sound_button.slug)

    # Mute outranks quiet hours, so this press inside the window is reported as
    # mute rather than quiet hours.
    clock.advance(4)
    await store.set_setting("audio.muted", True)
    muted = await service.press(sound_button.slug)
    await store.set_setting("audio.muted", False)

    # Quiet hours suppress before the action is resolved, so move past the window
    # to exercise a dispatch failure on the same button.
    clock.now = datetime(2026, 1, 2, 7, 0)
    dispatcher.error = PlaybackError("cannot play chime.mp3: unsupported format")
    failed = await service.press(sound_button.slug)

    clock.advance(4)
    await store.set_setting("tts.default_engine", "none")
    disabled = await service.press(tts_button.slug)

    assert queued.outcome is PressOutcome.QUEUED
    assert deduped.outcome is PressOutcome.DEDUPED
    assert suppressed.outcome is PressOutcome.SUPPRESSED_QUIET_HOURS
    assert failed.outcome is PressOutcome.FAILED
    assert muted.outcome is PressOutcome.SUPPRESSED_MUTED
    assert disabled.code is PressCode.TTS_DISABLED

    rows = await store.recent_presses()
    assert len(rows) == 6
    assert [row.outcome for row in rows] == [
        "failed",
        "failed",
        "suppressed_muted",
        "suppressed_quiet_hours",
        "deduped",
        "queued",
    ]
    assert rows[0].detail is not None
    assert rows[0].detail.startswith("tts_disabled: ")
    assert rows[1].detail is not None
    assert rows[1].detail.startswith("audio_error: ")


async def test_suppressed_press_arms_the_dedupe_window(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """Quiet hours are applied after dedupe, and a suppression is an accepted press.

    :param store: connected configuration store.
    :param hub: real hub that receives the press messages.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    await _enable_quiet_hours(store, volume=0)
    button = await _seed_sound(store, tmp_path)
    service = _service(store, hub, _FakeDispatcher(), _Clock(datetime(2026, 1, 1, 23, 30)))

    first = await service.press(button.slug)
    second = await service.press(button.slug)

    assert first.outcome is PressOutcome.SUPPRESSED_QUIET_HOURS
    assert second.outcome is PressOutcome.DEDUPED


async def test_failed_press_arms_the_dedupe_window(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A failed press still arms the window, so it is measured from the accepted press.

    :param store: connected configuration store.
    :param hub: real hub that receives the press messages.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path)
    service = _service(
        store, hub, _FakeDispatcher(RuntimeError("boom")), _Clock(datetime(2026, 1, 1, 12, 0))
    )

    first = await service.press(button.slug)
    second = await service.press(button.slug)

    assert first.outcome is PressOutcome.FAILED
    assert second.outcome is PressOutcome.DEDUPED


async def test_concurrent_presses_of_one_button_queue_once(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """Two presses in the same event-loop tick cannot both be accepted.

    :param store: connected configuration store.
    :param hub: real hub that receives the press messages.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)))

    results = await asyncio.wait_for(
        asyncio.gather(service.press(button.slug), service.press(button.slug)), timeout=2.0
    )

    assert sorted(result.outcome for result in results) == [
        PressOutcome.DEDUPED,
        PressOutcome.QUEUED,
    ]
    assert len(dispatcher.jobs) == 1
    rows = await store.recent_presses()
    assert len(rows) == 2


async def test_aware_clock_is_converted_to_local_wall_clock(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """An aware clock is localised before quiet hours are decided.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    await _enable_quiet_hours(store, volume=0)
    button = await _seed_sound(store, tmp_path)
    dispatcher = _FakeDispatcher()
    service = _service(
        store,
        hub,
        dispatcher,
        lambda: datetime(2026, 1, 1, 23, 30).astimezone(),
    )

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.SUPPRESSED_QUIET_HOURS
    assert dispatcher.jobs == []


async def test_disabled_button_still_dispatches(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """The UI test action exists on every row, including a disabled button.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path, enabled=False)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)))

    result = await service.press(button.slug, source=SOURCE_UI_TEST)

    assert button.enabled is False
    assert result.outcome is PressOutcome.QUEUED
    assert len(dispatcher.jobs) == 1


async def test_a_measured_synthesis_time_reaches_the_result_and_the_row(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
) -> None:
    """A clip carrying a measurement stores it on the press row and the result.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    """
    button = await _seed_tts(store)
    clip = SpeechClip(path=Path("/tmp/clip.wav"), engine="piper", synthesis_ms=240)
    service = _service(
        store,
        hub,
        _FakeDispatcher(),
        _Clock(datetime(2026, 1, 1, 12, 0)),
        speech=_FakeSpeech(clip=clip),
    )

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert result.synthesis_ms == 240
    (row,) = await store.recent_presses()
    assert row.synthesis_ms == 240
    # The measurement is not a degradation, so it must not leak into the detail.
    assert row.detail is not None
    assert "240" not in row.detail


async def test_a_failed_press_records_no_synthesis_time(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
) -> None:
    """A render that never succeeded measured nothing, so the row stays ``NULL``.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    """
    button = await _seed_tts(store)
    fake_speech = _FakeSpeech(error=SpeechUnavailable("piper could not render speech: boom"))
    service = _service(
        store, hub, _FakeDispatcher(), _Clock(datetime(2026, 1, 1, 12, 0)), speech=fake_speech
    )

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.FAILED
    assert result.synthesis_ms is None
    (row,) = await store.recent_presses()
    assert row.synthesis_ms is None


async def test_a_sound_press_records_no_synthesis_time(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A sound button synthesises nothing, so its press row carries no measurement.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory used for the sound path.
    """
    button = await _seed_sound(store, tmp_path)
    service = _service(store, hub, _FakeDispatcher(), _Clock(datetime(2026, 1, 1, 12, 0)))

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert result.synthesis_ms is None
    (row,) = await store.recent_presses()
    assert row.synthesis_ms is None


_HANGING_WAIT_S = 0.05
"""Bound for a press that must not finish on its own; a timeout is the expected result."""

_SLOW_WRITE_S = 0.05
"""Time the slow store spends inside its press-log write, so a cancellation lands in it."""

_CANCEL_WAIT_S = 0.02
"""Bound for the press that is cancelled while its row is being written."""


class _HangingSpeech:
    """A speech source whose ``resolve`` never answers, so only a cancellation ends it."""

    def __init__(self) -> None:
        """Create the source; it waits on an event nothing ever sets."""
        self.entered = asyncio.Event()

    async def resolve(
        self, *, button: Button, engine: str, deadline: PressDeadline | None = None
    ) -> SpeechClip:
        """Announce the call and then wait for a cancellation.

        :param button: button being pressed.
        :param engine: effective engine the service chose.
        :param deadline: whole-press budget the service created, or ``None``.
        :raises AssertionError: never; the wait raises ``CancelledError`` first.
        """
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("a never-set event must not return")


class _SlowPressLogStore(ConfigStore):
    """A store whose press-log write is slow enough for a cancellation to land inside it."""

    async def log_press(self, entry: PressLogIn) -> None:
        """Wait a few tens of milliseconds, then delegate the real write.

        :param entry: press-log row to write.
        """
        await asyncio.sleep(_SLOW_WRITE_S)
        await super().log_press(entry)


async def test_a_cancelled_press_still_writes_one_failed_row(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
) -> None:
    """A press the seam cancels leaves exactly one honest ``press_cancelled`` row.

    HSD-96k6: a cancelled press used to write no row at all, so a silent doorbell
    had nothing to explain it. The row must claim nothing that did not happen —
    no audio was resolved, let alone played.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    """
    button = await _seed_tts(store)
    service = _service(
        store,
        hub,
        _FakeDispatcher(),
        _Clock(datetime(2026, 1, 1, 12, 0)),
        speech=_HangingSpeech(),
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(service.press(button.slug), timeout=_HANGING_WAIT_S)

    (row,) = await store.recent_presses()
    assert row.outcome == "failed"
    assert row.detail is not None
    assert row.detail.startswith("press_cancelled: ")
    assert "nothing was played" in row.detail

    presses = _presses(await _settle(hub, client))
    assert len(presses) == 1
    assert _data(presses[0])["outcome"] == "failed"


async def test_a_cancellation_during_the_row_write_keeps_the_real_outcome(
    tmp_path: Path,
    hub: StateHub,
) -> None:
    """The inline write is redone after a cancellation, so the row keeps the real outcome.

    The write is deliberately not shielded: it runs inline, in the caller's task.
    The store takes one write lock per transaction, so submitting the press's
    ``queued`` insert first is exactly what keeps it ahead of the playback
    worker's ``played`` update; moving the write into a task instead lets the
    update overtake the insert, which the pipeline tests see as a lost flip.

    The price is that a cancellation can land inside the write itself, so
    ``PressService._log_press`` writes the row again after a cancelled write,
    checking ``_press_row_exists`` first so a write that already committed is
    never duplicated. Either way the cancellation neither loses the row nor
    replaces its outcome: this press was queued, so the row says ``queued`` and
    never ``press_cancelled`` (HSD-96k6).

    :param tmp_path: pytest temporary directory used for the database and sound.
    :param hub: real hub that receives the press message.
    """
    async with _SlowPressLogStore(tmp_path / "slow.db") as store:
        button = await _seed_sound(store, tmp_path)
        service = _service(store, hub, _FakeDispatcher(), _Clock(datetime(2026, 1, 1, 12, 0)))

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(service.press(button.slug), timeout=_CANCEL_WAIT_S)

        (row,) = await store.recent_presses()
        assert row.outcome == "queued"


_PAYLOAD = b"RIFF....WAVEfmt "
"""Non-empty bytes for a fake render: the cache checks that a file exists, not the audio."""


class _ScriptedEngine:
    """Write a placeholder payload, or raise the configured render error."""

    def __init__(self, name: str, *, error: TtsRenderError | None = None) -> None:
        """Create an engine that always reports itself as available.

        :param name: engine name registered with the resolver.
        :param error: failure raised by every render, or ``None`` to write a payload.
        """
        self.name = name
        self.error = error
        self.renders = 0

    async def available(self) -> Availability:
        """Report the engine as available, because a render failure is not a probe failure."""
        return Availability(True)

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Count the render, then write a payload or raise the configured error.

        :param text: phrase to speak.
        :param options: options for this render.
        :param destination: exact output path the cache publishes.
        :raises TtsRenderError: when *error* is configured.
        """
        self.renders += 1
        if self.error is not None:
            raise self.error
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_PAYLOAD)
        return destination

    async def warmup(self) -> None:
        """Do nothing, because this fake has nothing to prepare at startup."""


@pytest.mark.integration
async def test_a_transient_render_failure_names_the_fallback_engine_in_the_press_row(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """HSD-96k6 requires the row of a retried render to name the engine that spoke.

    This pins the ticket bullet "retry only when the remaining deadline can pay
    for a bounded second attempt, and record the fallback in
    ``press_log.detail``". The real resolver is used rather than a canned clip,
    so the proof covers the whole press: ``ha_tts`` fails transiently, the press
    retries once on ``espeak_ng``, exactly one job is dispatched, and the single
    ``press_log`` row names both the retry and the engine that really spoke, so
    the history explains which voice the doorbell used.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub, so the press message is observable.
    :param tmp_path: pytest temporary directory used for the clip cache.
    """
    button = await _seed_tts(store)
    ha = _ScriptedEngine("ha_tts", error=TtsRenderError("Home Assistant timed out", transient=True))
    espeak = _ScriptedEngine("espeak_ng")
    source = TtsSpeechSource(store, [ha, espeak], TtsCache(tmp_path / "tts-cache"))
    dispatcher = _FakeDispatcher()
    service = _service(
        store,
        hub,
        dispatcher,
        _Clock(datetime(2026, 1, 1, 12, 0)),
        speech=source,
    )

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    (row,) = await store.recent_presses()
    assert row.detail is not None
    assert "retried on espeak_ng" in row.detail
    assert "engine espeak_ng" in row.detail
    assert ha.renders == 1
    assert espeak.renders == 1
    assert len(dispatcher.jobs) == 1
    presses = _presses(await _settle(hub, client))
    assert len(presses) == 1
    assert _data(presses[0])["outcome"] == "queued"


async def test_the_press_hands_the_speech_port_the_whole_press_budget(
    store: ConfigStore,
    hub: StateHub,
) -> None:
    """The press really creates the deadline and hands it to the speech port.

    The deadline is one budget for the whole press, so its budget must be exactly
    :data:`PRESS_DEADLINE_S` and it must already have spent some of it by the time
    the speech port sees it.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    """
    button = await _seed_tts(store)
    fake = _FakeSpeech(clip=SpeechClip(path=Path("/tmp/clip.wav"), engine="piper"))
    service = _service(
        store,
        hub,
        _FakeDispatcher(),
        _Clock(datetime(2026, 1, 1, 12, 0)),
        speech=fake,
    )

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    (deadline,) = fake.deadlines
    assert deadline is not None
    assert deadline.budget == PRESS_DEADLINE_S
    assert deadline.remaining() <= PRESS_DEADLINE_S


class _CostingEngine:
    """A fake ``http_tts`` engine that also reports what its synthesis cost.

    It is the press-path proof of the cost channel (HSD-s856): a billed engine
    is one that satisfies :class:`~chime_deck.tts.SynthesisCosting`, and the
    press row must say what the press actually paid without ever flipping the
    ``tts`` health component.
    """

    name = "http_tts"

    def __init__(self) -> None:
        """Create an engine that has rendered nothing yet."""
        self.renders: list[str] = []
        self.prices: list[str] = []

    async def available(self) -> Availability:
        """Report available; the resolver probes before every render."""
        return Availability(True)

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Write a payload at *destination* and record the render.

        :param text: phrase to speak.
        :param options: options for this render.
        :param destination: exact output path.
        """
        self.renders.append(text)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"RIFF....WAVEfmt ")
        return destination

    async def warmup(self) -> None:
        """Do nothing; there is nothing to prime."""
        return None

    async def estimate_cost(self, text: str) -> TtsCost:
        """Return what *text* cost, at a deliberately simple price.

        :param text: the phrase that was sent.
        """
        self.prices.append(text)
        return TtsCost(chars=len(text), usd=0.02 * len(text) / 1000.0)


async def test_a_billed_http_tts_press_notes_the_cost_and_leaves_tts_ready(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A billed synthesis reports its cost in the row, and never through ``detail``.

    ``clip.detail`` is the DEGRADED trigger in ``speech.py``, and a successful
    paid render is not a degraded component, so the note is the only trace the
    cost may leave.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory.
    """
    button = await _seed_tts(store, tts_engine="http_tts", dedupe_seconds=0)
    engine = _CostingEngine()
    health = HealthRegistry()
    source = TtsSpeechSource(store, [engine], TtsCache(tmp_path / "tts-cache"), health=health)
    dispatcher = _FakeDispatcher()
    service = _service(store, hub, dispatcher, _Clock(datetime(2026, 1, 1, 12, 0)), speech=source)
    note = TtsCost(chars=len("Someone is at the door"), usd=0.02 * 22 / 1000.0).note()

    result = await service.press(button.slug)

    assert result.outcome is PressOutcome.QUEUED
    assert engine.renders == ["Someone is at the door"]
    rows = await store.recent_presses()
    assert rows[0].detail == f"engine http_tts; {note}"
    assert health.get("tts") is HealthStatus.READY
    assert health.message()["detail"] == "engine http_tts"


async def test_a_cache_hit_bills_nothing(
    store: ConfigStore,
    hub: StateHub,
    client: HubClient,
    tmp_path: Path,
) -> None:
    """A cache hit reports ``cost=None``: nothing was synthesised, so nothing was paid.

    :param store: connected configuration store.
    :param hub: real hub that receives the press message.
    :param client: client registered with the hub.
    :param tmp_path: pytest temporary directory.
    """
    button = await _seed_tts(store, tts_engine="http_tts", dedupe_seconds=0)
    engine = _CostingEngine()
    source = TtsSpeechSource(store, [engine], TtsCache(tmp_path / "tts-cache"))

    first = await source.resolve(button=button, engine="http_tts")
    hit = await source.resolve(button=button, engine="http_tts")

    assert first.cost is not None
    assert hit.path == first.path
    assert hit.cost is None
    assert len(engine.renders) == 1


async def test_a_bake_hit_bills_nothing(
    store: ConfigStore,
    tmp_path: Path,
) -> None:
    """A bake hit reports ``cost=None`` too: the synthesis happened on an earlier press.

    :param store: connected configuration store.
    :param tmp_path: pytest temporary directory.
    """
    button = await _seed_tts(store, tts_engine="http_tts")
    engine = _CostingEngine()
    source = TtsSpeechSource(
        store,
        [engine],
        TtsCache(tmp_path / "tts-cache"),
        bakes=BakeStore(tmp_path / "baked"),
    )

    await source.bake_button(button=button)
    clip = await source.resolve(button=button, engine="http_tts")

    assert clip.cost is None
    assert len(engine.renders) == 1
