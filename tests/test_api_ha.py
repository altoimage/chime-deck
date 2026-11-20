"""``GET /api/ha/tts_engines``: the dropdown data, its wastes and its scrubbing.

Every test drives the real application through ``httpx.ASGITransport`` and hands
it a ``MockTransport`` client, so no test opens a socket or talks to a Home
Assistant. The route's contract is bounded degradation: an unreachable, asleep or
unconfigured install answers ``200`` with an empty list and one whole sentence,
and a secret that Home Assistant echoes back is scrubbed before it leaves.

Owning ticket: HSD-npt0.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state
from test_tts_ha import BASE_URL, TOKEN, Scripted, _json, _replies

from chime_deck.api.ha import HaEntityResponse, HaEntityRow
from chime_deck.app import AppState, create_app
from chime_deck.config import SECRET_SCRUBBER, ServerConfig
from chime_deck.entity_info import EntityContext
from chime_deck.store import ConfigStore

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_ROUTE = "/api/ha/tts_engines"
"""The endpoint under test."""

_ENTITIES: list[dict[str, object]] = [
    {
        "entity_id": "tts.piper",
        "state": "unknown",
        "attributes": {"friendly_name": "Piper", "language": "en_GB"},
    },
    {
        "entity_id": "tts.cloud",
        "state": "unknown",
        "attributes": {"friendly_name": 123, "language": "en_US"},
    },
    {
        "entity_id": "tts.blank",
        "state": "unknown",
        "attributes": {"friendly_name": "   "},
    },
    {"entity_id": "tts.nolang", "state": "unknown", "attributes": {}},
    {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
]
"""A realistic ``/api/states`` answer: four TTS entities and one non-TTS entity."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test."""
    with preserve_log_state():
        yield


def _build_app(
    tmp_path: Path,
    *,
    ha_client: httpx.AsyncClient | None = None,
) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory used as the data directory.
    :param ha_client: borrowed client for the Home Assistant routes; never
        closed by the app.
    """
    config = ServerConfig(data_dir=tmp_path, static_dir=tmp_path / "no-dist")
    app = create_app(config, ha_client=ha_client)
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


async def _seed_ha(store: ConfigStore) -> None:
    """Store the Home Assistant settings the route needs.

    :param store: connected configuration store.
    """
    await store.set_setting("ha.base_url", BASE_URL)
    await store.set_setting("ha.token", TOKEN)


async def test_the_tts_engines_are_sorted_and_reduced(tmp_path: Path) -> None:
    """TTS entities are sorted by id, named humanly and stripped of the extras.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted({("GET", "/api/states"): _replies(lambda: _json(_ENTITIES))})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, state = _build_app(tmp_path, ha_client=raw)
    try:
        async with _running(app) as client:
            await _seed_ha(state.store)
            response = await client.get(_ROUTE)
    finally:
        await raw.aclose()

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["warning"] is None
    engines = cast("list[dict[str, object]]", body["engines"])
    assert [engine["engine_id"] for engine in engines] == [
        "tts.blank",
        "tts.cloud",
        "tts.nolang",
        "tts.piper",
    ]
    by_id = {cast(str, engine["engine_id"]): engine for engine in engines}
    assert by_id["tts.piper"]["name"] == "Piper"
    assert by_id["tts.piper"]["language"] == "en_GB"
    assert by_id["tts.cloud"]["name"] == "123"
    assert by_id["tts.cloud"]["language"] == "en_US"
    assert by_id["tts.blank"]["name"] == "tts.blank"
    assert by_id["tts.nolang"]["language"] is None
    assert len(handler.requests) == 1


async def test_no_tts_entities_is_a_whole_sentence_warning(tmp_path: Path) -> None:
    """A reachable install with no TTS integration answers an explanatory sentence.

    :param tmp_path: pytest temporary directory.
    """
    only_light = [{"entity_id": "light.kitchen", "state": "on", "attributes": {}}]
    handler = Scripted({("GET", "/api/states"): _replies(lambda: _json(only_light))})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, state = _build_app(tmp_path, ha_client=raw)
    try:
        async with _running(app) as client:
            await _seed_ha(state.store)
            response = await client.get(_ROUTE)
    finally:
        await raw.aclose()

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["engines"] == []
    warning = cast(str, body["warning"])
    assert "no text-to-speech engines" in warning


async def test_an_unconfigured_install_names_the_token_setting(tmp_path: Path) -> None:
    """The registry's empty token answers a warning without any request.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted({("GET", "/api/states"): _replies(lambda: _json(_ENTITIES))})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, _state = _build_app(tmp_path, ha_client=raw)
    try:
        async with _running(app) as client:
            response = await client.get(_ROUTE)
    finally:
        await raw.aclose()

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["engines"] == []
    warning = cast(str, body["warning"])
    assert "ha.token" in warning
    assert handler.requests == []


async def test_an_unreachable_home_assistant_is_a_cannot_connect_warning(tmp_path: Path) -> None:
    """A connect failure is answered with the client's own sentence.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted({("GET", "/api/states"): httpx.ConnectError("boom")})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, state = _build_app(tmp_path, ha_client=raw)
    try:
        async with _running(app) as client:
            await _seed_ha(state.store)
            response = await client.get(_ROUTE)
    finally:
        await raw.aclose()

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["engines"] == []
    warning = cast(str, body["warning"])
    assert "cannot connect" in warning


async def test_an_unexpected_failure_warns_and_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``RuntimeError`` still answers ``200``, with its text and one log record.

    :param tmp_path: pytest temporary directory.
    :param caplog: pytest log capture fixture.
    """
    handler = Scripted({("GET", "/api/states"): RuntimeError("boom")})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, state = _build_app(tmp_path, ha_client=raw)
    try:
        async with _running(app) as client:
            await _seed_ha(state.store)
            with caplog.at_level(logging.WARNING, logger="chime_deck.api.ha"):
                response = await client.get(_ROUTE)
    finally:
        await raw.aclose()

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["engines"] == []
    warning = cast(str, body["warning"])
    assert "boom" in warning
    logged = [
        record.getMessage() for record in caplog.records if record.name == "chime_deck.api.ha"
    ]
    assert logged


async def test_a_malformed_state_list_is_a_warning_not_a_500(tmp_path: Path) -> None:
    """A JSON object where an array was expected still answers ``200`` and a sentence.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted({("GET", "/api/states"): _json({"not": "a list"})})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, state = _build_app(tmp_path, ha_client=raw)
    try:
        async with _running(app) as client:
            await _seed_ha(state.store)
            response = await client.get(_ROUTE)
    finally:
        await raw.aclose()

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["engines"] == []
    warning = cast(str, body["warning"])
    assert warning


async def test_the_token_is_scrubbed_from_the_answer(tmp_path: Path) -> None:
    """A ``friendly_name`` that echoes the token is scrubbed before it leaves.

    :param tmp_path: pytest temporary directory.
    """
    entities = [
        {
            "entity_id": "tts.piper",
            "state": "unknown",
            "attributes": {"friendly_name": f"Piper {TOKEN}"},
        }
    ]
    handler = Scripted({("GET", "/api/states"): _replies(lambda: _json(entities))})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, state = _build_app(tmp_path, ha_client=raw)
    try:
        async with _running(app) as client:
            await _seed_ha(state.store)
            SECRET_SCRUBBER.set_secrets([TOKEN])
            response = await client.get(_ROUTE)
    finally:
        await raw.aclose()

    assert response.status_code == 200
    assert TOKEN not in response.text


async def test_the_borrowed_client_stays_open_for_a_second_request(tmp_path: Path) -> None:
    """The injected client is never closed, so two consecutive requests work.

    :param tmp_path: pytest temporary directory.
    """
    handler = Scripted({("GET", "/api/states"): _replies(lambda: _json(_ENTITIES))})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, state = _build_app(tmp_path, ha_client=raw)
    try:
        async with _running(app) as client:
            await _seed_ha(state.store)
            first = await client.get(_ROUTE)
            second = await client.get(_ROUTE)
            still_open = raw.is_closed is False
    finally:
        await raw.aclose()

    assert first.status_code == 200
    assert second.status_code == 200
    assert still_open is True
    assert len(handler.requests) == 2


async def test_the_route_deadline_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung call is abandoned at the route's budget and named in the warning.

    ``MockTransport`` does not enforce httpx's timeout slots, so the inner budget
    is raised above the outer one: the route's own ``asyncio.timeout`` must be the
    one that fires, and its number is what the page shows.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that shrinks the route budget.
    """

    async def hang(request: httpx.Request) -> httpx.Response:
        """Never answer inside the test's lifetime.

        :param request: the state request.
        """
        await asyncio.sleep(10)
        return _json(_ENTITIES)

    monkeypatch.setattr("chime_deck.api.ha.TTS_ENGINES_TIMEOUT_S", 0.05)
    monkeypatch.setattr("chime_deck.api.ha.TTS_ENGINES_INNER_TIMEOUT_S", 2.0)
    handler = Scripted({("GET", "/api/states"): hang})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, state = _build_app(tmp_path, ha_client=raw)
    try:
        async with _running(app) as client:
            await _seed_ha(state.store)
            response = await client.get(_ROUTE)
    finally:
        await raw.aclose()

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["engines"] == []
    warning = cast(str, body["warning"])
    assert "0.05 s" in warning


async def test_an_owned_client_is_closed_after_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the route builds its own client, it closes it before answering.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that installs the recording client factory.
    """
    handler = Scripted({("GET", "/api/states"): _replies(lambda: _json(_ENTITIES))})
    created: list[httpx.AsyncClient] = []

    def factory() -> httpx.AsyncClient:
        """Build and record the client the route will own."""
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        created.append(client)
        return client

    monkeypatch.setattr("chime_deck.api.ha._new_client", factory)
    app, state = _build_app(tmp_path)
    async with _running(app) as client:
        await _seed_ha(state.store)
        response = await client.get(_ROUTE)

    assert response.status_code == 200
    assert len(created) == 1
    assert created[0].is_closed is True


class _FakeLiveEntityInfo:
    """A scripted ``LiveEntityInfo``: complete mappings, ``None``, or a delay."""

    def __init__(
        self,
        contexts: Mapping[str, EntityContext] | None = None,
        *,
        delay: float = 0.0,
    ) -> None:
        """Record the scripted answer.

        :param contexts: complete mapping to return, or ``None`` for "cannot ask".
        :param delay: seconds to sleep before answering, to exercise the bound.
        """
        self.calls: list[list[str]] = []
        self._contexts = contexts
        self._delay = delay

    async def contexts_for(self, entity_ids: Sequence[str]) -> Mapping[str, EntityContext] | None:
        """Answer as scripted, recording the requested ids.

        :param entity_ids: entity ids to look up.
        """
        self.calls.append(list(entity_ids))
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._contexts


async def test_one_entity_is_described_live(tmp_path: Path) -> None:
    """A live context fills every field of the single-entity response.

    :param tmp_path: pytest temporary directory.
    """
    attributes = {
        "friendly_name": "Hall temperature",
        "device_class": "temperature",
        "unit_of_measurement": "°C",
    }
    app, state = _build_app(tmp_path)
    async with _running(app) as client:
        seam = _FakeLiveEntityInfo(
            {
                "sensor.hall": EntityContext(
                    entity_id="sensor.hall",
                    found=True,
                    state="21.4",
                    attributes=attributes,
                    last_changed="2026-01-01T00:00:00Z",
                )
            }
        )
        state.live_entities = seam
        response = await client.get("/api/ha/entities/sensor.hall")

    assert response.status_code == 200
    assert response.json() == {
        "entity_id": "sensor.hall",
        "name": "Hall temperature",
        "state": "21.4",
        "icon": "mdi:thermometer",
        "icon_source": "fallback",
        "domain": "sensor",
        "device_class": "temperature",
        "attributes": attributes,
        "last_changed": "2026-01-01T00:00:00Z",
        "found": True,
        "warning": None,
    }
    assert seam.calls == [["sensor.hall"]]


async def test_a_missing_friendly_name_falls_back_to_the_entity_id(tmp_path: Path) -> None:
    """Home Assistant's own name is optional, so the id is the honest fallback.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    async with _running(app) as client:
        state.live_entities = _FakeLiveEntityInfo(
            {
                "lock.front_door": EntityContext(
                    entity_id="lock.front_door", found=True, state="locked"
                )
            }
        )
        response = await client.get("/api/ha/entities/lock.front_door")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["name"] == "lock.front_door"
    assert body["device_class"] is None
    assert body["attributes"] == {}


async def test_an_unknown_entity_is_a_sentence_not_a_404(tmp_path: Path) -> None:
    """``found: false`` is a ``200`` with the actionable sentence beside it.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    async with _running(app) as client:
        state.live_entities = _FakeLiveEntityInfo(
            {"light.nowhere": EntityContext(entity_id="light.nowhere", found=False)}
        )
        response = await client.get("/api/ha/entities/light.nowhere")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["found"] is False
    assert body["state"] is None
    warning = cast(str, body["warning"])
    assert "does not know the entity 'light.nowhere'" in warning


async def test_a_seam_that_cannot_answer_is_null_and_a_sentence(tmp_path: Path) -> None:
    """``found: null`` tells the editor Home Assistant is unreachable, not wrong.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    async with _running(app) as client:
        state.live_entities = _FakeLiveEntityInfo(None)
        response = await client.get("/api/ha/entities/lock.front_door")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["found"] is None
    assert body["state"] is None
    assert body["attributes"] == {}
    warning = cast(str, body["warning"])
    assert "did not answer" in warning
    assert "ha.base_url" in warning


async def test_a_malformed_entity_id_is_a_422(tmp_path: Path) -> None:
    """An id that is not Home Assistant's shape is the caller's mistake.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)
    async with _running(app) as client:
        response = await client.get("/api/ha/entities/Not An Id")

    assert response.status_code == 422
    body = cast("dict[str, object]", response.json())
    error = cast("dict[str, object]", body["error"])
    assert error["code"] == "validation_error"
    assert "entity_id" in cast(str, error["message"])
    assert error["detail"] == {"field": "entity_id"}


async def test_a_slow_seam_answers_within_the_bound(tmp_path: Path) -> None:
    """A lookup slower than the bound degrades instead of stalling the route.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    async with _running(app) as client:
        state.live_entities = _FakeLiveEntityInfo(
            {"lock.front_door": EntityContext(entity_id="lock.front_door", found=True)},
            delay=5.0,
        )
        state.live_lookup_timeout = 0.01
        started = asyncio.get_running_loop().time()
        response = await client.get("/api/ha/entities/lock.front_door")
        elapsed = asyncio.get_running_loop().time() - started

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    assert body["found"] is None
    assert elapsed < 1.0


async def test_the_described_entity_is_a_superset_of_a_search_row(tmp_path: Path) -> None:
    """The single-entity row carries every list-row key, and its contract is unchanged.

    The editor picks an entity from ``GET /api/ha/entities?q=`` and then previews
    it with ``GET /api/ha/entities/{entity_id}``, so the second answer must be a
    superset of the first: a field the picker showed cannot be missing from the
    preview. The three-state ``found`` and the ``422``-only-for-malformed rule are
    asserted here too, as the one named test for this ticket's single-entity half.

    :param tmp_path: pytest temporary directory.
    """
    assert set(HaEntityRow.model_fields) <= set(HaEntityResponse.model_fields)
    context = EntityContext(
        entity_id="sensor.hall",
        found=True,
        state="21.4",
        attributes={"friendly_name": "Hall temperature", "device_class": "temperature"},
        last_changed="2026-01-01T00:00:00Z",
    )
    app, state = _build_app(tmp_path)
    async with _running(app) as client:
        state.live_entities = _FakeLiveEntityInfo({"sensor.hall": context})
        live = await client.get("/api/ha/entities/sensor.hall")
        state.live_entities = _FakeLiveEntityInfo(
            {"sensor.nowhere": EntityContext(entity_id="sensor.nowhere", found=False)}
        )
        unknown = await client.get("/api/ha/entities/sensor.nowhere")
        state.live_entities = _FakeLiveEntityInfo(None)
        unanswered = await client.get("/api/ha/entities/sensor.hall")
        malformed = await client.get("/api/ha/entities/Not An Id")

    answered = (live, unknown, unanswered)
    assert [response.status_code for response in (*answered, malformed)] == [200, 200, 200, 422]
    bodies = [cast("dict[str, object]", response.json()) for response in answered]
    assert [body["found"] for body in bodies] == [True, False, None]
    row = bodies[0]
    for field in HaEntityRow.model_fields:
        assert field in row, field
    assert (row["name"], row["icon"], row["icon_source"]) == (
        "Hall temperature",
        "mdi:thermometer",
        "fallback",
    )
