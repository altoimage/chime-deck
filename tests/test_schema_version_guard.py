"""Refusing a database written by a newer build: preflight, store and CLI.

A newer database can never be downgraded, so three layers say no: the CLI's
read-only preflight (one clean log line, exit ``78``), :class:`Database`'s
migration guard (the real guarantee, because a WAL-only bump can be invisible to
a read-only open) and a real child process, which proves the refusal costs no
traceback, no leaked connection and well under ten seconds.

Owning ticket: HSD-8cyd.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import aiosqlite
import pytest
from log_isolation import preserve_log_state

from chime_deck.cli import SCHEMA_TOO_NEW_EXIT_CODE, main
from chime_deck.store import (
    ConfigStore,
    SchemaTooNewError,
    StoreNotConnected,
    check_schema_version,
    latest_schema_version,
)

_ENV_PREFIX = "CHIMEDECK_"
_LEGACY_ENV_PREFIX = "SUPER_DOORBELL_"
"""Pre-rename prefix; ``chime_deck.env`` still honours it for one release, so it
must be cleared here too."""
_CHILD_TIMEOUT_S = 60.0
"""Hard bound on the child interpreter; a wedge must fail, not hang the suite."""

_EXIT_BUDGET_S = 10.0
"""Wall time a clean refusal may take, far below the hard bound."""


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ``CHIMEDECK_*`` variable so the ambient host cannot interfere.

    :param monkeypatch: pytest monkeypatch fixture.
    """
    for key in list(os.environ):
        if key.startswith((_ENV_PREFIX, _LEGACY_ENV_PREFIX)):
            monkeypatch.delenv(key, raising=False)


async def _build_db(path: Path) -> None:
    """Create a fully migrated database at *path* and close it.

    :param path: filesystem path of the SQLite file.
    """
    async with ConfigStore(path):
        pass


def _set_user_version(path: Path, version: int) -> None:
    """Overwrite ``PRAGMA user_version`` with a plain, non-async connection.

    :param path: filesystem path of the SQLite file.
    :param version: schema version to record.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"PRAGMA user_version = {version:d}")
        connection.commit()
    finally:
        connection.close()


async def _too_new_db(tmp_path: Path) -> tuple[Path, int, int]:
    """Build a database one schema version ahead of this build.

    :param tmp_path: pytest temporary directory.
    :returns: the path, its recorded version and the version this build supports.
    """
    path = tmp_path / "doorbell.db"
    await _build_db(path)
    supported = latest_schema_version()
    newer = supported + 1
    _set_user_version(path, newer)
    return path, newer, supported


def _crash_a_writer_mid_transaction(path: Path) -> None:
    """Leave a hot rollback journal behind, the way a crash or power cut does.

    The spilled pages are in the database and their pre-images are in the
    journal, and the writer never commits, so the next connection must roll the
    journal back before it may read the file.

    :param path: filesystem path of the SQLite file.
    """
    script = """
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("PRAGMA journal_mode=DELETE")
connection.execute("BEGIN IMMEDIATE")
connection.execute("PRAGMA cache_size=1")
connection.execute("CREATE TABLE IF NOT EXISTS crashed_write (payload BLOB)")
connection.execute("INSERT INTO crashed_write VALUES (randomblob(2000000))")
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        timeout=_CHILD_TIMEOUT_S,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    journal = path.with_name(path.name + "-journal")
    assert journal.is_file(), "the crashing writer left no hot journal"


async def _too_new_behind_a_hot_journal(tmp_path: Path) -> tuple[Path, int, int]:
    """Build a database whose newer version only a write-mode open can see.

    :param tmp_path: pytest temporary directory.
    :returns: the path, its recorded version and the version this build supports.
    """
    path, newer, supported = await _too_new_db(tmp_path)
    _crash_a_writer_mid_transaction(path)
    return path, newer, supported


@pytest.mark.asyncio
async def test_connect_refuses_a_newer_schema_and_closes_the_connection(
    tmp_path: Path,
) -> None:
    """A newer ``user_version`` raises before any migration is applied.

    The failed connection must be closed, not leaked: the interpreter-exit
    contract in ``test_shutdown_exit.py`` hangs on a live aiosqlite worker.

    :param tmp_path: pytest temporary directory.
    """
    path, newer, supported = await _too_new_db(tmp_path)

    store = ConfigStore(path)
    with pytest.raises(SchemaTooNewError) as caught:
        await store.connect()

    error = caught.value
    assert error.database_version == newer
    assert error.supported_version == supported
    message = str(error)
    assert f"database schema version {newer} " in message
    assert f"({supported})" in message
    assert "git pull && uv sync" in message
    assert "downgrades are not supported" in message

    with pytest.raises(StoreNotConnected):
        _ = store.connection


def test_check_schema_version_passes_a_missing_file_and_creates_nothing(
    tmp_path: Path,
) -> None:
    """A fresh install has no file yet, so the preflight must not create one.

    :param tmp_path: pytest temporary directory.
    """
    path = tmp_path / "doorbell.db"

    check_schema_version(path)
    check_schema_version(str(path))
    assert not path.exists()


@pytest.mark.asyncio
async def test_check_schema_version_passes_a_fresh_database(tmp_path: Path) -> None:
    """A database this build just created is never refused.

    :param tmp_path: pytest temporary directory.
    """
    path = tmp_path / "doorbell.db"
    await _build_db(path)

    check_schema_version(path)


@pytest.mark.asyncio
async def test_check_schema_version_refuses_a_newer_database(tmp_path: Path) -> None:
    """The read-only preflight raises the typed error the CLI catches.

    :param tmp_path: pytest temporary directory.
    """
    path, newer, supported = await _too_new_db(tmp_path)

    with pytest.raises(SchemaTooNewError) as caught:
        check_schema_version(path)

    assert caught.value.database_version == newer
    assert caught.value.supported_version == supported


@pytest.mark.asyncio
async def test_a_write_open_sees_a_version_the_read_only_preflight_cannot(
    tmp_path: Path,
) -> None:
    """The refusal cannot rest on the best-effort preflight alone.

    A read-only connection may not roll a hot journal back, so the preflight
    swallows the error and reports nothing; the write-mode open the store uses
    rolls the journal back and refuses. That is why the CLI runs the store's own
    guard before it serves anything.

    :param tmp_path: pytest temporary directory.
    """
    path, newer, supported = await _too_new_behind_a_hot_journal(tmp_path)
    journal = path.with_name(path.name + "-journal")

    check_schema_version(path)
    assert journal.is_file(), "the preflight attempt must not consume the journal"

    store = ConfigStore(path)
    with pytest.raises(SchemaTooNewError) as caught:
        await store.connect()

    assert caught.value.database_version == newer
    assert caught.value.supported_version == supported
    message = str(caught.value)
    assert f"database schema version {newer} " in message
    assert f"({supported})" in message

    with pytest.raises(StoreNotConnected):
        _ = store.connection


def test_check_schema_version_tolerates_a_file_that_is_not_sqlite(tmp_path: Path) -> None:
    """A garbage file is left to ``Database.connect``, which explains it far better.

    :param tmp_path: pytest temporary directory.
    """
    path = tmp_path / "doorbell.db"
    path.write_bytes(b"not a database")

    check_schema_version(path)


def test_latest_schema_version_is_the_newest_shipped_migration() -> None:
    """The shipped migration set currently ends at version 5."""
    assert latest_schema_version() == 5


def test_cli_refuses_a_newer_database_without_starting_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The refusal is one ERROR line and exit ``78``: nothing is ever served.

    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: pytest temporary directory.
    :param caplog: pytest log capture fixture.
    :param capsys: captured standard streams.
    """
    _clear_env(monkeypatch)
    _path, newer, supported = asyncio.run(_too_new_db(tmp_path))

    def _forbid(_configs: object) -> int:
        raise AssertionError("the serving layer must not be reached")

    monkeypatch.setattr("chime_deck.serving.serve", _forbid)

    with preserve_log_state(), caplog.at_level(logging.ERROR, logger="chime_deck.cli"):
        status = main(["--data-dir", str(tmp_path), "--port", "0"])

    assert status == SCHEMA_TOO_NEW_EXIT_CODE == 78
    assert status != 0

    errors = [
        record
        for record in caplog.records
        if record.name == "chime_deck.cli" and record.levelno == logging.ERROR
    ]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert str(newer) in message
    assert str(supported) in message
    assert "git pull && uv sync" in message

    captured = capsys.readouterr()
    assert "Traceback" not in caplog.text
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_a_real_child_process_refuses_a_newer_database(tmp_path: Path) -> None:
    """A real interpreter exits non-zero, quickly, with one clean message.

    :param tmp_path: pytest temporary directory.
    """
    _path, newer, supported = asyncio.run(_too_new_db(tmp_path))
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith((_ENV_PREFIX, _LEGACY_ENV_PREFIX))
    }
    env["PYTHONUNBUFFERED"] = "1"

    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "chime_deck.cli",
            "--data-dir",
            str(tmp_path),
            "--port",
            "0",
            # Nothing may bind the admin listener's default 0.0.0.0:8081 here.
            "--no-admin",
        ],
        timeout=_CHILD_TIMEOUT_S,
        capture_output=True,
        text=True,
        env=env,
    )
    elapsed = time.monotonic() - started
    combined = completed.stdout + completed.stderr
    output = f"elapsed {elapsed:.2f}s\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"

    assert completed.returncode == SCHEMA_TOO_NEW_EXIT_CODE, output
    assert elapsed < _EXIT_BUDGET_S, output
    assert "database schema version" in combined, output
    assert str(newer) in combined, output
    assert str(supported) in combined, output
    assert "Traceback" not in combined, output
    assert "ResourceWarning" not in combined, output


def test_a_real_child_process_refuses_a_version_only_the_write_open_can_see(
    tmp_path: Path,
) -> None:
    """The acceptance test: a blind preflight still ends in one clean exit 78.

    The version is real and too new, and the read-only preflight provably
    returns without refusing. The port is already held by this test, so a child
    that reached uvicorn's bind would fail with an address clash instead of
    returning 78 — the exit status itself proves no listener was ever created.

    :param tmp_path: pytest temporary directory.
    """
    path, newer, supported = asyncio.run(_too_new_behind_a_hot_journal(tmp_path))
    journal = path.with_name(path.name + "-journal")
    check_schema_version(path)
    assert journal.is_file(), "the preflight attempt must not consume the journal"

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith((_ENV_PREFIX, _LEGACY_ENV_PREFIX))
    }
    env["PYTHONUNBUFFERED"] = "1"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        started = time.monotonic()
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "chime_deck.cli",
                "--data-dir",
                str(tmp_path),
                "--port",
                str(port),
                # Nothing may bind the admin listener's default 0.0.0.0:8081 here.
                "--no-admin",
            ],
            timeout=_CHILD_TIMEOUT_S,
            capture_output=True,
            text=True,
            env=env,
        )
        elapsed = time.monotonic() - started
    finally:
        listener.close()
    combined = completed.stdout + completed.stderr
    output = f"elapsed {elapsed:.2f}s\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    errors = [line for line in combined.splitlines() if " ERROR " in line]

    assert completed.returncode == SCHEMA_TOO_NEW_EXIT_CODE, output
    assert elapsed < _EXIT_BUDGET_S, output
    assert len(errors) == 1, output
    assert (
        f"database schema version {newer} is newer than the newest version this build "
        f"supports ({supported}): refusing to start."
    ) in errors[0], output
    assert "Upgrade ChimeDeck (git pull && uv sync)" in errors[0], output
    assert "Traceback" not in combined, output
    assert "Started server process" not in combined, output
    assert "Uvicorn running" not in combined, output
    assert "ResourceWarning" not in combined, output


@pytest.mark.asyncio
async def test_a_version_1_database_migrates_forward(tmp_path: Path) -> None:
    """A database this build finds older is upgraded in place, not refused.

    The old schema is built by hand from the shipped SQL: producing it with the
    current code would test nothing.

    :param tmp_path: pytest temporary directory.
    """
    path = tmp_path / "doorbell.db"
    script = (
        importlib.resources.files("chime_deck.store.migrations")
        .joinpath("001_initial.sql")
        .read_text(encoding="utf-8")
    )
    connection = await aiosqlite.connect(path)
    try:
        await connection.executescript(script)
        await connection.execute("PRAGMA user_version = 1")
        await connection.commit()
    finally:
        await connection.close()

    async with ConfigStore(path) as store:
        async with store.connection.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert int(row[0]) == 5
