"""Server configuration resolved from the command line, the environment and defaults.

This module is deliberately pure: importing it reads no environment variables,
creates no directories and never touches the database. Everything happens in
:func:`resolve_config`, which the CLI calls once per process.

It also owns the one and only logging setup, including the secret scrubber that
keeps stored credentials out of console output and log files.

Every environment lookup here goes through :func:`chime_deck.env.env_value`, so
the canonical ``CHIMEDECK_`` variable names are read alongside the deprecated
pre-rename ones for one release.

Owning ticket: HSD-8gft; logging and scrubbing extended by HSD-91z1; icons seam
added by HSD-jwd3; static-UI packaging by HSD-sgkr.
"""

from __future__ import annotations

import argparse
import importlib.resources
import logging
import re
import traceback
from collections.abc import Iterable
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from .env import env_value
from .store import default_db_path

if TYPE_CHECKING:
    from .store import ConfigStore

__all__ = [
    "DEFAULT_ADMIN_HOST",
    "DEFAULT_ADMIN_PORT",
    "DEFAULT_HOST",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_PORT",
    "DEFAULT_ROLE",
    "LOG_FILE_BACKUPS",
    "LOG_FILE_MAX_BYTES",
    "LOG_LEVELS",
    "MAX_PORT",
    "MIN_PORT",
    "REDACTED",
    "ROLES",
    "SECRET_SCRUBBER",
    "ScrubbingFormatter",
    "SecretScrubber",
    "ServerConfig",
    "admin_bind",
    "apply_stored_log_level",
    "configure_logging",
    "default_static_dir",
    "install_log_scrubber",
    "is_loopback",
    "normalise_log_level",
    "packaged_static_dir",
    "parse_env_flag",
    "parse_port",
    "refresh_secret_scrubber",
    "resolve_config",
    "source_tree_static_dir",
]

_LOGGER = logging.getLogger(__name__)

LOG_LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
"""Log levels accepted by ``--log-level`` and ``CHIMEDECK_LOG_LEVEL``."""

DEFAULT_HOST: Final = "127.0.0.1"
"""Loopback bind address (decision D13)."""

DEFAULT_PORT: Final = 8080
"""Default HTTP port."""

DEFAULT_ADMIN_HOST: Final = "0.0.0.0"
"""Default bind address of the configuration-only admin listener (HSD-n6wi).

The point of that listener is to be reachable from another device's browser, so
its default is the LAN and not loopback; the doorbell's own default stays
loopback (D13). It is password-protected once one is set, and it serves
configuration only.
"""

DEFAULT_ADMIN_PORT: Final = 8081
"""Default HTTP port of the admin listener; next to the doorbell's 8080."""

DEFAULT_LOG_LEVEL: Final = "INFO"
"""Default log level."""

DEFAULT_ROLE: Final = "server"
"""The process the command line runs when ``--role`` is not given (D24)."""

ROLES: Final = ("server", "renderer")
"""The processes ``--role`` accepts: the doorbell itself, and the D24 renderer."""

MIN_PORT: Final = 0
MAX_PORT: Final = 65535

REDACTED: Final = "[REDACTED]"
"""Replacement text for anything the scrubber decides is a secret."""

LOG_FILE_MAX_BYTES: Final = 1_048_576
"""Size of one log file before it rotates: 1 MiB keeps disk use bounded on a Pi."""

LOG_FILE_BACKUPS: Final = 3
"""Number of rotated log files kept next to the current one."""

_MIN_SECRET_LENGTH: Final = 4
"""Shorter registered values are not scrubbed: they would redact ordinary words."""

_LOG_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s %(message)s"

_ENV_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
# An empty string counts as false: ``CHIMEDECK_LOG_FILE=`` is how a user
# switches a flag off in a compose file or a shell profile.
_ENV_FALSY: Final = frozenset({"0", "false", "no", "off", ""})

_BEARER_PATTERN: Final = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/=]{6,}")
"""Matches an ``Authorization``-style ``Bearer``/``Basic`` credential."""

_KEY_VALUE_PATTERN: Final = re.compile(
    r"(?i)\b([\w.\-]*(?:token|password|passwd|secret|api[_-]?key)[\w.\-]*)"
    r"\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\[REDACTED\]|[^\s,;&\]})]+)"
)
"""Matches ``token=...``, ``password: ...`` and friends.

``[REDACTED]`` is an alternative of its own so a second pass over text that was
already scrubbed is a no-op: records are scrubbed by the log record factory and
again by :class:`ScrubbingFormatter`, which would otherwise turn
``token=[REDACTED]`` into ``token=[REDACTED]]``.
"""

_URL_USERINFO_PATTERN: Final = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")
"""Matches the ``user:password@`` part of a URL."""

_BEARER_REPLACEMENT: Final = rf"\1 {REDACTED}"
_KEY_VALUE_REPLACEMENT: Final = rf"\1={REDACTED}"
_URL_USERINFO_REPLACEMENT: Final = rf"\1{REDACTED}@"

_SCRUB_RULES: Final = (
    (_BEARER_PATTERN, _BEARER_REPLACEMENT),
    (_KEY_VALUE_PATTERN, _KEY_VALUE_REPLACEMENT),
    (_URL_USERINFO_PATTERN, _URL_USERINFO_REPLACEMENT),
)
"""Pattern rules applied before the registered literal secret values.

Patterns run first and literals last so a matched ``token=...`` becomes
``token=[REDACTED]`` rather than a half-substituted mess.
"""

_SCRUBBER_TAG: Final = "_doorbell_scrubber"
"""Attribute marking a log record factory as already scrubbing records."""

_UNSCRUBBED_RECORD_ATTRS: Final = frozenset(
    {
        "name",
        "pathname",
        "filename",
        "module",
        "funcName",
        "levelname",
        "msg",
        "exc_text",
        "stack_info",
        "args",
    }
)
"""Record attributes handled explicitly by :meth:`SecretScrubber._scrub_record`."""


class SecretScrubber:
    """Remove secret values and secret-looking text from log records.

    This object is process-global by necessity: :mod:`logging` is global state,
    so there is exactly one scrubber per process (see :data:`SECRET_SCRUBBER`).

    Two layers of protection are applied in :meth:`scrub`:

    * the registered literal secret values, added with :meth:`set_secrets`. The
      registry is populated by :func:`refresh_secret_scrubber`, which the
      application lifespan calls after opening the store and the settings API
      calls after writing a secret; **every future writer of a secret setting
      must call it too**, because nothing else refreshes the set; and
    * patterns that recognise credentials by shape, so a token that was never
      stored still cannot reach a log line.

    :meth:`set_secrets` replaces the whole set rather than adding to it, so a
    cleared secret stops being scrubbed. Values shorter than
    :data:`_MIN_SECRET_LENGTH` are ignored: redacting every occurrence of a
    three-letter password would mangle ordinary log text.

    The object must never print its secrets, which is why it defines an explicit
    :meth:`__repr__` instead of relying on a dataclass repr.
    """

    def __init__(self) -> None:
        """Create an empty scrubber: no literals, patterns always active."""
        self._values: tuple[str, ...] = ()
        self._literals: re.Pattern[str] | None = None

    def set_secrets(self, values: Iterable[str]) -> None:
        """Replace the registered secret values.

        :param values: secret values to redact; short ones are dropped.
        """
        unique = {value for value in values if len(value) >= _MIN_SECRET_LENGTH}
        # Longest first so a value that contains another is redacted whole.
        self._values = tuple(sorted(unique, key=len, reverse=True))
        if self._values:
            self._literals = re.compile("|".join(re.escape(value) for value in self._values))
        else:
            self._literals = None

    def clear(self) -> None:
        """Forget every registered secret value, keeping the pattern rules."""
        self._values = ()
        self._literals = None

    def scrub(self, text: str) -> str:
        """Return *text* with pattern matches and registered values redacted.

        :param text: text about to be logged.
        """
        for pattern, replacement in _SCRUB_RULES:
            text = pattern.sub(replacement, text)
        if self._literals is not None:
            text = self._literals.sub(REDACTED, text)
        return text

    def scrub_record(self, record: logging.LogRecord) -> None:
        """Scrub one log record in place, swallowing every error.

        Called from the log record factory, so an exception here would break
        logging itself; nothing is logged on failure because logging the failure
        would re-enter this method.

        :param record: record built by the wrapped factory.
        """
        try:
            self._scrub_record(record)
        except Exception:
            return

    def _scrub_record(self, record: logging.LogRecord) -> None:
        """Scrub the message, exception, stack and extra attributes of *record*.

        The record is left byte-identical when there is nothing to redact, in
        particular ``args`` is only dropped when the merged message actually
        changed.

        :param record: record to scrub in place.
        """
        if record.args:
            try:
                merged = record.getMessage()
            except Exception:
                # An unformattable record is left alone rather than half-scrubbed:
                # replacing the message would lose the arguments it still needs.
                merged = ""
            scrubbed = self.scrub(merged)
            if scrubbed != merged:
                record.msg = scrubbed
                record.args = None
        elif isinstance(record.msg, str):
            record.msg = self.scrub(record.msg)

        exc_info = record.exc_info
        if isinstance(exc_info, tuple) and exc_info[0] is not None and not record.exc_text:
            text = "".join(traceback.format_exception(*exc_info))
            scrubbed_text = self.scrub(text)
            if scrubbed_text != text:
                record.exc_text = scrubbed_text
                record.exc_info = None

        if record.stack_info:
            scrubbed_stack = self.scrub(record.stack_info)
            if scrubbed_stack != record.stack_info:
                record.stack_info = scrubbed_stack

        for name, value in tuple(record.__dict__.items()):
            if name not in _UNSCRUBBED_RECORD_ATTRS and isinstance(value, str):
                record.__dict__[name] = self.scrub(value)

    def __repr__(self) -> str:
        """Describe the scrubber without revealing any registered value."""
        return f"SecretScrubber(registered={len(self._values)})"


SECRET_SCRUBBER: Final = SecretScrubber()
"""The process-wide scrubber used by the log record factory and formatter."""


class ScrubbingFormatter(logging.Formatter):
    """Format a record and scrub secrets out of the finished text.

    Scrubbing the finished string catches everything a format string
    interpolates, including ``extra`` fields such as ``%(token)s``.

    :meth:`format` never raises: a raising formatter makes the standard library
    print ``--- Logging error ---`` plus a real traceback to stderr, which would
    both leak the value being formatted and break tests that demand clean output.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Return the formatted record with secrets redacted.

        :param record: record to format.
        """
        try:
            return SECRET_SCRUBBER.scrub(super().format(record))
        except Exception:
            return super().format(record)


def install_log_scrubber() -> None:
    """Install the secret-scrubbing log record factory exactly once.

    Idempotency is detected by inspecting the installed factory (it carries a
    ``_doorbell_scrubber`` tag), not by a module flag: tests replace the factory
    to isolate logging state, which would leave a flag permanently set.
    """
    current = logging.getLogRecordFactory()
    if getattr(current, _SCRUBBER_TAG, False):
        return

    def _factory(*args: object, **kwargs: object) -> logging.LogRecord:
        """Build a record with the wrapped factory, then scrub it."""
        record = current(*args, **kwargs)
        SECRET_SCRUBBER.scrub_record(record)
        return record

    setattr(_factory, _SCRUBBER_TAG, True)
    logging.setLogRecordFactory(_factory)


def _add_file_handler(root: logging.Logger, log_file: Path) -> None:
    """Add a rotating file handler for *log_file* unless one already owns it.

    :param root: root logger to attach the handler to.
    :param log_file: log file to write, created with its parent directory.
    """
    path = log_file.expanduser().resolve()
    for handler in root.handlers:
        # Both sides are resolved so a symlinked data directory cannot make the
        # same file look like two different ones and add a duplicate handler.
        if not isinstance(handler, logging.FileHandler):
            continue
        if Path(handler.baseFilename).resolve() == path:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        path,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUPS,
        encoding="utf-8",
    )
    file_handler.setFormatter(ScrubbingFormatter(_LOG_FORMAT))
    root.addHandler(file_handler)


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Everything the web server needs to start.

    Values are resolved with a fixed precedence chain for the host, port and log
    level: **command line beats environment, environment beats the stored
    setting, which beats the default**. ``CHIMEDECK_LOG_LEVEL`` also counts
    as explicit, so a level chosen there is never overridden by the
    ``system.log_level`` setting; that setting is only applied when neither the
    command line nor the environment supplied a level (see
    :func:`apply_stored_log_level`).

    The environment equivalents are ``CHIMEDECK_HOST``,
    ``CHIMEDECK_PORT`` and ``CHIMEDECK_LOG_LEVEL``; the data directory
    reuses the store's own variables (``CHIMEDECK_DATA_DIR`` then
    ``CHIMEDECK_DATA`` then ``./data``) through
    :func:`chime_deck.store.default_db_path`. ``CHIMEDECK_LOG_FILE``
    turns file logging on or off (``1``/``true``/``yes``/``on`` versus
    ``0``/``false``/``no``/``off``); it is an environment variable rather than a
    flag because ``--help`` cannot mention environment variables and the option
    is a diagnostic aid, not a daily switch.

    The ``CHIMEDECK_`` names are canonical. The pre-rename spellings are still
    accepted for one release and log a deprecation warning naming their
    replacement, and the new name wins when
    both are set, so an existing unit file keeps working across the rename
    instead of silently losing its data directory. All of those lookups go
    through :func:`chime_deck.env.env_value`; see ``docs/installing.md`` for the
    operator-facing version of this note.

    The settings stored in ``data/doorbell.db`` (``server.host``,
    ``server.port``) are deliberately **not** consulted here: they cannot be read
    before the database is open. ``system.log_level`` is the one exception, and
    even that is applied by the application lifespan once the store is open.

    Defaults follow decision D13 (bind ``127.0.0.1`` so the unauthenticated
    API stays local) and decision D01 (one process, one asyncio event loop, so
    there is exactly one of these objects per process).

    ``db_path`` and ``static_dir`` are test seams: production code leaves them
    as ``None`` and derives both from ``data_dir`` and the repository layout.
    ``sounds_dir``, ``icons_dir`` and ``voices_dir`` are the other seams of that
    kind, deriving ``data_dir/sounds``, ``data_dir/icons`` and
    ``data_dir/voices`` when unset. ``seed_demo_sounds`` is a dataclass-only
    switch for tests: it has **no** CLI flag, environment variable or
    ``SETTINGS`` key in v1, so an operator cannot turn demo seeding off yet (it
    is always on in production).

    ``role`` is the D24 switch that runs this process as the doorbell
    (``"server"``) or as another doorbell's renderer (``"renderer"``). It is
    **CLI-only and has no environment variable on purpose**: an environment
    variable could enable a network-facing renderer invisibly, so the one switch
    that exposes this project's only arbitrary-text endpoint has to be typed in a
    unit file or on the command line.

    ``admin_host``, ``admin_port`` and ``admin_enabled`` describe the second,
    configuration-only listener (HSD-n6wi; see :func:`admin_bind`). Like the
    doorbell's own ``host``/``port`` they are resolved from the command line and
    the environment, never from a stored setting: a stored bind that is not
    consulted would be a lie. ``CHIMEDECK_ADMIN`` switches the listener off, and
    an install that never sets it keeps today's single listener.
    """

    data_dir: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    log_level: str = DEFAULT_LOG_LEVEL
    db_path: Path | None = None
    static_dir: Path | None = None
    sounds_dir: Path | None = None
    icons_dir: Path | None = None
    voices_dir: Path | None = None
    seed_demo_sounds: bool = True
    log_file: bool = False
    log_level_explicit: bool = False
    role: str = DEFAULT_ROLE
    admin_host: str = DEFAULT_ADMIN_HOST
    admin_port: int = DEFAULT_ADMIN_PORT
    admin_enabled: bool = True

    @property
    def database_path(self) -> Path:
        """Return the SQLite file to open, deriving it from ``data_dir`` if unset."""
        if self.db_path is not None:
            return self.db_path
        return self.data_dir / "doorbell.db"

    @property
    def resolved_static_dir(self) -> Path:
        """Return the directory the built frontend is served from."""
        if self.static_dir is not None:
            return self.static_dir
        return default_static_dir()

    @property
    def resolved_sounds_dir(self) -> Path:
        """Return the sound library directory to scan and download into.

        An explicit ``sounds_dir`` wins over ``data_dir / "sounds"``. Both sides
        are resolved the way :func:`_add_file_handler` resolves a log file, so a
        symlinked data directory cannot make the same library look like two
        different directories.
        """
        directory = self.sounds_dir if self.sounds_dir is not None else self.data_dir / "sounds"
        return directory.expanduser().resolve()

    @property
    def resolved_icons_dir(self) -> Path:
        """Return the directory custom SVG/PNG icons are read from.

        An explicit ``icons_dir`` wins over ``data_dir / "icons"``. Both sides
        are expanded and resolved the way :meth:`resolved_sounds_dir` resolves
        the sound library, so a symlinked data directory cannot make the same
        icon look like two different files, and ``custom:`` validation compares
        resolved paths only. The directory is never created here: it may not
        exist, and a missing directory simply means no custom icons.
        """
        directory = self.icons_dir if self.icons_dir is not None else self.data_dir / "icons"
        return directory.expanduser().resolve()

    @property
    def resolved_voices_dir(self) -> Path:
        """Return the directory local Piper voice models are read from.

        An explicit ``voices_dir`` wins over ``data_dir / "voices"``. Both sides
        are expanded and resolved the way :meth:`resolved_sounds_dir` resolves
        the sound library, so a symlinked data directory cannot make the same
        voice look like two different files. The directory is never created
        here: a missing directory simply means no voice is installed, and the
        engine reports that with an actionable reason.
        """
        directory = self.voices_dir if self.voices_dir is not None else self.data_dir / "voices"
        return directory.expanduser().resolve()

    @property
    def log_file_path(self) -> Path | None:
        """Return the rotating log file to write, or ``None`` when file logging is off."""
        if not self.log_file:
            return None
        return self.data_dir / "logs" / "chime-deck.log"


def admin_bind(config: ServerConfig) -> tuple[str, int] | None:
    """Return the admin listener's bind address, or ``None`` when it is disabled.

    One function rather than a scattered ``if config.admin_enabled``: the CLI, the
    application lifespan and the tests all have to agree on whether the listener
    exists, and ``None`` is the one shape that cannot be mistaken for a bind.

    :param config: resolved server configuration.
    """
    if not config.admin_enabled:
        return None
    return (config.admin_host, config.admin_port)


def packaged_static_dir() -> Path | None:
    """Return the built-UI directory shipped inside this package, or ``None``.

    The wheel carries the frontend: ``hatch`` force-includes ``web/dist`` as
    ``chime_deck/static`` (``pyproject.toml``), so an installed copy has no
    repository to walk up to and must be found through the package itself. The
    same ``Path(str(...))`` pattern as
    :func:`chime_deck.sounds.library.bundled_demo_sounds_dir` is used
    deliberately: serving needs a real filesystem path, which an unpacked wheel
    install has. A zipimported package (zipapp, ``.pyz``, an unextracted egg)
    yields a ``zipfile.Path`` rather than a directory, ``is_dir()`` is then
    ``False``, and the caller degrades to the source tree and finally to the
    placeholder page — unpacked installs are what is supported.
    """
    resource = importlib.resources.files("chime_deck").joinpath("static")
    path = Path(str(resource))
    return path if path.is_dir() else None


def source_tree_static_dir() -> Path:
    """Return the repository's ``web/dist`` directory.

    The layout is fixed by design 05 §5: the built frontend lands in
    ``web/dist`` next to ``src/``. This path only exists in a source checkout;
    an installed wheel resolves :func:`packaged_static_dir` instead.
    """
    return Path(__file__).resolve().parents[2] / "web" / "dist"


def default_static_dir() -> Path:
    """Return the built UI used when no explicit directory is configured.

    The package-internal copy wins over the source tree. In production that is
    the only copy there is, and putting it first means a stray
    ``<sys.prefix>/lib/python3.x/web/dist`` — exactly what the old
    ``parents[2]`` guess resolved to in an installed wheel — can never shadow
    the assets the wheel actually ships. In a source checkout
    ``src/chime_deck/static`` does not exist (``hatch`` materialises it only
    inside a built wheel), so development still serves ``web/dist``.
    """
    packaged = packaged_static_dir()
    return packaged if packaged is not None else source_tree_static_dir()


def is_loopback(host: str) -> bool:
    """Return whether *host* accepts loopback connections only.

    :param host: bind address from the CLI or the environment.
    """
    candidate = host.strip().lower()
    if candidate in {"localhost", "::1", "[::1]"}:
        return True
    # Every address in 127.0.0.0/8 is loopback, e.g. 127.0.0.1 or 127.1.2.3.
    return candidate.startswith("127.")


def parse_port(value: str) -> int:
    """Parse a TCP port and check its range.

    :param value: port as text, from the CLI or the environment.
    :raises ValueError: if *value* is not an integer inside 0-65535.
    """
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError(f"port must be an integer, got {value!r}") from exc
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValueError(f"port must be between {MIN_PORT} and {MAX_PORT}, got {port}")
    return port


def parse_env_flag(name: str, raw: str | None) -> bool:
    """Parse a boolean environment variable.

    An unset variable and an **empty** one both mean false, so
    ``CHIMEDECK_LOG_FILE=`` switches the feature off rather than raising:
    that is how the variable is written in a compose file or a shell profile.

    :param name: variable name, used in the error message.
    :param raw: raw text of the variable, or ``None`` when it is unset.
    :raises ValueError: for an unrecognised value.
    """
    if raw is None:
        return False
    candidate = raw.strip().lower()
    if candidate in _ENV_TRUTHY:
        return True
    if candidate in _ENV_FALSY:
        return False
    raise ValueError(
        f"{name} must be true (1, true, yes, on) or false (0, false, no, off), got {raw!r}"
    )


def normalise_log_level(value: str) -> str:
    """Upper-case a log level and check that it is one we accept.

    :param value: log level as text, from the CLI or the environment.
    :raises ValueError: if *value* is not one of :data:`LOG_LEVELS`.
    """
    level = value.strip().upper()
    if level not in LOG_LEVELS:
        raise ValueError(f"log level must be one of {', '.join(LOG_LEVELS)}, got {value!r}")
    return level


def resolve_config(args: argparse.Namespace) -> ServerConfig:
    """Build a :class:`ServerConfig` from parsed CLI arguments and the environment.

    Precedence is CLI > environment > default for the host, the port and the
    log level. The data directory comes from ``--data-dir`` when given and from
    the store's environment variables otherwise, so there is only one
    ``CHIMEDECK_DATA*`` scheme in the project.

    The admin listener resolves the same way from ``--admin-host``/
    ``CHIMEDECK_ADMIN_HOST`` and ``--admin-port``/``CHIMEDECK_ADMIN_PORT``, and
    ``--no-admin`` or a falsy ``CHIMEDECK_ADMIN`` switches it off (unset means
    enabled). Two combinations are refused, because each one would produce a
    listener the operator cannot reason about: the admin address being the
    doorbell's own address (two servers cannot share one socket), and port ``0``
    on a non-loopback admin host (the OS would pick a password-free configuration
    port nobody can discover).

    :param args: namespace produced by ``chime_deck.cli.build_parser``.
    :raises ValueError: if an environment value cannot be used, or the two binds
        collide.
    """
    cli_data_dir = cast("Path | str | None", getattr(args, "data_dir", None))
    cli_host = cast("str | None", getattr(args, "host", None))
    cli_port = cast("int | None", getattr(args, "port", None))
    cli_log_level = cast("str | None", getattr(args, "log_level", None))

    data_dir = Path(cli_data_dir) if cli_data_dir is not None else default_db_path().parent
    host = cli_host or env_value("HOST") or DEFAULT_HOST

    env_port = env_value("PORT")
    if cli_port is not None:
        port = cli_port
    elif env_port:
        try:
            port = parse_port(env_port)
        except ValueError as exc:
            raise ValueError(f"CHIMEDECK_PORT: {exc}") from exc
    else:
        port = DEFAULT_PORT

    raw_log_level = cli_log_level or env_value("LOG_LEVEL")
    log_level_explicit = bool(raw_log_level)
    if raw_log_level:
        try:
            log_level = normalise_log_level(raw_log_level)
        except ValueError as exc:
            raise ValueError(f"CHIMEDECK_LOG_LEVEL: {exc}") from exc
    else:
        log_level = DEFAULT_LOG_LEVEL

    env_log_file = env_value("LOG_FILE")
    try:
        log_file = parse_env_flag("CHIMEDECK_LOG_FILE", env_log_file)
    except ValueError as exc:
        raise ValueError(f"CHIMEDECK_LOG_FILE: {exc}") from exc

    cli_role = cast("str | None", getattr(args, "role", None))
    role = DEFAULT_ROLE if cli_role is None else cli_role
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}, got {role!r}")

    cli_admin_host = cast("str | None", getattr(args, "admin_host", None))
    cli_admin_port = cast("int | None", getattr(args, "admin_port", None))
    no_admin = bool(getattr(args, "no_admin", False))
    admin_host = cli_admin_host or env_value("ADMIN_HOST") or DEFAULT_ADMIN_HOST

    env_admin_port = env_value("ADMIN_PORT")
    if cli_admin_port is not None:
        admin_port = cli_admin_port
    elif env_admin_port:
        try:
            admin_port = parse_port(env_admin_port)
        except ValueError as exc:
            raise ValueError(f"CHIMEDECK_ADMIN_PORT: {exc}") from exc
    else:
        admin_port = DEFAULT_ADMIN_PORT

    if no_admin:
        admin_enabled = False
    else:
        raw_admin = env_value("ADMIN")
        admin_enabled = True if raw_admin is None else parse_env_flag("CHIMEDECK_ADMIN", raw_admin)

    if admin_enabled and (admin_host, admin_port) == (host, port):
        raise ValueError(
            "the admin listener cannot bind the same address as the doorbell "
            f"({host}:{port}) — set --admin-host/--admin-port (or CHIMEDECK_ADMIN_HOST/"
            "CHIMEDECK_ADMIN_PORT) to a second address, or --no-admin to switch it off"
        )
    if admin_enabled and admin_port == 0 and not is_loopback(admin_host):
        raise ValueError(
            "admin port 0 (let the OS choose) is only allowed on a loopback admin "
            "address, because the chosen port could not be found on the network; "
            f"got --admin-host {admin_host} — pick a real port, or bind 127.0.0.1"
        )

    return ServerConfig(
        data_dir=data_dir,
        host=host,
        port=port,
        log_level=log_level,
        log_file=log_file,
        log_level_explicit=log_level_explicit,
        role=role,
        admin_host=admin_host,
        admin_port=admin_port,
        admin_enabled=admin_enabled,
    )


async def refresh_secret_scrubber(store: ConfigStore) -> None:
    """Register the stored secret values so log records scrub them.

    Call it once the store is open and again after any secret setting is
    written; :meth:`SecretScrubber.set_secrets` replaces the whole set, so this
    single call covers every secret-bearing row the store knows about — the
    registered secret keys and the provider API keys alike. **Every future
    writer of a secret setting must call it too.**

    :param store: open configuration store to read the secrets from.
    """
    SECRET_SCRUBBER.set_secrets(await store.all_secret_values())


async def apply_stored_log_level(config: ServerConfig, store: ConfigStore) -> str | None:
    """Apply the stored ``system.log_level`` when neither the CLI nor the environment set one.

    Precedence: command line > environment > stored setting > :data:`DEFAULT_LOG_LEVEL`.
    Returns the level it applied, or ``None`` when nothing changed. A stored value that is not a
    known level is ignored with a warning.

    :param config: resolved server configuration, carrying the precedence flag.
    :param store: open configuration store to read the setting from.
    """
    if config.log_level_explicit:
        return None
    stored = await store.get_setting("system.log_level")
    if not isinstance(stored, str):
        return None
    try:
        level = normalise_log_level(stored)
    except ValueError:
        _LOGGER.warning("ignoring stored system.log_level %r: not a known level", stored)
        return None
    logging.getLogger().setLevel(level)
    return level


def configure_logging(level: str, *, log_file: Path | None = None) -> None:
    """Configure the root logger so the process logs to stderr at *level*.

    ``logging.basicConfig`` silently does nothing when the root logger already
    has handlers, which is always true under pytest, so the root level is set
    explicitly and the ``StreamHandler`` is only added when none exists. That
    makes the function idempotent and safe to call from tests.

    With *log_file* the process also writes a size-bounded rotating file (see
    :data:`LOG_FILE_MAX_BYTES` and :data:`LOG_FILE_BACKUPS`), created under the
    data directory when ``CHIMEDECK_LOG_FILE`` is set; passing the same path
    twice adds nothing the second time.

    Both sinks format through :class:`ScrubbingFormatter` and
    :func:`install_log_scrubber` wraps the log record factory, so stored
    credentials never reach the console or the log file.

    :param level: one of :data:`LOG_LEVELS` (case-insensitive).
    :param log_file: rotating log file to write, or ``None`` for console only.
    """
    install_log_scrubber()
    root = logging.getLogger()
    root.setLevel(normalise_log_level(level))
    if not root.handlers:
        console = logging.StreamHandler()
        console.setFormatter(ScrubbingFormatter(_LOG_FORMAT))
        root.addHandler(console)
    if log_file is not None:
        _add_file_handler(root, log_file)
