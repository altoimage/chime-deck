"""MQTT HTTP API: the discovery re-announcement route, fully offline.

Every test builds the real application through :func:`create_app`, runs the real
lifespan and speaks to it through ``httpx.ASGITransport``, so no socket is
opened. The broker is the :class:`~mqtt_fakes.FakeBroker` installed by
``tests/conftest.py``: a CONNACK is fired by hand, so "connected", "offline" and
"the broker refused this topic" are states a test chooses rather than races.

The route's contract is the JSON body, not the status code alone: HTTP 200
carries the verdict in ``ok``, exactly like ``POST /api/settings/test/*``.

Owning ticket: HSD-bq76.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state
from mqtt_fakes import FakeBroker, FakeClient, wait_until

from chime_deck.api import ErrorEnvelope
from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig
from chime_deck.mqtt import (
    ConnectHandler,
    DisconnectHandler,
    MessageHandler,
    MqttClient,
    MqttSettings,
    discovery_topic,
)
from chime_deck.store import Button, ButtonIn, ConfigStore, SoundIn
from chime_deck.wiring import Wiring

pytestmark = pytest.mark.asyncio

_REQUEST_TIMEOUT_S = 5.0
"""Bound for one in-process request; nothing here may hang the suite."""

INSTANCE: str = "default"
"""Instance id the default test settings use, and therefore the topic tree."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Keep the root logger isolated per test.

    The application lifespan applies the stored ``system.log_level`` to the root
    logger, so the level has to be put back or later tests inherit it.
    """
    with preserve_log_state():
        yield


def _config(tmp_path: Path) -> ServerConfig:
    """Return the server configuration every test in this module uses.

    :param tmp_path: pytest temporary directory, used as the data directory.
    """
    return ServerConfig(data_dir=tmp_path, static_dir=tmp_path / "no-dist", seed_demo_sounds=False)


def _build_app(tmp_path: Path, *, wiring: Wiring | None = None) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory.
    :param wiring: devices to inject; ``None`` keeps the conftest's offline wiring.
    """
    config = _config(tmp_path)
    app = create_app(config) if wiring is None else create_app(config, wiring=wiring)
    return app, cast(AppState, app.state.doorbell)


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield a client bound to it.

    :param app: application under test.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _body(response: httpx.Response) -> dict[str, object]:
    """Return one response body as a plain mapping.

    :param response: response from the re-announcement route.
    """
    return cast("dict[str, object]", response.json())


def _error_message(response: httpx.Response) -> str:
    """Return the ``error`` prose of one re-announcement response.

    :param response: response from the re-announcement route.
    """
    message = _body(response)["error"]
    assert isinstance(message, str)
    return message


async def _republish(client: httpx.AsyncClient) -> httpx.Response:
    """POST the re-announcement request, bounded so a bug cannot hang the suite.

    :param client: client bound to the running app.
    """
    return await asyncio.wait_for(client.post("/api/mqtt/republish"), timeout=_REQUEST_TIMEOUT_S)


async def _new_button(store: ConfigStore, name: str, slug: str) -> None:
    """Create one enabled sound button through the running store.

    The slug is explicit so the discovery topic is predictable without deriving
    it from the display name.

    :param store: the running application's configuration store.
    :param name: button display name.
    :param slug: button slug.
    """
    sound = await store.create_sound(
        SoundIn(name=f"{name} sound", source_type="file", path=f"{slug}.mp3")
    )
    await store.upsert_button(
        ButtonIn(name=name, action_type="sound", sound_id=sound.id, slug=slug)
    )


def _config_topic(slug: str) -> str:
    """Return the discovery topic of *slug* for the default test settings.

    :param slug: button slug.
    """
    return discovery_topic(MqttSettings(host="broker.test", instance_id=INSTANCE), slug)


def _published_topics(client: FakeClient) -> list[str]:
    """Return every topic the fake client recorded a successful publish for.

    :param client: the fake client the application built.
    """
    return [topic for topic, _payload, _qos, _retain in client.published]


async def _connected(fake_broker: FakeBroker, state: AppState) -> FakeClient:
    """Fire one CONNACK and wait until the bridge reports it connected.

    :param fake_broker: broker holding the client the app built.
    :param state: live application state, for the bridge under test.
    """
    client = fake_broker.clients[0]
    fake_broker.connect(client)
    await wait_until(lambda: state.chime.bridge is not None and state.chime.bridge.connected)
    return client


async def test_republish_announces_every_button(tmp_path: Path, fake_broker: FakeBroker) -> None:
    """A connected broker gets every enabled payload again, and ``ok`` says so.

    The response carries exactly the five documented keys, so a client can rely
    on them all being present.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the application's client routes through.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _new_button(state.store, "Front Door", "front-door")
        await _new_button(state.store, "Back Door", "back-door")
        fake_client = await _connected(fake_broker, state)
        response = await _republish(client)

    assert response.status_code == 200
    body = _body(response)
    assert set(body) == {"ok", "announced", "queued", "failed", "error"}
    assert body["ok"] is True
    assert body["announced"] == 2
    assert body["queued"] is False
    assert body["failed"] == 0
    assert body["error"] is None
    topics = _published_topics(fake_client)
    assert _config_topic("front-door") in topics
    assert _config_topic("back-door") in topics


async def test_republish_with_no_buttons_is_a_successful_no_op(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """There is nothing to announce, which is a successful run reporting zero.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker the application's client routes through.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _connected(fake_broker, state)
        response = await _republish(client)

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is True
    assert body["announced"] == 0
    assert body["queued"] is False
    assert body["failed"] == 0
    assert body["error"] is None


async def test_republish_with_no_buttons_is_a_no_op_while_the_broker_is_offline(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """With nothing to announce the broker being down is irrelevant, not "queued".

    Nothing was buffered, so claiming a queued announcement would be a nonsense
    sentence ("the 0 discovery payload(s) were queued") in the settings page.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker that deliberately never fires a CONNACK here.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await _republish(client)

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is True
    assert body["announced"] == 0
    assert body["queued"] is False
    assert body["failed"] == 0
    assert body["error"] is None


async def test_republish_reports_an_unconfigured_broker_verbatim(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """A cleared ``mqtt.host`` is reported by name, and nothing is announced.

    A blank host is a legal "not configured" state rather than a rejection, so
    the route has to name the setting itself instead of waiting for an exception.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker whose client count must not grow.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await state.store.set_setting("mqtt.host", "")
        response = await _republish(client)

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is False
    assert body["announced"] == 0
    assert "mqtt.host" in _error_message(response)
    assert len(fake_broker.clients) == 1


async def test_republish_reports_a_bridge_that_could_not_start(
    tmp_path: Path, offline_wiring: Wiring
) -> None:
    """A generation that never came up is named, with the health reason attached.

    ``MqttBridge.start`` contains the raising client factory itself and records
    the failure in the health registry, so the raising wiring supplies both the
    missing client and the detail this branch repeats. The pipeline only leaves
    no generation at all when the stored settings are unusable, and then the
    route's first step already answers with that exact message, so the missing
    generation is dropped here to reach the guard.

    :param tmp_path: pytest temporary directory.
    :param offline_wiring: the conftest wiring, whose fake speaker is kept so the
        app still opens no sound card.
    """
    wiring = replace(offline_wiring, mqtt_client_factory=_raising_client_factory)
    app, state = _build_app(tmp_path, wiring=wiring)

    async with _running(app) as client:
        # The raising factory already recorded "boom" in the health registry.
        state.chime._discovery = None
        response = await _republish(client)

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is False
    assert body["announced"] == 0
    message = _error_message(response)
    assert "the MQTT bridge is not running" in message
    assert "boom" in message
    # A whole sentence rather than a fragment: the failure, then what to check.
    assert "check mqtt.host" in message
    assert len(message.split()) >= 10


async def test_republish_reports_a_queued_announcement_when_the_broker_is_offline(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """Without a CONNACK the payloads are queued, and the prose says so.

    ``announced`` still counts the payloads handed to the bridge: they are
    buffered, not lost, so the honest answer is "queued", not "failed".

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker that deliberately never fires a CONNACK here.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _new_button(state.store, "Front Door", "front-door")
        await _new_button(state.store, "Back Door", "back-door")
        response = await _republish(client)

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is False
    assert body["queued"] is True
    assert body["announced"] == 2
    assert body["failed"] == 0
    assert "queued" in _error_message(response)


async def test_republish_reports_a_rejected_publish(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """A topic the broker refuses is counted, and the other payload still goes out.

    ``MqttBridge.publish`` contains the raising client call, so the discovery
    service never sees the failure: the bridge's own failure counter is the only
    witness, and the difference across the call is what the route reports.

    A republish publishes twice to a button's discovery topic — the HSD-bnb0
    blank that lets Home Assistant move the entity id, then the announcement —
    so a refused topic loses two payloads, not one.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker carrying the client that refuses one topic.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _new_button(state.store, "Front Door", "front-door")
        await _new_button(state.store, "Back Door", "back-door")
        fake_client = await _connected(fake_broker, state)
        rejected_topic = _config_topic("front-door")
        surviving_topic = _config_topic("back-door")
        # The connect-time birth republish must finish before it is counted.
        await wait_until(
            lambda: (
                rejected_topic in _published_topics(fake_client)
                and surviving_topic in _published_topics(fake_client)
            )
        )
        before_rejected = _published_topics(fake_client).count(rejected_topic)
        before_surviving = _published_topics(fake_client).count(surviving_topic)
        fake_client.publish_raises_for.add(rejected_topic)

        response = await _republish(client)

        topics = _published_topics(fake_client)

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is False
    assert body["announced"] == 2
    assert body["failed"] == 2
    assert body["queued"] is False
    assert "2 discovery payload(s) were not published" in _error_message(response)
    # The surviving topic gets the HSD-bnb0 blank and the announcement.
    assert topics.count(surviving_topic) == before_surviving + 2
    assert topics.count(rejected_topic) == before_rejected


async def test_republish_counts_a_refusal_and_a_raise_as_three_failures(
    tmp_path: Path, fake_broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused topic plus a raising payload is three lost payloads, not two.

    The two failure witnesses are disjoint. ``MqttBridge.publish`` contains the
    paho exception, so the refused slug still counts as ``announced`` and only
    the bridge's counter moves; ``republish_all`` records the slug whose
    ``_publish`` raised. The raise is forced by replacing the live discovery
    service's ``_serialise``, which *monkeypatch* puts back afterwards.

    A refused topic loses two payloads: the HSD-bnb0 blank and the announcement.
    The report counts lost payloads, not buttons, for that reason — the two are no
    longer comparable, so no ratio is quoted.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker carrying the client that refuses one topic.
    :param monkeypatch: fixture that restores ``_serialise`` after the test.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await _new_button(state.store, "Front Door", "front-door")
        await _new_button(state.store, "Back Door", "back-door")
        fake_client = await _connected(fake_broker, state)
        discovery = state.chime.discovery
        assert discovery is not None
        # The connect-time birth republish must finish before the two faults
        # are armed: it is the only witness that every slug reached the cache.
        await wait_until(
            lambda: "front-door" in discovery._published and "back-door" in discovery._published
        )
        fake_client.publish_raises_for.add(_config_topic("back-door"))
        serialise = discovery._serialise

        def raising_serialise(button: Button) -> str:
            if button.slug == "front-door":
                raise ValueError("serialise failed")
            return serialise(button)

        monkeypatch.setattr(discovery, "_serialise", raising_serialise)

        response = await _republish(client)

    assert response.status_code == 200
    body = _body(response)
    assert body["ok"] is False
    assert body["announced"] == 1
    assert body["queued"] is False
    assert body["failed"] == 3
    message = _error_message(response)
    # Both problems are named: the count of lost payloads, then the raising slug.
    assert "3 discovery payload(s) were not published" in message
    assert "the discovery payload for front-door could not be published" in message


async def test_republish_is_post_only_and_the_slash_less_path_is_canonical(
    tmp_path: Path,
) -> None:
    """Only ``POST`` reaches the route; a GET is claimed by the SPA fallback.

    ``PUT`` is the honest 405 probe, because the catch-all UI route owns ``GET``
    and answers the documented 404 envelope for a reserved ``/api/*`` path rather
    than a 405. The slash-less path is the one every other test proves answers 200.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        put = await client.put("/api/mqtt/republish")
        get = await client.get("/api/mqtt/republish")

    assert put.status_code == 405
    assert "POST" in put.headers["allow"]
    assert get.status_code == 404
    assert ErrorEnvelope.model_validate(_body(get)).error.code == "not_found"


def _raising_client_factory(
    *,
    client_id: str,
    clean_session: bool,
    on_connect: ConnectHandler,
    on_disconnect: DisconnectHandler,
    on_message: MessageHandler,
) -> MqttClient:
    """Refuse to build a paho client, so the MQTT generation cannot start.

    :param client_id: client id the bridge asked for; unused.
    :param clean_session: clean-session flag the bridge asked for; unused.
    :param on_connect: handler for a CONNACK; unused.
    :param on_disconnect: handler for a disconnect; unused.
    :param on_message: handler for every delivered message; unused.
    :raises RuntimeError: always, with the text the pipeline records in health.
    """
    raise RuntimeError("boom")
