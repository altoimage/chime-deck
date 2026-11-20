"""The real process: startup logging, clean SIGTERM shutdown, no dangling tasks.

Two subprocess runs against an ephemeral loopback port. The environment is
rebuilt from ``os.environ`` with every ``CHIMEDECK_*`` variable removed so
the developer's own configuration cannot leak into the server under test.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
from contextlib import suppress
from pathlib import Path
from typing import cast

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from chime_deck.sounds import SUPPORTED_FORMATS, bundled_demo_sounds_dir
from chime_deck.store import ConfigStore

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_ENV_PREFIX = "CHIMEDECK_"
_LEGACY_ENV_PREFIX = "SUPER_DOORBELL_"
"""Pre-rename prefix; ``chime_deck.env`` still honours it for one release, so the
child process must have it cleared too."""
_READY_TIMEOUT = 20.0
_EXIT_TIMEOUT = 10.0
_STARTUP_STEPS = tuple(f"startup: {step}/7" for step in range(1, 8))
_SHUTDOWN_STEPS = tuple(f"shutdown: {step}/4" for step in range(1, 5))
_UNCLEAN = (
    "Task was destroyed but it is pending",
    "Task exception was never retrieved",
    "Traceback",
)


def _clean_env() -> dict[str, str]:
    """Return the environment for the child process without ``CHIMEDECK_*``.

    The legacy ``SUPER_DOORBELL_*`` names are removed as well, because
    ``chime_deck.env`` still honours them for one release.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith((_ENV_PREFIX, _LEGACY_ENV_PREFIX))
    }
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _free_port() -> int:
    """Return a port that was free a moment ago."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 0))
        return cast(int, probe.getsockname()[1])


def _command(port: int, data_dir: Path) -> list[str]:
    """Return the command line that starts the server as a child process.

    :param port: loopback port to bind.
    :param data_dir: empty directory for the database.
    """
    return [
        sys.executable,
        "-m",
        "chime_deck.cli",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--data-dir",
        str(data_dir),
        "--log-level",
        "INFO",
        # Never bind the admin listener's default 0.0.0.0:8081 from a test: this
        # child serves the doorbell only (HSD-n6wi).
        "--no-admin",
    ]


async def _wait_for_health(port: int, proc: asyncio.subprocess.Process) -> bool:
    """Poll ``/api/health`` until the server answers or the deadline passes.

    :param port: port the server listens on.
    :param proc: the server process, so an early exit is noticed immediately.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _READY_TIMEOUT
    url = f"http://127.0.0.1:{port}/api/health"
    async with httpx.AsyncClient(timeout=1.0) as client:
        while True:
            if proc.returncode is not None:
                return False
            try:
                response = await client.get(url)
            except httpx.HTTPError:
                pass
            else:
                if response.status_code == 200:
                    return True
            if loop.time() > deadline:
                return False
            await asyncio.sleep(0.05)


async def _seed_offline_settings(tmp_path: Path) -> None:
    """Pre-seed the child's database before the subprocess starts.

    An empty ``mqtt.host`` makes the bridge ``not_configured``: it opens no
    socket and never resolves ``core-mosquitto.local``, which is what keeps this
    test inside AGENTS.md HARD RULE 4. The default ``audio.backend = "mpv"``
    makes the child build a real libmpv player, but no sound file is ever loaded,
    so nothing is audible; the SIGTERM latency assertion is spent on a bounded
    player stop, measured under 0.2 s on an x86_64 dev host. On a host without
    libmpv the ``audio`` health component only degrades — the assertions here do
    not look at it.

    :param tmp_path: pytest temporary directory used as the data directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        await store.set_setting("mqtt.host", "")


async def _start_server(
    tmp_path: Path,
) -> tuple[asyncio.subprocess.Process, asyncio.Task[tuple[bytes, bytes]], int]:
    """Start the server and wait until it is ready.

    :param tmp_path: pytest temporary directory used as the data directory.
    """
    await _seed_offline_settings(tmp_path)
    last_output = ""
    for _attempt in range(3):
        port = _free_port()
        proc = await asyncio.create_subprocess_exec(
            *_command(port, tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_clean_env(),
        )
        collector = asyncio.create_task(proc.communicate())
        if await _wait_for_health(port, proc):
            return proc, collector, port
        # The child never became reachable: kill and reap it before the next
        # attempt, otherwise a live server keeps the port (and its pipes) open.
        with suppress(ProcessLookupError):
            proc.kill()
        # ``wait_for`` cancels the collector when it times out, so the child is
        # awaited either way and cannot be orphaned across attempts.
        with suppress(TimeoutError):
            output, _ = await asyncio.wait_for(collector, timeout=_EXIT_TIMEOUT)
            last_output = output.decode("utf-8", errors="replace")
    raise AssertionError(f"server never became ready; output was:\n{last_output}")


async def _stop(
    proc: asyncio.subprocess.Process, collector: asyncio.Task[tuple[bytes, bytes]]
) -> tuple[str, float]:
    """Send SIGTERM, wait for the process and return its output and exit latency.

    :param proc: running server process.
    :param collector: task collecting the process output.
    """
    started = asyncio.get_running_loop().time()
    proc.send_signal(signal.SIGTERM)
    output, _ = await asyncio.wait_for(collector, timeout=_EXIT_TIMEOUT)
    elapsed = asyncio.get_running_loop().time() - started
    return output.decode("utf-8", errors="replace"), elapsed


def _assert_clean_shutdown(output: str, elapsed: float, returncode: int | None) -> None:
    """Assert the shutdown was fast, expected and free of asyncio complaints.

    :param output: combined stdout and stderr of the child.
    :param elapsed: seconds between SIGTERM and process exit.
    :param returncode: the child's exit status.
    """
    # 0.5 s of headroom under the ticket's three-second acceptance criterion.
    assert elapsed < 2.5, f"shutdown took {elapsed:.2f} s\n{output}"
    # uvicorn re-raises the captured signal, so the shell sees a signal death.
    assert returncode in (-signal.SIGTERM, 128 + signal.SIGTERM), output

    positions = []
    for needle in _STARTUP_STEPS:
        index = output.find(needle)
        assert index >= 0, f"missing {needle!r} in:\n{output}"
        positions.append(index)
    assert positions == sorted(positions), output

    shutdown_positions = []
    for needle in _SHUTDOWN_STEPS:
        index = output.find(needle)
        assert index >= 0, f"missing {needle!r} in:\n{output}"
        shutdown_positions.append(index)
    assert shutdown_positions == sorted(shutdown_positions), output
    assert shutdown_positions[0] > positions[-1], output

    sound_index = output.find("startup: sound library")
    assert sound_index >= 0, f"missing the sound library startup line in:\n{output}"
    assert positions[2] < sound_index < positions[3], output
    assets = bundled_demo_sounds_dir()
    assert assets is not None
    expected_demos = sum(
        1
        for path in assets.iterdir()
        if path.is_file() and path.suffix.lower().lstrip(".") in SUPPORTED_FORMATS
    )
    assert f"sound library: {expected_demos} sounds" in output, output

    hub_stopped = output.find("state hub stopped (cancelled 1 background task(s))")
    assert shutdown_positions[0] < hub_stopped < shutdown_positions[1], output

    for forbidden in _UNCLEAN:
        assert forbidden not in output, f"unclean output:\n{output}"


async def test_sigterm_exits_cleanly_without_clients(tmp_path: Path) -> None:
    """SIGTERM shuts an idle server down well inside three seconds.

    :param tmp_path: pytest temporary directory.
    """
    proc, collector, _port = await _start_server(tmp_path)
    try:
        output, elapsed = await _stop(proc, collector)
        _assert_clean_shutdown(output, elapsed, proc.returncode)
    finally:
        if proc.returncode is None:
            proc.kill()
            await collector


async def test_sigterm_exits_cleanly_with_a_connected_websocket(tmp_path: Path) -> None:
    """A connected tablet must not turn shutdown into a multi-second hang.

    :param tmp_path: pytest temporary directory.
    """
    proc, collector, port = await _start_server(tmp_path)
    try:
        url = f"ws://127.0.0.1:{port}/ws"
        async with connect(url, open_timeout=5, close_timeout=1) as websocket:
            snapshot = cast(
                dict[str, object], json.loads(await asyncio.wait_for(websocket.recv(), 5.0))
            )
            assert snapshot["type"] == "snapshot"

            started = asyncio.get_running_loop().time()
            proc.send_signal(signal.SIGTERM)

            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(websocket.recv(), timeout=_EXIT_TIMEOUT)

            output, _ = await asyncio.wait_for(collector, timeout=_EXIT_TIMEOUT)
            elapsed = asyncio.get_running_loop().time() - started

        _assert_clean_shutdown(output.decode("utf-8", errors="replace"), elapsed, proc.returncode)
    finally:
        if proc.returncode is None:
            proc.kill()
            await collector
