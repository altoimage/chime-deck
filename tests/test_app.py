"""Application factory, lifespan, routes and static serving (in-process, no sockets)."""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Final, cast

import httpx
import pytest
from fastapi import FastAPI
from ha_ws_fakes import TOKEN, FakeHaFactory, FakeHaPeer, make_client
from log_isolation import preserve_log_state
from mqtt_fakes import wait_until

from chime_deck import __version__
from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig
from chime_deck.entity_info import NO_LIVE_ENTITY_INFO
from chime_deck.ha import HaEntityInfo, HaEntityState, HomeAssistantWebSocketClient
from chime_deck.health import HealthRegistry, HealthStatus
from chime_deck.store import ConfigStore, EntityIn

pytestmark = pytest.mark.asyncio

_REVOKED_HA_TOKEN: Final = "revoked-token-0123456789abcdef"
"""A token the test peers reject; no test may log it."""

_NEW_HA_TOKEN: Final = "renewed-token-fedcba9876543210"
"""The token the test peers accept after a committed change; never logged."""

_ROTATED_HA_TOKEN: Final = "rotated-token-0011223344556677"
"""A second accepted token, used when one PATCH changes several ``ha.*`` keys."""

_HA_BASE_URL: Final = "http://homeassistant.local:8123"
"""The ``ha.base_url`` the rebuild tests seed."""


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the record factory after every test.

    The application lifespan applies the stored ``system.log_level`` to the root
    logger, so the level has to be put back or later tests inherit it.
    """
    with preserve_log_state():
        yield


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        """Start the clock at *now*.

        :param now: initial monotonic reading.
        """
        self.now = now

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now


def _build_app(
    tmp_path: Path,
    *,
    host: str = "127.0.0.1",
    static_dir: Path | None = None,
    clock: _Clock | None = None,
    seed_demo_sounds: bool = False,
    ha_websocket: HomeAssistantWebSocketClient | None = None,
) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and an injected static dir.

    :param tmp_path: pytest temporary directory.
    :param host: bind address recorded in the configuration.
    :param static_dir: directory the UI is served from; ``None`` means the repository default.
    :param clock: clock used for uptime and logging.
    :param seed_demo_sounds: whether the lifespan seeds the bundled demos; off by
        default so most tests do not depend on the asset set.
    :param ha_websocket: injected Home Assistant state client; ``None`` lets the
        lifespan build one from the stored settings.
    """
    config = ServerConfig(
        data_dir=tmp_path,
        host=host,
        static_dir=static_dir,
        seed_demo_sounds=seed_demo_sounds,
    )
    app = create_app(
        config,
        clock=clock if clock is not None else time.monotonic,
        ha_websocket=ha_websocket,
    )
    return app, cast(AppState, app.state.doorbell)


def _config(tmp_path: Path) -> ServerConfig:
    """Build the configuration the seeded-store tests write to before startup.

    :param tmp_path: pytest temporary directory.
    """
    return ServerConfig(
        data_dir=tmp_path, static_dir=tmp_path / "no-such-dist", seed_demo_sounds=False
    )


def _ha_steps(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return every captured ``startup: 6/7`` line, failures included.

    :param caplog: captured log records.
    """
    return [message for message in _messages(caplog) if _is_ha_step(message)]


def _is_ha_step(message: str) -> bool:
    """Return whether *message* is the Home Assistant startup step line.

    :param message: one captured log message.
    """
    return message.startswith("startup: 6/7")


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield a client bound to it.

    :param app: application under test.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the captured log messages in emission order.

    :param caplog: captured log records.
    """
    return [record.getMessage() for record in caplog.records]


def _patch_packaged_static(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Make ``importlib.resources.files`` resolve to a fake installed package.

    :param monkeypatch: patcher that installs the fake resource loader.
    :param root: directory standing in for ``site-packages/chime_deck``.
    """

    def fake_files(package: str) -> Path:
        """Return *root* whatever package was requested.

        :param package: package name the code under test asked for.
        """
        return root

    monkeypatch.setattr(importlib.resources, "files", fake_files)


def _patch_absent_source_tree(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Make the source-tree lookup resolve to a path that does not exist.

    :param monkeypatch: patcher that replaces the source-tree resolver.
    :param path: non-existent directory standing in for a checkout's ``web/dist``.
    """

    def fake_source_tree() -> Path:
        """Return *path* regardless of where the module really lives."""
        return path

    monkeypatch.setattr("chime_deck.config.source_tree_static_dir", fake_source_tree)


def _index_of(messages: list[str], needle: str) -> int:
    """Return the position of the first message starting with *needle*.

    :param messages: captured log messages.
    :param needle: prefix to look for.
    """
    for index, message in enumerate(messages):
        if message.startswith(needle):
            return index
    raise AssertionError(f"{needle!r} not found in {messages!r}")


async def test_placeholder_page_when_static_dir_is_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An absent frontend build renders the placeholder and warns once.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    static_dir = tmp_path / "no-such-dist"
    app, state = _build_app(tmp_path, static_dir=static_dir)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with _running(app) as client:
        response = await client.get("/")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert __version__ in response.text
        assert "web/dist" in response.text
        assert state.version == __version__

        # A second request must not log the warning again.
        assert (await client.get("/settings")).status_code == 200

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "no built UI found" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert warnings[0].name == "chime_deck.app"


async def test_packaged_ui_is_served_and_a_wheel_without_it_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wheel's ``static`` copy is served through the default resolution.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher installing the fake installed package.
    """
    package_root = tmp_path / "site-packages" / "chime_deck"
    (package_root / "static" / "assets").mkdir(parents=True)
    (package_root / "static" / "index.html").write_text(
        '<!doctype html><div id="app"></div>built dashboard', encoding="utf-8"
    )
    bundle = "console.log('dashboard');"
    (package_root / "static" / "assets" / "app.js").write_text(bundle, encoding="utf-8")
    _patch_packaged_static(monkeypatch, package_root)
    _patch_absent_source_tree(monkeypatch, tmp_path / "no-checkout" / "web" / "dist")

    # static_dir=None on purpose: the default resolution path is what ships.
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        root = await client.get("/")

        assert root.status_code == 200
        assert root.headers["content-type"].startswith("text/html")
        assert 'id="app"' in root.text
        assert "has not been built yet" not in root.text

        asset = await client.get("/assets/app.js")

        assert asset.status_code == 200
        assert "javascript" in asset.headers["content-type"]
        assert asset.text == bundle

    # A wheel built without the static data must degrade, not fail to start.
    bare_root = tmp_path / "bare-site-packages" / "chime_deck"
    bare_root.mkdir(parents=True)
    _patch_packaged_static(monkeypatch, bare_root)
    bare_app, _bare_state = _build_app(tmp_path)

    async with _running(bare_app) as client:
        placeholder = await client.get("/")

    assert placeholder.status_code == 200
    assert __version__ in placeholder.text
    assert "web/dist" in placeholder.text


async def test_health_reports_status_version_and_uptime(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``/api/health`` is exactly the design 05 §3 payload.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    clock = _Clock()
    app, _state = _build_app(tmp_path, static_dir=tmp_path / "no-such-dist", clock=clock)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with _running(app) as client:
        clock.now += 2.5
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = cast(dict[str, object], response.json())
    assert set(payload) == {"status", "version", "uptime_s"}
    assert payload["status"] == "ok"
    assert payload["version"] == __version__
    assert payload["uptime_s"] == 2.5


async def test_health_uptime_is_zero_for_a_frozen_clock(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A clock that does not move reports an uptime of zero, never a negative one.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    app, _state = _build_app(tmp_path, static_dir=tmp_path / "no-such-dist", clock=_Clock())
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with _running(app) as client:
        response = await client.get("/api/health")

    payload = cast(dict[str, object], response.json())
    assert payload["uptime_s"] == 0


async def test_api_and_ws_paths_never_reach_the_placeholder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Unknown API paths and a plain GET on ``/ws`` are 404, not the UI.

    The body is the HSD-91z1 error envelope, not Starlette's bare ``detail``.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    app, _state = _build_app(tmp_path, static_dir=tmp_path / "no-such-dist")
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with _running(app) as client:
        api_response = await client.get("/api/nope")
        ws_response = await client.get("/ws")

    for response in (api_response, ws_response):
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert "ChimeDeck" not in response.text
    assert api_response.json() == {
        "error": {"code": "not_found", "message": "not found", "detail": {}}
    }


async def test_startup_and_shutdown_are_ordered_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The six startup steps and three shutdown steps are logged in order.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    app, _state = _build_app(tmp_path, static_dir=tmp_path / "no-such-dist")
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        pass

    messages = _messages(caplog)
    startup = [_index_of(messages, f"startup: {step}/7") for step in range(1, 8)]
    assert startup == sorted(startup)

    shutdown = [_index_of(messages, f"shutdown: {step}/4") for step in range(1, 5)]
    assert shutdown == sorted(shutdown)
    assert shutdown[0] > startup[-1]

    hub_stopped = _index_of(messages, "state hub stopped")
    assert shutdown[0] < hub_stopped < shutdown[1]

    # _messages and caplog.records are parallel lists, so the index is shared.
    static_record = caplog.records[startup[3]]
    assert static_record.levelno == logging.WARNING
    assert "no built UI found" in messages[startup[3]]

    sound_index = _index_of(messages, "startup: sound library")
    assert caplog.records[sound_index].levelno == logging.INFO
    assert "sound library" in messages[sound_index]
    assert startup[2] < sound_index < startup[3]


async def test_loopback_bind_is_quiet_and_wildcard_bind_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Binding a network-reachable address warns on the ``5/7`` line (D13).

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    app, _state = _build_app(tmp_path, host="0.0.0.0", static_dir=tmp_path / "no-such-dist")
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        pass

    messages = _messages(caplog)
    listen_index = _index_of(messages, "startup: 5/7")
    assert caplog.records[listen_index].levelno == logging.WARNING
    assert "no authentication" in messages[listen_index]

    caplog.clear()
    loopback_app, _loopback_state = _build_app(
        tmp_path, host="127.0.0.1", static_dir=tmp_path / "no-such-dist"
    )
    async with loopback_app.router.lifespan_context(loopback_app):
        pass

    loopback_messages = _messages(caplog)
    loopback_index = _index_of(loopback_messages, "startup: 5/7")
    assert caplog.records[loopback_index].levelno == logging.INFO
    assert "no authentication" not in loopback_messages[loopback_index]


async def test_static_files_and_spa_fallback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A present ``web/dist`` is served, with SPA fallback and no path escape.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>built ui</body></html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('hi');", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("do not serve me", encoding="utf-8")

    app, _state = _build_app(tmp_path, static_dir=dist)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with _running(app) as client:
        root = await client.get("/")
        spa = await client.get("/settings")
        deep = await client.get("/settings/connection")
        asset = await client.get("/assets/app.js")
        api = await client.get("/api/nope")
        traversal = await client.get("/%2e%2e/secret.txt")

    assert root.status_code == 200
    assert "built ui" in root.text
    assert spa.status_code == 200
    assert "built ui" in spa.text
    assert deep.status_code == 200
    assert "built ui" in deep.text
    assert asset.status_code == 200
    assert "javascript" in asset.headers["content-type"]
    assert "console.log('hi');" in asset.text
    assert api.status_code == 404
    assert api.headers["content-type"].startswith("application/json")
    assert traversal.status_code == 404
    assert "do not serve me" not in traversal.text

    messages = _messages(caplog)
    static_index = _index_of(messages, "startup: 4/7")
    assert caplog.records[static_index].levelno == logging.INFO
    assert "serving static UI" in messages[static_index]


async def test_config_change_reaches_a_websocket_client(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A settings write through the store produces ``config_changed``.

    This is the acceptance criterion that must not be proven with an HTTP
    settings route (that route belongs to HSD-91z1): the store is mutated
    directly and the hub is driven one flush tick.
    """
    app, state = _build_app(tmp_path, static_dir=tmp_path / "no-such-dist")
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        client = state.hub.register()
        await state.store.set_setting("ui.theme", "light")
        await state.hub.flush_once()
        try:
            payload = await asyncio.wait_for(state.hub.next_payload(client), timeout=2.0)
        except TimeoutError:
            pytest.fail("the hub delivered no config_changed message within 2 s")
        message = cast(dict[str, object], json.loads(payload))

    assert message == {"type": "config_changed", "data": {"what": "settings"}}
    assert state.hub.config_version >= 1


async def test_disabled_speech_logs_info_and_reports_health_without_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``tts.default_engine: none`` is an INFO state, not a startup warning.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    config = ServerConfig(
        data_dir=tmp_path,
        static_dir=tmp_path / "no-such-dist",
        seed_demo_sounds=False,
    )
    async with ConfigStore(config.database_path) as seed:
        await seed.set_setting("tts.default_engine", "none")
    app = create_app(config)
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        assert state.speech is not None
        assert state.health.get("tts") is HealthStatus.DISABLED
        assert set(state.health.snapshot()) == {"ha", "mqtt", "audio", "tts"}
        assert state.health.snapshot()["tts"] == "disabled"

        disabled = [
            record for record in caplog.records if record.getMessage().startswith("TTS disabled")
        ]
        assert len(disabled) == 1
        assert disabled[0].levelno == logging.INFO

        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING and "TTS" in record.getMessage()
        ]
        assert warnings == []


async def test_speech_factory_failure_does_not_stop_startup(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken speech factory is logged and ignored, never fatal.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    :param monkeypatch: patcher used to break the factory.
    """

    async def boom(*args: object, **kwargs: object) -> None:
        """Fail like a broken engine factory would.

        :param args: ignored positional arguments.
        :param kwargs: ignored keyword arguments.
        """
        raise RuntimeError("boom")

    monkeypatch.setattr("chime_deck.app.build_speech_source", boom)
    app, state = _build_app(tmp_path, static_dir=tmp_path / "no-such-dist")
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        pass

    assert state.speech is None
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "could not prepare the speech engine" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "boom" in warnings[0].getMessage()


async def _subscribed_peer(factory: FakeHaFactory) -> FakeHaPeer:
    """Wait, bounded, until the client has subscribed and return that peer.

    :param factory: the scripted connection factory.
    :raises AssertionError: if no subscription appears within the bound.
    """
    await wait_until(
        lambda: bool(factory.connections) and bool(factory.connections[0].subscribed_ids()),
        timeout=5.0,
    )
    return factory.connections[0]


async def test_the_lifespan_subscribes_to_the_configured_tile_ids(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored tiles become the subscription, and the step line names the URL.

    The lifespan builds the client itself here (the production path); only the
    socket underneath it is replaced, so the ``constructed`` step line is the one
    under test.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    :param monkeypatch: patcher that installs the fake connection factory.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as seed:
        await seed.set_setting("ha.base_url", "http://homeassistant.local:8123")
        await seed.set_setting("ha.token", TOKEN)
        await seed.upsert_entity(EntityIn(entity_id="lock.front_door"))
        await seed.upsert_entity(EntityIn(entity_id="sensor.hall"))
    factory = FakeHaFactory()
    monkeypatch.setattr("chime_deck.ha.websocket.default_connection_factory", factory)
    app = create_app(config)
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        peer = await _subscribed_peer(factory)
        assert peer.subscribed_ids() == [["lock.front_door", "sensor.hall"]]
        assert isinstance(state.live_entities, HaEntityInfo)
        assert state.ha_websocket is not None

    ha_lines = _ha_steps(caplog)
    assert len(ha_lines) == 1
    assert "http://homeassistant.local:8123" in ha_lines[0]
    assert "2 entities" in ha_lines[0]
    assert TOKEN not in "\n".join(_messages(caplog))


async def test_ha_configured_after_startup_starts_watching_without_a_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first-run wizard stores the settings after startup; tiles are still watched.

    Without this a user who filled in the wizard would have to restart before the
    client existed and any tile was watched.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    :param monkeypatch: patcher that installs the fake connection factory.
    """
    config = _config(tmp_path)
    factory = FakeHaFactory()
    monkeypatch.setattr("chime_deck.ha.websocket.default_connection_factory", factory)
    app = create_app(config)
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        # Read the attribute into a local: asserting on the member itself narrows it for the
        # rest of the block, so the post-configuration ``is not None`` below would read as
        # unreachable even though the client really is created when the settings arrive.
        client_at_startup = state.ha_websocket
        assert client_at_startup is None
        assert state.health.get("ha") is HealthStatus.NOT_CONFIGURED
        assert state.live_entities is NO_LIVE_ENTITY_INFO
        assert len(_ha_steps(caplog)) == 1

        await state.store.set_setting("ha.base_url", "http://homeassistant.local:8123")
        await state.store.set_setting("ha.token", TOKEN)

        assert state.ha_websocket is not None
        assert state.health.get("ha") is HealthStatus.NO_ENTITIES
        assert factory.calls == []
        assert len(_ha_steps(caplog)) == 1
        assert any(message.startswith("home assistant:") for message in _messages(caplog))

        await state.store.upsert_entity(EntityIn(entity_id="lock.front_door"))
        await wait_until(
            lambda: (
                bool(factory.connections)
                and factory.connections[-1].subscribed_ids()[-1:] == [["lock.front_door"]]
            ),
            timeout=5.0,
        )
        await wait_until(lambda: state.health.get("ha") is HealthStatus.CONNECTED, timeout=5.0)
        assert isinstance(state.live_entities, HaEntityInfo)


async def test_a_tile_added_after_startup_is_watched(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A tile created after startup reaches the live subscription, without a restart.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as seed:
        await seed.upsert_entity(EntityIn(entity_id="lock.front_door"))
    factory = FakeHaFactory()
    app = create_app(config, ha_websocket=make_client(factory))
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        peer = await _subscribed_peer(factory)
        await state.store.upsert_entity(EntityIn(entity_id="light.hall"))
        await wait_until(
            lambda: peer.subscribed_ids()[-1:] == [["lock.front_door", "light.hall"]],
            timeout=5.0,
        )


async def test_a_tile_edit_resubscribes_with_the_new_entity_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A PATCH that renames a tile's entity id resubscribes to the new id.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as seed:
        await seed.upsert_entity(EntityIn(entity_id="sensor.old"))
    factory = FakeHaFactory()
    app = create_app(config, ha_websocket=make_client(factory))
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        peer = await _subscribed_peer(factory)
        stored = (await state.store.list_entities())[0]
        await state.store.upsert_entity(
            EntityIn(
                entity_id="sensor.new",
                id=stored.id,
                display_name=stored.display_name,
                icon=stored.icon,
                accent_color=stored.accent_color,
                show_attributes=stored.show_attributes,
                state_tones=stored.state_tones,
                hide_when_unavailable=stored.hide_when_unavailable,
                colour_by_state=stored.colour_by_state,
            )
        )
        await wait_until(lambda: peer.subscribed_ids()[-1:] == [["sensor.new"]], timeout=5.0)


async def test_deleting_the_last_tile_reports_no_entities_not_not_configured(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty tile list after a delete reads ``no_entities``, not ``not_configured``.

    The client still exists and is still started, so the header must not claim the
    integration was never configured. The client is built by the lifespan (the
    production path) because the app's health registry is what the header reads.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    :param monkeypatch: patcher that installs the fake connection factory.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as seed:
        await seed.set_setting("ha.base_url", "http://homeassistant.local:8123")
        await seed.set_setting("ha.token", TOKEN)
        await seed.upsert_entity(EntityIn(entity_id="lock.front_door"))
    factory = FakeHaFactory()
    monkeypatch.setattr("chime_deck.ha.websocket.default_connection_factory", factory)
    app = create_app(config)
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        peer = await _subscribed_peer(factory)
        await wait_until(lambda: state.health.get("ha") is HealthStatus.CONNECTED, timeout=5.0)
        stored = (await state.store.list_entities())[0]
        await state.store.delete_entity(stored.id)
        await wait_until(lambda: state.health.get("ha") is HealthStatus.NO_ENTITIES, timeout=5.0)
        assert state.ha_websocket is not None
        # An empty list closes the socket instead of subscribing to every entity,
        # so the fake records no further subscription and the peer is closed.
        assert peer.subscribed_ids() == [["lock.front_door"]]
        assert peer.closed is True


async def test_the_two_off_states_are_distinct_in_the_health_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``not_configured`` and ``no_entities`` say different things about Home Assistant.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher that installs the fake connection factory.
    """
    unconfigured_app, unconfigured_state = _build_app(
        tmp_path / "a", static_dir=tmp_path / "no-such-dist"
    )
    configured_config = _config(tmp_path / "b")
    async with ConfigStore(configured_config.database_path) as seed:
        await seed.set_setting("ha.base_url", "http://homeassistant.local:8123")
        await seed.set_setting("ha.token", TOKEN)
    factory = FakeHaFactory()
    monkeypatch.setattr("chime_deck.ha.websocket.default_connection_factory", factory)
    configured_app = create_app(configured_config)
    configured_state = cast(AppState, configured_app.state.doorbell)

    async with unconfigured_app.router.lifespan_context(unconfigured_app):
        unconfigured_message = unconfigured_state.health.message()
    async with configured_app.router.lifespan_context(configured_app):
        configured_message = configured_state.health.message()

    assert unconfigured_message["ha"] == "not_configured"
    assert configured_message["ha"] == "no_entities"
    assert unconfigured_message["ha"] != configured_message["ha"]
    assert "tiles" in configured_message["detail"]
    assert "tiles" not in unconfigured_message.get("detail", "")


async def test_a_pushed_state_event_becomes_an_entity_state_frame(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One websocket state event reaches a connected hub client with all seven keys.

    The presentation block travels on the delta too, so the tile's word and unit
    cannot lag behind its colour (HSD-7oto).

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as seed:
        await seed.upsert_entity(EntityIn(entity_id="lock.front_door"))
    factory = FakeHaFactory()
    app = create_app(config, ha_websocket=make_client(factory))
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")
    epoch = 1767225600.0

    async with app.router.lifespan_context(app):
        peer = await _subscribed_peer(factory)
        audience = state.hub.register()
        peer.push_states(
            {"lock.front_door": {"s": "unlocked", "a": {"battery_level": 91}, "lc": epoch}}
        )
        try:
            payload = await asyncio.wait_for(state.hub.next_payload(audience), 5.0)
        except TimeoutError:
            pytest.fail("the hub delivered no entity_state message within 5 s")

    assert json.loads(payload) == {
        "type": "entity_state",
        "data": {
            "entity_id": "lock.front_door",
            "state": "unlocked",
            "attributes": {"battery_level": 91},
            "last_changed": datetime.fromtimestamp(epoch, tz=UTC).isoformat(),
            "selected": False,
            "displayed_state": "Unlocked",
            "unit_of_measurement": None,
            "tone": "alert",
            "attribute_hints": [],
        },
    }


async def test_an_entity_write_resubscribes_with_the_new_tile(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A committed tile change drops the old subscription and adds the new id.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as seed:
        await seed.upsert_entity(EntityIn(entity_id="lock.front_door"))
    factory = FakeHaFactory()
    app = create_app(config, ha_websocket=make_client(factory))
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        peer = await _subscribed_peer(factory)
        await state.store.upsert_entity(EntityIn(entity_id="light.hall"))
        await wait_until(lambda: len(peer.subscribed_ids()) >= 2, timeout=5.0)
        assert peer.subscribed_ids()[1] == ["lock.front_door", "light.hall"]
        assert "unsubscribe_events" in peer.commands()


async def test_shutdown_closes_the_home_assistant_client(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The lifespan owns the client, so the lifespan closes it and its peers.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as seed:
        await seed.upsert_entity(EntityIn(entity_id="lock.front_door"))
    factory = FakeHaFactory()
    client = make_client(factory)
    app = create_app(config, ha_websocket=client)
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        await wait_until(lambda: client.connected, timeout=5.0)
        # Same reason as above: asserting on ``client.connected`` itself narrows it to True for
        # the rest of the function, so the post-shutdown ``is False`` would read as unreachable.
        connected_before_shutdown = client.connected
        assert connected_before_shutdown is True

    assert state.ha_websocket is client
    assert client.connected is False
    assert factory.connections
    assert all(peer.closed for peer in factory.connections)
    assert [message for message in _messages(caplog) if "Task was destroyed" in message] == []


async def test_an_unconfigured_install_builds_no_client(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With no token stored the step is an INFO line and no client exists.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    app, state = _build_app(tmp_path, static_dir=tmp_path / "no-such-dist")
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        assert state.ha_websocket is None
        assert state.live_entities is NO_LIVE_ENTITY_INFO

    ha_lines = [record for record in caplog.records if _is_ha_step(record.getMessage())]
    assert len(ha_lines) == 1
    assert ha_lines[0].levelno == logging.INFO
    assert "not configured" in ha_lines[0].getMessage()


async def test_no_startup_or_shutdown_line_contains_the_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A configured install logs every step without ever naming the token.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as seed:
        await seed.set_setting("ha.base_url", "http://homeassistant.local:8123")
        await seed.set_setting("ha.token", TOKEN)
        await seed.upsert_entity(EntityIn(entity_id="lock.front_door"))
    factory = FakeHaFactory()
    app = create_app(config, ha_websocket=make_client(factory))
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        await _subscribed_peer(factory)

    messages = "\n".join(_messages(caplog))
    assert "startup: 6/7" in messages
    assert "shutdown: 2/4" in messages
    assert TOKEN not in messages


async def test_an_unusable_base_url_warns_and_startup_completes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A stored address the client rejects is a WARNING, not a failed startup.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as seed:
        # The store's own pattern accepts this; the client rejects the query string,
        # which is the "configured but unusable" state this test pins.
        await seed.set_setting("ha.base_url", "http://homeassistant.local:8123/?x=1")
        await seed.set_setting("ha.token", TOKEN)
    app = create_app(config)
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with _running(app) as client:
        assert state.ha_websocket is None
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert state.live_entities is NO_LIVE_ENTITY_INFO
    ha_lines = [record for record in caplog.records if _is_ha_step(record.getMessage())]
    assert len(ha_lines) == 1
    assert ha_lines[0].levelno == logging.WARNING
    assert "ha.base_url" in ha_lines[0].getMessage()


async def test_an_injected_never_opened_client_is_closed_silently(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``aclose()`` on a client the lifespan never had to open is a no-op.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    factory = FakeHaFactory()
    client = make_client(factory)
    app, state = _build_app(tmp_path, static_dir=tmp_path / "no-such-dist", ha_websocket=client)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        assert state.ha_websocket is client
        assert client.connected is False

    # Nothing was subscribed to, so no socket was ever opened and no task ran.
    assert factory.connections == []
    messages = _messages(caplog)
    assert [message for message in messages if "Task was destroyed" in message] == []
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert [message for message in warnings if "Home Assistant" in message] == []
    assert [message for message in warnings if "did not stop cleanly" in message] == []


def _ha_error_mentions_rejection(state: AppState) -> bool:
    """Return whether the header says the stored token was rejected.

    :param state: the running application's shared state.
    """
    if state.health.get("ha") is not HealthStatus.ERROR:
        return False
    detail = state.health.message().get("detail", "")
    return "rejected" in detail or "401" in detail


def _subscribed_after(factory: FakeHaFactory, previous: FakeHaPeer) -> FakeHaPeer | None:
    """Return the newest peer that subscribed after *previous*, if any.

    A rebuild opens a brand-new connection, so "the new peer" cannot be
    ``connections[-1]`` at a fixed moment: the client subscribes from its own
    supervisor task. The identity check on *previous* keeps the helper honest
    when the factory also handed out short-lived live-data connections.

    :param factory: the scripted connection factory.
    :param previous: the peer of the generation being replaced.
    """
    subscribed = [
        peer for peer in factory.connections if peer is not previous and peer.subscribed_ids()
    ]
    return subscribed[-1] if subscribed else None


async def test_saving_a_new_ha_token_rebuilds_the_client_without_a_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A committed ``ha.token`` change rebuilds the running client in place.

    The acceptance criterion for cd-80m2: a revoked token is replaced with a good
    one through the store's real event bus, with no restart, and the tiles resume
    live data on the new generation.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as seed:
        await seed.set_setting("ha.base_url", _HA_BASE_URL)
        await seed.set_setting("ha.token", _REVOKED_HA_TOKEN)
        await seed.upsert_entity(EntityIn(entity_id="lock.front_door"))
    factory = FakeHaFactory(partial(FakeHaPeer, accepted_tokens={_NEW_HA_TOKEN}))
    app = create_app(
        config, ha_websocket_factory=partial(HomeAssistantWebSocketClient, connect=factory)
    )
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        await wait_until(lambda: _ha_error_mentions_rejection(state), timeout=5.0)
        old_client = state.ha_websocket
        old_peer = factory.connections[0]
        old_live = state.live_entities
        assert old_client is not None

        # The store's own bus delivers the event; nothing here calls the handler.
        await state.store.set_setting("ha.token", _NEW_HA_TOKEN)

        assert state.ha_websocket is not old_client
        assert old_peer.closed is True
        assert old_client.connected is False
        await wait_until(lambda: _subscribed_after(factory, old_peer) is not None, timeout=5.0)
        new_peer = _subscribed_after(factory, old_peer)
        assert new_peer is not None
        assert new_peer.sent[0] == {"type": "auth", "access_token": _NEW_HA_TOKEN}
        assert new_peer.subscribed_ids() == [["lock.front_door"]]
        assert old_peer.subscribed_ids() == []
        await wait_until(lambda: state.health.get("ha") is HealthStatus.CONNECTED, timeout=5.0)
        assert state.live_entities is not old_live
        assert isinstance(state.live_entities, HaEntityInfo)

        # Tiles resume live data: a state pushed through the new peer reaches the hub.
        await state.hub.flush_once()
        audience = state.hub.register()
        new_peer.push_states({"lock.front_door": {"s": "unlocked", "a": {}, "lc": 0}})
        try:
            payload = await asyncio.wait_for(state.hub.next_payload(audience), 5.0)
        except TimeoutError:
            pytest.fail("the hub delivered no message within 5 s after the rebuild")
        data = cast("dict[str, object]", json.loads(payload)["data"])
        assert data["entity_id"] == "lock.front_door"
        assert data["state"] == "unlocked"

    messages = "\n".join(_messages(caplog))
    assert _NEW_HA_TOKEN not in messages
    assert _REVOKED_HA_TOKEN not in messages


async def test_saving_a_rejected_ha_token_reports_401_instead_of_keeping_the_old_client(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A new token the peer rejects closes the old client and reports the rejection.

    Acceptance criterion 3: the header must say the token was rejected rather
    than keep serving on stale credentials behind a green dot.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as seed:
        await seed.set_setting("ha.base_url", _HA_BASE_URL)
        await seed.set_setting("ha.token", _NEW_HA_TOKEN)
        await seed.upsert_entity(EntityIn(entity_id="lock.front_door"))
    factory = FakeHaFactory(partial(FakeHaPeer, accepted_tokens={_NEW_HA_TOKEN}))
    app = create_app(
        config, ha_websocket_factory=partial(HomeAssistantWebSocketClient, connect=factory)
    )
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        await wait_until(lambda: state.health.get("ha") is HealthStatus.CONNECTED, timeout=5.0)
        old_client = state.ha_websocket
        old_peer = factory.connections[0]
        assert old_client is not None

        await state.store.set_setting("ha.token", _REVOKED_HA_TOKEN)

        assert state.ha_websocket is not old_client
        assert state.ha_websocket is not None
        assert old_peer.closed is True
        await wait_until(lambda: _ha_error_mentions_rejection(state), timeout=5.0)
        assert "rejected" in state.health.message().get("detail", "")
        # The new generation is installed even though it cannot authenticate; the
        # header, not a missing seam, is what tells the operator.
        assert isinstance(state.live_entities, HaEntityInfo)

    assert _REVOKED_HA_TOKEN not in "\n".join(_messages(caplog))


async def test_clearing_ha_token_tears_the_client_down_without_a_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``ha.token`` cleared to ``""`` stops the client and reports not configured.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as seed:
        await seed.set_setting("ha.base_url", _HA_BASE_URL)
        await seed.set_setting("ha.token", TOKEN)
        await seed.upsert_entity(EntityIn(entity_id="lock.front_door"))
    factory = FakeHaFactory()
    app = create_app(
        config, ha_websocket_factory=partial(HomeAssistantWebSocketClient, connect=factory)
    )
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with app.router.lifespan_context(app):
        await wait_until(lambda: state.health.get("ha") is HealthStatus.CONNECTED, timeout=5.0)
        old_peer = factory.connections[0]

        await state.store.set_setting("ha.token", "")

        assert state.ha_websocket is None
        assert old_peer.closed is True
        assert state.live_entities is NO_LIVE_ENTITY_INFO
        assert state.health.get("ha") is HealthStatus.NOT_CONFIGURED


async def test_one_settings_patch_with_both_ha_keys_rebuilds_exactly_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One ``PATCH`` that changes both ``ha.*`` keys builds exactly one new client.

    The store emits one event per written key, so without the generation guard a
    single save would rebuild twice and open and close a socket for nothing.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    config = _config(tmp_path)
    async with ConfigStore(config.database_path) as seed:
        await seed.set_setting("ha.base_url", _HA_BASE_URL)
        await seed.set_setting("ha.token", _NEW_HA_TOKEN)
        await seed.upsert_entity(EntityIn(entity_id="lock.front_door"))
    factory = FakeHaFactory(partial(FakeHaPeer, accepted_tokens={_NEW_HA_TOKEN, _ROTATED_HA_TOKEN}))
    constructions: list[str] = []

    def build(
        *,
        base_url: str,
        token: str,
        health: HealthRegistry,
        on_state_change: Callable[[HaEntityState], None],
    ) -> HomeAssistantWebSocketClient:
        """Build one client through the real class, recording each construction.

        :param base_url: the stored ``ha.base_url``.
        :param token: the stored ``ha.token``; recorded, never logged.
        :param health: the app's own health registry.
        :param on_state_change: the app's state callback.
        """
        constructions.append(token)
        return HomeAssistantWebSocketClient(
            base_url=base_url,
            token=token,
            health=health,
            on_state_change=on_state_change,
            connect=factory,
        )

    app = create_app(config, ha_websocket_factory=build)
    state = cast(AppState, app.state.doorbell)
    caplog.set_level(logging.INFO, logger="chime_deck")

    async with _running(app) as client:
        await wait_until(lambda: state.health.get("ha") is HealthStatus.CONNECTED, timeout=5.0)
        assert constructions == [_NEW_HA_TOKEN]

        response = await client.patch(
            "/api/settings",
            json={
                "ha.base_url": "http://homeassistant.lan:9123",
                "ha.token": _ROTATED_HA_TOKEN,
            },
        )

        assert response.status_code == 200
        # Exact equality: the initial client plus exactly ONE rebuild.
        assert constructions == [_NEW_HA_TOKEN, _ROTATED_HA_TOKEN]
        await wait_until(lambda: state.health.get("ha") is HealthStatus.CONNECTED, timeout=5.0)
        assert constructions == [_NEW_HA_TOKEN, _ROTATED_HA_TOKEN]
