"""``GET /api/ha/entities?q=``: the picker's corpus, order, caps and sentences.

Every test drives the real application through ``httpx.ASGITransport`` and hands
it a ``MockTransport`` client answering ``GET /api/states``, so no test opens a
socket or talks to a Home Assistant. The route's contract under test is the
envelope's invariant — ``warning`` is set if and only if the list is empty
*because Home Assistant could not be asked* — plus the ordering a picker depends
on, the cap that keeps one keystroke cheap, and D16's icon chain, which the picker
previews and the tile then renders.

Owning ticket: HSD-zyru.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Final, NamedTuple, cast

import httpx
import pytest
from ha_ws_fakes import FakeHaFactory, FakeHaPeer, make_client
from log_isolation import preserve_log_state
from test_api_ha import _build_app, _running, _seed_ha
from test_tts_ha import TOKEN, Scripted, _Item, _json, _replies

from chime_deck.app import AppState
from chime_deck.config import SECRET_SCRUBBER
from chime_deck.ha import HaEntityInfo
from chime_deck.store import ConfigStore, Entity, EntityIn

pytestmark = pytest.mark.asyncio

_ROUTE: Final = "/api/ha/entities"
"""The endpoint under test."""

_REGISTRY_COMMAND: Final = "config/entity_registry/list_for_display"
"""The websocket command the concrete adapter fetches registry icons with."""

_BOUND_S: Final = 1.0
"""A generous in-test wall-clock bound for a route that must not stall."""

_STATES: Final[list[dict[str, object]]] = [
    {
        "entity_id": "lock.front_door",
        "state": "locked",
        "attributes": {"friendly_name": "Front Door", "icon": "mdi:door-closed-lock"},
        "last_changed": "2026-01-01T00:00:00Z",
    },
    {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {"friendly_name": "Kitchen Ceiling"},
    },
    {
        "entity_id": "sensor.hall",
        "state": "21.4",
        "attributes": {"friendly_name": "Hall Temperature", "device_class": "temperature"},
    },
    {
        "entity_id": "switch.porch",
        "state": "off",
        "attributes": {"friendly_name": "Porch Light"},
    },
]
"""A small realistic ``/api/states`` corpus: four domains, one icon at each step."""

_ORDERING_STATES: Final[list[dict[str, object]]] = [
    {"entity_id": "sensor.hallway", "state": "on", "attributes": {}},
    {
        "entity_id": "sensor.hall",
        "state": "21.4",
        "attributes": {"friendly_name": "Hall Temperature"},
    },
]
"""Two entities that both match ``sensor.hall``, one exactly and one as a substring."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test."""
    with preserve_log_state():
        yield


class _Doorbell(NamedTuple):
    """A running app, its state and the ``MockTransport`` that records requests."""

    client: httpx.AsyncClient
    state: AppState
    handler: Scripted


@asynccontextmanager
async def _doorbell(
    tmp_path: Path,
    states: _Item,
    *,
    configured: bool = True,
) -> AsyncIterator[_Doorbell]:
    """Run the real app against a ``MockTransport`` Home Assistant.

    :param tmp_path: pytest temporary directory used as the data directory.
    :param states: the answer of ``GET /api/states``: a response, a handler or an
        exception to raise.
    :param configured: whether ``ha.base_url`` and ``ha.token`` are stored, so a
        test can exercise the "not configured" sentence with no socket at all.
    """
    handler = Scripted({("GET", "/api/states"): states})
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, state = _build_app(tmp_path, ha_client=raw)
    try:
        async with _running(app) as client:
            if configured:
                await _seed_ha(state.store)
            yield _Doorbell(client=client, state=state, handler=handler)
    finally:
        await raw.aclose()


def _rows(response: httpx.Response) -> list[dict[str, object]]:
    """Return the ``entities`` list of a search answer.

    :param response: answer of ``GET /api/ha/entities``.
    """
    body = cast("dict[str, object]", response.json())
    return cast("list[dict[str, object]]", body["entities"])


def _entity_ids(response: httpx.Response) -> list[str]:
    """Return the entity ids of a search answer, in order.

    :param response: answer of ``GET /api/ha/entities``.
    """
    return [cast(str, row["entity_id"]) for row in _rows(response)]


def _warning(response: httpx.Response) -> str | None:
    """Return the answer's ``warning``, which is ``None`` when there is nothing to say.

    :param response: answer of ``GET /api/ha/entities``.
    """
    body = cast("dict[str, object]", response.json())
    return cast("str | None", body["warning"])


def _live_info(entries: list[dict[str, object]]) -> HaEntityInfo:
    """Build the REAL registry adapter over an offline, scripted websocket peer.

    :param entries: the registry's ``entities`` payload, as Home Assistant sends it.
    """
    factory = FakeHaFactory(partial(FakeHaPeer, results={_REGISTRY_COMMAND: {"entities": entries}}))
    return HaEntityInfo(make_client(factory))


def _broken_live_info() -> HaEntityInfo:
    """Build a real adapter whose registry fetch cannot open a connection at all."""
    factory = FakeHaFactory(_refuse_connection)
    return HaEntityInfo(make_client(factory))


def _refuse_connection() -> FakeHaPeer:
    """Refuse to open a websocket, the way a stopped Home Assistant would.

    :raises OSError: always; the client classifies it as a connection failure.
    """
    raise OSError("the websocket is not reachable")


async def _seed_tiles(store: ConfigStore, entity_ids: list[str]) -> None:
    """Configure tiles in dashboard order, as the user would have.

    :param store: connected configuration store.
    :param entity_ids: tile entity ids, first one first on the dashboard.
    """
    for entity_id in entity_ids:
        await store.upsert_entity(EntityIn(entity_id=entity_id))


async def test_the_search_filters_on_the_id_and_the_name(tmp_path: Path) -> None:
    """A hit is a case-insensitive substring of either the entity id or the name.

    :param tmp_path: pytest temporary directory.
    """
    async with _doorbell(tmp_path, _replies(lambda: _json(_STATES))) as doorbell:
        by_id = await doorbell.client.get(_ROUTE, params={"q": "PORCH"})
        by_name = await doorbell.client.get(_ROUTE, params={"q": "FRONT dOoR"})
        by_both = await doorbell.client.get(_ROUTE, params={"q": "hall"})

    assert _entity_ids(by_id) == ["switch.porch"]
    assert _entity_ids(by_name) == ["lock.front_door"]
    assert _entity_ids(by_both) == ["sensor.hall"]


async def test_a_search_that_matches_nothing_is_not_a_warning(tmp_path: Path) -> None:
    """``{"entities": [], "warning": null}`` is the honest "no matches" answer.

    :param tmp_path: pytest temporary directory.
    """
    async with _doorbell(tmp_path, _replies(lambda: _json(_STATES))) as doorbell:
        response = await doorbell.client.get(_ROUTE, params={"q": "nothing-here"})

    assert response.status_code == 200
    assert response.json() == {"entities": [], "warning": None}


async def test_a_row_is_design_05s_row_plus_the_icon_source(tmp_path: Path) -> None:
    """The row carries the six design keys and the D16 vocabulary the tile reports.

    :param tmp_path: pytest temporary directory.
    """
    async with _doorbell(tmp_path, _replies(lambda: _json(_STATES))) as doorbell:
        response = await doorbell.client.get(_ROUTE, params={"q": "lock.front_door"})

    assert _rows(response) == [
        {
            "entity_id": "lock.front_door",
            "name": "Front Door",
            "state": "locked",
            "icon": "mdi:door-closed-lock",
            "icon_source": "ha-attribute",
            "domain": "lock",
            "device_class": None,
        }
    ]


async def test_the_registry_icon_is_d16s_second_step(tmp_path: Path) -> None:
    """A live registry ``ic`` beats the domain table and loses to ``attributes.icon``.

    :param tmp_path: pytest temporary directory.
    """
    entries: list[dict[str, object]] = [
        {"ei": "switch.porch", "ic": "mdi:flash"},
        {"ei": "lock.front_door", "ic": "mdi:door-sliding"},
        {"ei": "sensor.hall", "ic": None},
    ]
    async with _doorbell(tmp_path, _replies(lambda: _json(_STATES))) as doorbell:
        doorbell.state.live_entities = _live_info(entries)
        response = await doorbell.client.get(_ROUTE)

    by_id = {cast(str, row["entity_id"]): row for row in _rows(response)}
    assert (by_id["switch.porch"]["icon"], by_id["switch.porch"]["icon_source"]) == (
        "mdi:flash",
        "ha-registry",
    )
    assert (by_id["lock.front_door"]["icon"], by_id["lock.front_door"]["icon_source"]) == (
        "mdi:door-closed-lock",
        "ha-attribute",
    )
    assert (by_id["sensor.hall"]["icon"], by_id["sensor.hall"]["icon_source"]) == (
        "mdi:thermometer",
        "fallback",
    )


async def test_the_domain_table_is_the_third_icon_step(tmp_path: Path) -> None:
    """With no registry capability at all, the domain table still answers.

    ``NoLiveEntityInfo`` deliberately does not satisfy ``LiveEntityRegistry``, so
    this is the ordinary build: rows keep a sensible icon and no warning appears.

    :param tmp_path: pytest temporary directory.
    """
    async with _doorbell(tmp_path, _replies(lambda: _json(_STATES))) as doorbell:
        response = await doorbell.client.get(_ROUTE, params={"q": "sensor"})

    assert _warning(response) is None
    assert [(row["icon"], row["icon_source"]) for row in _rows(response)] == [
        ("mdi:thermometer", "fallback")
    ]
    assert _rows(response)[0]["device_class"] == "temperature"


async def test_an_exact_id_hit_outranks_a_configured_tile(tmp_path: Path) -> None:
    """A type-ahead that typed a whole id gets that row first, tile or not.

    :param tmp_path: pytest temporary directory.
    """
    async with _doorbell(tmp_path, _replies(lambda: _json(_ORDERING_STATES))) as doorbell:
        await _seed_tiles(doorbell.state.store, ["sensor.hallway"])
        exact = await doorbell.client.get(_ROUTE, params={"q": "sensor.hall"})
        partial_hit = await doorbell.client.get(_ROUTE, params={"q": "sensor"})

    assert _entity_ids(exact) == ["sensor.hall", "sensor.hallway"]
    assert _entity_ids(partial_hit) == ["sensor.hallway", "sensor.hall"]


async def test_the_initial_view_is_the_tiles_then_the_alphabet(tmp_path: Path) -> None:
    """An empty ``q`` opens on the user's own tiles in dashboard order, then A-Z.

    :param tmp_path: pytest temporary directory.
    """
    async with _doorbell(tmp_path, _replies(lambda: _json(_STATES))) as doorbell:
        await _seed_tiles(doorbell.state.store, ["switch.porch", "sensor.hall"])
        response = await doorbell.client.get(_ROUTE)

    assert _warning(response) is None
    assert _entity_ids(response) == [
        "switch.porch",
        "sensor.hall",
        "lock.front_door",
        "light.kitchen",
    ]


async def test_an_unreadable_tile_list_still_lists_the_entities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that cannot answer costs the ordering, never the list or a warning.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that makes the tile read fail.
    """
    async with _doorbell(tmp_path, _replies(lambda: _json(_STATES))) as doorbell:
        await _seed_tiles(doorbell.state.store, ["switch.porch", "sensor.hall"])

        async def refuse() -> list[Entity]:
            """Fail the way a lost database would.

            :raises RuntimeError: always.
            """
            raise RuntimeError("the database is gone")

        monkeypatch.setattr(doorbell.state.store, "list_entities", refuse)
        response = await doorbell.client.get(_ROUTE)

    assert _warning(response) is None
    assert _entity_ids(response) == [
        "lock.front_door",
        "sensor.hall",
        "light.kitchen",
        "switch.porch",
    ]


async def test_the_default_page_is_fifty_rows(tmp_path: Path) -> None:
    """A picker that sends no ``limit`` gets fifty rows, not the whole house.

    :param tmp_path: pytest temporary directory.
    """
    corpus = [
        {"entity_id": f"sensor.e{index:02d}", "state": "on", "attributes": {}}
        for index in range(55)
    ]
    async with _doorbell(tmp_path, _replies(lambda: _json(corpus))) as doorbell:
        response = await doorbell.client.get(_ROUTE)

    assert len(_rows(response)) == 50
    assert _warning(response) is None


async def test_the_limit_is_applied_after_the_ordering(tmp_path: Path) -> None:
    """``limit`` truncates the head of the ordered list, so the best rows survive.

    :param tmp_path: pytest temporary directory.
    """
    async with _doorbell(tmp_path, _replies(lambda: _json(_STATES))) as doorbell:
        await _seed_tiles(doorbell.state.store, ["switch.porch"])
        response = await doorbell.client.get(_ROUTE, params={"limit": 2})

    assert _entity_ids(response) == ["switch.porch", "lock.front_door"]


@pytest.mark.parametrize("limit", [0, 201])
async def test_a_limit_outside_the_range_is_the_error_envelope(tmp_path: Path, limit: int) -> None:
    """The cap is part of the contract, so a wrong value is a ``422``, not a clamp.

    :param tmp_path: pytest temporary directory.
    :param limit: an out-of-range page size.
    """
    async with _doorbell(tmp_path, _replies(lambda: _json(_STATES))) as doorbell:
        response = await doorbell.client.get(_ROUTE, params={"limit": limit})

    assert response.status_code == 422
    body = cast("dict[str, object]", response.json())
    error = cast("dict[str, object]", body["error"])
    assert error["code"] == "validation_error"
    assert error["detail"] is not None


async def test_an_unconfigured_install_names_both_settings(tmp_path: Path) -> None:
    """Nothing stored means nothing to ask: the sentence says which settings to fill in.

    :param tmp_path: pytest temporary directory.
    """
    async with _doorbell(tmp_path, _replies(lambda: _json(_STATES)), configured=False) as doorbell:
        response = await doorbell.client.get(_ROUTE)
        requested = doorbell.handler.requests

    assert response.status_code == 200
    assert _rows(response) == []
    warning = cast(str, _warning(response))
    assert "not configured" in warning
    assert "ha.base_url" in warning
    assert "ha.token" in warning
    assert requested == []


async def test_a_rejected_token_is_the_scrubbed_sentence(tmp_path: Path) -> None:
    """A ``401`` is a ``200`` carrying Home Assistant's own actionable sentence.

    :param tmp_path: pytest temporary directory.
    """
    answer = _json({"message": "unauthorized"}, status=401)
    async with _doorbell(tmp_path, answer) as doorbell:
        response = await doorbell.client.get(_ROUTE)

    assert response.status_code == 200
    assert _rows(response) == []
    assert "rejected the token" in cast(str, _warning(response))


async def test_an_unreachable_home_assistant_is_a_cannot_connect_sentence(
    tmp_path: Path,
) -> None:
    """A connect failure is answered with the client's own sentence.

    :param tmp_path: pytest temporary directory.
    """
    async with _doorbell(tmp_path, httpx.ConnectError("boom")) as doorbell:
        response = await doorbell.client.get(_ROUTE)

    assert response.status_code == 200
    assert _rows(response) == []
    assert "cannot connect" in cast(str, _warning(response))


async def test_a_slow_home_assistant_answers_inside_the_route_bound(tmp_path: Path) -> None:
    """A hung corpus is abandoned at ``live_lookup_timeout`` and named in the sentence.

    ``MockTransport`` does not enforce httpx's timeout slots, so the inner budget
    is far larger than the shipped one: the route's own ``asyncio.timeout`` must be
    the one that fires, and its number is what the picker shows.

    :param tmp_path: pytest temporary directory.
    """

    async def hang(request: httpx.Request) -> httpx.Response:
        """Never answer inside the test's lifetime.

        :param request: the state request.
        """
        await asyncio.sleep(10)
        return _json(_STATES)

    async with _doorbell(tmp_path, hang) as doorbell:
        doorbell.state.live_lookup_timeout = 0.05
        started = asyncio.get_running_loop().time()
        response = await doorbell.client.get(_ROUTE)
        elapsed = asyncio.get_running_loop().time() - started

    assert response.status_code == 200
    assert _rows(response) == []
    assert "0.05 s" in cast(str, _warning(response))
    # Far below the 10 s the transport sleeps and above the route's own 0.05 s
    # budget: the answer came from the route's bound, not from the transport.
    assert elapsed < _BOUND_S


async def test_a_failing_registry_keeps_the_rows_and_stays_silent(tmp_path: Path) -> None:
    """A registry that cannot be asked costs an icon step, never a row or a warning.

    :param tmp_path: pytest temporary directory.
    """
    async with _doorbell(tmp_path, _replies(lambda: _json(_STATES))) as doorbell:
        doorbell.state.live_entities = _broken_live_info()
        response = await doorbell.client.get(_ROUTE, params={"q": "switch"})

    assert _warning(response) is None
    assert [(row["entity_id"], row["icon_source"]) for row in _rows(response)] == [
        ("switch.porch", "fallback")
    ]


async def test_a_warning_always_means_an_empty_list(tmp_path: Path) -> None:
    """The envelope's invariant holds for every answer the route can give.

    Home Assistant being unreachable always means ``entities == []`` plus a
    sentence, and a reachable answer always means no sentence at all — which is
    what lets the picker tell "no matches" from "Home Assistant is down" without
    a second field.

    :param tmp_path: pytest temporary directory.
    """
    cases: list[tuple[str, _Item, str, bool, bool]] = [
        ("matches", _replies(lambda: _json(_STATES)), "porch", False, False),
        ("no matches", _replies(lambda: _json(_STATES)), "nothing-here", False, True),
        ("unreachable", httpx.ConnectError("boom"), "", True, True),
        ("rejected", _json({"message": "nope"}, status=401), "", True, True),
    ]
    for name, answer, query, warns, empties in cases:
        async with _doorbell(tmp_path / name, answer) as doorbell:
            response = await doorbell.client.get(_ROUTE, params={"q": query})
        assert response.status_code == 200, name
        assert (_warning(response) is not None) is warns, name
        assert (_rows(response) == []) is empties, name


async def test_a_token_echoed_by_home_assistant_is_scrubbed(tmp_path: Path) -> None:
    """A secret Home Assistant echoes cannot reach the sentence the picker shows.

    :param tmp_path: pytest temporary directory.
    """
    answer = _json({"message": f"bad token {TOKEN}"}, status=401)
    async with _doorbell(tmp_path, answer) as doorbell:
        SECRET_SCRUBBER.set_secrets([TOKEN])
        response = await doorbell.client.get(_ROUTE)

    assert response.status_code == 200
    assert TOKEN not in response.text
