"""Event payloads, post-commit delivery and concurrency isolation."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from chime_deck.store import (
    ButtonIn,
    ConfigEvent,
    ConfigEventName,
    ConfigStore,
    EntityIn,
    SoundIn,
    WriteError,
)

pytestmark = pytest.mark.asyncio

_PAYLOAD_KEYS = {
    "ids",
    "removed_ids",
    "slugs",
    "removed_slugs",
    "renamed",
    "keys",
}


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


def _drain(queue: asyncio.Queue[ConfigEvent]) -> list[ConfigEvent]:
    """Return every event currently queued.

    :param queue: queue subscribed to the store's event bus.
    """
    events: list[ConfigEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


async def test_queue_receives_every_event_shape(store: ConfigStore) -> None:
    """Each table emits its own event with the complete payload key set."""
    queue = store.events.subscribe_queue(maxsize=50)

    sound = await store.create_sound(SoundIn(name="Ding", source_type="file", path="ding.mp3"))
    button = await store.upsert_button(
        ButtonIn(name="Front Door", action_type="sound", sound_id=sound.id)
    )
    entity = await store.upsert_entity(EntityIn(entity_id="lock.front_door"))
    await store.set_setting("audio.volume", 30)

    events = _drain(queue)
    assert [event.what for event in events] == ["sounds", "buttons", "entities", "settings"]
    assert [event.name for event in events] == [
        ConfigEventName.SOUNDS_CHANGED,
        ConfigEventName.BUTTONS_CHANGED,
        ConfigEventName.ENTITIES_CHANGED,
        ConfigEventName.SETTINGS_CHANGED,
    ]
    for event in events:
        assert set(event.payload) == _PAYLOAD_KEYS

    assert events[0].payload["ids"] == (sound.id,)
    assert events[1].payload["ids"] == (button.id,)
    assert events[1].payload["slugs"] == (button.slug,)
    assert events[2].payload["ids"] == (entity.id,)
    assert events[3].payload["keys"] == ("audio.volume",)


async def test_handler_observes_committed_row(store: ConfigStore) -> None:
    """A handler runs after the write commits and can query it."""
    seen: list[str] = []

    async def handler(event: ConfigEvent) -> None:
        """Record the entities visible while handling an event."""
        assert event.what == "entities"
        seen.extend(entity.entity_id for entity in await store.list_entities())

    unsubscribe = store.events.subscribe(handler)
    try:
        await store.upsert_entity(EntityIn(entity_id="lock.front_door"))
    finally:
        unsubscribe()

    assert seen == ["lock.front_door"]


async def test_rolled_back_write_publishes_nothing(store: ConfigStore) -> None:
    """Events buffered by a transaction that rolls back are discarded."""
    queue = store.events.subscribe_queue(maxsize=10)

    with pytest.raises(RuntimeError, match="rollback"):
        async with store.transaction():
            await store.set_setting("audio.volume", 10)
            raise RuntimeError("rollback")

    assert _drain(queue) == []
    assert await store.get_setting("audio.volume") == 70


async def test_raising_handler_does_not_break_the_write(
    store: ConfigStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A subscriber exception is logged and swallowed after commit."""

    def handler(event: ConfigEvent) -> None:
        """Fail on purpose for every event."""
        raise RuntimeError(f"cannot handle {event.name}")

    store.events.subscribe(handler)
    await store.set_setting("audio.volume", 25)

    assert await store.get_setting("audio.volume") == 25
    assert "config event handler failed" in caplog.text


async def test_failing_commit_rolls_back_and_leaves_store_usable(
    store: ConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed COMMIT raises WriteError, publishes nothing and does not wedge."""
    queue = store.events.subscribe_queue(maxsize=10)
    original_execute = store.connection.execute
    commit_attempts = 0

    async def fail_the_first_commit(
        sql: str, parameters: Iterable[object] | None = None
    ) -> aiosqlite.Cursor:
        """Raise ``OperationalError`` on the first COMMIT, delegate otherwise."""
        nonlocal commit_attempts
        if sql == "COMMIT" and commit_attempts == 0:
            commit_attempts += 1
            raise sqlite3.OperationalError("database is locked")
        return await original_execute(sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(store.connection, "execute", fail_the_first_commit)
        with pytest.raises(WriteError):
            await store.set_setting("audio.volume", 11)

    assert commit_attempts == 1
    assert _drain(queue) == []
    assert await store.get_setting("audio.volume") == 70

    await store.set_setting("ui.theme", "light")

    assert await store.get_setting("ui.theme") == "light"
    events = _drain(queue)
    assert [event.payload["keys"] for event in events] == [("ui.theme",)]


async def test_zero_subscribers_is_fine(store: ConfigStore) -> None:
    """Writes work with no event subscribers at all."""
    await store.set_setting("ui.theme", "light")
    assert await store.get_setting("ui.theme") == "light"


async def test_concurrent_writes_isolate_rollback_events(store: ConfigStore) -> None:
    """A concurrent rollback does not discard another task's event."""
    queue = store.events.subscribe_queue(maxsize=50)

    async def successful_write() -> None:
        """Commit one setting change."""
        await store.set_setting("audio.volume", 33)

    async def failing_write() -> None:
        """Emit an event inside a transaction that then rolls back."""
        with pytest.raises(RuntimeError, match="rollback"):
            async with store.transaction():
                await store.set_setting("ui.theme", "light")
                raise RuntimeError("rollback")

    await asyncio.gather(successful_write(), failing_write())

    assert await store.get_setting("audio.volume") == 33
    assert await store.get_setting("ui.theme") == "dark"
    events = _drain(queue)
    assert [event.what for event in events] == ["settings"]
    assert events[0].payload["keys"] == ("audio.volume",)


async def test_store_modules_do_not_import_web_frameworks() -> None:
    """The store package stays free of FastAPI and websockets imports."""
    script = (
        "import sys, chime_deck.store; "
        "print('fastapi' in sys.modules); "
        "print('websockets' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False", "False"]
