"""The live-data seam behind D16's icon-resolution steps 2 and 3.

The resolver never talks to Home Assistant itself. It asks a
:class:`LiveEntityInfo` what the entity ids it is about to render look like
right now, and this module is the only place that dependency enters the
application. The production default wired into the lifespan is the concrete
adapter over the websocket client's state cache,
:class:`chime_deck.ha.HaEntityInfo`; :class:`NoLiveEntityInfo` remains the
only implementation available when Home Assistant is not configured, and with
that default nothing is known, so steps 2 and 3 never hit and the chain falls
through to the configuration-only fallbacks in steps 4 and 5.

The seam has a three-state contract:

* ``None`` means "could not ask" — no connection, a timeout, or any other
  failure. A caller must degrade silently; it must never turn ``None`` into a
  warning or an error.
* A mapping missing an id, or carrying ``EntityContext(found=False)``, means
  Home Assistant answered that the entity does not exist.
* A mapping carrying ``EntityContext(found=True)`` means the state, attributes
  and registry entries are live and may be used.

Two implementation contract sentences matter to whoever wires the real client
next: an implementation's own budget must be strictly below
:data:`LIVE_LOOKUP_TIMEOUT_S`, which bounds the caller (the caller abandons the
call with :func:`asyncio.timeout`), and abandoning a call mid-flight must be
safe for whatever owns the socket — own the connection, or guarantee a stray
``result`` frame cannot be mis-associated. Implementations must not raise to
signal "no data".

:func:`live_contexts` is the ONE place a caller's bound is applied, so the hub
and the API cannot drift apart into two different timeouts.

**The registry capability is separate and optional.** :class:`LiveEntityInfo`
answers questions about entity ids the caller already knows; it deliberately
cannot enumerate the house. The entity picker needs the other direction — it has
a search term and no ids — so it reads Home Assistant's entity registry for the
``ic`` icons that D16's step 3 uses, through :class:`LiveEntityRegistry`. That is
a second, additive Protocol rather than a third member of ``LiveEntityInfo``:
``chime_deck.ha.HaEntityInfo`` satisfies it, :class:`NoLiveEntityInfo`
deliberately does not (no Home Assistant connection means no registry icons,
which is exactly the ``None`` case), and adding ``registry_icons`` to
``LiveEntityInfo`` would break every other implementation for a capability only
one route needs. ``None`` from it means "could not ask" — never an error, never
a warning.

Owning ticket: HSD-jwd3; :class:`LiveEntityRegistry` was added by HSD-zyru.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "LIVE_LOOKUP_TIMEOUT_S",
    "NO_LIVE_ENTITY_INFO",
    "EntityContext",
    "LiveEntityInfo",
    "LiveEntityRegistry",
    "NoLiveEntityInfo",
    "live_contexts",
]

_LOGGER = logging.getLogger(__name__)

LIVE_LOOKUP_TIMEOUT_S: Final = 3.0
"""Seconds a route may spend asking for live entity data before it degrades to
configuration-only icons."""


@dataclass(frozen=True, slots=True)
class EntityContext:
    """What Home Assistant knows about one entity, when anything is known.

    ``found`` records whether Home Assistant answered that the entity exists;
    ``attributes`` are the live state attributes; ``registry_icon`` is the
    entity-registry ``ic``; and ``device_class`` is an authoritative override
    that wins over ``attributes["device_class"]``. ``last_changed`` is Home
    Assistant's own ISO-8601 timestamp for the last state change, or ``None``
    when it was not reported; it trails the other fields so no positional call
    site breaks.
    """

    entity_id: str
    found: bool
    state: str | None = None
    attributes: Mapping[str, object] = field(default_factory=dict)
    registry_icon: str | None = None
    device_class: str | None = None
    last_changed: str | None = None

    def __post_init__(self) -> None:
        """Replace *attributes* with a read-only copy."""
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


class LiveEntityInfo(Protocol):
    """Typed seam for live entity data (D16 steps 2 and 3).

    Returning ``None`` means "could not ask" (no connection, timeout, or any
    failure) — a caller must never turn that into a warning or an error. When
    data is available the mapping contains an entry for EVERY requested id, and
    ``EntityContext.found is False`` means Home Assistant answered that the
    entity does not exist.
    """

    async def contexts_for(self, entity_ids: Sequence[str]) -> Mapping[str, EntityContext] | None:
        """Return live contexts for *entity_ids*, or ``None`` when unavailable.

        An implementation's own budget must be strictly below the caller's
        bound (:data:`LIVE_LOOKUP_TIMEOUT_S`, after which the caller abandons
        the call with :func:`asyncio.timeout`), and abandoning a call mid-flight
        must be safe for whatever owns the socket: own the connection, or
        guarantee a stray ``result`` frame cannot be mis-associated. Implementations
        must not raise to signal "no data".

        :param entity_ids: entity ids to look up; the result covers all of them.
        """
        ...


@runtime_checkable
class LiveEntityRegistry(Protocol):
    """The OPTIONAL entity-registry capability of a live-data implementation.

    :class:`LiveEntityInfo` answers for ids a caller already has and cannot
    enumerate the house, so the entity picker asks a different question: what
    does Home Assistant's registry say about every entity's ``ic`` icon? This
    Protocol is the answer, kept separate and additive so ``LiveEntityInfo``
    stays the one seam the hub and the tile routes depend on, and so a build
    without a Home Assistant connection is simply a build without this
    capability rather than a stub that has to say "no icons" convincingly.

    ``chime_deck.ha.HaEntityInfo`` satisfies it. :class:`NoLiveEntityInfo`
    deliberately does not — that IS the "no registry icons" case, and a caller
    tests with ``isinstance(seam, LiveEntityRegistry)``.

    Returning ``None`` means "could not ask" (no connection, a budget too small,
    any failure), and it must degrade silently: the caller keeps its rows with a
    lesser icon from the attribute or the domain table. It never produces an
    error or a warning on a response.
    """

    async def registry_icons(self) -> Mapping[str, str | None] | None:
        """Return the registry ``ic`` icon of every entity, or ``None``.

        The mapping covers only entities that have a registry entry; a missing
        id means "the registry has no icon for it", not "does not exist". An
        implementation's own budget must stay strictly below the caller's bound
        (``asyncio.timeout(state.live_lookup_timeout)`` in the API), which the
        concrete adapter guarantees by construction.

        :returns: ``entity_id -> registry icon`` (``None`` where the entry has
            no ``ic``), or ``None`` when the registry could not be asked.
        """
        ...


@dataclass(frozen=True, slots=True)
class NoLiveEntityInfo:
    """Default: no Home Assistant connection is wired into this build, so nothing is known."""

    async def contexts_for(self, entity_ids: Sequence[str]) -> Mapping[str, EntityContext] | None:
        """Return ``None``: live entity data is unavailable."""
        return None


NO_LIVE_ENTITY_INFO: Final = NoLiveEntityInfo()


async def live_contexts(
    seam: LiveEntityInfo, entity_ids: Sequence[str], *, timeout_s: float
) -> Mapping[str, EntityContext] | None:
    """Fetch live entity data for *entity_ids*, or ``None`` when it is unavailable.

    ``None`` is the seam's "could not ask" answer and must degrade silently: a
    timeout or a failure logs (at most) and falls back to configuration-only
    icons, never to a warning on the response or a failed request. This is the
    ONE place the caller's bound is applied, so the hub and the API cannot drift.

    :param seam: the live-data implementation to ask.
    :param entity_ids: ids to look up; an empty sequence answers ``{}`` without
        touching *seam*.
    :param timeout_s: seconds the call may take before it degrades.
    :returns: the seam's mapping, or ``None`` when it could not be asked.
    """
    if not entity_ids:
        return {}
    try:
        async with asyncio.timeout(timeout_s):
            return await seam.contexts_for(entity_ids)
    except TimeoutError:
        _LOGGER.debug(
            "live entity lookup timed out after %.2f s; using configuration-only icons", timeout_s
        )
        return None
    except Exception:
        _LOGGER.warning("live entity lookup failed; using configuration-only icons", exc_info=True)
        return None
