"""The interpreter-exit contract: no leftover aiosqlite thread outlives the app.

An aiosqlite :class:`~aiosqlite.Connection` runs its statements on a worker
thread created without ``daemon=True``. If the connection is still reachable
when the interpreter finalizes, that thread is parked in ``SimpleQueue.get()``
and ``threading._shutdown`` joins it with no timeout, so the process never
exits and the SQLite ``-wal``/``-shm`` descriptors stay open. Before
:func:`chime_deck.app.create_app`'s lifespan closed the store in a
``finally`` that could be skipped by a failing ``stop()``, this wedged a real
deployment for fifteen hours.

Coverage: a scan that probes through ``asyncio.to_thread`` lets a real second
interpreter exit cleanly; an unclosed store demonstrably does not (the negative
control that keeps the first test honest); a connected store pins exactly one
non-daemon worker thread until ``close()``; and the lifespan still closes the
store when a subsystem's ``stop()`` raises.

Owning ticket: HSD-cwvc.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest
from aiosqlite.core import _connection_worker_thread
from ha_ws_fakes import FakeHaFactory, make_client
from log_isolation import preserve_log_state

from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig
from chime_deck.ha import HomeAssistantWebSocketClient
from chime_deck.sounds import bundled_demo_sounds_dir
from chime_deck.store import ConfigStore, StoreNotConnected

_ENV_PREFIX = "CHIMEDECK_"
_LEGACY_ENV_PREFIX = "SUPER_DOORBELL_"
"""Pre-rename prefix; ``chime_deck.env`` still honours it for one release, so it
must be cleared here too."""
_CHILD_TIMEOUT_S = 60.0
"""Hard bound on a child interpreter; a wedge must fail, not hang the suite."""

_EXIT_BUDGET_S = 10.0
"""Wall time a clean child may take, far below the hard bound."""

_HANG_TIMEOUT_S = 8.0
"""How long the negative control lets the wedged child sit before killing it."""

_DEMO_DURATION_MIN_MS = 400
_DEMO_DURATION_MAX_MS = 600
"""Plausible window for the 0.5 s demo (this host's ffprobe reports 500)."""

_NO_FFPROBE = shutil.which("ffprobe") is None
_FFPROBE_REASON = (
    "ffprobe not installed; run ./scripts/test-integration.sh (container) "
    "or ./scripts/test-integration.sh --board (real aarch64)"
)

_SCAN_SCRIPT = """
import asyncio
import sys
from pathlib import Path

from chime_deck.sounds import SoundLibrary
from chime_deck.store import ConfigStore


async def main() -> None:
    store = ConfigStore(Path(sys.argv[2]))
    await store.connect()
    report = await SoundLibrary(store, Path(sys.argv[1])).scan()
    rows = await store.list_sounds()
    await store.close()
    print("report", report.added, report.removed, report.missing, report.total, flush=True)
    printed = ["none" if row.duration_ms is None else str(row.duration_ms) for row in rows]
    print("durations", "|".join(printed), flush=True)


asyncio.run(main())
"""
"""Child that scans a directory with the real ffprobe and then exits.

It is a separate interpreter on purpose: ``tests/conftest.py`` makes ``ffprobe``
look absent for every in-process test, and that patch must not cross the
process boundary -- exercising the real binary is the point.
"""

_HANG_SCRIPT = """
import asyncio
import sys
from pathlib import Path

from chime_deck.store import ConfigStore

STORE = None


async def main() -> None:
    global STORE
    STORE = ConfigStore(Path(sys.argv[1]))
    await STORE.connect()
    print("connected", flush=True)


asyncio.run(main())
Path(sys.argv[2]).write_text("reached-exit")
"""
"""Negative control: the same child, but the connection is never closed.

``STORE`` is a module-level global so the connection is still reachable at
finalization -- the exact shape that pins the non-daemon worker thread.
"""


def _clean_env() -> dict[str, str]:
    """Return the environment for a child process without ``CHIMEDECK_*``.

    ``ResourceWarning`` is ignored by default, so the leak assertion below could
    never fire without promoting it here.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith((_ENV_PREFIX, _LEGACY_ENV_PREFIX))
    }
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONWARNINGS"] = "always::ResourceWarning"
    return env


def _line_with(output: str, key: str) -> str:
    """Return the first line of *output* that starts with *key* and a space.

    :param output: child output to search.
    :param key: line prefix to look for.
    """
    prefix = f"{key} "
    for line in output.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"{key!r} missing from child output:\n{output}")


def _aiosqlite_threads() -> list[threading.Thread]:
    """Return the live threads running aiosqlite's connection worker.

    Matched by identity against the private module-level target function:
    aiosqlite gives the thread no name, so a name match is not an option.
    Importing the private name is deliberate -- an aiosqlite upgrade that
    renames it must fail loudly at collection instead of quietly passing.
    """
    return [
        thread
        for thread in threading.enumerate()
        if getattr(thread, "_target", None) is _connection_worker_thread
    ]


def _exploding_stop(reason: str) -> Callable[[], Awaitable[None]]:
    """Return an async ``stop`` replacement that always raises.

    :param reason: message carried by the raised :class:`RuntimeError`.
    """

    async def boom() -> None:
        raise RuntimeError(reason)

    return boom


@pytest.mark.integration
@pytest.mark.skipif(_NO_FFPROBE, reason=_FFPROBE_REASON)
def test_scan_probe_thread_lets_the_interpreter_exit(tmp_path: Path) -> None:
    """A scan whose probe ran on a worker thread still exits promptly.

    :param tmp_path: pytest temporary directory.
    """
    assets = bundled_demo_sounds_dir()
    assert assets is not None
    sounds_dir = tmp_path / "sounds"
    sounds_dir.mkdir()
    shutil.copyfile(assets / "demo_ding.wav", sounds_dir / "demo_ding.wav")

    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-c", _SCAN_SCRIPT, str(sounds_dir), str(tmp_path / "config.db")],
        timeout=_CHILD_TIMEOUT_S,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    elapsed = time.monotonic() - started
    output = f"elapsed {elapsed:.2f}s\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"

    assert completed.returncode == 0, output
    assert elapsed < _EXIT_BUDGET_S, output
    # aiosqlite's __del__ emits a ResourceWarning for a connection that was
    # never closed; it is the direct leak detector for this contract.
    assert "ResourceWarning" not in completed.stderr, output
    assert "Exception ignored" not in completed.stderr, output

    report = _line_with(completed.stdout, "report").split()[1:]
    assert report == ["1", "0", "0", "1"], output

    durations = _line_with(completed.stdout, "durations").split()[1:]
    assert len(durations) == 1, output
    assert durations[0] != "none", f"the probe path never ran (duration unknown)\n{output}"
    duration_ms = int(durations[0])
    assert _DEMO_DURATION_MIN_MS <= duration_ms <= _DEMO_DURATION_MAX_MS, output


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.skipif(_NO_FFPROBE, reason=_FFPROBE_REASON)
def test_an_unclosed_store_would_hang_the_interpreter(tmp_path: Path) -> None:
    """Negative control: without ``close()`` the child never exits.

    :param tmp_path: pytest temporary directory.
    """
    marker = tmp_path / "reached-exit"
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            [sys.executable, "-c", _HANG_SCRIPT, str(tmp_path / "config.db"), str(marker)],
            timeout=_HANG_TIMEOUT_S,
            capture_output=True,
            text=True,
            env=_clean_env(),
        )

    # ``subprocess.run`` leaves TimeoutExpired.stdout as None on POSIX, so the
    # marker file is the only proof the child walked to its exit point instead
    # of wedging somewhere unrelated.
    assert marker.is_file(), (
        f"child did not reach its exit point within {_HANG_TIMEOUT_S}s; "
        "an unclosed aiosqlite Connection no longer pins the interpreter -- "
        "re-evaluate this control and the sensitivity claim in "
        "test_scan_probe_thread_lets_the_interpreter_exit"
    )
    assert marker.read_text() == "reached-exit"


@pytest.mark.asyncio
async def test_a_connected_store_pins_a_non_daemon_thread_until_close(tmp_path: Path) -> None:
    """One connected store owns one non-daemon worker thread, and close ends it.

    :param tmp_path: pytest temporary directory.
    """
    baseline = _aiosqlite_threads()
    store = ConfigStore(tmp_path / "config.db")
    await store.connect()
    spawned = [thread for thread in _aiosqlite_threads() if thread not in baseline]
    assert len(spawned) == 1, spawned
    # That non-daemon default is the hazard threading._shutdown cannot escape.
    assert spawned[0].daemon is False

    await store.close()

    # ``close()`` resolves the worker's future through ``call_soon_threadsafe``
    # before the worker breaks its loop, so the thread can still be enumerable
    # for a moment; poll to a bounded deadline instead of asserting instantly.
    # Only the thread this test spawned: a thread leaked by an earlier test is
    # not this contract's to assert on.
    watched = spawned
    for _ in range(200):
        if not any(thread.is_alive() for thread in watched):
            break
        await asyncio.sleep(0.01)
    assert [thread for thread in watched if thread.is_alive()] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attribute", "what", "reason"),
    [
        ("chime", "the chime pipeline", "chime stop exploded"),
        ("hub", "the state hub", "hub stop exploded"),
        ("ha_websocket", "the Home Assistant websocket client", "ha stop exploded"),
    ],
)
async def test_a_failing_stop_still_closes_the_store(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    what: str,
    reason: str,
) -> None:
    """A raising ``stop()`` is reported and cannot skip ``store.close()``.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    :param monkeypatch: patcher that breaks one subsystem's ``stop``.
    :param attribute: :class:`AppState` attribute whose ``stop`` explodes.
    :param what: name the shutdown warning must use for that subsystem.
    :param reason: exception message the warning must carry.
    """
    config = ServerConfig(
        data_dir=tmp_path,
        host="127.0.0.1",
        static_dir=tmp_path / "no-such-dist",
        seed_demo_sounds=False,
    )
    factory = FakeHaFactory()
    websocket_client: HomeAssistantWebSocketClient = make_client(factory)
    app = create_app(config, ha_websocket=websocket_client)
    state = cast(AppState, app.state.doorbell)
    real_chime_stop = state.chime.stop
    real_hub_stop = state.hub.stop
    real_ha_stop = websocket_client.stop
    monkeypatch.setattr(getattr(state, attribute), "stop", _exploding_stop(reason))

    caplog.set_level(logging.INFO, logger="chime_deck")
    try:
        with preserve_log_state():
            async with app.router.lifespan_context(app):
                pass
    finally:
        # The lifespan never reached the patched stop, so reclaim whatever that
        # subsystem owns before the test's event loop closes.
        await real_chime_stop()
        await real_hub_stop()
        await real_ha_stop()

    warnings = [
        record
        for record in caplog.records
        if record.name == "chime_deck.app"
        and record.levelno == logging.WARNING
        and "did not stop cleanly" in record.getMessage()
    ]
    messages = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1, messages
    assert warnings[0].getMessage() == (
        f"shutdown: {what} did not stop cleanly: RuntimeError: {reason}"
    )

    first = messages.index("shutdown: 1/4 stopping state hub")
    second = messages.index("shutdown: 2/4 stopping the Home Assistant websocket client")
    third = messages.index("shutdown: 3/4 closing configuration store")
    fourth = messages.index("shutdown: 4/4 shutdown complete")
    assert first < second < third < fourth
    # The Home Assistant stop lives in the second block, between 2/4 and 3/4;
    # the chime and hub stops live in the first.
    window_start = second if attribute == "ha_websocket" else first
    assert window_start < messages.index(warnings[0].getMessage()) < third

    with pytest.raises(StoreNotConnected):
        _ = state.store.connection
