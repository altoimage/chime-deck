"""One bounded, never-raising subprocess call: the offline seam for the mixer.

The hardware volume service has to run ``amixer`` and ``pactl``, and a test must
never execute either of them (AGENTS.md HARD RULE 4). The seam is therefore the
spawner: :class:`SpawnedProcess` and :data:`DEFAULT_SPAWN` mirror
:mod:`chime_deck.sounds.probe` and :mod:`chime_deck.tts.engines`, and a test
injects a recording fake instead of forking anything.

:func:`run_command` is the one place the timeout, kill and reap rules live
(HARD RULE 1). It never raises for a command problem — a missing binary, a
non-zero exit and a timeout are all just a :class:`CommandResult`. That is what
makes "fail soft at every step" trivial to honour in the caller: there is no
failure path to forget. Only :class:`asyncio.CancelledError` propagates, and the
child is killed and reaped first so a cancelled request cannot leak a process.

The default spawner is resolved at call time, so a suite-wide replacement (as
``tests/conftest.py`` does for ``ffprobe``) would reach every caller.

Owning ticket: cd-qul9.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Final, Protocol, TypeAlias, cast

__all__ = [
    "COMMAND_TIMEOUT_S",
    "DEFAULT_SPAWN",
    "KILL_GRACE_S",
    "CommandResult",
    "CommandSpawner",
    "SpawnedProcess",
    "run_command",
]

COMMAND_TIMEOUT_S: Final = 2.0
"""How long one mixer command may take before it is killed.

Matches :data:`~chime_deck.sounds.probe.FFPROBE_TIMEOUT_S` and
``mpv_backend._DEFAULT_COMMAND_TIMEOUT_S``: a mixer that has not answered in two
seconds is wedged, and the caller has an honest "did not answer" sentence to use.
"""

KILL_GRACE_S: Final = 2.0
"""How long a killed command may take to exit before it is abandoned."""

_NOT_INSTALLED_RETURNCODE: Final = 127
"""Exit status reported when the binary could not be started at all.

The shell's own "command not found" convention, so a caller can tell "there is no
``amixer`` on this host" (127) from "the mixer did not answer inside the bound"
(``None``) without sharing exception objects across the seam.
"""

_NOT_EXECUTABLE_RETURNCODE: Final = 126
"""Exit status reported when the binary exists but could not be executed."""


class SpawnedProcess(Protocol):
    """The subset of :class:`asyncio.subprocess.Process` one command uses."""

    @property
    def returncode(self) -> int | None:
        """Exit status, or ``None`` while the process is still running."""
        ...

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        """Feed *input* to stdin, close it, drain both pipes and wait.

        :param input: bytes written to stdin before it is closed, or ``None``.
        """
        ...

    def kill(self) -> None:
        """Send SIGKILL; safe to call more than once."""
        ...

    async def wait(self) -> int:
        """Wait for the process to exit and return its status."""
        ...


CommandSpawner: TypeAlias = Callable[[Sequence[str]], Awaitable[SpawnedProcess]]
"""Starts one process from an argv, without a shell; injectable for tests."""


async def _default_spawn(argv: Sequence[str]) -> SpawnedProcess:
    """Start one real process with both pipes captured and stdin closed.

    :param argv: complete argv; never passed through a shell.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return cast(SpawnedProcess, process)


DEFAULT_SPAWN: CommandSpawner = _default_spawn
"""Process factory used when the caller does not inject one."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Everything one bounded command produced, including why it produced nothing.

    ``returncode`` is ``None`` only when there is no exit status to report: the
    command did not finish inside the bound. A binary that could not be started
    reports :data:`_NOT_INSTALLED_RETURNCODE` (or ``126`` when it exists but is
    not executable) with the exception text in ``stderr``, so ``ok`` is ``False``
    in every failure case and the timeout stays distinguishable.
    """

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Return whether the command ran and exited successfully."""
        return self.returncode == 0


async def _kill_and_reap(process: SpawnedProcess) -> None:
    """Kill a process and await its exit, tolerating both races.

    A process may already have exited (``ProcessLookupError``) and a process may
    ignore SIGKILL for longer than the grace period (``TimeoutError``). Neither
    may escape, because the caller is already returning a failure result.

    :param process: process to kill and reap.
    """
    with suppress(ProcessLookupError):
        process.kill()
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), KILL_GRACE_S)


def _decode(data: bytes) -> str:
    """Decode captured output without ever failing on a stray byte.

    :param data: raw bytes captured from a pipe.
    """
    return data.decode("utf-8", errors="replace")


async def run_command(
    argv: Sequence[str],
    *,
    spawner: CommandSpawner | None = None,
    timeout: float = COMMAND_TIMEOUT_S,
) -> CommandResult:
    """Run *argv* under a bound and describe the outcome instead of raising it.

    Total by construction: a binary that is not installed, a command that exits
    non-zero and a command that outlasts *timeout* each yield a
    :class:`CommandResult` with ``ok`` false and an explanatory ``stderr``. A
    caller that only cares about success can therefore write ``if not result.ok``
    and never need a ``try``. The one exception that propagates is
    :class:`asyncio.CancelledError` — a shutdown is not a command failure — and
    the child is killed and reaped before it does.

    :param argv: complete argv; never passed through a shell.
    :param spawner: process factory; ``None`` uses :data:`DEFAULT_SPAWN` now, so
        a suite-wide replacement reaches this call.
    :param timeout: seconds the command may take before it is killed.
    :return: the exit status and both captured streams, or the reason there are
        none.
    """
    command = tuple(argv)
    spawn = spawner if spawner is not None else DEFAULT_SPAWN
    try:
        process = await spawn(command)
    except FileNotFoundError as exc:
        return CommandResult(command, _NOT_INSTALLED_RETURNCODE, "", str(exc))
    except OSError as exc:
        return CommandResult(command, _NOT_EXECUTABLE_RETURNCODE, "", str(exc))

    stdout = b""
    stderr = b""
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        await _kill_and_reap(process)
        return CommandResult(
            command,
            None,
            _decode(stdout),
            f"the command did not answer within {timeout:g} s",
        )
    except asyncio.CancelledError:
        await _kill_and_reap(process)
        raise
    return CommandResult(command, process.returncode, _decode(stdout), _decode(stderr))
