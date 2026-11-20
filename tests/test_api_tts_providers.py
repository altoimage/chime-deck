"""``/api/tts/providers``: provider CRUD, the secrecy rule and the measured probe.

Every test drives the real application through ``httpx.ASGITransport`` against a
temporary database, so no socket, broker or sound card is touched
(``offline_wiring`` is autouse). The provider *test* route borrows the
application's injected ``ha_client`` — the same seam the Home Assistant routes
use — so a test answers it with ``httpx.MockTransport`` and no provider is ever
contacted.

The secrecy test is the headline here: it runs a whole provider lifecycle with a
distinctive sentinel key and scans every response body, every captured log record
and the configuration export for it, so "the key never leaves" is asserted, not
assumed.

Owning ticket: HSD-s856.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state

from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig, install_log_scrubber

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_PROVIDERS_ROUTE = "/api/tts/providers"
"""The provider collection under test."""

_SENTINEL = "sk-test-DO-NOT-LEAK-0123456789"
"""A distinctive API key: unique enough that a leak assertion cannot pass by luck."""

_SENTINEL_OVERRIDE = "sk-inside-overrides-DO-NOT-LEAK"
"""A key hidden in the wrong place; it must be refused and never echoed."""

_TEST_PHRASE = "This is a test of the doorbell's speech provider."
"""The route's fixed phrase; repeated here so the characters sent are assertable."""

_MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"\xff\xfb\x90\x00" * 40
"""Bytes that sniff as MP3 and are large enough to count as audible audio."""

_Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]
"""One scripted transport answer."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test."""
    with preserve_log_state():
        yield


def _build_app(tmp_path: Path, *, raw_client: httpx.AsyncClient | None = None) -> FastAPI:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory used as the data directory.
    :param raw_client: the borrowed HTTP client a provider test may use.
    """
    config = ServerConfig(
        data_dir=tmp_path,
        static_dir=tmp_path / "no-dist",
        seed_demo_sounds=False,
    )
    return create_app(config, ha_client=raw_client)


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


def _state(app: FastAPI) -> AppState:
    """Return the application state behind *app*.

    :param app: application under test.
    """
    return cast(AppState, app.state.doorbell)


def _mock_client(handler: _Handler) -> httpx.AsyncClient:
    """Build a client whose every request is answered by *handler*.

    :param handler: scripted answer, one per request.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _error(response: httpx.Response) -> dict[str, object]:
    """Return the ``error`` object of a failed response.

    :param response: response carrying the standard error envelope.
    """
    return cast("dict[str, object]", response.json()["error"])


async def _create(
    client: httpx.AsyncClient,
    *,
    name: str = "OpenAI",
    preset: str = "openai",
    voice: str = "alloy",
    api_key: object = _SENTINEL,
    price: object = None,
    base_url: str | None = None,
    overrides: dict[str, object] | None = None,
) -> tuple[httpx.Response, dict[str, object]]:
    """Create one provider and return its response and decoded body.

    :param client: client bound to the application under test.
    :param name: provider display name.
    :param preset: preset id.
    :param voice: voice the preset request names.
    :param api_key: top-level key to send; ``...`` omits the field.
    :param price: record price per 1000 characters, ``None`` for the preset's.
    :param base_url: explicit server address, ``None`` for the preset's default.
    :param overrides: request-shape overrides, for the ``custom`` preset.
    """
    body: dict[str, object] = {"name": name, "preset": preset, "voice": voice}
    if api_key is not ...:
        body["api_key"] = api_key
    if price is not None:
        body["cost_per_1k_chars_usd"] = price
    if base_url is not None:
        body["base_url"] = base_url
    if overrides is not None:
        body["overrides"] = overrides
    response = await client.post(_PROVIDERS_ROUTE, json=body)
    return response, cast("dict[str, object]", response.json())


async def test_the_empty_provider_list_offers_the_six_presets(tmp_path: Path) -> None:
    """A fresh install lists no providers and every preset with its request shape.

    :param tmp_path: pytest temporary directory.
    """
    app = _build_app(tmp_path)
    async with _running(app) as client:
        response = await client.get(_PROVIDERS_ROUTE)

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["providers"] == []
    assert body["selected"] == ""
    assert body["warning"] is None
    presets = cast("list[dict[str, object]]", body["presets"])
    assert [preset["id"] for preset in presets] == [
        "azure",
        "custom",
        "elevenlabs",
        "google",
        "openai",
        "openai_compatible",
    ]
    for preset in presets:
        shape = cast("dict[str, object]", preset["shape"])
        assert set(shape) == {
            "method",
            "path",
            "headers",
            "auth",
            "body_kind",
            "body",
            "response",
            "format",
        }
        auth = cast("dict[str, object]", shape["auth"])
        assert set(auth) == {"style", "name", "prefix"}
        assert shape["format"] == "mp3"
    by_id = {cast(str, preset["id"]): preset for preset in presets}
    assert by_id["custom"]["requires_base_url"] is True
    assert by_id["custom"]["cost_per_1k_chars_usd"] is None
    assert by_id["openai"]["requires_base_url"] is False
    assert by_id["openai"]["cost_per_1k_chars_usd"] == pytest.approx(0.015)


async def test_a_preset_and_a_key_are_enough_to_create_a_provider(tmp_path: Path) -> None:
    """The acceptance shape: a preset, a voice and a key, nothing else.

    :param tmp_path: pytest temporary directory.
    """
    app = _build_app(tmp_path)
    async with _running(app) as client:
        response, body = await _create(client)
        listed = cast("dict[str, object]", (await client.get(_PROVIDERS_ROUTE)).json())

    assert response.status_code == 201
    provider_id = cast(str, body["id"])
    assert re.fullmatch(r"[0-9a-f]{12}", provider_id)
    assert body["name"] == "OpenAI"
    assert body["preset"] == "openai"
    assert body["base_url"] == "https://api.openai.com"
    assert body["voice"] == "alloy"
    assert body["paid"] is True
    assert body["has_api_key"] is True
    assert body["overrides"] == {}
    assert _SENTINEL not in response.text
    providers = cast("list[dict[str, object]]", listed["providers"])
    assert [provider["id"] for provider in providers] == [provider_id]
    assert providers[0]["has_api_key"] is True
    assert _SENTINEL not in str(listed)


async def test_the_api_key_never_appears_in_a_response_a_log_or_the_export(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A full lifecycle with a sentinel key leaves it in no visible text anywhere.

    The test provider answers ``401`` with the key echoed in its body, which is
    the one reply most likely to smuggle it out. Every response's raw text, every
    captured log record and the configuration export are scanned together.

    :param tmp_path: pytest temporary directory.
    :param caplog: pytest's log capture fixture.
    """
    seen: list[str] = []

    async def reject(request: httpx.Request) -> httpx.Response:
        """Answer like a provider that echoes the rejected key.

        :param request: the request being answered.
        """
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(
            401,
            headers={"content-type": "application/json"},
            json={"error": {"message": f"invalid key {_SENTINEL}"}},
        )

    raw = _mock_client(reject)
    app = _build_app(tmp_path, raw_client=raw)
    # Production installs this with the logging configuration; the scrubber's
    # record factory is what redacts a value out of a log line.
    install_log_scrubber()
    try:
        async with _running(app) as client:
            caplog.set_level(logging.DEBUG)
            created = await client.post(
                _PROVIDERS_ROUTE,
                json={
                    "name": "Leaky",
                    "preset": "openai",
                    "voice": "alloy",
                    "api_key": _SENTINEL,
                },
            )
            provider_id = cast(str, created.json()["id"])
            responses = [
                created,
                await client.get(_PROVIDERS_ROUTE),
                await client.get("/api/settings"),
                await client.get("/api/config/export"),
                await client.patch(f"{_PROVIDERS_ROUTE}/{provider_id}", json={"voice": "nova"}),
                await client.post(f"{_PROVIDERS_ROUTE}/{provider_id}/test"),
                await client.delete(f"{_PROVIDERS_ROUTE}/{provider_id}"),
            ]
    finally:
        await raw.aclose()

    bodies = [response.text for response in responses]
    logs = caplog.text
    test_answer = cast("dict[str, object]", responses[5].json())
    assert test_answer["ok"] is False
    assert test_answer["latency_ms"] is None
    error = cast(str, test_answer["error"])
    assert "rejected the API key" in error
    assert seen and seen[0] == f"Bearer {_SENTINEL}"
    joined = "\n".join([*bodies, logs])
    assert _SENTINEL not in joined


async def test_patch_keeps_clears_and_refuses_the_key(tmp_path: Path) -> None:
    """An absent key is left alone, ``null`` clears it and ``""`` is refused.

    :param tmp_path: pytest temporary directory.
    """
    app = _build_app(tmp_path)
    state = _state(app)
    async with _running(app) as client:
        _, body = await _create(client)
        provider_id = cast(str, body["id"])

        renamed = await client.patch(f"{_PROVIDERS_ROUTE}/{provider_id}", json={"name": "Renamed"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Renamed"
        assert renamed.json()["has_api_key"] is True

        # A submitted id is ignored: the path names the record.
        asserted = await client.patch(
            f"{_PROVIDERS_ROUTE}/{provider_id}",
            json={"id": "feedfacecafe", "name": "Renamed Again"},
        )
        assert asserted.status_code == 200
        assert asserted.json()["id"] == provider_id
        assert await state.store.has_provider_secret(provider_id)

        cleared = await client.patch(f"{_PROVIDERS_ROUTE}/{provider_id}", json={"api_key": None})
        assert cleared.status_code == 200
        assert cleared.json()["has_api_key"] is False
        assert not await state.store.has_provider_secret(provider_id)

        refused = await client.patch(f"{_PROVIDERS_ROUTE}/{provider_id}", json={"api_key": ""})
        assert refused.status_code == 422
        assert "send null to clear a secret, not an empty string" in cast(
            str, _error(refused)["message"]
        )

        replaced = await client.patch(
            f"{_PROVIDERS_ROUTE}/{provider_id}", json={"api_key": "sk-replacement-0123456789"}
        )
        assert replaced.status_code == 200
        assert replaced.json()["has_api_key"] is True
        assert _SENTINEL not in replaced.text


async def test_rejections_name_the_field_without_echoing_a_value(tmp_path: Path) -> None:
    """Unknown presets, bad addresses, duplicates and hidden keys are all refused.

    :param tmp_path: pytest temporary directory.
    """
    app = _build_app(tmp_path)
    async with _running(app) as client:
        unknown = await client.post(
            _PROVIDERS_ROUTE,
            json={"name": "Nowhere", "preset": "does-not-exist", "voice": "alloy"},
        )
        assert unknown.status_code == 422
        message = cast(str, _error(unknown)["message"])
        assert "does-not-exist" in message
        assert "azure" in message and "custom" in message

        bad_url = await client.post(
            _PROVIDERS_ROUTE,
            json={"name": "Bad URL", "preset": "openai", "voice": "alloy", "base_url": "ftp://x"},
        )
        assert bad_url.status_code == 422
        assert "base_url" in cast(str, _error(bad_url)["message"])

        first, _ = await _create(client, name="Kitchen")
        assert first.status_code == 201
        duplicate = await client.post(
            _PROVIDERS_ROUTE,
            json={"name": "Kitchen", "preset": "openai", "voice": "nova"},
        )
        assert duplicate.status_code == 422
        assert "repeats the name" in cast(str, _error(duplicate)["message"])

        hidden = await client.post(
            _PROVIDERS_ROUTE,
            json={
                "name": "Sneaky",
                "preset": "custom",
                "base_url": "http://custom.test",
                "api_key": _SENTINEL,
                "overrides": {"api_key": _SENTINEL_OVERRIDE},
            },
        )
        assert hidden.status_code == 422
        hidden_message = cast(str, _error(hidden)["message"])
        assert "top-level api_key field" in hidden_message
        assert _SENTINEL_OVERRIDE not in hidden.text
        assert _SENTINEL not in hidden.text


async def test_delete_removes_the_record_its_key_and_the_selection(tmp_path: Path) -> None:
    """A deleted provider loses its key and cannot stay silently selected.

    :param tmp_path: pytest temporary directory.
    """
    app = _build_app(tmp_path)
    state = _state(app)
    async with _running(app) as client:
        _, body = await _create(client)
        provider_id = cast(str, body["id"])
        selected = await client.patch("/api/settings", json={"tts.http_tts.provider": provider_id})
        assert selected.status_code == 200

        deleted = await client.delete(f"{_PROVIDERS_ROUTE}/{provider_id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "id": provider_id}

        listed = cast("dict[str, object]", (await client.get(_PROVIDERS_ROUTE)).json())
        assert listed["providers"] == []
        assert listed["selected"] == ""
        assert not await state.store.has_provider_secret(provider_id)
        assert await state.store.get_setting("tts.http_tts.provider") == ""

        missing = await client.delete(f"{_PROVIDERS_ROUTE}/{provider_id}")
        assert missing.status_code == 404
        assert _error(missing)["code"] == "not_found"


async def test_the_test_route_measures_a_successful_synthesis(tmp_path: Path) -> None:
    """A working provider answers a measured round trip and its priced cost.

    :param tmp_path: pytest temporary directory.
    """
    asked: list[httpx.Request] = []

    async def audio(request: httpx.Request) -> httpx.Response:
        """Answer with MP3 bytes, recording the request.

        :param request: the request being answered.
        """
        asked.append(request)
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=_MP3)

    raw = _mock_client(audio)
    app = _build_app(tmp_path, raw_client=raw)
    try:
        async with _running(app) as client:
            _, body = await _create(client, price=0.015)
            provider_id = cast(str, body["id"])
            response = await client.post(f"{_PROVIDERS_ROUTE}/{provider_id}/test")
    finally:
        await raw.aclose()

    assert response.status_code == 200
    answer = cast("dict[str, object]", response.json())
    assert answer["ok"] is True
    assert answer["provider"] == provider_id
    latency = answer["latency_ms"]
    assert isinstance(latency, float)
    assert latency >= 0.0
    assert answer["chars"] == len(_TEST_PHRASE)
    assert answer["format"] == "mp3"
    assert answer["cost_per_100_chars_usd"] == pytest.approx(0.015 / 10)
    assert answer["estimated_cost_usd"] == pytest.approx(len(_TEST_PHRASE) * 0.015 / 1000)
    assert answer["error"] is None
    assert asked and asked[0].url == "https://api.openai.com/v1/audio/speech"
    assert _SENTINEL not in response.text


async def test_an_unpriced_provider_reports_no_cost(tmp_path: Path) -> None:
    """A provider with no price says so instead of inventing ``0.0``.

    :param tmp_path: pytest temporary directory.
    """

    async def audio(request: httpx.Request) -> httpx.Response:
        """Answer with MP3 bytes.

        :param request: the request being answered.
        """
        return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=_MP3)

    raw = _mock_client(audio)
    app = _build_app(tmp_path, raw_client=raw)
    try:
        async with _running(app) as client:
            _, body = await _create(
                client,
                name="Local",
                preset="custom",
                voice="",
                api_key=...,
                base_url="http://custom.test",
            )
            provider_id = cast(str, body["id"])
            response = await client.post(f"{_PROVIDERS_ROUTE}/{provider_id}/test")
    finally:
        await raw.aclose()

    answer = cast("dict[str, object]", response.json())
    assert answer["ok"] is True
    assert answer["cost_per_100_chars_usd"] is None
    assert answer["estimated_cost_usd"] is None


async def test_failures_are_a_200_with_a_sentence_and_no_latency(tmp_path: Path) -> None:
    """A non-audio reply, a missing key and a 500 all answer a verdict, not a 5xx.

    :param tmp_path: pytest temporary directory.
    """

    async def html_page(request: httpx.Request) -> httpx.Response:
        """Answer with an HTML error page.

        :param request: the request being answered.
        """
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<!doctype html><html><body>nope</body></html>",
        )

    raw = _mock_client(html_page)
    app = _build_app(tmp_path, raw_client=raw)
    try:
        async with _running(app) as client:
            _, body = await _create(client)
            provider_id = cast(str, body["id"])
            not_audio = await client.post(f"{_PROVIDERS_ROUTE}/{provider_id}/test")
            missing = await client.post(f"{_PROVIDERS_ROUTE}/000000000000/test")
    finally:
        await raw.aclose()

    assert not_audio.status_code == 200
    answer = cast("dict[str, object]", not_audio.json())
    assert answer["ok"] is False
    assert answer["latency_ms"] is None
    assert answer["chars"] is None
    assert "did not return audio" in cast(str, answer["error"])
    assert missing.status_code == 404
    assert _error(missing)["code"] == "not_found"


async def test_a_missing_key_or_a_500_is_reported_as_a_failed_test(tmp_path: Path) -> None:
    """A keyless provider names itself, and a 500 still answers 200.

    :param tmp_path: pytest temporary directory.
    """

    async def broken(request: httpx.Request) -> httpx.Response:
        """Answer every request with a server error.

        :param request: the request being answered.
        """
        return httpx.Response(500, json={"error": {"message": "upstream is down"}})

    raw = _mock_client(broken)
    app = _build_app(tmp_path, raw_client=raw)
    try:
        async with _running(app) as client:
            _, keyless = await _create(client, name="Keyless", api_key=...)
            keyless_id = cast(str, keyless["id"])
            no_key = await client.post(f"{_PROVIDERS_ROUTE}/{keyless_id}/test")
            _, broken_body = await _create(client, name="Broken")
            broken_id = cast(str, broken_body["id"])
            failed = await client.post(f"{_PROVIDERS_ROUTE}/{broken_id}/test")
    finally:
        await raw.aclose()

    assert no_key.status_code == 200
    no_key_answer = cast("dict[str, object]", no_key.json())
    assert no_key_answer["ok"] is False
    assert "Keyless" in cast(str, no_key_answer["error"])
    assert "API key" in cast(str, no_key_answer["error"])

    assert failed.status_code == 200
    failed_answer = cast("dict[str, object]", failed.json())
    assert failed_answer["ok"] is False
    assert failed_answer["latency_ms"] is None
    assert "HTTP 500" in cast(str, failed_answer["error"])


async def test_an_unreadable_stored_list_is_a_warning_not_a_500(tmp_path: Path) -> None:
    """A hand-edited ``tts.providers`` of nonsense is reported, never fatal.

    The public setter refuses a value that is not JSON by design, so the row is
    written the way a hand edit (or a newer build) arrives: straight into the
    settings table.

    :param tmp_path: pytest temporary directory.
    """
    app = _build_app(tmp_path)
    state = _state(app)
    async with _running(app) as client:
        async with state.store.transaction() as connection:
            await connection.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                ("tts.providers", json.dumps("nonsense"), "2026-01-01T00:00:00+00:00"),
            )
        response = await client.get(_PROVIDERS_ROUTE)
        refused = await client.post(
            _PROVIDERS_ROUTE,
            json={"name": "Late", "preset": "openai", "voice": "alloy"},
        )

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["providers"] == []
    assert body["selected"] == ""
    warning = cast(str, body["warning"])
    assert "could not be read" in warning
    assert "settings file" in warning
    assert "nonsense" not in warning

    assert refused.status_code == 422
    assert "cannot read" in cast(str, _error(refused)["message"])
    assert "nonsense" not in refused.text
