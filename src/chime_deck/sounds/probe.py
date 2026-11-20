"""``ffprobe`` duration probing, with every failure mode becoming ``None``.

Duration is metadata, not correctness: a missing ``ffprobe``, a broken file or
a probe that exceeds its deadline must never fail a scan. The function is sync
because callers (the scanner and the downloader) bridge it through
:func:`asyncio.to_thread`; internally it runs the subprocess with asyncio so the
timeout, kill and reap rules mirror :mod:`chime_deck.tts.engines`.

:class:`SpawnedProcess`, :data:`DEFAULT_WHICH` and :data:`DEFAULT_SPAWN` are the
test seam: unit tests inject fakes and never fork a real process. The module
level defaults are looked up at call time, which is what lets ``tests/conftest``
patch ``DEFAULT_WHICH`` for the whole suite.

Owning ticket: HSD-cwvc.
"""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Final, Protocol, cast

__all__ = [
    "DEFAULT_SPAWN",
    "DEFAULT_WHICH",
    "FFPROBE_TIMEOUT_S",
    "KILL_GRACE_S",
    "ProcessSpawner",
    "SpawnedProcess",
    "Which",
    "probe_duration_ms",
]

_LOGGER = logging.getLogger(__name__)

FFPROBE_TIMEOUT_S: Final = 2.0
"""How long one ffprobe may take before it is killed and the duration is left unknown."""

KILL_GRACE_S: Final = 2.0
"""How long a killed ffprobe may take to exit before the probe gives up on it."""

_PROBE_ARGV: Final[tuple[str, ...]] = (
    "-v",
    "error",
    "-show_entries",
    "format=duration",
    "-of",
    "default=noprint_wrappers=1:nokey=1",
)
"""ffprobe arguments that print one bare duration in seconds."""


class SpawnedProcess(Protocol):
    """The subset of :class:`asyncio.subprocess.Process` a duration probe uses."""

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


Which = Callable[[str], str | None]
"""Lookup for an executable name; ``shutil.which`` in production."""

ProcessSpawner = Callable[..., Awaitable[SpawnedProcess]]
"""Starts one process from an argv, without a shell; injectable for tests."""

DEFAULT_WHICH: Which = shutil.which
"""Executable lookup used when the caller does not inject one."""


async def _default_spawn(*argv: str) -> SpawnedProcess:
    """Start one real ``ffprobe`` process.

    :param argv: complete argv; never passed through a shell.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return cast(SpawnedProcess, process)


DEFAULT_SPAWN: ProcessSpawner = _default_spawn
"""Process factory used when the caller does not inject one."""


async def _kill_and_reap(process: SpawnedProcess) -> None:
    """Kill a process and await its exit, tolerating both races.

    A process may already have exited (``ProcessLookupError``) and a process may
    ignore SIGKILL for longer than the grace period (``TimeoutError``). Neither
    may escape, because the caller is already returning "duration unknown".

    :param process: process to kill and reap.
    """
    with suppress(ProcessLookupError):
        process.kill()
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), KILL_GRACE_S)


def _parse_duration(stdout: bytes, path: Path) -> int | None:
    """Parse ffprobe's bare seconds value into whole milliseconds.

    :param stdout: bytes printed by ffprobe.
    :param path: file that was probed, for the debug message.
    """
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text or text == "N/A":
        _LOGGER.debug("ffprobe reported no duration for %s", path.name)
        return None
    try:
        seconds = float(text)
    except ValueError:
        _LOGGER.debug("ffprobe reported an unparseable duration for %s", path.name)
        return None
    if not math.isfinite(seconds) or seconds < 0:
        _LOGGER.debug("ffprobe reported an unusable duration for %s", path.name)
        return None
    return round(seconds * 1000)


async def _probe(
    path: Path,
    timeout_s: float,
    resolver: Which,
    spawner: ProcessSpawner,
) -> int | None:
    """Run one bounded ffprobe and return its duration in milliseconds.

    :param path: audio file to inspect.
    :param timeout_s: seconds the probe may take before it is killed.
    :param resolver: executable lookup.
    :param spawner: process factory.
    """
    binary = resolver("ffprobe")
    if binary is None:
        _LOGGER.debug("ffprobe is not installed; duration for %s is unknown", path.name)
        return None

    argv = (binary, *_PROBE_ARGV, str(path))
    try:
        process = await spawner(*argv)
    except FileNotFoundError:
        _LOGGER.debug("ffprobe is not installed; duration for %s is unknown", path.name)
        return None

    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout_s)
    except TimeoutError:
        await _kill_and_reap(process)
        _LOGGER.info(
            "ffprobe timed out reading %s; duration will be filled by a later scan",
            path.name,
        )
        return None
    except asyncio.CancelledError:
        await _kill_and_reap(process)
        raise

    if process.returncode != 0:
        _LOGGER.debug("ffprobe exited with status %s for %s", process.returncode, path.name)
        return None
    return _parse_duration(stdout, path)


def probe_duration_ms(
    path: Path,
    *,
    timeout_s: float = FFPROBE_TIMEOUT_S,
    which: Which | None = None,
    spawner: ProcessSpawner | None = None,
) -> int | None:
    """Return *path*'s duration in whole milliseconds, or ``None``.

    This function never raises for a probe problem: "duration unknown" is a
    valid outcome and a later scan retries it. It is called from sync code (a
    worker thread in production) and creates its own event loop for the
    subprocess, which is finished before the loop is closed.

    :param path: audio file to inspect.
    :param timeout_s: seconds the probe may take before it is killed.
    :param which: executable lookup; ``None`` uses :data:`DEFAULT_WHICH` now.
    :param spawner: process factory; ``None`` uses :data:`DEFAULT_SPAWN` now.
    """
    resolver = which if which is not None else DEFAULT_WHICH
    factory = spawner if spawner is not None else DEFAULT_SPAWN
    return asyncio.run(_probe(path, timeout_s, resolver, factory))
