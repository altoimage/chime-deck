"""The concrete live-data adapter: the websocket cache and the entity registry.

This is the ONE place where the reconnecting websocket client's state cache and
Home Assistant's entity registry become :class:`~chime_deck.entity_info.LiveEntityInfo`
data. Nothing above it talks to the socket; the API and the hub only see
:class:`~chime_deck.entity_info.EntityContext` values.

Every budget here is strictly nested under the caller's bound
(:data:`~chime_deck.entity_info.LIVE_LOOKUP_TIMEOUT_S`): the registry fetch is
cheaper than one whole lookup, so a slow registry still leaves the lookup time to
finish, and the lookup is cheaper than the caller's bound, so the caller never has
to abandon this adapter mid-flight.

A failed or unconnected lookup returns ``None`` — never a fabricated
``found=False``. "Could not ask" and "Home Assistant does not know the entity" are
different answers and the UI prints them differently (a silent fallback versus a
visible warning), so this adapter must not conflate them.

The registry answer is cached behind a bounded TTL because
``config/entity_registry/list_for_display`` opens its own short-lived socket per
call: asking once per rendered page would open a connection per page.

Owning ticket: HSD-qr3k.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from ..entity_info import LIVE_LOOKUP_TIMEOUT_S, EntityContext
from ..store import ConfigStore
from ..store.events import ConfigEvent, ConfigEventName
from .errors import HaConfigurationError
from .websocket import HomeAssistantWebSocketClient

__all__ = [
    "ENTITY_REGISTRY_TTL_S",
    "LIVE_LOOKUP_BUDGET_S",
    "REGISTRY_BUDGET_S",
    "HaEntityInfo",
    "sync_tile_ids",
    "watch_tile_changes",
]

_LOGGER = logging.getLogger(__name__)

_LIST_FOR_DISPLAY: Final = "config/entity_registry/list_for_display"
"""The registry command behind D16's step 3; answered with ``entities`` entries."""

LIVE_LOOKUP_BUDGET_S: Final = 2.0
"""Seconds one whole live lookup may take.

Strictly below :data:`~chime_deck.entity_info.LIVE_LOOKUP_TIMEOUT_S` (3.0), so
this adapter finishes — and logs its own reason — before the caller's timeout can
abandon it.
"""

REGISTRY_BUDGET_S: Final = 1.5
"""Seconds the entity-registry fetch may take.

Strictly below :data:`LIVE_LOOKUP_BUDGET_S`, so a slow registry still leaves the
lookup time to read the state cache and build the contexts.
"""

ENTITY_REGISTRY_TTL_S: Final = 30.0
"""Seconds one registry answer is reused.

The command opens its own short-lived socket per call, so a per-page fetch would
open a connection per page; half a minute keeps icons fresh without that cost.
"""


def _require_positive(name: str, value: float) -> float:
    """Return *value*, or reject a non-positive budget at construction time.

    :param name: setting name, used in the error message.
    :param value: the submitted budget in seconds.
    :raises HaConfigurationError: if *value* is not positive.
    """
    if value <= 0:
        raise HaConfigurationError(f"{name} must be positive; got {value:g} s")
    return value


def _parse_registry(result: object) -> dict[str, str | None]:
    """Turn a ``list_for_display`` result into an ``entity_id -> icon`` map.

    The payload comes from Home Assistant, so every shape is defended: a
    malformed entry is skipped rather than allowed to fail a page.

    :param result: the command's result object, of any runtime shape.
    :returns: ``entity_id -> registry icon`` (or ``None`` when it has no ``ic``).
    """
    if not isinstance(result, Mapping):
        return {}
    entries = result.get("entities")
    if not isinstance(entries, list):
        return {}
    icons: dict[str, str | None] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        entity_id = entry.get("ei")
        if not isinstance(entity_id, str) or not entity_id:
            continue
        icon = entry.get("ic")
        icons[entity_id] = icon if isinstance(icon, str) and icon else None
    return icons


class HaEntityInfo:
    """LiveEntityInfo backed by the reconnecting websocket client and the entity registry."""

    def __init__(
        self,
        client: HomeAssistantWebSocketClient,
        *,
        registry_ttl_s: float = ENTITY_REGISTRY_TTL_S,
        lookup_budget_s: float = LIVE_LOOKUP_BUDGET_S,
        registry_budget_s: float = REGISTRY_BUDGET_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the adapter to *client* and validate the nested budgets.

        :param client: the websocket client whose cache and command side channel
            this adapter reads.
        :param registry_ttl_s: how long one registry answer is reused.
        :param lookup_budget_s: seconds one whole lookup may take.
        :param registry_budget_s: seconds the registry fetch may take.
        :param clock: monotonic clock, overridden by tests.
        :raises HaConfigurationError: if a budget is not positive, or a budget is
            not strictly below the one that bounds it.
        """
        self._client = client
        self._registry_ttl_s = _require_positive("registry_ttl_s", registry_ttl_s)
        self._lookup_budget_s = _require_positive("lookup_budget_s", lookup_budget_s)
        self._registry_budget_s = _require_positive("registry_budget_s", registry_budget_s)
        if not self._registry_budget_s < self._lookup_budget_s < LIVE_LOOKUP_TIMEOUT_S:
            raise HaConfigurationError(
                "the live-lookup budgets must nest: registry_budget_s "
                f"({self._registry_budget_s:g} s) < lookup_budget_s "
                f"({self._lookup_budget_s:g} s) < LIVE_LOOKUP_TIMEOUT_S "
                f"({LIVE_LOOKUP_TIMEOUT_S:g} s)"
            )
        self._clock = clock
        self._registry: dict[str, str | None] | None = None
        self._registry_at: float | None = None
        self._lock = asyncio.Lock()

    async def registry_icons(self) -> Mapping[str, str | None] | None:
        """Return the entity-registry icons, or ``None`` when they cannot be fetched.

        The answer is cached for :attr:`ENTITY_REGISTRY_TTL_S` so a page render
        does not open one short-lived socket per id. Public because the list route
        needs the same map without going through a state lookup.

        :returns: ``entity_id -> registry icon``, or ``None`` when the registry
            could not be asked and no earlier answer exists.
        """
        cached = self._cached_registry()
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._cached_registry()
            if cached is not None:
                return cached
            try:
                async with asyncio.timeout(self._registry_budget_s):
                    result = await self._client.command(_LIST_FOR_DISPLAY)
            except Exception as exc:
                if self._registry is not None:
                    _LOGGER.debug(
                        "the Home Assistant entity-registry lookup failed, reusing the "
                        "previous answer: %s",
                        exc,
                    )
                    return self._registry
                _LOGGER.debug(
                    "the Home Assistant entity-registry lookup failed; no registry icons "
                    "this time: %s",
                    exc,
                )
                return None
            self._registry = _parse_registry(result)
            self._registry_at = self._clock()
            return self._registry

    async def contexts_for(self, entity_ids: Sequence[str]) -> Mapping[str, EntityContext] | None:
        """Return live contexts for *entity_ids*, or ``None`` when they are unavailable.

        ``None`` means "could not ask" and must degrade silently; it is never
        turned into ``found=False``, which would claim Home Assistant answered.

        :param entity_ids: entity ids to look up; the result covers all of them,
            in caller order and without duplicates.
        :returns: one context per id, or ``None`` when the data is unavailable.
        """
        if not entity_ids:
            return {}
        try:
            async with asyncio.timeout(self._lookup_budget_s):
                return await self._resolve(entity_ids)
        except TimeoutError:
            _LOGGER.debug(
                "the live entity lookup timed out after %.2f s; the caller falls back to "
                "configuration-only icons",
                self._lookup_budget_s,
            )
            return None
        except Exception:
            _LOGGER.debug(
                "the live entity lookup failed; the caller falls back to configuration-only icons",
                exc_info=True,
            )
            return None

    def _cached_registry(self) -> dict[str, str | None] | None:
        """Return the registry map while it is inside the TTL, else ``None``."""
        registry = self._registry
        fetched_at = self._registry_at
        if registry is None or fetched_at is None:
            return None
        if self._clock() - fetched_at < self._registry_ttl_s:
            return registry
        return None

    async def _resolve(self, entity_ids: Sequence[str]) -> Mapping[str, EntityContext] | None:
        """Build one context per id from the cache and the registry.

        A disconnected client is never asked: its cache is a stale snapshot, and
        an empty cache must not read as "does not exist".

        :param entity_ids: entity ids to look up.
        :returns: the contexts, or ``None`` when the client is not connected or
            the registry could not be asked.
        """
        if not self._client.connected:
            _LOGGER.debug("the Home Assistant websocket is not connected; no live entity data")
            return None
        registry = await self.registry_icons()
        if registry is None:
            return None
        contexts: dict[str, EntityContext] = {}
        for entity_id in entity_ids:
            if entity_id in contexts:
                continue
            state = self._client.get_state(entity_id)
            contexts[entity_id] = EntityContext(
                entity_id=entity_id,
                found=state is not None or entity_id in registry,
                state=None if state is None else state.state or None,
                attributes={} if state is None else state.attributes,
                registry_icon=registry.get(entity_id),
                device_class=None,
                last_changed=None if state is None else state.last_changed or None,
            )
        return contexts


async def sync_tile_ids(
    store: ConfigStore, client: HomeAssistantWebSocketClient
) -> tuple[str, ...]:
    """Subscribe *client* to the configured status-tile entity ids and return them.

    :param store: the configuration store holding the tiles.
    :param client: the websocket client whose subscription is replaced.
    :returns: the entity ids now subscribed to, in store order.
    :raises StoreNotConnected: if the store was not opened.
    """
    entities = await store.list_entities()
    entity_ids = tuple(entity.entity_id for entity in entities)
    client.set_entity_ids(entity_ids)
    return entity_ids


def watch_tile_changes(
    store: ConfigStore, client: HomeAssistantWebSocketClient
) -> Callable[[], None]:
    """Keep the subscription in step with the configuration; return the unsubscribe callable.

    The handler is async because it re-reads the store; the event bus awaits it
    inside the write, exactly like the chime pipeline's settings watch, so no task
    is spawned.

    :param store: the configuration store holding the tiles and the event bus.
    :param client: the websocket client whose subscription is kept in step.
    :returns: a callable that removes the subscription.
    """

    async def on_config_event(event: ConfigEvent) -> None:
        """Re-sync the subscribed ids after a committed entity-list change.

        :param event: committed configuration change, from the store's bus.
        """
        if event.name is not ConfigEventName.ENTITIES_CHANGED:
            return
        try:
            entity_ids = await sync_tile_ids(store, client)
        except Exception as exc:
            _LOGGER.warning("could not re-read the entity list for Home Assistant: %s", exc)
            return
        _LOGGER.info(
            "home assistant: entity list changed, now watching %d entities", len(entity_ids)
        )

    return store.events.subscribe(on_config_event)
