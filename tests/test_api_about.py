"""About HTTP API: the version block and the bounded, scrubbed log excerpt.

Every test drives the real application through ``httpx.ASGITransport`` with an
isolated data directory. The log file is written by the test rather than by a
logging handler, which is exactly the "an older or foreign process wrote this
file" case the excerpt's second scrubbing pass exists for.

Owning ticket: HSD-l22f.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state

from chime_deck.api.about import LOG_EXCERPT_MAX_BYTES, LOG_EXCERPT_MAX_LINES
from chime_deck.app import AppState, create_app
from chime_deck.config import REDACTED, SECRET_SCRUBBER, ServerConfig
from chime_deck.store.database import latest_schema_version

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_SECRET = "super-secret-token-value"
"""Value a foreign writer put in the log file; long enough to be scrubbed."""

_OLDEST = "OLDEST-LINE-MARKER"
"""Text at the far start of the oversized log file."""

_NEWEST = "NEWEST-LINE-MARKER"
"""Text at the very end of the oversized log file."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger, its handlers and the registered secrets."""
    with preserve_log_state():
        yield


def _build_app(tmp_path: Path, *, log_file: bool = False) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory used as the data directory.
    :param log_file: whether the configuration enables the rotating log file.
    """
    config = ServerConfig(
        data_dir=tmp_path,
        static_dir=tmp_path / "no-dist",
        seed_demo_sounds=False,
        log_file=log_file,
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
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=_HTTP_TIMEOUT_S
        ) as client:
            yield client


def _body(response: httpx.Response) -> dict[str, object]:
    """Return a decoded JSON object body.

    :param response: successful response.
    """
    return cast(dict[str, object], response.json())


def _error(response: httpx.Response) -> dict[str, object]:
    """Return the ``error`` object of a failed response.

    :param response: response with the standard error envelope.
    """
    return cast("dict[str, object]", response.json()["error"])


def _write_log(state: AppState, text: str) -> Path:
    """Write *text* to the log file ``state`` is configured for.

    :param state: application state whose configuration names the log file.
    :param text: exact file content to write.
    """
    path = state.config.log_file_path
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def test_about_reports_the_version_the_schema_and_one_path(tmp_path: Path) -> None:
    """The body is exactly the five documented fields and leaks no other path.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/about")

        assert response.status_code == 200
        body = _body(response)
        assert set(body) == {
            "version",
            "schema_version",
            "uptime_s",
            "data_dir",
            "log_available",
        }
        assert body["version"] == state.version
        assert body["schema_version"] == latest_schema_version()
        assert cast(float, body["uptime_s"]) >= 0
        assert body["data_dir"] == str(tmp_path)
        assert body["log_available"] is False
        assert ".db" not in response.text
        assert "logs/" not in response.text


async def test_log_route_explains_how_to_enable_file_logging(tmp_path: Path) -> None:
    """With file logging off the route is a 404 that says how to turn it on.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/about/log")

        assert response.status_code == 404
        error = _error(response)
        assert error["code"] == "not_found"
        message = cast(str, error["message"])
        assert "--log-file" in message
        assert "CHIMEDECK_LOG_FILE" in message


async def test_an_oversized_log_is_bounded_and_has_no_partial_first_line(
    tmp_path: Path,
) -> None:
    """A >100 KiB file comes back cut to the byte window with whole lines only.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path, log_file=True)

    async with _running(app) as client:
        padding = "x" * 99
        lines = [_OLDEST]
        lines.extend(f"2026-01-01 00:00:00 line {index:05d} {padding}" for index in range(1500))
        lines.append(_NEWEST)
        _write_log(state, "\n".join(lines) + "\n")
        assert state.config.log_file_path is not None
        assert state.config.log_file_path.stat().st_size > 100 * 1024

        response = await client.get("/api/about/log")

        assert response.status_code == 200
        assert len(response.text.encode()) <= LOG_EXCERPT_MAX_BYTES
        assert _NEWEST in response.text
        assert _OLDEST not in response.text
        first = response.text.splitlines()[0]
        assert first in lines
        assert len(response.text.splitlines()) <= LOG_EXCERPT_MAX_LINES
        assert response.headers["content-disposition"] == ('attachment; filename="chime-deck.log"')
        assert response.headers["content-type"].startswith("text/plain")


async def test_the_excerpt_keeps_at_most_the_line_cap(tmp_path: Path) -> None:
    """600 short lines come back as the newest 500 of them.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path, log_file=True)

    async with _running(app) as client:
        _write_log(state, "".join(f"line {index:03d}\n" for index in range(600)))

        response = await client.get("/api/about/log")

        assert response.status_code == 200
        body_lines = response.text.splitlines()
        assert len(body_lines) == LOG_EXCERPT_MAX_LINES
        assert body_lines[-1] == "line 599"
        assert body_lines[0] == "line 100"


async def test_the_excerpt_is_scrubbed_again_on_the_way_out(tmp_path: Path) -> None:
    """A secret a foreign writer logged verbatim never leaves the route.

    The file here is not written by this process's scrubbing handlers, which is
    the case the second scrubbing pass exists for.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path, log_file=True)

    async with _running(app) as client:
        SECRET_SCRUBBER.set_secrets([_SECRET])
        _write_log(state, f"2026-01-01 00:00:00 token={_SECRET} accepted\n")

        response = await client.get("/api/about/log")

        assert response.status_code == 200
        assert _SECRET not in response.text
        assert REDACTED in response.text


async def test_the_route_cannot_be_pointed_at_another_file(tmp_path: Path) -> None:
    """A ``path`` query parameter is ignored, because no route reads one.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path, log_file=True)

    async with _running(app) as client:
        _write_log(state, "2026-01-01 00:00:00 the only log line\n")

        response = await client.get("/api/about/log", params={"path": "/etc/passwd"})

        assert response.status_code == 200
        assert "the only log line" in response.text
        assert "root:" not in response.text
        assert response.text == "2026-01-01 00:00:00 the only log line"
