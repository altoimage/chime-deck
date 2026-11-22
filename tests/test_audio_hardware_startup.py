"""Startup, settings changes and the test tone all apply the device's own level.

These tests run the real lifespan through :func:`create_app` with the offline
devices from :mod:`chime_fakes` plus a recording ``amixer``/``pactl`` spawner, so
the measured bug of cd-qul9 is asserted end to end: a USB DAC whose own control
sits at 43% is brought to the stored 100 at boot, again when the setting changes,
and never turns a cosmetic mixer failure into an ``error`` audio health.

Owning ticket: cd-qul9.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from audio_hardware_fakes import FakeCommand, FakeControl, FakeMixer, RecordingSpawner
from chime_fakes import RecordingBackend, build_offline_wiring
from fastapi import FastAPI
from log_isolation import preserve_log_state
from mqtt_fakes import FakeBroker, wait_until

from chime_deck.api import LiveProbes
from chime_deck.app import AppState, create_app
from chime_deck.audio import AudioDevice, HardwareVolumeStatus
from chime_deck.config import ServerConfig
from chime_deck.health import HealthStatus
from chime_deck.store import ConfigStore

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request; nothing here may hang the suite."""

_PATCH_TIMEOUT_S = 5.0
"""Bound for one settings write, including the re-apply it schedules."""

_SETTLE_TIMEOUT_S = 5.0
"""Bound for the background re-apply a settings change schedules."""

_USB_DEVICE = AudioDevice(name="alsa/hw:CARD=Device,DEV=0", hostapi="alsa", is_default=True)

_APPLY_ARGV = ("amixer", "-c", "Device", "sset", "Speaker", "100%", "unmute")
"""The exact command startup runs to replace a DAC's own 43% with the stored 100."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger after every test.

    The application lifespan applies the stored ``system.log_level`` to the root
    logger, so the level has to be put back.
    """
    with preserve_log_state():
        yield


def _config(data_dir: Path) -> ServerConfig:
    """Return the server configuration these tests use.

    :param data_dir: directory that holds the database and the sounds.
    """
    return ServerConfig(
        data_dir=data_dir, static_dir=data_dir / "no-such-dist", seed_demo_sounds=False
    )


async def _seed(data_dir: Path, settings: Mapping[str, object] | None = None) -> None:
    """Write settings before the app is built.

    :param data_dir: directory that holds the database.
    :param settings: settings written before anything starts, keyed by name.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    async with ConfigStore(data_dir / "doorbell.db") as store:
        for key, value in (settings or {}).items():
            await store.set_setting(key, value)


def _backend() -> RecordingBackend:
    """Return a recording speaker whose one output is a USB DAC."""
    backend = RecordingBackend()
    backend._devices = [_USB_DEVICE]
    return backend


def _app(
    tmp_path: Path,
    backend: RecordingBackend,
    spawner: RecordingSpawner,
) -> tuple[FastAPI, AppState]:
    """Build the real application with a fake speaker and fake mixer commands.

    :param tmp_path: pytest temporary directory.
    :param backend: recording speaker the pipeline starts.
    :param spawner: fake ``amixer``/``pactl`` commands.
    """
    wiring = build_offline_wiring(broker=FakeBroker(), backend=backend, hardware_spawner=spawner)
    app = create_app(_config(tmp_path), wiring=wiring)
    return app, cast(AppState, app.state.doorbell)


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the real lifespan and yield a client bound to the application.

    :param app: application under test.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_startup_replaces_the_devices_own_level_with_the_stored_one(
    tmp_path: Path,
) -> None:
    """The bug itself: booting must move a 43% DAC control to the stored 100%.

    :param tmp_path: pytest temporary directory.
    """
    mixer = FakeMixer({"Speaker": FakeControl(percent=43)})
    spawner = RecordingSpawner(mixer)
    backend = _backend()
    app, state = _app(tmp_path, backend, spawner)

    async with _running(app):
        status = await state.chime.read_hardware()

    assert spawner.argv_matching("amixer", "sset") == [_APPLY_ARGV]
    assert mixer.writes == [("Speaker", 100)]
    assert status.available is True
    assert status.percent == 100
    assert status.applied == 100
    assert state.health.get("audio") is HealthStatus.READY


async def test_startup_writes_nothing_when_the_device_already_matches(
    tmp_path: Path,
) -> None:
    """An already correct output is left alone, so a boot causes no click.

    :param tmp_path: pytest temporary directory.
    """
    mixer = FakeMixer({"Speaker": FakeControl(percent=100)})
    spawner = RecordingSpawner(mixer)
    app, state = _app(tmp_path, _backend(), spawner)

    async with _running(app):
        status = await state.chime.read_hardware()

    assert spawner.argv_matching("amixer", "sset") == []
    assert status.percent == 100
    assert status.applied is None


async def test_a_settings_change_reapplies_the_stored_level(tmp_path: Path) -> None:
    """A ``PATCH`` of ``audio.hardware_volume`` reaches a running speaker's device.

    :param tmp_path: pytest temporary directory.
    """
    mixer = FakeMixer({"Speaker": FakeControl(percent=100)})
    spawner = RecordingSpawner(mixer)
    app, state = _app(tmp_path, _backend(), spawner)

    async with _running(app) as client:
        # Pretend the OS moved the control behind our back, which is the case a
        # settings change has to fix without a restart.
        mixer.controls["Speaker"].percent = 43
        response = await asyncio.wait_for(
            client.patch("/api/settings", json={"audio.hardware_volume": 60}),
            timeout=_PATCH_TIMEOUT_S,
        )
        assert response.status_code == 200
        await wait_until(lambda: ("Speaker", 60) in mixer.writes, timeout=_SETTLE_TIMEOUT_S)

    assert mixer.controls["Speaker"].percent == 60
    assert state.health.get("audio") is HealthStatus.READY


async def test_a_mixer_failure_never_flips_audio_health_to_error(tmp_path: Path) -> None:
    """A host without ``amixer`` still plays chimes; only the device level is unavailable.

    :param tmp_path: pytest temporary directory.
    """

    def _missing(_argv: tuple[str, ...]) -> FakeCommand:
        raise FileNotFoundError("amixer")

    spawner = RecordingSpawner(_missing)
    app, state = _app(tmp_path, _backend(), spawner)

    async with _running(app) as client:
        assert state.health.get("audio") is HealthStatus.READY
        status = await state.chime.read_hardware()
        assert status.available is False
        assert status.reason == "the amixer utility is not installed"
        tone = cast(
            "dict[str, object]",
            (await client.post("/api/settings/test/audio")).json(),
        )

    assert tone["ok"] is True
    assert state.health.get("audio") is HealthStatus.READY


async def test_the_public_hardware_methods_report_a_status(tmp_path: Path) -> None:
    """``read_hardware`` and ``reapply_hardware`` return statuses and never raise.

    :param tmp_path: pytest temporary directory.
    """
    mixer = FakeMixer({"Speaker": FakeControl(percent=43)})
    app, state = _app(tmp_path, _backend(), RecordingSpawner(mixer))

    async with _running(app):
        read = await state.chime.read_hardware()
        applied = await state.chime.reapply_hardware()

    assert isinstance(read, HardwareVolumeStatus)
    assert isinstance(applied, HardwareVolumeStatus)
    assert read.available is True
    assert applied.available is True


async def test_the_test_tone_applies_the_level_first_and_cannot_be_failed_by_it(
    tmp_path: Path,
) -> None:
    """The device-change hook runs after ``set_device``, before the tone, and is guarded.

    :param tmp_path: pytest temporary directory.
    """
    backend = _backend()
    seen: list[tuple[list[str], int]] = []

    async def _hook() -> None:
        # Runs after the device was selected and before a file was played, so a
        # real instance would reach the new output rather than the old one.
        seen.append((list(backend.hints), len(backend.played)))
        raise RuntimeError("the mixer hiccuped")

    async with ConfigStore(tmp_path / "doorbell.db") as store:
        probes = LiveProbes(
            store,
            audio_backend=lambda: backend,
            device_changed=_hook,
        )
        result = await probes.test_audio()

    assert result.ok is True
    assert seen == [([""], 0)]
    assert len(backend.played) == 1
