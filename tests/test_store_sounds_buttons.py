"""Sound library, button validation, bake state and forced sound deletion."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import aiosqlite
import pytest
import pytest_asyncio

from chime_deck.mqtt import is_mqtt_safe_slug
from chime_deck.store import (
    Button,
    ButtonIn,
    ButtonNotFound,
    ConfigEvent,
    ConfigStore,
    SoundIn,
    SoundInUseError,
    SoundNotFound,
    SoundUpdate,
    StoreConflict,
    ValidationError,
    derive_slug,
)
from chime_deck.store.database import Database

pytestmark = pytest.mark.asyncio


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


async def _get_button(store: ConfigStore, button_id: int) -> Button:
    """Return one button by id.

    :param store: connected store.
    :param button_id: database id.
    """
    for button in await store.list_buttons():
        if button.id == button_id:
            return button
    raise AssertionError(f"button {button_id} not found")


async def test_sound_crud_and_unique_file_path(store: ConfigStore) -> None:
    """File sounds are created, patched and deduplicated by path."""
    created = await store.create_sound(
        SoundIn(name="Ding dong", source_type="file", path="ding-dong.mp3")
    )
    assert created.path == "ding-dong.mp3"
    assert await store.get_sound(created.id) == created

    with pytest.raises(StoreConflict):
        await store.create_sound(SoundIn(name="Copy", source_type="file", path="ding-dong.mp3"))

    updated = await store.update_sound(created.id, SoundUpdate(name="Ding"))
    assert updated.name == "Ding"
    assert updated.path == "ding-dong.mp3"

    await store.create_sound(SoundIn(name="Other", source_type="file", path="other.mp3"))
    with pytest.raises(StoreConflict):
        await store.update_sound(created.id, SoundUpdate(path="other.mp3"))

    with pytest.raises(SoundNotFound):
        await store.get_sound(999)


async def test_url_sounds_are_not_unique(store: ConfigStore) -> None:
    """A URL sound has no unique index, so duplicates are allowed."""
    first = await store.create_sound(
        SoundIn(name="Alarm", source_type="url", url="https://example.invalid/alarm.wav")
    )
    second = await store.create_sound(
        SoundIn(name="Alarm copy", source_type="url", url="https://example.invalid/alarm.wav")
    )
    assert first.id != second.id


async def test_button_validation_failures(store: ConfigStore) -> None:
    """The sound/TTS/volume business rules reject invalid buttons."""
    sound = await store.create_sound(SoundIn(name="Ding", source_type="file", path="ding.mp3"))

    with pytest.raises(ValidationError):
        await store.upsert_button(ButtonIn(name="Bad", action_type="beep"))
    with pytest.raises(ValidationError):
        await store.upsert_button(ButtonIn(name="No sound", action_type="sound"))
    with pytest.raises(ValidationError):
        await store.upsert_button(ButtonIn(name="Missing sound", action_type="sound", sound_id=999))
    with pytest.raises(ValidationError):
        await store.upsert_button(ButtonIn(name="Empty", action_type="tts", tts_text="   "))
    with pytest.raises(ValidationError):
        await store.upsert_button(
            ButtonIn(name="Loud", action_type="sound", sound_id=sound.id, volume_override=101)
        )


async def test_slug_generation_and_conflicts(store: ConfigStore) -> None:
    """Slugs are derived from names and must be unique."""
    sound = await store.create_sound(SoundIn(name="Ding", source_type="file", path="ding.mp3"))
    button = await store.upsert_button(
        ButtonIn(name="Front Door Chime", action_type="sound", sound_id=sound.id)
    )
    assert button.slug == "front-door-chime"

    with pytest.raises(StoreConflict):
        await store.upsert_button(
            ButtonIn(name="Front Door Chime", action_type="sound", sound_id=sound.id)
        )

    explicit = await store.upsert_button(
        ButtonIn(name="Anything", action_type="sound", sound_id=sound.id, slug="custom-slug")
    )
    assert explicit.slug == "custom-slug"


async def test_derive_slug_is_public_and_does_not_truncate() -> None:
    """The store keeps no charset or length bound: the API is the gate (HSD-po1g).

    A caller has to be able to predict the slug an insert will write, and a
    legacy row or a restored bundle must keep working, so `derive_slug` is
    exported and deliberately permissive.
    """
    slug = derive_slug("A" * 200)
    assert slug == "a" * 200
    assert len(slug) == 200
    assert is_mqtt_safe_slug(slug) is False


async def test_update_preserves_slug_and_reports_rename(store: ConfigStore) -> None:
    """An update keeps the stored slug unless an explicit new one is supplied."""
    sound = await store.create_sound(SoundIn(name="Ding", source_type="file", path="ding.mp3"))
    created = await store.upsert_button(
        ButtonIn(name="Front Door Chime", action_type="sound", sound_id=sound.id)
    )

    renamed_name_only = await store.upsert_button(
        ButtonIn(
            id=created.id,
            name="Front Door Bell",
            action_type="sound",
            sound_id=sound.id,
        )
    )
    assert renamed_name_only.slug == created.slug

    queue = store.events.subscribe_queue(maxsize=50)
    renamed = await store.upsert_button(
        ButtonIn(
            id=created.id,
            slug="front-bell",
            name="Front Door Bell",
            action_type="sound",
            sound_id=sound.id,
        )
    )
    assert renamed.slug == "front-bell"

    events = _drain(queue)
    assert len(events) == 1
    assert events[0].what == "buttons"
    assert events[0].payload["renamed"] == (("front-door-chime", "front-bell"),)
    assert events[0].payload["removed_slugs"] == ()


async def test_get_button_by_id_matches_the_slug_lookup(store: ConfigStore) -> None:
    """An id lookup returns the same row as the slug lookup, and nothing for an unknown id."""
    sound = await store.create_sound(SoundIn(name="Ding", source_type="file", path="ding.mp3"))
    created = await store.upsert_button(
        ButtonIn(name="Front Door Chime", action_type="sound", sound_id=sound.id)
    )

    assert await store.get_button_by_id(created.id) == created
    assert await store.get_button_by_id(created.id) == await store.get_button_by_slug(created.slug)
    assert await store.get_button_by_id(999) is None


async def test_get_button_by_id_survives_a_rename(store: ConfigStore) -> None:
    """A rename changes the slug but never the primary key the UI presses by."""
    sound = await store.create_sound(SoundIn(name="Ding", source_type="file", path="ding.mp3"))
    created = await store.upsert_button(
        ButtonIn(name="Front Door Chime", action_type="sound", sound_id=sound.id)
    )

    renamed = await store.upsert_button(
        ButtonIn(
            id=created.id,
            slug="front-bell",
            name="Front Door Bell",
            action_type="sound",
            sound_id=sound.id,
        )
    )

    assert renamed.id == created.id
    assert renamed.slug != created.slug
    assert await store.get_button_by_id(created.id) == renamed
    assert await store.get_button_by_slug(created.slug) is None
    assert await store.get_button_by_slug(renamed.slug) == renamed


async def test_unknown_button_operations(store: ConfigStore) -> None:
    """Missing buttons are reported consistently."""
    with pytest.raises(ButtonNotFound):
        await store.delete_button(999)
    with pytest.raises(ButtonNotFound):
        await store.set_bake_state(999, "abc")
    with pytest.raises(ButtonNotFound):
        await store.record_bake_failure(999, "boom")
    with pytest.raises(ButtonNotFound):
        await store.clear_bake(999)
    assert await store.get_button_by_slug("nope") is None


async def test_bake_state_transitions(store: ConfigStore) -> None:
    """Successful, failed and cleared bakes update every bake column."""
    button = await store.upsert_button(
        ButtonIn(name="Announcement", action_type="tts", tts_text="Hello there")
    )

    baked = await store.set_bake_state(button.id, "sha-1", fmt="mp3")
    assert baked.baked_sha == "sha-1"
    assert baked.baked_format == "mp3"
    assert baked.baked_at is not None
    assert baked.baked_error is None

    failed = await store.record_bake_failure(button.id, "engine offline")
    assert failed.baked_sha is None
    assert failed.baked_format is None
    assert failed.baked_at is None
    assert failed.baked_error == "engine offline"

    cleared = await store.clear_bake(button.id, "manual reset")
    assert cleared.baked_sha is None
    assert cleared.baked_format is None
    assert cleared.baked_at is None
    assert cleared.baked_error == "manual reset"


async def test_tts_edit_clears_bake(store: ConfigStore) -> None:
    """Changing ``tts_text`` or ``tts_engine`` invalidates the bake."""
    button = await store.upsert_button(
        ButtonIn(name="Announcement", action_type="tts", tts_text="Hello there")
    )
    await store.set_bake_state(button.id, "sha-1", fmt="wav")

    untouched = await store.upsert_button(
        ButtonIn(
            id=button.id,
            name="Announcement",
            action_type="tts",
            tts_text="Hello there",
            icon="mdi:bell",
        )
    )
    assert untouched.baked_sha == "sha-1"
    assert untouched.baked_format == "wav"

    await store.set_bake_state(button.id, "sha-1", fmt="wav")
    text_changed = await store.upsert_button(
        ButtonIn(id=button.id, name="Announcement", action_type="tts", tts_text="Good evening")
    )
    assert text_changed.baked_sha is None
    assert text_changed.baked_format is None
    assert text_changed.baked_at is None

    await store.set_bake_state(button.id, "sha-2")
    engine_changed = await store.upsert_button(
        ButtonIn(
            id=button.id,
            name="Announcement",
            action_type="tts",
            tts_text="Good evening",
            tts_engine="piper",
        )
    )
    assert engine_changed.baked_sha is None


async def test_tts_setting_change_clears_only_affected_engines(store: ConfigStore) -> None:
    """Engine options invalidate matching effective engines, not every button."""
    default_engine = await store.upsert_button(
        ButtonIn(name="Default engine", action_type="tts", tts_text="One")
    )
    ha_engine = await store.upsert_button(
        ButtonIn(name="HA engine", action_type="tts", tts_text="Two", tts_engine="ha_tts")
    )
    piper = await store.upsert_button(
        ButtonIn(name="Piper voice", action_type="tts", tts_text="Three", tts_engine="piper")
    )
    espeak = await store.upsert_button(
        ButtonIn(name="Espeak voice", action_type="tts", tts_text="Four", tts_engine="espeak_ng")
    )
    for button in (default_engine, ha_engine, piper, espeak):
        await store.set_bake_state(button.id, f"sha-{button.id}")

    queue = store.events.subscribe_queue(maxsize=50)
    await store.set_setting("tts.ha.language", "en_US")

    assert (await _get_button(store, default_engine.id)).baked_sha is None
    assert (await _get_button(store, ha_engine.id)).baked_sha is None
    assert (await _get_button(store, piper.id)).baked_sha == f"sha-{piper.id}"
    assert (await _get_button(store, espeak.id)).baked_sha == f"sha-{espeak.id}"

    events = _drain(queue)
    assert {event.what for event in events} == {"settings", "buttons"}
    button_event = next(event for event in events if event.what == "buttons")
    assert set(cast(tuple[int, ...], button_event.payload["ids"])) == {
        default_engine.id,
        ha_engine.id,
    }

    for button in (default_engine, ha_engine, piper, espeak):
        await store.set_bake_state(button.id, f"sha-{button.id}")
    await store.set_setting("tts.default_engine", "piper")
    for button in (default_engine, ha_engine, piper, espeak):
        assert (await _get_button(store, button.id)).baked_sha is None


async def test_remote_tts_url_change_clears_only_remote_bakes(store: ConfigStore) -> None:
    """A repointed renderer invalidates remote bakes; its token invalidates nothing.

    ``tts.remote_tts.url`` can change the rendered bytes while the cache key
    carries no URL, so every button whose effective engine is ``remote_tts``
    must re-bake. ``tts.remote_tts.token`` changes no rendered byte, so it must
    leave every bake alone — hence the single ``buttons`` event below.

    :param store: connected store.
    """
    remote = await store.upsert_button(
        ButtonIn(name="Remote engine", action_type="tts", tts_text="One", tts_engine="remote_tts")
    )
    ha_engine = await store.upsert_button(
        ButtonIn(name="HA engine", action_type="tts", tts_text="Two", tts_engine="ha_tts")
    )
    for button in (remote, ha_engine):
        await store.set_bake_state(button.id, f"sha-{button.id}")

    queue = store.events.subscribe_queue(maxsize=50)
    await store.set_setting("tts.remote_tts.url", "http://renderer.test")
    await store.set_setting("tts.remote_tts.token", "a-token-that-renders-nothing")

    assert (await _get_button(store, remote.id)).baked_sha is None
    assert (await _get_button(store, ha_engine.id)).baked_sha == f"sha-{ha_engine.id}"

    events = _drain(queue)
    assert {event.what for event in events} == {"settings", "buttons"}
    button_events = [event for event in events if event.what == "buttons"]
    assert len(button_events) == 1
    assert set(cast(tuple[int, ...], button_events[0].payload["ids"])) == {remote.id}


async def test_sound_in_use_lists_buttons_and_changes_nothing(store: ConfigStore) -> None:
    """A plain delete of a referenced sound fails without modifying anything."""
    sound = await store.create_sound(SoundIn(name="Ding", source_type="file", path="ding.mp3"))
    first = await store.upsert_button(
        ButtonIn(name="First", action_type="sound", sound_id=sound.id)
    )
    second = await store.upsert_button(
        ButtonIn(name="Second", action_type="sound", sound_id=sound.id)
    )
    assert {button.id for button in await store.buttons_using_sound(sound.id)} == {
        first.id,
        second.id,
    }

    queue = store.events.subscribe_queue(maxsize=50)
    with pytest.raises(SoundInUseError) as error:
        await store.delete_sound(sound.id)

    assert {button.id for button in error.value.affected_buttons} == {first.id, second.id}
    assert await store.get_sound(sound.id) == sound
    assert {button.id for button in await store.list_buttons()} == {first.id, second.id}
    assert _drain(queue) == []


async def test_forced_sound_delete_removes_buttons_in_one_transaction(
    store: ConfigStore,
) -> None:
    """A forced delete removes the buttons and sound, reported by snapshots."""
    sound = await store.create_sound(SoundIn(name="Ding", source_type="file", path="ding.mp3"))
    first = await store.upsert_button(
        ButtonIn(name="First", action_type="sound", sound_id=sound.id)
    )
    second = await store.upsert_button(
        ButtonIn(name="Second", action_type="sound", sound_id=sound.id)
    )

    queue = store.events.subscribe_queue(maxsize=50)
    result = await store.delete_sound(sound.id, force=True)

    assert result.deleted is True
    assert {button.id for button in result.affected_buttons} == {first.id, second.id}
    assert await store.list_buttons() == []
    with pytest.raises(SoundNotFound):
        await store.get_sound(sound.id)

    async with store.connection.execute("PRAGMA foreign_key_check") as cursor:
        assert await cursor.fetchall() == []

    events = _drain(queue)
    assert [event.what for event in events] == ["buttons", "sounds"]
    assert events[0].payload["ids"] == ()
    assert events[0].payload["slugs"] == ()
    assert set(cast(tuple[int, ...], events[0].payload["removed_ids"])) == {
        first.id,
        second.id,
    }
    assert set(cast(tuple[str, ...], events[0].payload["removed_slugs"])) == {
        first.slug,
        second.slug,
    }
    assert events[1].payload["removed_ids"] == (sound.id,)


async def test_unreferenced_sound_deletes_cleanly(store: ConfigStore) -> None:
    """A sound with no buttons deletes without touching any button rows."""
    sound = await store.create_sound(SoundIn(name="Ding", source_type="file", path="ding.mp3"))
    queue = store.events.subscribe_queue(maxsize=10)
    result = await store.delete_sound(sound.id)
    assert result.deleted is True
    assert result.affected_buttons == ()
    assert await store.list_sounds() == []
    assert [event.what for event in _drain(queue)] == ["sounds"]


async def test_reorder_buttons_renumbers_sparsely_with_one_write(
    store: ConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reorder is one transaction, one event, and refuses unknown ids untouched.

    :param store: connected store.
    :param monkeypatch: patcher that counts ``Database.transaction()`` calls.
    """
    sound = await store.create_sound(SoundIn(name="Ding", source_type="file", path="ding.mp3"))
    first = await store.upsert_button(ButtonIn(name="One", action_type="sound", sound_id=sound.id))
    second = await store.upsert_button(ButtonIn(name="Two", action_type="sound", sound_id=sound.id))
    third = await store.upsert_button(
        ButtonIn(name="Three", action_type="sound", sound_id=sound.id)
    )

    queue = store.events.subscribe_queue(maxsize=50)
    _drain(queue)

    calls: list[int] = []
    original = Database.transaction

    @asynccontextmanager
    async def counting_transaction(db: Database) -> AsyncIterator[aiosqlite.Connection]:
        calls.append(1)
        async with original(db) as connection:
            yield connection

    monkeypatch.setattr(Database, "transaction", counting_transaction)

    await store.reorder_buttons([third.id, first.id])
    assert calls == [1]
    assert [(button.id, button.sort_order) for button in await store.list_buttons()] == [
        (third.id, 10),
        (first.id, 20),
        (second.id, 30),
    ]

    events = _drain(queue)
    assert [event.what for event in events] == ["buttons"]
    assert events[0].payload["ids"] == (third.id, first.id, second.id)

    with pytest.raises(ButtonNotFound):
        await store.reorder_buttons([second.id, 9999])
    assert [(button.id, button.sort_order) for button in await store.list_buttons()] == [
        (third.id, 10),
        (first.id, 20),
        (second.id, 30),
    ]
    assert _drain(queue) == []

    await store.reorder_buttons([])
    assert _drain(queue) == []
