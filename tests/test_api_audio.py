"""Audio HTTP API: the device-discovery route, fully offline.

Every test builds the real application through :func:`create_app`, runs the real
lifespan and speaks to it through ``httpx.ASGITransport``, so no socket is opened
and no sound card is touched. The speaker is a fake
:class:`~chime_deck.audio.AudioBackend` whose device list, and whose failure,
a test chooses.

The route's contract is the JSON body, not the status code alone: HTTP 200
carries the verdict in ``ok``, exactly like ``POST /api/settings/test/*`` and
``POST /api/mqtt/republish``.

Owning ticket: HSD-wpm0.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from audio_hardware_fakes import FakeControl, FakeMixer, FakePulse, RecordingSpawner
from chime_fakes import build_offline_wiring
from fastapi import FastAPI
from log_isolation import preserve_log_state
from mqtt_fakes import FakeBroker

from chime_deck.api import PROBE_TIMEOUT_S, ErrorEnvelope
from chime_deck.api.audio import AUDIO_NOT_STARTED_TEXT
from chime_deck.app import AppState, create_app
from chime_deck.audio import (
    NO_HARDWARE_CONTROL_TEXT,
    AudioBackend,
    AudioBackendNotStarted,
    AudioBackendUnavailable,
    AudioDevice,
)
from chime_deck.config import SECRET_SCRUBBER, ServerConfig
from chime_deck.store import ConfigStore

pytestmark = pytest.mark.asyncio

_REQUEST_TIMEOUT_S = 5.0
"""Bound for one in-process request; nothing here may hang the suite."""

_RESPONSE_KEYS = {"ok", "hint", "resolved_device", "system_default", "devices", "error"}
_HARDWARE_KEYS = {
    "ok",
    "available",
    "device",
    "control",
    "percent",
    "muted",
    "reason",
    "configured",
    "applied",
}
_DEVICE_KEYS = {"name", "hostapi", "is_default"}

_SOUNDDEVICE_TEXT = (
    "audio.backend is set to 'sounddevice', which this build does not implement — set "
    "audio.backend to 'mpv' to get sound"
)
"""The sentence an unavailable backend carries, as ``wiring.resolve_backend`` builds it."""

_TOKEN = "long-lived-token-0123456789abcdef"
"""A secret-shaped value, used to prove the response is scrubbed."""


class _FakeBackend:
    """An :class:`~chime_deck.audio.AudioBackend` whose device list a test chooses.

    Only :meth:`list_devices` matters to this route; everything that would make a
    sound raises, so a route that starts playing by accident fails loudly instead
    of quietly touching a speaker.
    """

    def __init__(
        self,
        devices: Sequence[AudioDevice] | None = None,
        *,
        gate: asyncio.Event | None = None,
    ) -> None:
        """Create a backend reporting *devices*.

        :param devices: outputs to report; ``None`` means one ``pipewire`` output.
        :param gate: when set, :meth:`list_devices` waits on it, which is how a
            discovery that never answers is simulated.
        """
        self.devices: list[AudioDevice] = (
            [AudioDevice(name="pipewire/offline-output", hostapi="pipewire", is_default=True)]
            if devices is None
            else list(devices)
        )
        self.list_error: Exception | None = None
        self.gate = gate
        self.start_timeouts: list[float | None] = []

    @property
    def playing(self) -> bool:
        """Return ``False``: this fake never plays anything."""
        return False

    async def start(self, *, timeout_s: float | None = None) -> None:
        """Record the budget; the fake opens no device.

        :param timeout_s: budget the caller offered for this start, in seconds.
        """
        self.start_timeouts.append(timeout_s)

    async def stop(self) -> None:
        """Do nothing: the fake opened no device."""

    async def play(
        self,
        path: Path,
        *,
        volume: int,
        lead_in_ms: int = 0,
        when_done: Callable[[], None] | None = None,
    ) -> int:
        """Refuse to play: device discovery must never make a sound.

        :param path: file that would have been played.
        :param volume: volume that would have been used.
        :param lead_in_ms: lead-in that would have been asked for.
        :param when_done: completion callback that is never run.
        :raises AudioBackendNotStarted: always.
        """
        raise AudioBackendNotStarted("the fake backend never plays")

    async def stop_playback(self) -> None:
        """Do nothing: nothing is playing."""

    async def set_device(self, hint: str) -> None:
        """Refuse to select a device: this route only lists them.

        :param hint: device hint that would have been selected.
        :raises AudioBackendNotStarted: always.
        """
        raise AudioBackendNotStarted("the fake backend never selects a device")

    async def list_devices(self) -> list[AudioDevice]:
        """Return the configured outputs, hang on the gate, or raise.

        :raises Exception: the configured ``list_error``, when a test set one.
        """
        if self.list_error is not None:
            raise self.list_error
        if self.gate is not None:
            await self.gate.wait()
        return list(self.devices)


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Keep the root logger and the registered secrets isolated per test.

    The lifespan applies the stored ``system.log_level`` and registers stored
    secrets for log scrubbing, so both have to be put back afterwards.
    """
    with preserve_log_state():
        yield


def _build_app(
    tmp_path: Path,
    backend: AudioBackend | None = None,
    *,
    hardware_spawner: RecordingSpawner | None = None,
    probe_timeout: float | None = None,
) -> tuple[FastAPI, AppState]:
    """Build an app whose speaker is *backend*, with no built UI.

    :param tmp_path: pytest temporary directory, used as the data directory.
    :param backend: speaker the pipeline starts; ``None`` lets the conftest's
        offline wiring supply its recording backend.
    :param hardware_spawner: fake mixer commands; ``None`` builds a fresh
        recording spawner, which reports no hardware control and writes nothing.
    :param probe_timeout: bound for one discovery; ``None`` keeps the default.
    """
    config = ServerConfig(
        data_dir=tmp_path, static_dir=tmp_path / "no-dist", seed_demo_sounds=False
    )
    wiring = (
        None
        if backend is None and hardware_spawner is None
        else build_offline_wiring(
            broker=FakeBroker(),
            backend=backend if backend is not None else _FakeBackend(),
            hardware_spawner=RecordingSpawner() if hardware_spawner is None else hardware_spawner,
        )
    )
    timeout = PROBE_TIMEOUT_S if probe_timeout is None else probe_timeout
    app = create_app(config, wiring=wiring, probe_timeout=timeout)
    return app, cast(AppState, app.state.doorbell)


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield a client bound to it.

    :param app: application under test.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def _get(client: httpx.AsyncClient, path: str = "/api/audio/devices") -> httpx.Response:
    """GET *path*, bounded so a bug cannot hang the suite.

    :param client: client bound to the running app.
    :param path: request path under test.
    """
    return await asyncio.wait_for(client.get(path), timeout=_REQUEST_TIMEOUT_S)


def _body(response: httpx.Response) -> dict[str, object]:
    """Return one response body as a plain mapping.

    :param response: response from the device-discovery route.
    """
    return cast("dict[str, object]", response.json())


def _devices(body: dict[str, object]) -> list[dict[str, object]]:
    """Return the ``devices`` list of a response body, asserting it is a list of objects.

    :param body: decoded response body.
    """
    devices = body["devices"]
    assert isinstance(devices, list)
    return cast("list[dict[str, object]]", devices)


async def test_the_device_list_carries_the_documented_keys(tmp_path: Path) -> None:
    """The response and each device use exactly the documented key sets.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, _FakeBackend())

    async with _running(app) as client:
        response = await _get(client)

    assert response.status_code == 200
    body = _body(response)
    assert set(body) == _RESPONSE_KEYS
    devices = _devices(body)
    assert len(devices) == 1
    assert set(devices[0]) == _DEVICE_KEYS


async def test_the_stored_hint_is_resolved_to_a_full_device_name(tmp_path: Path) -> None:
    """A lowercase substring hint resolves to the device's full name, echoed verbatim.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path, _FakeBackend())

    async with _running(app) as client:
        await state.store.set_setting("audio.device_hint", "offline-output")
        response = await _get(client)

    body = _body(response)
    assert body["ok"] is True
    assert body["hint"] == "offline-output"
    assert body["resolved_device"] == "pipewire/offline-output"
    assert body["system_default"] == "auto"
    assert body["error"] is None
    assert _devices(body)[0]["name"] == "pipewire/offline-output"


async def test_a_blank_hint_resolves_to_nothing(tmp_path: Path) -> None:
    """An empty hint names no device: mpv keeps its system default and the UI says so.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, _FakeBackend())

    async with _running(app) as client:
        response = await _get(client)

    body = _body(response)
    assert body["ok"] is True
    assert body["hint"] == ""
    assert body["resolved_device"] is None


async def test_an_unmatched_hint_is_still_a_success(tmp_path: Path) -> None:
    """A hint that matches no device falls back to the system default, not an error.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path, _FakeBackend())

    async with _running(app) as client:
        await state.store.set_setting("audio.device_hint", "usb-headset")
        response = await _get(client)

    body = _body(response)
    assert body["ok"] is True
    assert body["hint"] == "usb-headset"
    assert body["resolved_device"] is None
    assert body["error"] is None


async def test_an_empty_host_is_a_successful_empty_list(tmp_path: Path) -> None:
    """Discovery that ran and found nothing is ``ok: true`` with an empty list.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, _FakeBackend(devices=[]))

    async with _running(app) as client:
        response = await _get(client)

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is True
    assert body["devices"] == []
    assert body["resolved_device"] is None
    assert body["error"] is None


async def test_a_failed_discovery_is_reported_on_http_200(tmp_path: Path) -> None:
    """A backend this build does not implement answers 200 with its own sentence.

    :param tmp_path: pytest temporary directory.
    """
    backend = _FakeBackend()
    backend.list_error = AudioBackendUnavailable(_SOUNDDEVICE_TEXT)
    app, _state = _build_app(tmp_path, backend)

    async with _running(app) as client:
        response = await _get(client)

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is False
    assert body["error"] == _SOUNDDEVICE_TEXT
    assert body["devices"] == []


async def test_a_pipeline_that_never_started_answers_honestly(tmp_path: Path) -> None:
    """Without the lifespan there is no speaker, and the route says so on HTTP 200.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, _FakeBackend())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _get(client)

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is False
    assert body["error"] == AUDIO_NOT_STARTED_TEXT


async def test_a_secret_in_the_hint_is_scrubbed(tmp_path: Path) -> None:
    """The echoed hint goes through the secret scrubber, so nothing leaks.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path, _FakeBackend())

    async with _running(app) as client:
        SECRET_SCRUBBER.set_secrets([_TOKEN])
        await state.store.set_setting("audio.device_hint", f"offline-output-{_TOKEN}")
        response = await _get(client)

    body = _body(response)
    assert body["ok"] is True
    assert _TOKEN not in response.text
    assert "[REDACTED]" in cast(str, body["hint"])


async def test_an_unexpected_discovery_error_is_reported(tmp_path: Path) -> None:
    """A failure that is not an audio error is wrapped in prose, not surfaced as a 500.

    :param tmp_path: pytest temporary directory.
    """
    backend = _FakeBackend()
    backend.list_error = RuntimeError("the sound server hung up")
    app, _state = _build_app(tmp_path, backend)

    async with _running(app) as client:
        response = await _get(client)

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is False
    assert body["error"] == "audio device discovery failed: the sound server hung up"
    assert body["devices"] == []


async def test_a_slow_discovery_times_out_within_the_bound(tmp_path: Path) -> None:
    """A discovery that never answers is cancelled and reported with the bound.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, _FakeBackend(gate=asyncio.Event()), probe_timeout=0.05)

    async with _running(app) as client:
        response = await _get(client)

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is False
    error = body["error"]
    assert isinstance(error, str)
    assert "did not answer within" in error
    assert "0.05" in error


async def test_post_is_not_allowed(tmp_path: Path) -> None:
    """The route is GET-only: a POST answers the standard 405 envelope.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, _FakeBackend())

    async with _running(app) as client:
        response = await asyncio.wait_for(
            client.post("/api/audio/devices"), timeout=_REQUEST_TIMEOUT_S
        )

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert ErrorEnvelope.model_validate(response.json()).error.code == "method_not_allowed"


async def test_the_slashed_path_is_not_a_second_endpoint(tmp_path: Path) -> None:
    """``/api/audio/devices/`` falls through to the UI handler and its 404 envelope.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, _FakeBackend())

    async with _running(app) as client:
        response = await _get(client, "/api/audio/devices/")

    assert response.status_code == 404
    assert ErrorEnvelope.model_validate(response.json()).error.code == "not_found"


def _usb_backend() -> _FakeBackend:
    """Return a backend whose one output is a USB DAC, so ``amixer -c Device`` is used."""
    return _FakeBackend(
        [AudioDevice(name="alsa/hw:CARD=Device,DEV=0", hostapi="alsa", is_default=True)]
    )


async def test_the_hardware_route_carries_the_documented_keys(tmp_path: Path) -> None:
    """The response uses exactly the contract's key set, with the read-back values.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(
        tmp_path,
        _usb_backend(),
        hardware_spawner=RecordingSpawner(FakeMixer({"Speaker": FakeControl(percent=100)})),
    )

    async with _running(app) as client:
        response = await _get(client, "/api/audio/hardware")

    assert response.status_code == 200
    body = _body(response)
    assert set(body) == _HARDWARE_KEYS
    assert body["ok"] is True
    assert body["available"] is True
    assert body["device"] == "alsa/hw:CARD=Device,DEV=0"
    assert body["control"] == "Speaker"
    assert body["percent"] == 100
    assert body["muted"] is False
    assert body["reason"] == ""
    assert body["configured"] == 100
    assert body["applied"] is None


async def test_the_hardware_route_shows_the_level_startup_wrote(tmp_path: Path) -> None:
    """The measured bug, end to end: a DAC at 43% is at 100% and the route says so.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(
        tmp_path,
        _usb_backend(),
        hardware_spawner=RecordingSpawner(FakeMixer({"Speaker": FakeControl(percent=43)})),
    )

    async with _running(app) as client:
        response = await _get(client, "/api/audio/hardware")

    body = _body(response)
    assert body["percent"] == 100
    assert body["applied"] == 100
    assert body["configured"] == 100


async def test_an_output_with_no_hardware_control_is_an_honest_answer(tmp_path: Path) -> None:
    """The HDMI case is ``ok: true, available: false`` with the reason verbatim.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(
        tmp_path,
        _usb_backend(),
        hardware_spawner=RecordingSpawner(
            FakeMixer({"IEC958": FakeControl(percent=0, has_volume=False)})
        ),
    )

    async with _running(app) as client:
        response = await _get(client, "/api/audio/hardware")

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is True
    assert body["available"] is False
    assert body["percent"] is None
    assert body["reason"] == NO_HARDWARE_CONTROL_TEXT


async def test_the_hardware_route_never_writes_the_mixer(tmp_path: Path) -> None:
    """A GET is read-only: startup writes once, and the request adds nothing.

    :param tmp_path: pytest temporary directory.
    """
    mixer = FakeMixer({"Speaker": FakeControl(percent=43)})
    spawner = RecordingSpawner(mixer)
    app, _state = _build_app(tmp_path, _usb_backend(), hardware_spawner=spawner)

    async with _running(app) as client:
        writes_after_startup = list(mixer.writes)
        response = await _get(client, "/api/audio/hardware")

    assert response.status_code == 200
    assert writes_after_startup == [("Speaker", 100)]
    assert mixer.writes == writes_after_startup
    assert spawner.argv_matching("amixer", "sset") == [
        ("amixer", "-c", "Device", "sset", "Speaker", "100%", "unmute")
    ]


async def test_the_hardware_route_reports_a_pulse_sink(tmp_path: Path) -> None:
    """A Pulse output is read through ``pactl`` and reported with its sink name.

    :param tmp_path: pytest temporary directory.
    """
    backend = _FakeBackend([AudioDevice(name="pulse/my-sink", hostapi="pulse", is_default=True)])
    app, _state = _build_app(
        tmp_path, backend, hardware_spawner=RecordingSpawner(FakePulse(percent=40))
    )

    async with _running(app) as client:
        response = await _get(client, "/api/audio/hardware")

    body = _body(response)
    assert body["available"] is True
    assert body["control"] == "my-sink"
    assert body["percent"] == 100
    assert body["applied"] == 100


async def test_the_hardware_route_echoes_the_stored_level(tmp_path: Path) -> None:
    """``configured`` is the stored setting, so a mismatch is visible in one glance.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as seeded:
        await seeded.set_setting("audio.hardware_volume", 42)
    app, _state = _build_app(
        tmp_path,
        _usb_backend(),
        hardware_spawner=RecordingSpawner(FakeMixer({"Speaker": FakeControl(percent=42)})),
    )

    async with _running(app) as client:
        response = await _get(client, "/api/audio/hardware")

    body = _body(response)
    assert body["configured"] == 42
    assert body["percent"] == 42
    assert body["applied"] is None


async def test_a_hardware_pipeline_that_never_started_answers_honestly(tmp_path: Path) -> None:
    """Without the lifespan there is no backend, and the route answers on HTTP 200.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, _usb_backend())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _get(client, "/api/audio/hardware")

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is False
    assert body["available"] is False
    assert body["reason"] == AUDIO_NOT_STARTED_TEXT


async def test_a_secret_in_a_sink_name_is_scrubbed(tmp_path: Path) -> None:
    """Every string in the hardware response goes through the secret scrubber.

    :param tmp_path: pytest temporary directory.
    """
    backend = _FakeBackend(
        [AudioDevice(name=f"pulse/sink-{_TOKEN}", hostapi="pulse", is_default=True)]
    )
    app, _state = _build_app(
        tmp_path, backend, hardware_spawner=RecordingSpawner(FakePulse(percent=100))
    )

    async with _running(app) as client:
        SECRET_SCRUBBER.set_secrets([_TOKEN])
        response = await _get(client, "/api/audio/hardware")

    body = _body(response)
    assert body["available"] is True
    assert _TOKEN not in response.text
    assert "[REDACTED]" in cast(str, body["control"])


async def test_the_hardware_post_is_not_allowed(tmp_path: Path) -> None:
    """The route is GET-only, like the sibling discovery route.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, _usb_backend())

    async with _running(app) as client:
        response = await asyncio.wait_for(
            client.post("/api/audio/hardware"), timeout=_REQUEST_TIMEOUT_S
        )

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert ErrorEnvelope.model_validate(response.json()).error.code == "method_not_allowed"
