"""SQLite connection ownership, PRAGMAs, migrations and transaction mechanics.

``Database`` is the only place that talks to :mod:`aiosqlite` directly. It
keeps a single connection in autocommit mode so explicit ``BEGIN IMMEDIATE``
transactions work, applies the WAL and foreign-key PRAGMAs on every connect,
and applies numbered SQL migrations with :data:`sqlite3.user_version`.

A database written by a newer build is never downgraded: :meth:`Database._migrate`
raises :class:`SchemaTooNewError` before applying anything, and
:func:`check_schema_version` offers a best-effort read-only preflight so the CLI
can refuse to start with one clean log line.

Transaction nesting is lexical and tracked in a :class:`contextvars.ContextVar`
so concurrent asyncio tasks never share depth or buffered events. The pending
event buffer is also a ``ContextVar``: a plain instance list would let one
task drain another task's events.

Owning ticket: HSD-2sem.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import logging
import re
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, closing
from contextvars import ContextVar
from pathlib import Path

import aiosqlite

from ..env import env_value
from .errors import MigrationError, SchemaTooNewError, StoreNotConnected, WriteError
from .events import ConfigEvent

__all__ = ["Database", "check_schema_version", "default_db_path", "latest_schema_version"]

_MIGRATION_NAME = re.compile(r"^(?P<version>\d{3})_(?P<label>[A-Za-z0-9_-]+)\.sql$")
_MIGRATIONS_PACKAGE = "chime_deck.store.migrations"

EventSink = Callable[[ConfigEvent], Awaitable[None]]


def default_db_path() -> Path:
    """Return the default database path.

    ``$CHIMEDECK_DATA_DIR`` (the deployment contract from design
    06-deployment.md) wins over ``$CHIMEDECK_DATA`` (design 01 §1); when
    neither is set or one is empty it falls back to the next source and finally
    to ``./data``. The database file is always named ``doorbell.db``. This is a
    pure function of the environment; callers that need an explicit path (such
    as ``ConfigStore``) pass it themselves, which is what HSD-8gft decides.

    The pre-rename data-directory variables are still accepted for one release
    with a deprecation warning, and the new names win when both are set (see
    :mod:`chime_deck.env` and ``docs/installing.md``); the filename stays
    ``doorbell.db`` on purpose so an existing database is found rather than
    recreated under a new name.
    """
    configured = env_value("DATA_DIR") or env_value("DATA")
    data_dir = Path(configured) if configured else Path("data")
    return data_dir / "doorbell.db"


class Database:
    """Own the SQLite connection, schema version and write transactions."""

    def __init__(self, path: Path | str) -> None:
        """Create a database handle without opening a connection.

        :param path: filesystem path of the SQLite file.
        """
        self._path = Path(path)
        self._connection: aiosqlite.Connection | None = None
        self._tx_depth: ContextVar[int] = ContextVar(f"tx_depth_{id(self)}", default=0)
        self._pending_events: ContextVar[tuple[ConfigEvent, ...]] = ContextVar(
            f"pending_events_{id(self)}", default=()
        )
        self._write_lock = asyncio.Lock()
        self._event_sink: EventSink | None = None
        self._logger = logging.getLogger(__name__)

    @property
    def path(self) -> Path:
        """Return the configured database file path."""
        return self._path

    @property
    def connection(self) -> aiosqlite.Connection:
        """Return the open connection.

        :raises StoreNotConnected: if :meth:`connect` has not been called.
        """
        if self._connection is None:
            raise StoreNotConnected("database is not connected")
        return self._connection

    def set_event_sink(self, sink: EventSink) -> None:
        """Install the async callable that receives committed events.

        :param sink: usually :meth:`EventBus.publish`.
        """
        self._event_sink = sink

    def emit(self, event: ConfigEvent) -> None:
        """Buffer an event for publication after the outermost commit.

        Must be called inside a transaction. Events buffered by a transaction
        that rolls back are discarded without being published.

        :param event: event to publish once the write commits.
        """
        self._pending_events.set((*self._pending_events.get(), event))

    async def connect(self) -> None:
        """Open the database, apply PRAGMAs and run pending migrations.

        The directory is created if necessary, the connection uses
        ``autocommit`` so explicit transactions work, and calling this twice
        returns immediately.
        """
        if self._connection is not None:
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self._path, isolation_level=None)
        connection.row_factory = aiosqlite.Row
        self._connection = connection
        try:
            await self._apply_pragmas(connection)
            await self._migrate(connection)
        except BaseException:
            self._connection = None
            await connection.close()
            raise

    async def close(self) -> None:
        """Close the connection, if any."""
        connection = self._connection
        self._connection = None
        if connection is not None:
            await connection.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run a write transaction, nesting safely inside an existing one.

        Only the outermost transaction takes the write lock, issues ``BEGIN
        IMMEDIATE``, commits or rolls back, and flushes buffered events. A
        nested transaction reuses the same connection and never commits. A
        failed ``COMMIT`` is rolled back and reported as
        :class:`~chime_deck.store.errors.WriteError`, so the connection is
        never left inside an open transaction.

        :yields: the connection bound to this transaction.
        """
        connection = self.connection
        depth = self._tx_depth.get()
        if depth > 0:
            depth_token = self._tx_depth.set(depth + 1)
            try:
                yield connection
            finally:
                self._tx_depth.reset(depth_token)
            return

        committed_events: tuple[ConfigEvent, ...] = ()
        async with self._write_lock:
            events_token = self._pending_events.set(())
            depth_token = self._tx_depth.set(1)
            try:
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                except BaseException:
                    await self._rollback_quietly(connection)
                    raise
                try:
                    await connection.execute("COMMIT")
                except sqlite3.Error as exc:
                    await self._rollback_quietly(connection)
                    raise WriteError("write transaction commit failed") from exc
                committed_events = self._pending_events.get()
            finally:
                self._pending_events.reset(events_token)
                self._tx_depth.reset(depth_token)

        await self._publish(committed_events)

    async def _rollback_quietly(self, connection: aiosqlite.Connection) -> None:
        """Roll back without letting a driver error mask the real failure.

        A ``COMMIT`` that already failed may have closed the transaction, and a
        broken connection may fail the ``ROLLBACK`` too. Either way the
        exception that triggered the rollback is the one callers must see.

        :param connection: connection bound to the failing transaction.
        """
        try:
            await connection.execute("ROLLBACK")
        except sqlite3.Error:
            self._logger.debug("ROLLBACK failed after a transaction error", exc_info=True)

    async def _publish(self, events: tuple[ConfigEvent, ...]) -> None:
        """Publish committed events, logging and swallowing sink failures.

        :param events: events captured from the committed transaction.
        """
        sink = self._event_sink
        if sink is None:
            return
        for event in events:
            try:
                await sink(event)
            except Exception:
                self._logger.exception("config event publish failed for %s", event.name)

    async def _apply_pragmas(self, connection: aiosqlite.Connection) -> None:
        """Apply the PRAGMAs that must be set before any transaction starts.

        :param connection: freshly opened connection.
        """
        async with connection.execute("PRAGMA journal_mode=WAL") as cursor:
            await cursor.fetchone()
        await connection.execute("PRAGMA synchronous=NORMAL")
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute("PRAGMA busy_timeout=5000")

    async def _migrate(self, connection: aiosqlite.Connection) -> None:
        """Apply every discovered migration newer than ``user_version``.

        Each file runs in its own transaction and records its number in
        ``PRAGMA user_version`` before committing.

        :param connection: open connection with PRAGMAs already applied.
        """
        current = await self._user_version(connection)
        migrations = self._discover_migrations()
        newest = migrations[-1][0] if migrations else 0
        if current > newest:
            # The CLI's preflight is best effort: a bump in an un-checkpointed
            # WAL can be invisible to a read-only open, so this is the real
            # guarantee that a newer database is never downgraded.
            raise SchemaTooNewError(current, newest)
        for version, filename in migrations:
            if version <= current:
                continue
            script = (
                importlib.resources.files(_MIGRATIONS_PACKAGE)
                .joinpath(filename)
                .read_text(encoding="utf-8")
            )
            statements = _split_statements(script, source=filename)
            async with self.transaction():
                for statement in statements:
                    await connection.execute(statement)
                await connection.execute(f"PRAGMA user_version = {version:d}")
            current = version

    async def _user_version(self, connection: aiosqlite.Connection) -> int:
        """Read the schema version stored in ``PRAGMA user_version``.

        :param connection: open connection.
        """
        async with connection.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        if row is None:
            return 0
        return int(row[0])

    def _discover_migrations(self) -> list[tuple[int, str]]:
        """Return the migration files sorted by version.

        Thin delegate to :func:`_discover_migrations`, kept because callers and
        tests reach it through the database handle.
        """
        return _discover_migrations()


def _discover_migrations() -> list[tuple[int, str]]:
    """Return migration files sorted by version.

    Both duplicate version numbers and ``.sql`` files whose names do not
    match ``NNN_label.sql`` raise :class:`MigrationError`: silently skipping
    either would leave the schema half-upgraded.

    :raises MigrationError: if a SQL migration file name is malformed or a
        version number appears twice.
    """
    package = importlib.resources.files(_MIGRATIONS_PACKAGE)
    found: dict[int, str] = {}
    for entry in package.iterdir():
        if entry.is_dir() or not entry.name.endswith(".sql"):
            continue
        match = _MIGRATION_NAME.fullmatch(entry.name)
        if match is None:
            raise MigrationError(f"malformed migration file name: {entry.name}")
        version = int(match.group("version"))
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version}: {found[version]} and {entry.name}"
            )
        found[version] = entry.name
    return sorted(found.items())


def latest_schema_version() -> int:
    """Return the newest schema version this build ships, or ``0`` for none."""
    migrations = _discover_migrations()
    return migrations[-1][0] if migrations else 0


def check_schema_version(path: Path | str) -> None:
    """Refuse a database file written by a newer build, without creating it.

    This is a **best-effort** preflight for the CLI: it opens the file
    read-only, so a fresh install (no file yet) passes and is never created by
    the check itself. :meth:`Database._migrate` is the real guarantee, because a
    ``user_version`` bump still sitting in an un-checkpointed WAL can be
    invisible to a read-only open.

    Any :class:`sqlite3.Error` or :class:`OSError` other than a too-new schema is
    swallowed: :meth:`Database.connect` reports those with far better context.

    :param path: filesystem path of the SQLite file.
    :raises SchemaTooNewError: if the file's schema version exceeds
        :func:`latest_schema_version`.
    """
    database_path = Path(path)
    if not database_path.exists():
        return
    supported = latest_schema_version()
    try:
        uri = f"{database_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            row = connection.execute("PRAGMA user_version").fetchone()
        current = int(row[0]) if row is not None else 0
    except (sqlite3.Error, OSError):
        return
    if current > supported:
        raise SchemaTooNewError(current, supported)


def _split_statements(script: str, *, source: str) -> list[str]:
    """Split a migration script into individual statements.

    ``executescript`` implicitly commits, which would break the one-transaction
    per migration guarantee, so statements are accumulated until the standard
    library considers them complete. Comment-only trailing text is ignored.

    :param script: migration file contents.
    :param source: file name used in error messages.
    :raises MigrationError: if the file ends with an incomplete statement.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines():
        buffer = f"{buffer}\n{line}" if buffer else line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""

    remainder = buffer.strip()
    if remainder and not _only_comments(remainder):
        raise MigrationError(f"incomplete statement at end of {source}")
    return statements


def _only_comments(text: str) -> bool:
    """Return whether every line in ``text`` is blank or a SQL comment.

    :param text: trailing text left after the last complete statement.
    """
    return all(not line.strip() or line.strip().startswith("--") for line in text.splitlines())
