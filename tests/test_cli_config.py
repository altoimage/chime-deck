"""CLI parsing, configuration precedence and logging setup.

Every test here asks for the ``env`` fixture, which removes the
``CHIMEDECK_*`` variables first so the ambient environment of whoever runs
the suite cannot change the expected precedence.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from chime_deck.cli import GRACEFUL_SHUTDOWN_TIMEOUT_S, build_parser, main, run_renderer
from chime_deck.config import (
    DEFAULT_ADMIN_HOST,
    DEFAULT_ADMIN_PORT,
    DEFAULT_HOST,
    DEFAULT_LOG_LEVEL,
    DEFAULT_PORT,
    DEFAULT_ROLE,
    ServerConfig,
    admin_bind,
    configure_logging,
    default_static_dir,
    resolve_config,
)
from chime_deck.renderer import RENDERER_CONFIG_EXIT_CODE
from chime_deck.store import ConfigStore

_ENV_SUFFIXES = (
    "ADMIN",
    "ADMIN_HOST",
    "ADMIN_PORT",
    "DATA",
    "DATA_DIR",
    "HOST",
    "LOG_FILE",
    "LOG_LEVEL",
    "PORT",
)

_ENV_KEYS = tuple(f"CHIMEDECK_{suffix}" for suffix in _ENV_SUFFIXES) + tuple(
    f"SUPER_DOORBELL_{suffix}" for suffix in _ENV_SUFFIXES
)
"""Every variable to clear before a test, new prefix first.

The pre-rename ``SUPER_DOORBELL_*`` names are cleared too: ``chime_deck.env``
still honours them for one release, so one left over on a developer's machine
would otherwise change the precedence a test observes.
"""

_TOKEN = "s3cret-renderer-token"
"""Token the renderer-role tests seed; it must never reach a log line or a repr."""

_RENDER_ROUTE = "/api/tts/render"
"""The renderer's one action route, used to prove the right app was launched."""


def _resolve(argv: list[str]) -> ServerConfig:
    """Parse *argv* with the real parser and resolve the server configuration.

    :param argv: command line arguments, without the program name.
    """
    return resolve_config(build_parser().parse_args(argv))


def _forbid_uvicorn(*_args: object, **_kwargs: object) -> None:
    """Fail the test if anything asks uvicorn to bind."""
    raise AssertionError("uvicorn.run must not be reached")


async def _seed_renderer_token(config: ServerConfig) -> None:
    """Store the renderer token in *config*'s database, as an operator would.

    :param config: resolved renderer configuration naming the database.
    """
    async with ConfigStore(config.database_path) as store:
        await store.set_setting("tts.renderer.token", _TOKEN)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Return a monkeypatch that has cleared every ``CHIMEDECK_*`` variable.

    :param monkeypatch: pytest monkeypatch fixture.
    """
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def root_logger() -> Iterator[logging.Logger]:
    """Yield the root logger and restore its level, handlers and record factory.

    ``configure_logging`` installs a secret-scrubbing log record factory, which
    is process-global state, so it has to be put back as well.
    """
    root = logging.getLogger()
    level = root.level
    handlers = list(root.handlers)
    factory = logging.getLogRecordFactory()
    try:
        yield root
    finally:
        root.setLevel(level)
        root.handlers[:] = handlers
        logging.setLogRecordFactory(factory)


def test_defaults(env: pytest.MonkeyPatch) -> None:
    """Without arguments or environment the documented defaults apply."""
    config = _resolve([])

    assert config.host == DEFAULT_HOST == "127.0.0.1"
    assert config.port == DEFAULT_PORT == 8080
    assert config.log_level == DEFAULT_LOG_LEVEL == "INFO"
    assert config.data_dir == Path("data")
    assert config.database_path == Path("data") / "doorbell.db"


def test_host_cli_beats_env(env: pytest.MonkeyPatch) -> None:
    """``--host`` wins over ``CHIMEDECK_HOST``.

    :param env: isolated environment.
    """
    env.setenv("CHIMEDECK_HOST", "0.0.0.0")

    assert _resolve(["--host", "192.0.2.10"]).host == "192.0.2.10"


def test_host_env_beats_default(env: pytest.MonkeyPatch) -> None:
    """``CHIMEDECK_HOST`` wins over the loopback default.

    :param env: isolated environment.
    """
    env.setenv("CHIMEDECK_HOST", "192.0.2.11")

    assert _resolve([]).host == "192.0.2.11"


def test_port_cli_beats_env(env: pytest.MonkeyPatch) -> None:
    """``--port`` wins over ``CHIMEDECK_PORT``.

    :param env: isolated environment.
    """
    env.setenv("CHIMEDECK_PORT", "9090")

    assert _resolve(["--port", "9999"]).port == 9999


def test_port_env_beats_default(env: pytest.MonkeyPatch) -> None:
    """``CHIMEDECK_PORT`` wins over the default port.

    :param env: isolated environment.
    """
    env.setenv("CHIMEDECK_PORT", "9090")

    assert _resolve([]).port == 9090


def test_port_zero_is_allowed(env: pytest.MonkeyPatch) -> None:
    """Port 0 (let the operating system choose) is a valid explicit value.

    :param env: isolated environment.
    """
    assert _resolve(["--port", "0"]).port == 0


def test_log_level_cli_beats_env(env: pytest.MonkeyPatch) -> None:
    """``--log-level`` wins over ``CHIMEDECK_LOG_LEVEL``.

    :param env: isolated environment.
    """
    env.setenv("CHIMEDECK_LOG_LEVEL", "ERROR")

    assert _resolve(["--log-level", "warning"]).log_level == "WARNING"


def test_log_level_env_beats_default(env: pytest.MonkeyPatch) -> None:
    """``CHIMEDECK_LOG_LEVEL`` wins over the default level.

    :param env: isolated environment.
    """
    env.setenv("CHIMEDECK_LOG_LEVEL", "debug")

    assert _resolve([]).log_level == "DEBUG"


def test_log_level_is_upper_cased(env: pytest.MonkeyPatch) -> None:
    """``--log-level debug`` normalises to ``DEBUG``.

    :param env: isolated environment.
    """
    assert _resolve(["--log-level", "debug"]).log_level == "DEBUG"


def test_data_dir_cli_beats_env(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--data-dir`` wins over both data directory environment variables.

    :param env: isolated environment.
    :param tmp_path: pytest temporary directory.
    """
    env.setenv("CHIMEDECK_DATA_DIR", str(tmp_path / "env-dir"))
    env.setenv("CHIMEDECK_DATA", str(tmp_path / "env-data-dir"))

    config = _resolve(["--data-dir", str(tmp_path / "cli-dir")])

    assert config.data_dir == tmp_path / "cli-dir"
    assert config.database_path == tmp_path / "cli-dir" / "doorbell.db"


def test_data_dir_env_beats_default(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``CHIMEDECK_DATA_DIR`` is used when the CLI says nothing.

    :param env: isolated environment.
    :param tmp_path: pytest temporary directory.
    """
    env.setenv("CHIMEDECK_DATA_DIR", str(tmp_path / "env-dir"))

    config = _resolve([])

    assert config.data_dir == tmp_path / "env-dir"
    assert config.database_path == tmp_path / "env-dir" / "doorbell.db"


def test_data_dir_env_dir_wins_over_env_data(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``CHIMEDECK_DATA_DIR`` wins over the older ``CHIMEDECK_DATA``.

    :param env: isolated environment.
    :param tmp_path: pytest temporary directory.
    """
    env.setenv("CHIMEDECK_DATA", str(tmp_path / "legacy"))
    env.setenv("CHIMEDECK_DATA_DIR", str(tmp_path / "preferred"))

    assert _resolve([]).data_dir == tmp_path / "preferred"


def test_data_dir_env_data_is_the_fallback(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``CHIMEDECK_DATA`` is used when ``CHIMEDECK_DATA_DIR`` is unset.

    :param env: isolated environment.
    :param tmp_path: pytest temporary directory.
    """
    env.setenv("CHIMEDECK_DATA", str(tmp_path / "legacy"))

    assert _resolve([]).data_dir == tmp_path / "legacy"


def test_the_new_env_names_are_canonical(
    env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The ``CHIMEDECK_*`` names resolve and log no deprecation at all.

    :param env: isolated environment.
    :param caplog: captured log records.
    """
    caplog.set_level(logging.DEBUG, logger="chime_deck.env")
    env.setenv("CHIMEDECK_HOST", "192.0.2.12")
    env.setenv("CHIMEDECK_PORT", "9091")
    env.setenv("CHIMEDECK_LOG_LEVEL", "warning")

    config = _resolve([])

    assert config.host == "192.0.2.12"
    assert config.port == 9091
    assert config.log_level == "WARNING"
    deprecations = [
        record.getMessage()
        for record in caplog.records
        if record.name == "chime_deck.env" and "deprecated" in record.getMessage()
    ]
    assert deprecations == []


def test_the_pre_rename_env_names_still_work_and_warn(
    env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A deployed unit that still sets the old name keeps working, loudly.

    :param env: isolated environment.
    :param caplog: captured log records.
    """
    caplog.set_level(logging.WARNING, logger="chime_deck.env")
    env.setenv("SUPER_DOORBELL_PORT", "9092")

    assert _resolve([]).port == 9092

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "chime_deck.env" and record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1
    assert "SUPER_DOORBELL_PORT" in warnings[0]
    assert "CHIMEDECK_PORT" in warnings[0]
    assert "deprecated" in warnings[0]


def test_the_new_env_name_wins_when_both_are_set(
    env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The canonical name wins, and the log says which one was ignored.

    :param env: isolated environment.
    :param caplog: captured log records.
    """
    caplog.set_level(logging.WARNING, logger="chime_deck.env")
    env.setenv("CHIMEDECK_PORT", "9093")
    env.setenv("SUPER_DOORBELL_PORT", "9094")

    assert _resolve([]).port == 9093

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "chime_deck.env" and record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1
    assert "SUPER_DOORBELL_PORT" in warnings[0]
    assert "CHIMEDECK_PORT" in warnings[0]
    assert "the new name wins" in warnings[0]


def test_explicit_db_path_overrides_data_dir(tmp_path: Path) -> None:
    """An explicitly supplied database path wins over the data directory.

    :param tmp_path: pytest temporary directory.
    """
    config = ServerConfig(data_dir=tmp_path / "data", db_path=tmp_path / "custom.db")

    assert config.database_path == tmp_path / "custom.db"


def test_default_static_dir_is_web_dist() -> None:
    """The built frontend is expected at ``web/dist`` in the repository."""
    assert default_static_dir().parts[-2:] == ("web", "dist")


def _fake_package_root(tmp_path: Path, *, with_static: bool) -> Path:
    """Create a stand-in for an installed ``chime_deck`` package directory.

    :param tmp_path: pytest temporary directory.
    :param with_static: whether the wheel's force-included ``static`` copy exists.
    """
    root = tmp_path / "site-packages" / "chime_deck"
    root.mkdir(parents=True)
    if with_static:
        (root / "static" / "assets").mkdir(parents=True)
        (root / "static" / "index.html").write_text("packaged ui", encoding="utf-8")
        (root / "static" / "assets" / "app.js").write_text("console.log('hi');", encoding="utf-8")
    return root


def _patch_packaged_dir(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Make ``importlib.resources.files`` resolve to *root*.

    :param monkeypatch: patcher that installs the fake resource loader.
    :param root: directory standing in for the installed package.
    """

    def fake_files(package: str) -> Path:
        """Return *root* whatever package was requested.

        :param package: package name the code under test asked for.
        """
        return root

    monkeypatch.setattr(importlib.resources, "files", fake_files)


def _patch_source_tree(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Make the source-tree lookup resolve to *path*.

    :param monkeypatch: patcher that replaces the source-tree resolver.
    :param path: directory standing in for a checkout's ``web/dist``.
    """

    def fake_source_tree() -> Path:
        """Return *path* regardless of where the module really lives."""
        return path

    monkeypatch.setattr("chime_deck.config.source_tree_static_dir", fake_source_tree)


def test_default_static_dir_prefers_the_packaged_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wheel's own copy wins over a stale ``web/dist`` in the source tree.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher installing the fake install and checkout.
    """
    package_root = _fake_package_root(tmp_path, with_static=True)
    stale_dist = tmp_path / "repo" / "web" / "dist"
    stale_dist.mkdir(parents=True)
    (stale_dist / "index.html").write_text("stale ui", encoding="utf-8")
    _patch_packaged_dir(monkeypatch, package_root)
    _patch_source_tree(monkeypatch, stale_dist)

    assert default_static_dir() == package_root / "static"


def test_default_static_dir_falls_back_to_the_source_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install with no package-internal copy still serves ``web/dist``.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher installing the fake install and checkout.
    """
    package_root = _fake_package_root(tmp_path, with_static=False)
    checkout_dist = tmp_path / "repo" / "web" / "dist"
    checkout_dist.mkdir(parents=True)
    (checkout_dist / "index.html").write_text("built ui", encoding="utf-8")
    _patch_packaged_dir(monkeypatch, package_root)
    _patch_source_tree(monkeypatch, checkout_dist)

    assert default_static_dir() == checkout_dist


def test_explicit_static_dir_beats_the_packaged_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``static_dir`` wins over the package-internal copy.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher installing the fake install.
    """
    _patch_packaged_dir(monkeypatch, _fake_package_root(tmp_path, with_static=True))
    other = tmp_path / "somewhere-else"

    # ServerConfig.static_dir is the documented override seam (there is no
    # --static-dir CLI flag); an explicit dir wins over the package-internal copy.
    assert ServerConfig(data_dir=tmp_path, static_dir=other).resolved_static_dir == other


def test_resolved_static_dir_honours_an_injected_path(tmp_path: Path) -> None:
    """An injected ``static_dir`` replaces the repository default.

    :param tmp_path: pytest temporary directory.
    """
    injected = tmp_path / "dist"

    assert ServerConfig(data_dir=tmp_path, static_dir=injected).resolved_static_dir == injected


def test_resolved_sounds_dir_defaults_and_honours_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library directory derives from the data dir unless one is injected.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that isolates ``expanduser`` from the real home.
    """
    assert ServerConfig(data_dir=tmp_path).resolved_sounds_dir == (tmp_path / "sounds").resolve()

    injected = tmp_path / "custom-sounds"
    assert (
        ServerConfig(data_dir=tmp_path, sounds_dir=injected).resolved_sounds_dir
        == injected.resolve()
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    home_relative = ServerConfig(data_dir=tmp_path, sounds_dir=Path("~/library"))
    assert home_relative.resolved_sounds_dir == (tmp_path / "library").resolve()


def test_resolved_icons_dir_defaults_and_honours_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The icon directory derives from the data dir unless one is injected.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that isolates ``expanduser`` from the real home.
    """
    assert ServerConfig(data_dir=tmp_path).resolved_icons_dir == (tmp_path / "icons").resolve()

    injected = tmp_path / "custom-icons"
    assert (
        ServerConfig(data_dir=tmp_path, icons_dir=injected).resolved_icons_dir == injected.resolve()
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    home_relative = ServerConfig(data_dir=tmp_path, icons_dir=Path("~/glyphs"))
    assert home_relative.resolved_icons_dir == (tmp_path / "glyphs").resolve()


def test_resolved_icons_dir_is_absolute_through_relative_and_symlinked_data_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolution removes both relative parts and symlinked data directories.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that moves the working directory into ``tmp_path``.
    """
    monkeypatch.chdir(tmp_path)
    relative = ServerConfig(data_dir=Path("data")).resolved_icons_dir
    assert relative == (tmp_path / "data" / "icons").resolve()
    assert relative.is_absolute()

    real = tmp_path / "real-data"
    real.mkdir()
    (tmp_path / "link-data").symlink_to(real, target_is_directory=True)
    linked = ServerConfig(data_dir=tmp_path / "link-data").resolved_icons_dir
    assert linked == (real / "icons").resolve()


def test_resolved_icons_dir_does_not_create_the_directory(tmp_path: Path) -> None:
    """Reading the property is a pure path computation.

    :param tmp_path: pytest temporary directory.
    """
    directory = ServerConfig(data_dir=tmp_path).resolved_icons_dir

    assert directory == (tmp_path / "icons").resolve()
    assert not directory.exists()


@pytest.mark.parametrize("value", ["abc", "-1", "70000", "8080.5", ""])
def test_invalid_port_argument_exits_two(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    """A bad ``--port`` is rejected by argparse with status 2.

    :param env: isolated environment.
    :param capsys: captured standard streams.
    :param value: unusable port text.
    """
    with pytest.raises(SystemExit) as exit_info:
        main(["--port", value])

    assert exit_info.value.code == 2
    assert "usage: chime-deck" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["LOUD", "verbose", ""])
def test_invalid_log_level_argument_exits_two(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    """A bad ``--log-level`` is rejected by argparse with status 2.

    :param env: isolated environment.
    :param capsys: captured standard streams.
    :param value: unusable log level text.
    """
    with pytest.raises(SystemExit) as exit_info:
        main(["--log-level", value])

    assert exit_info.value.code == 2
    assert "usage: chime-deck" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["abc", "-1", "70000"])
def test_invalid_port_environment_exits_two(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    """A bad ``CHIMEDECK_PORT`` reaches the user as a usage error.

    :param env: isolated environment.
    :param capsys: captured standard streams.
    :param value: unusable port text.
    """
    env.setenv("CHIMEDECK_PORT", value)

    with pytest.raises(SystemExit) as exit_info:
        main([])

    assert exit_info.value.code == 2
    error = capsys.readouterr().err
    assert "usage: chime-deck" in error
    assert "CHIMEDECK_PORT" in error


def test_invalid_log_level_environment_exits_two(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad ``CHIMEDECK_LOG_LEVEL`` reaches the user as a usage error.

    :param env: isolated environment.
    :param capsys: captured standard streams.
    """
    env.setenv("CHIMEDECK_LOG_LEVEL", "LOUD")

    with pytest.raises(SystemExit) as exit_info:
        main([])

    assert exit_info.value.code == 2
    error = capsys.readouterr().err
    assert "usage: chime-deck" in error
    assert "CHIMEDECK_LOG_LEVEL" in error


def test_log_file_switch_enables_the_file_sink(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``CHIMEDECK_LOG_FILE=1`` turns on the rotating file under ``data/logs``.

    :param env: isolated environment.
    :param tmp_path: pytest temporary directory.
    """
    env.setenv("CHIMEDECK_LOG_FILE", "1")

    config = _resolve(["--data-dir", str(tmp_path)])

    assert config.log_file is True
    assert config.log_file_path == tmp_path / "logs" / "chime-deck.log"


def test_log_file_switch_defaults_off(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """File logging is opt-in: unset means console only.

    :param env: isolated environment.
    :param tmp_path: pytest temporary directory.
    """
    config = _resolve(["--data-dir", str(tmp_path)])

    assert config.log_file is False
    assert config.log_file_path is None


def test_invalid_log_file_switch_exits_with_an_error(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad ``CHIMEDECK_LOG_FILE`` reaches the user as a usage error.

    :param env: isolated environment.
    :param capsys: captured standard streams.
    """
    env.setenv("CHIMEDECK_LOG_FILE", "maybe")

    with pytest.raises(SystemExit) as exit_info:
        main([])

    assert exit_info.value.code == 2
    error = capsys.readouterr().err
    assert "usage: chime-deck" in error
    assert "CHIMEDECK_LOG_FILE" in error


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [("CHIMEDECK_PORT", "abc"), ("CHIMEDECK_LOG_LEVEL", "LOUD")],
)
def test_invalid_environment_never_starts_uvicorn(
    env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    env_value: str,
) -> None:
    """Configuration errors are reported before the server is started.

    :param env: isolated environment.
    :param monkeypatch: pytest monkeypatch fixture.
    :param env_name: environment variable under test.
    :param env_value: unusable value for that variable.
    """
    env.setenv(env_name, env_value)

    def _fail(_configs: object) -> int:
        raise AssertionError("the serving layer must not be reached")

    monkeypatch.setattr("chime_deck.serving.serve", _fail)

    with pytest.raises(SystemExit):
        main([])


def test_configure_logging_sets_the_root_level(root_logger: logging.Logger) -> None:
    """``configure_logging`` applies the requested level to the root logger.

    :param root_logger: root logger with its state restored afterwards.
    """
    configure_logging("debug")

    assert root_logger.level == logging.DEBUG


def test_configure_logging_adds_one_handler_and_is_idempotent(
    root_logger: logging.Logger,
) -> None:
    """A handler is added only when the root logger has none, and only once.

    :param root_logger: root logger with its state restored afterwards.
    """
    root_logger.handlers.clear()

    configure_logging("INFO")
    assert len(root_logger.handlers) == 1

    configure_logging("INFO")
    assert len(root_logger.handlers) == 1


def test_configure_logging_keeps_existing_handlers(root_logger: logging.Logger) -> None:
    """An already configured root logger is left alone apart from its level.

    :param root_logger: root logger with its state restored afterwards.
    """
    sentinel = logging.NullHandler()
    root_logger.handlers.clear()
    root_logger.handlers.append(sentinel)

    configure_logging("ERROR")

    assert root_logger.handlers == [sentinel]
    assert root_logger.level == logging.ERROR


def test_main_passes_the_log_file_to_configure_logging(
    env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``main`` hands the enabled rotating log file to the logging setup.

    ``configure_logging`` itself is replaced by a recorder, so no real
    :class:`~logging.handlers.RotatingFileHandler` is created here; the serving
    layer is stubbed out so no server starts either.

    :param env: isolated environment.
    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: pytest temporary directory.
    """
    env.setenv("CHIMEDECK_LOG_FILE", "1")
    recorded: dict[str, object] = {}

    def _record(level: str, *, log_file: Path | None = None) -> None:
        recorded["level"] = level
        recorded["log_file"] = log_file

    monkeypatch.setattr("chime_deck.cli.configure_logging", _record)
    monkeypatch.setattr("chime_deck.serving.serve", lambda _configs: 0)

    status = main(["--data-dir", str(tmp_path)])

    assert status == 0
    assert recorded["log_file"] == tmp_path / "logs" / "chime-deck.log"


def test_help_lists_the_server_options(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--help`` documents the four options this ticket adds.

    :param env: isolated environment.
    :param capsys: captured standard streams.
    """
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    for flag in ("--host", "--port", "--data-dir", "--log-level"):
        assert flag in output


def test_main_returns_zero_after_uvicorn_returns(
    env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root_logger: logging.Logger,
) -> None:
    """A clean serve return becomes exit status 0, with our own logging settings.

    The serving seam is `chime_deck.serving.serve`, which receives the
    ``uvicorn.Config`` objects the CLI built: the assertions below are about those
    objects, so they stay faithful whether the process serves one listener or two.

    :param env: isolated environment.
    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: pytest temporary directory.
    :param root_logger: root logger, restored after the test so the level this
        call mutates cannot leak into the rest of the suite.
    """
    recorded: list[uvicorn.Config] = []

    def _fake_serve(configs: list[uvicorn.Config]) -> int:
        recorded.extend(configs)
        return 0

    monkeypatch.setattr("chime_deck.serving.serve", _fake_serve)

    status = main(["--data-dir", str(tmp_path), "--port", "8123", "--log-level", "DEBUG"])

    assert status == 0
    assert root_logger.level == logging.DEBUG
    doorbell = recorded[0]
    assert isinstance(doorbell.app, FastAPI)
    assert doorbell.host == "127.0.0.1"
    assert doorbell.port == 8123
    assert doorbell.log_level == "debug"
    assert doorbell.log_config is None
    assert doorbell.timeout_graceful_shutdown == GRACEFUL_SHUTDOWN_TIMEOUT_S == 1


def test_role_defaults_to_server_and_the_flag_selects_the_renderer(
    env: pytest.MonkeyPatch,
) -> None:
    """``--role`` is CLI-only and defaults to the doorbell (D24).

    :param env: isolated environment.
    """
    assert _resolve(["--role", "renderer"]).role == "renderer"
    assert _resolve([]).role == "server" == DEFAULT_ROLE


def test_an_unknown_role_is_rejected_by_argparse(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown ``--role`` is a usage error, refused before anything runs.

    :param env: isolated environment.
    :param capsys: captured standard streams.
    """
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["--role", "sideways"])

    assert exit_info.value.code == 2
    assert "usage: chime-deck" in capsys.readouterr().err


def test_help_documents_the_renderer_role(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--help`` names the role, its value and the token it needs.

    :param env: isolated environment.
    :param capsys: captured standard streams.
    """
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    assert "--role" in output
    assert "renderer" in output
    assert "tts.renderer.token" in output


def test_run_renderer_without_a_token_returns_the_config_exit_code(
    env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty ``tts.renderer.token`` is a returned status, never a raised exit.

    The pre-flight opens the store, so the database is created and migrated even
    though the process refuses to serve — which is what makes the advice in the
    message actionable on a fresh data directory.

    :param env: isolated environment.
    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: pytest temporary directory.
    :param caplog: pytest log capture fixture.
    """
    config = _resolve(["--role", "renderer", "--data-dir", str(tmp_path)])
    monkeypatch.setattr("chime_deck.cli.uvicorn.run", _forbid_uvicorn)

    with caplog.at_level(logging.ERROR, logger="chime_deck.cli"):
        status = run_renderer(config)

    assert isinstance(status, int)
    assert status == RENDERER_CONFIG_EXIT_CODE == 2
    assert config.database_path.is_file()
    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert any("tts.renderer.token is empty" in message for message in errors)
    assert all(_TOKEN not in message for message in errors)


def test_run_renderer_serves_the_renderer_app_with_the_seeded_token(
    env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root_logger: logging.Logger,
) -> None:
    """A seeded token launches exactly the renderer app on the configured bind.

    ``uvicorn.run`` is replaced by a recorder, so nothing binds a socket; the
    recorded call is the contract this test pins — the app, the host, the port
    and the bounded graceful shutdown.

    :param env: isolated environment.
    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: pytest temporary directory.
    :param root_logger: root logger, restored after the test.
    """
    config = _resolve(["--role", "renderer", "--data-dir", str(tmp_path), "--port", "9123"])
    asyncio.run(_seed_renderer_token(config))
    recorded: dict[str, object] = {}

    def _fake_run(app: object, **kwargs: object) -> None:
        recorded["app"] = app
        recorded.update(kwargs)

    monkeypatch.setattr("chime_deck.cli.uvicorn.run", _fake_run)

    status = run_renderer(config)

    assert status == 0
    assert set(recorded) == {
        "app",
        "host",
        "port",
        "log_level",
        "log_config",
        "timeout_graceful_shutdown",
    }
    launched = recorded["app"]
    assert isinstance(launched, FastAPI)
    assert launched.title == "ChimeDeck renderer"
    assert str(launched.url_path_for("render_speech")) == _RENDER_ROUTE
    assert recorded["host"] == DEFAULT_HOST == config.host
    assert recorded["port"] == 9123
    assert recorded["log_config"] is None
    assert recorded["timeout_graceful_shutdown"] == 1
    assert _TOKEN not in repr(recorded)


def test_main_with_the_renderer_role_returns_two_without_binding(
    env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root_logger: logging.Logger,
) -> None:
    """``main --role renderer`` reports a misconfigured renderer as status 2.

    The return value is a plain ``int``: a runtime configuration refusal is not a
    usage error, so ``main`` must not raise ``SystemExit`` for it.

    :param env: isolated environment.
    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: pytest temporary directory.
    :param root_logger: root logger, restored after the test.
    """
    monkeypatch.setattr("chime_deck.cli.uvicorn.run", _forbid_uvicorn)

    status = main(["--role", "renderer", "--data-dir", str(tmp_path)])

    assert isinstance(status, int)
    assert status == 2


def test_the_admin_listener_defaults_to_a_second_bind(env: pytest.MonkeyPatch) -> None:
    """The admin listener is on by default, on its own address (HSD-n6wi).

    :param env: isolated environment.
    """
    config = _resolve([])

    assert config.admin_enabled is True
    assert (config.admin_host, config.admin_port) == (DEFAULT_ADMIN_HOST, DEFAULT_ADMIN_PORT)
    assert admin_bind(config) == (DEFAULT_ADMIN_HOST, DEFAULT_ADMIN_PORT)


def test_no_admin_switches_the_listener_off(env: pytest.MonkeyPatch) -> None:
    """``--no-admin`` removes the listener, and ``admin_bind`` is the one answer.

    :param env: isolated environment.
    """
    assert admin_bind(_resolve(["--no-admin"])) is None


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_the_admin_environment_switch_off_values(env: pytest.MonkeyPatch, value: str) -> None:
    """A falsy ``CHIMEDECK_ADMIN`` disables the listener, and ``1`` keeps it.

    An empty value counts as off, exactly like ``CHIMEDECK_LOG_FILE=``.

    :param env: isolated environment.
    :param value: value of ``CHIMEDECK_ADMIN``.
    """
    env.setenv("CHIMEDECK_ADMIN", value)

    assert _resolve([]).admin_enabled is False

    env.setenv("CHIMEDECK_ADMIN", "1")
    assert _resolve([]).admin_enabled is True


def test_the_admin_bind_environment_and_cli_precedence(env: pytest.MonkeyPatch) -> None:
    """CLI beats environment beats the default, like the doorbell's own bind.

    :param env: isolated environment.
    """
    env.setenv("CHIMEDECK_ADMIN_HOST", "192.168.0.50")
    env.setenv("CHIMEDECK_ADMIN_PORT", "9091")

    from_env = _resolve([])
    assert (from_env.admin_host, from_env.admin_port) == ("192.168.0.50", 9091)

    overridden = _resolve(["--admin-host", "127.0.0.1", "--admin-port", "0"])
    assert (overridden.admin_host, overridden.admin_port) == ("127.0.0.1", 0)


def test_the_admin_port_zero_is_allowed_on_loopback(env: pytest.MonkeyPatch) -> None:
    """Port 0 stays a test seam, but only where the OS's choice is discoverable.

    :param env: isolated environment.
    """
    config = _resolve(["--admin-host", "127.0.0.1", "--admin-port", "0"])

    assert config.admin_port == 0
    assert admin_bind(config) == ("127.0.0.1", 0)


@pytest.mark.parametrize(
    "argv",
    [
        ["--admin-host", "127.0.0.1", "--admin-port", "8080"],
        [
            "--host",
            "127.0.0.1",
            "--port",
            "9000",
            "--admin-host",
            "127.0.0.1",
            "--admin-port",
            "9000",
        ],
    ],
)
def test_the_admin_listener_may_not_share_the_doorbell_address(
    env: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """Two servers cannot share one socket, so the combination is refused.

    :param env: isolated environment.
    :param argv: command line that collides.
    """
    with pytest.raises(ValueError) as raised:
        resolve_config(build_parser().parse_args(argv))

    assert "same address as the doorbell" in str(raised.value)


def test_the_admin_listener_may_not_take_an_os_chosen_lan_port(
    env: pytest.MonkeyPatch,
) -> None:
    """Port 0 on a non-loopback admin host would be undiscoverable, so it is refused.

    :param env: isolated environment.
    """
    with pytest.raises(ValueError) as raised:
        resolve_config(build_parser().parse_args(["--admin-host", "0.0.0.0", "--admin-port", "0"]))

    assert "only allowed on a loopback admin address" in str(raised.value)


def test_a_colliding_admin_bind_is_a_usage_error(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main`` turns the refusal into argparse's exit status 2 and a message.

    :param env: isolated environment.
    :param capsys: captured standard streams.
    """
    with pytest.raises(SystemExit) as exit_info:
        main(["--admin-host", "127.0.0.1", "--admin-port", "8080"])

    assert exit_info.value.code == 2
    error = capsys.readouterr().err
    assert "same address as the doorbell" in error


def test_an_unusable_admin_port_environment_is_a_usage_error(
    env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``CHIMEDECK_ADMIN_PORT`` is validated like ``CHIMEDECK_PORT``.

    :param env: isolated environment.
    :param capsys: captured standard streams.
    """
    env.setenv("CHIMEDECK_ADMIN_PORT", "not-a-port")

    with pytest.raises(SystemExit) as exit_info:
        main([])

    assert exit_info.value.code == 2
    assert "CHIMEDECK_ADMIN_PORT" in capsys.readouterr().err


def test_main_builds_an_admin_app_only_when_the_listener_is_on(
    env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root_logger: logging.Logger,
) -> None:
    """``main`` hands the serving layer the wrapper only when the listener exists.

    :param env: isolated environment.
    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: pytest temporary directory.
    :param root_logger: root logger, restored after the test.
    """
    recorded: list[uvicorn.Config] = []

    def _fake_serve(configs: list[uvicorn.Config]) -> int:
        recorded.extend(configs)
        return 0

    monkeypatch.setattr("chime_deck.serving.serve", _fake_serve)

    assert main(["--data-dir", str(tmp_path), "--no-admin"]) == 0
    assert len(recorded) == 1

    recorded.clear()
    assert main(["--data-dir", str(tmp_path), "--admin-host", "127.0.0.1"]) == 0
    assert len(recorded) == 2
    assert recorded[1].lifespan == "off"
