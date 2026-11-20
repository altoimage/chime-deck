"""History API: the press log, newest first, one page at a time.

``GET /api/history`` is the read half of :meth:`ConfigStore.log_press`: it
serves the rows the press path already wrote, and nothing on this route writes.
The rows are the newest ones first (``pressed_at DESC, id DESC``) so the head of
the page is what the operator wants, and ``limit`` is applied **after** the
``outcome`` filter because the filter lives in the SQL — asking for the single
newest failure answers with the newest failure even when the newest rows overall
succeeded.

``limit`` is an inclusive ``1 .. PRESS_LOG_RETENTION`` range and an out-of-range
value is the standard ``422`` envelope rather than a clamp, following
:mod:`chime_deck.api.limits`: a client that asks for more rows than the table
can hold is told so instead of silently getting a different page. An unknown
``outcome`` is pydantic's own ``422``, the same shape as every other typed query
parameter.

There is deliberately no ``code`` field. ``press_log`` has no code column: the
machine-readable :class:`~chime_deck.press.PressCode` is derived at press time
and only ``outcome`` and the human ``detail`` are stored, so a history page built
from the table cannot show a code it never kept. :class:`HistoryRow` therefore
reuses :class:`~chime_deck.api.buttons.PressResponse`'s field names and
spellings verbatim, minus that one derived field, so the UI reads both shapes
with one decoder.

``latency_ms``, the enqueue-to-first-sound delay, is nullable for the same reason
``duration_ms`` is: a job that never started was never measured, so ``null``
means there was nothing to measure rather than that the measurement was zero.
``0`` is a real measurement — a warm backend that started the worker immediately
— which is why the field is a plain nullable int and not a sentinel: the route
serves exactly what the column holds, and an absent measurement stays absent.

``synthesis_ms`` is the press's **own** synthesis time, measured by the TTS
engine that produced the clip. It is nullable for a different reason from the
other two: ``null`` means nothing was synthesised for this press — a cache hit,
a bake hit, a sound button, a failed press, or an engine that keeps no
measurement. For a hit that is a **success** state, not a missing measurement.
It is not the same number as ``duration_ms`` or ``latency_ms`` and must not be
derived from them: this one is time spent making the audio, those two are time
spent playing it.

Owning ticket: HSD-l22f.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..press import PressOutcome
from ..store import PRESS_LOG_RETENTION, PressLog

if TYPE_CHECKING:
    # ``app.py`` imports ``.api`` at module level, so a runtime import of
    # ``..app`` here would close the cycle (the same pattern as ``api/buttons.py``).
    from ..app import AppState

__all__ = [
    "HISTORY_LIMIT_DEFAULT",
    "HistoryResponse",
    "HistoryRow",
    "build_history_router",
]

HISTORY_LIMIT_DEFAULT: Final = 50
"""Rows ``GET /api/history`` returns when the caller sends no ``limit``.

The same page size as the entity and icon pickers: one screenful of rows, small
enough that an untouched history view never transfers the whole retained table.
"""


class HistoryRow(BaseModel):
    """One stored press, in the shape the history view consumes."""

    id: int
    button_id: int | None
    button_name: str
    pressed_at: str
    source: str
    outcome: str
    duration_ms: int | None = None
    latency_ms: int | None = None
    synthesis_ms: int | None = None
    detail: str | None = None

    @classmethod
    def from_log(cls, entry: PressLog) -> HistoryRow:
        """Build the response body from a stored press log row.

        :param entry: row returned by the store.
        """
        return cls(
            id=entry.id,
            button_id=entry.button_id,
            button_name=entry.button_name,
            pressed_at=entry.pressed_at,
            source=entry.source,
            outcome=entry.outcome,
            duration_ms=entry.duration_ms,
            latency_ms=entry.latency_ms,
            synthesis_ms=entry.synthesis_ms,
            detail=entry.detail,
        )


class HistoryResponse(BaseModel):
    """One page of press history plus the retention it is a window onto."""

    rows: list[HistoryRow]
    retention: int


def build_history_router(state: AppState) -> APIRouter:
    """Build the ``/api/history`` router bound to *state*.

    Handlers are closures over *state*, following the house style in
    :mod:`chime_deck.app`: no handler annotates ``AppState``, so FastAPI
    never has to resolve a forward reference at import time.

    :param state: live application state providing the store.
    """
    router = APIRouter(prefix="/api/history", tags=["history"])

    @router.get("")
    async def list_history(
        limit: int = Query(HISTORY_LIMIT_DEFAULT, ge=1, le=PRESS_LOG_RETENTION),
        outcome: PressOutcome | None = None,
    ) -> HistoryResponse:
        """Return the newest press log entries, optionally only one outcome.

        :param limit: maximum number of rows, within the retained window.
        :param outcome: keep only presses that ended with this outcome.
        """
        rows = await state.store.recent_presses(
            limit, outcome=None if outcome is None else outcome.value
        )
        return HistoryResponse(
            rows=[HistoryRow.from_log(row) for row in rows],
            retention=PRESS_LOG_RETENTION,
        )

    return router
