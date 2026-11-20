"""Command line entry point for ChimeDeck.

The CLI resolves the server configuration (command line beats environment
beats defaults), configures logging and runs the ASGI app through uvicorn.
Signals and ``KeyboardInterrupt`` belong to uvicorn, so nothing is caught here.

``--role renderer`` runs the D24 renderer instead of the doorbell, and it is
the only switch that does so: :func:`chime_deck.config.ServerConfig` has no
role environment variable on purpose, because an environment variable could
enable a network-facing renderer invisibly. The CLI's **return code** is the only
channel for a runtime configuration refusal (the renderer has no token), because
``parser.error`` would raise ``SystemExit`` from what is otherwise a plain
function.

Before either role serves anything the CLI preflights the database schema: a file
written by a newer build is refused with :data:`SCHEMA_TOO_NEW_EXIT_CODE` and one
clean ERROR line, because a downgrade would corrupt it and the renderer opens the
same store. That read-only preflight is best effort — it treats any SQLite error as
"cannot tell" — so the store's own migration guard is then run once, before
anything is bound: the refusal is the same single line and the same status on every
path that can see a newer version.

Owning ticket: HSD-8gft; the renderer role was added by HSD-5de8.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import uvicorn

from chime_deck import __version__, serving
from chime_deck.admin import PASSWORD_SETTING_KEY, create_admin_app
from chime_deck.app import create_app
from chime_deck.config import (
    DEFAULT_ADMIN_HOST,
    DEFAULT_ADMIN_PORT,
    DEFAULT_HOST,
    DEFAULT_LOG_LEVEL,
    DEFAULT_PORT,
    LOG_LEVELS,
    ROLES,
    ServerConfig,
    admin_bind,
    configure_logging,
    normalise_log_level,
    parse_port,
    resolve_config,
)
from chime_deck.renderer import (
    RENDERER_CONFIG_EXIT_CODE,
    RendererTokenMissing,
    create_renderer_app,
    fetch_renderer_token,
)
from chime_deck.serving import GRACEFUL_SHUTDOWN_TIMEOUT_S
from chime_deck.store import ConfigStore, SchemaTooNewError, check_schema_version

__all__ = [
    "GRACEFUL_SHUTDOWN_TIMEOUT_S",
    "RESET_FAILED_EXIT_CODE",
    "SCHEMA_TOO_NEW_EXIT_CODE",
    "build_parser",
    "main",
    "reset_admin_password",
    "run_renderer",
]

_LOGGER = logging.getLogger(__name__)

SCHEMA_TOO_NEW_EXIT_CODE: Final = 78
"""Exit status for a database newer than this build: sysexits ``EX_CONFIG``.

Distinct from :data:`RENDERER_CONFIG_EXIT_CODE` (2): this is a permanent
configuration refusal — the file can never be read by this build — not a usage
error and not a transient failure.
"""

RESET_FAILED_EXIT_CODE: Final = 1
"""Exit status when ``--reset-admin-password`` could not write the store.

No listener and no network path: the recovery credential is being able to run
this command as the service user with access to the data directory.
"""


def _port_type(value: str) -> int:
    """Convert a ``--port`` argument, rejecting anything outside 0-65535.

    :param value: raw argument text.
    :raises argparse.ArgumentTypeError: if the port is not usable.
    """
    try:
        return parse_port(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _log_level_type(value: str) -> str:
    """Convert a ``--log-level`` argument to upper case and validate it.

    :param value: raw argument text, any case.
    :raises argparse.ArgumentTypeError: if the level is not one we know.
    """
    try:
        return normalise_log_level(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``chime-deck`` command."""
    parser = argparse.ArgumentParser(
        prog="chime-deck",
        description="A self-hosted chime and status panel for Home Assistant.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="show the installed version and exit",
    )
    parser.add_argument(
        "--host",
        default=None,
        metavar="ADDRESS",
        help=(f"address to bind (env: CHIMEDECK_HOST; default: {DEFAULT_HOST}, localhost only)"),
    )
    parser.add_argument(
        "--port",
        type=_port_type,
        default=None,
        metavar="PORT",
        help=f"TCP port to bind (env: CHIMEDECK_PORT; default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "directory holding doorbell.db (env: CHIMEDECK_DATA_DIR, then "
            "CHIMEDECK_DATA; default: ./data)"
        ),
    )
    parser.add_argument(
        "--admin-host",
        default=None,
        metavar="ADDRESS",
        help=(
            "address of the configuration-only admin listener (env: "
            f"CHIMEDECK_ADMIN_HOST; default: {DEFAULT_ADMIN_HOST}, reachable from the "
            "network)"
        ),
    )
    parser.add_argument(
        "--admin-port",
        type=_port_type,
        default=None,
        metavar="PORT",
        help=(
            "port of the admin listener (env: CHIMEDECK_ADMIN_PORT; default: "
            f"{DEFAULT_ADMIN_PORT}); 0 (let the OS choose) is only allowed on a "
            "loopback admin host"
        ),
    )
    parser.add_argument(
        "--no-admin",
        action="store_true",
        help=(
            "do not start the configuration-only admin listener "
            "(env: CHIMEDECK_ADMIN=0/false/no/off); the doorbell itself is unaffected"
        ),
    )
    parser.add_argument(
        "--reset-admin-password",
        action="store_true",
        help=(
            "clear the configuration-interface password in the data directory and exit "
            "without serving; stop the service first so there is only one writer"
        ),
    )
    parser.add_argument(
        "--role",
        choices=ROLES,
        default=None,
        help=(
            "which process to run: 'server' is the doorbell (default); 'renderer' serves "
            "POST /api/tts/render for another doorbell and refuses to start unless "
            "tts.renderer.token is set (it needs no broker, Home Assistant or audio device; "
            "it binds to localhost unless --host says otherwise)"
        ),
    )
    parser.add_argument(
        "--log-level",
        type=_log_level_type,
        choices=LOG_LEVELS,
        default=None,
        metavar="LEVEL",
        help=(
            "log verbosity, one of "
            f"{', '.join(LOG_LEVELS)} (env: CHIMEDECK_LOG_LEVEL; "
            f"default: {DEFAULT_LOG_LEVEL})"
        ),
    )
    return parser


def run_renderer(config: ServerConfig) -> int:
    """Run the D24 renderer until uvicorn returns, or refuse to start without a token.

    The refusal is a returned status, never ``parser.error``: this function has no
    parser, and a runtime configuration refusal is not a usage error. That return
    code is the **only** channel the CLI has for it, so a unit file or a script can
    act on it. The message is logged at ERROR in full, so a ``systemd`` journal
    already carries the fix. ``--role`` is CLI-only by design, so nothing here
    reads an environment variable.

    :param config: resolved configuration carrying ``role="renderer"``.
    :returns: process exit status — :data:`RENDERER_CONFIG_EXIT_CODE` when the
        token is missing, ``0`` after a clean shutdown.
    """
    try:
        token = asyncio.run(fetch_renderer_token(config))
    except RendererTokenMissing as exc:
        _LOGGER.error("%s", exc)
        return RENDERER_CONFIG_EXIT_CODE
    app = create_renderer_app(config, token=token)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
        # Keep our own logging configuration and handler instead of uvicorn's.
        log_config=None,
        # Bounded graceful shutdown, exactly like the server role.
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_S,
    )
    return 0


async def _open_store_once(path: Path) -> None:
    """Open and close the store, so its own migration guard runs.

    :param path: filesystem path of the SQLite file.
    """
    async with ConfigStore(path):
        pass


def _refuse_a_too_new_database(config: ServerConfig) -> int | None:
    """Refuse a database newer than this build, best effort first, then for real.

    The read-only preflight opens the file without creating it and treats any
    SQLite error as "cannot tell", so it is deliberately best effort: a version
    that is still only in an un-checkpointed WAL, or one behind a hot rollback
    journal left by a crash, is invisible to it. Opening the store is what
    actually migrates, so its guard is the authority, and running it here makes
    the refusal land before ``create_app`` or uvicorn exists — no traceback, no
    bound socket, and the same exit status and line on every too-new path.

    :param config: resolved configuration naming the database.
    :returns: :data:`SCHEMA_TOO_NEW_EXIT_CODE` when the database is newer than
        this build, otherwise ``None``.
    """
    try:
        check_schema_version(config.database_path)
        asyncio.run(_open_store_once(config.database_path))
    except SchemaTooNewError as exc:
        _LOGGER.error("%s", exc)
        return SCHEMA_TOO_NEW_EXIT_CODE
    return None


async def _clear_stored_password(config: ServerConfig) -> None:
    """Open the store and clear the stored admin password hash.

    One row, one transaction: the admin listener's credential is gone the next
    time the process loads the gate. Nothing is served, nothing is read from the
    network and neither the old hash nor the new (empty) value is logged.

    :param config: resolved configuration naming the database.
    """
    async with ConfigStore(config.database_path) as store, store.transaction():
        await store.set_setting(PASSWORD_SETTING_KEY, "")


def reset_admin_password(config: ServerConfig) -> int:
    """Clear the configuration-interface password and return an exit status.

    The recovery path is physical access only (HSD-n6wi): being able to run this
    command as the service user with access to the data directory is the
    credential. There is no network endpoint, no reset token and no second
    procedure, so a stolen cookie cannot undo the protection. The operator is told
    to stop the unit first, so there is a single writer for the database.

    :param config: resolved configuration naming the database.
    :returns: ``0`` after the write, :data:`RESET_FAILED_EXIT_CODE` on failure.
    """
    try:
        asyncio.run(_clear_stored_password(config))
    except Exception as exc:
        # "credential", not "password:": the secret scrubber redacts the text
        # after a `password:`-shaped key, which would swallow the exception name
        # this line exists to report.
        _LOGGER.error(
            "could not clear the configuration-interface credential: %s: %s",
            type(exc).__name__,
            exc,
        )
        return RESET_FAILED_EXIT_CODE
    _LOGGER.info("the configuration-interface password was cleared; restart the service")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the server until uvicorn returns.

    :param argv: arguments to parse; ``None`` means ``sys.argv[1:]``.
    :returns: process exit status, 0 after a clean shutdown,
        :data:`SCHEMA_TOO_NEW_EXIT_CODE` when the database is newer than this build,
        or :data:`RESET_FAILED_EXIT_CODE` when ``--reset-admin-password`` could not
        write the store.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = resolve_config(args)
    except ValueError as exc:
        parser.error(str(exc))

    configure_logging(config.log_level, log_file=config.log_file_path)
    refusal = _refuse_a_too_new_database(config)
    if refusal is not None:
        return refusal
    if args.reset_admin_password:
        return reset_admin_password(config)
    if config.role == "renderer":
        return run_renderer(config)
    app = create_app(config)
    if admin_bind(config) is None:
        return serving.serve(serving.build_configs(config, app))
    admin_app = create_admin_app(app, gate=app.state.doorbell.admin)
    return serving.serve(serving.build_configs(config, app, admin_app=admin_app))


if __name__ == "__main__":
    raise SystemExit(main())
