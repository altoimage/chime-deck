"""Entity CRUD, ordering and sparse renumbering."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from chime_deck.store import (
    ConfigStore,
    EntityIdConflict,
    EntityIn,
    EntityNotFound,
    StoreConflict,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


async def test_create_and_list_entity(store: ConfigStore) -> None:
    """An inserted entity receives an id, timestamp and appended sort order."""
    entity = await store.upsert_entity(
        EntityIn(
            entity_id="lock.front_door",
            display_name="Front Door",
            icon="mdi:door",
            show_attributes=("battery_level",),
            hide_when_unavailable=True,
        )
    )

    assert entity.id > 0
    assert entity.entity_id == "lock.front_door"
    assert entity.show_attributes == ("battery_level",)
    assert entity.state_tones is None
    assert entity.hide_when_unavailable is True
    assert entity.colour_by_state is True
    assert entity.sort_order == 10
    assert entity.created_at.endswith("Z")
    assert await store.list_entities() == [entity]


async def test_state_tones_round_trip_as_a_mapping(store: ConfigStore) -> None:
    """Explicit state→tone overrides are stored and read back as a mapping."""
    created = await store.upsert_entity(
        EntityIn(entity_id="sensor.washer", state_tones={"running": "warn", "done": "ok"})
    )

    stored = await store.get_entity_by_id(created.id)
    assert stored is not None
    assert stored.state_tones == {"running": "warn", "done": "ok"}
    assert dict(stored.state_tones) == {"running": "warn", "done": "ok"}


async def test_no_state_tones_stays_distinct_from_an_empty_mapping(store: ConfigStore) -> None:
    """``None`` means "no overrides" and is not the same as an empty object."""
    untouched = await store.upsert_entity(EntityIn(entity_id="sensor.plain"))
    cleared = await store.upsert_entity(EntityIn(entity_id="sensor.cleared", state_tones={}))

    assert untouched.state_tones is None
    assert cleared.state_tones == {}
    reloaded = await store.get_entity_by_id(cleared.id)
    assert reloaded is not None
    assert reloaded.state_tones == {}


async def test_colour_by_state_persists_and_defaults_to_true(store: ConfigStore) -> None:
    """The opt-out is written where asked and on by default everywhere else."""
    neutral = await store.upsert_entity(EntityIn(entity_id="sensor.neutral", colour_by_state=False))
    coloured = await store.upsert_entity(EntityIn(entity_id="sensor.coloured"))

    assert neutral.colour_by_state is False
    assert coloured.colour_by_state is True
    reloaded = await store.get_entity_by_id(neutral.id)
    assert reloaded is not None
    assert reloaded.colour_by_state is False


async def test_update_can_clear_state_tones(store: ConfigStore) -> None:
    """An update writing ``None`` removes every explicit override."""
    created = await store.upsert_entity(
        EntityIn(entity_id="sensor.washer", state_tones={"running": "warn"})
    )

    updated = await store.upsert_entity(
        EntityIn(id=created.id, entity_id="sensor.washer", state_tones=None)
    )

    assert updated.state_tones is None
    reloaded = await store.get_entity_by_id(created.id)
    assert reloaded is not None
    assert reloaded.state_tones is None


async def test_deleting_an_entity_leaves_another_tones_intact(store: ConfigStore) -> None:
    """Tones belong to their own row and survive a neighbour's deletion."""
    keep = await store.upsert_entity(
        EntityIn(entity_id="sensor.keep", state_tones={"running": "alert"}, colour_by_state=False)
    )
    remove = await store.upsert_entity(EntityIn(entity_id="sensor.remove"))

    await store.delete_entity(remove.id)

    remaining = await store.get_entity_by_id(keep.id)
    assert remaining is not None
    assert remaining.state_tones == {"running": "alert"}
    assert remaining.colour_by_state is False


async def test_update_entity(store: ConfigStore) -> None:
    """Updating by id changes fields and preserves creation time and ordering."""
    created = await store.upsert_entity(EntityIn(entity_id="sensor.kitchen"))
    updated = await store.upsert_entity(
        EntityIn(
            id=created.id,
            entity_id="sensor.kitchen",
            display_name="Kitchen",
            accent_color="#ff5555",
        )
    )

    assert updated.id == created.id
    assert updated.display_name == "Kitchen"
    assert updated.accent_color == "#ff5555"
    assert updated.sort_order == created.sort_order
    assert updated.created_at == created.created_at


async def test_duplicate_entity_id_conflicts(store: ConfigStore) -> None:
    """Two rows cannot share an ``entity_id``."""
    await store.upsert_entity(EntityIn(entity_id="sensor.kitchen"))
    with pytest.raises(StoreConflict):
        await store.upsert_entity(EntityIn(entity_id="sensor.kitchen"))


async def test_duplicate_insert_reports_the_duplicated_entity_id(
    store: ConfigStore,
) -> None:
    """An insert that collides names the id and marks itself as a creation."""
    await store.upsert_entity(EntityIn(entity_id="sensor.kitchen"))

    with pytest.raises(EntityIdConflict) as exc_info:
        await store.upsert_entity(EntityIn(entity_id="sensor.kitchen"))

    error = exc_info.value
    assert isinstance(error, StoreConflict)
    assert error.entity_id == "sensor.kitchen"
    assert error.creating is True
    assert "UNIQUE constraint failed" not in str(error)


async def test_duplicate_update_reports_the_duplicated_entity_id(store: ConfigStore) -> None:
    """Renaming a row onto another row's id is reported as an update conflict."""
    first = await store.upsert_entity(EntityIn(entity_id="sensor.kitchen"))
    second = await store.upsert_entity(EntityIn(entity_id="sensor.hallway"))

    with pytest.raises(EntityIdConflict) as exc_info:
        await store.upsert_entity(EntityIn(id=second.id, entity_id=first.entity_id))

    error = exc_info.value
    assert error.entity_id == first.entity_id
    assert error.creating is False


async def test_get_entity_by_id_returns_the_row_or_none(store: ConfigStore) -> None:
    """The id lookup mirrors ``list_entities`` and misses with ``None``."""
    entity = await store.upsert_entity(EntityIn(entity_id="sensor.kitchen"))

    assert await store.get_entity_by_id(entity.id) == entity
    assert await store.get_entity_by_id(entity.id + 1) is None


async def test_unknown_entity_is_not_found(store: ConfigStore) -> None:
    """Updating or deleting a missing entity raises ``EntityNotFound``."""
    with pytest.raises(EntityNotFound):
        await store.upsert_entity(EntityIn(id=999, entity_id="sensor.missing"))
    with pytest.raises(EntityNotFound):
        await store.delete_entity(999)


async def test_list_is_ordered_by_sort_order_then_id(store: ConfigStore) -> None:
    """Entities sort by ``sort_order`` and fall back to insertion id."""
    third = await store.upsert_entity(EntityIn(entity_id="sensor.third", sort_order=30))
    first = await store.upsert_entity(EntityIn(entity_id="sensor.first", sort_order=10))
    second = await store.upsert_entity(EntityIn(entity_id="sensor.second", sort_order=20))

    assert [entity.id for entity in await store.list_entities()] == [
        first.id,
        second.id,
        third.id,
    ]


async def test_delete_entity(store: ConfigStore) -> None:
    """Deleting removes the row and a second delete reports not found."""
    entity = await store.upsert_entity(EntityIn(entity_id="sensor.kitchen"))
    await store.delete_entity(entity.id)
    assert await store.list_entities() == []
    with pytest.raises(EntityNotFound):
        await store.delete_entity(entity.id)


async def test_reorder_entities_is_sparse_and_appends_the_rest(store: ConfigStore) -> None:
    """Requested ids get 10, 20, … and the rest follow without collisions."""
    entities = [
        await store.upsert_entity(EntityIn(entity_id=f"sensor.{index}")) for index in range(4)
    ]
    first, second, third, fourth = entities

    await store.reorder_entities([third.id, first.id])

    orders = {entity.id: entity.sort_order for entity in await store.list_entities()}
    assert orders[third.id] == 10
    assert orders[first.id] == 20
    assert orders[second.id] == 30
    assert orders[fourth.id] == 40
    assert len(set(orders.values())) == len(orders)


async def test_reorder_unknown_id_changes_nothing(store: ConfigStore) -> None:
    """An unknown id aborts the transaction before any order is written."""
    entity = await store.upsert_entity(EntityIn(entity_id="sensor.kitchen"))
    with pytest.raises(EntityNotFound):
        await store.reorder_entities([entity.id, 999])

    remaining = await store.list_entities()
    assert remaining[0].sort_order == entity.sort_order
