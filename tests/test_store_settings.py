"""Settings defaults, type validation and secret redaction."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio

from chime_deck.store import (
    SETTINGS,
    ButtonIn,
    ConfigStore,
    SettingTypeError,
    SettingValueError,
    UnknownSettingKey,
)

pytestmark = pytest.mark.asyncio

_SECRET = "super-secret-token-value"
_PROVIDER_ID = "0123456789ab"
"""A synthetic provider id: 12 lower-case hex characters, as D25 defines them."""


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


async def test_defaults_are_returned(store: ConfigStore) -> None:
    """Unset keys return their registry defaults with the right type."""
    assert await store.get_setting("audio.volume") == 100
    assert await store.get_setting("audio.wake_lead_in_ms") == 1500
    assert await store.get_setting("mqtt.tls") is False
    assert await store.get_setting("ui.hide_cursor") is True
    assert await store.get_setting("audio.self_test_on_start") is False
    assert await store.get_setting("ha.base_url") == "http://homeassistant.local:8123"

    settings = await store.all_settings()
    assert settings["audio.volume"] == 100
    assert settings["buttons.default_dedupe_seconds"] == 3


async def test_override_round_trips_with_type(store: ConfigStore) -> None:
    """Stored values come back as the same Python type that was written."""
    await store.set_setting("audio.volume", 33)
    await store.set_setting("mqtt.tls", True)
    await store.set_setting("ui.theme", "light")

    assert await store.get_setting("audio.volume") == 33
    assert await store.get_setting("mqtt.tls") is True
    assert await store.get_setting("ui.theme") == "light"


async def test_wrong_value_type_is_rejected(store: ConfigStore) -> None:
    """Integer and boolean settings reject values of the wrong Python type."""
    with pytest.raises(SettingTypeError):
        await store.set_setting("audio.volume", "loud")
    with pytest.raises(SettingTypeError):
        await store.set_setting("mqtt.tls", 1)
    with pytest.raises(SettingTypeError):
        await store.set_setting("audio.volume", True)
    with pytest.raises(SettingTypeError):
        await store.set_setting("ui.theme", 3)


async def test_unknown_keys_are_rejected(store: ConfigStore) -> None:
    """Every settings entry point rejects an unregistered key."""
    with pytest.raises(UnknownSettingKey):
        await store.get_setting("nope.nothing")
    with pytest.raises(UnknownSettingKey):
        await store.set_setting("nope.nothing", "value")
    with pytest.raises(UnknownSettingKey):
        await store.has_secret("nope.nothing")


async def test_secrets_are_absent_when_redacted(store: ConfigStore) -> None:
    """Redacted settings omit secret keys entirely and never leak the value."""
    await store.set_setting("ha.token", _SECRET)
    await store.set_setting("mqtt.password", "hunter2")

    redacted = await store.all_settings(redact_secrets=True)
    assert "ha.token" not in redacted
    assert "mqtt.password" not in redacted
    assert _SECRET not in json.dumps(redacted)
    assert "hunter2" not in json.dumps(redacted)

    unredacted = await store.all_settings(redact_secrets=False)
    assert unredacted["ha.token"] == _SECRET
    assert unredacted["mqtt.password"] == "hunter2"
    assert await store.get_setting("ha.token") == _SECRET


async def test_secret_status_and_has_secret(store: ConfigStore) -> None:
    """Secret status tracks whether a non-empty value is stored.

    Every registered secret is tracked, including the two D24 renderer tokens and
    the HSD-0jke shared token, so the status map really grows with the registry
    rather than listing two keys.
    """
    every_secret_off = {
        "ha.token": False,
        "mqtt.password": False,
        "server.admin_password_hash": False,
        "server.token": False,
        "tts.remote_tts.token": False,
        "tts.renderer.token": False,
    }
    assert await store.has_secret("ha.token") is False
    assert await store.secret_status() == every_secret_off

    await store.set_setting("ha.token", _SECRET)
    await store.set_setting("tts.remote_tts.token", "remote-secret")
    assert await store.has_secret("ha.token") is True
    assert await store.has_secret("tts.remote_tts.token") is True
    assert await store.secret_status() == {
        "ha.token": True,
        "mqtt.password": False,
        "server.admin_password_hash": False,
        "server.token": False,
        "tts.remote_tts.token": True,
        "tts.renderer.token": False,
    }

    await store.set_setting("ha.token", "")
    assert await store.has_secret("ha.token") is False
    assert await store.secret_status() == {
        "ha.token": False,
        "mqtt.password": False,
        "server.admin_password_hash": False,
        "server.token": False,
        "tts.remote_tts.token": True,
        "tts.renderer.token": False,
    }

    await store.set_setting("tts.remote_tts.token", "")
    assert await store.has_secret("tts.remote_tts.token") is False
    assert await store.secret_status() == every_secret_off


async def test_unknown_stored_keys_round_trip(store: ConfigStore) -> None:
    """Rows written by another version still appear in ``all_settings``."""
    async with store.connection.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        ("future.key", json.dumps({"nested": [1, 2]}), "2026-01-01T00:00:00.000000Z"),
    ) as cursor:
        await cursor.close()

    settings = await store.all_settings()
    assert settings["future.key"] == {"nested": [1, 2]}


async def test_provider_secret_round_trips(store: ConfigStore) -> None:
    """A provider key is written, read, asked about and deleted through its own methods."""
    assert await store.get_provider_secret(_PROVIDER_ID) == ""
    assert await store.has_provider_secret(_PROVIDER_ID) is False

    await store.set_provider_secret(_PROVIDER_ID, "sk-live-123")

    assert await store.get_provider_secret(_PROVIDER_ID) == "sk-live-123"
    assert await store.has_provider_secret(_PROVIDER_ID) is True

    await store.delete_provider_secret(_PROVIDER_ID)

    assert await store.get_provider_secret(_PROVIDER_ID) == ""
    assert await store.has_provider_secret(_PROVIDER_ID) is False
    # Deleting an absent key is not an error.
    await store.delete_provider_secret(_PROVIDER_ID)


@pytest.mark.parametrize(
    "method",
    ["set_provider_secret", "get_provider_secret", "has_provider_secret", "delete_provider_secret"],
)
async def test_every_provider_secret_method_rejects_a_malformed_id(
    store: ConfigStore, method: str
) -> None:
    """All four refuse an id that is not 12 lower-case hex characters.

    :param store: connected store backed by a temporary database.
    :param method: provider-secret method under test.
    """
    method_under_test = getattr(store, method)
    arguments: tuple[object, ...] = ("ABCDEFABCDEF", "some-key")
    if method != "set_provider_secret":
        arguments = arguments[:1]

    with pytest.raises(SettingValueError) as exc_info:
        await method_under_test(*arguments)

    assert exc_info.value.key == "tts.providers.ABCDEFABCDEF.api_key"
    assert exc_info.value.reason == "must be a 12-character hexadecimal provider id"


@pytest.mark.parametrize("api_key", ["k" * 4097, 123])
async def test_set_provider_secret_rejects_a_bad_value_without_echoing_it(
    store: ConfigStore, api_key: str
) -> None:
    """An over-long or non-string key is refused, and the message never repeats it.

    The ``123`` case reaches the store as a non-string at runtime even though the
    annotation says ``str``: the point is that the store refuses it, not that a
    type checker could have caught it.

    :param store: connected store backed by a temporary database.
    :param api_key: rejected value of the wrong shape or length.
    """
    with pytest.raises(SettingValueError) as exc_info:
        await store.set_provider_secret(_PROVIDER_ID, api_key)

    message = str(exc_info.value)
    assert "tts.providers.0123456789ab.api_key" in message
    assert isinstance(api_key, str) is False or api_key not in message
    assert await store.get_provider_secret(_PROVIDER_ID) == ""


async def test_all_settings_hides_provider_keys_and_copies_the_default(
    store: ConfigStore,
) -> None:
    """A key row never reaches ``all_settings`` and a caller cannot mutate the default."""
    key = f"tts.providers.{_PROVIDER_ID}.api_key"
    await store.set_provider_secret(_PROVIDER_ID, "sk-live-123")

    redacted = await store.all_settings()
    assert key not in redacted
    assert "sk-live-123" not in json.dumps(redacted)
    assert redacted["tts.providers"] == []
    assert (await store.all_settings(redact_secrets=False))[key] == "sk-live-123"

    cast("list[object]", redacted["tts.providers"]).append({"id": _PROVIDER_ID})

    second = await store.all_settings()
    assert second["tts.providers"] == []
    assert SETTINGS["tts.providers"].default == []


async def test_all_secret_values_covers_static_and_provider_secrets(store: ConfigStore) -> None:
    """Every shape of stored secret is scrubbed, and a cleared one drops out."""
    await store.set_setting("ha.token", _SECRET)
    await store.set_setting("mqtt.password", "")
    await store.set_provider_secret(_PROVIDER_ID, "sk-live-123")

    assert sorted(await store.all_secret_values()) == sorted([_SECRET, "sk-live-123"])

    await store.set_provider_secret(_PROVIDER_ID, "")
    await store.set_setting("ha.token", "")

    assert await store.all_secret_values() == []


async def test_setting_a_provider_secret_does_not_clear_a_bake(store: ConfigStore) -> None:
    """Rotating a key changes credentials, not rendered bytes."""
    button = await store.upsert_button(
        ButtonIn(name="Hello", action_type="tts", tts_text="Hello there")
    )
    await store.set_bake_state(button.id, "baked-sha")

    await store.set_provider_secret(_PROVIDER_ID, "sk-live-123")

    stored = await store.get_button_by_id(button.id)
    assert stored is not None
    assert stored.baked_sha == "baked-sha"
