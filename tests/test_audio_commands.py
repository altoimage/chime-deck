"""``run_command``: one bounded subprocess call that never raises for a failure.

Every case here uses :class:`~audio_hardware_fakes.RecordingSpawner`, so no real
binary is ever started. The point of the module under test is that a missing
binary, a non-zero exit and a timeout all come back as a result rather than an
exception, leaving cancellation as the only failure that propagates — and even
that one reaps its child first.

Owning ticket: cd-qul9.
"""

from __future__ import annotations

import asyncio

import pytest
from audio_hardware_fakes import FakeCommand, RecordingSpawner
from mqtt_fakes import wait_until

from chime_deck.audio import COMMAND_TIMEOUT_S, CommandResult, run_command

pytestmark = pytest.mark.asyncio

_TIMEOUT_S = 0.05
"""Budget used by the timeout test: long enough to arm, far too short to sleep."""


async def test_the_argv_and_both_streams_are_returned_verbatim() -> None:
    """A successful command reports its argv, its output and a zero status."""
    spawner = RecordingSpawner(
        lambda _argv: FakeCommand(returncode=0, stdout="Simple mixer control 'Speaker',0\n")
    )

    result = await run_command(("amixer", "scontrols"), spawner=spawner)

    assert isinstance(result, CommandResult)
    assert result.argv == ("amixer", "scontrols")
    assert result.returncode == 0
    assert result.ok is True
    assert result.stdout == "Simple mixer control 'Speaker',0\n"
    assert result.stderr == ""
    assert spawner.calls == [("amixer", "scontrols")]


async def test_a_non_zero_exit_is_a_result_not_an_exception() -> None:
    """A command that fails reports its status and keeps stderr's own words."""
    spawner = RecordingSpawner(
        lambda _argv: FakeCommand(returncode=3, stderr="amixer: Unable to find simple control")
    )

    result = await run_command(("amixer", "sget", "Speaker"), spawner=spawner)

    assert result.ok is False
    assert result.returncode == 3
    assert result.stderr == "amixer: Unable to find simple control"


async def test_a_missing_binary_becomes_the_shell_not_found_status() -> None:
    """``FileNotFoundError`` is reported as 127 with the exception text in stderr."""

    def _missing(_argv: tuple[str, ...]) -> FakeCommand:
        raise FileNotFoundError("amixer")

    result = await run_command(("amixer", "scontrols"), spawner=RecordingSpawner(_missing))

    assert result.ok is False
    assert result.returncode == 127
    assert "amixer" in result.stderr


async def test_an_unexecutable_binary_is_distinguished_from_a_missing_one() -> None:
    """A generic ``OSError`` is the "exists but cannot run" case, reported as 126."""

    def _refused(_argv: tuple[str, ...]) -> FakeCommand:
        raise PermissionError("amixer is not executable")

    result = await run_command(("amixer", "scontrols"), spawner=RecordingSpawner(_refused))

    assert result.ok is False
    assert result.returncode == 126
    assert "not executable" in result.stderr


async def test_a_timeout_returns_no_status_and_kills_the_child() -> None:
    """A command that outlasts the bound is killed and reported with ``None``."""
    spawner = RecordingSpawner(lambda _argv: FakeCommand(hang=True))

    result = await run_command(("amixer", "sget", "Speaker"), spawner=spawner, timeout=_TIMEOUT_S)

    assert result.returncode is None
    assert result.ok is False
    assert "did not answer within" in result.stderr
    assert spawner.processes[0].killed is True


async def test_a_cancelled_call_reaps_its_child_before_propagating() -> None:
    """Cancellation is the one failure that propagates — after killing the child."""
    spawner = RecordingSpawner(lambda _argv: FakeCommand(hang=True))

    task = asyncio.create_task(run_command(("amixer", "sget", "Speaker"), spawner=spawner))
    await wait_until(lambda: len(spawner.processes) == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert spawner.processes[0].killed is True


async def test_the_default_bound_matches_the_other_command_budgets() -> None:
    """Two seconds is the house budget for one command, not an arbitrary value."""
    assert COMMAND_TIMEOUT_S == 2.0
