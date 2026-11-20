"""History HTTP API: the press log page, its filter, its cap and its envelope.

Every test drives the real application through ``httpx.ASGITransport`` and seeds
the ``press_log`` table through the app's own store, so no socket is opened and
nothing outside ``tmp_path`` is touched. The two retention tests insert a
thousand-plus rows and are marked ``slow`` for that reason.

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

from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig
from chime_deck.store import PRESS_LOG_RETENTION, ConfigStore, PressLogIn

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_FAILED_DETAIL = "no audio output device was found"


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test."""
    with preserve_log_state():
        yield


def _build_app(tmp_path: Path) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory used as the data directory.
    """
    config = ServerConfig(
        data_dir=tmp_path,
        static_dir=tmp_path / "no-dist",
        seed_demo_sounds=False,
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


def _rows(response: httpx.Response) -> list[dict[str, object]]:
    """Return the ``rows`` array of a history page.

    :param response: successful history response.
    """
    return cast("list[dict[str, object]]", _body(response)["rows"])


def _error(response: httpx.Response) -> dict[str, object]:
    """Return the ``error`` object of a failed response.

    :param response: response with the standard error envelope.
    """
    return cast("dict[str, object]", response.json()["error"])


def _detail(response: httpx.Response) -> dict[str, object]:
    """Return the ``error.detail`` object of a failed response.

    :param response: response with the standard error envelope.
    """
    return cast("dict[str, object]", _error(response)["detail"])


def _fields(response: httpx.Response) -> list[dict[str, object]]:
    """Return the ``error.detail.fields`` list of a pydantic-shaped 422.

    :param response: response with the standard error envelope.
    """
    return cast("list[dict[str, object]]", _detail(response)["fields"])


def _stamp(index: int) -> str:
    """Return a distinct, sortable ``pressed_at`` value for *index*.

    :param index: monotonically increasing seed index.
    """
    return f"2026-01-01T00:00:00.{index:06d}Z"


async def _log(
    store: ConfigStore,
    *,
    name: str,
    outcome: str = "played",
    pressed_at: str,
    detail: str | None = None,
    job_id: str | None = None,
    synthesis_ms: int | None = None,
) -> None:
    """Insert one press log row.

    :param store: connected store of the application under test.
    :param name: button name recorded on the row.
    :param outcome: outcome the press ended with.
    :param pressed_at: canonical UTC timestamp for the row.
    :param detail: human detail, or ``None``.
    :param job_id: id of the dispatched job, or ``None``.
    :param synthesis_ms: measured synthesis time, or ``None`` for a press that
        synthesised nothing (a hit, a sound button, a failed press).
    """
    await store.log_press(
        PressLogIn(
            button_id=None,
            button_name=name,
            source="mqtt",
            outcome=outcome,
            pressed_at=pressed_at,
            detail=detail,
            job_id=job_id,
            synthesis_ms=synthesis_ms,
        )
    )


@pytest.mark.slow
async def test_daily_traffic_keeps_exactly_the_retained_window(tmp_path: Path) -> None:
    """1001 presses leave 1000 rows and the route serves all of them.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        for index in range(PRESS_LOG_RETENTION + 1):
            await _log(state.store, name=f"Press {index}", pressed_at=_stamp(index))

        response = await client.get(f"/api/history?limit={PRESS_LOG_RETENTION}")

        assert response.status_code == 200
        body = _body(response)
        rows = _rows(response)
        assert len(rows) == PRESS_LOG_RETENTION
        assert body["retention"] == PRESS_LOG_RETENTION
        assert rows[0]["button_name"] == f"Press {PRESS_LOG_RETENTION}"
        assert rows[-1]["button_name"] == "Press 1"
        assert "Press 0" not in {row["button_name"] for row in rows}


async def test_limit_returns_that_many_newest_rows(tmp_path: Path) -> None:
    """``limit=3`` is the three newest rows, newest first.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        for index in range(5):
            await _log(state.store, name=f"Press {index}", pressed_at=_stamp(index))

        response = await client.get("/api/history?limit=3")

        assert response.status_code == 200
        assert [row["button_name"] for row in _rows(response)] == [
            "Press 4",
            "Press 3",
            "Press 2",
        ]


async def test_default_limit_is_the_shared_page_size(tmp_path: Path) -> None:
    """No ``limit`` means the documented default, not the whole table.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/history")

        assert response.status_code == 200
        assert _body(response) == {"rows": [], "retention": PRESS_LOG_RETENTION}


@pytest.mark.parametrize("limit", ["0", "-1", "1001", "abc"])
async def test_out_of_range_limits_are_422_envelopes(tmp_path: Path, limit: str) -> None:
    """A ``limit`` outside ``1..retention`` is a 422, never a clamp.

    :param tmp_path: pytest temporary directory.
    :param limit: query value that must be rejected.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/history", params={"limit": limit})

        assert response.status_code == 422
        error = _error(response)
        assert error["code"] == "validation_error"
        assert _fields(response)[0]["loc"] == ["query", "limit"]


async def test_identical_timestamps_are_ordered_by_id_desc(tmp_path: Path) -> None:
    """Two rows written in the same microsecond still have one total order.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        for name in ("First", "Second", "Third"):
            await _log(state.store, name=name, pressed_at="2026-01-01T12:00:00.000000Z")

        rows = _rows(await client.get("/api/history"))

        assert [row["button_name"] for row in rows] == ["Third", "Second", "First"]
        ids = [cast(int, row["id"]) for row in rows]
        assert ids == sorted(ids, reverse=True)


async def test_outcome_filter_returns_only_that_outcome(tmp_path: Path) -> None:
    """``outcome`` keeps matching rows only, and they stay newest first.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _log(state.store, name="Older fail", outcome="failed", pressed_at=_stamp(0))
        await _log(state.store, name="Older play", outcome="played", pressed_at=_stamp(1))
        await _log(state.store, name="Newer fail", outcome="failed", pressed_at=_stamp(2))

        rows = _rows(await client.get("/api/history", params={"outcome": "failed"}))

        assert [row["button_name"] for row in rows] == ["Newer fail", "Older fail"]
        assert {row["outcome"] for row in rows} == {"failed"}


async def test_outcome_filter_applies_before_the_limit(tmp_path: Path) -> None:
    """The newest *matching* row is served, not an empty page.

    The two newest rows overall are ``played``, so an implementation that
    filtered after applying ``limit`` would return nothing here.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _log(state.store, name="Fail 1", outcome="failed", pressed_at=_stamp(0))
        await _log(state.store, name="Fail 2", outcome="failed", pressed_at=_stamp(1))
        await _log(state.store, name="Play 3", outcome="played", pressed_at=_stamp(2))
        await _log(state.store, name="Play 4", outcome="played", pressed_at=_stamp(3))

        response = await client.get("/api/history", params={"outcome": "failed", "limit": "1"})

        assert response.status_code == 200
        rows = _rows(response)
        assert len(rows) == 1
        assert rows[0]["button_name"] == "Fail 2"


async def test_unknown_outcome_is_a_422_envelope(tmp_path: Path) -> None:
    """An outcome outside the enum is rejected by pydantic's own 422.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/history", params={"outcome": "nonsense"})

        assert response.status_code == 422
        assert _error(response)["code"] == "validation_error"
        assert _fields(response)[0]["loc"] == ["query", "outcome"]


async def test_failed_row_exposes_its_detail_verbatim(tmp_path: Path) -> None:
    """A failure's ``detail`` is served exactly as the press path wrote it.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _log(
            state.store,
            name="Front Door",
            outcome="failed",
            pressed_at=_stamp(0),
            detail=_FAILED_DETAIL,
        )

        (row,) = _rows(await client.get("/api/history", params={"outcome": "failed"}))

        assert row["detail"] == _FAILED_DETAIL
        assert row["outcome"] == "failed"
        assert "code" not in row


async def test_latency_ms_round_trips_from_the_stored_row(tmp_path: Path) -> None:
    """A stored latency and duration are served with the row they belong to.

    The latency is written the way production writes it: the press path seeds a
    ``queued`` row carrying the job id, then the playback worker revises that row
    through :meth:`ConfigStore.record_playback`.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _log(
            state.store,
            name="Front Door",
            outcome="queued",
            pressed_at=_stamp(0),
            job_id="job-1",
        )
        await state.store.record_playback(
            job_id="job-1", outcome="played", duration_ms=1800, latency_ms=37
        )

        (row,) = _rows(await client.get("/api/history"))

        assert row["latency_ms"] == 37
        assert isinstance(row["latency_ms"], int)
        assert row["duration_ms"] == 1800


async def test_a_never_started_job_serialises_latency_as_null(tmp_path: Path) -> None:
    """A row with no playback result serves ``null`` for both measurements.

    Both fields describe the same missing fact: the job never started, so there
    was nothing to measure. Serving ``null`` rather than ``0`` for the latency
    keeps "never started" distinguishable from "started immediately", and the
    duration is asserted alongside it because the two are the same nullable
    family with the same meaning.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _log(
            state.store,
            name="Front Door",
            outcome="queued",
            pressed_at=_stamp(0),
            job_id="job-never-started",
        )

        (row,) = _rows(await client.get("/api/history"))

        assert "latency_ms" in row
        assert row["latency_ms"] is None
        assert row["duration_ms"] is None


async def test_zero_latency_is_kept_as_zero_not_null(tmp_path: Path) -> None:
    """A measured latency of ``0`` is served as ``0``, never as ``null``.

    A warm backend legitimately starts the worker immediately and measures a
    latency of zero, so a truthiness pass-through such as ``latency or None``
    would erase a real measurement.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _log(
            state.store,
            name="Front Door",
            outcome="queued",
            pressed_at=_stamp(0),
            job_id="job-2",
        )
        await state.store.record_playback(
            job_id="job-2", outcome="played", duration_ms=1800, latency_ms=0
        )

        (row,) = _rows(await client.get("/api/history"))

        assert row["latency_ms"] == 0
        assert row["latency_ms"] is not None


async def test_synthesis_ms_round_trips_from_the_stored_row(tmp_path: Path) -> None:
    """A press's own synthesis time is served with the row it belongs to.

    The press path writes it, so the row is seeded through :meth:`log_press` the
    way production seeds it — nothing in the playback path fills it in.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _log(
            state.store,
            name="Front Door",
            outcome="queued",
            pressed_at=_stamp(0),
            job_id="job-synthesis",
            synthesis_ms=240,
        )

        (row,) = _rows(await client.get("/api/history"))

        assert row["synthesis_ms"] == 240
        assert isinstance(row["synthesis_ms"], int)


async def test_a_press_that_synthesised_nothing_serialises_synthesis_as_null(
    tmp_path: Path,
) -> None:
    """A cache or bake hit serves ``null``, never ``0``.

    ``null`` means nothing was synthesised for this press, which for a hit is a
    success state rather than a missing measurement. Serving ``0`` would claim a
    real synthesis that took no time at all.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _log(
            state.store,
            name="Front Door",
            outcome="played",
            pressed_at=_stamp(0),
        )

        (row,) = _rows(await client.get("/api/history"))

        assert "synthesis_ms" in row
        assert row["synthesis_ms"] is None
