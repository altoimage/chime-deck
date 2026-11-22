"""The acceptance sequences: a real press really makes the device play.

These tests drive the REAL application wiring: the real lifespan, the real
:class:`~chime_deck.wiring.ChimePipeline`, the real playback worker and the
real press service, with only the devices faked
(:class:`~chime_fakes.RecordingBackend` for the speaker and
:class:`~mqtt_fakes.FakeBroker` for the broker). A press arrives the way Home
Assistant sends it — on the button's command topic through the broker's own
router — and every effect is waited for, never assumed:

* ``PressService._finish`` writes the ``press_log`` row BEFORE pushing, and
  :class:`~chime_deck.playback.HubPlaybackRecorder` revises the row BEFORE
  pushing the flip. "The hub message arrived" therefore implies "the row is
  written", which is what lets these tests assert the store after seeing a
  message.
* Every wait is bounded: hub reads use ``asyncio.wait_for`` and every condition
  the application reaches on its own is polled with a deadline.

No listening socket, no network, no broker and no sound card is involved.

Owning ticket: HSD-dtpo.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from chime_fakes import RecordingBackend, build_offline_wiring
from fastapi import FastAPI
from log_isolation import preserve_log_state
from mqtt_fakes import FakeBroker, FakeClient, wait_until

from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig
from chime_deck.health import HealthStatus
from chime_deck.hub import HubClient
from chime_deck.mqtt import MqttSettings, command_topic
from chime_deck.press import SOURCE_MQTT, SOURCE_UI_TEST
from chime_deck.store import ButtonIn, ConfigStore, PressLog, SoundIn

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_HUB_TIMEOUT_S = 2.0
"""Bound for one hub message, delivered by the 0.1 s background flush."""

_ROW_TIMEOUT_S = 2.0
"""Bound for one bounded poll of the press history."""

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_POLL_S = 0.01
"""Seconds between press-history polls."""

_PRESS = b"PRESS"
"""The only payload that presses a button."""

_MAX_HUB_FRAMES = 8
"""Most hub frames one press read may skip before it gives up."""

_INSTANCE = "default"
"""Instance id the seeded settings derive their topics from."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test."""
    with preserve_log_state():
        yield


class _Clock:
    """A press clock that never moves unless the test says so."""

    def __init__(self, now: datetime) -> None:
        """Start the clock at *now*.

        :param now: reading returned by every call; a naive value is local time.
        """
        self.now = now

    def __call__(self) -> datetime:
        """Return the current reading."""
        return self.now


@dataclass(frozen=True, slots=True)
class _Button:
    """One seeded button and the topics and files that belong to it."""

    button_id: int
    slug: str
    sound_path: Path | None
    sound_id: int | None
    command_topic: str


def _config(tmp_path: Path) -> ServerConfig:
    """Return the server configuration every test in this module uses.

    :param tmp_path: pytest temporary directory, used as the data directory.
    """
    return ServerConfig(
        data_dir=tmp_path, static_dir=tmp_path / "no-such-dist", seed_demo_sounds=False
    )


def _topics(instance_id: str = _INSTANCE) -> MqttSettings:
    """Return settings good enough to derive a command topic.

    :param instance_id: instance id whose topic tree is wanted.
    """
    return MqttSettings(host="broker.test", instance_id=instance_id)


async def _seed_sound_buttons(
    tmp_path: Path,
    *,
    settings: dict[str, object] | None = None,
    buttons: Sequence[tuple[str, bool]] = (("Front Door", False),),
) -> list[_Button]:
    """Write settings and one sound button per ``(name, quiet_hours_exempt)`` pair.

    :param tmp_path: pytest temporary directory.
    :param settings: settings written before anything starts.
    :param buttons: ``(display name, quiet-hours exempt)`` for each button.
    """
    seeded: list[_Button] = []
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        for key, value in (settings or {}).items():
            await store.set_setting(key, value)
        for index, (name, exempt) in enumerate(buttons):
            path = tmp_path / f"sound-{index}.mp3"
            path.write_bytes(b"an offline test sound")
            sound = await store.create_sound(
                SoundIn(name=f"Sound {index}", source_type="file", path=str(path))
            )
            button = await store.upsert_button(
                ButtonIn(
                    name=name,
                    action_type="sound",
                    sound_id=sound.id,
                    quiet_hours_exempt=exempt,
                )
            )
            seeded.append(
                _Button(
                    button_id=button.id,
                    slug=button.slug,
                    sound_path=path,
                    sound_id=sound.id,
                    command_topic=command_topic(_topics(), button.slug),
                )
            )
    return seeded


async def _seed_tts_button(tmp_path: Path, *, settings: dict[str, object] | None = None) -> _Button:
    """Write settings and one TTS button.

    :param tmp_path: pytest temporary directory.
    :param settings: settings written before anything starts.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        for key, value in (settings or {}).items():
            await store.set_setting(key, value)
        button = await store.upsert_button(
            ButtonIn(name="Announcement", action_type="tts", tts_text="Someone is at the door")
        )
    return _Button(button.id, button.slug, None, None, command_topic(_topics(), button.slug))


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the real lifespan and yield a client bound to the application.

    :param app: application under test.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=_HTTP_TIMEOUT_S
        ) as client:
            yield client


def _state(app: FastAPI) -> AppState:
    """Return the application state attached to *app*.

    :param app: application built by :func:`create_app`.
    """
    return cast(AppState, app.state.doorbell)


async def _connect(state: AppState, broker: FakeBroker) -> FakeClient:
    """Connect the current generation's fake client and wait until presses route.

    :param state: running application state.
    :param broker: in-memory broker the bridge was built against.
    """
    client = broker.clients[-1]
    broker.connect(client)
    await wait_until(lambda: state.chime.bridge is not None and state.chime.bridge.connected)
    discovery = state.chime.discovery
    assert discovery is not None
    topics = tuple(discovery.command_topics)
    assert topics
    await wait_until(lambda: all(topic in client.subscriptions for topic in topics))
    return client


def _press(broker: FakeBroker, button: _Button) -> None:
    """Publish ``PRESS`` on the button's command topic, as Home Assistant does.

    :param broker: in-memory broker.
    :param button: button whose command topic is pressed.
    """
    broker.publish_from_test(button.command_topic, _PRESS)


async def _open_hub(state: AppState) -> HubClient:
    """Register a client after draining the startup/connect health flip.

    The bridge records a health change when it connects; flushing it before the
    client exists keeps the reads below about presses only.

    :param state: running application state holding the hub.
    """
    await state.hub.flush_once()
    return state.hub.register()


async def _next_press(state: AppState, hub_client: HubClient) -> dict[str, object]:
    """Read the next ``press`` message from the hub, bounded.

    ``config_changed`` and ``health`` frames may share the channel; they are
    skipped. The number of frames examined is bounded as well as each read.

    :param state: running application state holding the hub.
    :param hub_client: client registered on that hub.
    """
    for _ in range(_MAX_HUB_FRAMES):
        payload = await asyncio.wait_for(state.hub.next_payload(hub_client), timeout=_HUB_TIMEOUT_S)
        message = cast(dict[str, object], json.loads(payload))
        if message["type"] == "press":
            return cast(dict[str, object], message["data"])
    raise AssertionError(f"no press message in {_MAX_HUB_FRAMES} hub frames")


async def _wait_for_row(state: AppState, *, outcome: str, button_id: int | None = None) -> PressLog:
    """Poll the press history until a row with *outcome* exists, bounded.

    :param state: running application state.
    :param outcome: outcome to wait for, for example ``"played"``.
    :param button_id: button the row must belong to, or ``None`` for any.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _ROW_TIMEOUT_S
    while loop.time() < deadline:
        entries = await state.store.recent_presses()
        for entry in entries:
            if entry.outcome == outcome and (button_id is None or entry.button_id == button_id):
                return entry
        await asyncio.sleep(_POLL_S)
    raise AssertionError(f"no {outcome!r} press row appeared within {_ROW_TIMEOUT_S} s")


async def _wait_for_newest(state: AppState, *, outcome: str) -> PressLog:
    """Poll until the newest press row has *outcome*, bounded.

    :param state: running application state.
    :param outcome: outcome the newest row must report, for example ``"played"``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _ROW_TIMEOUT_S
    while loop.time() < deadline:
        entries = await state.store.recent_presses()
        if entries and entries[0].outcome == outcome:
            return entries[0]
        await asyncio.sleep(_POLL_S)
    raise AssertionError(f"the newest press row never became {outcome!r}")


async def test_an_mqtt_press_plays_the_sound(
    tmp_path: Path, fake_broker: FakeBroker, fake_backend: RecordingBackend
) -> None:
    """A command-topic press plays once and flips the row and the hub to ``played``.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: in-memory broker.
    :param fake_backend: recording speaker.
    """
    (button,) = await _seed_sound_buttons(tmp_path)
    gate = asyncio.Event()
    fake_backend.gate = gate
    app = create_app(_config(tmp_path))
    state = _state(app)

    try:
        async with _running(app):
            await _connect(state, fake_broker)
            hub_client = await _open_hub(state)
            _press(fake_broker, button)

            queued = await _next_press(state, hub_client)
            assert queued["outcome"] == "queued"
            assert queued["button_id"] == button.button_id
            # The message implies the row: it is written before the push. The
            # gate keeps the worker inside ``play``, so the row is still queued.
            queued_row = await _wait_for_row(state, outcome="queued", button_id=button.button_id)
            assert queued_row.pressed_at == queued["pressed_at"]
            assert queued_row.source == SOURCE_MQTT

            gate.set()
            played = await _next_press(state, hub_client)
            assert played["outcome"] == "played"
            assert played["button_id"] == button.button_id
            assert played["pressed_at"] == queued["pressed_at"]

            played_row = await _wait_for_row(state, outcome="played", button_id=button.button_id)
            assert played_row.id == queued_row.id
            assert played_row.pressed_at == queued["pressed_at"]
    finally:
        gate.set()

    assert fake_backend.played == [(cast(Path, button.sound_path), 100)]


async def test_two_presses_inside_the_dedupe_window_play_once(
    tmp_path: Path, fake_broker: FakeBroker, fake_backend: RecordingBackend
) -> None:
    """A second press inside the window is recorded ``deduped`` and plays nothing.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: in-memory broker.
    :param fake_backend: recording speaker.
    """
    (button,) = await _seed_sound_buttons(tmp_path)
    clock = _Clock(datetime(2026, 1, 1, 12, 0, 0))
    wiring = build_offline_wiring(broker=fake_broker, backend=fake_backend, press_clock=clock)
    app = create_app(_config(tmp_path), wiring=wiring)
    state = _state(app)

    async with _running(app):
        await _connect(state, fake_broker)
        hub_client = await _open_hub(state)

        _press(fake_broker, button)
        assert (await _next_press(state, hub_client))["outcome"] == "queued"
        assert (await _next_press(state, hub_client))["outcome"] == "played"

        _press(fake_broker, button)
        deduped = await _next_press(state, hub_client)
        assert deduped["outcome"] == "deduped"
        deduped_row = await _wait_for_row(state, outcome="deduped", button_id=button.button_id)
        assert deduped_row.pressed_at == deduped["pressed_at"]

    assert fake_backend.played == [(cast(Path, button.sound_path), 100)]


async def test_quiet_hours_suppress_a_non_exempt_button_and_play_an_exempt_one(
    tmp_path: Path, fake_broker: FakeBroker, fake_backend: RecordingBackend
) -> None:
    """Inside the window a capped-at-zero press is silent, unless it is exempt.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: in-memory broker.
    :param fake_backend: recording speaker.
    """
    ordinary, exempt = await _seed_sound_buttons(
        tmp_path,
        settings={
            "audio.quiet_hours_enabled": True,
            "audio.quiet_hours_start": "22:00",
            "audio.quiet_hours_end": "07:00",
            "audio.quiet_hours_volume": 0,
        },
        buttons=(("Front Door", False), ("Alarm", True)),
    )
    clock = _Clock(datetime(2026, 1, 1, 23, 30, 0))
    wiring = build_offline_wiring(broker=fake_broker, backend=fake_backend, press_clock=clock)
    app = create_app(_config(tmp_path), wiring=wiring)
    state = _state(app)

    async with _running(app):
        await _connect(state, fake_broker)
        hub_client = await _open_hub(state)

        _press(fake_broker, ordinary)
        suppressed = await _next_press(state, hub_client)
        assert suppressed["outcome"] == "suppressed_quiet_hours"
        suppressed_row = await _wait_for_row(
            state, outcome="suppressed_quiet_hours", button_id=ordinary.button_id
        )
        assert suppressed_row.source == SOURCE_MQTT
        assert fake_backend.played == []

        _press(fake_broker, exempt)
        assert (await _next_press(state, hub_client))["outcome"] == "queued"
        assert (await _next_press(state, hub_client))["outcome"] == "played"
        await _wait_for_row(state, outcome="played", button_id=exempt.button_id)

    assert fake_backend.played == [(cast(Path, exempt.sound_path), 100)]


async def test_a_muted_device_suppresses_an_mqtt_press(
    tmp_path: Path, fake_broker: FakeBroker, fake_backend: RecordingBackend
) -> None:
    """A mute saved through the settings API makes the next MQTT press silent (D28).

    :param tmp_path: pytest temporary directory.
    :param fake_broker: in-memory broker.
    :param fake_backend: recording speaker.
    """
    (button,) = await _seed_sound_buttons(tmp_path)
    wiring = build_offline_wiring(broker=fake_broker, backend=fake_backend)
    app = create_app(_config(tmp_path), wiring=wiring)
    state = _state(app)

    async with _running(app) as client:
        await _connect(state, fake_broker)
        hub_client = await _open_hub(state)

        response = await client.patch("/api/settings", json={"audio.muted": True})
        assert response.status_code == 200

        _press(fake_broker, button)
        muted = await _next_press(state, hub_client)
        assert muted["outcome"] == "suppressed_muted"
        muted_row = await _wait_for_row(
            state, outcome="suppressed_muted", button_id=button.button_id
        )
        assert muted_row.source == SOURCE_MQTT

    assert fake_backend.played == []


async def test_the_ui_and_mqtt_share_one_press_service(
    tmp_path: Path, fake_broker: FakeBroker, fake_backend: RecordingBackend
) -> None:
    """One dedupe window covers both sources; with the window off both really play.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: in-memory broker.
    :param fake_backend: recording speaker.
    """
    (button,) = await _seed_sound_buttons(tmp_path)
    clock = _Clock(datetime(2026, 1, 1, 12, 0, 0))
    wiring = build_offline_wiring(broker=fake_broker, backend=fake_backend, press_clock=clock)
    app = create_app(_config(tmp_path), wiring=wiring)
    state = _state(app)

    async with _running(app) as client:
        await _connect(state, fake_broker)
        hub_client = await _open_hub(state)

        # MQTT first, then the UI inside the same dedupe window.
        _press(fake_broker, button)
        assert (await _next_press(state, hub_client))["outcome"] == "queued"
        assert (await _next_press(state, hub_client))["outcome"] == "played"
        first_row = await _wait_for_row(state, outcome="played", button_id=button.button_id)

        response = await client.post(f"/api/buttons/{button.button_id}/press")
        assert response.status_code == 200
        body = cast(dict[str, object], response.json())
        assert body["outcome"] == "deduped"
        assert body["source"] == SOURCE_UI_TEST
        assert (await _next_press(state, hub_client))["outcome"] == "deduped"
        deduped_row = await _wait_for_row(state, outcome="deduped", button_id=button.button_id)
        assert deduped_row.source == SOURCE_UI_TEST
        assert len(fake_backend.played) == 1

        # Disable the window: the same pair must both play and both be logged.
        await state.store.set_setting("buttons.default_dedupe_seconds", 0)

        _press(fake_broker, button)
        assert (await _next_press(state, hub_client))["outcome"] == "queued"
        assert (await _next_press(state, hub_client))["outcome"] == "played"

        response = await client.post(f"/api/buttons/{button.button_id}/press")
        assert response.status_code == 200
        body = cast(dict[str, object], response.json())
        assert body["outcome"] == "queued"
        await _wait_for_newest(state, outcome="played")

        played_rows = [
            entry
            for entry in await state.store.recent_presses()
            if entry.outcome == "played" and entry.button_id == button.button_id
        ]
        assert len(played_rows) == 3
        # The same pair from two sources, identical apart from the source.
        ui_row, mqtt_row = played_rows[0], played_rows[1]
        assert ui_row.source == SOURCE_UI_TEST
        assert mqtt_row.source == SOURCE_MQTT
        assert (
            ui_row.button_id,
            ui_row.button_name,
            ui_row.pressed_at,
            ui_row.outcome,
            ui_row.detail,
            ui_row.sound_name,
        ) == (
            mqtt_row.button_id,
            mqtt_row.button_name,
            mqtt_row.pressed_at,
            mqtt_row.outcome,
            mqtt_row.detail,
            mqtt_row.sound_name,
        )
        assert first_row.id < mqtt_row.id < ui_row.id

    assert len(fake_backend.played) == 3


async def test_tts_disabled_is_disabled_not_unavailable(
    tmp_path: Path, fake_broker: FakeBroker, fake_backend: RecordingBackend
) -> None:
    """``tts.default_engine = "none"`` fails as ``tts_disabled`` and health says so.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: in-memory broker.
    :param fake_backend: recording speaker, which must stay silent.
    """
    button = await _seed_tts_button(tmp_path, settings={"tts.default_engine": "none"})
    app = create_app(_config(tmp_path))
    state = _state(app)

    async with _running(app):
        assert state.health.get("tts") is HealthStatus.DISABLED
        assert state.health.snapshot()["tts"] == "disabled"

        await _connect(state, fake_broker)
        hub_client = await _open_hub(state)
        _press(fake_broker, button)

        message = await _next_press(state, hub_client)
        assert message["outcome"] == "failed"
        row = await _wait_for_row(state, outcome="failed", button_id=button.button_id)
        assert row.detail is not None
        assert row.detail.startswith("tts_disabled")
        assert "tts_unavailable" not in row.detail

    assert fake_backend.played == []


async def test_a_sound_deleted_before_playback_fails_as_sound_missing(
    tmp_path: Path, fake_broker: FakeBroker, fake_backend: RecordingBackend
) -> None:
    """The press is accepted, then the worker refuses to play a vanished file.

    The press path resolves the sound ROW and does not stat the file, so the
    press is ``queued``; deleting the file before the press exercises exactly the
    worker's pre-play check.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: in-memory broker.
    :param fake_backend: recording speaker, which must stay silent.
    """
    (button,) = await _seed_sound_buttons(tmp_path)
    cast(Path, button.sound_path).unlink()
    app = create_app(_config(tmp_path))
    state = _state(app)

    async with _running(app):
        await _connect(state, fake_broker)
        hub_client = await _open_hub(state)
        _press(fake_broker, button)

        # The press path accepted the job (``queued``) before the worker's
        # pre-play stat saw the file was gone.
        queued = await _next_press(state, hub_client)
        assert queued["outcome"] == "queued"

        failed = await _next_press(state, hub_client)
        assert failed["outcome"] == "failed"
        assert failed["pressed_at"] == queued["pressed_at"]

        failed_row = await _wait_for_row(state, outcome="failed", button_id=button.button_id)
        assert failed_row.detail is not None
        assert failed_row.detail.startswith("sound_missing")
        assert f"sound {button.sound_id}" in failed_row.detail

    assert fake_backend.played == []


async def test_a_played_press_records_the_wake_lead_in_it_paid(
    tmp_path: Path, fake_broker: FakeBroker, fake_backend: RecordingBackend
) -> None:
    """The first chime of a run records the silence it prepended in ``press_log``.

    The press path wrote no detail for a plain sound button, so the note stands
    alone; the acceptance criterion is that the row a real press produces says
    what the chime really did.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: in-memory broker.
    :param fake_backend: recording speaker.
    """
    (button,) = await _seed_sound_buttons(tmp_path)
    app = create_app(_config(tmp_path))
    state = _state(app)

    async with _running(app):
        await _connect(state, fake_broker)
        hub_client = await _open_hub(state)
        _press(fake_broker, button)

        queued = await _next_press(state, hub_client)
        assert queued["outcome"] == "queued"
        played = await _next_press(state, hub_client)
        assert played["outcome"] == "played"

        played_row = await _wait_for_row(state, outcome="played", button_id=button.button_id)

    assert played_row.detail == "wake lead-in 1.5 s"
