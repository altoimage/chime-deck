"""Sounds HTTP API: list, rescan, URL download, seeding and press wiring.

Every test drives the real application through ``httpx.ASGITransport`` and every
download goes through ``httpx.MockTransport``, so no socket is opened and no real
``ffprobe`` runs (the autouse ``no_real_ffprobe`` fixture makes it absent, which
is why seeded durations are ``None``). Rename and browser-preview coverage was
added by HSD-w9eq.

Owning ticket: HSD-cwvc.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from chime_fakes import RecordingBackend
from fastapi import FastAPI
from log_isolation import preserve_log_state
from mqtt_fakes import wait_until

from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig
from chime_deck.sounds import (
    MAX_NAME_STEM,
    SUPPORTED_FORMATS,
    DownloadResult,
    SoundDownloadError,
    SoundErrorCode,
    bundled_demo_sounds_dir,
)
from chime_deck.store import ButtonIn, ConfigEvent, SoundIn

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_PLAY_TIMEOUT_S = 2.0
"""Bound for the playback worker to record one play."""

_WAV_BYTES = b"RIFF....WAVEfmt "


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test."""
    with preserve_log_state():
        yield


def _build_app(
    tmp_path: Path,
    *,
    seed_demo_sounds: bool = False,
    sound_client: httpx.AsyncClient | None = None,
) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory used as the data directory.
    :param seed_demo_sounds: whether the lifespan seeds the bundled demos.
    :param sound_client: borrowed client for URL downloads; never closed here.
    """
    config = ServerConfig(
        data_dir=tmp_path,
        static_dir=tmp_path / "no-dist",
        seed_demo_sounds=seed_demo_sounds,
    )
    app = create_app(config, sound_client=sound_client)
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


def _audio_client(
    calls: list[httpx.Request],
    *,
    content_type: str = "audio/wav",
    content: bytes = _WAV_BYTES,
) -> httpx.AsyncClient:
    """Build a mock-transport client that answers with one audio file.

    A fresh :class:`httpx.Response` is built per request, because a response can
    only be read once.

    :param calls: list that records every request the transport received.
    :param content_type: media type of the response.
    :param content: response body.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the request and answer with the configured file."""
        calls.append(request)
        return httpx.Response(200, headers={"content-type": content_type}, content=content)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _drain(events: asyncio.Queue[ConfigEvent]) -> list[ConfigEvent]:
    """Return and clear every queued event.

    :param events: queue subscribed to the store's event bus.
    """
    drained: list[ConfigEvent] = []
    while not events.empty():
        drained.append(events.get_nowait())
    return drained


async def _create_sound(
    state: AppState,
    directory: Path,
    *,
    name: str,
    stored: str,
    write_file: bool = True,
) -> int:
    """Create one sound row, optionally writing its file into the library.

    :param state: application state providing a connected store.
    :param directory: the app's ``data/sounds`` directory.
    :param name: display name for the row.
    :param stored: relative path stored on the row.
    :param write_file: whether to write ``_WAV_BYTES`` at *stored* first.
    """
    if write_file:
        target = directory / stored
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_WAV_BYTES)
    row = await state.store.create_sound(SoundIn(name=name, source_type="file", path=stored))
    return row.id


def _error_of(response: httpx.Response) -> dict[str, object]:
    """Return the ``error`` object of an error envelope.

    :param response: failing response under test.
    """
    return cast("dict[str, object]", response.json()["error"])


def _validation_detail(response: httpx.Response) -> dict[str, object]:
    """Assert a 422 ``validation_error`` and return its ``detail`` object.

    :param response: failing response under test.
    """
    assert response.status_code == 422
    error = _error_of(response)
    assert error["code"] == "validation_error"
    return cast("dict[str, object]", error["detail"])


async def test_get_is_empty_when_nothing_is_seeded(tmp_path: Path) -> None:
    """A fresh data directory lists no sounds when seeding is off.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/sounds")

    assert response.status_code == 200
    assert response.json() == []


async def test_rescan_on_an_empty_data_dir_reports_zeros(tmp_path: Path) -> None:
    """The first rescan creates the folder and changes nothing.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.post("/api/sounds/rescan")

    assert response.status_code == 200
    assert response.json() == {"added": 0, "removed": 0, "missing": 0}
    assert (tmp_path / "sounds").is_dir()


async def test_rescan_indexes_a_new_file(tmp_path: Path) -> None:
    """A written file is added with its stem name, format, bytes and no duration.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        (tmp_path / "sounds").mkdir(parents=True, exist_ok=True)
        (tmp_path / "sounds" / "ding.wav").write_bytes(_WAV_BYTES)
        rescan = await client.post("/api/sounds/rescan")
        listing = await client.get("/api/sounds")

    assert rescan.json()["added"] == 1
    rows = cast("list[dict[str, object]]", listing.json())
    assert len(rows) == 1
    assert rows[0]["name"] == "ding"
    assert rows[0]["format"] == "wav"
    assert rows[0]["bytes"] == len(_WAV_BYTES)
    assert rows[0]["missing"] is False
    assert rows[0]["duration_ms"] is None


async def test_vanished_file_is_flagged_missing_and_reappearing_clears_it(
    tmp_path: Path,
) -> None:
    """Deleting flags a row missing; restoring the file clears the flag.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)
    path = tmp_path / "sounds" / "ding.wav"

    async with _running(app) as client:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_WAV_BYTES)
        await client.post("/api/sounds/rescan")

        path.unlink()
        removed = (await client.post("/api/sounds/rescan")).json()
        missing_listing = cast("list[dict[str, object]]", (await client.get("/api/sounds")).json())

        path.write_bytes(_WAV_BYTES)
        restored = (await client.post("/api/sounds/rescan")).json()
        restored_listing = cast("list[dict[str, object]]", (await client.get("/api/sounds")).json())

    assert removed == {"added": 0, "removed": 1, "missing": 1}
    assert missing_listing[0]["missing"] is True
    assert restored["removed"] == 0
    assert restored["added"] == 0
    assert restored["missing"] == 0
    assert restored_listing[0]["missing"] is False


async def test_from_url_stores_one_file_with_metadata(tmp_path: Path) -> None:
    """A downloaded audio file is stored once with its URL, hash and size.

    :param tmp_path: pytest temporary directory.
    """
    calls: list[httpx.Request] = []

    async with _audio_client(calls) as sound_client:
        app, _state = _build_app(tmp_path, sound_client=sound_client)
        async with _running(app) as client:
            response = await client.post(
                "/api/sounds/from_url", json={"url": "https://example.test/ding.wav"}
            )

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["created"] is True
    sound = cast("dict[str, object]", body["sound"])
    assert sound["url"] == "https://example.test/ding.wav"
    assert sound["sha256"] == hashlib.sha256(_WAV_BYTES).hexdigest()
    assert sound["bytes"] == len(_WAV_BYTES)
    assert sound["format"] == "wav"
    assert sound["path"] == "ding.wav"
    assert [entry.name for entry in (tmp_path / "sounds").iterdir()] == ["ding.wav"]
    assert len(calls) == 1


async def test_from_url_is_a_no_op_for_identical_bytes(tmp_path: Path) -> None:
    """Re-downloading unchanged bytes creates nothing and emits no sound event.

    :param tmp_path: pytest temporary directory.
    """
    calls: list[httpx.Request] = []

    async with _audio_client(calls) as sound_client:
        app, state = _build_app(tmp_path, sound_client=sound_client)
        async with _running(app) as client:
            events = state.store.events.subscribe_queue()
            try:
                first = cast(
                    "dict[str, object]",
                    (
                        await client.post(
                            "/api/sounds/from_url", json={"url": "https://example.test/ding.wav"}
                        )
                    ).json(),
                )
                _drain(events)
                target = tmp_path / "sounds" / "ding.wav"
                mtime_before = target.stat().st_mtime_ns
                sha_before = hashlib.sha256(target.read_bytes()).hexdigest()

                second = cast(
                    "dict[str, object]",
                    (
                        await client.post(
                            "/api/sounds/from_url", json={"url": "https://example.test/ding.wav"}
                        )
                    ).json(),
                )
                sound_events = [event for event in _drain(events) if event.what == "sounds"]
            finally:
                state.store.events.unsubscribe_queue(events)

    first_sound = cast("dict[str, object]", first["sound"])
    second_sound = cast("dict[str, object]", second["sound"])
    assert second["created"] is False
    assert second_sound["id"] == first_sound["id"]
    assert sound_events == []
    assert [entry.name for entry in (tmp_path / "sounds").iterdir()] == ["ding.wav"]
    assert target.stat().st_mtime_ns == mtime_before
    assert hashlib.sha256(target.read_bytes()).hexdigest() == sha_before


async def test_from_url_rejects_a_web_page(tmp_path: Path) -> None:
    """A non-audio content type is 415 with an actionable, URL-free message.

    :param tmp_path: pytest temporary directory.
    """
    calls: list[httpx.Request] = []

    async with _audio_client(calls, content_type="text/html", content=b"<html>") as sound_client:
        app, state = _build_app(tmp_path, sound_client=sound_client)
        async with _running(app) as client:
            response = await client.post(
                "/api/sounds/from_url", json={"url": "https://example.test/page"}
            )
            assert await state.store.list_sounds() == []

    assert response.status_code == 415
    error = cast("dict[str, object]", response.json()["error"])
    assert error["code"] == "sound_not_audio"
    message = cast(str, error["message"])
    assert "not audio" in message
    for token in ("wav", "mp3", "ogg", "flac"):
        assert token in message
    assert "http" not in message
    assert not (tmp_path / "sounds" / "page.wav").exists()


async def test_from_url_rejects_a_non_http_url_without_a_request(tmp_path: Path) -> None:
    """``file:`` is rejected as an invalid URL and no transport call is made.

    :param tmp_path: pytest temporary directory.
    """
    calls: list[httpx.Request] = []

    async with _audio_client(calls) as sound_client:
        app, _state = _build_app(tmp_path, sound_client=sound_client)
        async with _running(app) as client:
            response = await client.post("/api/sounds/from_url", json={"url": "file:///etc/passwd"})

    assert response.status_code == 422
    error = cast("dict[str, object]", response.json()["error"])
    assert error["code"] == "invalid_url"
    assert calls == []


async def test_port_outside_the_valid_range_is_a_422_with_a_real_client(
    tmp_path: Path,
) -> None:
    """A port anyio cannot connect to is a submitted-value error, never a 500.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.post(
            "/api/sounds/from_url", json={"url": "http://127.0.0.1:99999/a.mp3"}
        )
        rows = await state.store.list_sounds()

    assert response.status_code == 422
    error = cast("dict[str, object]", response.json()["error"])
    assert error["code"] == "invalid_url"
    assert rows == []
    assert list((tmp_path / "sounds").iterdir()) == []


async def test_a_refused_loopback_connection_is_a_502(tmp_path: Path) -> None:
    """A real transport that cannot connect stays a transfer failure.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.post(
            "/api/sounds/from_url", json={"url": "http://127.0.0.1:1/a.mp3"}
        )
        rows = await state.store.list_sounds()

    assert response.status_code == 502
    error = cast("dict[str, object]", response.json()["error"])
    assert error["code"] == "download_failed"
    assert rows == []


async def test_startup_seeds_the_bundled_demos(tmp_path: Path) -> None:
    """Seeding copies every bundled audio file and the licence, and indexes only audio.

    :param tmp_path: pytest temporary directory.
    """
    assets = bundled_demo_sounds_dir()
    assert assets is not None
    expected = sum(
        1
        for path in assets.iterdir()
        if path.is_file() and path.suffix.lower().lstrip(".") in SUPPORTED_FORMATS
    )
    app, _state = _build_app(tmp_path, seed_demo_sounds=True)

    async with _running(app) as client:
        listing = await client.get("/api/sounds")

    rows = cast("list[dict[str, object]]", listing.json())
    assert len(rows) == expected
    assert (tmp_path / "sounds" / "LICENSES.md").is_file()
    assert all(row["source_type"] == "file" for row in rows)
    assert all(row["duration_ms"] is None for row in rows)


async def test_seeding_can_be_switched_off(tmp_path: Path) -> None:
    """With seeding off the folder stays empty and the list is empty.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        listing = await client.get("/api/sounds")

    assert listing.json() == []
    directory = tmp_path / "sounds"
    assert not directory.exists() or list(directory.iterdir()) == []


async def test_press_of_a_scanned_relative_row_plays_the_library_file(
    tmp_path: Path, fake_backend: RecordingBackend
) -> None:
    """The production wiring resolves a relative row against ``data/sounds``.

    :param tmp_path: pytest temporary directory.
    :param fake_backend: recording speaker shared with the application.
    """
    app, state = _build_app(tmp_path)
    path = tmp_path / "sounds" / "ding.wav"

    async with _running(app) as client:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_WAV_BYTES)
        await client.post("/api/sounds/rescan")
        rows = cast("list[dict[str, object]]", (await client.get("/api/sounds")).json())
        sound_id = cast(int, rows[0]["id"])
        button = await state.store.upsert_button(
            ButtonIn(name="Door", action_type="sound", sound_id=sound_id)
        )

        response = await client.post(f"/api/buttons/{button.id}/press")
        assert response.status_code == 200
        await wait_until(lambda: len(fake_backend.played) == 1, timeout=_PLAY_TIMEOUT_S)

    assert fake_backend.played == [(tmp_path / "sounds" / "ding.wav", 70)]
    assert path.is_file()


async def test_name_exhaustion_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exhausted name maps to 409 ``conflict``, not the generic 422 name error.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that makes the downloader report exhaustion.
    """
    app, state = _build_app(tmp_path)

    async def failing_download(url: str, *, name: str | None = None) -> DownloadResult:
        """Report that every ``stem-N`` variant was already taken."""
        raise SoundDownloadError(
            SoundErrorCode.INVALID_NAME,
            "no free file name could be made from 'ding' — send a different name",
            {"reason": "name_exhausted"},
        )

    monkeypatch.setattr(state.downloader, "download", failing_download)

    async with _running(app) as client:
        response = await client.post(
            "/api/sounds/from_url", json={"url": "https://example.test/ding.mp3"}
        )

    assert response.status_code == 409
    error = cast("dict[str, object]", response.json()["error"])
    assert error["code"] == "conflict"
    assert "different name" in cast(str, error["message"])


async def test_listing_is_ordered_by_id(tmp_path: Path) -> None:
    """Two rows come back in ascending id order, matching ``list_sounds()``.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        first = await state.store.create_sound(
            SoundIn(name="First", source_type="file", path="first.wav")
        )
        second = await state.store.create_sound(
            SoundIn(name="Second", source_type="file", path="second.wav")
        )
        rows = cast("list[dict[str, object]]", (await client.get("/api/sounds")).json())

    assert [row["id"] for row in rows] == [first.id, second.id]
    assert [row["name"] for row in rows] == ["First", "Second"]


async def test_delete_a_used_sound_is_a_conflict_until_confirmed(tmp_path: Path) -> None:
    """A referenced sound is refused with the buttons that would be deleted.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await state.store.create_sound(
            SoundIn(name="Ding", source_type="file", path="ding.mp3")
        )
        button = await state.store.upsert_button(
            ButtonIn(name="First", action_type="sound", sound_id=sound.id)
        )

        response = await client.delete(f"/api/sounds/{sound.id}")
        assert response.status_code == 409
        error = cast("dict[str, object]", response.json()["error"])
        assert error["code"] == "conflict"
        assert "?confirm=true" in cast(str, error["message"])
        detail = cast("dict[str, object]", error["detail"])
        assert detail["requires"] == "confirm"
        assert detail["count"] == 1
        assert detail["affected_buttons"] == [{"id": button.id, "name": "First", "slug": "first"}]
        # Nothing was deleted by the refused request.
        assert await state.store.get_sound(sound.id) == sound
        assert await state.store.list_buttons() == [button]


async def test_delete_with_confirm_removes_the_sound_and_its_buttons(tmp_path: Path) -> None:
    """``?confirm=true`` deletes the sound and the buttons that used it.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound = await state.store.create_sound(
            SoundIn(name="Ding", source_type="file", path="ding.mp3")
        )
        button = await state.store.upsert_button(
            ButtonIn(name="First", action_type="sound", sound_id=sound.id)
        )

        response = await client.delete(f"/api/sounds/{sound.id}?confirm=true")
        assert response.status_code == 200
        assert response.json() == {
            "affected_buttons": [{"id": button.id, "name": "First", "slug": "first"}]
        }
        assert await state.store.list_sounds() == []
        assert await state.store.list_buttons() == []


async def test_rename_changes_only_the_name(tmp_path: Path) -> None:
    """``PATCH`` changes the display name and nothing else, on disk or in the row.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    directory = tmp_path / "sounds"

    async with _running(app) as client:
        sound_id = await _create_sound(state, directory, name="Ding", stored="ding.wav")
        before = await state.store.get_sound(sound_id)
        response = await client.patch(f"/api/sounds/{sound_id}", json={"name": "Doorbell"})
        after = await state.store.get_sound(sound_id)

    assert response.status_code == 200
    assert response.json()["name"] == "Doorbell"
    for field in ("path", "source_type", "format", "duration_ms", "bytes", "missing"):
        assert getattr(after, field) == getattr(before, field)
    assert (directory / "ding.wav").is_file()
    assert not (directory / "Doorbell.wav").exists()


async def test_rename_strips_surrounding_whitespace(tmp_path: Path) -> None:
    """Leading and trailing spaces are removed before the name is stored.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound_id = await _create_sound(state, tmp_path / "sounds", name="Ding", stored="ding.wav")
        response = await client.patch(f"/api/sounds/{sound_id}", json={"name": "  Door chime  "})

    assert response.status_code == 200
    assert response.json()["name"] == "Door chime"


async def test_rename_rejects_an_empty_name(tmp_path: Path) -> None:
    """An empty name is a 422; the submitted value never comes back.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound_id = await _create_sound(state, tmp_path / "sounds", name="Ding", stored="ding.wav")
        response = await client.patch(f"/api/sounds/{sound_id}", json={"name": ""})

    detail = _validation_detail(response)
    assert detail["field"] == "name"
    assert detail["reason"] == "empty"
    assert '"name": ""' not in response.text


async def test_rename_rejects_a_whitespace_only_name(tmp_path: Path) -> None:
    """Whitespace is stripped first, so a blank name is refused, not stored empty.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    submitted = "   "

    async with _running(app) as client:
        sound_id = await _create_sound(state, tmp_path / "sounds", name="Ding", stored="ding.wav")
        response = await client.patch(f"/api/sounds/{sound_id}", json={"name": submitted})

    detail = _validation_detail(response)
    assert detail["field"] == "name"
    assert detail["reason"] == "empty"
    assert submitted not in response.text


async def test_rename_rejects_a_control_character(tmp_path: Path) -> None:
    """A control character in a name is a 422 that does not echo the value.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    submitted = "bad\x00name"

    async with _running(app) as client:
        sound_id = await _create_sound(state, tmp_path / "sounds", name="Ding", stored="ding.wav")
        response = await client.patch(f"/api/sounds/{sound_id}", json={"name": submitted})

    detail = _validation_detail(response)
    assert detail["field"] == "name"
    assert detail["reason"] == "control_characters"
    assert submitted not in response.text


async def test_rename_rejects_a_name_over_the_limit(tmp_path: Path) -> None:
    """A name longer than ``MAX_NAME_STEM`` is a 422 that does not echo it.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    submitted = "A" * (MAX_NAME_STEM + 1)

    async with _running(app) as client:
        sound_id = await _create_sound(state, tmp_path / "sounds", name="Ding", stored="ding.wav")
        response = await client.patch(f"/api/sounds/{sound_id}", json={"name": submitted})

    detail = _validation_detail(response)
    assert detail["field"] == "name"
    assert detail["reason"] == "too_long"
    assert detail["max"] == MAX_NAME_STEM
    assert submitted not in response.text


async def test_rename_of_an_unknown_sound_is_404(tmp_path: Path) -> None:
    """An unknown id is the store's own 404 ``not_found``, not a validation error.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch("/api/sounds/999", json={"name": "New"})

    assert response.status_code == 404
    assert _error_of(response)["code"] == "not_found"


async def test_rename_ignores_unknown_body_keys(tmp_path: Path) -> None:
    """Only ``name`` can reach the store; extra keys are ignored, not applied.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound_id = await _create_sound(state, tmp_path / "sounds", name="Ding", stored="ding.wav")
        before = await state.store.get_sound(sound_id)
        response = await client.patch(
            f"/api/sounds/{sound_id}",
            json={"name": "New", "path": "../../etc/passwd", "missing": True},
        )
        after = await state.store.get_sound(sound_id)

    assert response.status_code == 200
    assert response.json()["name"] == "New"
    assert after.path == before.path == "ding.wav"
    assert after.missing is before.missing is False


async def test_rename_allows_a_duplicate_name(tmp_path: Path) -> None:
    """Names are not unique, so two rows may share one and both are listed.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    directory = tmp_path / "sounds"

    async with _running(app) as client:
        await _create_sound(state, directory, name="Ding", stored="ding.wav")
        second = await _create_sound(state, directory, name="Dong", stored="dong.wav")
        response = await client.patch(f"/api/sounds/{second}", json={"name": "Ding"})
        rows = cast("list[dict[str, object]]", (await client.get("/api/sounds")).json())

    assert response.status_code == 200
    assert [row["name"] for row in rows] == ["Ding", "Ding"]


@pytest.mark.parametrize(
    ("stored", "content_type"),
    [
        ("clip.wav", "audio/wav"),
        ("clip.mp3", "audio/mpeg"),
        ("clip.ogg", "audio/ogg"),
        ("clip.flac", "audio/flac"),
        ("clip.opus", "application/octet-stream"),
    ],
)
async def test_preview_serves_the_exact_bytes_with_the_right_content_type(
    tmp_path: Path, stored: str, content_type: str
) -> None:
    """The bytes on disk, the resolved file's type and the preview headers.

    :param tmp_path: pytest temporary directory.
    :param stored: file name inside the library; its suffix decides the type.
    :param content_type: media type the route must report.
    """
    data = _WAV_BYTES + stored.encode()
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound_id = await _create_sound(
            state, tmp_path / "sounds", name="Clip", stored=stored, write_file=False
        )
        (tmp_path / "sounds" / stored).write_bytes(data)
        response = await client.get(f"/api/sounds/{sound_id}/audio")

    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == content_type
    assert response.headers["accept-ranges"] == "bytes"
    assert "inline" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_preview_supports_a_byte_range(tmp_path: Path) -> None:
    """A ``Range`` request is a 206 with ``Content-Range`` so ``<audio>`` can seek.

    :param tmp_path: pytest temporary directory.
    """
    data = b"0123456789abcdef"
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound_id = await _create_sound(
            state, tmp_path / "sounds", name="Clip", stored="clip.wav", write_file=False
        )
        (tmp_path / "sounds" / "clip.wav").write_bytes(data)
        response = await client.get(f"/api/sounds/{sound_id}/audio", headers={"Range": "bytes=2-5"})

    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 2-5/{len(data)}"
    assert response.content == data[2:6]


async def test_preview_of_an_unknown_sound_is_a_json_404(tmp_path: Path) -> None:
    """An unknown id answers the API's JSON envelope, never the SPA placeholder.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/sounds/999/audio")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert _error_of(response)["code"] == "not_found"


async def test_preview_of_a_deleted_file_is_a_clean_missing_envelope(tmp_path: Path) -> None:
    """A row whose file vanished is a 404 ``sound_missing`` naming only the id.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    directory = tmp_path / "sounds"

    async with _running(app) as client:
        sound_id = await _create_sound(state, directory, name="Gone", stored="gone.wav")
        (directory / "gone.wav").unlink()
        response = await client.get(f"/api/sounds/{sound_id}/audio")

    assert response.status_code == 404
    error = _error_of(response)
    assert error["code"] == "sound_missing"
    assert error["detail"] == {"sound_id": sound_id}
    assert str(tmp_path) not in response.text


@pytest.mark.parametrize(
    "stored",
    ["../../etc/passwd", "/etc/passwd", "a/../../b.wav", "..\\..\\windows"],
)
async def test_preview_refuses_a_traversal_path(tmp_path: Path, stored: str) -> None:
    """A hostile stored path is refused as ``sound_missing``, never served or a 500.

    :param tmp_path: pytest temporary directory.
    :param stored: hostile path stored on the row.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        sound_id = await state.store.create_sound(
            SoundIn(name="Hostile", source_type="file", path=stored)
        )
        response = await client.get(f"/api/sounds/{sound_id.id}/audio")

    assert response.status_code == 404
    error = _error_of(response)
    assert error["code"] == "sound_missing"
    assert error["detail"] == {"sound_id": sound_id.id}
    assert "/etc" not in response.text


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unsupported here")
async def test_preview_refuses_a_symlink_that_leaves_the_library(tmp_path: Path) -> None:
    """Resolution follows the link, so a target outside the library is refused.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    directory = tmp_path / "sounds"
    outside = tmp_path / "outside.wav"

    async with _running(app) as client:
        outside.write_bytes(_WAV_BYTES)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "link.wav").symlink_to(outside)
        sound_id = await state.store.create_sound(
            SoundIn(name="Link", source_type="file", path="link.wav")
        )
        response = await client.get(f"/api/sounds/{sound_id.id}/audio")

    assert response.status_code == 404
    error = _error_of(response)
    assert error["code"] == "sound_missing"
    assert error["detail"] == {"sound_id": sound_id.id}


async def test_preview_never_falls_through_to_the_spa(tmp_path: Path) -> None:
    """Preview paths stay reserved, so a missing sound is JSON and a slashed one is not HTML.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        missing = await client.get("/api/sounds/999/audio")
        trailing = await client.get("/api/sounds/1/audio/")

    assert missing.headers["content-type"].startswith("application/json")
    assert _error_of(missing)["code"] == "not_found"
    assert trailing.status_code != 200
    assert "text/html" not in trailing.headers.get("content-type", "")
