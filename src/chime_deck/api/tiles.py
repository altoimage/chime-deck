"""Tiles API: the runtime tile-selection surface (D29).

``POST /api/tiles/{tile_id}/selection`` is body-less, toggles the tile's
selection and answers the **new** value. Selection is process-lifetime intent
(D29 §4), not configuration: :mod:`chime_deck.selection` holds it in one
volatile set, no SQLite column exists for it, nothing is migrated and no settings
key carries it, so a restart starts with every tile unselected.

**Why this endpoint and not ``PATCH /api/entities/{id}`` with a ``selected``
field.** ``/api/entities`` is the store-backed configuration CRUD surface, used
by Settings and by configuration import/export, and its documented contract is
"the store owns the write and emits the single ``config.entities_changed``, this
module publishes nothing itself". A volatile field on that body would break the
contract three ways: it would either have to be written to the store (it must
not) or silently skipped, which makes a route whose body is half configuration
and half runtime state; a selection change must **not** emit
``config.entities_changed``, or every client refetches the configuration because
a border moved; and PATCH means *set*, while this ticket asks for a *toggle* — a
``selected: true`` in a configuration body invites the reader to believe it
persists. Keeping the selection under ``/api/tiles`` makes the separation visible
in the URL and leaves ``/api/entities`` byte-for-byte intact, so a configuration
import can never resurrect a selection.

**Why the body is empty.** D29 §3 makes the panel the only writer, and the new
value comes back both in this response and in the ``entity_state`` broadcast the
hub sends, so a lost response cannot leave the panel disagreeing with the server.

``{tile_id}`` is the entity row's numeric primary key — the ``id`` of a snapshot
row and of :class:`~chime_deck.api.entities.EntityResponse` — matching
``/api/entities/{entity_id}``'s existing convention. The selection set itself is
keyed on the ``entity_id`` string (``lock.front_door``), so reordering the tiles
is a no-op for it.

The route publishes nothing itself: :meth:`~chime_deck.selection.SelectionService.toggle`
calls the service's ``on_change``, which
:func:`~chime_deck.app.create_app` wires to
:meth:`~chime_deck.hub.StateHub.notify_selection`. That is also the seam the
MQTT publisher (HSD-fro0) will subscribe to.

Owning ticket: HSD-u6k5.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import BaseModel

from .errors import ApiError, ErrorCode

if TYPE_CHECKING:
    # ``app.py`` imports ``.api`` at module level, so a runtime import of
    # ``..app`` here would close the cycle (the same pattern as ``api/entities.py``).
    from ..app import AppState

__all__ = ["TileSelectionResponse", "build_tiles_router"]


class TileSelectionResponse(BaseModel):
    """The new selection of one tile, after a toggle."""

    id: int
    entity_id: str
    selected: bool


def _not_found(tile_id: int) -> ApiError:
    """Build the 404 for an id that names no entity.

    :param tile_id: primary key that was requested.
    """
    return ApiError(
        404,
        ErrorCode.NOT_FOUND,
        f"no entity has the id {tile_id}",
        {"id": tile_id},
    )


def build_tiles_router(state: AppState) -> APIRouter:
    """Build the ``/api/tiles`` router bound to *state*.

    Handlers are closures over *state*, following the house style in
    :mod:`chime_deck.app`: no handler annotates ``AppState``, so FastAPI never
    has to resolve a forward reference at import time.

    :param state: live application state providing the store and the selection service.
    """
    router = APIRouter(prefix="/api/tiles", tags=["tiles"])

    @router.post("/{tile_id}/selection")
    async def toggle_selection(tile_id: int) -> TileSelectionResponse:
        """Flip one tile's selection and answer the new value.

        The row is read first so an unknown id is a 404 rather than a stray entry
        in the selection set; the toggle itself is in memory and writes nothing.
        The selection service's ``on_change`` calls the hub, so this route
        publishes nothing.

        :param tile_id: primary key of the entity to toggle.
        :raises ApiError: if no entity has that id.
        """
        row = await state.store.get_entity_by_id(tile_id)
        if row is None:
            raise _not_found(tile_id)
        selected = state.selection.toggle(row.entity_id)
        return TileSelectionResponse(id=row.id, entity_id=row.entity_id, selected=selected)

    return router
