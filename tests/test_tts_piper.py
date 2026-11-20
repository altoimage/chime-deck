"""Local Piper engine and probe: memory gate, import probe, serialised renders.

Every test injects the module importer and the meminfo fixture, so nothing here
reads the ambient environment: no test looks at the real ``/proc/meminfo``, at
whether ``piper`` happens to be installed, at a real voice model or at an audio
device. The same suite therefore passes on a host without piper, in a container
that has it, and on the ARM board.

The fake ``piper`` module is a :class:`types.ModuleType` whose ``PiperVoice``
class has the verified 1.8.0 shape (``load`` is a ``staticmethod`` returning a
voice with ``synthesize_wav(text, wav_file)``). The fake voice writes a real
0.3 s mono 16-bit 22050 Hz WAV, records when it entered and left, counts how many
syntheses overlapped, and can be held on a :class:`threading.Event` to prove the
engine serialises rather than corrupts.

Owning ticket: HSD-p50k.
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import logging
import sys
import threading
import time
import types
import wave
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol

import pytest

from chime_deck.mqtt.bridge import DEFAULT_SEAM_TIMEOUT_S
from chime_deck.store import SETTINGS
from chime_deck.tts import (
    FALLBACK_CHAIN,
    TEMPLATE_TIMEOUT_S,
    Availability,
    PiperEngine,
    PiperProbe,
    PiperVoicePort,
    TtsOptions,
    TtsRenderError,
    read_meminfo,
)
from chime_deck.tts.engines import SynthesisTiming
from chime_deck.tts.ha_tts import (
    AVAILABILITY_TIMEOUT_S,
    PRESS_MARGIN_S,
    TTS_RENDER_TOTAL_TIMEOUT_S,
)
from chime_deck.tts.piper import (
    DEFAULT_PIPER_MIN_RAM_MB,
    MAX_CONCURRENT_RENDERS,
    MAX_CONSECUTIVE_TIMEOUTS,
    PIPER_EXTRA_REASON,
    PIPER_MIN_RAM_SETTING,
    VOICE_ID_PATTERN,
    ModuleFinder,
    PiperVoiceFactory,
)

pytestmark = pytest.mark.asyncio

VOICE: Final = "en_GB-alan-low"
"""Voice id that matches :data:`VOICE_ID_PATTERN` and needs no real model."""

OTHER_VOICE: Final = "en_GB-cori-medium"
"""Second voice id, used to prove a changed voice reloads the model."""

SAMPLE_RATE_HZ: Final = 22050
FRAMES: Final = 6615
"""6615 frames at 22.05 kHz: 0.3 s, comfortably above the audible floor."""

SYNTHESIS_S: Final = 0.01
"""Time the fake voice spends 'synthesising', so the measurement is never 0 ms."""

BLOCK_CAP_S: Final = 5.0
"""Longest the fake voice waits on its event, so no thread can outlive the suite."""

TIMEOUT_S: Final = 0.05
"""Short render deadline used by the timeout tests."""

PLENTY_MB: Final = 4096
TOO_LITTLE_MB: Final = 900


def _write_meminfo(path: Path, total_mb: int, available_mb: int) -> Path:
    """Write a meminfo file the way Linux does, in kB.

    :param path: file to write.
    :param total_mb: value of ``MemTotal`` in megabytes.
    :param available_mb: value of ``MemAvailable`` in megabytes.
    """
    path.write_text(
        f"MemTotal:       {total_mb * 1024} kB\n"
        f"MemFree:        {available_mb * 1024} kB\n"
        f"MemAvailable:   {available_mb * 1024} kB\n",
        encoding="utf-8",
    )
    return path


class FakeVoice:
    """An instrumented stand-in for a loaded piper voice."""

    def __init__(self, release: threading.Event | None = None) -> None:
        """Create the fake voice.

        :param release: event that holds :meth:`synthesize_wav` until it is set,
            used to prove renders serialise instead of overlapping.
        """
        self.release = release
        self.calls: list[tuple[float, float]] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def synthesize_wav(self, text: str, wav_file: wave.Wave_write) -> None:
        """Write a real 0.3 s WAV and record the overlap.

        :param text: phrase being spoken; not inspected.
        :param wav_file: open WAV writer, exactly what piper is given.
        """
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        entered = time.monotonic()
        try:
            if self.release is not None:
                self.release.wait(BLOCK_CAP_S)
            time.sleep(SYNTHESIS_S)
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(SAMPLE_RATE_HZ)
            wav_file.writeframes(b"\x00\x00" * FRAMES)
        finally:
            with self._lock:
                self.active -= 1
                self.calls.append((entered, time.monotonic()))


class FakePiperModule(types.ModuleType):
    """A real :class:`types.ModuleType` typed to carry the piper entry point."""

    PiperVoice: type[PiperVoiceFactory]


def _fake_module(voice: FakeVoice, loaded: list[Path]) -> FakePiperModule:
    """Build a ``piper`` module with the verified 1.8.0 ``PiperVoice`` shape.

    :param voice: voice returned by every ``load`` call.
    :param loaded: list that records each loaded model path.
    """
    module = FakePiperModule("piper")

    class FakePiperVoice:
        """The one entry point the engine uses: ``PiperVoice.load``."""

        @staticmethod
        def load(model_path: Path) -> PiperVoicePort:
            """Record the model path and return the instrumented voice.

            :param model_path: ``.onnx`` model the engine asked for.
            """
            loaded.append(model_path)
            return voice

    module.PiperVoice = FakePiperVoice
    return module


class FakeSettings:
    """A settings reader that answers from a dict, like the real store."""

    def __init__(self, values: dict[str, object] | None = None) -> None:
        """Create the reader.

        :param values: overrides; anything else answers its registry default.
        """
        self.values: dict[str, object] = dict(values or {})
        self.reads: list[str] = []

    async def get_setting(self, key: str) -> object:
        """Return the override for *key*, or its registry default.

        :param key: registered setting key.
        """
        self.reads.append(key)
        if key in self.values:
            return self.values[key]
        return SETTINGS[key].default


class Harness:
    """Everything one test needs, and a guarantee that its threads are closed."""

    def __init__(self, meminfo_path: Path, voice: FakeVoice) -> None:
        """Create the harness.

        :param meminfo_path: meminfo file the probe will read.
        :param voice: fake voice every loaded model returns.
        """
        self.meminfo_path = meminfo_path
        self.voice = voice
        self.settings = FakeSettings()
        self.voices_dir = meminfo_path.parent / "voices"
        self.voices_dir.mkdir(parents=True, exist_ok=True)
        self.loaded: list[Path] = []
        self.imports: list[str] = []
        self.finds: list[str] = []
        self.found: object = object()
        self.module: FakePiperModule = _fake_module(voice, self.loaded)
        self._probes: list[PiperProbe] = []
        self._engines: list[PiperEngine] = []

    def finder(self, name: str) -> object | None:
        """Report every module as installed, recording the question.

        :param name: module name the probe looked for.
        """
        self.finds.append(name)
        return self.found

    def absent_finder(self, name: str) -> object | None:
        """Report no module at all: a machine without the optional extra.

        :param name: module name the probe looked for.
        """
        self.finds.append(name)
        return None

    def importer(self, name: str) -> ModuleType:
        """Record and answer one import request with the fake module.

        :param name: module name the probe asked for.
        """
        self.imports.append(name)
        return self.module

    def failing_importer(self, error: BaseException, *, found: bool = True) -> PiperProbe:
        """Return a probe whose import raises *error* instead of succeeding.

        :param error: exception the importer raises.
        :param found: whether the finder reports the package on disk; a missing
            extra must be injected explicitly, never read from the host.
        """

        def importer(name: str) -> ModuleType:
            self.imports.append(name)
            raise error

        finder: ModuleFinder = self.finder if found else self.absent_finder
        probe = PiperProbe(
            self.settings,
            importer=importer,
            finder=finder,
            meminfo_path=self.meminfo_path,
        )
        self._probes.append(probe)
        return probe

    def probe(self, *, found: bool = True) -> PiperProbe:
        """Return a probe over the fake module and the fixture meminfo.

        :param found: whether the finder reports the package on disk; the
            default keeps every other test independent of this host.
        """
        finder: ModuleFinder = self.finder if found else self.absent_finder
        probe = PiperProbe(
            self.settings,
            importer=self.importer,
            finder=finder,
            meminfo_path=self.meminfo_path,
        )
        self._probes.append(probe)
        return probe

    def engine(
        self,
        probe: PiperProbe | None = None,
        *,
        render_timeout_s: float = TTS_RENDER_TOTAL_TIMEOUT_S,
    ) -> PiperEngine:
        """Return an engine over *probe* (or a fresh one) and the voices directory.

        :param probe: probe to share; ``None`` builds one.
        :param render_timeout_s: render deadline in seconds.
        """
        engine = PiperEngine(
            probe if probe is not None else self.probe(),
            voices_dir=self.voices_dir,
            render_timeout_s=render_timeout_s,
        )
        self._engines.append(engine)
        return engine

    def install_voice(self, voice: str = VOICE) -> Path:
        """Create the model and config files a downloaded voice would have.

        :param voice: voice id to install.
        """
        model = self.voices_dir / f"{voice}.onnx"
        model.write_bytes(b"\x00")
        (self.voices_dir / f"{voice}.onnx.json").write_text("{}", encoding="utf-8")
        return model

    def model_path(self, voice: str) -> Path:
        """Return the model path the engine will load for *voice*.

        :param voice: voice id.
        """
        return self.voices_dir / f"{voice}.onnx"

    def hidden(self) -> list[str]:
        """Return the hidden files left in the voices directory."""
        return sorted(entry.name for entry in self.voices_dir.iterdir() if entry.name[0] == ".")

    def close(self) -> None:
        """Close every engine and probe, and release any held voice.

        The release comes first so a voice blocked on its event returns before
        its executor is shut down; :data:`BLOCK_CAP_S` is the backstop if that
        ever stops being enough.
        """
        if self.voice.release is not None:
            self.voice.release.set()
        for engine in reversed(self._engines):
            engine.close()
        for probe in reversed(self._probes):
            probe.close()


class HarnessFactory(Protocol):
    """The ``build`` fixture: one :class:`Harness` per call, closed at teardown."""

    def __call__(
        self,
        *,
        total_mb: int = PLENTY_MB,
        available_mb: int = PLENTY_MB,
        blocked: bool = False,
        register: bool = True,
        meminfo_path: Path | None = None,
    ) -> Harness:
        """Build one harness.

        :param total_mb: ``MemTotal`` the fixture meminfo reports.
        :param available_mb: ``MemAvailable`` the fixture meminfo reports.
        :param blocked: whether the fake voice waits on its release event.
        :param register: whether the fake module is registered in
            ``sys.modules``, which is what makes ``find_spec`` see it.
        :param meminfo_path: explicitly unreadable meminfo path, for the
            fail-open test.
        """
        ...


@pytest.fixture
def build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[HarnessFactory]:
    """Return the harness factory and close everything it built afterwards.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: used to register (and later unregister) the fake module.
    """
    built: list[Harness] = []
    counter = {"n": 0}

    def build(
        *,
        total_mb: int = PLENTY_MB,
        available_mb: int = PLENTY_MB,
        blocked: bool = False,
        register: bool = True,
        meminfo_path: Path | None = None,
    ) -> Harness:
        counter["n"] += 1
        path = meminfo_path
        if path is None:
            path = _write_meminfo(
                tmp_path / f"meminfo-{counter['n']}", total_mb=total_mb, available_mb=available_mb
            )
        harness = Harness(path, FakeVoice(threading.Event() if blocked else None))
        if register:
            harness.module.__spec__ = importlib.machinery.ModuleSpec("piper", loader=None)
            monkeypatch.setitem(sys.modules, "piper", harness.module)
        built.append(harness)
        return harness

    try:
        yield build
    finally:
        for harness in reversed(built):
            harness.close()


async def test_prime_without_the_extra_pays_no_thread_and_no_import(
    build: HarnessFactory,
) -> None:
    """A piper-free install must not start a thread or attempt a doomed import.

    :param build: harness factory.
    """
    harness = build()
    probe = harness.probe(found=False)
    before = sorted(thread.name for thread in threading.enumerate())

    assert await probe.prime() is False

    after = sorted(thread.name for thread in threading.enumerate())
    assert harness.imports == []
    assert harness.finds == ["piper"]
    assert [name for name in after if name.startswith("piper-probe")] == [
        name for name in before if name.startswith("piper-probe")
    ]


async def test_available_refuses_the_ram_when_mem_total_is_below_the_threshold(
    build: HarnessFactory,
) -> None:
    """MemTotal under the D22 threshold is a filtered, actionable refusal.

    :param build: harness factory.
    """
    harness = build(total_mb=TOO_LITTLE_MB, available_mb=300)
    harness.install_voice()
    probe = harness.probe()
    await probe.prime()

    availability = await probe.availability(VOICE, harness.voices_dir)

    assert availability.ok is False
    reason = availability.reason or ""
    assert str(TOO_LITTLE_MB) in reason
    assert str(DEFAULT_PIPER_MIN_RAM_MB) in reason
    assert PIPER_MIN_RAM_SETTING in reason
    assert "ha_tts" in reason
    assert "espeak-ng" in reason
    assert "low" in reason


async def test_available_names_the_installed_extra_when_it_is_absent(
    build: HarnessFactory,
) -> None:
    """An install without the extra is told the exact command that fixes it.

    :param build: harness factory.
    """
    harness = build()
    harness.install_voice()
    probe = harness.failing_importer(ModuleNotFoundError("No module named 'piper'"), found=False)
    assert await probe.prime() is False

    availability = await probe.availability(VOICE, harness.voices_dir)

    assert availability.ok is False
    assert availability.reason == PIPER_EXTRA_REASON
    assert "`uv sync --extra piper`" in (availability.reason or "")


async def test_available_names_the_missing_voice_and_its_config(
    build: HarnessFactory,
) -> None:
    """An installed extra without the voice names the file and the fix.

    :param build: harness factory.
    """
    harness = build()
    probe = harness.probe()
    await probe.prime()

    availability = await probe.availability(VOICE, harness.voices_dir)

    assert availability.ok is False
    reason = availability.reason or ""
    assert VOICE in reason
    assert str(harness.model_path(VOICE)) in reason
    assert "Settings → Speech" in reason


async def test_available_when_the_extra_the_ram_and_the_voice_all_pass(
    build: HarnessFactory,
) -> None:
    """All three gates passing is available, with no reason.

    :param build: harness factory.
    """
    harness = build()
    harness.install_voice()
    probe = harness.probe()
    assert await probe.prime() is True

    assert await probe.availability(VOICE, harness.voices_dir) == Availability(True)


async def test_an_unreadable_meminfo_fails_open_with_one_warning(
    build: HarnessFactory, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A container without /proc still speaks, and is told once, not per press.

    :param build: harness factory.
    :param caplog: log capture fixture.
    :param tmp_path: pytest temporary directory.
    """
    harness = build(meminfo_path=tmp_path / "no-such-meminfo")
    harness.install_voice()
    probe = harness.probe()
    await probe.prime()

    with caplog.at_level(logging.WARNING):
        first = await probe.memory()
        second = await probe.memory()
        availability = await probe.availability(VOICE, harness.voices_dir)

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "meminfo" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert first.ok is True
    assert first.total_mb is None
    assert first.reason is None
    assert second.ok is True
    assert availability.ok is True


async def test_available_never_imports_piper_and_prime_does(
    build: HarnessFactory,
) -> None:
    """Availability uses ``find_spec`` only; the import happens on ``prime()``.

    The claim is testable everywhere because of the injection, not because of the
    ambient environment: another module may already have imported ``piper``, so
    the assertion is that ``available()`` added nothing to ``sys.modules`` and
    never called the injected importer.

    :param build: harness factory.
    """
    harness = build(register=False)
    harness.install_voice()
    probe = harness.probe()

    before = set(sys.modules)
    verdict = await probe.availability(VOICE, harness.voices_dir)

    assert verdict.ok is False
    assert "piper" not in set(sys.modules) - before
    assert harness.imports == []
    assert probe.module() is None

    assert await probe.prime() is True
    assert probe.module() is harness.module
    assert harness.imports == ["piper"]


async def test_the_importer_runs_once_across_repeated_primes_and_availability(
    build: HarnessFactory,
) -> None:
    """Three ``prime()`` calls and an availability check cost one import.

    :param build: harness factory.
    """
    harness = build()
    harness.install_voice()
    probe = harness.probe()

    assert await probe.prime() is True
    assert await probe.prime() is True
    assert await probe.prime() is True
    assert await probe.availability(VOICE, harness.voices_dir) == Availability(True)

    assert harness.imports == ["piper"]


async def test_a_second_probe_imports_independently(build: HarnessFactory) -> None:
    """Each probe owns its import state; one probe's success is not another's.

    :param build: harness factory.
    """
    harness = build()
    harness.install_voice()
    first = harness.probe()
    second = harness.probe()

    assert await first.prime() is True
    assert second.module() is None
    assert await second.prime() is True

    assert harness.imports == ["piper", "piper"]
    assert second.module() is harness.module


async def test_render_writes_a_playable_wav_and_records_the_time(
    build: HarnessFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """A render publishes a real 16-bit PCM WAV at exactly the destination.

    :param build: harness factory.
    :param caplog: log capture fixture.
    """
    harness = build()
    harness.install_voice()
    engine = harness.engine()
    await engine.warmup()
    destination = harness.voices_dir / "clip.wav"
    assert isinstance(engine, SynthesisTiming)

    with caplog.at_level(logging.INFO):
        result = await engine.render("Ding dong", TtsOptions(voice=VOICE), destination=destination)

    assert result == destination
    with wave.open(str(destination), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == SAMPLE_RATE_HZ
        assert reader.getnframes() == FRAMES
    assert harness.hidden() == []
    measured = engine.last_synthesis_ms
    assert isinstance(measured, int)
    assert measured > 0
    assert any(
        "synthesised" in record.getMessage() and record.levelno == logging.INFO
        for record in caplog.records
    )
    assert harness.loaded == [harness.model_path(VOICE)]


async def test_a_changed_voice_loads_the_model_again(build: HarnessFactory) -> None:
    """Two voices mean two loads, and a repeat of the current voice does not reload.

    :param build: harness factory.
    """
    harness = build()
    engine = harness.engine()
    await engine.warmup()
    destination = harness.voices_dir / "clip.wav"

    await engine.render("one", TtsOptions(voice=VOICE), destination=destination)
    await engine.render("two", TtsOptions(voice=OTHER_VOICE), destination=destination)
    await engine.render("three", TtsOptions(voice=OTHER_VOICE), destination=destination)

    assert harness.loaded == [harness.model_path(VOICE), harness.model_path(OTHER_VOICE)]


async def test_the_options_voice_wins_over_the_stored_setting(build: HarnessFactory) -> None:
    """``options.voice`` is what the cache key names, so it is what gets loaded.

    :param build: harness factory.
    """
    harness = build()
    harness.settings.values["tts.piper.voice"] = OTHER_VOICE
    engine = harness.engine()
    await engine.warmup()

    await engine.render("one", TtsOptions(voice=VOICE), destination=harness.voices_dir / "c.wav")

    assert harness.loaded == [harness.model_path(VOICE)]
    assert "tts.piper.voice" not in harness.settings.reads


async def test_two_overlapping_renders_serialise_on_one_worker(
    build: HarnessFactory,
) -> None:
    """The single worker means overlapping presses never share the voice.

    :param build: harness factory.
    """
    harness = build(blocked=True)
    engine = harness.engine()
    await engine.warmup()
    release = harness.voice.release
    assert release is not None

    first = asyncio.create_task(
        engine.render("one", TtsOptions(voice=VOICE), destination=harness.voices_dir / "a.wav")
    )
    for _ in range(200):
        if harness.voice.active == 1:
            break
        await asyncio.sleep(0.01)
    second = asyncio.create_task(
        engine.render("two", TtsOptions(voice=VOICE), destination=harness.voices_dir / "b.wav")
    )
    await asyncio.sleep(0)
    release.set()

    await asyncio.gather(first, second)

    assert harness.voice.max_active == 1
    assert len(harness.voice.calls) == 2
    assert harness.imports == ["piper"]
    assert harness.loaded == [harness.model_path(VOICE)]


async def test_a_render_timeout_counts_and_two_of_them_latch_the_engine(
    build: HarnessFactory,
) -> None:
    """A timeout fails the press; two in a row stop the engine for the run.

    :param build: harness factory.
    """
    harness = build(blocked=True)
    harness.install_voice()
    probe = harness.probe()
    assert await probe.prime() is True
    engine = harness.engine(probe, render_timeout_s=TIMEOUT_S)
    destination = harness.voices_dir / "clip.wav"

    with pytest.raises(TtsRenderError) as first:
        await engine.render("one", TtsOptions(voice=VOICE), destination=destination)
    assert "did not finish" in str(first.value)
    assert not destination.exists()
    assert harness.hidden() == []
    assert (await engine.available()).ok is True

    with pytest.raises(TtsRenderError):
        await engine.render("two", TtsOptions(voice=VOICE), destination=destination)

    latched = await engine.available()
    assert latched.ok is False
    assert "restart the service" in (latched.reason or "")
    assert str(MAX_CONSECUTIVE_TIMEOUTS) in (latched.reason or "")
    with pytest.raises(TtsRenderError) as third:
        await engine.render("three", TtsOptions(voice=VOICE), destination=destination)
    assert str(third.value) == latched.reason


async def test_a_cancelled_render_does_not_count_towards_the_latch(
    build: HarnessFactory,
) -> None:
    """An outer cancellation discards the WAV and leaves the latch untouched.

    The press-wide deadline (HSD-96k6) cancels a press that ran out of budget.
    The worker thread cannot be killed, but the temporary file must go, and the
    latch must keep counting only Piper's own timeouts: the press deadline is
    not one, so cancelling must not condemn a healthy engine.

    :param build: harness factory.
    """
    harness = build(blocked=True)
    harness.install_voice()
    engine = harness.engine()
    await engine.warmup()
    destination = harness.voices_dir / "clip.wav"

    task = asyncio.create_task(
        engine.render("one", TtsOptions(voice=VOICE), destination=destination)
    )
    for _ in range(200):
        if harness.voice.active == 1:
            break
        await asyncio.sleep(0.01)
    assert harness.voice.active == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert engine._consecutive_timeouts == 0
    assert (await engine.available()).ok is True
    assert not destination.exists()
    assert harness.hidden() == []


async def test_the_busy_cap_refuses_the_third_pending_render(build: HarnessFactory) -> None:
    """Only :data:`MAX_CONCURRENT_RENDERS` presses may queue behind the worker.

    :param build: harness factory.
    """
    harness = build(blocked=True)
    engine = harness.engine()
    await engine.warmup()
    release = harness.voice.release
    assert release is not None

    first = asyncio.create_task(
        engine.render("one", TtsOptions(voice=VOICE), destination=harness.voices_dir / "a.wav")
    )
    await asyncio.sleep(0)
    second = asyncio.create_task(
        engine.render("two", TtsOptions(voice=VOICE), destination=harness.voices_dir / "b.wav")
    )
    await asyncio.sleep(0)

    with pytest.raises(TtsRenderError) as excinfo:
        await engine.render(
            "three", TtsOptions(voice=VOICE), destination=harness.voices_dir / "c.wav"
        )
    assert "already rendering" in str(excinfo.value)
    assert str(MAX_CONCURRENT_RENDERS) in str(excinfo.value)

    release.set()
    await asyncio.gather(first, second)
    assert harness.voice.max_active == 1


async def test_warmup_primes_the_import_but_never_loads_the_voice(
    build: HarnessFactory,
) -> None:
    """Startup pays for the import, never for the ~380 MB RSS loaded voice.

    :param build: harness factory.
    """
    harness = build()
    engine = harness.engine()

    await engine.warmup()

    assert harness.imports == ["piper"]
    assert harness.loaded == []


async def test_warmup_logs_a_raising_prime_instead_of_raising(
    build: HarnessFactory, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken probe must not stop the doorbell from starting.

    :param build: harness factory.
    :param caplog: log capture fixture.
    :param monkeypatch: used to make ``prime`` raise.
    """
    harness = build()
    probe = harness.probe()

    async def raising_prime() -> bool:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(probe, "prime", raising_prime)
    engine = harness.engine(probe)

    with caplog.at_level(logging.WARNING):
        await engine.warmup()

    assert any(
        "could not warm up the piper engine" in record.getMessage() for record in caplog.records
    )
    assert harness.loaded == []


async def test_read_meminfo_parses_kilobytes_and_fails_soft(tmp_path: Path) -> None:
    """The meminfo reader answers ``(None, None)`` for anything unusable.

    :param tmp_path: pytest temporary directory.
    """
    good = _write_meminfo(tmp_path / "meminfo", total_mb=PLENTY_MB, available_mb=1234)
    assert read_meminfo(good) == (PLENTY_MB, 1234)
    assert read_meminfo(tmp_path / "absent") == (None, None)
    partial = tmp_path / "partial"
    partial.write_text("MemTotal:       4096000 kB\n", encoding="utf-8")
    assert read_meminfo(partial) == (None, None)


async def test_the_seam_budget_bounds_one_piper_engine() -> None:
    """The nested budgets fit the press seam, and piper sits in the D24 chain.

    ``FALLBACK_CHAIN``'s own docstring carries the erratum that design 04 §6.5
    omits ``piper``.
    """
    assert (
        AVAILABILITY_TIMEOUT_S + TEMPLATE_TIMEOUT_S + TTS_RENDER_TOTAL_TIMEOUT_S + PRESS_MARGIN_S
        < DEFAULT_SEAM_TIMEOUT_S
    )
    assert FALLBACK_CHAIN == ("remote_tts", "ha_tts", "piper", "espeak_ng")
    assert PiperEngine.name in FALLBACK_CHAIN
    assert PiperEngine.name == "piper"
    assert VOICE_ID_PATTERN.fullmatch(VOICE) is not None
