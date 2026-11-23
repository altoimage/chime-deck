"""Configuration transfer HTTP API: export, import, dry run and its side effects.

Every test drives the real application through ``httpx.ASGITransport`` with an
isolated data directory, so nothing is downloaded and nothing outside
``tmp_path`` is touched. The two side effects an import owns are pinned here: the
sound library rescan (monkeypatched, so no file is probed) and the secret
scrubber refresh (observed through :data:`SECRET_SCRUBBER`).

Owning ticket: HSD-l22f.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state

from chime_deck.app import AppState, create_app
from chime_deck.config import REDACTED, SECRET_SCRUBBER, ServerConfig
from chime_deck.sounds import ScanReport
from chime_deck.store import Button, ConfigStore, Entity, Sound

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_SECRET = "super-secret-token-value"
"""Secret value an imported document carries; long enough to be scrubbed."""

_SETTINGS: dict[str, object] = {"audio.volume": 20}
"""Settings the import document writes."""

_ENTITY: dict[str, object] = {"entity_id": "sensor.imported"}
"""Entity the import document adds."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger, its handlers and the registered secrets."""
    with preserve_log_state():
        yield


def _build_app(tmp_path: Path) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory used as the data directory.
    """
    config = ServerConfig(
        data_dir=tmp_path,
        static_dir=tmp_path / "no-dist",
        seed_demo_sounds=False,
    )
    app = create_app(config)
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
    return cast(dict[str, object], response.json())


def _error(response: httpx.Response) -> dict[str, object]:
    """Return the ``error`` object of a failed response.

    :param response: response with the standard error envelope.
    """
    return cast("dict[str, object]", response.json()["error"])


def _problems(response: httpx.Response) -> list[str]:
    """Return the ``error.detail.problems`` list of a rejected import.

    :param response: response with the standard error envelope.
    """
    detail = cast("dict[str, object]", _error(response)["detail"])
    return cast("list[str]", detail["problems"])


def _document(*, version: int = 2, settings: dict[str, object] | None = None) -> dict[str, object]:
    """Build a minimal one-entity import document.

    :param version: document version to declare.
    :param settings: settings section, defaulting to :data:`_SETTINGS`.
    """
    return {
        "format": "chime-deck/config",
        "version": version,
        "settings": dict(_SETTINGS) if settings is None else settings,
        "entities": [dict(_ENTITY)],
        "sounds": [],
        "buttons": [],
    }


async def _snapshot(
    store: ConfigStore,
) -> tuple[dict[str, object], list[Entity], list[Sound], list[Button]]:
    """Return everything a write could have changed, for a deep comparison.

    :param store: connected store of the application under test.
    """
    return (
        await store.all_settings(),
        await store.list_entities(),
        await store.list_sounds(),
        await store.list_buttons(),
    )


class _RecordingScan:
    """Stand-in for ``SoundLibrary.scan`` that records its calls."""

    def __init__(self) -> None:
        """Create a fake that has not been called."""
        self.calls = 0

    async def scan(self, *, budget_s: float = 0.0) -> ScanReport:
        """Record the call and report an empty library.

        :param budget_s: ignored; the fake probes nothing.
        """
        self.calls += 1
        return ScanReport(added=0, removed=0, missing=0, total=0)


async def test_export_is_a_downloadable_json_document(tmp_path: Path) -> None:
    """The export is one attachment carrying the versioned four-section document.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/config/export")

        assert response.status_code == 200
        assert response.headers["content-disposition"] == (
            'attachment; filename="chime-deck-config.json"'
        )
        assert response.headers["cache-control"] == "no-store"
        document = _body(response)
        assert document["format"] == "chime-deck/config"
        assert document["version"] == 2
        for section in ("settings", "entities", "sounds", "buttons"):
            assert section in document


async def test_secrets_travel_only_when_asked_for(tmp_path: Path) -> None:
    """``include_secrets`` decides whether the stored token is in the body.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await state.store.set_setting("ha.token", _SECRET)

        redacted = await client.get("/api/config/export")
        included = await client.get("/api/config/export", params={"include_secrets": "true"})

        assert redacted.status_code == 200
        assert _SECRET not in redacted.text
        assert included.status_code == 200
        assert _SECRET in included.text


async def test_dry_run_reports_the_counts_and_writes_nothing(tmp_path: Path) -> None:
    """A dry run answers with what it would write and leaves the store untouched.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        before = await _snapshot(state.store)

        response = await client.post(
            "/api/config/import", json={"dry_run": True, "document": _document()}
        )

        assert response.status_code == 200
        body = _body(response)
        assert body["dry_run"] is True
        assert body["settings_written"] == len(_SETTINGS)
        assert body["entities"] == 1
        assert body["sounds"] == 0
        assert body["buttons"] == 0
        assert await _snapshot(state.store) == before


async def test_import_applies_the_document(tmp_path: Path) -> None:
    """The same POST without ``dry_run`` writes the document's rows.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.post(
            "/api/config/import", json={"dry_run": False, "document": _document()}
        )

        assert response.status_code == 200
        body = _body(response)
        assert body["dry_run"] is False
        assert body["settings_written"] == len(_SETTINGS)
        assert body["entities"] == 1
        assert [entity.entity_id for entity in await state.store.list_entities()] == [
            "sensor.imported"
        ]
        assert await state.store.get_setting("audio.volume") == 20


async def test_a_future_version_is_rejected_and_writes_nothing(tmp_path: Path) -> None:
    """A document from a newer build is a 422 naming the version it wanted.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        before = await _snapshot(state.store)

        response = await client.post("/api/config/import", json={"document": _document(version=3)})

        assert response.status_code == 422
        assert _error(response)["code"] == "validation_error"
        assert any("version must be one of 1, 2" in problem for problem in _problems(response))
        assert await _snapshot(state.store) == before


async def test_an_invalid_document_lists_every_problem(tmp_path: Path) -> None:
    """The rejection names its problems in the message and the detail.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        before = await _snapshot(state.store)

        response = await client.post(
            "/api/config/import",
            json={"document": _document(settings={"nope.unknown": 1})},
        )

        assert response.status_code == 422
        error = _error(response)
        assert error["code"] == "validation_error"
        problems = _problems(response)
        assert problems
        assert any("unknown setting key" in problem for problem in problems)
        assert "unknown setting key" in cast(str, error["message"])
        assert await _snapshot(state.store) == before


async def test_import_rescans_the_sound_library_but_a_dry_run_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rescan is a follow-up of a committed import and only of a committed one.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that swaps the library's scan for a recorder.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        fake = _RecordingScan()
        monkeypatch.setattr(state.sounds, "scan", fake.scan)

        await client.post("/api/config/import", json={"dry_run": True, "document": _document()})
        assert fake.calls == 0

        await client.post("/api/config/import", json={"document": _document()})
        assert fake.calls == 1


async def test_import_refreshes_the_secret_scrubber(tmp_path: Path) -> None:
    """A secret that arrives with the document is scrubbed from the log from then on.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        assert SECRET_SCRUBBER.scrub(_SECRET) == _SECRET

        response = await client.post(
            "/api/config/import",
            json={"document": _document(settings={"ha.token": _SECRET})},
        )

        assert response.status_code == 200
        assert await state.store.get_setting("ha.token") == _SECRET
        assert SECRET_SCRUBBER.scrub(_SECRET) == REDACTED


async def test_mute_setting_survives_export_and_import(tmp_path: Path) -> None:
    """``audio.muted`` is a registry setting, so export carries it and import writes it.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await state.store.set_setting("audio.muted", True)
        exported = _body(await client.get("/api/config/export"))
        exported_settings = cast("dict[str, object]", exported["settings"])
        assert exported_settings["audio.muted"] is True

        await state.store.set_setting("audio.muted", False)
        response = await client.post(
            "/api/config/import",
            json={"document": _document(settings={"audio.muted": True})},
        )

        assert response.status_code == 200
        assert await state.store.get_setting("audio.muted") is True


async def test_appearance_settings_survive_export_and_import(tmp_path: Path) -> None:
    """The appearance keys are registry settings, so export carries them and import writes them.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    appearance: dict[str, object] = {
        "ui.theme": "light",
        "ui.accent_color": "#2ec4b6",
        "ui.dashboard_columns": 3,
        "ui.scale": 130,
        "ui.show_control_rail": False,
    }
    defaults: dict[str, object] = {
        "ui.theme": "dark",
        "ui.accent_color": "#4f9cf9",
        "ui.dashboard_columns": 0,
        "ui.scale": 100,
        "ui.show_control_rail": True,
    }

    async with _running(app) as client:
        for key, value in appearance.items():
            await state.store.set_setting(key, value)
        exported = _body(await client.get("/api/config/export"))
        exported_settings = cast("dict[str, object]", exported["settings"])
        for key, value in appearance.items():
            assert exported_settings[key] == value

        for key, value in defaults.items():
            await state.store.set_setting(key, value)
        response = await client.post(
            "/api/config/import",
            json={"document": _document(settings=appearance)},
        )

        assert response.status_code == 200
        for key, value in appearance.items():
            assert await state.store.get_setting(key) == value


async def test_a_non_default_scale_round_trips_as_an_integer(tmp_path: Path) -> None:
    """``ui.scale`` survives export and import as the int 130, not the string "130".

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await state.store.set_setting("ui.scale", 130)
        exported = _body(await client.get("/api/config/export"))
        exported_settings = cast("dict[str, object]", exported["settings"])
        assert exported_settings["ui.scale"] == 130

        await state.store.set_setting("ui.scale", 100)
        response = await client.post(
            "/api/config/import",
            json={"document": _document(settings={"ui.scale": 130})},
        )

        assert response.status_code == 200
        stored = await state.store.get_setting("ui.scale")
        assert stored == 130
        assert isinstance(stored, int)


async def test_import_rejects_a_scale_that_is_not_a_step(tmp_path: Path) -> None:
    """A percent between two steps is a validation problem and writes nothing.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        before = await _snapshot(state.store)

        response = await client.post(
            "/api/config/import",
            json={"document": _document(settings={"ui.scale": 87})},
        )

        assert response.status_code == 422
        assert _error(response)["code"] == "validation_error"
        assert any(
            "must be one of 85, 100, 115, 130, 150" in problem for problem in _problems(response)
        )
        assert await _snapshot(state.store) == before


async def test_export_round_trips_through_import(tmp_path: Path) -> None:
    """An export of a populated store imports back without changing a count.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await state.store.set_setting("audio.volume", 45)
        exported = (await client.get("/api/config/export")).text

        response = await client.post("/api/config/import", json={"document": json.loads(exported)})

        assert response.status_code == 200
        body = _body(response)
        written = body["settings_written"]
        assert isinstance(written, int) and written > 0
        assert await state.store.get_setting("audio.volume") == 45
