"""``/api/tts``: the local engine list, the voice catalogue and the download jobs.

Every test drives the real application through ``httpx.ASGITransport``. The
default install is exercised for real, and its answer depends on this machine —
whether the optional ``piper`` extra is present and how much RAM it reports — so
the catalogue test asserts the *consistency* of that answer rather than one
host's wording. The download routes are driven by plain fake manager and
registry objects so nothing opens a socket or touches a voice file.

Owning ticket: HSD-p50k.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state

from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig
from chime_deck.tts import (
    DEFAULT_PIPER_MIN_RAM_MB,
    SUGGESTED_VOICES,
    Availability,
    EngineStatus,
    InstalledVoice,
    SuggestedVoice,
    TtsOptions,
    TtsSpeechSource,
    VoiceDownloadJob,
    VoiceDownloadRegistry,
    VoiceError,
    VoiceErrorCode,
    VoiceManager,
    VoiceState,
    read_meminfo,
)
from chime_deck.tts.piper import (
    PIPER_MIN_RAM_SETTING,
    PROBE_THREAD_PREFIX,
    RENDER_THREAD_PREFIX,
)

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_ENGINES_ROUTE = "/api/tts/engines"
"""The local engine list under test."""

_VOICES_ROUTE = "/api/tts/voices"
"""The voice catalogue under test."""

_DOWNLOAD_ROUTE = "/api/tts/voices/en_GB-alan-low/download"
"""A curated voice id that needs no real model to be refused."""

_NOT_SUGGESTED = VoiceError(
    VoiceErrorCode.VOICE_NOT_SUGGESTED,
    "the voice 'en_GB-nope-low' is not in the curated list this doorbell can verify — choose "
    "one from the suggested list in Settings → Speech",
    {"voice": "en_GB-nope-low"},
)
"""A well-formed but uncurated id, refused before the network is touched."""

_MALFORMED = VoiceError(
    VoiceErrorCode.INVALID_VOICE,
    "the voice id '../etc/passwd' is not a valid Piper voice id — choose one from the suggested "
    "list in Settings → Speech",
    {"voice": "../etc/passwd"},
)
"""A hostile id, refused as a validation error."""

_ALREADY_RUNNING = VoiceError(
    VoiceErrorCode.VOICE_UNAVAILABLE,
    "the en_GB-alan-low voice is already downloading — wait for it to finish",
    {"reason": "already_running", "voice": "en_GB-alan-low"},
)
"""The one refusal that is a conflict rather than a validation error."""

_TOO_LARGE = VoiceError(
    VoiceErrorCode.VOICE_TOO_LARGE,
    "the en_GB-alan-low file is larger than the 128 MB limit — that voice cannot be installed "
    "on this device",
    {"voice": "en_GB-alan-low", "limit_bytes": 128 * 1024 * 1024},
)
"""An oversized body, refused as ``413``."""


class FakeManager:
    """A voice manager whose answers are fixed, so no file or network is read."""

    def __init__(
        self,
        *,
        gate: Availability | None = None,
        memory_warning: str | None = None,
        installed: list[InstalledVoice] | None = None,
    ) -> None:
        """Record the answers this fake gives.

        :param gate: answer to :meth:`gate`; ``None`` means installable.
        :param memory_warning: answer to :meth:`memory_warning`.
        :param installed: rows :meth:`installed` reports.
        """
        self._gate = Availability(True) if gate is None else gate
        self._memory_warning = memory_warning
        self._installed: list[InstalledVoice] = [] if installed is None else installed

    def suggested(self) -> list[SuggestedVoice]:
        """Return the real curated catalogue, in its shipped order."""
        return list(SUGGESTED_VOICES)

    def installed(self) -> list[InstalledVoice]:
        """Return the fixed installed rows; sync so ``asyncio.to_thread`` works."""
        return list(self._installed)

    async def gate(self) -> Availability:
        """Return the fixed install verdict."""
        return self._gate

    async def memory_warning(self) -> str | None:
        """Return the fixed D22 sentence, or ``None`` when RAM is sufficient."""
        return self._memory_warning

    def sweep_stale_temps(self) -> int:
        """Sweep nothing: the fake owns no temporary directories."""
        return 0

    def close(self) -> None:
        """Release nothing: the fake owns no client or thread."""


class FakeRegistry:
    """A download registry that starts jobs without downloading anything."""

    def __init__(
        self,
        *,
        job: VoiceDownloadJob | None = None,
        error: VoiceError | None = None,
    ) -> None:
        """Record the snapshot or the refusal :meth:`start` produces.

        :param job: snapshot returned on success.
        :param error: refusal raised instead of starting a job.
        """
        self._job = job
        self._error = error
        self.started: list[str] = []

    async def start(self, voice_id: str) -> VoiceDownloadJob:
        """Record the id and return the fixed snapshot or raise the fixed error.

        :param voice_id: curated voice id from the request.
        """
        self.started.append(voice_id)
        if self._error is not None:
            raise self._error
        if self._job is None:
            raise AssertionError("FakeRegistry needs a job or an error")
        return self._job

    def status(self, job_id: str) -> VoiceDownloadJob | None:
        """Return the fixed snapshot when *job_id* matches, else ``None``.

        :param job_id: id from the request.
        """
        if self._job is None or self._job.job_id != job_id:
            return None
        return self._job

    async def aclose(self) -> None:
        """Stop nothing: the fake starts no task."""


class SlowEngine:
    """An engine whose availability probe outlives the per-engine deadline."""

    name = "slow_engine"

    async def available(self) -> Availability:
        """Take much longer than the engine-status deadline allows."""
        await asyncio.sleep(5)
        return Availability(True)

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Fail: the engines route must never render.

        :param text: phrase that must not be spoken.
        :param options: options that must not be used.
        :param destination: path that must not be written.
        """
        raise AssertionError("the engines route must not render")

    async def warmup(self) -> None:
        """Do nothing; the fake needs no preparation."""
        return None


class ReadyEngine:
    """An engine that answers its availability probe immediately."""

    name = "ready_engine"

    async def available(self) -> Availability:
        """Report that this engine can speak."""
        return Availability(True)

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Write a payload, though the engines route never asks.

        :param text: phrase to speak.
        :param options: options for the render.
        :param destination: exact output path.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"RIFF....WAVEfmt ")
        return destination

    async def warmup(self) -> None:
        """Do nothing; the fake needs no preparation."""
        return None


class HangingSpeech:
    """A resolver whose status call outlives a shortened route budget."""

    is_disabled = False

    async def engine_status(self) -> list[EngineStatus]:
        """Sleep far longer than the test's route budget."""
        await asyncio.sleep(1)
        return []

    async def aclose(self) -> None:
        """Close nothing: this fake owns no worker."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test."""
    with preserve_log_state():
        yield


def _build_app(tmp_path: Path) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory used as the data directory.
    """
    config = ServerConfig(data_dir=tmp_path, static_dir=tmp_path / "no-dist")
    app = create_app(config)
    return app, cast(AppState, app.state.doorbell)


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield a client bound to it.

    :param app: application under test.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=_HTTP_TIMEOUT_S
        ) as client:
            yield client


def _job() -> VoiceDownloadJob:
    """Build one running download snapshot for the status route."""
    return VoiceDownloadJob(
        job_id="job-1",
        voice="en_GB-alan-low",
        state=VoiceState.RUNNING,
        bytes_done=1024,
        bytes_total=2048,
        error=None,
        detail=MappingProxyType({}),
        started_at="2026-01-01T00:00:00+00:00",
        finished_at=None,
    )


def _inject(state: AppState, manager: FakeManager, registry: FakeRegistry) -> None:
    """Put the fakes where the ``/api/tts`` router reads them.

    :param state: application state under test.
    :param manager: fake voice manager.
    :param registry: fake download registry.
    """
    state.voices = cast(VoiceManager, manager)
    state.voice_downloads = cast(VoiceDownloadRegistry, registry)


async def test_the_engines_are_listed_in_chain_order(tmp_path: Path) -> None:
    """The settings page sees every engine a press can try, in fallback-chain order.

    ``piper`` is unavailable on this host because the optional extra is absent and
    ``remote_tts`` is unavailable because no renderer URL is configured, so the
    assertions are about shape and about non-empty sentences — never about this
    machine's exact wording, beyond the one setting name the fix must name.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)
    async with _running(app) as client:
        response = await client.get(_ENGINES_ROUTE)

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["disabled"] is False
    assert body["warning"] is None
    engines = cast("list[dict[str, object]]", body["engines"])
    assert [engine["name"] for engine in engines] == [
        "remote_tts",
        "ha_tts",
        "piper",
        "espeak_ng",
        # The D25 provider engine (HSD-s856) is registered but deliberately not
        # in FALLBACK_CHAIN, so it is listed last: chain members come first.
        "http_tts",
    ]
    for engine in engines:
        if engine["available"] is False:
            reason = engine["reason"]
            assert isinstance(reason, str)
            assert reason.strip()
    by_name = {cast(str, engine["name"]): engine for engine in engines}
    assert by_name["piper"]["available"] is False
    piper_reason = cast(str, by_name["piper"]["reason"])
    assert "piper" in piper_reason
    assert by_name["piper"]["last_synthesis_ms"] is None
    assert by_name["remote_tts"]["available"] is False
    remote_reason = cast(str, by_name["remote_tts"]["reason"])
    assert remote_reason.strip()
    assert "tts.remote_tts.url" in remote_reason


async def test_speech_switched_off_is_an_empty_list_and_a_refused_download(
    tmp_path: Path,
) -> None:
    """D27: the deliberate off state has no engine and refuses a voice download.

    ``tts.default_engine`` is stored before the lifespan runs, because that is
    when the resolver reads it.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    await state.store.connect()
    try:
        await state.store.set_setting("tts.default_engine", "none")
    finally:
        await state.store.close()

    async with _running(app) as client:
        engines_response = await client.get(_ENGINES_ROUTE)
        voices_response = await client.get(_VOICES_ROUTE)
        download_response = await client.post(_DOWNLOAD_ROUTE)

    assert engines_response.status_code == 200
    engines = cast("dict[str, object]", engines_response.json())
    assert engines["disabled"] is True
    assert engines["engines"] == []
    warning = cast(str, engines["warning"])
    assert "switched off" in warning

    assert voices_response.status_code == 200
    voices = cast("dict[str, object]", voices_response.json())
    assert voices["disabled"] is True
    assert voices["can_download"] is False
    assert "switched off" in cast(str, voices["warning"])
    assert "switched off" in cast(str, voices["unavailable_reason"])

    assert download_response.status_code == 422
    error = cast("dict[str, object]", download_response.json()["error"])
    assert error["code"] == "voice_unavailable"


async def test_the_voice_catalogue_matches_this_environment(tmp_path: Path) -> None:
    """A default install lists the 13 curated voices and agrees with itself.

    The route's answer depends on this machine (the optional extra, the RAM), so
    this test asserts the *consistency* of that answer — a refusal always
    carries its sentence, the RAM warning appears exactly when this machine is
    too small — and never one host's wording. The specific refusals are proven
    by the injected-manager tests that follow.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)
    async with _running(app) as client:
        response = await client.get(_VOICES_ROUTE)

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["disabled"] is False
    suggested = cast("list[dict[str, object]]", body["suggested"])
    assert len(suggested) == 13
    qualities = [cast(str, voice["quality"]) for voice in suggested]
    assert qualities.index("medium") > max(
        index for index, quality in enumerate(qualities) if quality == "low"
    )
    for voice in suggested:
        assert voice["id"]
        assert voice["licence"]
        assert cast(float, voice["size_mb"]) > 0
        assert voice["installed"] is False
    assert body["installed"] == []
    reason = body["unavailable_reason"]
    assert (reason is None) == bool(body["can_download"])
    if reason is not None:
        assert isinstance(reason, str)
        assert reason.strip()
        assert " " in reason.strip()

    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        pytest.skip("no /proc/meminfo, so the D22 memory verdict cannot be checked here")
    total_mb, _available_mb = read_meminfo(meminfo)
    expected_warning = total_mb is not None and total_mb < DEFAULT_PIPER_MIN_RAM_MB
    warning = body["memory_warning"]
    assert (warning is not None) == expected_warning
    if warning is not None:
        assert isinstance(warning, str)
        assert PIPER_MIN_RAM_SETTING in warning


async def test_a_fake_registry_drives_start_and_status(tmp_path: Path) -> None:
    """With an installable manager, a download starts and its job is pollable.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    installed = InstalledVoice(
        id="en_GB-alan-low",
        onnx_path=tmp_path / "voices" / "en_GB-alan-low.onnx",
        config_path=tmp_path / "voices" / "en_GB-alan-low.onnx.json",
        bytes=63104526,
        licence="See model card",
        licence_known=True,
        checksum_verified=True,
        problem=None,
    )
    manager = FakeManager(installed=[installed])
    registry = FakeRegistry(job=_job())
    _inject(state, manager, registry)

    async with _running(app) as client:
        voices_response = await client.get(_VOICES_ROUTE)
        start_response = await client.post(_DOWNLOAD_ROUTE)
        status_response = await client.get("/api/tts/voices/downloads/job-1")
        unknown_response = await client.get("/api/tts/voices/downloads/missing")

    assert voices_response.status_code == 200
    voices = cast("dict[str, object]", voices_response.json())
    assert voices["can_download"] is True
    assert voices["unavailable_reason"] is None
    installed_rows = cast("list[dict[str, object]]", voices["installed"])
    assert [row["id"] for row in installed_rows] == ["en_GB-alan-low"]
    installed_row = installed_rows[0]
    assert installed_row["quality"] == "low"
    assert installed_row["language"] == "en_GB"
    assert cast(float, installed_row["size_mb"]) > 0
    suggested = cast("list[dict[str, object]]", voices["suggested"])
    alan = next(row for row in suggested if row["id"] == "en_GB-alan-low")
    assert alan["installed"] is True

    assert registry.started == ["en_GB-alan-low"]
    assert start_response.status_code == 202
    started = cast("dict[str, object]", start_response.json())
    assert started["job_id"] == "job-1"
    assert started["state"] == "running"

    assert status_response.status_code == 200
    status = cast("dict[str, object]", status_response.json())
    assert status["bytes_done"] == 1024
    assert status["bytes_total"] == 2048

    assert unknown_response.status_code == 404
    unknown = cast("dict[str, object]", unknown_response.json()["error"])
    assert unknown["code"] == "not_found"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        pytest.param(_MALFORMED, 422, "validation_error", id="malformed-id"),
        pytest.param(_NOT_SUGGESTED, 422, "validation_error", id="uncurated-id"),
        pytest.param(_ALREADY_RUNNING, 409, "conflict", id="already-running"),
        pytest.param(_TOO_LARGE, 413, "voice_too_large", id="too-large"),
    ],
)
async def test_a_refused_download_is_mapped_to_its_status(
    tmp_path: Path, error: VoiceError, status: int, code: str
) -> None:
    """Each voice error becomes the status and envelope code the API promises.

    :param tmp_path: pytest temporary directory.
    :param error: refusal the fake registry raises.
    :param status: expected HTTP status.
    :param code: expected envelope code.
    """
    app, state = _build_app(tmp_path)
    _inject(state, FakeManager(), FakeRegistry(error=error))

    async with _running(app) as client:
        response = await client.post(_DOWNLOAD_ROUTE)

    assert response.status_code == status
    body = cast("dict[str, object]", response.json()["error"])
    assert body["code"] == code
    assert body["message"] == str(error)


async def test_a_memory_gated_manager_refuses_the_download(tmp_path: Path) -> None:
    """Below the D22 threshold the page explains the RAM and refuses the start.

    The real registry is used on purpose: its own gate check is what turns the
    manager's verdict into the refusal.

    :param tmp_path: pytest temporary directory.
    """
    sentence = (
        "this machine reports 900 MB of RAM but local Piper needs 2048 MB — lower "
        "tts.piper.min_ram_mb or install a voice on a larger device"
    )
    app, state = _build_app(tmp_path)
    manager = FakeManager(gate=Availability(False, sentence), memory_warning=sentence)
    state.voices = cast(VoiceManager, manager)
    state.voice_downloads = VoiceDownloadRegistry(cast(VoiceManager, manager))

    async with _running(app) as client:
        voices_response = await client.get(_VOICES_ROUTE)
        download_response = await client.post(_DOWNLOAD_ROUTE)

    assert voices_response.status_code == 200
    voices = cast("dict[str, object]", voices_response.json())
    assert voices["can_download"] is False
    assert voices["unavailable_reason"] == sentence
    assert voices["memory_warning"] == sentence

    assert download_response.status_code == 422
    error = cast("dict[str, object]", download_response.json()["error"])
    assert error["code"] == "voice_unavailable"
    assert error["message"] == sentence


async def test_the_new_router_answers_before_the_ui_catch_all(tmp_path: Path) -> None:
    """``/api/tts/engines`` is served by this router, not the buttons router or UI.

    ``api/buttons.py`` is registered first and the SPA catch-all last, so the
    response proves the new router's registration point is effective.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)
    async with _running(app) as client:
        response = await client.get(_ENGINES_ROUTE)

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert "engines" in body


async def test_a_slow_engine_does_not_delay_the_engines_route(tmp_path: Path) -> None:
    """The probes run concurrently, so one slow engine costs 2 s, not 7 s.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    async with _running(app) as client:
        state.speech = TtsSpeechSource(state.store, [SlowEngine(), ReadyEngine()], None)
        started = time.monotonic()
        response = await client.get(_ENGINES_ROUTE)
        elapsed = time.monotonic() - started

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    engines = cast("list[dict[str, object]]", body["engines"])
    by_name = {cast(str, engine["name"]): engine for engine in engines}
    assert by_name["ready_engine"]["available"] is True
    slow = by_name["slow_engine"]
    assert slow["available"] is False
    assert "did not answer within 2 s" in cast(str, slow["reason"])
    assert elapsed < 3.0


async def test_the_engines_route_reports_its_own_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolver that outlives the shortened route budget answers ``200``.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: shortens the route deadline.
    """
    monkeypatch.setattr("chime_deck.api.tts.TTS_ENGINES_ROUTE_TIMEOUT_S", 0.05)
    app, state = _build_app(tmp_path)
    async with _running(app) as client:
        state.speech = cast(TtsSpeechSource, HangingSpeech())
        response = await client.get(_ENGINES_ROUTE)

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["engines"] == []
    assert "did not answer within 0.05 s" in cast(str, body["warning"])


async def test_engines_without_a_speech_source_are_reported(tmp_path: Path) -> None:
    """No resolver is a ``200`` with the startup sentence, never a ``500``.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    async with _running(app) as client:
        state.speech = None
        response = await client.get(_ENGINES_ROUTE)

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["engines"] == []
    assert "speech could not be prepared at startup" in cast(str, body["warning"])


async def test_voices_without_a_manager_are_reported(tmp_path: Path) -> None:
    """No voice manager is a ``200`` with an empty catalogue and a sentence.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    async with _running(app) as client:
        state.voices = None
        response = await client.get(_VOICES_ROUTE)

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["suggested"] == []
    assert body["can_download"] is False
    assert "not wired" in cast(str, body["warning"])


async def test_download_routes_without_a_registry_fail_cleanly(tmp_path: Path) -> None:
    """Both download routes answer a ``500`` envelope when the registry is absent.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    async with _running(app) as client:
        state.voice_downloads = None
        start_response = await client.post(_DOWNLOAD_ROUTE)
        status_response = await client.get("/api/tts/voices/downloads/x")

    assert start_response.status_code == 500
    start_error = cast("dict[str, object]", start_response.json()["error"])
    assert start_error["code"] == "internal_error"

    assert status_response.status_code == 500
    status_error = cast("dict[str, object]", status_response.json()["error"])
    assert status_error["code"] == "internal_error"


async def test_a_stale_voice_temp_directory_is_swept_at_startup(tmp_path: Path) -> None:
    """An old ``.*.tmp`` directory is removed, a fresh one is left alone.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    voices_dir = state.config.resolved_voices_dir
    voices_dir.mkdir(parents=True, exist_ok=True)
    stale = voices_dir / ".stale.tmp"
    stale.mkdir()
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = voices_dir / ".fresh.tmp"
    fresh.mkdir()

    async with _running(app):
        pass

    assert not stale.exists()
    assert fresh.exists()


async def test_shutdown_reclaims_the_piper_worker_threads(tmp_path: Path) -> None:
    """Neither the probe nor the render worker outlives the lifespan.

    The assertion is a *before/after* comparison, not "no piper thread exists":
    this test must pass in a process where an earlier test pointed a real
    :class:`~chime_deck.tts.piper.PiperProbe` at a piper that is installed
    and never shut it down. What the app owns, the app must reclaim.

    Where ``piper`` really is installed (the integration image and the board),
    the test also asserts that a probe thread existed *during* the run, so it
    cannot pass by never starting one on a piper-free host and then silently
    degrade to a vacuous before == after == [].

    :param tmp_path: pytest temporary directory.
    """

    def piper_threads() -> list[str]:
        """Return the names of this process's piper worker threads."""
        return sorted(
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith((PROBE_THREAD_PREFIX, RENDER_THREAD_PREFIX))
        )

    before = piper_threads()
    app, _state = _build_app(tmp_path)
    async with _running(app) as client:
        await client.get(_ENGINES_ROUTE)
        if importlib.util.find_spec("piper") is not None:
            for _ in range(50):
                if any(name.startswith(PROBE_THREAD_PREFIX) for name in piper_threads()):
                    break
                time.sleep(0.02)
            assert any(name.startswith(PROBE_THREAD_PREFIX) for name in piper_threads())

    for _ in range(50):
        if piper_threads() == before:
            break
        time.sleep(0.02)

    assert piper_threads() == before
