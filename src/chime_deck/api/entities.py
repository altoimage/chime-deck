"""Entities API: the status tiles' CRUD surface and reorder.

Owning ticket: HSD-jwd3.

``GET/POST /api/entities``, ``GET /api/entities/state-tones``,
``POST /api/entities/reorder`` and ``PATCH/DELETE /api/entities/{entity_id}`` are
the UI's whole contract for Home Assistant status tiles. ``GET
/api/entities/state-tones`` publishes the tone vocabulary and the built-in
default table, so the tile editor pre-fills from the one home that table has (the
code) and the browser keeps no copy. Every one of these routes is thin: the
store owns the write, the uniqueness rule and the event. **This module publishes
nothing itself** — :class:`~chime_deck.store.ConfigStore` emits exactly one
``config.entities_changed`` per mutation, and the websocket hub forwards it, so a
route that also announced a change would make every tile update twice.

Validation policy: the ``entity_id`` shape rule below is an **API-write rule
only**. :class:`~chime_deck.store.ConfigStore` and
:class:`~chime_deck.store.EntityIn` deliberately accept historical ids (a
configuration import must not lose a tile because a domain was renamed), and D16's
resolver is total, so a stored row that is not in Home Assistant's shape still
renders — with the default icon when its domain has no table entry. The API is
where a *new* row is kept honest; the store is where old rows keep working.

Custom-icon policy: a save that names an unusable ``custom:`` file answers 422
naming the ``icon`` field, because that is the moment the user can fix it. A read
never fails on one: :func:`~chime_deck.icons.validate_icon_reference` is
re-run while rendering, a miss falls through D16's chain to a usable icon, and the
response says so in ``warning``. Re-validating a *stored* icon on an unrelated
PATCH is deliberately not done, or a file deleted once would block every future
edit of that row.

``icon_source`` is one of D16's five values — ``user``, ``ha-attribute``,
``ha-registry``, ``fallback`` and ``default`` — so the UI can tell a resolved
guess from a deliberate choice. ``accent_color`` is stored verbatim: its input
constraints belong to HSD-4umw, not this ticket.

Selection (D29, HSD-u6k5) is the one piece of state this module touches that the
store knows nothing about: it is runtime intent held in memory by
:class:`~chime_deck.selection.SelectionService`, and the store emits no event
for it. Deleting a tile is therefore the one route here that also drops the row's
selection key — ``discard``, so an unknown key is a no-op — and the JSON body
never carries the flag, because a configuration import must not resurrect a
selection.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeVar, cast

from fastapi import APIRouter
from pydantic import BaseModel

from ..entity_info import EntityContext, live_contexts
from ..icons import (
    DEFAULT_STATE_TONES,
    TONES,
    AttributeHint,
    IconSource,
    domain_of,
    is_tone,
    presentation_hints,
    resolve_icon,
    validate_icon_reference,
)
from ..store import Entity, EntityIdConflict, EntityIn, EntityNotFound
from .errors import ApiError, ErrorCode

if TYPE_CHECKING:
    # ``app.py`` imports ``.api`` at module level, so a runtime import of
    # ``..app`` here would close the cycle (the same pattern as ``api/buttons.py``).
    from ..app import AppState

__all__ = [
    "AttributeHintResponse",
    "DeleteEntityResponse",
    "EntityCreateRequest",
    "EntityResponse",
    "EntityUpdateRequest",
    "ReorderEntitiesRequest",
    "StateToneDefaultsResponse",
    "build_entities_router",
]

ENTITY_ID_PATTERN: Final = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
"""Home Assistant's entity-id shape: lower-case domain, dot, lower-case object id."""

_MISSING_IN_HA_WARNING: Final = (
    "Home Assistant does not know the entity {entity_id!r} — it was saved anyway. "
    "Check the spelling, or make sure the integration that provides it is loaded in Home Assistant."
)
_VANISHED_ICON_WARNING: Final = (
    "the stored icon {icon!r} is no longer a usable file in data/icons/, so a fallback icon "
    "is shown — choose another icon or upload the file again"
)


class AttributeHintResponse(BaseModel):
    """One ``show_attributes`` entry, resolved to something a tile can render."""

    name: str
    label: str
    value: str

    @classmethod
    def from_hint(cls, hint: AttributeHint) -> AttributeHintResponse:
        """Build the response body from one resolved hint.

        :param hint: hint produced by :func:`~chime_deck.icons.presentation_hints`.
        """
        return cls(name=hint.name, label=hint.label, value=hint.value)


class EntityResponse(BaseModel):
    """One status tile, in the shape the UI consumes.

    ``icon`` is the **resolved** D16 icon, while ``icon_override`` is the raw
    stored column. The editor must round-trip ``icon_override`` and never write
    ``icon`` back: a resolved fallback must not overwrite the user's choice.
    ``warning`` is always present (``null`` when there is nothing to say).

    ``tone`` is the backend's answer for the entity's **current** state — the
    colour its tile should wear right now — and is ``null`` only when there is no
    state at all to colour. ``state_tones`` is the tile's explicit override map,
    ``null`` meaning none, in which case the code default table applies;
    ``colour_by_state`` is the per-tile opt-out from state colouring.
    """

    id: int
    entity_id: str
    display_name: str | None
    icon: str
    icon_source: IconSource
    icon_override: str | None
    accent_color: str | None
    show_attributes: list[str]
    hide_when_unavailable: bool
    sort_order: int
    created_at: str
    state: str | None
    displayed_state: str | None
    unit_of_measurement: str | None
    attribute_hints: list[AttributeHintResponse]
    tone: str | None
    state_tones: dict[str, str] | None
    colour_by_state: bool
    warning: str | None = None


class EntityCreateRequest(BaseModel):
    """Body of ``POST /api/entities``."""

    entity_id: str
    display_name: str | None = None
    icon: str | None = None
    accent_color: str | None = None
    show_attributes: list[str] | None = None
    hide_when_unavailable: bool = False
    state_tones: dict[str, str] | None = None
    colour_by_state: bool = True


class EntityUpdateRequest(BaseModel):
    """Body of ``PATCH /api/entities/{entity_id}``; every field is optional.

    Omitting a field keeps the stored value. An explicit ``null`` clears a
    nullable field: for ``state_tones`` that **clears every override**, falling
    back to the built-in default table. ``colour_by_state`` is non-nullable, so
    an explicit ``null`` keeps the stored value (the same rule as
    ``hide_when_unavailable``). ``sort_order`` is never patchable: reordering is
    its own route, so a patch can never silently move a tile.
    """

    entity_id: str | None = None
    display_name: str | None = None
    icon: str | None = None
    accent_color: str | None = None
    show_attributes: list[str] | None = None
    hide_when_unavailable: bool | None = None
    state_tones: dict[str, str] | None = None
    colour_by_state: bool | None = None


class ReorderEntitiesRequest(BaseModel):
    """Body of ``POST /api/entities/reorder``."""

    ids: list[int]


class DeleteEntityResponse(BaseModel):
    """Outcome of one entity deletion."""

    deleted: bool
    id: int
    entity_id: str


class StateToneDefaultsResponse(BaseModel):
    """The tone vocabulary and the built-in default state→tone table.

    The tile editor pre-fills from this, so the default table has exactly one
    home — :data:`chime_deck.icons.tones.DEFAULT_STATE_TONES` in the code — and
    the browser keeps no copy of it. ``tones`` is in display order for the
    editor's segmented control; ``defaults`` is sorted, so the JSON is
    deterministic.
    """

    tones: list[str]
    defaults: dict[str, str]


def _not_found(entity_id: int) -> ApiError:
    """Build the 404 for an id that names no entity.

    :param entity_id: primary key that was requested.
    """
    return ApiError(
        404,
        ErrorCode.NOT_FOUND,
        f"no entity has the id {entity_id}",
        {"id": entity_id},
    )


def _conflict(exc: EntityIdConflict) -> ApiError:
    """Translate a duplicate ``entity_id`` from the store into a 409 naming the field.

    :param exc: conflict raised by ``upsert_entity``.
    """
    if exc.creating:
        message = (
            f"the entity {exc.entity_id!r} is already configured — "
            "edit that row instead of adding it again"
        )
    else:
        message = (
            f"the entity {exc.entity_id!r} is already used by another entity — "
            "pick a different entity_id or edit that row"
        )
    return ApiError(
        409,
        ErrorCode.CONFLICT,
        message,
        {"field": "entity_id", "entity_id": exc.entity_id},
    )


def _entity_id_problem(value: str) -> str | None:
    """Return why *value* is not an entity-id shape, or ``None`` when it is.

    :param value: submitted entity id.
    """
    if ENTITY_ID_PATTERN.fullmatch(value.strip()) is None:
        return (
            "entity_id must look like 'domain.object_id' — lower-case letters, "
            "digits and underscores, e.g. 'lock.front_door'"
        )
    return None


def _require_entity_id(value: str) -> str:
    """Return the stripped entity id, or reject a malformed one with a 422.

    :param value: submitted entity id.
    :raises ApiError: when the value is not Home Assistant's shape.
    """
    stripped = value.strip()
    problem = _entity_id_problem(stripped)
    if problem is not None:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, problem, {"field": "entity_id"})
    return stripped


def _require_icon(icon: str | None, icons_dir: Path) -> None:
    """Reject an unusable icon reference before the write.

    :param icon: submitted icon reference, or ``None`` which passes.
    :param icons_dir: configured custom icon directory.
    :raises ApiError: when the reference is neither an ``mdi:`` name nor a usable file.
    """
    if icon is None:
        return
    problem = validate_icon_reference(icon, icons_dir)
    if problem is not None:
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            f"{problem} — use an 'mdi:<name>' icon name or a 'custom:<file>' reference "
            "to a .svg or .png file in data/icons/",
            {"field": "icon"},
        )


def _normalise_display_name(value: str | None) -> str | None:
    """Return the trimmed display name, or ``None`` when it is blank.

    :param value: submitted display name, or ``None``.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _normalise_show_attributes(values: list[str] | None) -> tuple[str, ...] | None:
    """Trim the requested attribute names, drop blanks and duplicates, keep order.

    :param values: submitted attribute names, or ``None`` which passes through.
    :raises ApiError: when an entry is blank.
    """
    if values is None:
        return None
    normalised: list[str] = []
    for value in values:
        name = value.strip()
        if not name:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                "show_attributes must contain attribute names — remove the blank entry",
                {"field": "show_attributes"},
            )
        if name not in normalised:
            normalised.append(name)
    return tuple(normalised)


def _normalise_state_tones(values: dict[str, str] | None) -> dict[str, str] | None:
    """Trim the override keys, reject a blank key or a tone outside the vocabulary.

    The values are keys of the raw Home Assistant state, so they are only
    stripped, never case-folded: what a user typed is what is looked up. The
    result is sorted, so the stored JSON is deterministic and a round-trip does
    not reorder a row.

    :param values: submitted state→tone overrides, or ``None`` which passes
        through as ``None`` (no overrides at all).
    :raises ApiError: when a key is blank or a value is not one of the four
        tones.
    """
    if values is None:
        return None
    normalised: dict[str, str] = {}
    for raw_state, tone in values.items():
        state = raw_state.strip()
        if not state:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                "state_tones must map a state to a tone — remove the blank state key",
                {"field": "state_tones"},
            )
        if not is_tone(tone):
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                f"{state!r} maps to {tone!r} — choose one of {', '.join(TONES)}",
                {"field": "state_tones"},
            )
        normalised[state] = tone
    return dict(sorted(normalised.items()))


_T = TypeVar("_T")


def _pick(changes: Mapping[str, object], key: str, current: _T) -> _T:
    """Return a submitted non-null value, or the stored one when omitted or null.

    :param changes: body fields that were set, from ``model_dump(exclude_unset=True)``.
    :param key: body field name.
    :param current: value stored on the existing row.
    """
    value = changes.get(key, current)
    return current if value is None else cast(_T, value)


def _nullable(changes: Mapping[str, object], key: str, current: _T) -> _T:
    """Return the submitted value, or the stored one when the key was omitted.

    An explicit ``null`` is returned as ``None``, which is how a nullable field
    is cleared even though ``model_dump(exclude_unset=True)`` kept it in the body.

    :param changes: body fields that were set, from ``model_dump(exclude_unset=True)``.
    :param key: body field name.
    :param current: value stored on the existing row.
    """
    return cast(_T, changes.get(key, current))


def _merged_entity(existing: Entity, changes: Mapping[str, object]) -> EntityIn:
    """Merge a PATCH body over the stored row into one complete write input.

    ``entity_id``, ``hide_when_unavailable`` and ``colour_by_state`` are
    non-nullable, so an explicit ``null`` keeps the stored value, while
    ``state_tones`` is nullable, so an explicit ``null`` clears every override.
    ``sort_order`` is ``None`` so the store keeps the position, and ``created_at``
    is never part of the write input.

    :param existing: row read before the patch.
    :param changes: body fields that were set, already normalised.
    """
    return EntityIn(
        entity_id=_pick(changes, "entity_id", existing.entity_id),
        id=existing.id,
        display_name=_nullable(changes, "display_name", existing.display_name),
        icon=_nullable(changes, "icon", existing.icon),
        accent_color=_nullable(changes, "accent_color", existing.accent_color),
        show_attributes=_nullable(changes, "show_attributes", existing.show_attributes),
        hide_when_unavailable=_pick(
            changes, "hide_when_unavailable", existing.hide_when_unavailable
        ),
        state_tones=_nullable(changes, "state_tones", existing.state_tones),
        colour_by_state=_pick(changes, "colour_by_state", existing.colour_by_state),
        sort_order=None,
    )


async def _live_contexts(
    state: AppState, entity_ids: Sequence[str]
) -> Mapping[str, EntityContext] | None:
    """Fetch live entity data for *entity_ids*, or ``None`` when it is unavailable.

    Delegates to :func:`~chime_deck.entity_info.live_contexts`, which owns the
    bound and the degradation logging for every caller, so this route and the hub
    cannot drift apart.

    :param state: live application state providing the seam and its budget.
    :param entity_ids: ids to look up.
    """
    return await live_contexts(state.live_entities, entity_ids, timeout_s=state.live_lookup_timeout)


def _entity_response(
    entity: Entity, context: EntityContext | None, *, icons_dir: Path
) -> EntityResponse:
    """Build one response row from a stored entity and its live context.

    :param entity: row returned by the store.
    :param context: live entity data, or ``None`` when it is unavailable.
    :param icons_dir: configured custom icon directory.
    """
    choice = resolve_icon(entity.icon, entity.entity_id, context, icons_dir=icons_dir)
    state = None if context is None else context.state
    attributes: Mapping[str, object] = {} if context is None else context.attributes
    hints = presentation_hints(
        domain_of(entity.entity_id),
        state=state,
        attributes=attributes,
        show_attributes=entity.show_attributes or (),
        state_tones=entity.state_tones,
        colour_by_state=entity.colour_by_state,
    )

    warnings: list[str] = []
    if context is not None and not context.found:
        warnings.append(_MISSING_IN_HA_WARNING.format(entity_id=entity.entity_id))
    if entity.icon is not None and validate_icon_reference(entity.icon, icons_dir) is not None:
        warnings.append(_VANISHED_ICON_WARNING.format(icon=entity.icon))

    return EntityResponse(
        id=entity.id,
        entity_id=entity.entity_id,
        display_name=entity.display_name,
        icon=choice.icon,
        icon_source=choice.source,
        icon_override=entity.icon,
        accent_color=entity.accent_color,
        show_attributes=list(entity.show_attributes or ()),
        hide_when_unavailable=entity.hide_when_unavailable,
        sort_order=entity.sort_order,
        created_at=entity.created_at,
        state=state,
        displayed_state=hints.displayed_state,
        unit_of_measurement=hints.unit_of_measurement,
        attribute_hints=[AttributeHintResponse.from_hint(hint) for hint in hints.attribute_hints],
        tone=hints.tone,
        state_tones=(
            None if entity.state_tones is None else dict(sorted(entity.state_tones.items()))
        ),
        colour_by_state=entity.colour_by_state,
        warning=" ".join(warnings) or None,
    )


def build_entities_router(state: AppState) -> APIRouter:
    """Build the ``/api/entities`` router bound to *state*.

    Handlers are closures over *state*, following the house style in
    :mod:`chime_deck.app`: no handler annotates ``AppState``, so FastAPI never
    has to resolve a forward reference at import time.

    :param state: live application state providing the store and the live seam.
    """
    router = APIRouter(prefix="/api/entities", tags=["entities"])
    icons_dir = state.config.resolved_icons_dir

    @router.get("/state-tones")
    async def read_state_tones() -> StateToneDefaultsResponse:
        """Return the four tones and the built-in default state→tone table.

        The editor pre-fills from this, so the default table has exactly one home
        — the code — and the browser keeps no copy of it.
        """
        return StateToneDefaultsResponse(
            tones=list(TONES),
            defaults=dict(sorted(DEFAULT_STATE_TONES.items())),
        )

    @router.get("")
    async def list_entities() -> list[EntityResponse]:
        """Return every entity in ``sort_order`` order, with its resolved icon.

        The live seam is asked once for the whole page, not once per row.
        """
        entities = await state.store.list_entities()
        contexts = await _live_contexts(state, [entity.entity_id for entity in entities])
        return [
            _entity_response(
                entity,
                None if contexts is None else contexts.get(entity.entity_id),
                icons_dir=icons_dir,
            )
            for entity in entities
        ]

    @router.post("", status_code=201)
    async def create_entity(body: EntityCreateRequest) -> EntityResponse:
        """Create one entity and answer **201** with the stored row.

        The store emits the single ``config.entities_changed`` event; this route
        publishes nothing.

        :param body: fields of the new entity.
        :raises ApiError: if the entity id or icon is rejected, or it is a duplicate.
        """
        entity_id = _require_entity_id(body.entity_id)
        _require_icon(body.icon, icons_dir)
        entity = EntityIn(
            entity_id=entity_id,
            display_name=_normalise_display_name(body.display_name),
            icon=body.icon,
            accent_color=body.accent_color,
            show_attributes=_normalise_show_attributes(body.show_attributes),
            hide_when_unavailable=body.hide_when_unavailable,
            state_tones=_normalise_state_tones(body.state_tones),
            colour_by_state=body.colour_by_state,
        )
        try:
            saved = await state.store.upsert_entity(entity)
        except EntityIdConflict as exc:
            raise _conflict(exc) from None
        contexts = await _live_contexts(state, [saved.entity_id])
        return _entity_response(
            saved,
            None if contexts is None else contexts.get(saved.entity_id),
            icons_dir=icons_dir,
        )

    @router.post("/reorder")
    async def reorder_entities(body: ReorderEntitiesRequest) -> dict[str, list[int]]:
        """Renumber the entities in the submitted order.

        The store emits the single ``config.entities_changed`` event; this route
        publishes nothing.

        :param body: entity ids in their new order.
        :raises ApiError: if any id names no entity; nothing is written then.
        """
        try:
            await state.store.reorder_entities(body.ids)
        except EntityNotFound as exc:
            raise _not_found(int(exc.entity)) from None
        return {"ids": body.ids}

    @router.patch("/{entity_id}")
    async def update_entity(entity_id: int, body: EntityUpdateRequest) -> EntityResponse:
        """Apply a partial update to one entity and answer 200.

        The body is merged over the stored row and written; the store emits the
        single ``config.entities_changed`` event and this route publishes nothing.
        A stored icon that was not submitted is never re-validated, so a file
        deleted since the last save cannot block an unrelated edit.

        :param entity_id: primary key of the entity to change.
        :param body: fields to change; omitted fields keep their stored value.
        :raises ApiError: if the entity does not exist or a submitted value is rejected.
        """
        before = await state.store.get_entity_by_id(entity_id)
        if before is None:
            raise _not_found(entity_id)
        changes = body.model_dump(exclude_unset=True)
        if "entity_id" in changes and changes["entity_id"] is not None:
            changes["entity_id"] = _require_entity_id(cast(str, changes["entity_id"]))
            # A rename leaves the old entity_id's selection key in the set until the
            # next restart: the set is never pruned and ``is_selected`` is only asked
            # about existing rows, so the stale key stays invisible. Accepted here.
        if "display_name" in changes:
            changes["display_name"] = _normalise_display_name(
                cast(str | None, changes["display_name"])
            )
        if "show_attributes" in changes:
            changes["show_attributes"] = _normalise_show_attributes(
                cast(list[str] | None, changes["show_attributes"])
            )
        if "state_tones" in changes:
            changes["state_tones"] = _normalise_state_tones(
                cast(dict[str, str] | None, changes["state_tones"])
            )
        if "icon" in changes and changes["icon"] is not None:
            _require_icon(cast(str, changes["icon"]), icons_dir)

        try:
            saved = await state.store.upsert_entity(_merged_entity(before, changes))
        except EntityIdConflict as exc:
            raise _conflict(exc) from None
        contexts = await _live_contexts(state, [saved.entity_id])
        return _entity_response(
            saved,
            None if contexts is None else contexts.get(saved.entity_id),
            icons_dir=icons_dir,
        )

    @router.delete("/{entity_id}")
    async def delete_entity(entity_id: int) -> DeleteEntityResponse:
        """Delete one entity and name it in the response, answering 200.

        The row is read first so the response can name the entity, and the store
        emits the single ``config.entities_changed`` event; this route publishes
        nothing.

        :param entity_id: primary key of the entity to delete.
        :raises ApiError: if no entity has that id.
        """
        before = await state.store.get_entity_by_id(entity_id)
        if before is None:
            raise _not_found(entity_id)
        try:
            await state.store.delete_entity(entity_id)
        except EntityNotFound as exc:
            raise _not_found(int(exc.entity)) from None
        # The row is gone, so its volatile selection key would be a dangling entry:
        # the store emits no event for selection, making this the one place that has
        # to drop it. ``discard`` keeps an unknown key a no-op.
        state.selection.discard(before.entity_id)
        return DeleteEntityResponse(deleted=True, id=before.id, entity_id=before.entity_id)

    return router
