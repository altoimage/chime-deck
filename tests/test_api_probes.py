"""Settings API connection probes: shapes, defaults, failures and the timeout bound.

The probes are injected through ``create_app(probes=...)``, so every test here
exercises the route's own logic (verdict mapping, scrubbing, timeout) without a
network, a broker or a sound card. The audio probe is exercised with a fake
:class:`~chime_deck.audio.AudioBackend`, so "the speaker made a sound" is a
recorded call rather than real hardware. The MQTT probe is exercised with the
in-memory :class:`~mqtt_fakes.FakeBroker`, so "the broker answered" is a recorded
CONNACK rather than a socket.

Owning ticket: HSD-91z1; the real audio probe is HSD-wpm0 and the real MQTT probe
is HSD-8qf2.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state
from mqtt_fakes import FakeBroker, count
from test_tts_ha import BASE_URL, Scripted, _json, _replies
from test_tts_ha import TOKEN as HA_TOKEN

from chime_deck.api import (
    PROBE_TIMEOUT_S,
    AudioProbeResult,
    ConnectionProbes,
    HaProbeResult,
    LiveProbes,
    MqttProbeResult,
)
from chime_deck.api.audio import AUDIO_NOT_STARTED_TEXT
from chime_deck.api.probes import MQTT_PROBE_TIMEOUT_S
from chime_deck.app import AppState, create_app
from chime_deck.audio import (
    AudioBackend,
    AudioDevice,
    NoAudioDeviceError,
    PlaybackInProgressError,
)
from chime_deck.config import REDACTED, SECRET_SCRUBBER, ServerConfig, install_log_scrubber
from chime_deck.mqtt import DEFAULT_KEEPALIVE_S

pytestmark = pytest.mark.asyncio

TOKEN = "long-lived-token-0123456789abcdef"

_HA_KEYS = {"ok", "ha_version", "entity_count", "error"}
_MQTT_KEYS = {"ok", "broker_version", "error", "auth_failed"}
_AUDIO_KEYS = {"ok", "device", "latency_ms", "error"}

_FAKE_PLAY_SECONDS = 0.45
"""How long the fake backend stays in ``play()``, so real elapsed time is non-zero."""

_NOT_STARTED = (
    "the audio engine has not started yet, so no test tone can play — check the audio health "
    "status and the application log, then test again"
)
"""The probe's not-started sentence, recorded here verbatim as the contract."""

_MUTED = (
    "audio.volume is 0%, so a test tone would be silent — raise the master volume above 0% on "
    "the Audio settings page and test again"
)
"""The probe's muted sentence, recorded here verbatim as the contract."""

_MUTE_ON = (
    "the doorbell is muted (audio.muted is on), so a test tone would be silent — unmute the "
    "device and test again"
)
"""The probe's mute-on sentence, recorded here verbatim as the contract."""

_MQTT_HOST = "broker.local"
"""Broker host every MQTT probe test stores."""

_MQTT_PORT = 1885
"""Broker port every MQTT probe test stores; not 1883, so the sentence is pinned by value."""

_MQTT_PROBE_BUDGET = 0.05
"""Injected inner budget for the timeout test, so the real wait is milliseconds."""

_MQTT_UNCONFIGURED = (
    "no MQTT broker is configured yet — set mqtt.host and mqtt.port in Settings, then test again"
)
"""The probe's unconfigured sentence, recorded here verbatim as the contract."""

_MQTT_REFUSED = (
    f"the MQTT broker at {_MQTT_HOST}:{_MQTT_PORT} refused the connection — check that the "
    "broker is running and that mqtt.port is the port it listens on"
)
"""The probe's connection-refused sentence, recorded here verbatim as the contract."""

_MQTT_UNRESOLVED = (
    f"the MQTT broker address {_MQTT_HOST} could not be resolved — check mqtt.host in Settings"
)
"""The probe's name-resolution sentence, recorded here verbatim as the contract."""

_MQTT_BAD_CREDENTIALS = (
    f"the MQTT broker at {_MQTT_HOST}:{_MQTT_PORT} rejected the credentials (Not authorized) — "
    "check mqtt.username and mqtt.password in Settings"
)
"""The probe's rejected-credentials sentence, recorded here verbatim as the contract."""

_REQUIRED_CREDENTIALS = ("doorbell", "hunter2")
"""Credentials the fake broker demands when it refuses anonymous clients."""

_MQTT_ANONYMOUS_REFUSAL = "Bad user name or password"
"""The CONNACK name paho assigns 0x86, which a required-credentials fake broker fires."""

_MQTT_ANONYMOUS_REFUSAL_TEXT = (
    f"the MQTT broker at {_MQTT_HOST}:{_MQTT_PORT} rejected the credentials "
    f"({_MQTT_ANONYMOUS_REFUSAL}) — check mqtt.username and mqtt.password in Settings"
)
"""The rejected-credentials sentence a broker that refuses anonymous clients earns."""

_MQTT_REJECTED = (
    f"the MQTT broker at {_MQTT_HOST}:{_MQTT_PORT} rejected the connection "
    "(Client identifier not valid) — check mqtt.instance_id and the broker configuration"
)
"""The probe's generic rejected-connection sentence, recorded here verbatim as the contract."""

_TLS_ERROR = ssl.SSLError("certificate verify failed")
"""The TLS failure every TLS probe test raises from ``connect``."""

_MQTT_TLS_FAILURE = (
    f"the TLS connection to the MQTT broker at {_MQTT_HOST}:{_MQTT_PORT} failed "
    f"({_TLS_ERROR}) — check mqtt.tls, the broker certificate and that mqtt.port is the TLS port"
)
"""The probe's TLS-failure sentence, recorded here verbatim as the contract."""

_MQTT_TIMEOUT = (
    f"the MQTT broker at {_MQTT_HOST}:{_MQTT_PORT} did not answer within "
    f"{_MQTT_PROBE_BUDGET:g} s — check that the broker is reachable from this device"
)
"""The probe's timeout sentence, recorded here verbatim as the contract."""

_MQTT_BAD_INSTANCE_ID = (
    "mqtt.instance_id must start with a letter or digit and contain only letters, digits, "
    "dots, underscores and dashes — it is used in the MQTT topic tree and the client id, so "
    "/, + and # are not allowed"
)
"""The loader's unusable-instance-id sentence, recorded here verbatim as the contract."""


class _FakeAudioBackend:
    """An :class:`~chime_deck.audio.AudioBackend` that records a tone instead of playing it.

    ``play`` really waits :data:`_FAKE_PLAY_SECONDS` on the event loop, so the
    probe's elapsed time is non-zero and the latency clamp has something to
    clamp. ``set_error`` and ``play_error`` are set by a test to make
    :meth:`set_device` / :meth:`play` raise, which is how "no output device" and
    "a chime is already playing" are simulated.
    """

    def __init__(self, devices: Sequence[AudioDevice] | None = None) -> None:
        """Create a backend offering *devices* (one plausible output by default).

        :param devices: outputs :meth:`list_devices` reports; ``None`` means one
            ``pipewire`` output named ``pipewire/Offline-Output``.
        """
        self.devices: list[AudioDevice] = (
            [AudioDevice(name="pipewire/Offline-Output", hostapi="pipewire", is_default=True)]
            if devices is None
            else list(devices)
        )
        self.set_error: Exception | None = None
        self.play_error: Exception | None = None
        self.hints: list[str] = []
        self.played: list[Path] = []
        self.volumes: list[int] = []
        self.start_timeouts: list[float | None] = []

    @property
    def playing(self) -> bool:
        """Return whether a tone is in flight; a fake never overlaps two tones."""
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
        """Record the tone and stay in flight long enough to measure, or raise.

        :param path: tone that would have been played.
        :param volume: volume that would have been used.
        :param lead_in_ms: silence a real backend would have prepended.
        :param when_done: completion callback, run once the fake's playback ends.
        :returns: the lead-in echoed back, as a real backend reports what it applied.
        :raises Exception: the configured ``play_error``, when a test set one.
        """
        if self.play_error is not None:
            raise self.play_error
        self.played.append(path)
        self.volumes.append(volume)
        await asyncio.sleep(_FAKE_PLAY_SECONDS)
        if when_done is not None:
            when_done()
        return lead_in_ms

    async def stop_playback(self) -> None:
        """Do nothing: nothing is ever playing."""

    async def set_device(self, hint: str) -> None:
        """Record the hint, or raise the configured failure.

        :param hint: device hint the probe read from ``audio.device_hint``.
        :raises Exception: the configured ``set_error``, when a test set one.
        """
        if self.set_error is not None:
            raise self.set_error
        self.hints.append(hint)

    async def list_devices(self) -> list[AudioDevice]:
        """Return the configured outputs without asking the host."""
        return list(self.devices)


class _ScriptedClock:
    """Return a fixed list of monotonic readings and count how often it was read.

    The last reading repeats, so an unexpected extra read cannot crash a test —
    it only makes ``reads`` exceed the count the test asserts.
    """

    def __init__(self, readings: Sequence[float]) -> None:
        """Record the readings to hand out, in order.

        :param readings: clock values, first to last.
        """
        self._readings = list(readings)
        self.reads = 0

    def __call__(self) -> float:
        """Return the next scripted reading, repeating the last one."""
        index = min(self.reads, len(self._readings) - 1)
        self.reads += 1
        return self._readings[index]


class _FakeProbes:
    """Configurable probe set: return a result, raise, or hang until cancelled."""

    def __init__(
        self,
        *,
        ha: HaProbeResult | None = None,
        mqtt: MqttProbeResult | None = None,
        audio: AudioProbeResult | None = None,
        error: str | None = None,
        hang: asyncio.Event | None = None,
        cancelled: asyncio.Event | None = None,
    ) -> None:
        """Configure what each probe does.

        :param ha: result returned by :meth:`test_ha`.
        :param mqtt: result returned by :meth:`test_mqtt`.
        :param audio: result returned by :meth:`test_audio`.
        :param error: when set, every probe raises ``RuntimeError(error)``.
        :param hang: when set, every probe blocks until it is cancelled.
        :param cancelled: witness set when a hanging probe is cancelled (or returns).
        """
        self._ha = ha
        self._mqtt = mqtt
        self._audio = audio
        self._error = error
        self._hang = hang
        self._cancelled = cancelled

    async def _wait(self) -> None:
        """Block forever when *hang* is configured, recording the cancellation."""
        if self._hang is None:
            return
        try:
            await asyncio.sleep(60)
        finally:
            if self._cancelled is not None:
                self._cancelled.set()

    def _raise_if_configured(self) -> None:
        """Raise the configured exception, if any."""
        if self._error is not None:
            raise RuntimeError(self._error)

    async def test_ha(self) -> HaProbeResult:
        """Return the configured Home Assistant result."""
        self._raise_if_configured()
        await self._wait()
        return self._ha if self._ha is not None else HaProbeResult(ok=False)

    async def test_mqtt(self) -> MqttProbeResult:
        """Return the configured MQTT result."""
        self._raise_if_configured()
        await self._wait()
        return self._mqtt if self._mqtt is not None else MqttProbeResult(ok=False)

    async def test_audio(self) -> AudioProbeResult:
        """Return the configured audio result."""
        self._raise_if_configured()
        await self._wait()
        return self._audio if self._audio is not None else AudioProbeResult(ok=False)


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Keep the root logger and the registered secrets isolated per test."""
    with preserve_log_state():
        yield


def _build_app(
    tmp_path: Path,
    *,
    probes: ConnectionProbes | None = None,
    probe_timeout: float | None = None,
    ha_client: httpx.AsyncClient | None = None,
) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory.
    :param probes: connection probes to inject; ``None`` keeps the default.
    :param probe_timeout: timeout to inject; ``None`` keeps the default.
    :param ha_client: borrowed Home Assistant transport for the default probe;
        ``None`` lets the probe build and close its own client.
    """
    config = ServerConfig(
        data_dir=tmp_path, static_dir=tmp_path / "no-dist", seed_demo_sounds=False
    )
    if probes is not None and probe_timeout is not None:
        app = create_app(config, probes=probes, probe_timeout=probe_timeout, ha_client=ha_client)
    elif probes is not None:
        app = create_app(config, probes=probes, ha_client=ha_client)
    elif probe_timeout is not None:
        app = create_app(config, probe_timeout=probe_timeout, ha_client=ha_client)
    else:
        app = create_app(config, ha_client=ha_client)
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


def _real_audio_probes(
    state: AppState,
    *,
    backend: AudioBackend | None,
    clock: Callable[[], float],
) -> LiveProbes:
    """Return the real probe set over *state*'s store and a fake speaker.

    The store only exists once ``create_app`` has built the app, so the probes
    are installed on the live state instead of being passed to ``create_app``.
    The route reads ``state.probes`` per request, which is why that works.

    :param state: live application state of the app under test.
    :param backend: speaker the probe drives, or ``None`` for a pipeline that
        never started.
    :param clock: monotonic clock the latency is measured with.
    """
    return LiveProbes(state.store, audio_backend=lambda: backend, clock=clock)


def _real_mqtt_probes(
    state: AppState,
    broker: FakeBroker,
    *,
    timeout_s: float = MQTT_PROBE_TIMEOUT_S,
) -> LiveProbes:
    """Return the real probe set over *state*'s store and the in-memory broker.

    Installed on the live state for the same reason :func:`_real_audio_probes`
    is: the store only exists once ``create_app`` has built the app, and the
    route reads ``state.probes`` per request.

    :param state: live application state of the app under test.
    :param broker: in-memory broker supplying the client factory.
    :param timeout_s: inner budget for one attempt; the default is the shipped one.
    """
    return LiveProbes(state.store, mqtt_client_factory=broker.factory, mqtt_timeout_s=timeout_s)


async def _configure_mqtt(
    state: AppState,
    *,
    host: str = _MQTT_HOST,
    port: int = _MQTT_PORT,
    username: str = "",
    password: str = "",
    tls: bool = False,
) -> None:
    """Store the ``mqtt.*`` settings an MQTT probe test needs.

    :param state: live application state of the app under test.
    :param host: broker host to store.
    :param port: broker port to store.
    :param username: broker username to store.
    :param password: broker password to store.
    :param tls: whether TLS is enabled for this broker.
    """
    await state.store.set_setting("mqtt.host", host)
    await state.store.set_setting("mqtt.port", port)
    await state.store.set_setting("mqtt.username", username)
    await state.store.set_setting("mqtt.password", password)
    await state.store.set_setting("mqtt.tls", tls)


def _mqtt_probe_threads() -> list[threading.Thread]:
    """Return the live threads named after the MQTT probe worker."""
    return [thread for thread in threading.enumerate() if thread.name == "mqtt-probe"]


async def _wait_for_no_mqtt_probe_threads() -> None:
    """Wait, bounded, until no MQTT probe thread is alive.

    :raises AssertionError: if a probe thread is still alive after two seconds.
    """
    for _attempt in range(200):
        if not _mqtt_probe_threads():
            return
        await asyncio.sleep(0.01)
    alive = len(_mqtt_probe_threads())
    raise AssertionError(f"{alive} mqtt-probe thread(s) are still alive")


async def test_default_probes_report_missing_configuration(tmp_path: Path) -> None:
    """A missing address is named before the missing client, and every test answers 200.

    ``mqtt.host`` ships with a default, so it is cleared first to exercise the
    unconfigured branch; ``ha.token`` is empty out of the box.

    The audio probe is REAL now (HSD-wpm0), so the app's own fake speaker answers
    this row: the autouse offline wiring starts a
    :class:`~chime_fakes.RecordingBackend`, which returns at once, so the start
    latency is unmeasurable and honestly ``None`` rather than zero. With a blank
    ``audio.device_hint`` the honest device label is mpv's ``auto`` fallback.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        ha = cast("dict[str, object]", (await client.post("/api/settings/test/ha")).json())
        await state.store.set_setting("mqtt.host", "")
        mqtt = cast("dict[str, object]", (await client.post("/api/settings/test/mqtt")).json())
        audio = cast("dict[str, object]", (await client.post("/api/settings/test/audio")).json())

    assert set(ha) == _HA_KEYS
    assert set(mqtt) == _MQTT_KEYS
    assert set(audio) == _AUDIO_KEYS

    assert ha["ok"] is False
    assert "ha.base_url" in cast(str, ha["error"])
    assert "ha.token" in cast(str, ha["error"])
    assert mqtt["ok"] is False
    assert "mqtt.host" in cast(str, mqtt["error"])

    assert audio["ok"] is True
    assert audio["device"] == "auto"
    assert audio["latency_ms"] is None
    assert audio["error"] is None
    audio_error = str(audio["error"])
    assert "not available in this build yet" not in audio_error
    assert "HSD-h13q" not in audio_error


async def test_default_probes_report_a_real_home_assistant(tmp_path: Path) -> None:
    """The default Home Assistant probe counts live entities; MQTT asks the fake broker.

    Audio is real: the row below is answered by the started fake speaker, and it
    makes no Home Assistant request of its own — the single ``/api/states`` call
    below belongs to the Home Assistant probe. The fake returns at once, so the
    latency is unmeasurable (``None``) rather than zero.

    MQTT is real too (HSD-8qf2): the app's default probe set now carries the
    wiring's client factory, so this row is answered by the in-memory broker's
    successful CONNACK rather than by a stub sentence. ``broker_version`` stays
    ``None`` because CONNACK carries no standards-based broker version.

    :param tmp_path: pytest temporary directory.
    """
    states = [
        {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
        {"entity_id": "lock.front_door", "state": "locked", "attributes": {}},
        {"entity_id": "sensor.hall", "state": "21.4", "attributes": {}},
    ]
    handler = Scripted({("GET", "/api/states"): _replies(lambda: _json(states))})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, state = _build_app(tmp_path, ha_client=raw)
    try:
        async with _running(app) as client:
            await state.store.set_setting("ha.base_url", BASE_URL)
            await state.store.set_setting("ha.token", HA_TOKEN)
            await state.store.set_setting("mqtt.host", "core-mosquitto.local")

            ha = cast("dict[str, object]", (await client.post("/api/settings/test/ha")).json())
            mqtt_body = await client.post("/api/settings/test/mqtt")
            mqtt = cast("dict[str, object]", mqtt_body.json())
            audio_body = await client.post("/api/settings/test/audio")
            audio = cast("dict[str, object]", audio_body.json())
    finally:
        await raw.aclose()

    assert set(ha) == _HA_KEYS
    assert ha == {"ok": True, "ha_version": None, "entity_count": 3, "error": None}
    assert [request.url.path for request in handler.requests] == ["/api/states"]

    assert set(mqtt) == _MQTT_KEYS
    assert mqtt["ok"] is True
    assert mqtt["broker_version"] is None
    assert mqtt["error"] is None
    assert "HSD-kslf" not in mqtt_body.text

    assert audio["ok"] is True
    assert audio["device"] == "auto"
    assert audio["latency_ms"] is None
    assert audio["error"] is None
    assert "not available in this build yet" not in audio_body.text
    assert "HSD-h13q" not in audio_body.text


async def test_a_rejected_token_is_a_failure_without_the_secret(tmp_path: Path) -> None:
    """A 401 answers ``ok: false`` with the client's sentence and no token.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted({("GET", "/api/states"): httpx.Response(401, text="unauthorized")})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, state = _build_app(tmp_path, ha_client=raw)
    try:
        async with _running(app) as client:
            await state.store.set_setting("ha.base_url", BASE_URL)
            await state.store.set_setting("ha.token", HA_TOKEN)
            response = await client.post("/api/settings/test/ha")
    finally:
        await raw.aclose()

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["ok"] is False
    assert body["entity_count"] is None
    error = cast(str, body["error"])
    assert "ha.token" in error
    assert HA_TOKEN not in response.text


async def test_an_unreachable_home_assistant_is_reported(tmp_path: Path) -> None:
    """A connect failure answers ``ok: false`` with the client's own sentence.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted({("GET", "/api/states"): httpx.ConnectError("boom")})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, state = _build_app(tmp_path, ha_client=raw)
    try:
        async with _running(app) as client:
            await state.store.set_setting("ha.base_url", BASE_URL)
            await state.store.set_setting("ha.token", HA_TOKEN)
            response = await client.post("/api/settings/test/ha")
    finally:
        await raw.aclose()

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["ok"] is False
    assert "cannot connect" in cast(str, body["error"])


async def test_a_slow_home_assistant_times_out_within_the_bound(tmp_path: Path) -> None:
    """A transport that never answers is cancelled and reported as a timeout.

    :param tmp_path: pytest temporary directory.
    """
    gate = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        """Wait for a gate the test never opens.

        :param request: the state request.
        """
        await gate.wait()
        return _json([])

    handler = Scripted({("GET", "/api/states"): hang})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, state = _build_app(tmp_path, ha_client=raw, probe_timeout=0.05)
    try:
        async with _running(app) as client:
            await state.store.set_setting("ha.base_url", BASE_URL)
            await state.store.set_setting("ha.token", HA_TOKEN)
            started = time.perf_counter()
            response = await client.post("/api/settings/test/ha")
            elapsed = time.perf_counter() - started
    finally:
        await raw.aclose()

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["ok"] is False
    error = cast(str, body["error"])
    assert "did not answer within" in error
    assert "0.05" in error
    assert elapsed < 0.5


async def test_default_ha_probe_reports_an_empty_base_url(tmp_path: Path) -> None:
    """A token with no address reaches the unconfigured branch and names the address.

    ``ha.base_url`` ships with a non-empty default, so it is cleared first; the
    token is set so the missing piece reported back is the address, not the
    credential.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await state.store.set_setting("ha.base_url", "")
        await state.store.set_setting("ha.token", TOKEN)
        response = await client.post("/api/settings/test/ha")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert set(body) == _HA_KEYS
    assert body["ok"] is False
    error = cast(str, body["error"])
    assert "ha.base_url" in error
    assert "HSD-8qf6" not in error


async def test_injected_probe_success_shapes(tmp_path: Path) -> None:
    """A successful probe fills the documented keys with its values.

    :param tmp_path: pytest temporary directory.
    """
    probes = _FakeProbes(
        ha=HaProbeResult(ok=True, ha_version="2026.1.0", entity_count=12),
        mqtt=MqttProbeResult(ok=True, broker_version="mosquitto 2.0.18"),
        audio=AudioProbeResult(ok=True, device="speakers", latency_ms=42.5),
    )
    app, _state = _build_app(tmp_path, probes=probes)

    async with _running(app) as client:
        ha = (await client.post("/api/settings/test/ha")).json()
        mqtt = (await client.post("/api/settings/test/mqtt")).json()
        audio = (await client.post("/api/settings/test/audio")).json()

    assert ha == {"ok": True, "ha_version": "2026.1.0", "entity_count": 12, "error": None}
    assert mqtt == {
        "ok": True,
        "broker_version": "mosquitto 2.0.18",
        "error": None,
        "auth_failed": False,
    }
    assert audio == {"ok": True, "device": "speakers", "latency_ms": 42.5, "error": None}


async def test_injected_probe_failure_shape(tmp_path: Path) -> None:
    """A failing probe keeps every documented key and leaves the unused ones null.

    :param tmp_path: pytest temporary directory.
    """
    probes = _FakeProbes(
        ha=HaProbeResult(ok=False, error="the token was rejected by Home Assistant"),
        mqtt=MqttProbeResult(ok=False, error="the broker refused the connection"),
        audio=AudioProbeResult(ok=False, error="no output device answered"),
    )
    app, _state = _build_app(tmp_path, probes=probes)

    async with _running(app) as client:
        ha = cast("dict[str, object]", (await client.post("/api/settings/test/ha")).json())
        mqtt = cast("dict[str, object]", (await client.post("/api/settings/test/mqtt")).json())
        audio = cast("dict[str, object]", (await client.post("/api/settings/test/audio")).json())

    assert ha == {
        "ok": False,
        "ha_version": None,
        "entity_count": None,
        "error": "the token was rejected by Home Assistant",
    }
    assert mqtt == {
        "ok": False,
        "broker_version": None,
        "error": "the broker refused the connection",
        "auth_failed": False,
    }
    assert audio == {
        "ok": False,
        "device": None,
        "latency_ms": None,
        "error": "no output device answered",
    }


@pytest.mark.parametrize(
    "path", ["/api/settings/test/ha", "/api/settings/test/mqtt", "/api/settings/test/audio"]
)
async def test_probe_exception_is_reported_without_the_secret(tmp_path: Path, path: str) -> None:
    """A probe that raises answers 200 with ``ok: false`` and a scrubbed message.

    The ``[REDACTED]`` marker is the positive control: it proves the scrubbing
    layer ran rather than the message being absent for another reason.

    :param tmp_path: pytest temporary directory.
    :param path: connection test route under test.
    """
    probes = _FakeProbes(error=f"auth failed for {TOKEN}")
    app, _state = _build_app(tmp_path, probes=probes)

    async with _running(app) as client:
        # The lifespan registers the stored secrets, which clears anything
        # registered before startup, so register the token afterwards.
        install_log_scrubber()
        SECRET_SCRUBBER.set_secrets([TOKEN])
        response = await client.post(path)

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["ok"] is False
    assert TOKEN not in response.text
    assert REDACTED in response.text
    assert cast(str, body["error"]).startswith("auth failed for ")


async def test_hung_probe_times_out_within_the_bound(tmp_path: Path) -> None:
    """A probe that never answers is cancelled and reported as a timeout.

    :param tmp_path: pytest temporary directory.
    """
    cancelled = asyncio.Event()
    probes = _FakeProbes(hang=asyncio.Event(), cancelled=cancelled)
    app, _state = _build_app(tmp_path, probes=probes, probe_timeout=0.05)

    async with _running(app) as client:
        started = time.perf_counter()
        response = await client.post("/api/settings/test/ha")
        elapsed = time.perf_counter() - started

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["ok"] is False
    error = cast(str, body["error"])
    assert "did not answer within" in error
    assert "0.05" in error
    assert elapsed < 0.5
    assert cancelled.is_set() is True


async def test_probe_timeout_defaults_to_five_seconds(tmp_path: Path) -> None:
    """The documented default bound is five seconds and reaches the app state.

    :param tmp_path: pytest temporary directory.
    """
    _app, state = _build_app(tmp_path)

    assert PROBE_TIMEOUT_S == 5.0
    assert state.probe_timeout == 5.0
    assert state.probes is not None


async def test_the_recorded_audio_sentences_are_the_probes_own() -> None:
    """The contract recorded at the top of this file is the text the probe returns.

    Pinning the prose in two places (this module and the probe) means a silent
    reword fails here instead of in front of the user.
    """
    assert AUDIO_NOT_STARTED_TEXT == _NOT_STARTED


async def test_the_audio_probe_resolves_the_device_and_measures_the_latency(
    tmp_path: Path,
) -> None:
    """A successful probe names the real device and measures its own start latency.

    The stored hint is a lowercase substring of a mixed-case device name, which
    proves the caller is not required to match the device's case.

    :param tmp_path: pytest temporary directory.
    """
    backend = _FakeAudioBackend(
        devices=[AudioDevice(name="pipewire/Offline-Output", hostapi="pipewire")]
    )
    clock = _ScriptedClock([10.000, 10.512])
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await state.store.set_setting("audio.device_hint", "offline-output")
        state.probes = _real_audio_probes(state, backend=backend, clock=clock)
        response = await client.post("/api/settings/test/audio")

    body = cast("dict[str, object]", response.json())
    assert set(body) == _AUDIO_KEYS
    assert body["ok"] is True
    assert body["device"] == "pipewire/Offline-Output"
    assert body["latency_ms"] == 112.0
    assert body["error"] is None
    assert clock.reads == 2
    assert backend.hints == ["offline-output"]
    assert backend.volumes == [70]
    assert len(backend.played) == 1


async def test_the_audio_probe_reports_no_latency_when_the_tone_finishes_early(
    tmp_path: Path,
) -> None:
    """A tone the engine finished early yields no latency, never a fake zero.

    ``10.232`` is the real measurement that motivated this rule: the real
    :class:`~chime_deck.audio.MpvBackend` returned a 0.4 s tone after 0.232 s
    on this dev host. libmpv's end-file event fires once the tone is buffered
    ahead, so the start delay is below what this method can resolve. ``None`` is
    the honest "not measurable this time", and it says nothing about whether a
    speaker was involved.

    :param tmp_path: pytest temporary directory.
    """
    backend = _FakeAudioBackend()
    clock = _ScriptedClock([10.000, 10.232])
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        state.probes = _real_audio_probes(state, backend=backend, clock=clock)
        response = await client.post("/api/settings/test/audio")

    body = cast("dict[str, object]", response.json())
    assert set(body) == _AUDIO_KEYS
    assert body["ok"] is True
    assert body["device"] == "auto"
    assert body["latency_ms"] is None
    assert body["error"] is None
    assert backend.played != []


async def test_the_audio_probe_still_names_the_device_when_the_latency_is_unmeasurable(
    tmp_path: Path,
) -> None:
    """An unmeasurable latency still reports the resolved device.

    A device that resolved is the one the tone was sent to, so the user needs it
    to know which output answered; only the start delay is unavailable.

    :param tmp_path: pytest temporary directory.
    """
    backend = _FakeAudioBackend(
        devices=[AudioDevice(name="pipewire/Offline-Output", hostapi="pipewire")]
    )
    clock = _ScriptedClock([10.000, 10.232])
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await state.store.set_setting("audio.device_hint", "offline-output")
        state.probes = _real_audio_probes(state, backend=backend, clock=clock)
        response = await client.post("/api/settings/test/audio")

    body = cast("dict[str, object]", response.json())
    assert body["device"] == "pipewire/Offline-Output"
    assert body["latency_ms"] is None
    assert body["ok"] is True


async def test_the_audio_probe_reports_a_measured_latency_when_the_tone_is_buffered_ahead(
    tmp_path: Path,
) -> None:
    """A run at least as long as the tone backs a real start latency.

    ``10.632`` is the ARM board's real paced number at volume 40: 0.232 s past
    the tone's own 0.4 s length, so the measured start delay is 232.0 ms.

    :param tmp_path: pytest temporary directory.
    """
    backend = _FakeAudioBackend()
    clock = _ScriptedClock([10.000, 10.632])
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        state.probes = _real_audio_probes(state, backend=backend, clock=clock)
        response = await client.post("/api/settings/test/audio")

    body = cast("dict[str, object]", response.json())
    assert body["ok"] is True
    assert body["latency_ms"] == 232.0


async def test_the_audio_probe_accepts_playback_paced_by_a_real_sink(tmp_path: Path) -> None:
    """A sink that really plays paces the tone, so the latency is measurable.

    ``10.452`` is 0.052 s past the tone's own 0.4 s length, which is the start
    latency the probe reports.

    :param tmp_path: pytest temporary directory.
    """
    backend = _FakeAudioBackend()
    clock = _ScriptedClock([10.000, 10.452])
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        state.probes = _real_audio_probes(state, backend=backend, clock=clock)
        response = await client.post("/api/settings/test/audio")

    body = cast("dict[str, object]", response.json())
    assert body["ok"] is True
    assert body["latency_ms"] == 52.0
    assert body["error"] is None


async def test_the_audio_probe_reports_no_latency_for_a_zero_length_playback(
    tmp_path: Path,
) -> None:
    """Two identical clock readings leave the start delay unmeasurable.

    ``elapsed == 0.0`` is shorter than the tone, so the probe cannot resolve a
    start delay and must not fabricate ``0.0`` ms: it reports success with
    ``latency_ms=None``.

    :param tmp_path: pytest temporary directory.
    """
    backend = _FakeAudioBackend()
    clock = _ScriptedClock([10.0, 10.0])
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        state.probes = _real_audio_probes(state, backend=backend, clock=clock)
        response = await client.post("/api/settings/test/audio")

    body = cast("dict[str, object]", response.json())
    assert body["ok"] is True
    assert body["latency_ms"] is None
    assert body["error"] is None


async def test_the_audio_probe_reports_a_host_with_no_output_device(tmp_path: Path) -> None:
    """Selecting a device on a host with no output reports the backend's own sentence.

    :param tmp_path: pytest temporary directory.
    """
    backend = _FakeAudioBackend(devices=[])
    backend.set_error = NoAudioDeviceError(
        "no audio output device was found — check that a sound card is present and that the "
        "service user can reach the sound server"
    )
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        state.probes = _real_audio_probes(state, backend=backend, clock=_ScriptedClock([1.0, 2.0]))
        response = await client.post("/api/settings/test/audio")

    body = cast("dict[str, object]", response.json())
    assert body["ok"] is False
    assert "no audio output device was found" in cast(str, body["error"])
    assert backend.played == []


async def test_the_audio_probe_reports_a_chime_already_playing_verbatim(tmp_path: Path) -> None:
    """A chime in flight is reported with the player's own sentence, nothing else.

    :param tmp_path: pytest temporary directory.
    """
    sentence = "a chime is already playing, so the test tone cannot start"
    backend = _FakeAudioBackend()
    backend.play_error = PlaybackInProgressError(sentence)
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        state.probes = _real_audio_probes(state, backend=backend, clock=_ScriptedClock([1.0, 2.0]))
        response = await client.post("/api/settings/test/audio")

    body = cast("dict[str, object]", response.json())
    assert body["ok"] is False
    assert body["error"] == sentence
    assert body["device"] is None
    assert body["latency_ms"] is None


async def test_the_audio_probe_reports_a_pipeline_that_never_started(tmp_path: Path) -> None:
    """No started backend answers the honest not-started sentence, not a 500.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        state.probes = LiveProbes(state.store, audio_backend=lambda: None)
        response = await client.post("/api/settings/test/audio")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["ok"] is False
    assert body["error"] == _NOT_STARTED


async def test_the_audio_probe_reports_a_muted_volume(tmp_path: Path) -> None:
    """A zero master volume is named before anything is played, so nothing rings.

    :param tmp_path: pytest temporary directory.
    """
    backend = _FakeAudioBackend()
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await state.store.set_setting("audio.volume", 0)
        state.probes = _real_audio_probes(state, backend=backend, clock=_ScriptedClock([1.0, 2.0]))
        response = await client.post("/api/settings/test/audio")

    body = cast("dict[str, object]", response.json())
    assert body["ok"] is False
    error = body["error"]
    assert isinstance(error, str)
    assert "audio.volume" in error
    assert error == _MUTED
    assert backend.played == []
    assert backend.volumes == []


async def test_the_audio_probe_reports_a_mute_state(tmp_path: Path) -> None:
    """``audio.muted`` is named before the volume check, so nothing is played (D28).

    :param tmp_path: pytest temporary directory.
    """
    backend = _FakeAudioBackend()
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await state.store.set_setting("audio.muted", True)
        state.probes = _real_audio_probes(state, backend=backend, clock=_ScriptedClock([1.0, 2.0]))
        response = await client.post("/api/settings/test/audio")

    body = cast("dict[str, object]", response.json())
    assert body["ok"] is False
    error = body["error"]
    assert isinstance(error, str)
    assert "audio.muted" in error
    assert error == _MUTE_ON
    assert backend.played == []
    assert backend.volumes == []


async def test_the_stale_audio_stub_sentence_is_gone(tmp_path: Path) -> None:
    """Neither a successful nor a device-less probe mentions the retired stub text.

    This is the regression guard for HSD-wpm0: before it, every audio row
    answered "the audio engine is not available in this build yet (HSD-h13q)".

    :param tmp_path: pytest temporary directory.
    """
    working = _FakeAudioBackend()
    failing = _FakeAudioBackend(devices=[])
    failing.set_error = NoAudioDeviceError("no audio output device was found")
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        state.probes = _real_audio_probes(state, backend=working, clock=_ScriptedClock([5.0, 5.9]))
        success = await client.post("/api/settings/test/audio")
        state.probes = _real_audio_probes(state, backend=failing, clock=_ScriptedClock([5.0, 5.9]))
        no_device = await client.post("/api/settings/test/audio")

    for response in (success, no_device):
        assert response.status_code == 200
        assert "not available in this build yet" not in response.text
        assert "HSD-h13q" not in response.text


async def test_the_route_round_trips_a_real_audio_result(tmp_path: Path) -> None:
    """A probe result crosses the route unchanged, key set included.

    :param tmp_path: pytest temporary directory.
    """
    probes = _FakeProbes(audio=AudioProbeResult(ok=True, device="pipewire/x", latency_ms=42.5))
    app, _state = _build_app(tmp_path, probes=probes)

    async with _running(app) as client:
        response = await client.post("/api/settings/test/audio")

    body = cast("dict[str, object]", response.json())
    assert set(body) == _AUDIO_KEYS
    assert body == {"ok": True, "device": "pipewire/x", "latency_ms": 42.5, "error": None}


async def test_the_mqtt_probe_reports_an_unconfigured_broker(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """A blank ``mqtt.host`` answers the unconfigured sentence without connecting.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker supplying the client factory the probe would use.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await state.store.set_setting("mqtt.host", "")
        state.probes = _real_mqtt_probes(state, fake_broker)
        response = await client.post("/api/settings/test/mqtt")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert set(body) == _MQTT_KEYS
    assert body == {
        "ok": False,
        "broker_version": None,
        "error": _MQTT_UNCONFIGURED,
        "auth_failed": False,
    }


async def test_the_mqtt_probe_names_a_refused_connection(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """A refused TCP connection answers the actionable sentence, host and port included.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker whose ``connect`` raises the refusal.
    """
    fake_broker.connect_raises = ConnectionRefusedError(111, "Connection refused")
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _configure_mqtt(state)
        state.probes = _real_mqtt_probes(state, fake_broker)
        response = await client.post("/api/settings/test/mqtt")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body == {
        "ok": False,
        "broker_version": None,
        "error": _MQTT_REFUSED,
        "auth_failed": False,
    }
    assert "mqtt.port" in cast(str, body["error"])


async def test_the_mqtt_probe_names_an_unresolvable_host(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """A name the resolver cannot resolve answers the ``mqtt.host`` sentence.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker whose ``connect`` raises the lookup failure.
    """
    fake_broker.connect_raises = socket.gaierror(8, "nodename nor servname provided")
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _configure_mqtt(state)
        state.probes = _real_mqtt_probes(state, fake_broker)
        response = await client.post("/api/settings/test/mqtt")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body == {
        "ok": False,
        "broker_version": None,
        "error": _MQTT_UNRESOLVED,
        "auth_failed": False,
    }


async def test_the_mqtt_probe_names_rejected_credentials(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """A CONNACK refusal for a bad username or password names both settings.

    The stored password is sent by the probe and must never be echoed back.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker that refuses the CONNACK as unauthorised.
    """
    fake_broker.connack_name = "Not authorized"
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _configure_mqtt(state, username="doorbell", password="hunter2")
        state.probes = _real_mqtt_probes(state, fake_broker)
        response = await client.post("/api/settings/test/mqtt")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body == {
        "ok": False,
        "broker_version": None,
        "error": _MQTT_BAD_CREDENTIALS,
        "auth_failed": True,
    }
    assert "hunter2" not in response.text


async def test_the_mqtt_probe_names_any_other_rejected_connection(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """A CONNACK refusal that is not about credentials names the instance id instead.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker that refuses the CONNACK for the client identifier.
    """
    fake_broker.connack_name = "Client identifier not valid"
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _configure_mqtt(state)
        state.probes = _real_mqtt_probes(state, fake_broker)
        response = await client.post("/api/settings/test/mqtt")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body == {
        "ok": False,
        "broker_version": None,
        "error": _MQTT_REJECTED,
        "auth_failed": False,
    }
    assert "mqtt.instance_id" in cast(str, body["error"])


async def test_the_mqtt_probe_names_a_tls_failure(tmp_path: Path, fake_broker: FakeBroker) -> None:
    """A failed TLS setup answers the TLS sentence with the library's own detail.

    ``mqtt.tls`` was on, so the probe did reach ``tls_set()`` before the failure.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker whose ``connect`` raises the TLS failure.
    """
    fake_broker.connect_raises = _TLS_ERROR
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _configure_mqtt(state, tls=True)
        state.probes = _real_mqtt_probes(state, fake_broker)
        response = await client.post("/api/settings/test/mqtt")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body == {
        "ok": False,
        "broker_version": None,
        "error": _MQTT_TLS_FAILURE,
        "auth_failed": False,
    }
    assert count(fake_broker.events, ("tls_set",)) >= 1


async def test_the_mqtt_probe_times_out_within_its_own_budget(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """A broker that never sends a CONNACK is reported after the injected budget.

    The injected budget is 0.05 s, so the real wait is milliseconds and the
    sentence names the budget that attempt actually used. The route's outer
    five-second timeout is never what ends this request.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker that never fires the queued CONNACK.
    """
    fake_broker.connack_never = True
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _configure_mqtt(state)
        state.probes = _real_mqtt_probes(state, fake_broker, timeout_s=_MQTT_PROBE_BUDGET)
        started = time.perf_counter()
        response = await client.post("/api/settings/test/mqtt")
        elapsed = time.perf_counter() - started

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body == {
        "ok": False,
        "broker_version": None,
        "error": _MQTT_TIMEOUT,
        "auth_failed": False,
    }
    assert "0.05" in cast(str, body["error"])
    assert elapsed < 0.5


async def test_the_mqtt_probe_reports_an_unusable_stored_setting(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """A stored ``mqtt.instance_id`` with a slash reports the loader's own sentence.

    The registry accepts any 64-character string, so an old or hand-edited
    database can hold a value the bridge refuses; the probe reports that refusal
    verbatim instead of connecting with an id the broker would reject.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker supplying the client factory the probe would use.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _configure_mqtt(state)
        await state.store.set_setting("mqtt.instance_id", "bad/id")
        state.probes = _real_mqtt_probes(state, fake_broker)
        response = await client.post("/api/settings/test/mqtt")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body == {
        "ok": False,
        "broker_version": None,
        "error": _MQTT_BAD_INSTANCE_ID,
        "auth_failed": False,
    }


async def test_the_mqtt_probe_connects_with_the_stored_settings(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """A successful attempt uses the stored credentials and TLS, then disconnects.

    The event log is cleared after the settings write, so what remains is exactly
    the probe's own traffic: no ``loop_start`` (there is no paho network thread),
    one ``disconnect`` in the ``finally``, and a client id that carries the
    ``-probe`` suffix so the probe can never kick the running bridge off the
    broker.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker that answers a successful CONNACK from ``loop``.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _configure_mqtt(state, username="doorbell", password="hunter2", tls=True)
        fake_broker.events.clear()
        fake_broker.factory_calls.clear()
        state.probes = _real_mqtt_probes(state, fake_broker)
        response = await client.post("/api/settings/test/mqtt")
        events = list(fake_broker.events)
        client_ids = [str(call["client_id"]) for call in fake_broker.factory_calls]

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert set(body) == _MQTT_KEYS
    assert body == {
        "ok": True,
        "broker_version": None,
        "error": None,
        "auth_failed": False,
    }
    assert ("username_pw_set", "doorbell", "hunter2") in events
    assert ("tls_set",) in events
    assert ("connect", _MQTT_HOST, _MQTT_PORT, DEFAULT_KEEPALIVE_S) in events
    assert count(events, ("disconnect",)) == 1
    assert count(events, ("loop_start",)) == 0
    assert client_ids == ["chime-deck-default-probe"]


async def test_a_broker_that_rejects_anonymous_clients_reports_an_auth_failure(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """An anonymous probe is refused by a broker that requires credentials.

    wizard step 2 owns mqtt.username/mqtt.password (HSD-9xpm): the stored settings
    carry no credentials, so the probe really sends none and the broker's own
    CONNACK is what classifies the failure as an auth failure.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker that refuses a client without the required credentials.
    """
    fake_broker.required_credentials = _REQUIRED_CREDENTIALS
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _configure_mqtt(state)
        fake_broker.events.clear()
        state.probes = _real_mqtt_probes(state, fake_broker)
        response = await client.post("/api/settings/test/mqtt")
        events = list(fake_broker.events)

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body == {
        "ok": False,
        "broker_version": None,
        "error": _MQTT_ANONYMOUS_REFUSAL_TEXT,
        "auth_failed": True,
    }
    assert not any(event[0] == "username_pw_set" for event in events)


async def test_the_mqtt_probe_authenticates_against_a_broker_that_requires_credentials(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """The probe presents the stored credentials and the broker accepts it.

    wizard step 2 owns mqtt.username/mqtt.password (HSD-9xpm): once they are stored,
    the same broker that refused the anonymous probe answers with a success.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker that refuses a client without the required credentials.
    """
    fake_broker.required_credentials = _REQUIRED_CREDENTIALS
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _configure_mqtt(state, username="doorbell", password="hunter2")
        fake_broker.events.clear()
        state.probes = _real_mqtt_probes(state, fake_broker)
        response = await client.post("/api/settings/test/mqtt")
        events = list(fake_broker.events)

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body == {
        "ok": True,
        "broker_version": None,
        "error": None,
        "auth_failed": False,
    }
    assert ("username_pw_set", "doorbell", "hunter2") in events
    assert "hunter2" not in response.text


async def test_the_mqtt_probe_leaves_no_thread_behind(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """No ``mqtt-probe`` thread survives a success, a refusal or a timeout.

    The worker is a daemon that is never joined, so the honest guarantee is that
    it ends on its own: the bounded poll here fails if it does not.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker driven through the three outcomes in turn.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _configure_mqtt(state)
        state.probes = _real_mqtt_probes(state, fake_broker)
        await client.post("/api/settings/test/mqtt")
        await _wait_for_no_mqtt_probe_threads()

        fake_broker.connack_never = True
        state.probes = _real_mqtt_probes(state, fake_broker, timeout_s=_MQTT_PROBE_BUDGET)
        await client.post("/api/settings/test/mqtt")
        await _wait_for_no_mqtt_probe_threads()

        fake_broker.connack_never = False
        fake_broker.connect_raises = ConnectionRefusedError(111, "Connection refused")
        state.probes = _real_mqtt_probes(state, fake_broker)
        await client.post("/api/settings/test/mqtt")
        await _wait_for_no_mqtt_probe_threads()


@pytest.mark.parametrize(
    "path", ["/api/settings/test/ha", "/api/settings/test/mqtt", "/api/settings/test/audio"]
)
async def test_no_probe_route_mentions_a_retired_stub(tmp_path: Path, path: str) -> None:
    """No connection test answers with a stub sentence or an internal ticket id.

    This is the regression guard for HSD-8qf2 (and HSD-wpm0 before it): the MQTT
    row used to say "the MQTT bridge is not available in this build yet", which
    stopped being true when the bridge shipped.

    :param tmp_path: pytest temporary directory.
    :param path: connection test route under test.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.post(path)

    assert response.status_code == 200
    for retired in ("not available in this build yet", "HSD-kslf", "HSD-h13q"):
        assert retired not in response.text
