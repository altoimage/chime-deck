"""Settings HTTP API: reads, writes, secret handling, events and error envelopes.

Every test drives the real application through ``httpx.ASGITransport`` (never a
live socket) and asserts on the documented JSON contract, including the HSD-91z1
error envelope. Secret assertions are positive controls: the ``[REDACTED]``
marker must be present, not merely the secret absent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state
from mqtt_fakes import FakeBroker

from chime_deck.api import (
    AudioProbeResult,
    ConnectionProbes,
    ErrorCode,
    ErrorEnvelope,
    HaProbeCandidate,
    HaProbeResult,
    MqttProbeResult,
    install_error_handlers,
)
from chime_deck.app import AppState, create_app
from chime_deck.config import REDACTED, SECRET_SCRUBBER, ServerConfig, install_log_scrubber
from chime_deck.health import HealthStatus
from chime_deck.store import SETTINGS, ConfigEvent, ConfigStore

pytestmark = pytest.mark.asyncio

TOKEN = "long-lived-token-0123456789abcdef"

_LOGGER = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Keep the root logger and the registered secrets isolated per test."""
    with preserve_log_state():
        yield


def _build_app(
    tmp_path: Path,
    *,
    probes: ConnectionProbes | None = None,
    probe_timeout: float | None = None,
) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory.
    :param probes: connection probes to inject; ``None`` keeps the default.
    :param probe_timeout: timeout to inject; ``None`` keeps the default.
    """
    config = ServerConfig(
        data_dir=tmp_path, static_dir=tmp_path / "no-dist", seed_demo_sounds=False
    )
    if probes is not None and probe_timeout is not None:
        app = create_app(config, probes=probes, probe_timeout=probe_timeout)
    elif probes is not None:
        app = create_app(config, probes=probes)
    elif probe_timeout is not None:
        app = create_app(config, probe_timeout=probe_timeout)
    else:
        app = create_app(config)
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


def _walk_strings(value: object) -> Iterator[str]:
    """Yield every string in a decoded JSON document, keys included.

    :param value: decoded JSON value.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in cast("Mapping[object, object]", value).items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from _walk_strings(item)


async def test_get_returns_every_non_secret_key_with_its_default(tmp_path: Path) -> None:
    """``GET /api/settings`` reports defaults and secret flags, and no secret value.

    The two D24 renderer tokens are write-only too, and so is the HSD-0jke shared
    token, so the frontend contract grows by that many booleans.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/settings")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    settings = cast("dict[str, object]", body["settings"])
    assert set(settings) == {key for key, spec in SETTINGS.items() if not spec.secret}
    assert settings["audio.volume"] == 100
    assert settings["audio.start_timeout_s"] == 25
    assert settings["ui.theme"] == "dark"
    assert settings["system.log_level"] == "info"
    assert body["secrets"] == {
        "ha.token": False,
        "mqtt.password": False,
        "server.admin_password_hash": False,
        "server.token": False,
        "tts.remote_tts.token": False,
        "tts.renderer.token": False,
    }
    assert set(SETTINGS) - set(settings) == {
        "ha.token",
        "mqtt.password",
        "server.admin_password_hash",
        "server.token",
        "tts.remote_tts.token",
        "tts.renderer.token",
    }


def _snapshot_settings(response: httpx.Response) -> dict[str, object]:
    """Return the ``settings`` object of a settings response.

    :param response: successful ``GET``/``PATCH`` response.
    """
    body = cast("dict[str, object]", response.json())
    return cast("dict[str, object]", body["settings"])


async def test_a_fresh_install_reports_the_registry_defaults(tmp_path: Path) -> None:
    """An unnamed fresh unit is "ChimeDeck" and its identity is still ``default``.

    Nothing is pinned until the unit is actually named, which is what keeps an
    upgrade from moving any existing entity (HSD-vwdy).

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/settings")

    assert response.status_code == 200
    settings = _snapshot_settings(response)
    assert settings["device.name"] == "ChimeDeck"
    assert settings["mqtt.instance_id"] == "default"


async def test_the_first_device_name_write_pins_the_instance_id(tmp_path: Path) -> None:
    """One ``device.name`` write echoes the name *and* the identity it pinned.

    The pin happens in the same transaction as the name, so a single echo shows
    both and the UI never displays a name with the old identity (HSD-vwdy).

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch("/api/settings", json={"device.name": "Hall Doorbell"})

        assert response.status_code == 200
        settings = _snapshot_settings(response)
        assert settings["device.name"] == "Hall Doorbell"
        assert settings["mqtt.instance_id"] == "hall-doorbell"
        assert await state.store.is_setting_stored("mqtt.instance_id") is True

        # A later GET agrees, so the pin is persisted rather than an echo illusion.
        get_settings = _snapshot_settings(await client.get("/api/settings"))
        assert get_settings["mqtt.instance_id"] == "hall-doorbell"


async def test_a_later_rename_leaves_the_pinned_instance_id_alone(tmp_path: Path) -> None:
    """Renaming the device changes the label only, never the identity.

    This is the ticket's headline guarantee: a rename must not move every Home
    Assistant entity (HSD-vwdy).

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={"device.name": "Hall Doorbell"})
        response = await client.patch("/api/settings", json={"device.name": "Kitchen"})

        assert response.status_code == 200
        settings = _snapshot_settings(response)
        assert settings["device.name"] == "Kitchen"
        assert settings["mqtt.instance_id"] == "hall-doorbell"


async def test_an_explicit_instance_id_in_the_same_patch_wins(tmp_path: Path) -> None:
    """A patch that sets both keys keeps the supplied identity.

    The pin only fills a gap: an operator who states the instance id gets it,
    whatever the name slugs to (HSD-vwdy).

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch(
            "/api/settings", json={"device.name": "Hall Doorbell", "mqtt.instance_id": "rig"}
        )

        assert response.status_code == 200
        settings = _snapshot_settings(response)
        assert settings["device.name"] == "Hall Doorbell"
        assert settings["mqtt.instance_id"] == "rig"


async def test_a_stored_instance_id_is_never_overwritten_by_a_name_write(tmp_path: Path) -> None:
    """An install that already stored its identity keeps it across a rename.

    The row exists before the name is written, which is the upgrade case of
    HSD-vwdy: an existing install is never re-identified by naming the device.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        await store.set_setting("mqtt.instance_id", "rig")
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch("/api/settings", json={"device.name": "Hall Doorbell"})

        assert response.status_code == 200
        settings = _snapshot_settings(response)
        assert settings["device.name"] == "Hall Doorbell"
        assert settings["mqtt.instance_id"] == "rig"


async def test_a_blank_stored_instance_id_is_pinned_too(tmp_path: Path) -> None:
    """A stored but empty instance id counts as "not pinned" and gets the slug.

    A blank id is the "MQTT cannot start" state, so filling it from the first
    name is a repair rather than an overwrite (HSD-vwdy).

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as store:
        await store.set_setting("mqtt.instance_id", "")
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch("/api/settings", json={"device.name": "Hall Doorbell"})

        assert response.status_code == 200
        assert _snapshot_settings(response)["mqtt.instance_id"] == "hall-doorbell"


async def test_setup_dismissed_defaults_to_false(tmp_path: Path) -> None:
    """The first-run wizard is not dismissed until someone says so (HSD-vl7t).

    A fresh install reports boolean ``false`` and not a missing key, so the
    dashboard's ``flag('ui.setup_dismissed', false)`` reads the same either way.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/settings")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    settings = cast("dict[str, object]", body["settings"])
    secrets = cast("dict[str, object]", body["secrets"])
    assert settings["ui.setup_dismissed"] is False
    assert "ui.setup_dismissed" not in secrets


async def test_setup_dismissed_round_trips(tmp_path: Path) -> None:
    """A PATCH echoes the dismissal and a later GET still reports it.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch("/api/settings", json={"ui.setup_dismissed": True})
        assert response.status_code == 200
        patched = cast("dict[str, object]", response.json())
        assert cast("dict[str, object]", patched["settings"])["ui.setup_dismissed"] is True

        get_body = cast("dict[str, object]", (await client.get("/api/settings")).json())
        assert cast("dict[str, object]", get_body["settings"])["ui.setup_dismissed"] is True


async def test_muted_defaults_to_false(tmp_path: Path) -> None:
    """A fresh install is not muted, and the key is visible to the UI (D28).

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/settings")

    assert response.status_code == 200
    body = cast("dict[str, object]", response.json())
    settings = cast("dict[str, object]", body["settings"])
    assert settings["audio.muted"] is False


async def test_muted_round_trips(tmp_path: Path) -> None:
    """A PATCH echoes the mute state and a later GET still reports it.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch("/api/settings", json={"audio.muted": True})
        assert response.status_code == 200
        patched = cast("dict[str, object]", response.json())
        assert cast("dict[str, object]", patched["settings"])["audio.muted"] is True

        get_body = cast("dict[str, object]", (await client.get("/api/settings")).json())
        assert cast("dict[str, object]", get_body["settings"])["audio.muted"] is True


@pytest.mark.parametrize("value", [1, "true"])
async def test_patch_rejects_a_non_boolean_mute_value(tmp_path: Path, value: object) -> None:
    """``audio.muted`` is a boolean, so ``1`` and ``\"true\"`` are type errors.

    :param tmp_path: pytest temporary directory.
    :param value: rejected non-boolean value.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch("/api/settings", json={"audio.muted": value})

    assert response.status_code == 422
    envelope = ErrorEnvelope.model_validate(cast("dict[str, object]", response.json()))
    assert envelope.error.code is ErrorCode.INVALID_SETTING
    assert "audio.muted" in envelope.error.message
    fields = cast("list[dict[str, object]]", envelope.error.detail["fields"])
    assert fields == [{"field": "audio.muted", "reason": "expects boolean"}]


async def test_the_scale_round_trips_and_a_non_step_is_refused(tmp_path: Path) -> None:
    """``ui.scale`` comes back as int 130, and 87 is a 422 naming the steps (HSD-nlbf).

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        accepted = await client.patch("/api/settings", json={"ui.scale": 130})
        get_body = cast("dict[str, object]", (await client.get("/api/settings")).json())
        rejected = await client.patch("/api/settings", json={"ui.scale": 87})

    assert accepted.status_code == 200
    stored = cast("dict[str, object]", get_body["settings"])["ui.scale"]
    assert stored == 130
    assert isinstance(stored, int)

    assert rejected.status_code == 422
    envelope = ErrorEnvelope.model_validate(cast("dict[str, object]", rejected.json()))
    assert envelope.error.code is ErrorCode.INVALID_SETTING
    assert "must be one of 85, 100, 115, 130, 150" in envelope.error.message


async def test_patch_stores_values_and_echoes_the_new_state(tmp_path: Path) -> None:
    """A PATCH writes every value, echoes it and a later GET agrees.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch(
            "/api/settings", json={"audio.volume": 33, "mqtt.tls": True, "ui.theme": "light"}
        )
        assert response.status_code == 200
        patched = cast("dict[str, object]", response.json())
        settings = cast("dict[str, object]", patched["settings"])
        assert settings["audio.volume"] == 33
        assert settings["mqtt.tls"] is True
        assert settings["ui.theme"] == "light"

        get_body = cast("dict[str, object]", (await client.get("/api/settings")).json())
        get_settings = cast("dict[str, object]", get_body["settings"])
        assert get_settings["audio.volume"] == 33
        assert get_settings["mqtt.tls"] is True
        assert get_settings["ui.theme"] == "light"

        assert await state.store.get_setting("audio.volume") == 33


_ENGINES_ROUTE = "/api/tts/engines"
"""The engine list the Speech panel reads."""

_DOWNLOAD_ROUTE = "/api/tts/voices/en_GB-alan-low/download"
"""A curated voice id that needs no real model to be refused."""


async def test_tts_default_engine_none_round_trips_and_reports_disabled_not_degraded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """D27: ``none`` is a stored choice the API round-trips, and health says disabled.

    The press path (``tests/test_app.py``) and the engine and voice routes
    (``tests/test_api_tts.py``) each seed the setting before startup and prove
    their own half. Neither covers the write path the Settings → Speech panel
    uses, nor that the *next* boot reads back what that panel stored:
    :func:`chime_deck.tts.speech.build_speech_source` resolves the engine
    only when it builds, so a live PATCH cannot show what a restart reports.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch("/api/settings", json={"tts.default_engine": "none"})
        assert response.status_code == 200
        patched = cast("dict[str, object]", response.json())
        assert cast("dict[str, object]", patched["settings"])["tts.default_engine"] == "none"

        get_body = cast("dict[str, object]", (await client.get("/api/settings")).json())
        assert cast("dict[str, object]", get_body["settings"])["tts.default_engine"] == "none"

        assert await state.store.get_setting("tts.default_engine") == "none"

    # A second app over the same data directory is the only vantage point on
    # what the next boot makes of that stored choice.
    restarted, restarted_state = _build_app(tmp_path)
    caplog.set_level(logging.INFO, logger="chime_deck")
    # Only the restart's log is under test: the first run's startup chatter
    # depends on the engines this host happens to have.
    caplog.clear()

    async with _running(restarted) as client:
        assert restarted_state.speech is not None
        assert restarted_state.health.get("tts") is HealthStatus.DISABLED
        assert restarted_state.health.snapshot()["tts"] == "disabled"
        assert restarted_state.health.get("tts") is not HealthStatus.DEGRADED

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

        engines_response = await client.get(_ENGINES_ROUTE)
        download_response = await client.post(_DOWNLOAD_ROUTE)

    assert engines_response.status_code == 200
    engines = cast("dict[str, object]", engines_response.json())
    assert engines["disabled"] is True
    assert engines["engines"] == []
    assert "switched off" in cast(str, engines["warning"])

    # Refused before anything is walked or fetched, so no host package can
    # change the answer.
    assert download_response.status_code == 422
    error = cast("dict[str, object]", cast("dict[str, object]", download_response.json())["error"])
    assert error["code"] == "voice_unavailable"


async def test_patch_persists_secrets_write_only(tmp_path: Path) -> None:
    """A secret can be written and is stored, but never comes back out.

    The two D24 renderer tokens are write-only too, and so is the HSD-0jke shared
    token, so the frontend contract grows by that many booleans.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch(
            "/api/settings", json={"ha.token": TOKEN, "mqtt.password": "hunter2"}
        )
        assert response.status_code == 200
        assert TOKEN not in response.text
        assert "hunter2" not in response.text
        body = cast("dict[str, object]", response.json())
        assert body["secrets"] == {
            "ha.token": True,
            "mqtt.password": True,
            "server.admin_password_hash": False,
            "server.token": False,
            "tts.remote_tts.token": False,
            "tts.renderer.token": False,
        }

        get_response = await client.get("/api/settings")
        get_body = cast("dict[str, object]", get_response.json())
        for value in _walk_strings(get_body):
            assert value != TOKEN
            assert value != "hunter2"
        # A raw-body scan as well: no key, no nesting and no serialisation
        # detail may smuggle the value back out.
        assert TOKEN not in get_response.text
        assert "hunter2" not in get_response.text

        # Write-only at the API level, not at the store level: the clients that
        # need the credential still read it back here.
        assert await state.store.get_setting("ha.token") == TOKEN
        assert await state.store.get_setting("mqtt.password") == "hunter2"


async def test_patch_null_clears_a_secret_and_empty_string_is_rejected(tmp_path: Path) -> None:
    """``null`` clears a stored secret; ``""`` is refused so it cannot be blanked by accident.

    The two D24 renderer tokens are write-only too, and so is the HSD-0jke shared
    token, so the frontend contract grows by that many booleans.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        assert (await client.patch("/api/settings", json={"ha.token": TOKEN})).status_code == 200

        cleared = await client.patch("/api/settings", json={"ha.token": None})
        assert cleared.status_code == 200
        cleared_body = cast("dict[str, object]", cleared.json())
        assert cleared_body["secrets"] == {
            "ha.token": False,
            "mqtt.password": False,
            "server.admin_password_hash": False,
            "server.token": False,
            "tts.remote_tts.token": False,
            "tts.renderer.token": False,
        }
        assert await state.store.has_secret("ha.token") is False

        rejected = await client.patch("/api/settings", json={"ha.token": ""})
        assert rejected.status_code == 422
        error = cast("dict[str, object]", cast("dict[str, object]", rejected.json())["error"])
        assert error["code"] == "invalid_setting"
        message = cast(str, error["message"])
        assert "ha.token" in message
        assert "send null to clear" in message
        assert await state.store.has_secret("ha.token") is False


async def test_null_clears_mqtt_password_too(tmp_path: Path) -> None:
    """Every secret behaves the same: ``null`` clears, ``""`` is rejected by name.

    The two D24 renderer tokens are write-only too, and so is the HSD-0jke shared
    token, so the frontend contract grows by that many booleans.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        stored = await client.patch("/api/settings", json={"mqtt.password": "hunter2"})
        assert stored.status_code == 200
        assert await state.store.has_secret("mqtt.password") is True

        cleared = await client.patch("/api/settings", json={"mqtt.password": None})
        assert cleared.status_code == 200
        cleared_body = cast("dict[str, object]", cleared.json())
        assert cleared_body["secrets"] == {
            "ha.token": False,
            "mqtt.password": False,
            "server.admin_password_hash": False,
            "server.token": False,
            "tts.remote_tts.token": False,
            "tts.renderer.token": False,
        }
        assert await state.store.has_secret("mqtt.password") is False

        rejected = await client.patch("/api/settings", json={"mqtt.password": ""})
        assert rejected.status_code == 422
        error = cast("dict[str, object]", cast("dict[str, object]", rejected.json())["error"])
        message = cast(str, error["message"])
        assert "mqtt.password" in message
        assert "send null to clear" in message
        assert await state.store.has_secret("mqtt.password") is False


async def test_saving_the_wizards_broker_keys_brings_the_bridge_up_authenticated(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """The wizard's step-2 key set authenticates the rebuilt bridge against the broker.

    wizard step 2 owns mqtt.username/mqtt.password (HSD-9xpm): saving the exact key
    set it sends stores the password write-only and the rebuilt generation is
    accepted by a broker that refuses anonymous clients.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker that refuses a client without the required credentials.
    """
    fake_broker.required_credentials = ("doorbell", "hunter2")
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch(
            "/api/settings",
            json={
                "mqtt.host": "broker.test",
                "mqtt.port": 1883,
                "mqtt.username": "doorbell",
                "mqtt.password": "hunter2",
            },
        )

        assert response.status_code == 200
        body = cast("dict[str, object]", response.json())
        settings = cast("dict[str, object]", body["settings"])
        assert settings["mqtt.username"] == "doorbell"
        assert cast("dict[str, object]", body["secrets"])["mqtt.password"] is True
        assert "hunter2" not in response.text

        get_response = await client.get("/api/settings")
        assert "hunter2" not in get_response.text

        assert len(fake_broker.clients) >= 2
        fake_broker.connect(fake_broker.clients[-1])
        for _attempt in range(200):
            if state.health.get("mqtt") is HealthStatus.CONNECTED:
                break
            await asyncio.sleep(0.01)
        assert state.health.get("mqtt") is HealthStatus.CONNECTED


async def test_the_mqtt_test_reports_auth_failure_as_auth_and_nothing_else(
    tmp_path: Path, fake_broker: FakeBroker
) -> None:
    """``auth_failed`` follows the broker's CONNACK, not the English sentence.

    wizard step 2 owns mqtt.username/mqtt.password (HSD-9xpm): the default probe
    set talks to the injected fake broker, so the anonymous attempt is refused and
    the same broker accepts the stored credentials afterwards.

    :param tmp_path: pytest temporary directory.
    :param fake_broker: broker that refuses a client without the required credentials.
    """
    fake_broker.required_credentials = ("doorbell", "hunter2")
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        refused = await client.post("/api/settings/test/mqtt")
        refused_body = cast("dict[str, object]", refused.json())
        assert refused.status_code == 200
        assert refused_body["ok"] is False
        assert refused_body["auth_failed"] is True

        stored = await client.patch(
            "/api/settings",
            json={"mqtt.username": "doorbell", "mqtt.password": "hunter2"},
        )
        assert stored.status_code == 200

        accepted = await client.post("/api/settings/test/mqtt")
        accepted_body = cast("dict[str, object]", accepted.json())
        assert accepted.status_code == 200
        assert accepted_body["ok"] is True
        assert accepted_body["auth_failed"] is False


async def test_empty_string_is_still_allowed_for_plain_strings(tmp_path: Path) -> None:
    """Ordinary strings may be blanked, unlike secrets.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch(
            "/api/settings", json={"audio.device_hint": "", "mqtt.username": ""}
        )

        assert response.status_code == 200
        assert await state.store.get_setting("audio.device_hint") == ""
        assert await state.store.get_setting("mqtt.username") == ""


def _drain(queue: asyncio.Queue[ConfigEvent]) -> list[ConfigEvent]:
    """Return every event queued so far without waiting.

    :param queue: queue subscribed to the store's event bus.
    """
    events: list[ConfigEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


@pytest.mark.parametrize(
    ("body", "names"),
    [
        ({"nope.nothing": 1}, ("nope.nothing",)),
        ({"nope.one": 1, "nope.two": "x"}, ("nope.one", "nope.two")),
    ],
)
async def test_patch_rejects_unknown_keys_by_name(
    tmp_path: Path, body: dict[str, object], names: tuple[str, ...]
) -> None:
    """An unregistered key is rejected by name and the envelope is the documented one.

    :param tmp_path: pytest temporary directory.
    :param body: patch body carrying the unknown keys.
    :param names: keys the message and detail must name.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch("/api/settings", json=body)

    assert response.status_code == 422
    payload = cast("dict[str, object]", response.json())
    envelope = ErrorEnvelope.model_validate(payload)
    assert envelope.error.code is ErrorCode.UNKNOWN_SETTING
    for name in names:
        assert name in envelope.error.message
    fields = cast("list[dict[str, object]]", envelope.error.detail["fields"])
    assert {field["field"] for field in fields} == set(names)


@pytest.mark.parametrize(
    ("key", "value"),
    [("audio.volume", 101), ("ui.theme", "purple"), ("audio.volume", "loud"), ("mqtt.port", 0)],
)
async def test_patch_rejects_bad_values_by_name(tmp_path: Path, key: str, value: object) -> None:
    """A bad value is rejected with ``invalid_setting`` and the key in the message.

    :param tmp_path: pytest temporary directory.
    :param key: setting under test.
    :param value: rejected value.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch("/api/settings", json={key: value})

    assert response.status_code == 422
    envelope = ErrorEnvelope.model_validate(cast("dict[str, object]", response.json()))
    assert envelope.error.code is ErrorCode.INVALID_SETTING
    assert key in envelope.error.message


async def test_failed_patch_writes_nothing(tmp_path: Path) -> None:
    """One bad value rejects the whole patch: no key is half-applied.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch(
            "/api/settings", json={"ui.theme": "light", "audio.volume": 101}
        )

        assert response.status_code == 422
        assert await state.store.get_setting("ui.theme") == "dark"

        get_body = cast("dict[str, object]", (await client.get("/api/settings")).json())
        assert cast("dict[str, object]", get_body["settings"])["ui.theme"] == "dark"


async def test_failed_patch_never_echoes_the_secret(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected patch carrying a token leaks it neither in the body nor in the log.

    The log check is a positive control: a token logged after the request must
    show up as :data:`REDACTED`, proving the scrubbing layer was in the path. The
    logged sentence has no ``token=`` shape, so only the registered literal can
    redact it — the patterns would not.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    caplog.set_level(logging.INFO)
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        # The lifespan registers the stored secrets, which clears anything
        # registered before startup, so register the token afterwards.
        install_log_scrubber()
        SECRET_SCRUBBER.set_secrets([TOKEN])

        response = await client.patch(
            "/api/settings", json={"ha.token": TOKEN, "audio.volume": 101}
        )

        assert response.status_code == 422
        assert TOKEN not in response.text
        assert await state.store.has_secret("ha.token") is False

    _LOGGER.info("rejected credential %s", TOKEN)
    assert TOKEN not in caplog.text
    assert REDACTED in caplog.text


async def test_system_log_level_is_normalised(tmp_path: Path) -> None:
    """The log level is stored lower-case, and an unknown level is refused.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        accepted = await client.patch("/api/settings", json={"system.log_level": "DEBUG"})
        rejected = await client.patch("/api/settings", json={"system.log_level": "chatty"})

    assert accepted.status_code == 200
    accepted_body = cast("dict[str, object]", accepted.json())
    assert cast("dict[str, object]", accepted_body["settings"])["system.log_level"] == "debug"

    assert rejected.status_code == 422
    envelope = ErrorEnvelope.model_validate(cast("dict[str, object]", rejected.json()))
    assert envelope.error.code is ErrorCode.INVALID_SETTING
    assert "system.log_level" in envelope.error.message


async def test_empty_patch_is_a_no_op(tmp_path: Path) -> None:
    """An empty patch answers 200 and changes nothing at all.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        before = await client.get("/api/settings")
        version_before = state.hub.config_version
        response = await client.patch("/api/settings", json={})
        after = await client.get("/api/settings")

    assert response.status_code == 200
    assert response.json() == before.json()
    assert after.json() == before.json()
    assert state.hub.config_version == version_before


async def test_non_object_body_is_a_validation_error(tmp_path: Path) -> None:
    """A non-object body is a ``validation_error`` naming ``body``, echoing nothing.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch("/api/settings", json=["secret-value"])

    assert response.status_code == 422
    assert "secret-value" not in response.text
    envelope = ErrorEnvelope.model_validate(cast("dict[str, object]", response.json()))
    assert envelope.error.code is ErrorCode.VALIDATION_ERROR
    fields = cast("list[dict[str, object]]", envelope.error.detail["fields"])
    assert {tuple(cast("list[object]", field["loc"])) for field in fields} == {("body",)}
    assert "body" in envelope.error.message


async def test_patch_publishes_config_changed_to_the_hub(tmp_path: Path) -> None:
    """One patch is one event burst: two settings events, one ``config_changed``.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        hub_client = state.hub.register()
        queue: asyncio.Queue[ConfigEvent] = state.store.events.subscribe_queue()
        # Park the background flush loop for the duration: with a one-hour
        # interval no tick can flush the patch behind the test's back, so the
        # single ``flush_once()`` below is the only flush that can happen.
        original_interval = state.hub._flush_interval
        state.hub._flush_interval = 3600.0
        try:
            # Let any tick that was already in flight land before the patch.
            await asyncio.sleep(0.15)
            assert hub_client.queue.empty()
            version_before = state.hub.config_version

            response = await client.patch(
                "/api/settings", json={"ui.theme": "light", "audio.volume": 55}
            )
            assert response.status_code == 200

            await state.hub.flush_once()
            payload = await asyncio.wait_for(state.hub.next_payload(hub_client), timeout=2.0)
            assert json.loads(payload) == {
                "type": "config_changed",
                "data": {"what": "settings"},
            }

            # Exactly one message: coalescing must not deliver a second copy.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(state.hub.next_payload(hub_client), timeout=0.05)

            events = _drain(queue)
            assert [event.name for event in events] == ["config.settings_changed"] * 2
            assert {event.payload["keys"] for event in events} == {
                ("ui.theme",),
                ("audio.volume",),
            }
            assert state.hub.config_version >= version_before + 1
        finally:
            state.hub._flush_interval = original_interval
            state.store.events.unsubscribe_queue(queue)
            state.hub.unregister(hub_client)


@pytest.mark.parametrize(
    ("method", "path", "payload", "code", "needs_allow"),
    [
        ("PATCH", "/api/settings", {"ui.theme": "purple"}, ErrorCode.INVALID_SETTING, False),
        ("GET", "/api/not-a-route", None, ErrorCode.NOT_FOUND, False),
        ("PUT", "/api/settings", None, ErrorCode.METHOD_NOT_ALLOWED, True),
        ("DELETE", "/api/health", None, ErrorCode.METHOD_NOT_ALLOWED, False),
    ],
)
async def test_error_envelopes_match_the_documented_schema(
    tmp_path: Path,
    method: str,
    path: str,
    payload: dict[str, object] | None,
    code: ErrorCode,
    needs_allow: bool,
) -> None:
    """Every failing request answers with a valid, single-line error envelope.

    :param tmp_path: pytest temporary directory.
    :param method: HTTP method to send.
    :param path: request path.
    :param payload: JSON body, or ``None`` for no body.
    :param code: expected machine-readable error code.
    :param needs_allow: whether the response must carry an ``Allow`` header.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        if payload is None:
            response = await client.request(method, path)
        else:
            response = await client.request(method, path, json=payload)

    assert response.status_code in {404, 405, 422}
    assert response.headers["content-type"].startswith("application/json")
    envelope = ErrorEnvelope.model_validate(cast("dict[str, object]", response.json()))
    assert envelope.error.code is code
    assert envelope.error.message
    assert "\n" not in envelope.error.message
    if needs_allow:
        assert response.headers["allow"]


async def test_unhandled_exception_returns_the_generic_envelope_and_logs_the_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A crashing route answers with the generic envelope; the traceback stays in the log.

    :param caplog: captured log records.
    """
    install_log_scrubber()
    SECRET_SCRUBBER.set_secrets([TOKEN])
    caplog.set_level(logging.DEBUG)

    small_app = FastAPI()
    install_error_handlers(small_app)

    @small_app.get("/boom")
    async def _boom() -> None:
        """Raise with a secret in the message, as a real handler might."""
        raise RuntimeError(f"boom {TOKEN}")

    transport = httpx.ASGITransport(app=small_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom")

    assert response.status_code == 500
    body = cast("dict[str, object]", response.json())
    envelope = ErrorEnvelope.model_validate(body)
    assert envelope.error.code is ErrorCode.INTERNAL_ERROR
    assert envelope.error.detail == {}
    assert envelope.error.message
    assert "Traceback" not in response.text
    assert TOKEN not in response.text
    assert "boom" not in response.text
    assert "RuntimeError" not in response.text

    assert "Traceback" in caplog.text
    assert "boom" in caplog.text
    assert TOKEN not in caplog.text
    assert REDACTED in caplog.text


async def test_patch_refuses_the_provider_array_and_names_the_provider_route(
    tmp_path: Path,
) -> None:
    """``tts.providers`` is written only by ``/api/tts/providers`` (D25).

    Refusing it here is what keeps the generic PATCH from becoming a second write
    path for provider configuration, so an ``api_key`` can never arrive through it.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch("/api/settings", json={"tts.providers": []})

        assert response.status_code == 422
        envelope = ErrorEnvelope.model_validate(cast("dict[str, object]", response.json()))
        assert envelope.error.code is ErrorCode.INVALID_SETTING
        assert "/api/tts/providers" in envelope.error.message
        assert "tts.providers" in envelope.error.message
        fields = cast("list[dict[str, object]]", envelope.error.detail["fields"])
        assert fields == [
            {
                "field": "tts.providers",
                "reason": "is managed through /api/tts/providers, not the settings patch",
            }
        ]
        assert await state.store.get_setting("tts.providers") == []


async def test_patch_refuses_the_admin_password_hash_and_names_the_password_route(
    tmp_path: Path,
) -> None:
    """``server.admin_password_hash`` is written only by ``/api/auth/password`` (HSD-n6wi).

    The mirror of the ``tts.providers`` refusal above: the generic PATCH would
    bypass the current-password rule, so the key has its own reason and nothing is
    written.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch(
            "/api/settings",
            json={"server.admin_password_hash": "scrypt$16384$8$1$AAAA$AAAA"},
        )

        assert response.status_code == 422
        envelope = ErrorEnvelope.model_validate(cast("dict[str, object]", response.json()))
        assert envelope.error.code is ErrorCode.INVALID_SETTING
        assert "/api/auth/password" in envelope.error.message
        assert "server.admin_password_hash" in envelope.error.message
        fields = cast("list[dict[str, object]]", envelope.error.detail["fields"])
        assert fields == [
            {
                "field": "server.admin_password_hash",
                "reason": "is managed through /api/auth/password, not the settings patch",
            }
        ]
        assert await state.store.get_setting("server.admin_password_hash") == ""


async def test_patch_does_not_special_case_a_provider_secret_key(tmp_path: Path) -> None:
    """A provider key is an unknown key, not a clearable secret: ``""`` gets no special answer.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)
    key = "tts.providers.0123456789ab.api_key"

    async with _running(app) as client:
        response = await client.patch("/api/settings", json={key: ""})

    assert response.status_code == 422
    envelope = ErrorEnvelope.model_validate(cast("dict[str, object]", response.json()))
    assert envelope.error.code is ErrorCode.UNKNOWN_SETTING
    fields = cast("list[dict[str, object]]", envelope.error.detail["fields"])
    assert fields == [{"field": key, "reason": "is not a known setting"}]


async def test_get_reports_the_provider_settings_and_never_a_key_row(tmp_path: Path) -> None:
    """The three new settings are ordinary reads; a stored key row stays invisible.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await state.store.set_provider_secret("0123456789ab", "sk-live-123")
        response = await client.get("/api/settings")

    assert response.status_code == 200
    dumped = json.dumps(cast("dict[str, object]", response.json()))
    body = cast("dict[str, object]", response.json())
    settings = cast("dict[str, object]", body["settings"])
    assert settings["tts.providers"] == []
    assert settings["tts.allow_dynamic_paid"] is False
    assert settings["tts.http_tts.provider"] == ""
    assert "tts.providers.0123456789ab.api_key" not in dumped
    assert "sk-live-123" not in dumped
    assert set(cast("dict[str, object]", body["secrets"])) == {
        "ha.token",
        "mqtt.password",
        "server.admin_password_hash",
        "server.token",
        "tts.remote_tts.token",
        "tts.renderer.token",
    }


async def test_dynamic_paid_and_http_provider_stay_ordinarily_writable(tmp_path: Path) -> None:
    """Only ``tts.providers`` is refused: these two are plain settings (D25).

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.patch(
            "/api/settings",
            json={"tts.allow_dynamic_paid": True, "tts.http_tts.provider": "0123456789ab"},
        )

        assert response.status_code == 200
        patched = cast("dict[str, object]", response.json())
        settings = cast("dict[str, object]", patched["settings"])
        assert settings["tts.allow_dynamic_paid"] is True
        assert settings["tts.http_tts.provider"] == "0123456789ab"


class _RecordingHaProbes:
    """Record the candidate each Home Assistant test received, and pass."""

    def __init__(self) -> None:
        """Start with no recorded requests."""
        self.candidates: list[HaProbeCandidate | None] = []

    async def test_ha(self, candidate: HaProbeCandidate | None = None) -> HaProbeResult:
        """Record the candidate and answer a pass.

        :param candidate: credentials the route asked the probe to try.
        """
        self.candidates.append(candidate)
        return HaProbeResult(ok=True, entity_count=2)

    async def test_mqtt(self) -> MqttProbeResult:
        """Never used by this test."""
        raise AssertionError("the MQTT probe must not run")

    async def test_audio(self) -> AudioProbeResult:
        """Never used by this test."""
        raise AssertionError("the audio probe must not run")


async def test_the_ha_test_body_is_optional_and_null_is_accepted(tmp_path: Path) -> None:
    """``{}`` and a JSON ``null`` both test the stored settings.

    The body is optional so a client that has nothing to add keeps sending no
    body at all; an empty object is the same request, and a literal ``null`` is
    the shape pydantic receives for a bodiless POST. All three must reach the
    probe as \"use the stored values\".

    :param tmp_path: pytest temporary directory.
    """
    probes = _RecordingHaProbes()
    app, _state = _build_app(tmp_path, probes=probes)

    async with _running(app) as client:
        empty = await client.post("/api/settings/test/ha", json={})
        null = await client.post(
            "/api/settings/test/ha",
            content=b"null",
            headers={"content-type": "application/json"},
        )

    for response in (empty, null):
        assert response.status_code == 200
        body = cast("dict[str, object]", response.json())
        assert body["ok"] is True
        assert body["entity_count"] == 2
    assert probes.candidates == [
        HaProbeCandidate(base_url=None, token=None),
        HaProbeCandidate(base_url=None, token=None),
    ]
