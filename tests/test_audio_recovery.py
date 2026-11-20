"""The audio start recovery ladder: bounded retries, honest health, honest presses.

Every test here runs the real lifespan through :func:`create_app` and the real
:class:`~chime_deck.wiring.ChimePipeline`, with the offline devices from
:mod:`chime_fakes` plus a *pinned* recovery clock and sleep: the ladder in
``chime_deck.wiring`` is bounded both by an attempt cap and by a wall-clock
budget, so a test that let either run on the real clock would either sleep for a
real second or be slow to fail. The ladder's task handle
(``state.chime._audio_recovery``) is the seam: awaiting it is how a test knows
the ladder has finished, and a :class:`~mqtt_fakes.GatedSleep` holds a pending
retry open so the mid-recovery state can be observed without a race.

Owning ticket: HSD-zghm.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from pathlib import Path
from typing import cast

import httpx
import pytest
from chime_fakes import RecordingBackend, build_offline_wiring
from fastapi import FastAPI
from log_isolation import preserve_log_state
from mqtt_fakes import FakeBroker, GatedSleep, RecordingSleep, wait_until

from chime_deck.app import AppState, create_app
from chime_deck.audio import AudioBackendUnavailable
from chime_deck.config import ServerConfig
from chime_deck.health import HealthStatus
from chime_deck.store import ButtonIn, ConfigStore, SoundIn
from chime_deck.wiring import Wiring

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request; nothing here may hang the suite."""

_PATCH_TIMEOUT_S = 5.0
"""Bound for one settings write, including the re-arm it triggers."""

_AWAIT_TIMEOUT_S = 5.0
"""Bound for the recovery task a test awaits."""


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


class _JumpingClock:
    """A monotonic clock that runs 20 s fast on every reading.

    The ladder reads it once when it starts, twice around its first backoff and
    again before any second attempt, so the fourth reading has already put the
    run past the 60 s retry budget. The wall-clock budget, not the attempt cap,
    is what ends this run — and no real time passes.
    """

    _STEP_S = 20.0

    def __init__(self) -> None:
        """Start the clock at zero; it has not been read yet."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the next reading, 20 s after the previous one."""
        self.now += self._STEP_S
        return self.now


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test.

    The application lifespan applies the stored ``system.log_level`` to the root
    logger, so the level has to be put back or later tests inherit it.
    """
    with preserve_log_state():
        yield


def _config(data_dir: Path) -> ServerConfig:
    """Return the server configuration every test in this module uses.

    :param data_dir: directory that holds the database and the sounds.
    """
    return ServerConfig(
        data_dir=data_dir, static_dir=data_dir / "no-such-dist", seed_demo_sounds=False
    )


async def _seed(directory: Path, *, settings: Mapping[str, object] | None = None) -> int:
    """Write settings and one playable sound button before the app is built.

    The sound file is created (the worker only stats it; the fake backend never
    decodes it), and the rows are written before the lifespan opens the same
    database.

    :param directory: data directory the database and the sound live in.
    :param settings: settings written before anything starts, keyed by name.
    :return: the id of the seeded button.
    """
    directory.mkdir(parents=True, exist_ok=True)
    sound_path = directory / "ding.mp3"
    sound_path.write_bytes(b"an offline test sound")
    async with ConfigStore(directory / "doorbell.db") as store:
        for key, value in (settings or {}).items():
            await store.set_setting(key, value)
        sound = await store.create_sound(
            SoundIn(name="Ding dong", source_type="file", path=str(sound_path))
        )
        button = await store.upsert_button(
            ButtonIn(name="Front Door", action_type="sound", sound_id=sound.id)
        )
    return button.id


def _write_raw_setting(database: Path, key: str, value: object) -> None:
    """Write one ``settings`` row with raw SQLite, bypassing the key registry.

    This is how a hand-edited or older database looks: the value is stored even
    though the registry would refuse it on a normal write.

    :param database: SQLite file to write.
    :param key: settings key to store.
    :param value: decoded value, stored JSON-encoded like the store does.
    """
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value), "2026-11-13T00:00:00.000000Z"),
        )


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
    return cast("AppState", app.state.doorbell)


def _wiring(
    broker: FakeBroker,
    backend: RecordingBackend,
    *,
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], float],
) -> Wiring:
    """Return the fully offline wiring with the recovery ladder's timing pinned.

    :param broker: in-memory broker that supplies the MQTT client factory.
    :param backend: speaker the playback worker starts.
    :param sleep: delay between start attempts; never the real one.
    :param clock: monotonic clock that bounds the ladder.
    """
    return replace(
        build_offline_wiring(broker=broker, backend=backend),
        audio_sleep=sleep,
        audio_clock=clock,
    )


async def _finish_recovery(state: AppState) -> None:
    """Await the armed recovery ladder, bounded, so the test sees its outcome.

    :param state: running application state holding the ladder.
    """
    task = state.chime._audio_recovery
    assert task is not None, "the recovery ladder was never armed"
    await asyncio.wait_for(task, timeout=_AWAIT_TIMEOUT_S)


async def _press(client: httpx.AsyncClient, button_id: int) -> dict[str, object]:
    """Press one button through the API and return the result body.

    :param client: client bound to the running application.
    :param button_id: id of the button to press.
    """
    response = await client.post(f"/api/buttons/{button_id}/press")
    assert response.status_code == 200
    return cast("dict[str, object]", response.json())


async def test_a_first_attempt_success_arms_no_recovery_task(
    tmp_path: Path,
    fake_broker: FakeBroker,
    fake_backend: RecordingBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A speaker that starts on the first attempt is ready, with no ladder behind it.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the offline wiring hands to the app.
    :param fake_backend: speaker the offline wiring hands to the app.
    :param caplog: captured log records.
    """
    sleep = RecordingSleep()
    app = create_app(
        _config(tmp_path),
        wiring=_wiring(fake_broker, fake_backend, sleep=sleep, clock=_FakeClock()),
    )
    state = _state(app)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with _running(app):
        assert state.health.get("audio") is HealthStatus.READY
        assert fake_backend.started == 1
        assert fake_backend.start_timeouts == [25.0]
        assert sleep.delays == []
        assert state.chime._audio_recovery is None

    assert [record.getMessage() for record in caplog.records].count("audio playback is ready") == 1


async def test_a_retry_that_succeeds_recovers_into_ready_and_restores_sound(
    tmp_path: Path,
    fake_broker: FakeBroker,
    fake_backend: RecordingBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One failed start is retried once, and the recovered speaker really plays.

    ``started`` counts real transitions, so the two attempts are counted on
    ``start_timeouts``: the boot attempt is the first, and recovery names both.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the offline wiring hands to the app.
    :param fake_backend: speaker scripted to fail once and then start.
    :param caplog: captured log records.
    """
    button_id = await _seed(tmp_path)
    fake_backend.start_failures = [AudioBackendUnavailable("libmpv is not installed")]
    sleep = RecordingSleep()
    app = create_app(
        _config(tmp_path),
        wiring=_wiring(fake_broker, fake_backend, sleep=sleep, clock=_FakeClock()),
    )
    state = _state(app)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with _running(app) as client:
        await _finish_recovery(state)

        assert len(fake_backend.start_timeouts) == 2
        assert fake_backend.started == 1
        assert sleep.delays == [1.0]
        assert state.health.get("audio") is HealthStatus.READY

        body = await _press(client, button_id)
        assert body["outcome"] == "queued"
        await wait_until(lambda: len(fake_backend.played) == 1)

    assert "audio recovered after 2 attempts" in caplog.text


async def test_retries_exhausted_give_up_loudly_and_presses_still_fail_specifically(
    tmp_path: Path,
    fake_broker: FakeBroker,
    fake_backend: RecordingBackend,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permanently broken speaker gets five attempts, then one WARNING and honest presses.

    The registry's ``detail`` is global and sticky and the MQTT bridge records a
    later one, so the audio detail is captured as it is written.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the offline wiring hands to the app.
    :param fake_backend: speaker told to fail every start.
    :param caplog: captured log records.
    :param monkeypatch: patcher used to observe the health writes.
    """
    button_id = await _seed(tmp_path)
    fake_backend.start_error = AudioBackendUnavailable("libmpv is not installed")
    sleep = RecordingSleep()
    app = create_app(
        _config(tmp_path),
        wiring=_wiring(fake_broker, fake_backend, sleep=sleep, clock=_FakeClock()),
    )
    state = _state(app)
    recorded: list[tuple[str, HealthStatus, str | None]] = []
    original = state.health.set

    def capture(component: str, status: HealthStatus, detail: str | None = None) -> None:
        """Record one health write before forwarding it.

        :param component: component name.
        :param status: status being recorded.
        :param detail: actionable detail being recorded.
        """
        recorded.append((component, status, detail))
        original(component, status, detail)

    monkeypatch.setattr(state.health, "set", capture)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with _running(app) as client:
        await _finish_recovery(state)

        assert len(fake_backend.start_timeouts) == 5
        assert fake_backend.started == 0
        assert sleep.delays == [1.0, 2.0, 4.0, 8.0]
        assert state.health.get("audio") is HealthStatus.ERROR

        body = await _press(client, button_id)
        assert body["outcome"] == "failed"
        assert body["code"] == "audio_error"
        detail = cast(str, body["detail"])
        assert "the playback worker is not running" in detail

    details = [
        detail
        for component, status, detail in recorded
        if component == "audio" and status is HealthStatus.ERROR
    ]
    assert details
    assert "gave up" in cast(str, details[-1])
    assert "after 5 attempts" in cast(str, details[-1])
    assert "giving up on the speaker" in caplog.text


async def test_the_wall_clock_budget_also_bounds_the_ladder(
    tmp_path: Path,
    fake_broker: FakeBroker,
    fake_backend: RecordingBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A clock that outruns the retry budget stops the ladder before the attempt cap.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the offline wiring hands to the app.
    :param fake_backend: speaker told to fail every start.
    :param caplog: captured log records.
    """
    fake_backend.start_error = AudioBackendUnavailable("libmpv is not installed")
    sleep = RecordingSleep()
    app = create_app(
        _config(tmp_path),
        wiring=_wiring(fake_broker, fake_backend, sleep=sleep, clock=_JumpingClock()),
    )
    state = _state(app)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with _running(app):
        await _finish_recovery(state)

        # The boot attempt plus one bounded retry: fewer than the five the
        # attempt cap would allow, because the budget ran out first.
        assert len(fake_backend.start_timeouts) == 2
        assert fake_backend.started == 0
        assert state.health.get("audio") is HealthStatus.ERROR

    assert "giving up on the speaker" in caplog.text


async def test_a_settings_change_while_audio_is_down_retries_at_once(
    tmp_path: Path,
    fake_broker: FakeBroker,
    fake_backend: RecordingBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ``audio.*`` write re-arms a stopped ladder with no backoff and the new budget.

    The boot ladder is parked inside its first backoff step when the write
    lands, so "the retry started before that step finished" is observable: the
    gate is still shut when the speaker is ready.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the offline wiring hands to the app.
    :param fake_backend: speaker scripted to fail the boot attempt only.
    :param caplog: captured log records.
    """
    await _seed(tmp_path)
    fake_backend.start_failures = [AudioBackendUnavailable("libmpv is not installed")]
    sleep = GatedSleep()
    app = create_app(
        _config(tmp_path),
        wiring=_wiring(fake_broker, fake_backend, sleep=sleep, clock=_FakeClock()),
    )
    state = _state(app)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with _running(app) as client:
        await wait_until(lambda: sleep.entered.is_set())
        assert sleep.delays == [1.0]
        assert state.health.get("audio") is HealthStatus.ERROR

        response = await asyncio.wait_for(
            client.patch("/api/settings", json={"audio.start_timeout_s": 40}),
            timeout=_PATCH_TIMEOUT_S,
        )
        assert response.status_code == 200

        await _finish_recovery(state)

        assert state.health.get("audio") is HealthStatus.READY
        assert fake_backend.start_timeouts == [25.0, 40.0]
        # The re-armed ladder slept nothing, and the retirement of the parked
        # one did not have to wait out its backoff step either.
        assert sleep.delays == [1.0]
        assert not sleep.release.is_set()

        # Let the retired ladder wake: it sees the newer generation before it
        # attempts anything and exits, so no second start is recorded.
        sleep.release.set()
        await asyncio.sleep(0)
        assert fake_backend.start_timeouts == [25.0, 40.0]

    assert "audio recovered after 1 attempt" in caplog.text


async def test_a_settings_change_while_audio_is_healthy_leaves_the_speaker_alone(
    tmp_path: Path,
    fake_broker: FakeBroker,
    fake_backend: RecordingBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A running speaker is never restarted by a settings write.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the offline wiring hands to the app.
    :param fake_backend: speaker the offline wiring hands to the app.
    :param caplog: captured log records.
    """
    sleep = RecordingSleep()
    app = create_app(
        _config(tmp_path),
        wiring=_wiring(fake_broker, fake_backend, sleep=sleep, clock=_FakeClock()),
    )
    state = _state(app)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with _running(app) as client:
        assert fake_backend.started == 1

        response = await asyncio.wait_for(
            client.patch("/api/settings", json={"audio.volume": 33}),
            timeout=_PATCH_TIMEOUT_S,
        )
        assert response.status_code == 200

        # Nothing was armed by the write: give a task one chance to appear.
        await asyncio.sleep(0)

        assert fake_backend.started == 1
        assert state.chime._audio_recovery is None
        assert state.health.get("audio") is HealthStatus.READY

    assert "keeps its current output until the service restarts" in caplog.text


async def test_the_stored_start_timeout_reaches_the_backend(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every start reads ``audio.start_timeout_s``, clamped and never trusted blindly.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """

    async def first_start_budget(
        directory: Path,
        *,
        stored: object | None = None,
        raw: bool = False,
    ) -> RecordingBackend:
        """Run one app to startup and return the speaker it tried to start.

        :param directory: data directory for this case.
        :param stored: value to store, or ``None`` to leave the default.
        :param raw: write *stored* with raw SQLite, bypassing the registry.
        """
        if stored is not None and not raw:
            await _seed(directory, settings={"audio.start_timeout_s": stored})
        else:
            await _seed(directory)
            if stored is not None:
                _write_raw_setting(directory / "doorbell.db", "audio.start_timeout_s", stored)
        backend = RecordingBackend()
        app = create_app(
            _config(directory),
            wiring=_wiring(FakeBroker(), backend, sleep=RecordingSleep(), clock=_FakeClock()),
        )
        async with _running(app):
            pass
        return backend

    caplog.set_level(logging.INFO, logger="chime_deck")

    default = await first_start_budget(tmp_path / "default")
    assert default.start_timeouts == [25.0]
    assert "audio.start_timeout_s" not in caplog.text

    stored = await first_start_budget(tmp_path / "stored", stored=40)
    assert stored.start_timeouts == [40.0]

    caplog.clear()
    clamped = await first_start_budget(tmp_path / "clamped", stored=5, raw=True)
    assert clamped.start_timeouts == [10.0]
    assert "outside the 10-120 s range" in caplog.text

    caplog.clear()
    nonsense = await first_start_budget(tmp_path / "nonsense", stored="nonsense", raw=True)
    assert nonsense.start_timeouts == [25.0]
    assert "audio.start_timeout_s is not an integer" in caplog.text


async def test_presses_and_health_agree_during_and_after_recovery(
    tmp_path: Path,
    fake_broker: FakeBroker,
    fake_backend: RecordingBackend,
) -> None:
    """A press never claims success while the ladder is still trying.

    Dedupe is switched off for this button: the failed press arms the window,
    and a deduped second press would say nothing about recovery.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the offline wiring hands to the app.
    :param fake_backend: speaker scripted to fail the boot attempt only.
    """
    button_id = await _seed(tmp_path, settings={"buttons.default_dedupe_seconds": 0})
    fake_backend.start_failures = [AudioBackendUnavailable("libmpv is not installed")]
    sleep = GatedSleep()
    app = create_app(
        _config(tmp_path),
        wiring=_wiring(fake_broker, fake_backend, sleep=sleep, clock=_FakeClock()),
    )
    state = _state(app)

    async with _running(app) as client:
        await wait_until(lambda: sleep.entered.is_set())
        assert state.health.get("audio") is HealthStatus.ERROR

        during = await _press(client, button_id)
        assert during["outcome"] == "failed"
        assert during["code"] == "audio_error"
        assert during["detail"]

        sleep.release.set()
        await _finish_recovery(state)

        assert state.health.get("audio") is HealthStatus.READY
        after = await _press(client, button_id)
        assert after["outcome"] == "queued"
        await wait_until(lambda: len(fake_backend.played) == 1)


async def test_a_finished_ladder_does_not_outlive_shutdown(
    tmp_path: Path,
    fake_broker: FakeBroker,
    fake_backend: RecordingBackend,
) -> None:
    """Exiting the lifespan retires the ladder, so no start attempt follows shutdown.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the offline wiring hands to the app.
    :param fake_backend: speaker told to fail every start.
    """
    fake_backend.start_error = AudioBackendUnavailable("libmpv is not installed")
    sleep = RecordingSleep()
    app = create_app(
        _config(tmp_path),
        wiring=_wiring(fake_broker, fake_backend, sleep=sleep, clock=_FakeClock()),
    )
    state = _state(app)

    async with _running(app):
        await _finish_recovery(state)
        task = state.chime._audio_recovery
        assert task is not None
        assert task.done()
        attempts = len(fake_backend.start_timeouts)
        assert attempts == 5

    assert task.done()
    assert task.exception() is None
    assert state.chime._audio_recovery is None

    await asyncio.sleep(0)

    assert len(fake_backend.start_timeouts) == attempts
    assert fake_backend.started == 0
