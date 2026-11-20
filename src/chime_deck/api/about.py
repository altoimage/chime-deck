"""About API: the version, the one path an operator needs, and a log excerpt.

``GET /api/about`` answers "what am I running, and where does it keep its data"
and nothing else. ``data_dir`` is served verbatim (a relative configured path
stays relative) because it is the single path the operator needs in order to
find their database, their sounds and their log; the database path, the log path
and the sounds/icons/voices directories are deliberately **not** served, both
because they are derivable from it and because a path list is a map of the
filesystem that this API does not need to hand out.

``GET /api/about/log`` serves the tail of the configured log file so the operator
can see a failure without a shell. The route takes **no path and no query
parameter at all**: it can only ever read ``ServerConfig.log_file_path``, so it
cannot be pointed at another file. The excerpt is bounded twice, by
:data:`LOG_EXCERPT_MAX_BYTES` (how much of the end of the file is read) and by
:data:`LOG_EXCERPT_MAX_LINES` (how many lines come back), and is written as
``text/plain`` as an attachment with ``Cache-Control: no-store``.

The body is scrubbed a second time by
:data:`~chime_deck.config.SECRET_SCRUBBER` even though everything this
process logs has already passed through
:class:`~chime_deck.config.ScrubbingFormatter` and the scrubbing record
factory. That is deliberate defence in depth: the file may have been written by an
older build, or by another process that shares the path, and neither of those ran
this process's scrubbing. The excerpt is the body and nothing else — no
prefix/suffix line that a client would have to strip.

Owning ticket: HSD-l22f.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel

from ..config import SECRET_SCRUBBER
from ..store.database import latest_schema_version
from .errors import ApiError, ErrorCode

if TYPE_CHECKING:
    # ``app.py`` imports ``.api`` at module level, so a runtime import of
    # ``..app`` here would close the cycle (the same pattern as ``api/buttons.py``).
    from pathlib import Path

    from ..app import AppState

__all__ = [
    "LOG_EXCERPT_MAX_BYTES",
    "LOG_EXCERPT_MAX_LINES",
    "LOG_FILENAME",
    "AboutResponse",
    "build_about_router",
]

LOG_EXCERPT_MAX_BYTES: Final = 64 * 1024
"""How many bytes from the end of the log file one excerpt may read."""

LOG_EXCERPT_MAX_LINES: Final = 500
"""How many lines one excerpt may return, counted from the end of that window."""

LOG_FILENAME: Final = "chime-deck.log"
"""The log file's name; used for the download filename only.

The path itself is never built here — it is whatever the process configured (see
:attr:`~chime_deck.config.ServerConfig.log_file_path`), so a route can never
name a file the process does not already write.
"""

_LOG_MISSING: Final = (
    "the log file does not exist yet — it is created as soon as something is logged; "
    "check that the process may write to its data directory"
)
"""404 message for an enabled log file that is not on disk."""

_LOG_LOGGING_OFF: Final = (
    "file logging is off, so there is no log file to read — start the service with "
    "--log-file, or set CHIMEDECK_LOG_FILE=1, to write one under the data directory"
)
"""404 message explaining how to turn file logging on."""

_LOG_UNREADABLE: Final = "the log file could not be read; check the process's file permissions"
"""500 message for a log file that exists but cannot be read.

It names no path: the operator already has ``GET /api/about``'s ``data_dir``.
"""


class AboutResponse(BaseModel):
    """What this build is, how long it has been up and where its data lives."""

    version: str
    schema_version: int
    uptime_s: float
    data_dir: str
    log_available: bool


def _read_excerpt(path: Path) -> str:
    """Return the bounded tail of *path*, without a partial first line.

    Blocking by design: it is called through :func:`asyncio.to_thread` so the
    event loop is never held while the disk is read.

    :param path: log file to read the end of.
    :raises OSError: if the file cannot be opened or read.
    """
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        offset = max(0, handle.tell() - LOG_EXCERPT_MAX_BYTES)
        handle.seek(offset)
        data = handle.read(LOG_EXCERPT_MAX_BYTES)
    text = data.decode("utf-8", errors="replace")
    if offset > 0:
        # The window started mid-file, so its first line is a fragment: drop it
        # rather than hand the client half a log line that looks like a whole one.
        newline = text.find("\n")
        text = "" if newline < 0 else text[newline + 1 :]
    lines = text.splitlines()
    return "\n".join(lines[-LOG_EXCERPT_MAX_LINES:])


def _log_missing() -> ApiError:
    """Build the 404 for a configured log file that is not on disk."""
    return ApiError(404, ErrorCode.NOT_FOUND, _LOG_MISSING)


def build_about_router(state: AppState) -> APIRouter:
    """Build the ``/api/about`` router bound to *state*.

    Handlers are closures over *state*, following the house style in
    :mod:`chime_deck.app`: no handler annotates ``AppState``, so FastAPI
    never has to resolve a forward reference at import time.

    :param state: live application state providing the config and the store.
    """
    router = APIRouter(prefix="/api/about", tags=["about"])

    @router.get("")
    async def about() -> AboutResponse:
        """Report the version, the schema and the one path worth telling the operator.

        ``schema_version`` is the version this build migrates *to*, and refuses to
        go beyond (:func:`~chime_deck.store.database.latest_schema_version`),
        so after a successful start it is also the version of the file on disk.
        """
        return AboutResponse(
            version=state.version,
            schema_version=latest_schema_version(),
            uptime_s=round(state.uptime_s(), 3),
            data_dir=str(state.config.data_dir),
            log_available=state.config.log_file_path is not None,
        )

    @router.get("/log")
    async def log_excerpt() -> Response:
        """Serve the bounded, scrubbed tail of the configured log file.

        The route takes no path and no query parameter at all, so the only file it
        can ever read is the one ``ServerConfig.log_file_path`` names for this
        process.

        :raises ApiError: 404 when file logging is off or the file is missing.
        """
        path = state.config.log_file_path
        if path is None:
            raise ApiError(404, ErrorCode.NOT_FOUND, _LOG_LOGGING_OFF)
        try:
            excerpt = await asyncio.to_thread(_read_excerpt, path)
        except FileNotFoundError:
            raise _log_missing() from None
        except OSError:
            raise ApiError(500, ErrorCode.INTERNAL_ERROR, _LOG_UNREADABLE) from None
        return Response(
            content=SECRET_SCRUBBER.scrub(excerpt),
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{LOG_FILENAME}"',
                "Cache-Control": "no-store",
            },
        )

    return router
