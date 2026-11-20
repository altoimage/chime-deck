"""Bake API: save-time pre-rendering, the manual bake route and the bake reports.

Every test drives the real application through ``httpx.ASGITransport`` against a
temporary database, with the speech engine list injected as fakes through
``create_app(..., speech_engines=[...])``. No network, no synthesis, no engine
binary: a fake engine writes a small RIFF payload, which is a real ``wav``
container as far as :func:`~chime_deck.tts.engines.sniff_container` is
concerned.

Owning ticket: HSD-dzek.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from test_tts_speech import FakeEngine

from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig
from chime_deck.store import ButtonIn, ConfigEvent, ConfigStore, Sound, SoundIn
from chime_deck.tts import TtsOptions, TtsRenderError, baked_root

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_STATIC = "The front door is open"
"""A phrase with no Jinja syntax, so it can be baked exactly once (D21)."""

_TEMPLATED = "The bell was pressed by {{ states('sensor.presser') }}"
"""A phrase that is rendered on every press and is therefore never baked."""


class FailingEngine(FakeEngine):
    """A fake engine whose render always fails the way a broken engine does."""

    async def render(self, text: str, options: TtsOptions, *, destination: Path) -> Path:
        """Record the attempt, then fail.

        :param text: phrase that was to be spoken.
        :param options: options for this render.
        :param destination: exact output path the render must have written.
        :raises TtsRenderError: always.
        """
        self.renders.append((text, options, destination))
        raise TtsRenderError("engine offline")


def _build_app(tmp_path: Path, engines: list[FakeEngine]) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and the injected engines.

    :param tmp_path: pytest temporary directory used as the data directory.
    :param engines: the fake speech engine list the lifespan must use.
    """
    config = ServerConfig(
        data_dir=tmp_path,
        static_dir=tmp_path / "no-dist",
        seed_demo_sounds=False,
    )
    app = create_app(config, speech_engines=list(engines))
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


def _body(response: httpx.Response) -> dict[str, object]:
    """Return a decoded JSON object body.

    :param response: successful response.
    """
    return cast("dict[str, object]", response.json())


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


def _drain(queue: asyncio.Queue[ConfigEvent]) -> list[ConfigEvent]:
    """Return and clear every queued configuration event.

    :param queue: queue subscribed to the store's event bus.
    """
    drained: list[ConfigEvent] = []
    while not queue.empty():
        drained.append(queue.get_nowait())
    return drained


async def _new_sound(store: ConfigStore) -> Sound:
    """Create one file sound so a sound button can point at it.

    :param store: connected store.
    """
    return await store.create_sound(SoundIn(name="Ding", source_type="file", path="ding.mp3"))


async def _static_tts(client: httpx.AsyncClient, name: str = "Speak") -> dict[str, object]:
    """Save one static TTS button through the API and return its body.

    :param client: client bound to the running app.
    :param name: button display name.
    """
    response = await client.post(
        "/api/buttons", json={"name": name, "action_type": "tts", "tts_text": _STATIC}
    )
    assert response.status_code == 201
    return _body(response)


async def _seed(
    store: ConfigStore,
    name: str,
    *,
    text: str = _STATIC,
    engine: str | None = None,
) -> int:
    """Write one TTS button straight into the store, bypassing the save-time bake.

    :param store: connected store.
    :param name: button display name.
    :param text: phrase the button speaks.
    :param engine: per-button engine name, or ``None`` for the default.
    """
    button = await store.upsert_button(
        ButtonIn(name=name, action_type="tts", tts_text=text, tts_engine=engine)
    )
    return button.id


async def test_the_list_carries_the_bake_state(tmp_path: Path) -> None:
    """Every button row reports its bake state, ``None`` for a sound button.

    :param tmp_path: pytest temporary directory.
    """
    engine = FakeEngine("ha_tts")
    app, state = _build_app(tmp_path, [engine])

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        await client.post(
            "/api/buttons", json={"name": "Chime", "action_type": "sound", "sound_id": sound.id}
        )
        rows = cast("list[dict[str, object]]", (await client.get("/api/buttons")).json())

    assert len(rows) == 1
    assert rows[0]["baked_sha"] is None
    assert rows[0]["baked_format"] is None
    assert rows[0]["baked_at"] is None
    assert rows[0]["baked_error"] is None


async def test_a_static_tts_save_bakes_and_answers_with_the_artefact(tmp_path: Path) -> None:
    """A first save pre-renders the phrase and shows it in the 201 body (D21).

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, [FakeEngine("ha_tts")])

    async with _running(app) as client:
        body = await _static_tts(client)

    sha = body["baked_sha"]
    assert isinstance(sha, str)
    assert body["baked_format"] == "wav"
    assert body["baked_at"] is not None
    assert body["baked_error"] is None

    directory = baked_root(tmp_path) / str(body["id"])
    artefact = directory / f"{sha}.wav"
    assert artefact.is_file()
    sidecar = json.loads((directory / f"{sha}.json").read_text(encoding="utf-8"))
    assert sidecar["format"] == "wav"
    assert sidecar["text"] == _STATIC


async def test_a_failed_bake_still_saves_and_reports_the_reason(tmp_path: Path) -> None:
    """A bake failure never rejects the save; the row carries the reason instead.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, [FailingEngine("ha_tts")])

    async with _running(app) as client:
        body = await _static_tts(client)

    assert body["baked_sha"] is None
    assert body["baked_format"] is None
    assert body["baked_at"] is None
    error = body["baked_error"]
    assert isinstance(error, str)
    assert "engine offline" in error


async def test_a_templated_phrase_is_never_baked_on_save(tmp_path: Path) -> None:
    """A dynamic phrase is rendered per press, so no bake directory appears.

    :param tmp_path: pytest temporary directory.
    """
    engine = FakeEngine("ha_tts")
    app, _state = _build_app(tmp_path, [engine])

    async with _running(app) as client:
        response = await client.post(
            "/api/buttons", json={"name": "Talk", "action_type": "tts", "tts_text": _TEMPLATED}
        )

    assert response.status_code == 201
    body = _body(response)
    assert body["baked_sha"] is None
    assert body["baked_format"] is None
    assert not baked_root(tmp_path).exists()
    assert engine.renders == []


async def test_a_static_save_emits_two_button_events_a_sound_save_one(tmp_path: Path) -> None:
    """The bake state is a second write in its own transaction (D21).

    The artefact cannot exist before the row does — its directory is named by the
    button id — so the bake state cannot be folded into the save's transaction.
    A sound button has nothing to bake and still emits exactly one event.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path, [FakeEngine("ha_tts")])

    async with _running(app) as client:
        sound = await _new_sound(state.store)
        queue = state.store.events.subscribe_queue(maxsize=50)
        body = await _static_tts(client)
        events = _drain(queue)

        assert [event.what for event in events] == ["buttons", "buttons"]
        assert events[0].payload["ids"] == (body["id"],)
        assert events[1].payload["ids"] == (body["id"],)

        await client.post(
            "/api/buttons", json={"name": "Chime", "action_type": "sound", "sound_id": sound.id}
        )
        events = _drain(queue)

    assert [event.what for event in events] == ["buttons"]


async def test_patching_the_phrase_rebakes_and_patching_anything_else_does_not(
    tmp_path: Path,
) -> None:
    """A changed phrase replaces the artefact; an unrelated edit keeps it.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, [FakeEngine("ha_tts")])

    async with _running(app) as client:
        created = await _static_tts(client)
        old_sha = cast(str, created["baked_sha"])
        directory = baked_root(tmp_path) / str(created["id"])
        old_artefact = directory / f"{old_sha}.wav"
        assert old_artefact.is_file()

        icon_only = await client.patch(f"/api/buttons/{created['id']}", json={"icon": "mdi:bell"})
        assert icon_only.status_code == 200
        unchanged = _body(icon_only)
        assert unchanged["baked_sha"] == old_sha
        assert unchanged["baked_format"] == "wav"
        assert unchanged["baked_at"] == created["baked_at"]
        assert old_artefact.is_file()

        patched = await client.patch(
            f"/api/buttons/{created['id']}", json={"tts_text": "The side door is open"}
        )
        assert patched.status_code == 200
        body = _body(patched)

    new_sha = body["baked_sha"]
    assert isinstance(new_sha, str)
    assert new_sha != old_sha
    assert body["baked_format"] == "wav"
    assert not old_artefact.exists()
    assert (directory / f"{new_sha}.wav").is_file()


async def test_the_manual_bake_route_renders_a_phrase_whose_artefact_vanished(
    tmp_path: Path,
) -> None:
    """A hand-deleted artefact is re-rendered on demand, and the row follows.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, [FakeEngine("ha_tts")])

    async with _running(app) as client:
        created = await _static_tts(client)
        sha = cast(str, created["baked_sha"])
        directory = baked_root(tmp_path) / str(created["id"])
        (directory / f"{sha}.wav").unlink()
        (directory / f"{sha}.json").unlink()

        response = await client.post(f"/api/buttons/{created['id']}/bake")

    assert response.status_code == 200
    body = _body(response)
    assert body["baked_sha"] == sha
    assert body["baked_error"] is None
    assert (directory / f"{sha}.wav").is_file()


async def test_the_manual_bake_route_refuses_what_it_cannot_bake(tmp_path: Path) -> None:
    """A template, a sound button and an unknown id each get their own rejection.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path, [FakeEngine("ha_tts")])

    async with _running(app) as client:
        templated = _body(
            await client.post(
                "/api/buttons", json={"name": "Talk", "action_type": "tts", "tts_text": _TEMPLATED}
            )
        )
        dynamic = await client.post(f"/api/buttons/{templated['id']}/bake")
        assert dynamic.status_code == 422
        assert _error(dynamic)["code"] == "validation_error"
        assert _detail(dynamic) == {"field": "tts_text", "reason": "dynamic_phrase"}

        sound = await _new_sound(state.store)
        sound_button = _body(
            await client.post(
                "/api/buttons",
                json={"name": "Chime", "action_type": "sound", "sound_id": sound.id},
            )
        )
        not_tts = await client.post(f"/api/buttons/{sound_button['id']}/bake")
        assert not_tts.status_code == 422
        assert _error(not_tts)["code"] == "validation_error"
        assert _detail(not_tts) == {"field": "action_type"}

        missing = await client.post("/api/buttons/9999/bake")
        assert missing.status_code == 404
        assert _error(missing)["code"] == "not_found"


async def test_the_manual_bake_route_refuses_while_speech_is_off_without_probing(
    tmp_path: Path,
) -> None:
    """The deliberate off state (D27) is a 422 and never reaches an engine.

    The app is built while speech is enabled and the setting is switched off
    afterwards, so the source is live and the *route* is what refuses. The engine
    must never be probed: ``available_calls`` stays at zero.

    :param tmp_path: pytest temporary directory.
    """
    engine = FakeEngine("ha_tts")
    app, state = _build_app(tmp_path, [engine])

    async with _running(app) as client:
        # Seeded straight into the store so no save-time bake probes the engine.
        button_id = await _seed(state.store, "Speak")
        await state.store.set_setting("tts.default_engine", "none")

        response = await client.post(f"/api/buttons/{button_id}/bake")

    assert response.status_code == 422
    assert _error(response)["code"] == "invalid_setting"
    detail = _detail(response)
    assert detail["field"] == "tts.default_engine"
    assert detail["setting"] == "tts.default_engine"
    assert "tts.default_engine" in cast(str, _error(response)["message"])
    assert engine.available_calls == 0


async def test_bake_all_reports_every_button_and_never_fails_the_batch(
    tmp_path: Path,
) -> None:
    """One batch bakes, skips and fails per button, then reports what is current.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path, [FakeEngine("ha_tts"), FailingEngine("failing")])

    async with _running(app) as client:
        static = await _seed(state.store, "Static")
        dynamic = await _seed(state.store, "Dynamic", text=_TEMPLATED)
        broken = await _seed(state.store, "Broken", engine="failing")

        first = await client.post("/api/tts/bake_all")
        assert first.status_code == 200
        body = _body(first)
        assert body["baked"] == [static]
        assert body["skipped_dynamic"] == [dynamic]
        assert body["skipped_current"] == []
        failures = cast("list[dict[str, object]]", body["failed"])
        assert [failure["button"] for failure in failures] == [broken]
        assert "engine offline" in cast(str, failures[0]["error"])

        stored = await state.store.get_button_by_id(broken)
        assert stored is not None
        assert stored.baked_error is not None
        assert stored.baked_sha is None

        second = await client.post("/api/tts/bake_all")
        assert second.status_code == 200
        repeated = _body(second)
        assert repeated["baked"] == []
        assert repeated["skipped_current"] == [static]
        assert repeated["skipped_dynamic"] == [dynamic]


async def test_bake_all_refuses_while_speech_is_off(tmp_path: Path) -> None:
    """The batch answers the explicit D27 sentence instead of a fallback chain.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path, [FakeEngine("ha_tts")])

    async with _running(app) as client:
        await state.store.set_setting("tts.default_engine", "none")
        response = await client.post("/api/tts/bake_all")

    assert response.status_code == 422
    assert _error(response)["code"] == "invalid_setting"
    assert "tts.default_engine" in cast(str, _error(response)["message"])


async def test_bake_usage_measures_the_real_directories(tmp_path: Path) -> None:
    """The report matches the artefact and its sidecar on disk, and the empty cache.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, [FakeEngine("ha_tts")])

    async with _running(app) as client:
        created = await _static_tts(client)
        response = await client.get("/api/tts/bake_usage")

    assert response.status_code == 200
    body = _body(response)
    directory = baked_root(tmp_path) / str(created["id"])
    files = [path for path in directory.iterdir() if path.is_file()]
    baked = cast("dict[str, int]", body["baked"])
    assert baked == {"files": len(files), "bytes": sum(path.stat().st_size for path in files)}
    assert baked["files"] == 2
    assert cast("dict[str, int]", body["cache"]) == {"files": 0, "bytes": 0}


async def test_bake_usage_answers_zeroes_with_no_data_and_no_resolver(tmp_path: Path) -> None:
    """Without a lifespan there is no resolver, and the routes still answer ``200``.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path, [FakeEngine("ha_tts")])
    assert state.speech is None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=_HTTP_TIMEOUT_S
    ) as client:
        response = await client.get("/api/tts/bake_usage")

    assert response.status_code == 200
    assert _body(response) == {
        "baked": {"files": 0, "bytes": 0},
        "cache": {"files": 0, "bytes": 0},
    }


async def test_deleting_a_button_removes_its_bake_directory(tmp_path: Path) -> None:
    """The row and its artefacts go together, so no orphan is left for the sweep.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path, [FakeEngine("ha_tts")])

    async with _running(app) as client:
        created = await _static_tts(client)
        directory = baked_root(tmp_path) / str(created["id"])
        assert directory.is_dir()

        deleted = await client.delete(f"/api/buttons/{created['id']}")

    assert deleted.status_code == 200
    assert not directory.exists()
