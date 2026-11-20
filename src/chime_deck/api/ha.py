"""The Home Assistant helper endpoints the settings page and the pickers need.

Three endpoints live here: ``GET /api/ha/tts_engines`` lists the ``tts.*``
entities Home Assistant reports, so the settings page can offer
``tts.ha.engine_id`` as a dropdown instead of asking the user to type an entity id
they have to look up themselves; ``GET /api/ha/entities`` searches every entity
Home Assistant knows so the tile editor can offer a picker instead of the same
kind of typing; and ``GET /api/ha/entities/{entity_id}`` describes **one** entity
live, which is what the tile editor previews before a row is saved.

**The single-entity erratum.** Design 05 §3's row for the entity route is
``{entity_id, name, state, icon, device_class}``. The response here is an
additive superset: it also carries ``domain``, ``icon_source``, ``attributes``,
``last_changed``, ``found`` and ``warning``. ``found`` and ``warning`` are
load-bearing — the editor must be able to tell "Home Assistant is unreachable"
(``found: null``) from "Home Assistant does not know it" (``found: false``),
because the two need different fixes. The live ``attributes`` and
``last_changed`` are what a preview renders, and ``icon_source`` is the same D16
vocabulary the tiles use, so the preview and the tile cannot disagree.

**The envelope erratum.** Design 05 §3 lists the TTS route as a bare array
(``-> [{engine_id, name, language}]``). It answers with an object instead:
``{"engines": [...], "warning": "..."}``. A list cannot carry the sentence that
explains why it is empty — "Home Assistant is not reachable" and "Home Assistant
reports no TTS integration" need different fixes — and the settings page must
show one without inventing it. The envelope is the only shape that keeps the
route honest.

**Bounded degradation.** Every failure is a ``200`` with an empty list and a
whole-sentence ``warning``: a Home Assistant install that is asleep, not
configured, or has no TTS integration is an ordinary state of a working doorbell,
not a server error, and the settings page must still render. The routes never
answer ``500``; the settings page is where a broken connection is reported, and
the response is scrubbed before it leaves. The entity route never answers ``404``
either: an id Home Assistant does not know is a ``200`` with ``found: false``, so
the editor can show the sentence instead of a bare error.

**The search erratum.** Design 05 §3 lists ``GET /api/ha/entities?q=`` as a bare
array. It answers with an envelope instead:
``{"entities": [...], "warning": null | "…"}``. The reason is the same one that
made the TTS route an object: a bare array cannot carry the sentence that
separates "nothing matches your search" from "Home Assistant could not be asked",
and those two empty pickers need different fixes. The envelope's invariant is
``warning is not None`` **if and only if** ``entities`` is empty *because Home
Assistant could not answer*, so ``{"entities": [], "warning": null}`` is the
honest "no matches" answer. The row is design 05 §3's row
(``entity_id, name, state, icon, domain, device_class``) plus ``icon_source``, the
same D16 vocabulary the tiles report, so the picker previews exactly the icon the
tile will render.

Owning ticket: HSD-npt0; the single-entity route was added by HSD-qr3k and the
search route by HSD-zyru.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Final

import httpx
from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..entity_info import EntityContext, LiveEntityRegistry, live_contexts
from ..ha import (
    LIVE_LOOKUP_BUDGET_S,
    HaConfigurationError,
    HaError,
    HaState,
    HomeAssistantClient,
)
from ..icons import IconSource, domain_of, resolve_icon
from ..tts import clean_detail
from .entities import ENTITY_ID_PATTERN
from .errors import ApiError, ErrorCode
from .limits import SEARCH_LIMIT_DEFAULT, SEARCH_LIMIT_MAX

if TYPE_CHECKING:
    # ``app.py`` imports ``.api`` at module level, so a runtime import of
    # ``..app`` here would close the cycle (the same pattern as ``api/sounds.py``).
    from ..app import AppState

__all__ = [
    "TTS_ENGINES_INNER_TIMEOUT_S",
    "TTS_ENGINES_TIMEOUT_S",
    "HaEntityResponse",
    "HaEntityRow",
    "HaEntitySearchResponse",
    "HaTtsEngineResponse",
    "HaTtsEnginesResponse",
    "build_ha_router",
]

_LOGGER = logging.getLogger(__name__)

TTS_ENGINES_TIMEOUT_S: Final = 5.0
"""Seconds the whole route may take before it reports a timeout.

The same number as :data:`~chime_deck.api.probes.PROBE_TIMEOUT_S`, so the
settings page's connection test and its engine list give up together.
"""

TTS_ENGINES_INNER_TIMEOUT_S: Final = 4.0
"""Seconds the ``GET /api/states`` call itself may take, retry included.

Deliberately below :data:`TTS_ENGINES_TIMEOUT_S` so the client's own typed
timeout message wins over the outer :func:`asyncio.timeout` one.
"""

_NO_ANSWER_WARNING: Final = (
    "Home Assistant did not answer the live lookup, so this entity's current state is "
    "unknown — check ha.base_url and ha.token in Settings, then try again"
)
_UNKNOWN_ENTITY_WARNING: Final = (
    "Home Assistant does not know the entity {entity_id!r} — check the spelling, or make "
    "sure the integration that provides it is loaded in Home Assistant."
)
_NOT_CONFIGURED_WARNING: Final = (
    "Home Assistant is not configured, so the entity list cannot be read — set "
    "ha.base_url and ha.token in Settings, then try again"
)
_NO_TILE_POSITION: Final = 1 << 30
"""Sort position given to an entity that is not a configured tile.

Bigger than any dashboard position, so the ``rank`` in front of it always decides
first and this only orders the other two ranks' members.
"""


class HaEntityResponse(BaseModel):
    """One Home Assistant entity described live, for the tile editor's preview.

    This describes Home Assistant, not a stored tile: there is no user icon
    override to apply, which is why ``icon`` comes from the live attributes, the
    registry or the domain table only. ``found`` is three-state on purpose —
    ``None`` means the lookup could not answer, ``False`` means Home Assistant
    answered that the entity does not exist, and ``True`` means the values below
    are live. ``warning`` is always present (``null`` when there is nothing to
    say).
    """

    entity_id: str
    name: str | None
    state: str | None
    icon: str
    icon_source: IconSource
    domain: str
    device_class: str | None
    attributes: dict[str, object]
    last_changed: str | None
    found: bool | None
    warning: str | None = None


class HaEntityRow(BaseModel):
    """One row of the entity picker: design 05 §3's six keys plus ``icon_source``.

    ``name`` is always present — Home Assistant's ``friendly_name`` falls back to
    the entity id, because a picker row the user cannot identify is worse than a
    technical one. ``state`` is ``None`` when Home Assistant reports none, which
    is a real state (an entity that has never run), not a missing row.
    ``device_class`` prefers the authoritative value and falls back to the
    attribute. ``icon`` and ``icon_source`` come from the same D16 resolution the
    tile uses (attributes, then registry, then the domain table), so the preview
    cannot promise an icon the tile will not show.
    """

    entity_id: str
    name: str
    state: str | None
    icon: str
    icon_source: IconSource
    domain: str
    device_class: str | None


class HaEntitySearchResponse(BaseModel):
    """The search hits plus the one sentence explaining why there are none.

    ``warning is not None`` means Home Assistant could not be asked, and then
    ``entities`` is empty: the picker must render the sentence, not "no matches".
    ``{"entities": [], "warning": null}`` is the honest no-match answer.
    """

    entities: list[HaEntityRow]
    warning: str | None = None


class HaTtsEngineResponse(BaseModel):
    """One TTS engine Home Assistant reports, in the shape the dropdown uses."""

    engine_id: str
    name: str
    language: str | None = None


class HaTtsEnginesResponse(BaseModel):
    """The engine list plus the one sentence explaining why it may be empty."""

    engines: list[HaTtsEngineResponse]
    warning: str | None = None


def _new_client() -> httpx.AsyncClient:
    """Build the client this route owns when the application injects none."""
    return httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(TTS_ENGINES_TIMEOUT_S))


def _engine_from_state(entity: HaState) -> HaTtsEngineResponse:
    """Reduce one ``tts.*`` state object to a dropdown entry.

    Home Assistant puts the human name in ``friendly_name`` and the language in
    ``language``; both are unconstrained JSON attributes, so a non-string is
    coerced or dropped rather than trusted. The entity id is the fallback name,
    because an entry the user cannot identify is worse than a technical one.

    :param entity: a state object whose ``entity_id`` starts with ``tts.``.
    """
    raw_name = entity.attributes.get("friendly_name")
    if isinstance(raw_name, str):
        text = raw_name
    elif raw_name is None:
        text = ""
    else:
        text = str(raw_name)
    raw_language = entity.attributes.get("language")
    language = None
    if isinstance(raw_language, str):
        language = clean_detail(raw_language.strip()) or None
    return HaTtsEngineResponse(
        engine_id=clean_detail(entity.entity_id),
        name=clean_detail(text.strip()) or clean_detail(entity.entity_id),
        language=language,
    )


def _warning(exc: BaseException) -> str:
    """Turn a failure into the sentence the settings page shows.

    :param exc: the exception the route caught.
    """
    return clean_detail(str(exc)) or type(exc).__name__


def _non_blank(value: object) -> str | None:
    """Return *value* stripped when it is a non-blank string, else ``None``.

    Home Assistant's attributes are unconstrained JSON, so every read defends.

    :param value: an attribute value, of any runtime shape.
    """
    if isinstance(value, str):
        return value.strip() or None
    return None


def _display_name(entity_id: str, attributes: Mapping[str, object]) -> str:
    """Return the human name Home Assistant reports, or the entity id itself.

    Home Assistant's ``friendly_name`` is unconstrained JSON, so it is defended
    like every other attribute: a blank or non-string value falls back to the
    entity id rather than rendering an empty picker row.

    :param entity_id: entity id being named.
    :param attributes: that entity's state attributes.
    """
    name = _non_blank(attributes.get("friendly_name"))
    return entity_id if name is None else name


def _entity_name(entity_id: str, context: EntityContext | None) -> str:
    """Return the human name Home Assistant reports, or the entity id itself.

    :param entity_id: entity id being described.
    :param context: live context, or ``None`` when the lookup could not answer.
    """
    if context is not None:
        return _display_name(entity_id, context.attributes)
    return entity_id


def _matches(entity: HaState, q_folded: str) -> bool:
    """Whether *entity* is a hit for the already-casefolded search term.

    Both the entity id and the friendly name are searched, casefolded, so
    ``?q=FRONT`` finds ``lock.front_door`` ("Front Door"). An empty term matches
    everything: that is the picker's initial view, not a search.

    :param entity: one state object from Home Assistant's corpus.
    :param q_folded: ``q.strip().casefold()``, computed once per request.
    """
    if not q_folded:
        return True
    if q_folded in entity.entity_id.casefold():
        return True
    return q_folded in _display_name(entity.entity_id, entity.attributes).casefold()


def _rank_key(
    entity: HaState, q_folded: str, tile_positions: Mapping[str, int]
) -> tuple[int, int, str, str]:
    """Return the picker's ordering key for *entity*.

    Rank ``0`` is an entity id a type-ahead typed exactly, rank ``1`` is a tile the
    user already configured, and rank ``2`` is everything else. Within a rank,
    configured tiles come in dashboard order first, then by display name, then by
    entity id, so the order is total and two requests with the same data answer
    the same list.

    :param entity: one state object from Home Assistant's corpus.
    :param q_folded: ``q.strip().casefold()``, computed once per request.
    :param tile_positions: configured tile ``entity_id -> sort_order`` position.
    """
    entity_id = entity.entity_id
    if entity_id.casefold() == q_folded:
        rank = 0
    elif entity_id in tile_positions:
        rank = 1
    else:
        rank = 2
    return (
        rank,
        tile_positions.get(entity_id, _NO_TILE_POSITION),
        _display_name(entity_id, entity.attributes).casefold(),
        entity_id,
    )


def _entity_device_class(context: EntityContext | None) -> str | None:
    """Return the authoritative device class, falling back to the attribute.

    :param context: live context, or ``None`` when the lookup could not answer.
    """
    if context is None:
        return None
    if context.device_class is not None and context.device_class.strip():
        return context.device_class
    return _non_blank(context.attributes.get("device_class"))


def build_ha_router(state: AppState) -> APIRouter:
    """Build the ``/api/ha`` router bound to *state*.

    Handlers are closures over *state*, following the house style in
    :mod:`chime_deck.app`: no handler annotates ``AppState``, so FastAPI
    never has to resolve a forward reference at import time.

    :param state: application state carrying the store and the optional borrowed
        Home Assistant HTTP client.
    """
    router = APIRouter(prefix="/api/ha", tags=["ha"])

    @asynccontextmanager
    async def _session() -> AsyncIterator[HomeAssistantClient]:
        """Yield a client built from the stored settings, closing what it owns.

        The injected ``ha_client`` is borrowed: this route closes the
        ``HomeAssistantClient`` wrapper but never the socket it did not create.
        """
        raw = state.ha_client
        owned: httpx.AsyncClient | None = None
        if raw is None:
            owned = _new_client()
            raw = owned
        ha = await HomeAssistantClient.from_store(state.store, client=raw)
        try:
            yield ha
        finally:
            with suppress(Exception):
                await ha.aclose()
            if owned is not None:
                with suppress(Exception):
                    await owned.aclose()

    async def _entity_corpus() -> list[HaState]:
        """Return every entity state Home Assistant reports.

        ``GET /api/states`` is the only way to enumerate the house, and it is one
        keep-alive request through the client this module already borrows for
        ``/api/tts_engines`` — not a websocket subscription, which would change
        what the connection is subscribed to for a search nobody saved.

        The budgets nest so they cannot drift: the adapter's registry fetch (1.5 s)
        is below :data:`~chime_deck.ha.LIVE_LOOKUP_BUDGET_S` (2.0 s) used here,
        which is below ``state.live_lookup_timeout`` (3.0 s) applied by the route's
        own :func:`asyncio.timeout`, which is below the lifespan's 5 s. The client
        therefore reports its own timeout first, and the route's sentence is the
        backstop rather than the ordinary answer.
        """
        async with _session() as ha:
            return await ha.states(timeout_s=LIVE_LOOKUP_BUDGET_S)

    async def _registry_icons() -> Mapping[str, str | None] | None:
        """Return the registry icons when this build has the capability, else ``None``.

        ``isinstance`` against the runtime-checkable
        :class:`~chime_deck.entity_info.LiveEntityRegistry` is the whole feature
        test: with no Home Assistant connection the seam is
        ``NoLiveEntityInfo``, which does not satisfy it, and that is the ordinary
        "no registry icons" case rather than a failure. Any exception is answered
        with ``None`` too — a broken registry costs the rows their registry icon,
        never the row and never a warning.
        """
        seam = state.live_entities
        if not isinstance(seam, LiveEntityRegistry):
            return None
        try:
            return await seam.registry_icons()
        except Exception:
            _LOGGER.debug(
                "the Home Assistant entity-registry lookup failed; the entity list uses "
                "the attribute and domain icons instead",
                exc_info=True,
            )
            return None

    async def _tile_positions() -> Mapping[str, int]:
        """Return each configured tile's dashboard position, or ``{}`` when unreadable.

        The picker opens on the user's own tiles, so the search needs their order.
        The read is a convenience, not a contract: a store that cannot answer
        degrades to a plain alphabetical list with a debug line and no warning,
        because a page of entities is still a correct answer to the question.
        """
        try:
            tiles = await state.store.list_entities()
        except Exception:
            _LOGGER.debug(
                "the configured tiles could not be read; the entity list is alphabetical",
                exc_info=True,
            )
            return {}
        return {tile.entity_id: position for position, tile in enumerate(tiles)}

    def _timeout_warning() -> str:
        """Return the sentence for a lookup that ran out of the route's budget."""
        return (
            f"listing the Home Assistant entities took longer than "
            f"{state.live_lookup_timeout:g} s — check that ha.base_url is reachable"
        )

    def _corpus_warning(exc: BaseException) -> str:
        """Return the one sentence explaining why the corpus could not be read.

        The four cases need four different fixes, so they get four different
        answers: a missing setting, an install that never answered in time, an
        unreachable or rejecting Home Assistant (whose own sentence is already
        scrubbed), and a genuine surprise, which is also logged.

        :param exc: the exception the ``asyncio.gather`` slot carried.
        """
        if isinstance(exc, HaConfigurationError):
            return _NOT_CONFIGURED_WARNING
        if isinstance(exc, TimeoutError):
            return _timeout_warning()
        if isinstance(exc, (HaError, httpx.HTTPError)):
            return _warning(exc)
        _LOGGER.warning("listing the Home Assistant entities failed", exc_info=exc)
        return _warning(exc)

    @router.get("/entities")
    async def search_entities(
        q: str = "",
        limit: int = Query(SEARCH_LIMIT_DEFAULT, ge=1, le=SEARCH_LIMIT_MAX),
    ) -> HaEntitySearchResponse:
        """Search every Home Assistant entity, degrading instead of failing.

        ``q`` is matched case-insensitively against the entity id and the display
        name; an empty ``q`` matches everything. The ordering is the picker's
        contract: an exact entity-id hit first, then the user's own tiles in
        dashboard order, then everything else by display name and id — so an empty
        ``q`` opens on the tiles the user already chose and then an alphabetical
        list, and typing a full id puts that row on top.

        ``limit`` (default :data:`~chime_deck.api.limits.SEARCH_LIMIT_DEFAULT`,
        maximum :data:`~chime_deck.api.limits.SEARCH_LIMIT_MAX`) is applied
        *after* filtering and ordering, so at most ``limit`` rows are ever built:
        a big installation never pays for contexts nobody asked to see.

        Never fails: Home Assistant being unconfigured, unreachable, slow or
        broken all answer ``200`` with an empty list and one whole sentence. The
        envelope's invariant is that ``warning`` is set if and only if the list is
        empty because Home Assistant could not be asked.
        """
        try:
            async with asyncio.timeout(state.live_lookup_timeout):
                states_answer, registry_answer = await asyncio.gather(
                    _entity_corpus(), _registry_icons(), return_exceptions=True
                )
        except TimeoutError:
            return HaEntitySearchResponse(entities=[], warning=_timeout_warning())
        if isinstance(states_answer, BaseException):
            return HaEntitySearchResponse(entities=[], warning=_corpus_warning(states_answer))
        # A registry that could not answer costs the rows a lesser icon; it never
        # costs them the row, and it never produces a warning.
        registry = registry_answer if isinstance(registry_answer, Mapping) else None
        q_folded = q.strip().casefold()
        matches = [entity for entity in states_answer if _matches(entity, q_folded)]
        tile_positions = await _tile_positions()
        matches.sort(key=lambda entity: _rank_key(entity, q_folded, tile_positions))
        rows: list[HaEntityRow] = []
        for entity in matches[:limit]:
            context = EntityContext(
                entity_id=entity.entity_id,
                found=True,
                state=entity.state or None,
                attributes=entity.attributes,
                registry_icon=None if registry is None else registry.get(entity.entity_id),
                device_class=None,
                last_changed=entity.last_changed,
            )
            choice = resolve_icon(
                None, entity.entity_id, context, icons_dir=state.config.resolved_icons_dir
            )
            rows.append(
                HaEntityRow(
                    entity_id=entity.entity_id,
                    name=_entity_name(entity.entity_id, context),
                    state=context.state,
                    icon=choice.icon,
                    icon_source=choice.source,
                    domain=domain_of(entity.entity_id),
                    device_class=_entity_device_class(context),
                )
            )
        return HaEntitySearchResponse(entities=rows, warning=None)

    @router.get("/tts_engines")
    async def list_tts_engines() -> HaTtsEnginesResponse:
        """List the TTS engines Home Assistant reports, sorted by entity id.

        Never fails: a timeout, an unreachable Home Assistant, a bad token or an
        install with no TTS integration all answer ``200`` with an empty list and
        a sentence the settings page can show unchanged.
        """
        try:
            async with asyncio.timeout(TTS_ENGINES_TIMEOUT_S):
                async with _session() as ha:
                    entities = await ha.states(timeout_s=TTS_ENGINES_INNER_TIMEOUT_S)
        except TimeoutError:
            return HaTtsEnginesResponse(
                engines=[],
                warning=(
                    f"listing the Home Assistant TTS engines took longer than "
                    f"{TTS_ENGINES_TIMEOUT_S:g} s — check that ha.base_url is reachable"
                ),
            )
        except (HaError, httpx.HTTPError) as exc:
            return HaTtsEnginesResponse(engines=[], warning=_warning(exc))
        except Exception as exc:
            _LOGGER.warning("listing the Home Assistant TTS engines failed", exc_info=True)
            return HaTtsEnginesResponse(engines=[], warning=_warning(exc))
        engines = [
            _engine_from_state(entity) for entity in entities if entity.entity_id.startswith("tts.")
        ]
        if not engines:
            return HaTtsEnginesResponse(
                engines=[],
                warning=(
                    "Home Assistant is reachable but reports no text-to-speech engines — add a "
                    "TTS integration in Home Assistant (for example the Piper add-on), then set "
                    "tts.ha.engine_id"
                ),
            )
        return HaTtsEnginesResponse(engines=sorted(engines, key=lambda entry: entry.engine_id))

    @router.get("/entities/{entity_id}")
    async def describe_entity(entity_id: str) -> HaEntityResponse:
        """Describe one Home Assistant entity live, degrading instead of failing.

        A malformed id is the caller's mistake and is a ``422``; everything else
        — an unreachable Home Assistant or an id Home Assistant does not know —
        is a ``200`` with ``found`` set and one sentence the editor can show.
        """
        stripped = entity_id.strip()
        if ENTITY_ID_PATTERN.fullmatch(stripped) is None:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                "entity_id must look like 'domain.object_id' — lower-case letters, "
                "digits and underscores, e.g. 'lock.front_door'",
                {"field": "entity_id"},
            )
        contexts = await live_contexts(
            state.live_entities, [stripped], timeout_s=state.live_lookup_timeout
        )
        context = None if contexts is None else contexts.get(stripped)
        choice = resolve_icon(None, stripped, context, icons_dir=state.config.resolved_icons_dir)
        found: bool | None
        warning: str | None
        if context is None:
            found = None
            warning = _NO_ANSWER_WARNING
        elif not context.found:
            found = False
            warning = _UNKNOWN_ENTITY_WARNING.format(entity_id=stripped)
        else:
            found = True
            warning = None
        return HaEntityResponse(
            entity_id=stripped,
            name=_entity_name(stripped, context),
            state=None if context is None else context.state,
            icon=choice.icon,
            icon_source=choice.source,
            domain=domain_of(stripped),
            device_class=_entity_device_class(context),
            attributes={} if context is None else dict(context.attributes),
            last_changed=None if context is None else context.last_changed,
            found=found,
            warning=warning,
        )

    return router
