"""Schema, PRAGMA and migration behaviour of the configuration store."""

from __future__ import annotations

import importlib.resources
import logging
from pathlib import Path

import aiosqlite
import pytest

from chime_deck.store import ConfigStore, EntityIn, default_db_path

pytestmark = pytest.mark.asyncio

_DATA_ENV_NAMES = (
    "CHIMEDECK_DATA",
    "CHIMEDECK_DATA_DIR",
    "SUPER_DOORBELL_DATA",
    "SUPER_DOORBELL_DATA_DIR",
)
"""Data-directory variables to clear, new prefix first.

The pre-rename ``SUPER_DOORBELL_*`` names are cleared too: ``chime_deck.env``
still honours them for one release, so one left over on a developer's machine
would otherwise redirect the default database path.
"""


def _clear_data_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every data-directory variable, new spelling and legacy one.

    :param monkeypatch: pytest monkeypatch fixture.
    """
    for name in _DATA_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "settings": ("key", "value", "updated_at"),
    "entities": (
        "id",
        "entity_id",
        "display_name",
        "icon",
        "accent_color",
        "show_attributes",
        "hide_when_unavailable",
        "sort_order",
        "created_at",
        "state_tones",
        "colour_by_state",
    ),
    "sounds": (
        "id",
        "name",
        "source_type",
        "path",
        "url",
        "sha256",
        "format",
        "duration_ms",
        "bytes",
        "missing",
        "created_at",
    ),
    "buttons": (
        "id",
        "slug",
        "name",
        "icon",
        "action_type",
        "sound_id",
        "tts_text",
        "tts_engine",
        "baked_sha",
        "baked_at",
        "baked_error",
        "volume_override",
        "dedupe_seconds",
        "show_on_dashboard",
        "quiet_hours_exempt",
        "enabled",
        "sort_order",
        "created_at",
        "updated_at",
        "baked_format",
    ),
    "press_log": (
        "id",
        "button_id",
        "button_name",
        "pressed_at",
        "source",
        "outcome",
        "detail",
        "sound_name",
        "duration_ms",
        "job_id",
        "latency_ms",
        "synthesis_ms",
    ),
}


async def _fetch_all(
    connection: aiosqlite.Connection, sql: str, parameters: tuple[object, ...] = ()
) -> list[aiosqlite.Row]:
    """Fetch every row of a query.

    :param connection: open store connection.
    :param sql: query text.
    :param parameters: bound parameters.
    """
    async with connection.execute(sql, parameters) as cursor:
        return list(await cursor.fetchall())


async def _scalar(connection: aiosqlite.Connection, sql: str) -> object:
    """Return the first column of the first row of a query.

    :param connection: open store connection.
    :param sql: query text.
    """
    rows = await _fetch_all(connection, sql)
    assert rows
    return rows[0][0]


async def _scalar_int(connection: aiosqlite.Connection, sql: str) -> int:
    """Return an integer scalar, asserting that SQLite really produced one.

    :param connection: open store connection.
    :param sql: query text.
    """
    value = await _scalar(connection, sql)
    assert isinstance(value, int)
    return value


async def test_fresh_database_has_expected_schema(tmp_path: Path) -> None:
    """A fresh file gets all tables, columns, indexes and version 5."""
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        connection = store.connection

        tables = {
            str(row[0])
            for row in await _fetch_all(
                connection, "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert set(EXPECTED_COLUMNS) <= tables

        for table, expected in EXPECTED_COLUMNS.items():
            rows = await _fetch_all(connection, f"PRAGMA table_info({table})")
            assert tuple(str(row["name"]) for row in rows) == expected

        indexes = {
            str(row[0])
            for row in await _fetch_all(
                connection,
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name NOT LIKE 'sqlite_autoindex%'",
            )
        }
        assert indexes == {"sounds_unique_file", "press_log_pressed_at"}
        assert await _scalar_int(connection, "PRAGMA user_version") == 5


async def test_connection_pragmas_are_applied(tmp_path: Path) -> None:
    """WAL mode and foreign keys are enabled on every connection."""
    path = tmp_path / "doorbell.db"
    async with ConfigStore(path) as store:
        connection = store.connection
        assert str(await _scalar(connection, "PRAGMA journal_mode")).lower() == "wal"
        assert await _scalar_int(connection, "PRAGMA foreign_keys") == 1

    async with ConfigStore(path) as second:
        connection = second.connection
        assert str(await _scalar(connection, "PRAGMA journal_mode")).lower() == "wal"
        assert await _scalar_int(connection, "PRAGMA foreign_keys") == 1


async def test_reopen_does_not_reapply_migrations(tmp_path: Path) -> None:
    """Reopening keeps ``user_version`` and leaves existing rows intact."""
    path = tmp_path / "doorbell.db"
    async with ConfigStore(path) as store:
        await store.upsert_entity(EntityIn(entity_id="lock.front_door"))
        before = await store.list_entities()

    async with ConfigStore(path) as reopened:
        assert await _scalar_int(reopened.connection, "PRAGMA user_version") == 5
        after = await reopened.list_entities()
        assert [entity.entity_id for entity in after] == [entity.entity_id for entity in before]


async def test_deleting_database_file_recreates_it(tmp_path: Path) -> None:
    """A deleted database file is recreated with an empty schema."""
    path = tmp_path / "doorbell.db"
    async with ConfigStore(path) as store:
        await store.upsert_entity(EntityIn(entity_id="lock.front_door"))

    path.unlink()
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)

    async with ConfigStore(path) as recreated:
        assert await recreated.list_entities() == []
        assert await _scalar_int(recreated.connection, "PRAGMA user_version") == 5


async def test_migrations_are_discovered_in_version_order(tmp_path: Path) -> None:
    """The shipped migrations are found through package resources."""
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        assert store._db._discover_migrations() == [
            (1, "001_initial.sql"),
            (2, "002_press_log_playback.sql"),
            (3, "003_bake_columns.sql"),
            (4, "004_press_log_synthesis.sql"),
            (5, "005_entity_state_tones.sql"),
        ]


async def test_version_1_database_is_upgraded_in_place(tmp_path: Path) -> None:
    """An existing version-1 database gains the playback columns without losing rows.

    The old database is built by hand from the shipped SQL, not by running
    :class:`ConfigStore` on it: using the current code to produce the "old"
    schema would test nothing.

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
        await connection.execute(
            "INSERT INTO press_log (button_id, button_name, pressed_at, source, outcome) "
            "VALUES (NULL, 'Older', '2026-01-01T10:00:00.000000Z', 'mqtt', 'played')"
        )
        await connection.execute(
            "INSERT INTO press_log (button_id, button_name, pressed_at, source, outcome) "
            "VALUES (NULL, 'Newer', '2026-01-01T11:00:00.000000Z', 'mqtt', 'failed')"
        )
        await connection.execute("PRAGMA user_version = 1")
        await connection.commit()
    finally:
        await connection.close()

    async with ConfigStore(path) as store:
        database = store.connection
        columns = tuple(
            str(row["name"]) for row in await _fetch_all(database, "PRAGMA table_info(press_log)")
        )
        assert columns[-3:] == ("job_id", "latency_ms", "synthesis_ms")
        assert await _scalar_int(database, "PRAGMA user_version") == 5

        entries = await store.recent_presses()
        assert [entry.button_name for entry in entries] == ["Newer", "Older"]
        assert all(entry.job_id is None for entry in entries)
        assert all(entry.latency_ms is None for entry in entries)
        assert all(entry.synthesis_ms is None for entry in entries)


async def test_version_2_database_is_upgraded_in_place(tmp_path: Path) -> None:
    """An existing version-2 database gains ``baked_format`` without losing rows.

    The old database is built by hand from the two shipped SQL files, not by
    running :class:`ConfigStore` on it: using the current code to produce the
    "old" schema would test nothing. The column is appended by
    ``ALTER TABLE ADD COLUMN``, so it is asserted **last**, and the index set is
    unchanged.

    :param tmp_path: pytest temporary directory.
    """
    path = tmp_path / "doorbell.db"
    resources = importlib.resources.files("chime_deck.store.migrations")
    script = "".join(
        resources.joinpath(name).read_text(encoding="utf-8")
        for name in ("001_initial.sql", "002_press_log_playback.sql")
    )
    connection = await aiosqlite.connect(path)
    try:
        await connection.executescript(script)
        await connection.execute(
            "INSERT INTO buttons (slug, name, action_type, tts_text, created_at, updated_at) "
            "VALUES ('front-door', 'Front Door', 'tts', 'Hello there', "
            "'2026-01-01T10:00:00.000000Z', '2026-01-01T10:00:00.000000Z')"
        )
        await connection.execute("PRAGMA user_version = 2")
        await connection.commit()
    finally:
        await connection.close()

    async with ConfigStore(path) as store:
        database = store.connection
        columns = tuple(
            str(row["name"]) for row in await _fetch_all(database, "PRAGMA table_info(buttons)")
        )
        assert columns[-1] == "baked_format"
        assert await _scalar_int(database, "PRAGMA user_version") == 5

        buttons = await store.list_buttons()
        assert [button.slug for button in buttons] == ["front-door"]
        assert buttons[0].baked_format is None


async def test_version_3_database_is_upgraded_in_place(tmp_path: Path) -> None:
    """An existing version-3 database gains ``synthesis_ms`` without losing a press row.

    The old database is built by hand from the three shipped SQL files, not by
    running :class:`ConfigStore` on it: using the current code to produce the
    "old" schema would test nothing. The column is appended by
    ``ALTER TABLE ADD COLUMN``, so it is asserted **last**, the existing row
    survives with a ``NULL`` measurement (nothing synthesised for it), and the
    index set is unchanged.

    :param tmp_path: pytest temporary directory.
    """
    path = tmp_path / "doorbell.db"
    resources = importlib.resources.files("chime_deck.store.migrations")
    script = "".join(
        resources.joinpath(name).read_text(encoding="utf-8")
        for name in (
            "001_initial.sql",
            "002_press_log_playback.sql",
            "003_bake_columns.sql",
        )
    )
    connection = await aiosqlite.connect(path)
    try:
        await connection.executescript(script)
        await connection.execute(
            "INSERT INTO press_log (button_id, button_name, pressed_at, source, outcome) "
            "VALUES (NULL, 'Front Door', '2026-01-01T10:00:00.000000Z', 'mqtt', 'queued')"
        )
        await connection.execute("PRAGMA user_version = 3")
        await connection.commit()
    finally:
        await connection.close()

    async with ConfigStore(path) as store:
        database = store.connection
        columns = tuple(
            str(row["name"]) for row in await _fetch_all(database, "PRAGMA table_info(press_log)")
        )
        assert columns[-1] == "synthesis_ms"
        assert await _scalar_int(database, "PRAGMA user_version") == 5

        indexes = {
            str(row[0])
            for row in await _fetch_all(
                database,
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name NOT LIKE 'sqlite_autoindex%'",
            )
        }
        assert indexes == {"sounds_unique_file", "press_log_pressed_at"}

        entries = await store.recent_presses()
        assert [entry.button_name for entry in entries] == ["Front Door"]
        assert entries[0].synthesis_ms is None


async def test_version_4_database_is_upgraded_in_place(tmp_path: Path) -> None:
    """An existing version-4 database gains the tone columns without losing an entity.

    The old database is built by hand from the four shipped SQL files, not by
    running :class:`ConfigStore` on it: using the current code to produce the
    "old" schema would test nothing. Both columns are appended by
    ``ALTER TABLE ADD COLUMN``, so they are asserted **last**, the row written
    before the upgrade survives untouched (no overrides, colour on), and the
    index set is unchanged.

    :param tmp_path: pytest temporary directory.
    """
    path = tmp_path / "doorbell.db"
    resources = importlib.resources.files("chime_deck.store.migrations")
    script = "".join(
        resources.joinpath(name).read_text(encoding="utf-8")
        for name in (
            "001_initial.sql",
            "002_press_log_playback.sql",
            "003_bake_columns.sql",
            "004_press_log_synthesis.sql",
        )
    )
    connection = await aiosqlite.connect(path)
    try:
        await connection.executescript(script)
        await connection.execute(
            "INSERT INTO entities (entity_id, display_name, show_attributes, sort_order, "
            "created_at) VALUES ('lock.front_door', 'Front Door', '[\"battery_level\"]', "
            "10, '2026-01-01T10:00:00.000000Z')"
        )
        await connection.execute("PRAGMA user_version = 4")
        await connection.commit()
    finally:
        await connection.close()

    async with ConfigStore(path) as store:
        database = store.connection
        columns = tuple(
            str(row["name"]) for row in await _fetch_all(database, "PRAGMA table_info(entities)")
        )
        assert columns[-2:] == ("state_tones", "colour_by_state")
        assert await _scalar_int(database, "PRAGMA user_version") == 5

        indexes = {
            str(row[0])
            for row in await _fetch_all(
                database,
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name NOT LIKE 'sqlite_autoindex%'",
            )
        }
        assert indexes == {"sounds_unique_file", "press_log_pressed_at"}

        entities = await store.list_entities()
        assert [entity.entity_id for entity in entities] == ["lock.front_door"]
        assert entities[0].display_name == "Front Door"
        assert entities[0].show_attributes == ("battery_level",)
        assert entities[0].state_tones is None
        assert entities[0].colour_by_state is True


async def test_default_db_path_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CHIMEDECK_DATA_DIR`` wins over ``CHIMEDECK_DATA``."""
    _clear_data_env(monkeypatch)
    assert default_db_path() == Path("data") / "doorbell.db"

    monkeypatch.setenv("CHIMEDECK_DATA", "/srv/data")
    assert default_db_path() == Path("/srv/data") / "doorbell.db"

    monkeypatch.delenv("CHIMEDECK_DATA")
    monkeypatch.setenv("CHIMEDECK_DATA_DIR", "/srv/data_dir")
    assert default_db_path() == Path("/srv/data_dir") / "doorbell.db"

    monkeypatch.setenv("CHIMEDECK_DATA", "/srv/data")
    assert default_db_path() == Path("/srv/data_dir") / "doorbell.db"


async def test_the_new_data_dir_env_name_is_canonical(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``CHIMEDECK_DATA_DIR`` selects the directory and logs no deprecation.

    :param monkeypatch: pytest monkeypatch fixture.
    :param caplog: captured log records.
    """
    _clear_data_env(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="chime_deck.env")
    monkeypatch.setenv("CHIMEDECK_DATA_DIR", "/srv/canonical")

    assert default_db_path() == Path("/srv/canonical") / "doorbell.db"

    deprecations = [
        record.getMessage()
        for record in caplog.records
        if record.name == "chime_deck.env" and "deprecated" in record.getMessage()
    ]
    assert deprecations == []


async def test_the_pre_rename_data_dir_env_name_still_selects_the_directory(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A unit still exporting the old name must not fall back to ``./data``.

    This is the production hazard: an upgraded installation whose systemd unit
    still sets the pre-rename variable would otherwise open a fresh, empty
    database and orphan the live one.

    :param monkeypatch: pytest monkeypatch fixture.
    :param caplog: captured log records.
    """
    _clear_data_env(monkeypatch)
    caplog.set_level(logging.WARNING, logger="chime_deck.env")
    monkeypatch.setenv("SUPER_DOORBELL_DATA_DIR", "/srv/legacy")

    assert default_db_path() == Path("/srv/legacy") / "doorbell.db"

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "chime_deck.env" and record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1
    assert "SUPER_DOORBELL_DATA_DIR" in warnings[0]
    assert "CHIMEDECK_DATA_DIR" in warnings[0]


async def test_the_new_data_dir_env_name_wins_when_both_are_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The canonical name wins, and the log names the ignored legacy one.

    :param monkeypatch: pytest monkeypatch fixture.
    :param caplog: captured log records.
    """
    _clear_data_env(monkeypatch)
    caplog.set_level(logging.WARNING, logger="chime_deck.env")
    monkeypatch.setenv("CHIMEDECK_DATA_DIR", "/srv/canonical")
    monkeypatch.setenv("SUPER_DOORBELL_DATA_DIR", "/srv/legacy")

    assert default_db_path() == Path("/srv/canonical") / "doorbell.db"

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "chime_deck.env" and record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1
    assert "SUPER_DOORBELL_DATA_DIR" in warnings[0]
    assert "CHIMEDECK_DATA_DIR" in warnings[0]
    assert "the new name wins" in warnings[0]
