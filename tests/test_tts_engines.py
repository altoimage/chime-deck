"""espeak-ng engine: availability, invocation building and render validation.

Fully offline: every test injects a fake ``ProcessSpawner``, so no real
espeak-ng, subprocess or audio device is needed and the suite passes whether or
not the binary is installed on the host.

Owning ticket: HSD-bw8h.
"""

from __future__ import annotations

import asyncio
import wave
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from chime_deck.mqtt.bridge import DEFAULT_SEAM_TIMEOUT_S
from chime_deck.press import PRESS_DEADLINE_S, PRESS_FINISH_MARGIN_S
from chime_deck.tts import (
    Availability,
    EspeakNgEngine,
    NullTtsEngine,
    ProcessSpawner,
    SpawnedProcess,
    TtsOptions,
    TtsRenderError,
)
from chime_deck.tts.engines import _KILL_GRACE_S as ESPEAK_KILL_GRACE_S
from chime_deck.tts.engines import _TIMEOUT_S as ESPEAK_TIMEOUT_S
from chime_deck.tts.ha_tts import (
    AVAILABILITY_TIMEOUT_S,
    PRESS_MARGIN_S,
    TTS_RENDER_TOTAL_TIMEOUT_S,
)
from chime_deck.tts.remote_tts import REMOTE_AVAILABILITY_TIMEOUT_S
from chime_deck.tts.speech import (
    MIN_RETRY_RENDER_S,
    PROBE_TIMEOUT_S,
    RETRY_MIN_BUDGET_S,
)
from chime_deck.tts.templating import TEMPLATE_TIMEOUT_S

pytestmark = pytest.mark.asyncio

_FOREVER_S = 3600.0
"""A fake communicate that never returns on its own; the engine must time it out."""

_HANGING_TIMEOUT_S = 0.05
"""Engine deadline used by the timeout tests."""

_WAIT_S = 1.0
"""Bound for one in-test wait, so a hung await fails instead of stalling the suite."""

_FIXED_TEXT = "Testing one two three."
_HOSTILE_TEXT = 'He said "hi"; rm -rf / && $(id) | tee x > y\nsecond line\nthird line'


def _write_wav(path: Path, frames: int) -> None:
    """Write a 22.05 kHz mono 16-bit silent WAV with *frames* frames.

    :param path: output path.
    :param frames: number of sample frames, which decide the duration.
    """
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(22050)
        writer.writeframes(b"\x00\x00" * frames)


class FakeProcess:
    """A controllable stand-in for :class:`asyncio.subprocess.Process`."""

    argv: tuple[str, ...] = ()
    stdin: bytes | None = None
    killed = False

    def __init__(
        self,
        *,
        fixture: Callable[[Path], None] | None = None,
        returncode: int = 0,
        stderr: bytes = b"",
        hangs: bool = False,
        kill_error: Exception | None = None,
        entered: asyncio.Event | None = None,
    ) -> None:
        """Create the fake process.

        :param fixture: writes the engine's output file at the ``-w`` path.
        :param returncode: status reported after the process exits.
        :param stderr: bytes returned from the stderr pipe.
        :param hangs: whether :meth:`communicate` never returns by itself.
        :param kill_error: exception raised by :meth:`kill`, for the race tests.
        :param entered: event set as soon as :meth:`communicate` is reached, so a
            test can cancel the render while the engine is really waiting.
        """
        self._fixture = fixture
        self._returncode = returncode
        self._stderr = stderr
        self._hangs = hangs
        self._kill_error = kill_error
        self._entered = entered

    @property
    def returncode(self) -> int | None:
        """Exit status, never ``None`` once :meth:`communicate` has returned."""
        return self._returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes | None]:
        """Record stdin, optionally write the fixture and return stderr.

        :param input: complete stdin payload written by the engine.
        """
        self.stdin = input
        if self._entered is not None:
            self._entered.set()
        if self._hangs:
            await asyncio.sleep(_FOREVER_S)
        if self._fixture is not None:
            self._fixture(self.output_path())
        return b"", self._stderr

    async def wait(self) -> int:
        """Return the configured exit status immediately."""
        return self._returncode

    def kill(self) -> None:
        """Record the kill, or raise the configured error.

        :raises Exception: the configured ``kill_error``, when one was given.
        """
        if self._kill_error is not None:
            raise self._kill_error
        self.killed = True

    def output_path(self) -> Path:
        """Return the path following ``-w`` in the recorded argv."""
        index = self.argv.index("-w")
        return Path(self.argv[index + 1])


class FakeSpawner:
    """A :class:`ProcessSpawner` that records argv and returns one fake process."""

    def __init__(self, process: FakeProcess | None = None) -> None:
        """Create the spawner.

        :param process: process returned for every spawn; a fresh one by default.
        """
        self.process = process if process is not None else FakeProcess()
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, argv: Sequence[str]) -> SpawnedProcess:
        """Record *argv* and return the fake process.

        :param argv: complete command line the engine built.
        """
        self.calls.append(tuple(argv))
        self.process.argv = tuple(argv)
        return self.process


_SPAWNER: ProcessSpawner = FakeSpawner()
"""Pin the spawn seam conformance under mypy."""

_PROCESS: SpawnedProcess = FakeProcess()
"""Pin the process seam conformance under mypy."""


def _engine(spawner: FakeSpawner, *, timeout_s: float = ESPEAK_TIMEOUT_S) -> EspeakNgEngine:
    """Build an engine with an injected spawner and a fake ``which``.

    :param spawner: fake process factory.
    :param timeout_s: render deadline in seconds.
    """
    return EspeakNgEngine(
        which=lambda _binary: "/usr/bin/espeak-ng",
        spawner=spawner,
        timeout_s=timeout_s,
    )


def _hidden(directory: Path) -> list[str]:
    """Return the names of hidden entries in *directory*.

    :param directory: directory to inspect.
    """
    return sorted(path.name for path in directory.iterdir() if path.name.startswith("."))


async def test_available_reports_an_installed_binary() -> None:
    """A binary found by ``which`` is available with no reason."""
    engine = EspeakNgEngine(which=lambda _binary: "/usr/bin/espeak-ng")

    availability = await engine.available()

    assert availability.ok is True
    assert availability.reason is None


async def test_available_names_the_install_fix_when_missing() -> None:
    """A missing binary is unavailable and the reason says how to fix it."""
    engine = EspeakNgEngine(which=lambda _binary: None)

    availability = await engine.available()

    assert availability.ok is False
    assert availability.reason is not None
    assert "not installed" in availability.reason
    assert "apt install espeak-ng" in availability.reason


async def test_hostile_text_stays_on_stdin_and_out_of_argv(tmp_path: Path) -> None:
    """Shell metacharacters and newlines never reach the command line."""
    engine = EspeakNgEngine()
    destination = tmp_path / "clip.wav"
    options = TtsOptions(voice="en-gb", speed=160)

    invocation = engine.build_invocation(_HOSTILE_TEXT, options, destination)

    assert invocation.stdin == _HOSTILE_TEXT.encode("utf-8")
    assert invocation.argv == (
        "espeak-ng",
        "-v",
        "en-gb",
        "-s",
        "160",
        "-w",
        str(destination),
        "--stdin",
    )
    joined = "\x00".join(invocation.argv)
    assert _HOSTILE_TEXT not in joined
    for fragment in ("rm -rf", "$(id)", "second line", ";"):
        assert fragment not in joined


@pytest.mark.parametrize(("speed", "expected"), [(160, "160"), (200, "200"), (0, "0")])
async def test_build_invocation_passes_speed_verbatim(
    tmp_path: Path, speed: int, expected: str
) -> None:
    """The speed is passed exactly as stored; the resolver owns the default.

    :param tmp_path: pytest temporary directory.
    :param speed: configured speed value.
    :param expected: argv token expected after ``-s``.
    """
    engine = EspeakNgEngine()

    invocation = engine.build_invocation(
        _FIXED_TEXT, TtsOptions(voice="en-gb", speed=speed), tmp_path / "clip.wav"
    )

    assert invocation.argv[invocation.argv.index("-s") + 1] == expected


async def test_render_writes_the_wav_and_cleans_up(tmp_path: Path) -> None:
    """A successful render returns the destination and leaves no temporary file.

    :param tmp_path: pytest temporary directory.
    """
    spawner = FakeSpawner(FakeProcess(fixture=lambda path: _write_wav(path, 4410)))
    engine = _engine(spawner)
    destination = tmp_path / "nested" / "out.wav"

    result = await engine.render(_FIXED_TEXT, TtsOptions(voice="en-gb"), destination=destination)

    assert result == destination
    assert destination.is_file()
    with wave.open(str(destination), "rb") as reader:
        assert reader.getnframes() == 4410
    assert _hidden(destination.parent) == []
    assert spawner.process.stdin == _FIXED_TEXT.encode("utf-8")


async def test_render_reports_engine_stderr_on_failure(tmp_path: Path) -> None:
    """A non-zero exit becomes a typed error naming the status and stderr.

    :param tmp_path: pytest temporary directory.
    """
    spawner = FakeSpawner(
        FakeProcess(
            returncode=1,
            stderr=b"Error: The specified espeak-ng voice does not exist.",
        )
    )
    engine = _engine(spawner)
    destination = tmp_path / "out.wav"

    with pytest.raises(TtsRenderError) as excinfo:
        await engine.render(_FIXED_TEXT, TtsOptions(voice="nope"), destination=destination)

    message = str(excinfo.value)
    assert "status 1" in message
    assert "does not exist" in message
    assert not destination.exists()
    assert _hidden(tmp_path) == []


async def test_render_rejects_an_exit_zero_without_a_file(tmp_path: Path) -> None:
    """espeak-ng exits 0 for an unwritable path; that is still a failed render.

    :param tmp_path: pytest temporary directory.
    """
    spawner = FakeSpawner(FakeProcess(stderr=b"Can't write to: '/nope/x.wav'"))
    engine = _engine(spawner)
    destination = tmp_path / "out.wav"

    with pytest.raises(TtsRenderError) as excinfo:
        await engine.render(_FIXED_TEXT, TtsOptions(voice="en-gb"), destination=destination)

    message = str(excinfo.value)
    assert "produced no output" in message
    assert "Can't write to" in message
    assert not destination.exists()
    assert _hidden(tmp_path) == []


@pytest.mark.parametrize("frames", [176, 0])
async def test_render_rejects_inaudible_audio(tmp_path: Path, frames: int) -> None:
    """Exit 0 is not enough: 8 ms or 0 frames of silence is a failed render.

    :param tmp_path: pytest temporary directory.
    :param frames: frame count of the fixture; 176 is 8 ms, 0 is a bare header.
    """
    spawner = FakeSpawner(FakeProcess(fixture=lambda path: _write_wav(path, frames)))
    engine = _engine(spawner)
    destination = tmp_path / "out.wav"

    with pytest.raises(TtsRenderError) as excinfo:
        await engine.render(_FIXED_TEXT, TtsOptions(voice="en-gb"), destination=destination)

    assert "no audible audio" in str(excinfo.value)
    assert not destination.exists()
    assert _hidden(tmp_path) == []


async def test_render_kills_a_hung_engine(tmp_path: Path) -> None:
    """A render that exceeds the deadline is killed and reported.

    :param tmp_path: pytest temporary directory.
    """
    spawner = FakeSpawner(FakeProcess(hangs=True))
    engine = _engine(spawner, timeout_s=_HANGING_TIMEOUT_S)
    destination = tmp_path / "out.wav"

    with pytest.raises(TtsRenderError) as excinfo:
        await engine.render(_FIXED_TEXT, TtsOptions(voice="en-gb"), destination=destination)

    assert f"did not finish within {_HANGING_TIMEOUT_S:g} s" in str(excinfo.value)
    assert spawner.process.killed is True
    assert not destination.exists()
    assert _hidden(tmp_path) == []


async def test_render_tolerates_a_process_that_already_exited(tmp_path: Path) -> None:
    """A kill that raises ``ProcessLookupError`` still yields the typed error.

    :param tmp_path: pytest temporary directory.
    """
    spawner = FakeSpawner(FakeProcess(hangs=True, kill_error=ProcessLookupError()))
    engine = _engine(spawner, timeout_s=_HANGING_TIMEOUT_S)
    destination = tmp_path / "out.wav"

    with pytest.raises(TtsRenderError) as excinfo:
        await engine.render(_FIXED_TEXT, TtsOptions(voice="en-gb"), destination=destination)

    assert f"did not finish within {_HANGING_TIMEOUT_S:g} s" in str(excinfo.value)
    assert spawner.process.killed is False
    assert not destination.exists()


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
async def test_blank_text_fails_before_spawning(tmp_path: Path, text: str) -> None:
    """Whitespace-only text is rejected without forking espeak-ng.

    :param tmp_path: pytest temporary directory.
    :param text: blank phrase to reject.
    """
    spawner = FakeSpawner()
    engine = _engine(spawner)

    with pytest.raises(TtsRenderError) as excinfo:
        await engine.render(text, TtsOptions(), destination=tmp_path / "out.wav")

    assert "no text to speak" in str(excinfo.value)
    assert spawner.calls == []


async def test_a_cancelled_render_kills_the_engine_process(tmp_path: Path) -> None:
    """A cancellation from outside kills espeak-ng and discards the temp file.

    The press-wide deadline (HSD-96k6) cancels a press that ran out of budget,
    and the MQTT seam cancels one that outlived it. Either way the subprocess
    must not outlive the cancellation, so every exit from the wait — not only
    the deadline expiry — kills and discards.

    :param tmp_path: pytest temporary directory.
    """
    entered = asyncio.Event()
    spawner = FakeSpawner(FakeProcess(hangs=True, entered=entered))
    engine = _engine(spawner)
    destination = tmp_path / "out.wav"

    task = asyncio.create_task(
        engine.render(_FIXED_TEXT, TtsOptions(voice="en-gb"), destination=destination)
    )
    try:
        await asyncio.wait_for(entered.wait(), _WAIT_S)
        # espeak-ng has started writing its hidden output file by now.
        spawner.process.output_path().write_bytes(b"RIFF")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()

    assert spawner.process.killed is True
    assert spawner.process.returncode is not None
    assert not destination.exists()
    assert _hidden(tmp_path) == []


async def test_null_engine_reports_disabled_and_refuses_to_render(tmp_path: Path) -> None:
    """The null engine never probes and never writes.

    :param tmp_path: pytest temporary directory.
    """
    engine = NullTtsEngine()

    assert engine.name == "none"
    availability = await engine.available()
    assert availability == Availability(False, "speech is disabled (tts.default_engine is 'none')")
    with pytest.raises(TtsRenderError) as excinfo:
        await engine.render(_FIXED_TEXT, TtsOptions(), destination=tmp_path / "out.wav")
    assert "disabled" in str(excinfo.value)
    await engine.warmup()


async def test_espeak_warmup_is_a_no_op() -> None:
    """Warming up espeak-ng starts no process and returns ``None``."""
    spawner = FakeSpawner()
    engine = _engine(spawner)

    await engine.warmup()
    assert spawner.calls == []


async def test_the_probe_then_espeak_press_fits_strictly_inside_the_seam() -> None:
    """A probe-then-espeak press cannot outlive the MQTT seam that wraps it.

    HSD-bw8h gave this engine its own 10 s render deadline when ``espeak_ng``
    was the only engine there was. HSD-npt0 then registered ``ha_tts``, whose
    1.0 s availability probe runs *before* a fall-through render, so the sum
    crossed the 10 s seam the MQTT bridge wraps around a whole press
    (:data:`~chime_deck.mqtt.bridge.DEFAULT_SEAM_TIMEOUT_S`). A seam expiry
    cancels the press before ``press.py`` can write a press log row, so the
    doorbell went silent with only a warning to explain it. HSD-4y6l closed
    that window by lowering the render budget and the kill grace; this test is
    what fails loudly if a later change re-opens it.

    HSD-96k6 then made the bound press-wide instead of per-engine: the chain
    case this docstring used to call uncovered is now covered by
    :data:`~chime_deck.press.PRESS_DEADLINE_S`, which stays a whole finish
    margin inside the seam and binds *before* the 9.5 s single-engine worst case,
    so a press fails with a recorded row instead of being cancelled without one.
    The behavioural proof — a retry only inside the remaining budget, a probe
    backstop above every engine's own probe budget, a render that outlives the
    deadline — lives in ``tests/test_tts_speech.py``; this test pins the
    arithmetic those behaviours depend on.
    """
    terms = {
        "availability probe": AVAILABILITY_TIMEOUT_S,
        "press-time template render": TEMPLATE_TIMEOUT_S,
        "espeak-ng render": ESPEAK_TIMEOUT_S,
        "kill grace": ESPEAK_KILL_GRACE_S,
        "publish margin": PRESS_MARGIN_S,
    }
    total = sum(terms.values())
    breakdown = ", ".join(f"{name} {value:g} s" for name, value in terms.items())

    assert total < DEFAULT_SEAM_TIMEOUT_S, (
        f"a probe-then-espeak press can take {total:g} s ({breakdown}), but the"
        f" MQTT seam cancels it after {DEFAULT_SEAM_TIMEOUT_S:g} s and then no"
        " press log row is written (HSD-4y6l)"
    )
    # The press-wide deadline, plus the seconds kept for writing the row, has to
    # fit inside the seam: otherwise the seam cancels the press first and there
    # is no row at all, which is the failure HSD-96k6 exists to remove.
    assert PRESS_DEADLINE_S + PRESS_FINISH_MARGIN_S <= DEFAULT_SEAM_TIMEOUT_S, (
        f"the press deadline of {PRESS_DEADLINE_S:g} s plus a finish margin of"
        f" {PRESS_FINISH_MARGIN_S:g} s must stay inside the {DEFAULT_SEAM_TIMEOUT_S:g} s"
        " MQTT seam (HSD-96k6)"
    )
    # ...and it has to bind *before* the single-engine worst case above, so a
    # press that runs out of time fails with a row instead of being cancelled.
    single_engine_worst_case = (
        AVAILABILITY_TIMEOUT_S + TEMPLATE_TIMEOUT_S + TTS_RENDER_TOTAL_TIMEOUT_S + PRESS_MARGIN_S
    )
    assert single_engine_worst_case > PRESS_DEADLINE_S, (
        f"the press deadline of {PRESS_DEADLINE_S:g} s must bind before the"
        f" {single_engine_worst_case:g} s single-engine worst case, or the press is"
        " cancelled without a press log row (HSD-96k6)"
    )
    # The resolver's probe backstop sits *above* every engine's own probe budget,
    # so it can never truncate a working probe: it exists for the engine that does
    # not bound itself.
    assert PROBE_TIMEOUT_S > AVAILABILITY_TIMEOUT_S
    assert PROBE_TIMEOUT_S > REMOTE_AVAILABILITY_TIMEOUT_S
    assert RETRY_MIN_BUDGET_S == PROBE_TIMEOUT_S + MIN_RETRY_RENDER_S
    # The sum is only meaningful if the engine really defaults to the constant
    # it is built from, so a hardcoded default cannot drift away from it.
    default_engine = EspeakNgEngine(which=lambda _binary: "/usr/bin/espeak-ng")
    assert default_engine._timeout_s == ESPEAK_TIMEOUT_S
