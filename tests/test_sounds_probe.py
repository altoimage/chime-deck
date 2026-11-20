"""``ffprobe`` duration probing: every failure degrades, nothing raises.

Every unit test injects a fake spawner, so no real process runs. The autouse
``no_real_ffprobe`` fixture makes the default lookup absent, and one
integration test opts back in with ``real_ffprobe`` and skips when the binary is
missing.

Owning ticket: HSD-cwvc.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

import pytest

from chime_deck.sounds.probe import (
    KILL_GRACE_S,
    ProcessSpawner,
    SpawnedProcess,
    probe_duration_ms,
)

_HANG_S = 3600.0
"""A fake ``communicate`` that never returns by itself; the probe must time it out."""


class FakeProcess:
    """A controllable stand-in for :class:`asyncio.subprocess.Process`."""

    def __init__(
        self,
        *,
        stdout: bytes = b"0.5\n",
        returncode: int = 0,
        hangs: bool = False,
        kill_error: Exception | None = None,
    ) -> None:
        """Create the fake process.

        :param stdout: bytes returned from the stdout pipe.
        :param returncode: status reported after the process exits.
        :param hangs: whether :meth:`communicate` never returns by itself.
        :param kill_error: exception raised by :meth:`kill`, for the race test.
        """
        self._stdout = stdout
        self._returncode = returncode
        self._hangs = hangs
        self._kill_error = kill_error
        self.killed = 0
        self.waited = False

    @property
    def returncode(self) -> int | None:
        """Exit status, never ``None`` once :meth:`communicate` has returned."""
        return self._returncode

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        """Return stdout and empty stderr, or hang when configured.

        :param input: ignored; ffprobe gets no stdin.
        """
        if self._hangs:
            await asyncio.sleep(_HANG_S)
        return self._stdout, b""

    async def wait(self) -> int:
        """Record the bounded grace wait and return the status."""
        self.waited = True
        return self._returncode

    def kill(self) -> None:
        """Record the kill, or raise the configured race error.

        :raises Exception: the configured ``kill_error``, when one was given.
        """
        self.killed += 1
        if self._kill_error is not None:
            raise self._kill_error


def _spawner(process: FakeProcess) -> ProcessSpawner:
    """Return a spawner that always hands back *process*.

    :param process: fake process to return.
    """

    async def spawn(*argv: str) -> SpawnedProcess:
        """Record the argv and return the fake process.

        :param argv: argv the probe built.
        """
        assert argv[0] == "/usr/bin/ffprobe"
        return process

    return spawn


def _resolver(_name: str) -> str | None:
    """Return a fake ffprobe path, as if the binary were installed."""
    return "/usr/bin/ffprobe"


def test_timeout_kills_once_and_leaves_duration_unknown(caplog: pytest.LogCaptureFixture) -> None:
    """A hanging probe is killed, reaped with a grace bound and logged once."""
    process = FakeProcess(hangs=True)
    caplog.set_level(logging.INFO, logger="chime_deck.sounds.probe")

    result = probe_duration_ms(
        Path("ding.wav"),
        timeout_s=0.01,
        which=_resolver,
        spawner=_spawner(process),
    )

    assert result is None
    assert process.killed == 1
    assert process.waited is True
    infos = [record for record in caplog.records if record.levelno == logging.INFO]
    assert len(infos) == 1
    assert "timed out" in infos[0].getMessage()


def test_kill_race_is_swallowed() -> None:
    """A process that vanished before the kill still yields an unknown duration."""
    process = FakeProcess(hangs=True, kill_error=ProcessLookupError())

    result = probe_duration_ms(
        Path("ding.wav"),
        timeout_s=0.01,
        which=_resolver,
        spawner=_spawner(process),
    )

    assert result is None
    assert process.waited is True


def test_nonzero_exit_is_unknown() -> None:
    """A failed ffprobe leaves the duration unknown."""
    process = FakeProcess(stdout=b"0.5\n", returncode=1)

    result = probe_duration_ms(Path("ding.wav"), which=_resolver, spawner=_spawner(process))

    assert result is None


@pytest.mark.parametrize("stdout", [b"N/A\n", b"fast\n", b""])
def test_unparseable_output_is_unknown(stdout: bytes) -> None:
    """Empty, ``N/A`` and non-numeric output all mean "unknown"."""
    process = FakeProcess(stdout=stdout)

    result = probe_duration_ms(Path("ding.wav"), which=_resolver, spawner=_spawner(process))

    assert result is None


def test_spawn_file_not_found_is_unknown() -> None:
    """A binary that disappears between lookup and spawn is not an error."""

    async def spawn(*argv: str) -> SpawnedProcess:
        """Fail as a missing executable does.

        :param argv: argv the probe built.
        """
        raise FileNotFoundError(argv[0])

    assert probe_duration_ms(Path("ding.wav"), which=_resolver, spawner=spawn) is None


def test_missing_binary_never_spawns() -> None:
    """A missing ffprobe is reported without starting any process."""
    spawn_calls: list[tuple[str, ...]] = []

    async def spawn(*argv: str) -> SpawnedProcess:
        """Record a spawn that should never happen.

        :param argv: argv the probe built.
        """
        spawn_calls.append(tuple(argv))
        return FakeProcess()

    result = probe_duration_ms(Path("ding.wav"), which=lambda _name: None, spawner=spawn)

    assert result is None
    assert spawn_calls == []


def test_autouse_fixture_hides_real_ffprobe() -> None:
    """The suite-wide fixture makes the default lookup absent."""
    assert probe_duration_ms(Path("ding.wav")) is None


def test_parses_seconds_into_milliseconds() -> None:
    """A valid duration is rounded to whole milliseconds."""
    process = FakeProcess(stdout=b"0.5239\n")

    assert probe_duration_ms(Path("ding.wav"), which=_resolver, spawner=_spawner(process)) == 524


def test_kill_grace_constant_is_bounded() -> None:
    """The grace period after a kill is short enough to keep a scan bounded."""
    assert 0 < KILL_GRACE_S <= 5.0


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("ffprobe") is None,
    reason="ffprobe is not installed; run ./scripts/test-integration.sh",
)
def test_probe_reads_a_real_tone(real_ffprobe: None, tmp_path: Path) -> None:
    """A real ffprobe reports roughly half a second for a generated tone.

    :param real_ffprobe: restores the real executable lookup.
    :param tmp_path: pytest temporary directory.
    """
    from audio_fixtures import generate_audio_fixtures

    fixtures = generate_audio_fixtures(tmp_path)
    duration = probe_duration_ms(fixtures["wav"])

    assert duration is not None
    assert 200 <= duration <= 2000
