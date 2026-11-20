"""The page size the entity and icon pickers share.

Both list routes — ``GET /api/ha/entities`` (the entity picker) and
``GET /api/icons/search`` (the icon picker) — take the same ``limit`` query
parameter and apply it to the same kind of answer: a filtered, ordered list whose
head is what the user wants. They live in different modules with different data,
so the numbers are defined once, here, and imported: two constants in two files
is how two pickers end up disagreeing about how many rows one screenful holds.

``limit`` is the response's row count, not a request the caller has to police,
because the cap is applied after filtering and ordering. It is an inclusive
range: ``1`` (one row) to :data:`SEARCH_LIMIT_MAX`, and an out-of-range value is
the standard ``422`` error envelope rather than a clamp, so the client is told its
mistake instead of silently getting a different page size than it asked for.

Owning ticket: HSD-zyru.
"""

from __future__ import annotations

from typing import Final

__all__ = ["SEARCH_LIMIT_DEFAULT", "SEARCH_LIMIT_MAX"]

SEARCH_LIMIT_DEFAULT: Final = 50
"""Rows a list route returns when the caller sends no ``limit``.

Wide enough to be a useful first screenful of a picker — the user's own tiles,
or the head of one icon prefix — and small enough that an untouched picker never
transfers thousands of rows nobody read.
"""

SEARCH_LIMIT_MAX: Final = 200
"""The largest ``limit`` a list route accepts, inclusive.

A caller that asks for more is rejected with a ``422`` instead of being clamped:
the cap is part of the contract, and a silent clamp would let a client believe it
had the whole list when it had two hundred rows.
"""
