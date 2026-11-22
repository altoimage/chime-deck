"""A dev-only test bench that boots the real backend for Playwright.

The browser tier is not allowed to mock the backend it is supposed to verify, so
this script runs the **real** application (:func:`chime_deck.app.create_app`)
with every device replaced by the offline fakes the test suite already owns:
:class:`~mqtt_fakes.FakeBroker` for MQTT, :class:`~chime_fakes.RecordingBackend`
for the speaker, and a real
:class:`~chime_deck.ha.HomeAssistantWebSocketClient` — built by the app's own
construction seam — talking to a scripted :class:`~ha_ws_fakes.FakeHaPeer`.
Because the bench passes a *factory* rather than a client instance, the app's
lifespan owns every generation of that client and a committed
``ha.base_url``/``ha.token`` change rebuilds it in place, still offline.

The Home Assistant **REST** edge is faked too, through an
:class:`httpx.MockTransport` that is handed to :func:`~chime_deck.app.create_app`
as ``ha_client``. That transport is the one the settings page's connection probe
borrows, so without it ``POST /api/settings/test/ha`` on this bench could only
ever answer "cannot connect to Home Assistant": there is no socket here and no
name to resolve. The Test button is a user-facing control, and ticket cd-ev3m is
about exactly that control — it must test the token the user typed rather than
the stored one — so the tier has to be able to press it and read a real verdict.
The fake is deliberately tiny: one ``/api/states`` answer guarded by the same
bearer the scripted websocket peer accepts, a ``401`` for any other token and a
``404`` for anything else. No socket, no DNS, no real Home Assistant.

Nothing here is production code and nothing here may touch the repository: the
data directory is a fixed per-port path under the system temp directory that is
wiped before the bench opens it and removed on exit, the socket binds
``127.0.0.1`` only, and no audio device, MQTT broker or Home Assistant is ever
opened. The path is deterministic rather than random because Playwright kills
the bench with ``SIGKILL`` at teardown, so a :func:`tempfile.mkdtemp` name would
leak one database directory per run, while a fixed name leaks at most one that
the next run reuses and wipes.

The fakes are scripted rather than free-running, so the bench also completes the
handshakes a real broker and a real Home Assistant would complete on their own:
it fires the CONNACK the :class:`~mqtt_fakes.FakeClient` never fires, and it
reports the Home Assistant status its subscribed peer is actually in. Both flips
land in the app's own :class:`~chime_deck.health.HealthRegistry`, because
that registry is what the header reads — a bench that reported
``not_configured`` over six live tiles would be lying about itself.

A Playwright spec drives the bench through three control routes, which exist
only on the outer Starlette wrapper and can never be reached through the app's
own catch-all:

``GET /__test/status``
    ``200`` once the seed and the initial Home Assistant snapshot exist, ``503``
    before that. Playwright's ``webServer`` gate treats ``2xx/3xx`` as ready, so a
    spec cannot start against a half-seeded database.

``POST /__test/reset``
    Re-applies the canonical seed to the *live* store and re-pushes the initial
    states (``beforeEach``), leaving ids stable so a spec may keep a reference.
    Only drifted values are written, so a reset with nothing to restore emits no
    configuration event at all.

``POST /__test/ha_state``
    Pushes one state through the fake Home Assistant peer and waits (bounded)
    until the hub's own snapshot shows it — the "fake HA" seam.

``POST /__test/token``
    Writes ``server.token`` through the *live* store, or clears it with ``""`` /
    ``null``, so a spec can turn the shared-token gate on and off per test. The
    gate refreshes itself from the store's committed settings event, so the new
    state applies to the very next request with no restart. Only this outer
    wrapper exposes it and it is never reachable through the app: the app's own
    catch-all serves the SPA for ``/__test/token`` like any other unknown path.

``POST /__test/ha_credentials``
    Writes ``ha.base_url`` and/or ``ha.token`` through the *live* store, or clears
    the token with ``""`` / ``null``. The app's own settings subscriber rebuilds
    the websocket client in place (against the scripted peer factory, still
    offline), so a spec can watch a credential change take effect with no
    restart. It also reports a ``token_fingerprint`` (a SHA-256 prefix) so a spec
    can prove the stored row holds exactly the value it typed without any
    response ever carrying the secret. Exposed only on this outer wrapper,
    exactly like ``__test/token``.

``tests/`` is deliberately not a package, so the plain ``chime_fakes`` and
``ha_ws_fakes`` imports only work when the caller puts ``tests`` on
``PYTHONPATH``. The Playwright ``webServer`` does exactly that.

Owning ticket: HSD-rj5j.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import logging
import shutil
import signal
import socket
import tempfile
import time
import wave
from collections.abc import AsyncIterator, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Final

import httpx
import uvicorn
from audio_hardware_fakes import FakeCommand, RecordingSpawner
from chime_fakes import RecordingBackend, build_offline_wiring
from ha_ws_fakes import FakeHaFactory, FakeHaPeer
from mqtt_fakes import FakeBroker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig, configure_logging
from chime_deck.ha import HomeAssistantWebSocketClient
from chime_deck.health import HealthStatus
from chime_deck.store import (
    Button,
    ButtonIn,
    ConfigStore,
    Entity,
    EntityIn,
    Sound,
    SoundIn,
    SoundUpdate,
    normalise_value,
)

__all__ = ["INITIAL_STATES", "TestBench", "main"]

DEFAULT_PORT: Final = 4174
"""Port the bench listens on unless ``--port`` says otherwise."""

BIND_HOST: Final = "127.0.0.1"
"""Loopback only: the bench inherits the app's unauthenticated API (D13)."""

WAIT_TIMEOUT_S: Final = 5.0
"""Upper bound for every readiness wait in this file."""

POLL_INTERVAL_S: Final = 0.05
"""Delay between two readiness polls."""

CHIME_FILENAME: Final = "doorbell.wav"
"""The one sound file the canonical seed writes into the temporary library."""

CHIME_SAMPLE_RATE: Final = 8000
"""Samples per second: 8 kHz mono, the smallest honest WAV a decoder accepts."""

CHIME_FRAME_COUNT: Final = 1600
"""0.2 s of silence at :data:`CHIME_SAMPLE_RATE`."""

SENTINEL_ENTITY_ID: Final = "lock.front_door"
"""The entity whose state proves the initial Home Assistant snapshot arrived."""

HA_TOKEN: Final = "long-lived-token-0123456789abcdef"
"""The ``ha.token`` the seed stores and the bench's peer factory accepts.

One constant rather than two literals: the scripted peer rejects any token it
was not handed here, so a drift between the seed and the allowlist would leave
the bench with a client that can never authenticate.
"""

TOKEN_FINGERPRINT_CHARS: Final = 12
"""Hex characters of the ``ha.token`` SHA-256 that ``__test/ha_credentials`` reports.

Twelve lowercase hex characters — 48 bits — are a fingerprint, not a secret: a
spec can tell "the stored row really changed to the value I typed" from "the row
still holds the old one" without the response ever being worth reading for an
attacker. That ceiling is what lets this ticket's spec assert exact storage.
"""

CANONICAL_ENTITIES: Final = (
    ("lock.front_door", "Front Door", "mdi:lock"),
    ("sensor.hallway_temperature", "Hallway Temperature", "mdi:thermometer"),
    ("light.kitchen_ceiling", "Kitchen Ceiling", "mdi:lightbulb"),
    ("binary_sensor.garage_door", "Garage Door", "mdi:garage"),
    ("lock.back_door", "Back Door", "mdi:lock"),
    ("sensor.living_room_humidity", "Living Room Humidity", "mdi:water"),
)
"""The six seeded tiles, in ``sort_order`` 1..6.

The order is part of the contract: phase 2's specs assert the dashboard's tile
order, so this tuple must stay byte-identical to what they expect.
"""

CANONICAL_SETTINGS: Final = (
    ("ui.setup_dismissed", True),
    ("ui.show_clock", True),
    ("ha.base_url", "http://homeassistant.local:8123"),
    ("ha.token", HA_TOKEN),
    ("audio.quiet_hours_start", "22:00"),
    ("audio.volume", 100),
    ("audio.hardware_volume", 100),
    ("audio.rail_target", "software"),
    ("audio.hardware_control", ""),
)
"""The stored settings the seed writes; every other registry default is untouched."""

INITIAL_STATES: Final = {
    "lock.front_door": {"s": "locked", "a": {"friendly_name": "Front Door"}},
    "sensor.hallway_temperature": {
        "s": "21.4",
        "a": {"friendly_name": "Hallway Temperature", "unit_of_measurement": "°C"},
    },
    "light.kitchen_ceiling": {"s": "on", "a": {"friendly_name": "Kitchen Ceiling"}},
    "binary_sensor.garage_door": {"s": "open", "a": {"friendly_name": "Garage Door"}},
    "lock.back_door": {"s": "unavailable", "a": {"friendly_name": "Back Door"}},
    "sensor.living_room_humidity": {
        "s": "48",
        "a": {"friendly_name": "Living Room Humidity", "unit_of_measurement": "%"},
    },
}
"""The Home Assistant states pushed once the fake peer has subscribed.

These values are chosen for the *words* the UI renders: ``lock`` maps
``locked``/``unlocked``/``unavailable`` to itself, ``on`` and ``open`` fall
through the default table unchanged, and ``unit_of_measurement`` is copied
verbatim into ``displayed_state``/``unit_of_measurement`` by
:func:`chime_deck.icons.presentation_hints`.
"""

_LOGGER = logging.getLogger("chime_deck.test_bench")

DEFAULT_STATIC_DIR: Final = Path(__file__).resolve().parents[3] / "web" / "dist"
"""The committed build of the SPA, resolved from this file so the cwd is irrelevant."""


def _ha_states_payload() -> list[dict[str, object]]:
    """Return one ``/api/states`` object per seeded entity, in seed order.

    Derived from :data:`INITIAL_STATES` rather than restated, so the count the
    Test button reports is the number of tiles the seed actually holds: a second
    copy of the six entity ids would drift the day one of them is renamed, and
    the spec would then be asserting a number that means nothing.

    :class:`~chime_deck.ha.HomeAssistantClient` reduces each entry with
    ``_parse_state``, which requires a string ``entity_id`` and ``state`` and
    copies ``attributes`` through as a mapping. The optional ``last_changed`` /
    ``last_updated`` timestamps are omitted on purpose: the probe counts entries
    and never reads them, and a fabricated clock reading is a lie a spec could
    later trust.

    :returns: JSON-ready state objects, in seed order.
    """
    return [
        {"entity_id": entity_id, "state": values["s"], "attributes": values["a"]}
        for entity_id, values in INITIAL_STATES.items()
    ]


def _ha_rest_handler(request: httpx.Request) -> httpx.Response:
    """Answer the Home Assistant REST edge in memory, as far as the probe needs.

    The bench had no REST fake at all, which left one user-facing control
    unexercisable: ``POST /api/settings/test/ha`` could only report "cannot
    connect" because nothing in this process may open a socket or resolve a
    name. The Test button is what ticket cd-ev3m is about — it must probe the
    token the user typed, not the stored one — so the bench answers the one
    request that probe makes.

    The allowlist is :data:`HA_TOKEN`, the same constant the scripted websocket
    peer accepts, so the bench cannot disagree with itself about which credential
    is current: a spec that seeds a stale token gets a ``401`` from both edges
    and a save that stores :data:`HA_TOKEN` gets ``200`` from both.

    Only ``GET /api/states`` is served. Everything else is a ``404`` with Home
    Assistant's own shape, so a future probe that reads ``/api/config`` fails
    loudly instead of silently getting an entity list.

    :param request: the outgoing REST request the borrowed client produced.
    :returns: the scripted answer; nothing here touches the network.
    """
    if request.method == "GET" and request.url.path == "/api/states":
        if request.headers.get("Authorization") != f"Bearer {HA_TOKEN}":
            return httpx.Response(401, json={"message": "401: Unauthorized"})
        return httpx.Response(200, json=_ha_states_payload())
    return httpx.Response(404, json={"message": "404: Not Found"})


def _ha_rest_client() -> httpx.AsyncClient:
    """Return the borrowed Home Assistant REST client the app and its probes share.

    :class:`httpx.MockTransport` replaces the transport, so the client is real
    httpx — same retry, timeout and status mapping as production — with every
    connection answered in memory. The app never closes a borrowed client, so it
    lives as long as this process.

    :returns: an ``AsyncClient`` whose only transport is :func:`_ha_rest_handler`.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(_ha_rest_handler))


def _attempts(timeout_s: float) -> int:
    """Return how many polls fit in *timeout_s*.

    :param timeout_s: wall-clock budget of one bounded wait.
    """
    return max(1, int(timeout_s / POLL_INTERVAL_S))


def bench_data_dir(port: int) -> Path:
    """Return the bench's data directory for *port*, wiping any previous leftover.

    The name is fixed per port instead of random because Playwright kills the
    bench with ``SIGKILL`` during ``webServer`` teardown, so the ``finally``
    cleanup never runs: a random :func:`tempfile.mkdtemp` name would leak one
    database directory per run, whereas a fixed name leaks at most one, and the
    next run reuses and wipes it. Two live benches cannot share a port, so the
    fixed name can never collide with a running instance.

    :param port: TCP port the bench binds, unique to this instance.
    :returns: the freshly created, empty data directory.
    """
    path = Path(tempfile.gettempdir()) / f"doorbell-playwright-bench-{port}"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=False)
    return path


def _write_chime_wav(sounds_dir: Path) -> tuple[int, str]:
    """Write the tiny silent chime WAV and return its size and SHA-256.

    The file is real (a decoder can open it) but it is 0.2 s of silence, so no
    audio device is ever involved — the bench only needs a path the store and the
    sound library can agree on.

    :param sounds_dir: directory the sound library scans.
    :returns: ``(bytes, sha256_hex)`` of the file just written.
    """
    sounds_dir.mkdir(parents=True, exist_ok=True)
    path = sounds_dir / CHIME_FILENAME
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(CHIME_SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * CHIME_FRAME_COUNT)
    payload = path.read_bytes()
    return len(payload), hashlib.sha256(payload).hexdigest()


def _entity_is_canonical(row: Entity, display_name: str, icon: str, sort_order: int) -> bool:
    """Return whether *row* already carries the canonical tile fields.

    :param row: stored entity row.
    :param display_name: canonical ``display_name``.
    :param icon: canonical ``icon``.
    :param sort_order: canonical ``sort_order``.
    """
    return (row.display_name, row.icon, row.sort_order) == (display_name, icon, sort_order)


async def _upsert_entities(store: ConfigStore) -> None:
    """Write the six canonical entity rows, updating only the ones that drift.

    ``sort_order`` is fixed, so the dashboard order never depends on insert
    order, and an existing row keeps its id so a spec may hold on to it. A row
    that already holds the canonical name, icon and order is left untouched: an
    entity write emits ``config.entities_changed``, and every such event makes
    the Home Assistant client drop and rebuild its subscription, so a reset with
    nothing to restore must not write a single row.

    :param store: open configuration store.
    """
    existing = {entity.entity_id: entity for entity in await store.list_entities()}
    for sort_order, (entity_id, display_name, icon) in enumerate(CANONICAL_ENTITIES, start=1):
        row = existing.get(entity_id)
        if row is not None and _entity_is_canonical(row, display_name, icon, sort_order):
            continue
        await store.upsert_entity(
            EntityIn(
                entity_id=entity_id,
                id=None if row is None else row.id,
                display_name=display_name,
                icon=icon,
                sort_order=sort_order,
            )
        )


async def _upsert_chime_sound(store: ConfigStore, sounds_dir: Path) -> Sound:
    """Ensure the silent chime file and its sound row exist, and return the row.

    :param store: open configuration store.
    :param sounds_dir: directory the sound library scans.
    """
    size, digest = _write_chime_wav(sounds_dir)
    for sound in await store.list_sounds():
        if sound.path == CHIME_FILENAME:
            if sound.missing:
                return await store.update_sound(sound.id, SoundUpdate(missing=False))
            return sound
    return await store.create_sound(
        SoundIn(
            name="Doorbell",
            source_type="file",
            path=CHIME_FILENAME,
            format="wav",
            duration_ms=200,
            bytes=size,
            sha256=digest,
        )
    )


def _button_is_canonical(button: Button, sound_id: int) -> bool:
    """Return whether *button* already plays the chime the canonical seed asks for.

    :param button: stored button row.
    :param sound_id: id of the seeded chime sound.
    """
    return (button.sound_id, button.dedupe_seconds, button.show_on_dashboard, button.enabled) == (
        sound_id,
        None,
        True,
        True,
    )


async def _upsert_chime_button(store: ConfigStore, sound: Sound) -> None:
    """Ensure the one chime button exists, playing *sound*, and leave it alone otherwise.

    ``dedupe_seconds=None`` is deliberate: a spec that presses the button twice
    in a row must never have the second press swallowed by the dedupe window.

    The row is only written when one of those four fields drifts, because a
    button write emits ``config.buttons_changed`` and a ``beforeEach`` reset that
    changes nothing must stay silent.

    :param store: open configuration store.
    :param sound: the seeded sound the button plays.
    """
    existing = next(
        (button for button in await store.list_buttons() if button.name == "Front Door Chime"),
        None,
    )
    if existing is not None and _button_is_canonical(existing, sound.id):
        return
    await store.upsert_button(
        ButtonIn(
            name="Front Door Chime",
            action_type="sound",
            id=None if existing is None else existing.id,
            sound_id=sound.id,
            dedupe_seconds=None,
            show_on_dashboard=True,
            enabled=True,
        )
    )


async def _apply_settings(store: ConfigStore) -> None:
    """Write only those canonical settings whose stored value has drifted.

    ``set_setting`` emits ``config.settings_changed`` unconditionally, and the
    chime pipeline rebuilds the MQTT bridge for every ``mqtt.*`` key it sees, so
    a reset that changes nothing must write nothing.

    :param store: open configuration store.
    """
    for key, value in CANONICAL_SETTINGS:
        if await store.get_setting(key) == normalise_value(key, value):
            continue
        await store.set_setting(key, value)


async def apply_canonical_seed(store: ConfigStore, config: ServerConfig) -> None:
    """Apply the canonical bench state to *store*, touching nothing that already matches.

    Used twice: once against a throwaway store before the app is built, and again
    by ``POST /__test/reset`` against the live store. Every step reads the stored
    state first and writes only what drifted, so a reset with nothing to restore
    emits no configuration event at all — while a spec's saved URL, icon override
    or moved tile is still written back.

    :param store: an open configuration store.
    :param config: server configuration resolving the sound directory.
    """
    await _apply_settings(store)
    sound = await _upsert_chime_sound(store, config.resolved_sounds_dir)
    await _upsert_entities(store)
    await _upsert_chime_button(store, sound)


async def seed_database(config: ServerConfig) -> None:
    """Seed the database before :func:`create_app` opens the same file.

    :param config: server configuration naming the database and sound directory.
    """
    store = ConfigStore(config.database_path)
    await store.connect()
    try:
        await apply_canonical_seed(store, config)
    finally:
        await store.close()


async def _snapshot_rows(state: AppState) -> dict[str, dict[str, object]]:
    """Return the hub's own snapshot rows keyed by ``entity_id``.

    The snapshot is read through :meth:`chime_deck.hub.StateHub.snapshot_message`
    — the public method the ``/ws`` route uses to build its ``snapshot`` frame —
    so the bench never reaches into a private cache and the wait observes exactly
    what a browser would.

    :param state: the running application's shared state.
    """
    snapshot = await state.hub.snapshot_message()
    data = snapshot.get("data")
    if not isinstance(data, Mapping):
        return {}
    rows = data.get("entities")
    if not isinstance(rows, list):
        return {}
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        entity_id = row.get("entity_id")
        if isinstance(entity_id, str):
            result[entity_id] = dict(row)
    return result


class TestBench:
    """The scripted device side of the bench plus its control routes.

    One instance owns the fake broker, the peer factory, the readiness flag and
    the bounded waits the control routes use. Every wait is a ``for _ in
    range(n)`` poll with ``asyncio.sleep(POLL_INTERVAL_S)``; nothing here can hang
    a Playwright run.

    It also owns the two health flips a real broker and a real Home Assistant
    would produce on their own, because the fakes deliberately do not: the
    :class:`~mqtt_fakes.FakeClient` records ``loop_start`` without ever firing a
    CONNACK, and the websocket client the app builds talks to a scripted peer
    factory. Both are reported from the app's own event loop, bounded, so the
    header describes the bench that is actually running.
    """

    def __init__(
        self,
        config: ServerConfig,
        state: AppState,
        factory: FakeHaFactory,
        broker: FakeBroker,
    ) -> None:
        """Bind the bench to one running application.

        :param config: the configuration the app was built with.
        :param state: the app state carrying the live store and hub.
        :param factory: the scripted connection factory behind the injected client.
        :param broker: the in-memory broker behind the MQTT bridge's client factory.
        """
        self._config = config
        self._state = state
        self._factory = factory
        self._broker = broker
        self.ready = False
        self.phase = "starting"

    def _subscribed_peer(self) -> FakeHaPeer | None:
        """Return the most recent fake peer that has sent ``subscribe_entities``.

        Not just ``connections[0]``: the live-data adapter opens its own
        short-lived connections for the entity registry, so only a peer with a
        subscription can carry a state event.
        """
        subscribed = [peer for peer in self._factory.connections if peer.subscribed_ids()]
        return None if not subscribed else subscribed[-1]

    async def _wait_for_subscribed_peer(self) -> FakeHaPeer | None:
        """Wait (bounded) until the injected client has subscribed and return its peer."""
        for _ in range(_attempts(WAIT_TIMEOUT_S)):
            peer = self._subscribed_peer()
            if peer is not None:
                return peer
            await asyncio.sleep(POLL_INTERVAL_S)
        return None

    def _report_home_assistant_connected(self) -> None:
        """Publish the Home Assistant status the bench's peer is actually in.

        The app builds the client through its own construction seam with its own
        :class:`~chime_deck.health.HealthRegistry`, so the client itself records
        ``connected`` once the peer has accepted the handshake and the
        subscription, and nothing lands in a private registry any more. The write
        below survives as a fallback for the window before the supervisor's own
        status arrives: the caller only reaches it after
        :meth:`_wait_for_subscribed_peer` succeeded, so the state recorded here is
        the state the client is really in, never a fabricated one.
        """
        health = self._state.health
        if health.get("ha") is not HealthStatus.CONNECTED:
            health.set("ha", HealthStatus.CONNECTED)

    async def _connect_broker_clients(self) -> bool:
        """Fire the CONNACK the fake broker owes every client, bounded.

        :class:`~mqtt_fakes.FakeClient` shapes paho but is scripted: ``loop_start``
        only records the call, so without this the real
        :class:`~chime_deck.mqtt.MqttBridge` sits at ``reconnecting`` for ever
        and publishes no discovery payload.

        Called from the app's own event loop, so the CONNACK's callback can hand
        its work back to the loop the bridge recorded. Idempotent: a client that
        is already connected is skipped, which is what makes it safe in both
        ``on_startup`` and every ``POST /__test/reset``. A settings write can
        rebuild the bridge into a brand-new client, so the loop re-sweeps
        :attr:`FakeBroker.clients` on every poll and only reports success when no
        client is left unconnected.

        :returns: whether the bridge reached ``connected`` within
            :data:`WAIT_TIMEOUT_S`.
        """
        for _ in range(_attempts(WAIT_TIMEOUT_S)):
            for client in self._broker.clients:
                if not client.connected:
                    self._broker.connect(client)
            unconnected = [client for client in self._broker.clients if not client.connected]
            if not unconnected and self._state.health.get("mqtt") is HealthStatus.CONNECTED:
                return True
            await asyncio.sleep(POLL_INTERVAL_S)
        return False

    async def _states_reflected(self, expected: Mapping[str, Mapping[str, object]]) -> bool:
        """Return whether the hub snapshot already shows every expected state.

        :param expected: ``entity_id`` to the compressed state dict pushed to HA.
        """
        rows = await _snapshot_rows(self._state)
        for entity_id, values in expected.items():
            row = rows.get(entity_id)
            if row is None or row.get("state") != values.get("s"):
                return False
        return True

    async def _push_until_reflected(self, states: Mapping[str, Mapping[str, object]]) -> bool:
        """Push *states* to the fake peer until the hub snapshot shows them.

        Re-pushing on every poll (rather than once) keeps the wait honest across
        the client's own re-subscription after a configuration write: an event
        that arrives for the subscription the client just dropped is ignored, and
        the next poll pushes to the current one.

        :param states: ``entity_id`` to the compressed state dict, without ``lc``.
        """
        for _ in range(_attempts(WAIT_TIMEOUT_S)):
            peer = self._subscribed_peer()
            if peer is not None:
                now = time.time()
                frame = {entity_id: {**values, "lc": now} for entity_id, values in states.items()}
                with contextlib.suppress(RuntimeError):
                    peer.push_states(frame)
            if await self._states_reflected(states):
                return True
            await asyncio.sleep(POLL_INTERVAL_S)
        return False

    async def on_startup(self) -> None:
        """Connect the fake broker, push the initial states and flip the readiness gate.

        Called by the outer lifespan *after* the doorbell's own lifespan has run,
        so the injected client already exists and the seed is already visible.

        :raises RuntimeError: when the MQTT bridge, the fake peer or the hub
            snapshot does not converge inside :data:`WAIT_TIMEOUT_S`; Playwright
            then reports a failed startup instead of running specs against a dead
            bench.
        """
        self.phase = "connecting the MQTT bridge"
        if not await self._connect_broker_clients():
            raise RuntimeError(f"the MQTT bridge never connected within {WAIT_TIMEOUT_S:g} s")
        self.phase = "waiting for subscribe_entities"
        if await self._wait_for_subscribed_peer() is None:
            raise RuntimeError(
                f"the fake Home Assistant peer never subscribed within {WAIT_TIMEOUT_S:g} s"
            )
        self._report_home_assistant_connected()
        self.phase = "pushing the initial states"
        if not await self._push_until_reflected(INITIAL_STATES):
            raise RuntimeError(
                f"the hub snapshot never showed the initial states within {WAIT_TIMEOUT_S:g} s"
            )
        self.ready = True
        self.phase = "ready"
        print(f"doorbell test bench: ready ({len(INITIAL_STATES)} entities seeded)", flush=True)

    async def status(self, _request: Request) -> Response:
        """Answer the readiness gate Playwright's ``webServer`` polls.

        :param _request: the incoming request, unused.
        """
        if not self.ready:
            return JSONResponse({"ready": False, "phase": self.phase}, status_code=503)
        entities = await self._state.store.list_entities()
        buttons = await self._state.store.list_buttons()
        return JSONResponse(
            {
                "ready": True,
                "entities": len(entities),
                "buttons": len(buttons),
                "peer_subscribed": self._subscribed_peer() is not None,
                "data_dir": str(self._config.data_dir),
            }
        )

    async def reset(self, _request: Request) -> Response:
        """Re-apply the canonical seed and the initial Home Assistant states.

        Runs in ``beforeEach``, so it reuses the live store (no reconnect, no
        restart) and keeps existing ids: only the values and the live snapshot
        are restored. The seed writes nothing that already matches, so a reset
        after a test that changed nothing emits no configuration event — and
        therefore does not re-subscribe the Home Assistant client or rebuild the
        MQTT bridge.

        :param _request: the incoming request, unused.
        """
        await apply_canonical_seed(self._state.store, self._config)
        if not await self._connect_broker_clients():
            return JSONResponse(
                {"reset": False, "error": "the MQTT bridge did not connect"}, status_code=503
            )
        if await self._wait_for_subscribed_peer() is None:
            return JSONResponse(
                {"reset": False, "error": "the Home Assistant peer is not subscribed"},
                status_code=503,
            )
        self._report_home_assistant_connected()
        if not await self._push_until_reflected(INITIAL_STATES):
            return JSONResponse(
                {"reset": False, "error": "the hub snapshot did not converge"}, status_code=503
            )
        return JSONResponse({"reset": True})

    async def ha_state(self, request: Request) -> Response:
        """Push one state through the real client's fake peer, then wait for the hub.

        :param request: the incoming JSON request.
        """
        payload = await request.json()
        if not isinstance(payload, Mapping):
            return JSONResponse({"error": "the body must be a JSON object"}, status_code=400)
        entity_id = payload.get("entity_id")
        state = payload.get("state")
        raw_attributes = payload.get("attributes", {})
        if not isinstance(entity_id, str) or not entity_id:
            return JSONResponse({"error": "entity_id must be a non-empty string"}, status_code=400)
        if not isinstance(state, str):
            return JSONResponse({"error": "state must be a string"}, status_code=400)
        if not isinstance(raw_attributes, Mapping):
            return JSONResponse({"error": "attributes must be a JSON object"}, status_code=400)
        attributes = {str(key): value for key, value in raw_attributes.items()}

        if await self._wait_for_subscribed_peer() is None:
            return JSONResponse(
                {"error": "the fake Home Assistant peer is not subscribed"}, status_code=503
            )
        pushed = {entity_id: {"s": state, "a": attributes}}
        if not await self._push_until_reflected(pushed):
            return JSONResponse(
                {"entity_id": entity_id, "state": state, "reflected": False}, status_code=503
            )
        return JSONResponse({"entity_id": entity_id, "state": state})

    async def token(self, request: Request) -> Response:
        """Set or clear ``server.token`` through the live store.

        The value is written through :meth:`ConfigStore.set_setting` rather than
        the settings route: the store accepts an empty string for this secret
        (which is exactly what "no authentication" means), while the route
        deliberately refuses ``""`` and wants ``null`` to clear. A committed
        write publishes the settings event the gate subscribes to, and that
        subscriber is awaited inside the write, so the answer below is already
        the state the next request will be judged by.

        :param request: the incoming JSON request, ``{"token": "..."}``; a
            ``null`` token is the same clear as ``""``.
        """
        payload = await request.json()
        if not isinstance(payload, Mapping):
            return JSONResponse({"error": "the body must be a JSON object"}, status_code=400)
        raw = payload.get("token")
        if raw is not None and not isinstance(raw, str):
            return JSONResponse(
                {"error": "token must be a string, or null to clear it"}, status_code=400
            )
        await self._state.store.set_setting("server.token", "" if raw is None else raw)
        return JSONResponse({"authenticated": self._state.auth.required()})

    async def ha_credentials(self, request: Request) -> Response:
        """Set or clear the Home Assistant credentials through the live store.

        Written through :meth:`ConfigStore.set_setting` rather than the settings
        route for the same reason as :meth:`token`: the store accepts ``""`` for
        this secret (which is how a credential is cleared), while the route
        deliberately refuses an empty secret. A committed write publishes the
        settings event the app's own subscriber listens for, and that subscriber
        is awaited inside the write, so the answer below already describes the
        client the next request will meet: the old one is closed and a new one is
        built from the new settings, with no restart.

        ``token_fingerprint`` is the first :data:`TOKEN_FINGERPRINT_CHARS`
        lowercase hex characters of the SHA-256 of the stored ``ha.token``, and
        ``null`` when no token is stored. A spec can then assert that the stored
        row holds *exactly* the value it typed — the question this ticket is held
        to, because the panel's own save path must carry the pasted token rather
        than a pending clear — without any response ever carrying the secret. The
        stored value is read with ``get_setting``, which is the same accessor the
        websocket client uses to build its ``auth`` frame; nothing here logs it,
        echoes it or puts it in a response.

        :param request: the incoming JSON request, ``{"base_url": "...",
            "token": "..."}``; either key may be omitted, an empty body is a pure
            read, and a ``null`` or ``""`` token clears it.
        """
        payload = await request.json()
        if not isinstance(payload, Mapping):
            return JSONResponse({"error": "the body must be a JSON object"}, status_code=400)
        raw_base_url = payload.get("base_url")
        raw_token = payload.get("token")
        if raw_base_url is not None and not isinstance(raw_base_url, str):
            return JSONResponse({"error": "base_url must be a string"}, status_code=400)
        if raw_token is not None and not isinstance(raw_token, str):
            return JSONResponse(
                {"error": "token must be a string, or null to clear it"}, status_code=400
            )
        if raw_base_url is not None:
            await self._state.store.set_setting("ha.base_url", raw_base_url)
        if "token" in payload:
            await self._state.store.set_setting("ha.token", "" if raw_token is None else raw_token)
        stored = await self._state.store.get_setting("ha.token")
        fingerprint = None
        if isinstance(stored, str) and stored != "":
            digest = hashlib.sha256(stored.encode("utf-8")).hexdigest()
            fingerprint = digest[:TOKEN_FINGERPRINT_CHARS]
        return JSONResponse(
            {
                "base_url": await self._state.store.get_setting("ha.base_url"),
                "token_stored": await self._state.store.has_secret("ha.token"),
                "ha": self._state.health.get("ha").value,
                "token_fingerprint": fingerprint,
            }
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the bench's two options.

    :param argv: argument list, or ``None`` for ``sys.argv[1:]``.
    """
    parser = argparse.ArgumentParser(description="Run the real doorbell backend for Playwright.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--static-dir", type=Path, default=DEFAULT_STATIC_DIR)
    return parser.parse_args(argv)


def _listen_socket(port: int) -> socket.socket:
    """Create the loopback listening socket uvicorn is handed.

    :param port: TCP port to bind.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((BIND_HOST, port))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def _browser_hardware_spawner() -> RecordingSpawner:
    """Provide an available, stateful PipeWire sink to the real API in browser tests.

    The bench must never touch host audio, but its UI needs a usable hardware
    slider so Playwright can prove the real settings PATCH. This fake models the
    one output returned by ``RecordingBackend`` and carries writes into later
    read-backs.
    """
    state: dict[str, int | bool] = {"percent": 100, "muted": False}

    def command(argv: Sequence[str]) -> FakeCommand:
        if len(argv) >= 3 and argv[1:3] == ("get-sink-volume", "offline-output"):
            return FakeCommand(stdout=f"Volume: front-left: 65536 / {state['percent']}% / 0.00 dB")
        if len(argv) >= 3 and argv[1:3] == ("get-sink-mute", "offline-output"):
            return FakeCommand(stdout=f"Mute: {'yes' if state['muted'] else 'no'}")
        if len(argv) == 4 and argv[1:3] == ("set-sink-volume", "offline-output"):
            state["percent"] = int(argv[3].removesuffix("%"))
            return FakeCommand()
        if len(argv) == 4 and argv[1:3] == ("set-sink-mute", "offline-output"):
            state["muted"] = argv[3] != "0"
            return FakeCommand()
        return FakeCommand(returncode=1, stderr="unexpected fake mixer command")

    return RecordingSpawner(command)


def _build_outer_app(
    doorbell: Starlette,
    bench: TestBench,
) -> Starlette:
    """Wrap the real app so the control routes exist and its lifespan runs.

    A mounted app's lifespan is **not** run by Starlette, so the outer lifespan
    enters ``doorbell.router.lifespan_context`` explicitly; without that the
    store is never opened and the first ``/api/settings`` would answer ``500``.

    :param doorbell: the real FastAPI application.
    :param bench: the control-channel state.
    """
    routes = [
        Route("/__test/status", bench.status, methods=["GET"]),
        Route("/__test/reset", bench.reset, methods=["POST"]),
        Route("/__test/ha_state", bench.ha_state, methods=["POST"]),
        Route("/__test/token", bench.token, methods=["POST"]),
        Route("/__test/ha_credentials", bench.ha_credentials, methods=["POST"]),
        Mount("/", app=doorbell),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        """Run the real lifespan, then the bench's initial push.

        :param _app: the outer app, unused.
        """
        async with doorbell.router.lifespan_context(doorbell):
            await bench.on_startup()
            yield

    return Starlette(routes=routes, lifespan=lifespan)


async def _serve(port: int, static_dir: Path, data_dir: Path) -> None:
    """Boot the bench and serve it until a signal asks for shutdown.

    :param port: TCP port to bind on loopback.
    :param static_dir: built SPA directory to serve.
    :param data_dir: bench data directory, see :func:`bench_data_dir`.
    """
    config = ServerConfig(
        data_dir=data_dir,
        host=BIND_HOST,
        port=port,
        db_path=data_dir / "doorbell.db",
        static_dir=static_dir,
        sounds_dir=data_dir / "sounds",
        seed_demo_sounds=False,
    )
    await seed_database(config)

    factory = FakeHaFactory(partial(FakeHaPeer, accepted_tokens={HA_TOKEN}))
    broker = FakeBroker()
    wiring = build_offline_wiring(
        broker=broker,
        backend=RecordingBackend(),
        hardware_spawner=_browser_hardware_spawner(),
    )
    # The construction seam rather than an injected instance: the app builds every
    # generation of the client itself (with its own health registry and state
    # callback), so a committed ``ha.*`` change rebuilds in place and every
    # rebuild still lands on the scripted peer factory. No socket is opened.
    doorbell = create_app(
        config,
        wiring=wiring,
        # The settings page's Test button borrows this client, so the bench fakes
        # the REST edge as well as the websocket one: without it the only verdict
        # a probe could reach here is "cannot connect", and the control this
        # ticket is about would stay unexercised.
        ha_client=_ha_rest_client(),
        ha_websocket_factory=partial(HomeAssistantWebSocketClient, connect=factory),
    )
    bench = TestBench(config, doorbell.state.doorbell, factory, broker)
    outer = _build_outer_app(doorbell, bench)

    sock = _listen_socket(port)
    server = uvicorn.Server(
        uvicorn.Config(
            outer,
            host=BIND_HOST,
            port=port,
            log_level="warning",
            log_config=None,
            timeout_graceful_shutdown=2,
        )
    )

    def _request_exit() -> None:
        """Ask uvicorn to exit promptly; also covers the window before ``serve``."""
        server.should_exit = True

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, _request_exit)

    print(f"doorbell test bench: listening on http://{BIND_HOST}:{port}", flush=True)
    _LOGGER.info(
        "bench: data dir %s, static dir %s, %d seeded entities",
        data_dir,
        static_dir,
        len(CANONICAL_ENTITIES),
    )
    try:
        await server.serve(sockets=[sock])
    finally:
        sock.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the bench from the command line.

    :param argv: argument list, or ``None`` for ``sys.argv[1:]``.
    """
    args = parse_args(argv)
    configure_logging("INFO")
    data_dir = bench_data_dir(args.port)
    try:
        asyncio.run(_serve(args.port, args.static_dir, data_dir))
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
        print("doorbell test bench: stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
