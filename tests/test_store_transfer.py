"""JSON export/import round-tripping, secrets, dry-run and validation."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio

from chime_deck.store import (
    ButtonIn,
    ConfigStore,
    EntityIn,
    ImportValidationError,
    SoundIn,
    export_config,
    import_config,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def source(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store that tests export from.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "source.db") as instance:
        yield instance


async def _normalized(document: dict[str, object]) -> dict[str, object]:
    """Return a copy of an export without its volatile timestamp.

    :param document: export returned by :func:`export_config`.
    """
    copy = dict(document)
    copy.pop("exported_at")
    return copy


def _entity_presentation(document: dict[str, object]) -> dict[str, tuple[object, object]]:
    """Return each exported entity's ``(state_tones, colour_by_state)`` pair.

    :param document: export returned by :func:`export_config`.
    """
    entities = cast("list[dict[str, object]]", document["entities"])
    return {
        cast(str, entity["entity_id"]): (entity["state_tones"], entity["colour_by_state"])
        for entity in entities
    }


async def _populate(store: ConfigStore) -> None:
    """Create a representative configuration in a store.

    :param store: connected store.
    """
    await store.set_setting("audio.volume", 55)
    await store.set_setting("audio.start_timeout_s", 40)
    await store.set_setting("ha.token", "source-secret")
    await store.set_setting("mqtt.password", "source-password")

    await store.upsert_entity(
        EntityIn(
            entity_id="lock.front_door",
            display_name="Front Door",
            icon="mdi:door",
            show_attributes=("battery_level", "tamper"),
            state_tones={"locked": "ok", "running": "warn"},
            colour_by_state=False,
            sort_order=20,
        )
    )
    await store.upsert_entity(EntityIn(entity_id="sensor.kitchen", sort_order=10))

    file_sound = await store.create_sound(
        SoundIn(
            name="Ding dong",
            source_type="file",
            path="ding-dong.mp3",
            format="mp3",
            duration_ms=1200,
            bytes=999,
        )
    )
    await store.create_sound(
        SoundIn(name="Siren", source_type="url", url="https://example.invalid/siren.ogg")
    )

    button = await store.upsert_button(
        ButtonIn(
            name="Front Door Chime",
            action_type="sound",
            sound_id=file_sound.id,
            dedupe_seconds=3,
            show_on_dashboard=True,
        )
    )
    await store.set_bake_state(button.id, "sha-that-must-not-export")
    await store.upsert_button(
        ButtonIn(
            name="Announcement",
            action_type="tts",
            tts_text="Someone is here",
            tts_engine="piper",
            volume_override=80,
            quiet_hours_exempt=True,
        )
    )


async def test_export_import_export_round_trips(source: ConfigStore, tmp_path: Path) -> None:
    """Exporting, importing into a fresh database, and exporting matches."""
    await _populate(source)
    first = await export_config(source)

    async with ConfigStore(tmp_path / "target.db") as target:
        report = await import_config(target, first)
        second = await export_config(target)

    assert report.dry_run is False
    assert report.entities == 2
    assert report.sounds == 2
    assert report.buttons == 2
    assert report.settings_written > 0
    first_settings = cast("dict[str, object]", first["settings"])
    assert first_settings["audio.start_timeout_s"] == 40
    second_settings = cast("dict[str, object]", second["settings"])
    assert second_settings["audio.start_timeout_s"] == 40
    assert _entity_presentation(first) == {
        "lock.front_door": ({"locked": "ok", "running": "warn"}, False),
        "sensor.kitchen": (None, True),
    }
    assert _entity_presentation(second) == _entity_presentation(first)
    assert await _normalized(second) == await _normalized(first)


async def test_an_export_carries_the_current_format_tag(source: ConfigStore) -> None:
    """A new export is stamped with the post-rename tag, never the old one."""
    document = await export_config(source)

    assert document["format"] == "chime-deck/config"


async def test_an_export_taken_before_the_rename_still_imports(
    source: ConfigStore, tmp_path: Path
) -> None:
    """The pre-rename tag is still accepted, so yesterday's backup restores.

    :param source: store holding the configuration to export.
    :param tmp_path: pytest temporary directory.
    """
    await _populate(source)
    document = dict(await export_config(source))
    document["format"] = "ha-super-doorbell/config"
    expected_buttons = len(cast("list[object]", document["buttons"]))
    expected_entities = len(cast("list[object]", document["entities"]))

    async with ConfigStore(tmp_path / "restored.db") as target:
        report = await import_config(target, document)

        assert report.buttons == expected_buttons
        assert report.entities == expected_entities


async def test_export_excludes_bake_state_and_ids(source: ConfigStore) -> None:
    """No database ids, timestamps or bake columns appear in an export."""
    await _populate(source)
    document = await export_config(source)
    dumped = json.dumps(document)

    assert "sha-that-must-not-export" not in dumped
    assert "baked_sha" not in dumped
    assert "baked_format" not in dumped
    assert "created_at" not in dumped
    assert "updated_at" not in dumped
    assert '"id"' not in dumped
    buttons = document["buttons"]
    assert isinstance(buttons, list)
    sound_references = {button["sound"] for button in buttons if isinstance(button, dict)}
    assert sound_references == {"ding-dong.mp3", None}


async def test_secrets_are_excluded_unless_requested(source: ConfigStore, tmp_path: Path) -> None:
    """Secrets stay out by default, survive a merge, and import when included."""
    await _populate(source)

    default_export = await export_config(source)
    default_dump = json.dumps(default_export)
    assert "ha.token" not in default_dump
    assert "mqtt.password" not in default_dump
    assert "source-secret" not in default_dump
    assert "source-password" not in default_dump

    with_secrets = await export_config(source, include_secrets=True)
    settings = with_secrets["settings"]
    assert isinstance(settings, dict)
    assert settings["ha.token"] == "source-secret"
    assert settings["mqtt.password"] == "source-password"

    async with ConfigStore(tmp_path / "target.db") as target:
        await target.set_setting("ha.token", "target-secret")
        await import_config(target, default_export)
        assert await target.get_setting("ha.token") == "target-secret"

        await import_config(target, with_secrets)
        assert await target.get_setting("ha.token") == "source-secret"


async def test_dry_run_writes_nothing(source: ConfigStore, tmp_path: Path) -> None:
    """A dry-run validates and reports, then rolls the whole import back."""
    await _populate(source)
    document = await export_config(source)

    async with ConfigStore(tmp_path / "target.db") as target:
        queue = target.events.subscribe_queue(maxsize=100)
        report = await import_config(target, document, dry_run=True)

        assert report.dry_run is True
        assert report.entities == 2
        assert await target.list_entities() == []
        assert await target.list_sounds() == []
        assert await target.list_buttons() == []
        assert await target.get_setting("audio.volume") == 70
        assert queue.empty()


async def test_import_accumulates_all_problems_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """Bad format, version, keys, duplicates and dangling refs all report."""
    malformed: dict[str, object] = {
        "format": "not-our-format",
        "version": 99,
        "settings": {"nope.unknown": 1},
        "entities": [
            {"entity_id": "sensor.duplicate"},
            {"entity_id": "sensor.duplicate"},
        ],
        "sounds": [
            {"name": "One", "source_type": "file", "path": "same.mp3"},
            {"name": "Two", "source_type": "file", "path": "same.mp3"},
        ],
        "buttons": [
            {
                "slug": "duplicate-slug",
                "name": "One",
                "action_type": "sound",
                "sound": "missing.mp3",
                "volume_override": 999,
            },
            {
                "slug": "duplicate-slug",
                "name": "Two",
                "action_type": "sound",
                "sound": "missing.mp3",
            },
        ],
    }

    async with ConfigStore(tmp_path / "target.db") as target:
        with pytest.raises(ImportValidationError) as error:
            await import_config(target, malformed)

        problems = "\n".join(error.value.problems)
        assert len(error.value.problems) >= 8
        assert "format must be" in problems
        assert "version must be" in problems
        assert "unknown setting key" in problems
        assert "duplicates" in problems
        assert "does not resolve" in problems
        assert "volume_override" in problems

        assert await target.list_entities() == []
        assert await target.list_sounds() == []
        assert await target.list_buttons() == []
        assert await target.get_setting("audio.volume") == 70


async def test_import_merges_and_resolves_existing_sounds(
    source: ConfigStore, tmp_path: Path
) -> None:
    """Rows absent from the document survive and existing sounds resolve."""
    async with ConfigStore(tmp_path / "target.db") as target:
        existing_sound = await target.create_sound(
            SoundIn(name="Keep", source_type="file", path="keep.mp3")
        )
        await target.upsert_entity(EntityIn(entity_id="sensor.keep"))
        await target.upsert_button(
            ButtonIn(name="Keep Button", action_type="sound", sound_id=existing_sound.id)
        )

        document: dict[str, object] = {
            "format": "chime-deck/config",
            "version": 1,
            "exported_at": "2026-01-01T00:00:00.000000Z",
            "settings": {"audio.volume": 20},
            "entities": [{"entity_id": "sensor.new"}],
            "sounds": [{"name": "New", "source_type": "file", "path": "new.mp3"}],
            "buttons": [
                {
                    "slug": "new-button",
                    "name": "New Button",
                    "action_type": "sound",
                    "sound": "new.mp3",
                },
                {
                    "slug": "old-reference",
                    "name": "Old Reference",
                    "action_type": "sound",
                    "sound": "keep.mp3",
                },
            ],
        }
        report = await import_config(target, document)

        assert report.entities == 1
        assert report.sounds == 1
        assert report.buttons == 2
        assert {entity.entity_id for entity in await target.list_entities()} == {
            "sensor.keep",
            "sensor.new",
        }
        buttons = {button.slug: button for button in await target.list_buttons()}
        assert set(buttons) == {"keep-button", "new-button", "old-reference"}
        assert buttons["old-reference"].sound_id == existing_sound.id
        assert await target.get_setting("audio.volume") == 20


async def test_export_omits_settings_unknown_to_this_build(
    source: ConfigStore, tmp_path: Path
) -> None:
    """A stored key this build does not know stays local and never breaks import."""
    await source.set_setting("audio.volume", 45)
    await source.connection.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        ("future.only_key", '"kept-locally"', "2026-01-01T00:00:00.000000Z"),
    )

    assert (await source.all_settings())["future.only_key"] == "kept-locally"
    document = await export_config(source)

    settings = document["settings"]
    assert isinstance(settings, dict)
    assert "future.only_key" not in settings
    assert settings["audio.volume"] == 45

    async with ConfigStore(tmp_path / "target.db") as target:
        await import_config(target, document)
        assert await target.get_setting("audio.volume") == 45
        assert "future.only_key" not in await target.all_settings()


_PROVIDER_ID = "0123456789ab"
"""A synthetic provider id: 12 lower-case hex characters, as D25 defines them."""

_PROVIDER_SECRET_KEY = f"tts.providers.{_PROVIDER_ID}.api_key"


async def test_export_with_secrets_carries_the_provider_key(
    source: ConfigStore, tmp_path: Path
) -> None:
    """A provider key leaves the device only with ``include_secrets`` and re-imports alone."""
    await source.set_provider_secret(_PROVIDER_ID, "sk-live-123")

    default_export = await export_config(source)
    default_settings = default_export["settings"]
    assert isinstance(default_settings, dict)
    assert _PROVIDER_SECRET_KEY not in default_settings
    assert "sk-live-123" not in json.dumps(default_export)

    with_secrets = await export_config(source, include_secrets=True)
    assert (
        cast("dict[str, object]", with_secrets["settings"])[_PROVIDER_SECRET_KEY] == "sk-live-123"
    )

    async with ConfigStore(tmp_path / "target.db") as target:
        report = await import_config(target, with_secrets)

        assert report.settings_written == len(cast("dict[str, object]", with_secrets["settings"]))
        assert await target.get_provider_secret(_PROVIDER_ID) == "sk-live-123"


_ADMIN_PASSWORD_HASH_KEY = "server.admin_password_hash"
"""The configuration-interface password hash: local to the device, always."""

_STORED_HASH = "scrypt$16384$8$1$c2FsdA$a2V5"
_OTHER_HASH = "scrypt$16384$8$1$b3RoZXI$a2V5"


async def test_the_password_hash_never_leaves_the_device(
    source: ConfigStore, tmp_path: Path
) -> None:
    """Even ``include_secrets`` omits the admin password hash, and import leaves it alone.

    The hash signs the admin cookie, so a copy of it is a copy of the login, and a
    document cannot prove the current password. A backup must therefore neither
    carry it nor be able to clear it.

    :param source: store to export from.
    :param tmp_path: pytest temporary directory.
    """
    await source.set_setting(_ADMIN_PASSWORD_HASH_KEY, _STORED_HASH)

    document = await export_config(source, include_secrets=True)
    dumped = json.dumps(document)
    settings = document["settings"]

    assert isinstance(settings, dict)
    assert _ADMIN_PASSWORD_HASH_KEY not in settings
    assert _ADMIN_PASSWORD_HASH_KEY not in dumped
    assert _STORED_HASH not in dumped

    async with ConfigStore(tmp_path / "target.db") as target:
        await target.set_setting(_ADMIN_PASSWORD_HASH_KEY, _OTHER_HASH)
        await import_config(target, document)
        assert await target.get_setting(_ADMIN_PASSWORD_HASH_KEY) == _OTHER_HASH


async def test_import_refuses_a_document_that_carries_the_password_hash(
    tmp_path: Path,
) -> None:
    """Naming the hash is a problem, not a skip, and the whole import writes nothing.

    Silently dropping the row would let a hand-edited document look like it
    rotated the credential; refusing it keeps the current-password rule absolute.

    :param tmp_path: pytest temporary directory.
    """
    document: dict[str, object] = {
        "format": "chime-deck/config",
        "version": 1,
        "exported_at": "2026-01-01T00:00:00.000000Z",
        "settings": {_ADMIN_PASSWORD_HASH_KEY: "", "audio.volume": 33},
    }

    async with ConfigStore(tmp_path / "target.db") as target:
        await target.set_setting(_ADMIN_PASSWORD_HASH_KEY, _STORED_HASH)

        with pytest.raises(ImportValidationError) as error:
            await import_config(target, document)

        problems = "\n".join(error.value.problems)
        assert _ADMIN_PASSWORD_HASH_KEY in problems
        assert "set the password from the settings page" in problems
        assert _STORED_HASH not in problems
        assert await target.get_setting(_ADMIN_PASSWORD_HASH_KEY) == _STORED_HASH
        assert await target.get_setting("audio.volume") == 70


async def test_import_rejects_a_provider_key_that_is_not_a_string(tmp_path: Path) -> None:
    """The problem names the key and never the submitted value."""
    document: dict[str, object] = {
        "format": "chime-deck/config",
        "version": 1,
        "exported_at": "2026-01-01T00:00:00.000000Z",
        "settings": {_PROVIDER_SECRET_KEY: ["not", "a", "string"]},
    }

    async with ConfigStore(tmp_path / "target.db") as target:
        with pytest.raises(ImportValidationError) as error:
            await import_config(target, document)

        problems = "\n".join(error.value.problems)
        assert _PROVIDER_SECRET_KEY in problems
        assert "must be a string" in problems
        assert "not a string" not in problems
        assert await target.get_provider_secret(_PROVIDER_ID) == ""


async def test_import_refuses_an_over_long_provider_key(tmp_path: Path) -> None:
    """A key longer than the 4096-character cap is rejected by name, not by value."""
    too_long = "k" * 4097
    document: dict[str, object] = {
        "format": "chime-deck/config",
        "version": 1,
        "exported_at": "2026-01-01T00:00:00.000000Z",
        "settings": {_PROVIDER_SECRET_KEY: too_long},
    }

    async with ConfigStore(tmp_path / "target.db") as target:
        with pytest.raises(ImportValidationError) as error:
            await import_config(target, document)

        problems = "\n".join(error.value.problems)
        assert "must be at most 4096 characters" in problems
        assert too_long not in problems


async def test_state_tones_none_and_empty_stay_distinct_across_a_round_trip(
    source: ConfigStore, tmp_path: Path
) -> None:
    """``None`` (no overrides) and ``{}`` (a cleared mapping) both survive import."""
    await source.upsert_entity(EntityIn(entity_id="sensor.plain", state_tones=None))
    await source.upsert_entity(EntityIn(entity_id="sensor.cleared", state_tones={}))
    first = await export_config(source)

    async with ConfigStore(tmp_path / "target.db") as target:
        await import_config(target, first)
        second = await export_config(target)

    assert _entity_presentation(first) == {
        "sensor.plain": (None, True),
        "sensor.cleared": ({}, True),
    }
    assert _entity_presentation(second) == _entity_presentation(first)


async def test_version_1_document_without_the_new_fields_still_imports(
    tmp_path: Path,
) -> None:
    """An older export imports, landing as "no overrides, colour on"."""
    document: dict[str, object] = {
        "format": "chime-deck/config",
        "version": 1,
        "exported_at": "2026-01-01T00:00:00.000000Z",
        "entities": [{"entity_id": "sensor.older", "display_name": "Older"}],
    }

    async with ConfigStore(tmp_path / "target.db") as target:
        report = await import_config(target, document)

        assert report.entities == 1
        entities = await target.list_entities()
        assert [entity.entity_id for entity in entities] == ["sensor.older"]
        assert entities[0].state_tones is None
        assert entities[0].colour_by_state is True


async def test_import_rejects_a_tone_outside_the_vocabulary(tmp_path: Path) -> None:
    """A bad tone is named with its state and the accepted vocabulary."""
    document: dict[str, object] = {
        "format": "chime-deck/config",
        "version": 2,
        "exported_at": "2026-01-01T00:00:00.000000Z",
        "entities": [
            {"entity_id": "sensor.washer", "state_tones": {"running": "loud"}},
        ],
    }

    async with ConfigStore(tmp_path / "target.db") as target:
        with pytest.raises(ImportValidationError) as error:
            await import_config(target, document)

        problems = "\n".join(error.value.problems)
        assert (
            "entities[0].state_tones.running must be one of ok, warn, alert, off — got 'loud'"
            in problems
        )
        assert await target.list_entities() == []


async def test_import_rejects_a_future_document_version(tmp_path: Path) -> None:
    """A version this build does not know is refused instead of half-imported."""
    document: dict[str, object] = {
        "format": "chime-deck/config",
        "version": 3,
        "exported_at": "2026-01-01T00:00:00.000000Z",
    }

    async with ConfigStore(tmp_path / "target.db") as target:
        with pytest.raises(ImportValidationError) as error:
            await import_config(target, document)

        problems = "\n".join(error.value.problems)
        assert "version must be one of 1, 2" in problems
