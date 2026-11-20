"""Logging: the secret scrubber, formatters, the file sink and log-level precedence.

``logging`` is process-global state, so every test here runs under
:func:`log_isolation.preserve_log_state`, which restores the root logger, its
handlers and the log record factory and forgets any registered secret.

Every scrubbing assertion is a positive control: the ``[REDACTED]`` marker must
be present, so the test cannot pass because the text never reached the sink.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from log_isolation import preserve_log_state

import chime_deck.config
from chime_deck.app import AppState, create_app
from chime_deck.cli import build_parser
from chime_deck.config import (
    REDACTED,
    SECRET_SCRUBBER,
    ScrubbingFormatter,
    ServerConfig,
    apply_stored_log_level,
    configure_logging,
    install_log_scrubber,
    parse_env_flag,
    resolve_config,
)
from chime_deck.store import ConfigStore

TOKEN = "long-lived-token-0123456789abcdef"

_ENV_SUFFIXES = ("DATA", "DATA_DIR", "HOST", "LOG_FILE", "LOG_LEVEL", "PORT")

_ENV_KEYS = tuple(f"CHIMEDECK_{suffix}" for suffix in _ENV_SUFFIXES) + tuple(
    f"SUPER_DOORBELL_{suffix}" for suffix in _ENV_SUFFIXES
)
"""Every variable to clear before a test, new prefix first.

The pre-rename ``SUPER_DOORBELL_*`` names are cleared too: ``chime_deck.env``
still honours them for one release, so one left over on a developer's machine
would otherwise leak into the configuration under test.
"""

_LOGGER = logging.getLogger(__name__)


class _CapturingHandler(logging.Handler):
    """A handler that records the formatted output, for formatter-level tests."""

    def __init__(self, fmt: str, *, defaults: dict[str, str] | None = None) -> None:
        """Attach a :class:`ScrubbingFormatter` with *fmt*.

        :param fmt: format string, which may reference ``extra`` fields.
        :param defaults: default values for fields the format string names.
        """
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(ScrubbingFormatter(fmt, defaults=defaults or {}))

    def emit(self, record: logging.LogRecord) -> None:
        """Format and keep one record.

        :param record: record being handled.
        """
        self.lines.append(self.format(record))


class _RecordHandler(logging.Handler):
    """A handler that keeps the raw records, for factory-level tests."""

    def __init__(self) -> None:
        """Create an empty handler."""
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep one record unformatted.

        :param record: record being handled.
        """
        self.records.append(record)


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Keep the process-global logging state isolated per test."""
    with preserve_log_state():
        yield


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Return a monkeypatch that has cleared every ``CHIMEDECK_*`` variable.

    :param monkeypatch: pytest monkeypatch fixture.
    """
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


def _build_app(tmp_path: Path) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory.
    """
    config = ServerConfig(
        data_dir=tmp_path, static_dir=tmp_path / "no-dist", seed_demo_sounds=False
    )
    app = create_app(config)
    return app, cast(AppState, app.state.doorbell)


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield a client bound to it.

    :param app: application under test.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _flush_root_handlers() -> None:
    """Flush every handler on the root logger so file contents are complete."""
    for handler in logging.getLogger().handlers:
        handler.flush()


def _file_handlers(root: logging.Logger) -> list[logging.FileHandler]:
    """Return the file handlers attached to *root*.

    :param root: logger to inspect.
    """
    return [handler for handler in root.handlers if isinstance(handler, logging.FileHandler)]


def test_configure_logging_keeps_console_only_by_default() -> None:
    """Without ``log_file`` no file handler is attached."""
    root = logging.getLogger()
    root.handlers.clear()

    configure_logging("INFO")

    assert _file_handlers(root) == []


def test_configure_logging_creates_a_scrubbed_rotating_file(tmp_path: Path) -> None:
    """The file sink creates its directory and never stores a secret.

    :param tmp_path: pytest temporary directory.
    """
    log_file = tmp_path / "logs" / "doorbell.log"
    install_log_scrubber()
    SECRET_SCRUBBER.set_secrets([TOKEN])
    configure_logging("INFO", log_file=log_file)

    _LOGGER.info("token=%s", TOKEN)
    _LOGGER.info("password=%s", TOKEN)
    try:
        raise RuntimeError(f"auth {TOKEN}")
    except RuntimeError:
        _LOGGER.exception("connection failed")
    _flush_root_handlers()

    assert log_file.parent.is_dir()
    content = log_file.read_text(encoding="utf-8")
    assert TOKEN not in content
    assert REDACTED in content
    assert "token=[REDACTED]" in content
    assert "RuntimeError: auth [REDACTED]" in content
    assert "connection failed" in content


def test_file_sink_is_idempotent(tmp_path: Path) -> None:
    """Configuring the same log file twice leaves exactly one handler for it.

    :param tmp_path: pytest temporary directory.
    """
    log_file = tmp_path / "logs" / "doorbell.log"

    configure_logging("INFO", log_file=log_file)
    configure_logging("INFO", log_file=log_file)

    root = logging.getLogger()
    matching = [
        handler
        for handler in _file_handlers(root)
        if Path(handler.baseFilename).resolve() == log_file.resolve()
    ]
    assert len(matching) == 1


def test_file_sink_rotates_when_it_reaches_the_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reaching the size limit rotates the file instead of growing without bound.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    max_bytes = 256
    monkeypatch.setattr(chime_deck.config, "LOG_FILE_MAX_BYTES", max_bytes)
    log_file = tmp_path / "logs" / "doorbell.log"
    message = "x" * 64

    configure_logging("INFO", log_file=log_file)
    for index in range(40):
        _LOGGER.info("%s %02d", message, index)
    _flush_root_handlers()

    rotated = tmp_path / "logs" / "doorbell.log.1"
    assert rotated.is_file()
    assert rotated.stat().st_size > 0
    assert log_file.stat().st_size < max_bytes


def test_scrubber_redacts_fstring_percent_args_and_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every logging call shape is scrubbed, and the marker proves it.

    :param caplog: captured log records.
    """
    install_log_scrubber()
    SECRET_SCRUBBER.set_secrets([TOKEN])
    caplog.set_level(logging.DEBUG)

    _LOGGER.info(f"using {TOKEN}")
    _LOGGER.info("using %s", TOKEN)
    try:
        raise RuntimeError(f"auth {TOKEN}")
    except RuntimeError:
        _LOGGER.exception("failed")

    assert TOKEN not in caplog.text
    assert caplog.text.count(REDACTED) >= 3
    assert "RuntimeError: auth [REDACTED]" in caplog.text


def test_scrubber_redacts_extra_fields_rendered_by_the_formatter() -> None:
    """An ``extra`` field interpolated by the format string is scrubbed too."""
    SECRET_SCRUBBER.set_secrets([TOKEN])
    handler = _CapturingHandler("%(message)s %(context)s", defaults={"context": ""})
    logger = logging.getLogger("test_logging.extra")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.info("checking the broker", extra={"context": f"token={TOKEN}"})
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert len(handler.lines) == 1
    line = handler.lines[0]
    assert line.startswith("checking the broker ")
    assert "token=[REDACTED]" in line
    assert TOKEN not in line


@pytest.mark.asyncio
async def test_scrubber_masks_values_written_through_the_settings_api(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A secret written through the API is registered literally with the scrubber.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    install_log_scrubber()
    caplog.set_level(logging.INFO)
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch("/api/settings", json={"ha.token": TOKEN})
        assert response.status_code == 200

        # No "token=" shape here: only the literal registration can redact it.
        _LOGGER.info("using %s", TOKEN)

    assert TOKEN not in caplog.text
    assert REDACTED in caplog.text


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer abcdef123456", "abcdef123456"),
        ("password=hunter2", "hunter2"),
        ("api_key: s3cret", "s3cret"),
        ("http://user:hunter2@broker.local:1883/", "user:hunter2"),
    ],
)
def test_scrubber_patterns_catch_unregistered_credentials(text: str, secret: str) -> None:
    """Credentials are recognised by shape even when no value was ever registered.

    :param text: logged text containing a credential.
    :param secret: part of the text that must not survive.
    """
    assert SECRET_SCRUBBER.scrub(text) != text
    assert secret not in SECRET_SCRUBBER.scrub(text)
    assert REDACTED in SECRET_SCRUBBER.scrub(text)


def test_scrubber_leaves_ordinary_text_alone() -> None:
    """A sentence with no credential in it is returned unchanged."""
    text = "the broker is reachable"

    assert SECRET_SCRUBBER.scrub(text) == text


def test_scrubber_repr_and_short_values() -> None:
    """The repr never lists values, and very short values are not redacted."""
    SECRET_SCRUBBER.set_secrets(["abc", TOKEN])

    assert repr(SECRET_SCRUBBER) == "SecretScrubber(registered=1)"
    assert SECRET_SCRUBBER.scrub("abc is short") == "abc is short"
    assert SECRET_SCRUBBER.scrub(f"long {TOKEN}") == f"long {REDACTED}"


def test_record_is_left_untouched_when_nothing_matches() -> None:
    """An unchanged record keeps its format string and args byte-identical."""
    install_log_scrubber()
    handler = _RecordHandler()
    logger = logging.getLogger("test_logging.record")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.info("hello %s", "world")
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert len(handler.records) == 1
    record = handler.records[0]
    assert record.msg == "hello %s"
    assert record.args == ("world",)


def test_install_log_scrubber_is_idempotent() -> None:
    """Installing twice keeps the first factory, and that factory really scrubs."""
    before = logging.getLogRecordFactory()

    install_log_scrubber()
    installed = logging.getLogRecordFactory()
    install_log_scrubber()

    assert installed is not before
    assert logging.getLogRecordFactory() is installed

    SECRET_SCRUBBER.set_secrets([TOKEN])
    record = installed(
        "test_logging.factory", logging.INFO, __file__, 1, f"token {TOKEN}", None, None
    )
    assert TOKEN not in record.getMessage()
    assert REDACTED in record.getMessage()


def test_formatter_survives_a_broken_scrubber(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scrubber that raises cannot break formatting or leak a logging error.

    :param monkeypatch: pytest monkeypatch fixture.
    """

    def _raising(_text: str) -> str:
        raise RuntimeError("scrubber failure")

    monkeypatch.setattr(SECRET_SCRUBBER, "scrub", _raising)
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello %s", ("world",), None)

    assert ScrubbingFormatter("%(message)s").format(record) == "hello world"


@pytest.mark.asyncio
async def test_apply_stored_log_level_uses_the_setting_as_a_fallback(
    tmp_path: Path, store: ConfigStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The stored level applies only when the CLI and the environment said nothing.

    :param tmp_path: pytest temporary directory.
    :param store: connected store backed by a temporary database.
    :param caplog: captured log records.
    """
    await store.set_setting("system.log_level", "debug")
    root = logging.getLogger()
    root.setLevel(logging.WARNING)

    explicit = await apply_stored_log_level(
        ServerConfig(data_dir=tmp_path, log_level_explicit=True), store
    )
    assert explicit is None
    assert root.level == logging.WARNING

    applied = await apply_stored_log_level(ServerConfig(data_dir=tmp_path), store)
    assert applied == "DEBUG"
    assert root.level == logging.DEBUG

    async with store.connection.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("system.log_level", json.dumps("chatty"), "2026-01-01T00:00:00.000000Z"),
    ) as cursor:
        await cursor.close()
    root.setLevel(logging.WARNING)

    ignored = await apply_stored_log_level(ServerConfig(data_dir=tmp_path), store)

    assert ignored is None
    assert root.level == logging.WARNING
    assert any("chatty" in record.getMessage() for record in caplog.records)
    assert any(record.levelno == logging.WARNING for record in caplog.records)


@pytest.mark.parametrize("raw", ["1", "true", "ON", "yes"])
def test_parse_env_flag_accepts_the_documented_truthy_values(raw: str) -> None:
    """Truthy spellings enable the switch, case and space insensitive.

    :param raw: raw environment value.
    """
    assert parse_env_flag("CHIMEDECK_LOG_FILE", raw) is True


@pytest.mark.parametrize("raw", ["0", "false", "off", ""])
def test_parse_env_flag_accepts_the_documented_falsy_values(raw: str) -> None:
    """Falsy spellings disable the switch.

    :param raw: raw environment value.
    """
    assert parse_env_flag("CHIMEDECK_LOG_FILE", raw) is False


def test_parse_env_flag_defaults_to_false_and_names_the_variable() -> None:
    """An unset variable is off, and an unknown value names the variable."""
    assert parse_env_flag("CHIMEDECK_LOG_FILE", None) is False

    with pytest.raises(ValueError) as exc_info:
        parse_env_flag("CHIMEDECK_LOG_FILE", "maybe")

    assert "CHIMEDECK_LOG_FILE" in str(exc_info.value)


def test_resolve_config_reads_the_log_file_switch(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``CHIMEDECK_LOG_FILE`` turns the file sink on; the default path is fixed.

    :param env: isolated environment.
    :param tmp_path: pytest temporary directory.
    """
    parser = build_parser()

    off = resolve_config(parser.parse_args(["--data-dir", str(tmp_path)]))
    assert off.log_file is False
    assert off.log_file_path is None

    env.setenv("CHIMEDECK_LOG_FILE", "1")
    on = resolve_config(parser.parse_args(["--data-dir", str(tmp_path)]))
    assert on.log_file is True
    assert on.log_file_path == tmp_path / "logs" / "chime-deck.log"

    assert ServerConfig(data_dir=tmp_path, log_file=True).log_file_path == (
        tmp_path / "logs" / "chime-deck.log"
    )
    assert ServerConfig(data_dir=tmp_path).log_file_path is None
