"""Buttons CRUD API, save-time validation, reorder, events and sound deletion.

Every test drives the real application through ``httpx.ASGITransport`` against a
temporary database, so no socket, broker or sound card is touched:
``offline_wiring`` (autouse) makes every app built by ``create_app`` fully
offline, and the speech engine list is a fake injected through
``create_app(..., speech_engines=[...])``, so no real engine binary or socket is
ever probed. A static TTS save *does* bake now (HSD-dzek), against that fake.

Owning ticket: HSD-1drv.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state
from test_tts_speech import FakeEngine

from chime_deck.api.buttons import PressResponse
from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig
from chime_deck.mqtt import command_topic, discovery_topic, load_mqtt_settings
from chime_deck.press import PressOutcome, PressResult
from chime_deck.store import Button, ButtonIn, ConfigEvent, ConfigStore, Sound, SoundIn
from chime_deck.store.database import Database
from chime_deck.tts import NO_PAID_PROVIDERS

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_TEMPLATED = "The bell was pressed by {{ states('sensor.presser') }}"
"""A phrase with Jinja syntax, which D25 treats as rendered (and billed) per press."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test."""
    with preserve_log_state():
        yield


class _FakePaidLookup:
    """A paid lookup that matches exactly, used to prove the seam and its gap.

    Exact matching is deliberate: ``ElevenLabs`` is **not** ``elevenlabs``, which
    pins the exact-match fragility HSD-npt0/HSD-s856 have to normalise someday.
    """

    def __init__(self, paid: set[str]) -> None:
        """Record which engine names bill per character.

        :param paid: engine names that are paid.
        """
        self._paid = frozenset(paid)

    async def is_paid(self, engine: str) -> bool:
        """Return whether *engine* is one of the configured paid names.

        :param engine: configured engine name, exactly as stored.
        """
        return engine in self._paid


def _build_app(tmp_path: Path) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    The speech engine list is a fake, so a save-time bake never probes a real
    binary or opens a socket.

    :param tmp_path: pytest temporary directory used as the data directory.
    """
    config = ServerConfig(
        data_dir=tmp_path,
        static_dir=tmp_path / "no-dist",
        seed_demo_sounds=False,
    )
    app = create_app(config, speech_engines=[FakeEngine("ha_tts")])
    return app, cast(AppState, app.state.doorbell)


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield a client bound to it.

    :param app: application under test.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=_HTTP_TIMEOUT_S
        ) as client:
            yield client


def _drain(queue: asyncio.Queue[ConfigEvent]) -> list[ConfigEvent]:
    """Return and clear every queued configuration event.

    :param queue: queue subscribed to the store's event bus.
    """
    drained: list[ConfigEvent] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    return drained


def _error(response: httpx.Response) -> dict[str, object]:
    """Return the ``error`` object of a failed response.

    :param response: response with the standard error envelope.
    """
    return cast("dict[str, object]", response.json()["error"])


def _detail(response: httpx.Response) -> dict[str, object]:
    """Return the ``error.detail`` object of a failed response.

    :param response: response with the standard error envelope.
    """
    return cast("dict[str, object]", _error(response)["detail"])


def _body(response: httpx.Response) -> dict[str, object]:
    """Return a decoded JSON object body.

    :param response: successful response.
    """
    return cast("dict[str, object]", response.json())


def _rows(response: httpx.Response) -> list[dict[str, object]]:
    """Return a decoded JSON array of objects.

    :param response: successful list response.
    """
    return cast("list[dict[str, object]]", response.json())


def _affected(response: httpx.Response) -> list[dict[str, object]]:
    """Return ``affected_buttons`` from either a 409 or a delete response.

    :param response: sound-deletion response.
    """
    container = _detail(response) if response.status_code == 409 else _body(response)
    return cast("list[dict[str, object]]", container["affected_buttons"])


async def _new_sound(store: ConfigStore, name: str = "Ding", path: str = "ding.mp3") -> Sound:
    """Create one file sound to point buttons at.

    :param store: connected store.
    :param name: sound display name.
    :param path: path relative to the sounds directory.
    """
    return await store.create_sound(SoundIn(name=name, source_type="file", path=path))


async def _new_button(client: httpx.AsyncClient, name: str, sound_id: int) -> dict[str, object]:
    """Create one sound button through the API and return its body.

    :param client: client bound to the running app.
    :param name: button display name.
    :param sound_id: sound the button plays.
    """
    response = await client.post(
        "/api/buttons",
        json={"name": name, "action_type": "sound", "sound_id": sound_id},
    )
    assert response.status_code == 201, response.text
    return _body(response)


def _count_transactions(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count every ``Database.transaction()`` call from now on.

    The store is autocommit and ``connection.commit()`` is never called, so "one
    write" means exactly one outermost ``Database.transaction()``. The wrapper
    counts nested calls too, which is deliberately loud rather than quiet: a
    refactor that nests transactions would fail this assertion instead of
    silently counting one.

    :param monkeypatch: patcher that installs the counting wrapper.
    """
    calls: list[int] = []
    original = Database.transaction

    @asynccontextmanager
    async def counting_transaction(db: Database) -> AsyncIterator[aiosqlite.Connection]:
        calls.append(1)
        async with original(db) as connection:
            yield connection

    monkeypatch.setattr(Database, "transaction", counting_transaction)
    return calls


async def test_crud_round_trip(tmp_path: Path) -> None:
    """Create, list, patch and delete round-trip through the API.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        first = await _new_button(client, "Front Door", sound.id)
        assert first["slug"] == "front-door"
        assert first["sort_order"] == 10
        assert first["action_type"] == "sound"
        assert first["sound_id"] == sound.id

        second = await _new_button(client, "Back Door", sound.id)
        rows = _rows(await client.get("/api/buttons"))
        assert [row["id"] for row in rows] == [first["id"], second["id"]]
        assert [row["sort_order"] for row in rows] == [10, 20]

        patched = await client.patch(f"/api/buttons/{first['id']}", json={"name": "Front"})
        assert patched.status_code == 200
        assert _body(patched)["name"] == "Front"
        # A rename alone keeps the stored slug; the slug is its own editable field.
        assert _body(patched)["slug"] == "front-door"

        deleted = await client.delete(f"/api/buttons/{first['id']}")
        assert deleted.status_code == 200
        assert _body(deleted) == {"deleted": True, "id": first["id"], "slug": "front-door"}

        await client.delete(f"/api/buttons/{second['id']}")
        assert _rows(await client.get("/api/buttons")) == []


async def test_sound_button_without_a_sound_is_a_field_specific_422(tmp_path: Path) -> None:
    """A sound button without ``sound_id`` is rejected naming that field.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.post("/api/buttons", json={"name": "Clang", "action_type": "sound"})

    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"
    assert _detail(response)["field"] == "sound_id"
    assert "sound_id" in cast(str, _error(response)["message"])


async def test_a_deleted_sound_id_is_a_422_that_names_the_id(tmp_path: Path) -> None:
    """A ``sound_id`` whose row is gone is rejected before any write.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        await state.store.delete_sound(sound.id)
        response = await client.post(
            "/api/buttons",
            json={"name": "Clang", "action_type": "sound", "sound_id": sound.id},
        )
        assert _rows(await client.get("/api/buttons")) == []

    assert response.status_code == 422
    assert _detail(response)["field"] == "sound_id"
    assert _detail(response)["sound_id"] == sound.id
    assert str(sound.id) in cast(str, _error(response)["message"])


async def test_tts_save_is_rejected_while_speech_is_disabled(tmp_path: Path) -> None:
    """D27 is enforced at save time and stops applying once an engine is set.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        await state.store.set_setting("tts.default_engine", "none")

        rejected = await client.post(
            "/api/buttons",
            json={"name": "Speak", "action_type": "tts", "tts_text": "Hello"},
        )
        assert rejected.status_code == 422
        assert _error(rejected)["code"] == "invalid_setting"
        assert _detail(rejected)["field"] == "action_type"
        assert _detail(rejected)["setting"] == "tts.default_engine"
        message = cast(str, _error(rejected)["message"])
        assert "tts.default_engine" in message
        # The remedy is part of the contract: setting the engine, or a sound button.
        assert "Settings" in message
        assert "sound" in message
        assert _rows(await client.get("/api/buttons")) == []

        # A setting value only: no engine is constructed by this test.
        await state.store.set_setting("tts.default_engine", "espeak_ng")
        accepted = await client.post(
            "/api/buttons",
            json={"name": "Speak", "action_type": "tts", "tts_text": "Hello"},
        )
        assert accepted.status_code == 201

        sound_button = await _new_button(client, "Chime", sound.id)
        await state.store.set_setting("tts.default_engine", "none")

        # The D27 consequence, asserted so it reads as a decision: a sound button
        # is still editable, but re-validating an existing TTS row is refused.
        renamed = await client.patch(f"/api/buttons/{sound_button['id']}", json={"name": "Bell"})
        assert renamed.status_code == 200

        blocked = await client.patch(
            f"/api/buttons/{_body(accepted)['id']}", json={"name": "Talker"}
        )
        assert blocked.status_code == 422
        assert _error(blocked)["code"] == "invalid_setting"
        assert _detail(blocked)["field"] == "action_type"


async def test_paid_provider_requires_an_explicit_override(tmp_path: Path) -> None:
    """D25 blocks a templated phrase on a paid engine unless the request accepts it.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        state.paid_providers = _FakePaidLookup({"elevenlabs"})

        rejected = await client.post(
            "/api/buttons",
            json={
                "name": "Paid",
                "action_type": "tts",
                "tts_text": _TEMPLATED,
                "tts_engine": "elevenlabs",
            },
        )
        assert rejected.status_code == 422
        assert _error(rejected)["code"] == "validation_error"
        assert _detail(rejected)["field"] == "tts_text"
        assert _detail(rejected)["engine"] == "elevenlabs"
        assert _detail(rejected)["requires"] == "acknowledge_paid_template"
        assert "acknowledge_paid_template" in cast(str, _error(rejected)["message"])

        accepted = await client.post(
            "/api/buttons",
            json={
                "name": "Paid",
                "action_type": "tts",
                "tts_text": _TEMPLATED,
                "tts_engine": "elevenlabs",
                "acknowledge_paid_template": True,
            },
        )
        assert accepted.status_code == 201
        # Write-only: the override is never persisted and never returned.
        assert "acknowledge_paid_template" not in _body(accepted)

        # The exact-match gap HSD-npt0/HSD-s856 must normalise: the lookup is
        # case-sensitive, so a differently-cased engine is not treated as paid.
        near_miss = await client.post(
            "/api/buttons",
            json={
                "name": "Nearly Paid",
                "action_type": "tts",
                "tts_text": _TEMPLATED,
                "tts_engine": "ElevenLabs",
            },
        )
        assert near_miss.status_code == 201

        # The production default cannot mark anything paid, so D25 is dormant.
        state.paid_providers = NO_PAID_PROVIDERS
        dormant = await client.post(
            "/api/buttons",
            json={
                "name": "Dormant",
                "action_type": "tts",
                "tts_text": _TEMPLATED,
                "tts_engine": "elevenlabs",
            },
        )
        assert dormant.status_code == 201

        # D25 is scoped to TTS rows: a sound button keeps a templated phrase and
        # a paid engine (never spoken), so it must still save.
        state.paid_providers = _FakePaidLookup({"elevenlabs"})
        switched = await client.post(
            "/api/buttons",
            json={
                "name": "Switched",
                "action_type": "sound",
                "sound_id": sound.id,
                "tts_text": _TEMPLATED,
                "tts_engine": "elevenlabs",
            },
        )
        assert switched.status_code == 201


async def test_the_operator_can_allow_templated_paid_phrases(tmp_path: Path) -> None:
    """D25's operator half removes the per-save ack, and only for paid templates.

    ``tts.allow_dynamic_paid`` is read at save time, so flipping it in the store
    changes the very next save. A static phrase is unaffected either way, and a
    templated phrase on an unpaid engine stays unaffected too.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        state.paid_providers = _FakePaidLookup({"elevenlabs"})

        # The default is still the strict behaviour.
        assert await state.store.get_setting("tts.allow_dynamic_paid") is False
        strict = await client.post(
            "/api/buttons",
            json={
                "name": "Strict",
                "action_type": "tts",
                "tts_text": _TEMPLATED,
                "tts_engine": "elevenlabs",
            },
        )
        assert strict.status_code == 422
        assert _detail(strict)["requires"] == "acknowledge_paid_template"

        # The operator says yes: the same save now succeeds without the ack.
        await state.store.set_setting("tts.allow_dynamic_paid", True)
        allowed = await client.post(
            "/api/buttons",
            json={
                "name": "Allowed",
                "action_type": "tts",
                "tts_text": _TEMPLATED,
                "tts_engine": "elevenlabs",
            },
        )
        assert allowed.status_code == 201
        assert "acknowledge_paid_template" not in _body(allowed)

        # A static phrase never needed the ack, with the policy either way.
        static = await client.post(
            "/api/buttons",
            json={
                "name": "Static",
                "action_type": "tts",
                "tts_text": "The doorbell rang",
                "tts_engine": "elevenlabs",
            },
        )
        assert static.status_code == 201

        await state.store.set_setting("tts.allow_dynamic_paid", False)
        static_strict = await client.post(
            "/api/buttons",
            json={
                "name": "Static Strict",
                "action_type": "tts",
                "tts_text": "The doorbell rang again",
                "tts_engine": "elevenlabs",
            },
        )
        assert static_strict.status_code == 201


async def test_a_slug_change_reports_the_old_and_new_topics(tmp_path: Path) -> None:
    """A rename reports both topic sets; conflicts and bad names are rejected.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        created = _body(
            await client.post(
                "/api/buttons",
                json={"name": "Front Door", "action_type": "sound", "sound_id": sound.id},
            )
        )
        assert created["slug"] == "front-door"
        settings = await load_mqtt_settings(state.store)

        response = await client.patch(f"/api/buttons/{created['id']}", json={"slug": "side-door"})
        assert response.status_code == 200
        body = _body(response)
        assert body["slug"] == "side-door"
        change = cast("dict[str, object]", body["slug_change"])
        assert change["old_slug"] == "front-door"
        assert change["new_slug"] == "side-door"
        expected_old = {
            "command": command_topic(settings, "front-door"),
            "discovery": discovery_topic(settings, "front-door"),
        }
        expected_new = {
            "command": command_topic(settings, "side-door"),
            "discovery": discovery_topic(settings, "side-door"),
        }
        assert change["old_topics"] == expected_old
        assert change["new_topics"] == expected_new
        assert body["mqtt"] == expected_new
        warning = cast(str, change["warning"])
        assert "side-door" in warning
        assert "NEW entity" in warning

        conflict = await client.post(
            "/api/buttons",
            json={
                "name": "Clash",
                "slug": "side-door",
                "action_type": "sound",
                "sound_id": sound.id,
            },
        )
        assert conflict.status_code == 409
        assert _error(conflict)["code"] == "conflict"
        assert _detail(conflict)["field"] == "slug"
        assert _detail(conflict)["slug"] == "side-door"
        assert "already used" in cast(str, _error(conflict)["message"])

        nameless = await client.post(
            "/api/buttons",
            json={"name": "!!!", "action_type": "sound", "sound_id": sound.id},
        )
        assert nameless.status_code == 422
        assert _error(nameless)["code"] == "validation_error"
        assert _detail(nameless)["field"] == "name"
        assert "no usable slug" in cast(str, _error(nameless)["message"])


async def test_reorder_is_one_transaction_and_one_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reordering writes once, emits once, and refuses an unknown id untouched.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that counts ``Database.transaction()`` calls.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        first = (await _new_button(client, "One", sound.id))["id"]
        second = (await _new_button(client, "Two", sound.id))["id"]
        third = (await _new_button(client, "Three", sound.id))["id"]

        queue = state.store.events.subscribe_queue(maxsize=50)
        _drain(queue)
        calls = _count_transactions(monkeypatch)

        response = await client.post("/api/buttons/reorder", json={"ids": [third, first]})
        assert response.status_code == 200
        assert _body(response) == {"ids": [third, first]}
        # Exactly one outermost transaction: the store is autocommit, so this is
        # what "one write" means.
        assert calls == [1]

        rows = _rows(await client.get("/api/buttons"))
        assert [row["id"] for row in rows] == [third, first, second]
        assert [row["sort_order"] for row in rows] == [10, 20, 30]

        events = _drain(queue)
        assert [event.what for event in events] == ["buttons"]
        assert events[0].payload["ids"] == (third, first, second)

        missing = await client.post("/api/buttons/reorder", json={"ids": [second, 9999]})
        assert missing.status_code == 404
        assert _error(missing)["code"] == "not_found"
        assert _detail(missing)["id"] == 9999
        assert _rows(await client.get("/api/buttons")) == rows
        assert _drain(queue) == []


async def test_every_mutation_emits_exactly_one_button_event(tmp_path: Path) -> None:
    """Create, rename, delete and reorder each publish one button event.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        queue = state.store.events.subscribe_queue(maxsize=50)

        created = _body(
            await client.post(
                "/api/buttons",
                json={"name": "First Button", "action_type": "sound", "sound_id": sound.id},
            )
        )
        events = _drain(queue)
        assert [event.what for event in events] == ["buttons"]
        assert events[0].payload["ids"] == (created["id"],)
        assert events[0].payload["slugs"] == ("first-button",)

        other = _body(
            await client.post(
                "/api/buttons",
                json={"name": "Second Button", "action_type": "sound", "sound_id": sound.id},
            )
        )
        _drain(queue)

        renamed = await client.patch(
            f"/api/buttons/{created['id']}", json={"name": "Renamed", "slug": "renamed"}
        )
        assert renamed.status_code == 200
        events = _drain(queue)
        assert [event.what for event in events] == ["buttons"]
        assert events[0].payload["renamed"] == (("first-button", "renamed"),)
        assert events[0].payload["slugs"] == ("renamed",)

        deleted = await client.delete(f"/api/buttons/{other['id']}")
        assert deleted.status_code == 200
        events = _drain(queue)
        assert [event.what for event in events] == ["buttons"]
        assert events[0].payload["removed_ids"] == (other["id"],)
        assert events[0].payload["removed_slugs"] == ("second-button",)

        reorder = await client.post("/api/buttons/reorder", json={"ids": [created["id"]]})
        assert reorder.status_code == 200
        events = _drain(queue)
        assert [event.what for event in events] == ["buttons"]
        assert events[0].payload["ids"] == (created["id"],)


async def test_deleting_a_used_sound_needs_confirm_and_names_the_buttons(
    tmp_path: Path,
) -> None:
    """A used sound answers 409 until ``?confirm=true`` deletes it and its buttons.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        first = await _new_button(client, "First Chime", sound.id)
        second = await _new_button(client, "Second Chime", sound.id)
        queue = state.store.events.subscribe_queue(maxsize=50)
        _drain(queue)

        refused = await client.delete(f"/api/sounds/{sound.id}")
        assert refused.status_code == 409
        assert _error(refused)["code"] == "conflict"
        detail = _detail(refused)
        assert detail["requires"] == "confirm"
        assert detail["count"] == 2
        assert {entry["id"] for entry in _affected(refused)} == {first["id"], second["id"]}
        assert {entry["name"] for entry in _affected(refused)} == {"First Chime", "Second Chime"}
        assert "?confirm=true" in cast(str, _error(refused)["message"])
        # A refused delete changes nothing and announces nothing.
        assert await state.store.get_sound(sound.id) == sound
        assert _drain(queue) == []

        confirmed = await client.delete(f"/api/sounds/{sound.id}?confirm=true")
        assert confirmed.status_code == 200
        assert {entry["id"] for entry in _affected(confirmed)} == {first["id"], second["id"]}
        assert await state.store.list_sounds() == []
        assert await state.store.list_buttons() == []

        # A used-sound delete is two events, one per table: the one-event rule is
        # per mutation type, not per request.
        events = _drain(queue)
        assert [event.what for event in events] == ["buttons", "sounds"]
        assert set(cast(tuple[int, ...], events[0].payload["removed_ids"])) == {
            first["id"],
            second["id"],
        }
        assert events[1].payload["removed_ids"] == (sound.id,)


async def test_a_tts_button_that_kept_a_sound_id_is_deleted_by_a_forced_delete(
    tmp_path: Path,
) -> None:
    """The preserved stale ``sound_id`` consequence, pinned rather than papered over.

    The merge never drops a reference the user did not ask to drop, and the
    store's sound lookup filters on ``sound_id`` alone, so a TTS button that kept
    one is listed in the 409 and deleted by ``?confirm=true``.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store, name="Chime", path="chime.mp3")
        flipped = await _new_button(client, "Flipped", sound.id)

        response = await client.patch(
            f"/api/buttons/{flipped['id']}",
            json={"action_type": "tts", "tts_text": "Hello"},
        )
        assert response.status_code == 200
        body = _body(response)
        assert body["action_type"] == "tts"
        assert body["sound_id"] == sound.id

        refused = await client.delete(f"/api/sounds/{sound.id}")
        assert refused.status_code == 409
        assert [entry["id"] for entry in _affected(refused)] == [flipped["id"]]

        confirmed = await client.delete(f"/api/sounds/{sound.id}?confirm=true")
        assert confirmed.status_code == 200
        assert await state.store.list_buttons() == []


async def test_broken_mqtt_settings_never_fail_a_save(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A bad broker setting degrades the topics to null instead of a 500.

    :param tmp_path: pytest temporary directory.
    :param caplog: fixture that captures the warning the route logs.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        # Forced on the connection: ``set_setting`` would reject port 0, and a
        # plain UPDATE is a no-op until something has written the row. ``mqtt.host``
        # is left at its registry default so ``MqttSettings`` does not skip the
        # port check on a blank host.
        await state.store.connection.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('mqtt.port', '0', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("2026-01-01T00:00:00.000000Z",),
        )

        created = await client.post(
            "/api/buttons",
            json={"name": "Front Door", "action_type": "sound", "sound_id": sound.id},
        )
        assert created.status_code == 201
        assert _body(created)["mqtt"] is None

        patched = await client.patch(f"/api/buttons/{_body(created)['id']}", json={"slug": "front"})
        assert patched.status_code == 200
        body = _body(patched)
        assert body["mqtt"] is None
        change = cast("dict[str, object]", body["slug_change"])
        assert change["old_topics"] is None
        assert change["new_topics"] is None
        assert "front" in cast(str, change["warning"])
        # The degradation is reported, not silent.
        assert "button topics are unavailable" in caplog.text


async def test_press_still_works_and_reports_the_press_log_shape(tmp_path: Path) -> None:
    """The press route is unchanged by the CRUD surface around it.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        button = ButtonIn(name="Front Door", action_type="sound", sound_id=sound.id)
        stored = await state.store.upsert_button(button)

        response = await client.post(f"/api/buttons/{stored.id}/press")
        assert response.status_code == 200
        body = _body(response)
        assert body["button_id"] == stored.id
        assert body["source"] == "ui_test"

        missing = await client.post("/api/buttons/9999/press")
        assert missing.status_code == 404


# ------------------------------------------------- HSD-po1g: topic validation

_UNSAFE_SLUG_MESSAGE = "lowercase a-z, digits, hyphens and underscores, 1 to 128 characters"
"""The charset sentence a refused slug carries; the UI shows it verbatim."""


async def _legacy_unsafe_button(state: AppState, sound_id: int) -> Button:
    """Insert a row directly through the store with an unsafe slug and icon.

    The API would refuse this write, so only the store (deliberately permissive)
    can create the pre-HSD-po1g row the grandfather clauses have to keep usable.

    :param state: live application state providing the store.
    :param sound_id: sound the row plays.
    """
    return await state.store.upsert_button(
        ButtonIn(
            name="Front Door",
            action_type="sound",
            sound_id=sound_id,
            slug="front.door",
            icon="Front Door",
        )
    )


@pytest.mark.parametrize("slug", ["Front Door", "Front.Door", "front door!", "x" * 129])
async def test_create_refuses_an_unsafe_slug_naming_the_charset_rule(
    tmp_path: Path, slug: str
) -> None:
    """A submitted slug MQTT cannot carry is a 422 naming the rule, writing nothing.

    :param tmp_path: pytest temporary directory.
    :param slug: submitted slug under test.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        queue = state.store.events.subscribe_queue(maxsize=10)
        response = await client.post(
            "/api/buttons",
            json={
                "name": "Front Door",
                "slug": slug,
                "action_type": "sound",
                "sound_id": sound.id,
            },
        )
        assert _rows(await client.get("/api/buttons")) == []
        assert _drain(queue) == []

    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"
    assert _detail(response)["field"] == "slug"
    assert _detail(response)["slug"] == slug
    assert _UNSAFE_SLUG_MESSAGE in cast(str, _error(response)["message"])


async def test_create_refuses_a_name_whose_derived_slug_exceeds_128_characters(
    tmp_path: Path,
) -> None:
    """A derived slug's only possible failure is the length bound, and it says so.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        response = await client.post(
            "/api/buttons",
            json={
                "name": "A" * 200,
                "slug": None,
                "action_type": "sound",
                "sound_id": sound.id,
            },
        )
        assert _rows(await client.get("/api/buttons")) == []

    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"
    assert _detail(response)["field"] == "name"
    message = cast(str, _error(response)["message"])
    assert "longer than 128 characters" in message
    assert "set an explicit slug" in message


@pytest.mark.parametrize(
    "icon", ["Front Door", "mdi: front door", "mdi:", "::", " doorbell", "custom:cat.svg"]
)
async def test_create_refuses_an_icon_that_is_not_an_mdi_name(tmp_path: Path, icon: str) -> None:
    """An icon Home Assistant would reject is refused, naming the value and the fix.

    :param tmp_path: pytest temporary directory.
    :param icon: unusable icon under test.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        response = await client.post(
            "/api/buttons",
            json={"name": "Front Door", "icon": icon, "action_type": "sound", "sound_id": sound.id},
        )
        assert _rows(await client.get("/api/buttons")) == []

    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"
    assert _detail(response)["field"] == "icon"
    assert _detail(response)["icon"] == icon
    message = cast(str, _error(response)["message"])
    assert repr(icon) in message
    assert "mdi:<name>" in message


@pytest.mark.parametrize("icon", ["mdi:doorbell", None, ""])
async def test_create_accepts_a_safe_slug_and_a_blank_or_mdi_icon(
    tmp_path: Path, icon: str | None
) -> None:
    """A safe slug and an absent or `mdi:` icon are all accepted with a 201.

    :param tmp_path: pytest temporary directory.
    :param icon: accepted icon under test.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        response = await client.post(
            "/api/buttons",
            json={
                "name": "Front Door",
                "slug": "front-door",
                "icon": icon,
                "action_type": "sound",
                "sound_id": sound.id,
            },
        )

    assert response.status_code == 201, response.text
    assert _body(response)["slug"] == "front-door"
    assert _body(response)["icon"] == icon


async def test_patch_to_an_unsafe_slug_is_refused_and_changes_nothing(
    tmp_path: Path,
) -> None:
    """Changing a slug to one MQTT cannot carry is refused, leaving the row and events alone.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        created = await _new_button(client, "Front Door", sound.id)
        queue = state.store.events.subscribe_queue(maxsize=10)
        response = await client.patch(f"/api/buttons/{created['id']}", json={"slug": "front.door"})
        rows = _rows(await client.get("/api/buttons"))
        assert _drain(queue) == []

    assert response.status_code == 422
    assert _error(response)["code"] == "validation_error"
    assert _detail(response)["field"] == "slug"
    assert _UNSAFE_SLUG_MESSAGE in cast(str, _error(response)["message"])
    assert [row["slug"] for row in rows] == ["front-door"]


async def test_a_legacy_unsafe_row_stays_editable_when_its_name_changes(
    tmp_path: Path,
) -> None:
    """A stored unsafe slug and icon are grandfathered: an unrelated edit still saves.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        created = await _legacy_unsafe_button(state, sound.id)
        response = await client.patch(f"/api/buttons/{created.id}", json={"name": "Back Door"})

    assert response.status_code == 200, response.text
    body = _body(response)
    assert body["name"] == "Back Door"
    assert body["slug"] == "front.door"
    assert body["icon"] == "Front Door"


async def test_a_legacy_unsafe_row_is_still_listed_with_its_topics(
    tmp_path: Path,
) -> None:
    """The list route still serialises a legacy row, with topics built from its own slug.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        await _legacy_unsafe_button(state, sound.id)
        response = await client.get("/api/buttons")
        settings = await load_mqtt_settings(state.store)

    assert response.status_code == 200
    (row,) = _rows(response)
    assert row["slug"] == "front.door"
    assert row["icon"] == "Front Door"
    assert row["mqtt"] == {
        "command": command_topic(settings, "front.door"),
        "discovery": discovery_topic(settings, "front.door"),
    }


async def test_patch_resending_the_stored_unsafe_slug_is_accepted(
    tmp_path: Path,
) -> None:
    """The grandfather clause is by value, not by presence: the same slug passes.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        created = await _legacy_unsafe_button(state, sound.id)
        response = await client.patch(f"/api/buttons/{created.id}", json={"slug": "front.door"})

    assert response.status_code == 200, response.text
    body = _body(response)
    assert body["slug"] == "front.door"
    assert body["icon"] == "Front Door"
    assert body["slug_change"] is None


async def test_a_press_with_no_synthesis_reports_a_null_synthesis_ms(tmp_path: Path) -> None:
    """The press response always carries ``synthesis_ms``, and ``null`` when nothing was made.

    A sound button synthesises nothing, so the field must be present but ``null``
    — never ``0`` and never missing — and a result that does carry a measured
    time must survive :meth:`PressResponse.from_result` verbatim.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        button = ButtonIn(name="Front Door", action_type="sound", sound_id=sound.id)
        stored = await state.store.upsert_button(button)

        response = await client.post(f"/api/buttons/{stored.id}/press")
        assert response.status_code == 200
        body = _body(response)
        assert "synthesis_ms" in body
        assert body["synthesis_ms"] is None

    measured = PressResponse.from_result(
        PressResult(
            button_id=1,
            button_name="Front Door",
            source="mqtt",
            outcome=PressOutcome.QUEUED,
            pressed_at="2026-01-01T12:00:00.000000Z",
            synthesis_ms=240,
        )
    )
    assert measured.synthesis_ms == 240
