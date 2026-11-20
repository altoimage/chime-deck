"""The stop path, end to end, and the arithmetic behind the shipped systemd unit.

Three tiers prove this ticket, and only the first two run everywhere:

* Tests A-C drive the real process on this host: a clean SIGTERM, a SIGKILL that
  leaves a WAL the next start recovers, and a chime stop that raises while the
  lifespan still finishes and the interpreter still exits. They are offline — an
  empty ``mqtt.host`` keeps every socket shut — and marked ``integration``
  because each one spawns a server.
* Test D is offline unit-file arithmetic: it parses ``deploy/chime-deck.service``
  and re-derives the stop budget from the constants the application actually
  uses, so the number in the file and the number in the code cannot drift apart.
* Test E is the only real proof that systemd restarts the unit and holds a
  too-new database failed. It needs a live ``systemd --user`` session and writes
  a throwaway unit into ``~/.config/systemd/user``, so it skips cleanly unless
  ``CHIMEDECK_TEST_SYSTEMD=1`` is set on the ARM board.
"""

from __future__ import annotations

import asyncio
import getpass
import grp
import math
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import closing, suppress
from pathlib import Path
from typing import cast

import httpx
import pytest

from chime_deck.audio.mpv_backend import _STOP_TIMEOUT_S as MPV_STOP_TIMEOUT_S
from chime_deck.cli import GRACEFUL_SHUTDOWN_TIMEOUT_S
from chime_deck.ha.websocket import DEFAULT_SHUTDOWN_TIMEOUT_S as HA_SHUTDOWN_TIMEOUT_S
from chime_deck.hub import HUB_STOP_TIMEOUT_S
from chime_deck.mqtt.bridge import DEFAULT_SHUTDOWN_TIMEOUT_S as MQTT_SHUTDOWN_TIMEOUT_S
from chime_deck.playback import _STOP_SETTLE_S, SHUTDOWN_GRACE_S
from chime_deck.sounds.probe import FFPROBE_TIMEOUT_S, KILL_GRACE_S
from chime_deck.store import ConfigStore, latest_schema_version
from chime_deck.tts.engines import _KILL_GRACE_S as ENGINE_KILL_GRACE_S
from chime_deck.tts.engines import _TIMEOUT_S as ENGINE_TIMEOUT_S

_ENV_PREFIX = "CHIMEDECK_"
_LEGACY_ENV_PREFIX = "SUPER_DOORBELL_"
"""Pre-rename prefix; ``chime_deck.env`` still honours it for one release, so it
must be cleared here too."""
_READY_TIMEOUT = 20.0
_EXIT_TIMEOUT = 10.0
_UNCLEAN = (
    "Traceback",
    "Task was destroyed but it is pending",
    "Task exception was never retrieved",
)

_RAISING_STOP_SCRIPT = """
import asyncio
import sys
from pathlib import Path

from chime_deck.app import create_app
from chime_deck.config import ServerConfig


async def explode() -> None:
    raise RuntimeError("chime stop exploded")


async def main() -> None:
    data_dir = Path(sys.argv[1])
    config = ServerConfig(
        data_dir=data_dir,
        static_dir=data_dir / "no-dist",
        seed_demo_sounds=False,
    )
    app = create_app(config)
    app.state.doorbell.chime.stop = explode
    async with app.router.lifespan_context(app):
        pass
    print("lifespan-finished")


asyncio.run(main())
"""


def _clean_env() -> dict[str, str]:
    """Return the environment for a child process without ``CHIMEDECK_*``."""
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
    :param data_dir: directory holding the database.
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
    """Pre-seed the child's database so it opens no socket and no audio device.

    An empty ``mqtt.host`` makes the bridge ``not_configured``, which keeps these
    tests inside AGENTS.md HARD RULE 4. The default ``audio.backend = "mpv"``
    makes the child build a real libmpv player, but no sound file is ever loaded,
    so nothing is audible; the stop budget absorbs a bounded player stop,
    measured under 0.2 s on an x86_64 dev host. On a host without libmpv the
    ``audio`` health component only degrades, and no assertion here reads it.

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
        # A child that never became reachable keeps the port and its pipes open:
        # kill and reap it before the next attempt.
        with suppress(ProcessLookupError):
            proc.kill()
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


def _integrity_check(db: Path) -> str:
    """Return the result of ``PRAGMA integrity_check`` for a database file.

    :param db: SQLite file to check.
    """
    with closing(sqlite3.connect(db)) as connection:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    return "" if row is None else str(row[0])


def _raw_setting_value(db: Path, key: str) -> str:
    """Return the raw JSON text of one ``settings`` row, bypassing the key registry.

    :func:`ConfigStore.get_setting` only accepts keys in the settings registry, so
    an out-of-band row such as the one this test writes has to be read directly.

    :param db: SQLite file to read.
    :param key: settings key to look up.
    """
    with closing(sqlite3.connect(db)) as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    assert row is not None, f"setting {key!r} is missing"
    return str(row[0])


# ---------------------------------------------------------------------------
# A-C: the real process on this host
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sigterm_exits_promptly_and_releases_the_database(tmp_path: Path) -> None:
    """A clean stop is fast, signal-shaped, child-free and leaves a usable database.

    :param tmp_path: pytest temporary directory.
    """
    proc, collector, _port = await _start_server(tmp_path)
    output = ""
    try:
        pid = proc.pid
        # /proc, not `ps`: the container image is python:3.11-slim and does not
        # install procps. The file is the kernel's own child list for the main
        # thread; an idle doorbell must have none before it is stopped.
        children_path = Path(f"/proc/{pid}/task/{pid}/children")
        children = children_path.read_text(encoding="utf-8").split()
        assert children == [], f"pid {pid} already has children: {children}"

        output, elapsed = await _stop(proc, collector)
        assert elapsed < 5.0, f"shutdown took {elapsed:.2f} s\n{output}"
        # uvicorn re-raises the captured signal: signal death or 128 + 15.
        assert proc.returncode in (-signal.SIGTERM, 128 + signal.SIGTERM), output
        for forbidden in _UNCLEAN:
            assert forbidden not in output, f"unclean output:\n{output}"

        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not Path(f"/proc/{pid}").exists(), f"pid {pid} is still in /proc"
    finally:
        if proc.returncode is None:
            proc.kill()
            await collector

    # The database must be immediately usable: no lock held, no WAL to replay
    # that a second open cannot handle, no rollback journal left behind.
    db = tmp_path / "doorbell.db"
    started = time.monotonic()
    async with ConfigStore(db) as store:
        assert await store.recent_presses() == []
        assert await store.get_setting("mqtt.host") == ""
    assert _integrity_check(db) == "ok"
    reopen_elapsed = time.monotonic() - started
    assert reopen_elapsed < 5.0, f"reopen took {reopen_elapsed:.2f} s"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_kill_9_leaves_a_wal_that_the_next_start_recovers(tmp_path: Path) -> None:
    """SIGKILL leaves a WAL, and both the store and a second start recover it.

    :param tmp_path: pytest temporary directory.
    """
    proc, collector, _port = await _start_server(tmp_path)
    db = tmp_path / "doorbell.db"
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)",
            ("test.kill9", '"survives"', "2026-11-13T00:00:00.000000Z"),
        )
        writer.commit()

        proc.kill()
        output, _ = await asyncio.wait_for(collector, timeout=_EXIT_TIMEOUT)
        assert proc.returncode == -signal.SIGKILL, output.decode("utf-8", errors="replace")
        assert Path(f"{db}-wal").exists(), "SIGKILL left no WAL to recover"

        # A second reader sees the seeded settings through the WAL while the
        # writer is still open, and the committed out-of-band row is there too.
        async with ConfigStore(db) as store:
            assert await store.get_setting("mqtt.host") == ""
        assert _raw_setting_value(db, "test.kill9") == '"survives"'
        assert _integrity_check(db) == "ok"
    finally:
        writer.close()

    # The process-level equivalent of Restart=always: a fresh child against the
    # same data directory comes up, then stops on SIGTERM inside its budget.
    second, second_collector, _second_port = await _start_server(tmp_path)
    try:
        second_output, second_elapsed = await _stop(second, second_collector)
        assert second_elapsed < 5.0, f"shutdown took {second_elapsed:.2f} s\n{second_output}"
        assert second.returncode in (-signal.SIGTERM, 128 + signal.SIGTERM), second_output
    finally:
        if second.returncode is None:
            second.kill()
            await second_collector


@pytest.mark.integration
def test_a_raising_stop_still_lets_a_real_process_exit(tmp_path: Path) -> None:
    """A chime ``stop`` that raises is reported, and the process still exits 0.

    :param tmp_path: pytest temporary directory.
    """
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", _RAISING_STOP_SCRIPT, str(tmp_path)],
        timeout=30,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert elapsed < 10.0, f"the process took {elapsed:.2f} s to exit"
    assert (
        "shutdown: the chime pipeline did not stop cleanly: RuntimeError: chime stop exploded"
        in result.stderr
    ), result.stderr
    assert "lifespan-finished" in result.stdout, result.stdout
    assert "Traceback" not in result.stderr, result.stderr
    assert "ResourceWarning" not in result.stderr, result.stderr


# ---------------------------------------------------------------------------
# D: offline unit-file tests
# ---------------------------------------------------------------------------


def _unit_path() -> Path:
    """Return the path of the shipped systemd user unit."""
    return Path(__file__).resolve().parents[1] / "deploy" / "chime-deck.service"


def _unit_values() -> dict[str, list[str]]:
    """Parse the unit file into ``key -> [values]``, skipping comments and sections."""
    values: dict[str, list[str]] = {}
    for line in _unit_path().read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#;[":
            continue
        key, _, value = stripped.partition("=")
        values.setdefault(key.strip(), []).append(value.strip())
    return values


def test_the_unit_is_a_user_unit_with_the_documented_environment() -> None:
    """The shipped unit starts the documented binary in the documented directory."""
    values = _unit_values()
    assert "docs/installing.md" in values["Documentation"][0]
    assert values["Type"] == ["simple"]
    assert values["WorkingDirectory"] == ["%h/chime_deck"]
    assert values["ExecStart"] == ["%h/chime_deck/.venv/bin/chime-deck"]
    assert values["Environment"] == [
        "CHIMEDECK_HOST=127.0.0.1",
        "CHIMEDECK_PORT=8080",
        "CHIMEDECK_DATA_DIR=%h/chime_deck/data",
    ]
    assert values["Restart"] == ["always"]
    assert values["RestartSec"] == ["3"]
    assert values["WantedBy"] == ["default.target"]


def test_the_unit_treats_a_clean_sigterm_as_success_and_holds_a_too_new_schema_failed() -> None:
    """Signal 15 is success, and exit 78 must not be restarted in a loop."""
    values = _unit_values()
    assert values["KillSignal"] == ["SIGTERM"]
    tokens = " ".join(values["SuccessExitStatus"]).split()
    assert "143" in tokens
    assert "SIGTERM" in tokens
    assert values["RestartPreventExitStatus"] == ["78"]


def test_the_unit_has_no_supplementary_groups_regression_guard() -> None:
    """``SupplementaryGroups=`` is a measured failure in a user unit: never re-add it."""
    values = _unit_values()
    assert "SupplementaryGroups" not in values
    assert "216/GROUP" in _unit_path().read_text(encoding="utf-8")


def test_the_unit_timeout_covers_the_whole_bounded_stop_path() -> None:
    """``TimeoutStopSec`` is derived from, not guessed at, the constants in the code."""
    # The two constants that live on their owners' instances rather than at
    # module scope, pinned with the file:line the unit's comment names.
    voice_close_grace_s = 5.0  # tts/voices.py:993,1073 (VoiceDownloadRegistry close_grace_s)
    store_close_timeout_s = 5.0  # store/database.py:224 PRAGMA busy_timeout=5000

    serial = (
        float(GRACEFUL_SHUTDOWN_TIMEOUT_S)
        + voice_close_grace_s
        + MQTT_SHUTDOWN_TIMEOUT_S  # _stop_client
        + MQTT_SHUTDOWN_TIMEOUT_S  # _drain_pending
        + (2 * SHUTDOWN_GRACE_S + 3 * _STOP_SETTLE_S)  # playback worker stop
        + MPV_STOP_TIMEOUT_S
        + HUB_STOP_TIMEOUT_S
        + store_close_timeout_s
    )
    concurrent = ENGINE_TIMEOUT_S + ENGINE_KILL_GRACE_S + FFPROBE_TIMEOUT_S + KILL_GRACE_S
    budget = serial + concurrent + HA_SHUTDOWN_TIMEOUT_S

    assert math.isclose(serial, 35.0), serial
    assert math.isclose(concurrent, 11.0), concurrent
    assert math.isclose(budget, 51.0), budget

    values = _unit_values()
    timeout = float(values["TimeoutStopSec"][0])
    assert timeout >= math.ceil(1.25 * budget)
    assert timeout > budget
    assert values["KillMode"] == ["mixed"]


def test_systemd_analyze_accepts_the_unit_syntax(tmp_path: Path) -> None:
    """``systemd-analyze verify`` parses the unit; skips on hosts without it.

    :param tmp_path: pytest temporary directory for the rewritten copy.
    """
    systemd_analyze = shutil.which("systemd-analyze")
    if systemd_analyze is None:
        pytest.skip("systemd-analyze is not installed; this is the integration tier")

    text = _unit_path().read_text(encoding="utf-8")
    text = text.replace("WorkingDirectory=%h/chime_deck", "WorkingDirectory=/tmp").replace(
        "ExecStart=%h/chime_deck/.venv/bin/chime-deck", "ExecStart=/bin/true"
    )
    copy = tmp_path / "chime-deck.service"
    copy.write_text(text, encoding="utf-8")

    result = subprocess.run(
        [systemd_analyze, "verify", "--user", str(copy)],
        timeout=20,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    # systemd-analyze reports problems in OTHER user units too (the ARM board's
    # spice-vdagent has an unrelated "Unknown key" warning), so only the lines
    # that name the file under test are ours to judge.
    diagnostics = [line for line in combined.splitlines() if copy.name in line]
    assert not diagnostics, combined


# ---------------------------------------------------------------------------
# E: the gated real-systemd tests (ARM board only)
# ---------------------------------------------------------------------------

_SYSTEMD_READY = os.environ.get("CHIMEDECK_TEST_SYSTEMD") == "1"
_SYSTEMD_SKIP = (
    "requires a real systemd --user session; run on the ARM board with "
    "CHIMEDECK_TEST_SYSTEMD=1 and CHIMEDECK_EXPECTED_HEAD=<commit> "
    "(see docs/installing.md)"
)


def _repo_root() -> Path:
    """Return the repository root."""
    return Path(__file__).resolve().parents[1]


def _user_unit_dir() -> Path:
    """Return ``~/.config/systemd/user``."""
    return Path.home() / ".config" / "systemd" / "user"


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``systemctl --user`` with the arguments and a bounded wait.

    :param args: arguments after ``systemctl --user``.
    """
    return subprocess.run(
        ["systemctl", "--user", *args], timeout=15, capture_output=True, text=True
    )


def _wait(predicate: Callable[[], bool], timeout: float = 30.0) -> bool:
    """Poll *predicate* every 0.2 s, at most ``timeout / 0.2`` times.

    :param predicate: condition to poll.
    :param timeout: longest wait, in seconds.
    """
    for _ in range(int(timeout / 0.2)):
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _show(unit: str, prop: str) -> str:
    """Return one property of a unit, stripped.

    ``--value`` is load-bearing: without it ``systemctl show`` prints
    ``Property=value`` and every caller here parses the value alone.

    :param unit: unit name.
    :param prop: property name, e.g. ``MainPID``.
    """
    return _systemctl("show", "-p", prop, "--value", unit).stdout.strip()


def _failed_units() -> set[str]:
    """Return the names of the user units currently in the failed state."""
    result = _systemctl("--failed", "--no-legend")
    return {line.split()[0] for line in result.stdout.splitlines() if line.strip()}


def _linger_enabled() -> bool:
    """Return whether lingering is enabled for this user."""
    result = subprocess.run(
        ["loginctl", "show-user", getpass.getuser(), "-p", "Linger"],
        timeout=15,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().endswith("yes")


def _set_linger(enabled: bool) -> None:
    """Enable or disable lingering, falling back to non-interactive sudo.

    :param enabled: whether the user manager should start at boot.
    """
    action = "enable-linger" if enabled else "disable-linger"
    user = getpass.getuser()
    result = subprocess.run(["loginctl", action, user], timeout=15, capture_output=True, text=True)
    if result.returncode != 0:
        subprocess.run(["sudo", "-n", "loginctl", action, user], timeout=15, capture_output=True)


def _bound_port() -> int:
    """Return a loopback port that is free right now.

    :raises pytest.skip.Exception: if every candidate is already bound.
    """
    for _attempt in range(5):
        port = _free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    pytest.skip("no free loopback port available for the test unit")


def _require_committed_unit() -> None:
    """Skip unless the installed unit is the committed one, at the expected commit.

    :raises pytest.skip.Exception: if the checkout is not the expected HEAD or
        ``deploy/chime-deck.service`` has local changes.
    """
    repo = _repo_root()
    expected = os.environ.get("CHIMEDECK_EXPECTED_HEAD", "")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, timeout=15, capture_output=True, text=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "deploy/chime-deck.service"],
        cwd=repo,
        timeout=15,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if expected == "" or expected != head or dirty != "":
        pytest.skip(
            "the installed unit must be the committed file: "
            f"expected HEAD {expected or '<unset>'}, have {head}, local changes {dirty!r}"
        )


def _write_unit(name: str) -> Path:
    """Install the shipped unit text byte-for-byte under a throwaway name.

    :param name: unit name without the ``.service`` suffix.
    """
    source = _unit_path().read_text(encoding="utf-8")
    target = _user_unit_dir() / f"{name}.service"
    _user_unit_dir().mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    assert target.read_text(encoding="utf-8") == source, "the installed unit is not identical"
    return target


def _write_dropin(name: str, *, data_dir: Path, port: int) -> Path:
    """Point the throwaway unit at this checkout, a temporary data dir and a free port.

    :param name: unit name without the ``.service`` suffix.
    :param data_dir: data directory for the throwaway instance.
    :param port: loopback port for the throwaway instance.
    """
    repo = _repo_root()
    drop_in = _user_unit_dir() / f"{name}.service.d" / "10-test.conf"
    drop_in.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[Service]",
        f"WorkingDirectory={repo}",
        "ExecStart=",
        f"ExecStart={repo}/.venv/bin/chime-deck",
        f"Environment=CHIMEDECK_DATA_DIR={data_dir}",
        f"Environment=CHIMEDECK_PORT={port}",
        "Environment=CHIMEDECK_LOG_FILE=",
        # The board's user journal is not always readable from the test process
        # (journalctl reports "No journal files were found"), so the refusal test
        # owns its evidence. The drop-in is already the one test-only deviation.
        f"StandardOutput=append:{data_dir}/unit.log",
        f"StandardError=append:{data_dir}/unit.log",
    ]
    drop_in.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return drop_in


def _remove_unit(name: str) -> None:
    """Stop, disable and forget a throwaway unit and its drop-in.

    Every ``systemctl`` is bounded; a timeout during cleanup must not skip the
    file removal, or an enabled ``Restart=always`` unit could survive the run
    pointing at a deleted temporary data directory.

    :param name: unit name without the ``.service`` suffix.
    """
    with suppress(subprocess.TimeoutExpired):
        _systemctl("stop", name)
    with suppress(subprocess.TimeoutExpired):
        _systemctl("disable", name)
    (_user_unit_dir() / f"{name}.service").unlink(missing_ok=True)
    drop_in_dir = _user_unit_dir() / f"{name}.service.d"
    if drop_in_dir.is_dir():
        for child in drop_in_dir.iterdir():
            child.unlink()
        drop_in_dir.rmdir()
    with suppress(subprocess.TimeoutExpired):
        _systemctl("daemon-reload")
    with suppress(subprocess.TimeoutExpired):
        _systemctl("reset-failed", name)


async def _bump_schema(db: Path) -> None:
    """Create a migrated database and then make its schema one version too new.

    :param db: path of the database file to create and bump.
    """
    async with ConfigStore(db) as store:
        await store.set_setting("mqtt.host", "")
    with closing(sqlite3.connect(db)) as connection:
        connection.execute(f"PRAGMA user_version = {latest_schema_version() + 1}")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()


def _unit_is_active(name: str) -> bool:
    """Return whether the unit is active.

    :param name: unit name without the ``.service`` suffix.
    """
    return _systemctl("is-active", name).stdout.strip() == "active"


def _purge_stale_data_dirs() -> None:
    """Remove throwaway data dirs a previously killed run could not clean up."""
    for stale in Path.home().glob(".hsd8cyd-e2e-*"):
        shutil.rmtree(stale, ignore_errors=True)


def _health_ok(port: int) -> bool:
    """Return whether the unit answers its health route on *port*.

    :param port: loopback port the drop-in told the unit to bind.
    """
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1.0)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


@pytest.mark.integration
def test_the_shipped_unit_restarts_after_kill_9() -> None:
    """A real user unit restarts after SIGKILL and keeps its audio group.

    The throwaway data directory lives under ``$HOME``, not ``tmp_path``: the
    shipped unit sets ``PrivateTmp=true``, so the service cannot see the test's
    ``/tmp`` and would silently create its own database instead of opening the
    one this test prepared.
    """
    if not _SYSTEMD_READY:
        pytest.skip(_SYSTEMD_SKIP)
    _require_committed_unit()
    repo = _repo_root()
    port = _bound_port()
    name = f"hsd8cyd-{uuid.uuid4().hex[:8]}"
    _purge_stale_data_dirs()
    data_dir = Path.home() / f".hsd8cyd-e2e-{name}"
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True)
    linger_before = _linger_enabled()
    failed_before = _failed_units()

    try:
        _write_unit(name)
        _write_dropin(name, data_dir=data_dir, port=port)
        _systemctl("daemon-reload")
        _systemctl("reset-failed", name)
        _systemctl("enable", name)
        _systemctl("start", name)
        assert f"{repo}/.venv/bin/chime-deck" in _show(name, "ExecStart")
        assert _show(name, "WorkingDirectory") == str(repo)
        assert _wait(lambda: _unit_is_active(name)), _systemctl("status", name).stdout
        assert _wait(lambda: _health_ok(port), 30.0), "the unit never served /api/health"
        db = data_dir / "doorbell.db"
        assert _wait(lambda: db.exists(), 20.0), "the unit never created its database"

        main_pid = int(_show(name, "MainPID"))
        assert main_pid > 0
        status = Path(f"/proc/{main_pid}/status").read_text(encoding="utf-8")
        groups_line = next(line for line in status.splitlines() if line.startswith("Groups:"))
        audio_gid = grp.getgrnam("audio").gr_gid
        assert str(audio_gid) in groups_line.split()[1:], (
            f"the service is not in the audio group ({groups_line}); run "
            '`sudo usermod -aG audio "$USER"` then `loginctl terminate-user "$USER"` '
            "or reboot, and verify with "
            "`systemctl --user show -p MainPID chime-deck` plus "
            "`grep ^Groups /proc/<MainPID>/status`"
        )

        restarts = int(_show(name, "NRestarts"))
        os.kill(main_pid, signal.SIGKILL)

        def restarted() -> bool:
            current = _show(name, "MainPID")
            return current not in ("", "0", str(main_pid)) and _unit_is_active(name)

        assert _wait(restarted), _systemctl("status", name).stdout
        assert int(_show(name, "NRestarts")) > restarts
        assert _wait(lambda: _health_ok(port), 30.0), "the restarted unit never served /api/health"
        assert _integrity_check(db) == "ok"
    finally:
        _remove_unit(name)
        shutil.rmtree(data_dir, ignore_errors=True)
        with suppress(subprocess.TimeoutExpired):
            if _linger_enabled() != linger_before:
                _set_linger(linger_before)
        assert name not in _failed_units(), "the throwaway unit was left failed"
        assert _failed_units() <= failed_before


@pytest.mark.integration
def test_a_too_new_database_unit_is_held_failed() -> None:
    """A too-new database exits 78, and the unit stays failed instead of looping.

    The data directory is under ``$HOME`` for the same ``PrivateTmp=true``
    reason as the restart test above.
    """
    if not _SYSTEMD_READY:
        pytest.skip(_SYSTEMD_SKIP)
    _require_committed_unit()
    port = _bound_port()
    name = f"hsd8cyd-{uuid.uuid4().hex[:8]}"
    _purge_stale_data_dirs()
    data_dir = Path.home() / f".hsd8cyd-e2e-{name}"
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True)
    db = data_dir / "doorbell.db"
    asyncio.run(_bump_schema(db))
    linger_before = _linger_enabled()
    failed_before = _failed_units()

    try:
        _write_unit(name)
        _write_dropin(name, data_dir=data_dir, port=port)
        _systemctl("daemon-reload")
        _systemctl("reset-failed", name)
        _systemctl("enable", name)
        _systemctl("start", name)
        assert _wait(lambda: _systemctl("is-failed", name).stdout.strip() == "failed"), _systemctl(
            "status", name
        ).stdout
        assert _show(name, "MainPID") == "0"

        def stays_failed() -> bool:
            # Ten seconds of capped polling: Restart=always alone would have
            # restarted it several times inside this window.
            for _ in range(50):
                if (
                    _unit_is_active(name)
                    or _systemctl("is-failed", name).stdout.strip() != "failed"
                ):
                    return False
                time.sleep(0.2)
            return True

        assert stays_failed(), "the unit did not stay failed on exit status 78"
        assert _show(name, "ExecMainStatus") == "78"
        log = data_dir / "unit.log"
        assert _wait(
            lambda: log.exists() and "database schema version" in log.read_text(encoding="utf-8"),
            20.0,
        ), (
            "the refusal never reached the unit log: "
            f"{log.read_text(encoding='utf-8') if log.exists() else '<missing>'}"
        )
        text = log.read_text(encoding="utf-8")
        assert "git pull && uv sync" in text
    finally:
        _remove_unit(name)
        shutil.rmtree(data_dir, ignore_errors=True)
        with suppress(subprocess.TimeoutExpired):
            if _linger_enabled() != linger_before:
                _set_linger(linger_before)
        assert name not in _failed_units(), "the throwaway unit was left failed"
        assert _failed_units() <= failed_before
