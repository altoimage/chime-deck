"""Fully offline tests for the concrete live-data adapter.

Every test drives :class:`~chime_deck.ha.HaEntityInfo` against a scripted fake
client, so no socket is opened and no test needs a live Home Assistant. The
adapter is the seam HSD-zyru builds on: these tests pin its budgets, its registry
cache, its "could not ask" answer and the lifespan helpers beside it.

Owning ticket: HSD-qr3k.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
from log_isolation import preserve_log_state

from chime_deck.entity_info import LIVE_LOOKUP_TIMEOUT_S, EntityContext
from chime_deck.ha import (
    ENTITY_REGISTRY_TTL_S,
    LIVE_LOOKUP_BUDGET_S,
    REGISTRY_BUDGET_S,
    HaCommandError,
    HaConfigurationError,
    HaEntityInfo,
    HaEntityState,
    HomeAssistantWebSocketClient,
    sync_tile_ids,
    watch_tile_changes,
)
from chime_deck.store import ConfigStore, Entity, EntityIn, StoreNotConnected
from chime_deck.store.events import EventBus, button_event, entity_event

pytestmark = pytest.mark.asyncio

_LIST_FOR_DISPLAY = "config/entity_registry/list_for_display"
"""The registry command the adapter must send, and the only one it may send."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test."""
    with preserve_log_state():
        yield


class FakeClient:
    """A scripted stand-in for the websocket client: cache, command result and records."""

    def __init__(
        self,
        *,
        connected: bool = True,
        states: Mapping[str, HaEntityState] | None = None,
        results: Sequence[object] = ({"entities": []},),
        error: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        """Create a client that reports *connected* and answers commands from *results*.

        :param connected: value of :attr:`connected`.
        :param states: the cache, keyed by entity id.
        :param results: one command result per call; the last one repeats.
        :param error: raised from every command instead of answering; set the
            attribute to change it after construction.
        :param delay_s: seconds a command waits before answering, to force a timeout.
        """
        self.connected = connected
        self.states: dict[str, HaEntityState] = dict(states) if states is not None else {}
        self.entity_ids: tuple[str, ...] = ()
        self.commands: list[str] = []
        self.cancelled = False
        self.error = error
        self._results = list(results)
        self._delay_s = delay_s
        self._calls = 0

    def get_state(self, entity_id: str) -> HaEntityState | None:
        """Return the cached state of *entity_id*, or ``None``.

        :param entity_id: entity id to look up.
        """
        return self.states.get(entity_id)

    def set_entity_ids(self, values: Iterable[str]) -> None:
        """Record the subscribed entity ids.

        :param values: the new subscription list.
        """
        self.entity_ids = tuple(values)

    async def command(self, command_type: str, /, **fields: object) -> Mapping[str, object]:
        """Record *command_type* and answer with the next scripted result.

        :param command_type: Home Assistant command type.
        :param fields: extra command fields, unused by the fake.
        :raises Exception: the scripted *error*, when one was given.
        """
        self.commands.append(command_type)
        result = self._results[min(self._calls, len(self._results) - 1)] if self._results else {}
        self._calls += 1
        if self._delay_s:
            try:
                await asyncio.sleep(self._delay_s)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.error is not None:
            raise self.error
        return cast(Mapping[str, object], result)


class _FailingStore:
    """A store whose entity read always fails, for the re-sync warning path."""

    def __init__(self) -> None:
        """Create a store with its own event bus."""
        self.events = EventBus()

    async def list_entities(self) -> list[Entity]:
        """Fail every read, as an unopened or closed store would.

        :raises StoreNotConnected: always.
        """
        raise StoreNotConnected("the store is not open")


def _adapter(
    client: FakeClient,
    *,
    clock: Callable[[], float] = time.monotonic,
    registry_budget_s: float = REGISTRY_BUDGET_S,
    lookup_budget_s: float = LIVE_LOOKUP_BUDGET_S,
) -> HaEntityInfo:
    """Build the adapter over *client* with the given budgets and clock.

    :param client: the scripted fake client.
    :param clock: monotonic clock used for the registry TTL.
    :param registry_budget_s: seconds the registry fetch may take.
    :param lookup_budget_s: seconds one whole lookup may take.
    """
    return HaEntityInfo(
        cast(HomeAssistantWebSocketClient, client),
        clock=clock,
        registry_budget_s=registry_budget_s,
        lookup_budget_s=lookup_budget_s,
    )


def _state(
    entity_id: str,
    state: str | None,
    *,
    attributes: Mapping[str, object] | None = None,
    last_changed: str | None = None,
) -> HaEntityState:
    """Build one cached entity state.

    :param entity_id: the entity id.
    :param state: the state string, or ``None``.
    :param attributes: live attributes.
    :param last_changed: ISO-8601 timestamp of the last change.
    """
    return HaEntityState(
        entity_id=entity_id,
        state=state,
        attributes=dict(attributes) if attributes is not None else {},
        last_changed=last_changed,
    )


def _registry(*entries: tuple[str, str]) -> dict[str, object]:
    """Build a ``list_for_display``-shaped result from ``(entity_id, icon)`` pairs.

    :param entries: pairs of entity id and registry icon.
    """
    return {"entities": [{"ei": entity_id, "ic": icon} for entity_id, icon in entries]}


async def test_live_state_attributes_and_last_changed() -> None:
    """A cached entity reports its live state, attributes and last change."""
    changed = "2024-05-01T10:00:00+00:00"
    client = FakeClient(
        states={
            "sensor.porch": _state(
                "sensor.porch",
                "22.5",
                attributes={"unit_of_measurement": "°C"},
                last_changed=changed,
            )
        },
        results=[_registry(("sensor.porch", "mdi:thermometer"))],
    )

    contexts = await _adapter(client).contexts_for(["sensor.porch"])

    assert contexts is not None
    context = contexts["sensor.porch"]
    assert context.found is True
    assert context.state == "22.5"
    assert dict(context.attributes) == {"unit_of_measurement": "°C"}
    assert context.last_changed == changed
    assert context.registry_icon == "mdi:thermometer"


async def test_unknown_entity_is_found_false() -> None:
    """An id neither cached nor in the registry answers ``found=False`` with no data."""
    client = FakeClient(results=[_registry(("sensor.other", "mdi:eye"))])

    contexts = await _adapter(client).contexts_for(["sensor.ghost"])

    assert contexts is not None
    context = contexts["sensor.ghost"]
    assert context.found is False
    assert context.state is None
    assert dict(context.attributes) == {}
    assert context.last_changed is None


async def test_registry_only_entity_is_found() -> None:
    """An id the registry knows but the cache does not is found, with no state."""
    client = FakeClient(results=[_registry(("sensor.from_registry", "mdi:door"))])

    contexts = await _adapter(client).contexts_for(["sensor.from_registry"])

    assert contexts is not None
    context = contexts["sensor.from_registry"]
    assert context.found is True
    assert context.state is None
    assert context.registry_icon == "mdi:door"


async def test_registry_icon_missing_or_blank_is_none() -> None:
    """The registry icon comes from ``ic``; a missing or blank ``ic`` yields ``None``."""
    client = FakeClient(
        results=[{"entities": [{"ei": "sensor.blank", "ic": ""}, {"ei": "sensor.absent"}]}]
    )

    contexts = await _adapter(client).contexts_for(["sensor.blank", "sensor.absent"])

    assert contexts is not None
    assert contexts["sensor.blank"].registry_icon is None
    assert contexts["sensor.absent"].registry_icon is None
    assert contexts["sensor.absent"].found is True


async def test_device_class_is_not_interpreted() -> None:
    """``device_class`` stays ``None``: attributes are not interpreted here."""
    client = FakeClient(
        states={
            "binary_sensor.front_door": _state(
                "binary_sensor.front_door", "off", attributes={"device_class": "door"}
            )
        }
    )

    contexts = await _adapter(client).contexts_for(["binary_sensor.front_door"])

    assert contexts is not None
    assert contexts["binary_sensor.front_door"].device_class is None


async def test_registry_answer_is_cached_for_the_ttl() -> None:
    """The registry is fetched once inside the TTL and refetched only after it expires."""
    now = [100.0]

    def clock() -> float:
        """Return the injected monotonic time."""
        return now[0]

    client = FakeClient(results=[_registry(("sensor.porch", "mdi:thermometer"))])
    adapter = _adapter(client, clock=clock)

    await adapter.contexts_for(["sensor.porch"])
    now[0] += ENTITY_REGISTRY_TTL_S - 1.0
    await adapter.contexts_for(["sensor.porch"])

    assert client.commands == [_LIST_FOR_DISPLAY]

    now[0] += 2.0
    await adapter.contexts_for(["sensor.porch"])

    assert client.commands == [_LIST_FOR_DISPLAY, _LIST_FOR_DISPLAY]


async def test_registry_failure_reuses_the_previous_answer() -> None:
    """A failed refetch returns the stale map, and the ids still resolve."""
    now = [100.0]

    def clock() -> float:
        """Return the injected monotonic time."""
        return now[0]

    client = FakeClient(
        results=[_registry(("sensor.porch", "mdi:thermometer"))],
    )
    adapter = _adapter(client, clock=clock)
    first = await adapter.contexts_for(["sensor.porch"])
    assert first is not None

    now[0] += ENTITY_REGISTRY_TTL_S + 1.0
    client.error = HaCommandError("registry refused", code="unknown_command", detail="nope")
    contexts = await adapter.contexts_for(["sensor.porch"])

    assert len(client.commands) == 2
    assert contexts is not None
    assert contexts["sensor.porch"].found is True
    assert contexts["sensor.porch"].registry_icon == "mdi:thermometer"


async def test_registry_failure_without_previous_answer_is_none() -> None:
    """A failed first registry fetch makes the whole lookup "could not ask"."""
    client = FakeClient(error=HaCommandError("registry refused", code=None, detail="nope"))

    assert await _adapter(client).contexts_for(["sensor.porch"]) is None


async def test_disconnected_client_returns_none_without_a_command() -> None:
    """A disconnected client is never asked: no command, no fabricated answer."""
    client = FakeClient(connected=False, states={"sensor.porch": _state("sensor.porch", "on")})

    assert await _adapter(client).contexts_for(["sensor.porch"]) is None
    assert client.commands == []


async def test_slow_registry_times_out_within_the_lookup_budget() -> None:
    """A command slower than the registry budget yields ``None`` and is cancelled."""
    client = FakeClient(results=[_registry(("sensor.porch", "mdi:eye"))], delay_s=5.0)
    adapter = _adapter(client, registry_budget_s=0.02, lookup_budget_s=0.1)

    started = time.monotonic()
    contexts = await adapter.contexts_for(["sensor.porch"])
    elapsed = time.monotonic() - started

    assert contexts is None
    assert client.cancelled is True
    assert elapsed < 2.0


async def test_command_error_is_swallowed() -> None:
    """A ``HaCommandError`` yields ``None`` instead of raising to the caller."""
    client = FakeClient(
        error=HaCommandError("registry refused", code="unknown_command", detail="x")
    )

    assert await _adapter(client).contexts_for(["sensor.porch"]) is None


@pytest.mark.parametrize(
    "result",
    [
        pytest.param("not a mapping", id="not-a-mapping"),
        pytest.param({"entities": "not a list"}, id="entities-not-a-list"),
        pytest.param(
            {"entities": [{"ic": "mdi:eye"}, "junk", {"ei": ""}, {"ei": 7}]},
            id="entries-without-entity-id",
        ),
    ],
)
async def test_malformed_registry_payload_is_tolerated(result: object) -> None:
    """A malformed registry payload is skipped, so the id reads as absent, not broken."""
    client = FakeClient(results=[result])

    contexts = await _adapter(client).contexts_for(["sensor.porch"])

    assert contexts is not None
    assert contexts["sensor.porch"].found is False
    assert contexts["sensor.porch"].registry_icon is None


async def test_empty_request_touches_nothing() -> None:
    """An empty request answers ``{}`` without sending a command."""
    client = FakeClient()

    assert await _adapter(client).contexts_for([]) == {}
    assert client.commands == []


async def test_budget_constants_are_strictly_ordered() -> None:
    """The registry budget nests inside the lookup budget, which nests inside the seam bound."""
    assert REGISTRY_BUDGET_S < LIVE_LOOKUP_BUDGET_S
    assert LIVE_LOOKUP_BUDGET_S < LIVE_LOOKUP_TIMEOUT_S


@pytest.mark.parametrize(
    ("lookup_budget_s", "registry_budget_s"),
    [
        pytest.param(LIVE_LOOKUP_TIMEOUT_S, REGISTRY_BUDGET_S, id="lookup-at-the-caller-bound"),
        pytest.param(LIVE_LOOKUP_BUDGET_S, LIVE_LOOKUP_BUDGET_S, id="registry-not-below-lookup"),
        pytest.param(0.0, REGISTRY_BUDGET_S, id="lookup-not-positive"),
        pytest.param(LIVE_LOOKUP_BUDGET_S, -1.0, id="registry-negative"),
    ],
)
async def test_mis_nested_budgets_fail_at_construction(
    lookup_budget_s: float, registry_budget_s: float
) -> None:
    """A mis-wired budget raises at construction instead of hanging a request later.

    :param lookup_budget_s: the lookup budget under test.
    :param registry_budget_s: the registry budget under test.
    """
    with pytest.raises(HaConfigurationError):
        _adapter(
            FakeClient(),
            lookup_budget_s=lookup_budget_s,
            registry_budget_s=registry_budget_s,
        )


async def test_concurrent_lookups_fetch_the_registry_once() -> None:
    """Two simultaneous lookups share one registry command, because of the lock."""
    client = FakeClient(results=[_registry(("sensor.porch", "mdi:eye"))], delay_s=0.01)
    adapter = _adapter(client)

    first, second = await asyncio.gather(
        adapter.contexts_for(["sensor.porch"]),
        adapter.contexts_for(["sensor.porch"]),
    )

    assert first is not None
    assert second is not None
    assert client.commands == [_LIST_FOR_DISPLAY]


async def test_sync_tile_ids_subscribes_in_store_order(tmp_path: Path) -> None:
    """The configured tile ids are passed to the client in store order and returned.

    :param tmp_path: pytest temporary directory for the database.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        await store.upsert_entity(EntityIn(entity_id="sensor.second", sort_order=20))
        await store.upsert_entity(EntityIn(entity_id="sensor.first", sort_order=10))
        client = FakeClient()

        entity_ids = await sync_tile_ids(store, cast(HomeAssistantWebSocketClient, client))

    assert list(entity_ids) == ["sensor.first", "sensor.second"]
    assert list(client.entity_ids) == ["sensor.first", "sensor.second"]


async def test_watch_tile_changes_follows_entity_writes(tmp_path: Path) -> None:
    """An entity write re-syncs the subscription, other writes do not, and unsubscribe stops it.

    :param tmp_path: pytest temporary directory for the database.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        await store.upsert_entity(EntityIn(entity_id="sensor.first"))
        client = FakeClient()
        unsubscribe = watch_tile_changes(store, cast(HomeAssistantWebSocketClient, client))
        await store.events.publish(entity_event(ids=(1,)))
        assert list(client.entity_ids) == ["sensor.first"]

        await store.upsert_entity(EntityIn(entity_id="sensor.second"))
        await store.events.publish(button_event(ids=(1,)))
        assert list(client.entity_ids) == ["sensor.first", "sensor.second"]

        unsubscribe()
        await store.upsert_entity(EntityIn(entity_id="sensor.third"))
        await store.events.publish(entity_event(ids=(1,)))

    assert list(client.entity_ids) == ["sensor.first", "sensor.second"]


async def test_watch_tile_changes_warns_when_the_store_cannot_be_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing store read logs a warning and never raises out of the handler.

    :param caplog: captured log records.
    """
    caplog.set_level(logging.WARNING, logger="chime_deck")
    store = _FailingStore()
    client = FakeClient()
    watch_tile_changes(cast(ConfigStore, store), cast(HomeAssistantWebSocketClient, client))

    await store.events.publish(entity_event(ids=(1,)))

    assert list(client.entity_ids) == []
    assert any(
        "could not re-read the entity list for Home Assistant" in record.getMessage()
        for record in caplog.records
    )


async def test_contexts_preserve_caller_order_without_duplicates() -> None:
    """Duplicate ids collapse to one context, in the caller's order."""
    client = FakeClient(
        states={"sensor.a": _state("sensor.a", "1"), "sensor.b": _state("sensor.b", "2")}
    )

    contexts = await _adapter(client).contexts_for(["sensor.b", "sensor.a", "sensor.b"])

    assert contexts is not None
    assert list(contexts) == ["sensor.b", "sensor.a"]
    assert all(isinstance(context, EntityContext) for context in contexts.values())
