"""FastAPI application factory, lifespan and routes.

The factory owns no module-level state: every dependency is created inside
:func:`create_app`, so importing this module never opens a database, reads the
environment or starts a task. The CLI builds a :class:`ServerConfig` and hands
it over; tests build one too.

Owning ticket: HSD-8gft.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final, Protocol

import httpx
from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.websockets import WebSocketDisconnected

from . import __version__
from .admin import AdminGate
from .api import (
    PROBE_TIMEOUT_S,
    ConnectionProbes,
    LiveProbes,
    build_about_router,
    build_audio_router,
    build_auth_router,
    build_buttons_router,
    build_config_router,
    build_entities_router,
    build_ha_router,
    build_history_router,
    build_icons_router,
    build_mqtt_router,
    build_provider_router,
    build_settings_router,
    build_sounds_router,
    build_tiles_router,
    build_tts_router,
    install_error_handlers,
)
from .audio import AudioBackend
from .auth import SharedTokenMiddleware, TokenGate
from .config import (
    ServerConfig,
    admin_bind,
    apply_stored_log_level,
    is_loopback,
    refresh_secret_scrubber,
)
from .entity_info import LIVE_LOOKUP_TIMEOUT_S, NO_LIVE_ENTITY_INFO, LiveEntityInfo
from .ha import (
    HaConfigurationError,
    HaEntityInfo,
    HaEntityState,
    HomeAssistantWebSocketClient,
    sync_tile_ids,
    watch_tile_changes,
)
from .health import HealthRegistry, HealthStatus
from .hub import HubClient, StateHub
from .selection import SelectionService
from .sounds import (
    SoundDownloader,
    SoundLibrary,
    bundled_demo_sounds_dir,
    copy_bundled_demo_sounds,
)
from .store import ConfigEvent, ConfigEventName, ConfigStore
from .tts import (
    JITTER_MAX_S,
    JITTER_MIN_S,
    NO_PAID_PROVIDERS,
    BakeStore,
    BakeSweeper,
    ConfiguredProviders,
    PaidProviderLookup,
    PiperProbe,
    TtsEngine,
    TtsSpeechSource,
    VoiceDownloadRegistry,
    VoiceManager,
    baked_root,
    build_speech_source,
)
from .wiring import ChimePipeline, Wiring, default_wiring

__all__ = [
    "PLACEHOLDER_HTML",
    "STARTUP_SCAN_BUDGET_S",
    "AppState",
    "HaWebSocketFactory",
    "create_app",
]

_LOGGER = logging.getLogger(__name__)

STARTUP_SCAN_BUDGET_S: Final = 2.0
"""Total probe budget for the boot scan, in seconds.

The boot scan must never threaten the 20 s ready window, so it spends at most
this on starting probes (plus at most one in-flight
:data:`~chime_deck.sounds.probe.FFPROBE_TIMEOUT_S`). Files it could not
probe are indexed with an unknown duration; the route's rescan uses the 15 s
default and fills them in later, once the server is already serving.
"""

PLACEHOLDER_HTML: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ChimeDeck</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; background: #0e1116; color: #e8edf4;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  main { max-width: 34rem; padding: 2rem; text-align: center; }
  h1 { font-size: 1.75rem; margin: 0 0 0.75rem; }
  p { color: #8b98a8; line-height: 1.5; margin: 0.5rem 0; }
  code { background: #171c23; border: 1px solid #26303c; border-radius: 6px;
         padding: 0.1rem 0.35rem; color: #e8edf4; }
  .version { color: #4f9cf9; font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<main>
  <h1>ChimeDeck</h1>
  <p class="version">version __VERSION__</p>
  <p>The server is running, but the web UI has not been built yet.</p>
  <p>Run <code>npm run build</code> in <code>web/</code> to produce
     <code>web/dist</code>, then restart the service.</p>
</main>
</body>
</html>
"""


@dataclass(slots=True)
class AppState:
    """Per-process state shared by the routes, the websocket and tests.

    ``started_monotonic`` and ``clock`` come from the injectable clock passed to
    :func:`create_app`, so uptime is deterministic in tests.

    ``probes`` answers the settings page's connection tests and ``probe_timeout``
    bounds each of them; both are injection seams for tests, so the default is
    :class:`~chime_deck.api.LiveProbes` and
    :data:`~chime_deck.api.PROBE_TIMEOUT_S`.

    ``chime`` is the audio worker, the shared press service and the MQTT bridge.
    It is required because both the buttons route and the lifespan need it; it is
    constructed here and started by the lifespan, so building the app opens
    nothing.

    ``sounds`` indexes ``data/sounds`` and ``downloader`` publishes URL downloads
    into it. Both share one write lock and are constructed here, so building the
    app still opens nothing.

    ``paid_providers`` answers whether a TTS engine bills per character (D25)
    when a phrase is saved on a TTS button. HSD-s856 supplies the real lookup:
    :func:`create_app` installs
    :class:`~chime_deck.tts.providers.ConfiguredProviders` over the store once
    the store exists, so the answer is the live configuration. The dataclass
    default stays :data:`~chime_deck.tts.NO_PAID_PROVIDERS` — the honest
    "nothing is known to be paid" default for a directly constructed state and
    for a test that wants the D25 gate dormant. ``is_paid`` is asynchronous on
    purpose, so the answer can never be a stale cached one.

    ``live_entities`` and ``live_lookup_timeout`` are the live-data seam D16's
    steps 2 and 3 ask for. It is an injection point: the production default is
    :class:`~chime_deck.ha.HaEntityInfo` over ``ha_websocket``, installed by
    the lifespan once that client exists, so the rows carry live state and the
    icon chain reaches its Home Assistant steps.
    :data:`~chime_deck.entity_info.NO_LIVE_ENTITY_INFO` survives only for an
    install with no Home Assistant configured, where those two steps never hit and
    icons come from configuration.
    ``live_lookup_timeout`` bounds each lookup (the caller's bound; the adapter's
    own nested budgets are
    :data:`~chime_deck.ha.LIVE_LOOKUP_BUDGET_S` and
    :data:`~chime_deck.ha.REGISTRY_BUDGET_S`), and a timeout or an exception
    degrades to configuration-only (no warning, no failure).

    ``ha_client`` is the borrowed transport for the Home Assistant routes, for
    the ``ha_tts`` speech engine and for the press-time Template renderer. It is
    distinct from ``downloader``'s ``sound_client``: that one fetches sound
    files, this one talks to Home Assistant. It is ``None`` in production, which
    lets each caller build and close its own client, and a test injects one
    backed by ``httpx.MockTransport``. The app never closes it: HSD-qr3k
    deliberately did not create a second transport, so the settings page's
    connection probe borrows this one too.

    ``ha_websocket`` is the lifespan-owned reconnecting state client, distinct
    from the borrowed ``ha_client`` HTTP transport. The lifespan starts it,
    installs :class:`~chime_deck.ha.HaEntityInfo` over it and closes it at
    shutdown (``aclose()`` is a no-op on a client that was never started, so a
    test may inject a never-opened one). ``None`` means Home Assistant is not
    configured, and :func:`create_app`'s parameter is the offline injection seam.

    ``piper_probe`` is the one environment probe for local Piper (D22). The
    engine resolver, the voice manager and the settings routes all share it, so
    those three cannot disagree about this machine's RAM, the optional extra or
    the configured voice model.

    ``voices`` downloads and describes the Piper voice models in ``data/voices``,
    using that same probe. It is constructed here and reads nothing, so building
    an app opens nothing.

    ``voice_downloads`` runs those downloads as background jobs. It is
    constructed but not started, following the same "building an app opens
    nothing" rule.

    ``bake_store`` is the one durable pre-rendered-clip directory shared by the
    resolver and the startup sweeper, so both take the same ``asyncio.Lock``: a
    sweep can never delete a directory an in-flight bake is writing. Building the
    app creates no directory; the first static phrase does.

    ``speech_engines`` is the offline injection seam for the speech engine list,
    so an API test never probes a real binary or opens a socket. ``None`` lets the
    lifespan build ``remote_tts``, ``ha_tts``, ``piper`` and ``espeak_ng``.

    ``bake_sweeper`` is the lifespan-owned once-per-boot pre-render sweep. It is
    constructed inside the lifespan, after the ``ha_tts`` client exists, and
    cancelled before the resolver is closed, so the sweep never outlives the
    engine it renders with.

    ``selection`` is the volatile D29 tile-selection state (HSD-u6k5): an
    in-memory set of entity ids that is never persisted — no SQLite column, no
    migration, no settings key — because a power blip must disable the
    conditional alarms rather than leave one armed forever (D29 §4). It is a
    ``default_factory`` field so a directly constructed state gets a fresh, empty
    selection, exactly like a fresh install; the lifespan clears it at startup
    for the same reason.

    This object is attached to the ASGI app as ``app.state.doorbell``.
    """

    config: ServerConfig
    store: ConfigStore
    hub: StateHub
    health: HealthRegistry
    started_monotonic: float
    version: str
    probes: ConnectionProbes
    chime: ChimePipeline
    sounds: SoundLibrary
    downloader: SoundDownloader
    clock: Callable[[], float] = time.monotonic
    probe_timeout: float = PROBE_TIMEOUT_S
    speech: TtsSpeechSource | None = None
    ha_client: httpx.AsyncClient | None = None
    ha_websocket: HomeAssistantWebSocketClient | None = None
    paid_providers: PaidProviderLookup = NO_PAID_PROVIDERS
    live_entities: LiveEntityInfo = NO_LIVE_ENTITY_INFO
    live_lookup_timeout: float = LIVE_LOOKUP_TIMEOUT_S
    piper_probe: PiperProbe | None = None
    voices: VoiceManager | None = None
    voice_downloads: VoiceDownloadRegistry | None = None
    bake_store: BakeStore | None = None
    bake_sweeper: BakeSweeper | None = None
    speech_engines: Sequence[TtsEngine] | None = None
    selection: SelectionService = field(default_factory=SelectionService)
    """The volatile D29 tile-selection state (HSD-u6k5).

    Constructed empty, and never persisted: ``/api/tiles/{id}/selection`` toggles
    it, the hub reads it through the selection seam, and the lifespan clears it at
    startup so every boot starts with every tile unselected. A directly
    constructed state gets a fresh empty one, exactly like a fresh install.
    """

    auth: TokenGate = field(default_factory=TokenGate)
    """The optional shared-token gate (HSD-0jke).

    Constructed empty, which means "no authentication": the lifespan loads
    ``server.token`` into it and refreshes it on every committed settings change,
    and both the middleware and the login route read this one object. A directly
    constructed state keeps the gate off, exactly like a fresh install.
    """

    admin: AdminGate = field(default_factory=AdminGate)
    """The configuration-interface credential (HSD-n6wi).

    Constructed empty, which means "no password and no token": the admin listener
    is then open by the owner's choice, and the startup warning says what that
    exposes. The lifespan loads ``server.admin_password_hash`` and the shared
    token into it and refreshes both on every committed settings change; the admin
    wrapper, the login route and the status route all read this one object, so the
    three cannot disagree about which credential the listener wants. A directly
    constructed state keeps it open, exactly like a fresh install.
    """

    def uptime_s(self) -> float:
        """Return seconds elapsed since the application was created."""
        return max(0.0, self.clock() - self.started_monotonic)


def placeholder_page(version: str) -> HTMLResponse:
    """Render the stand-in page shown when ``web/dist`` does not exist.

    :param version: application version shown on the page.
    """
    return HTMLResponse(PLACEHOLDER_HTML.replace("__VERSION__", version))


def _is_reserved_path(full_path: str) -> bool:
    """Return whether *full_path* belongs to the API and must not reach the UI.

    :param full_path: request path with the leading slash stripped.
    """
    return full_path == "api" or full_path.startswith("api/") or full_path == "ws"


async def _stop_quietly(what: str, stop: Awaitable[None]) -> None:
    """Await *stop*, logging a failure instead of letting it escape shutdown.

    Only :class:`Exception` is caught: a cancellation must still propagate, so
    an operator's SIGTERM is never blocked by a stop that refuses to finish.
    The warning, rather than a silent ``suppress``, is deliberate -- a torn-down
    subsystem is worth reporting.

    :param what: human-readable name of the piece being stopped, used in the log.
    :param stop: coroutine that stops that piece.
    """
    try:
        await stop
    except Exception as exc:
        _LOGGER.warning("shutdown: %s did not stop cleanly: %s: %s", what, type(exc).__name__, exc)


class HaWebSocketFactory(Protocol):
    """Constructs one Home Assistant websocket client for a settings generation.

    This is the construction seam a *rebuild* needs: an injected client instance
    cannot be re-created, so the lifespan calls this factory for the first client
    and for every rebuild. A test or the browser bench passes one to keep the
    rebuild offline while still receiving the app's own health registry and state
    callback.
    """

    def __call__(
        self,
        *,
        base_url: str,
        token: str,
        health: HealthRegistry,
        on_state_change: Callable[[HaEntityState], None],
    ) -> HomeAssistantWebSocketClient:
        """Build a client that opens nothing until it is started.

        :param base_url: the stored ``ha.base_url``.
        :param token: the stored ``ha.token``; never logged.
        :param health: the app's own registry, so a rebuild reports into it.
        :param on_state_change: the hub's state callback.
        """


def create_app(
    config: ServerConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
    probes: ConnectionProbes | None = None,
    probe_timeout: float = PROBE_TIMEOUT_S,
    wiring: Wiring | None = None,
    sound_client: httpx.AsyncClient | None = None,
    ha_client: httpx.AsyncClient | None = None,
    ha_websocket: HomeAssistantWebSocketClient | None = None,
    ha_websocket_factory: HaWebSocketFactory | None = None,
    speech_engines: Sequence[TtsEngine] | None = None,
) -> FastAPI:
    """Build the ASGI application for *config*.

    Nothing is opened here: the store, hub and health registry are constructed
    but idle until the lifespan starts them, which keeps import time and test
    collection free of side effects.

    :param config: resolved host, port, data directory and log level.
    :param clock: monotonic clock used for uptime; injectable for tests.
    :param probes: connection probes for the settings page; ``None`` uses
        :class:`~chime_deck.api.LiveProbes` over the real store.
    :param probe_timeout: seconds each connection test may take before the
        settings route reports a timeout.
    :param wiring: substitutes for the devices the lifespan would open; ``None``
        resolves :func:`~chime_deck.wiring.default_wiring` at call time, so a
        test can replace that factory and make the whole app offline.
    :param sound_client: borrowed HTTP client for URL downloads; a test injects
        one backed by ``httpx.MockTransport``. It is never closed by the app.
        ``None`` makes :class:`~chime_deck.sounds.SoundDownloader` own (and
        close) one client per download.
    :param ha_client: borrowed HTTP client for the Home Assistant routes and the
        ``ha_tts`` speech engine; a test injects one backed by
        ``httpx.MockTransport``. It is never closed by the app. ``None`` lets
        each caller own (and close) its own client.
    :param ha_websocket: the offline injection seam for the reconnecting state
        client; a test passes a client built with a scripted ``connect``
        factory. The lifespan then uses it instead of constructing one, and
        still starts it and closes it. ``None`` means the lifespan reads
        ``ha.base_url``/``ha.token`` and builds a real client when they are set.
    :param ha_websocket_factory: the construction seam a *rebuild* needs — an
        injected instance cannot be re-created. The lifespan calls it for the
        first client and again whenever a committed ``ha.base_url``/``ha.token``
        change must replace the running one, so a test or the browser bench keeps
        that rebuild offline while the client still writes into the app's own
        health registry and reports through the app's own state callback.
        ``None`` constructs :class:`~chime_deck.ha.HomeAssistantWebSocketClient`
        itself. ``ha_websocket`` (an instance) stays supported and is still used
        as the first client.
    :param speech_engines: the offline injection seam for the speech engine
        list, so an API test never probes a real binary or opens a socket;
        ``None`` builds ``remote_tts``, ``ha_tts``, ``piper`` and ``espeak_ng``.
    """
    store = ConfigStore(config.database_path)
    health = HealthRegistry()
    selection = SelectionService()
    hub = StateHub(store, health, icons_dir=config.resolved_icons_dir, selection=selection)
    health.set_on_change(hub.notify_health)

    def _on_selection_change(entity_id: str, selected: bool) -> None:
        """Forward one D29 selection change to the hub and the MQTT publisher.

        The two consumers want different things from one seam. The hub is told
        *which* entity changed and nothing else: it re-reads the current value from
        the selection seam at flush time (see
        :meth:`~chime_deck.hub.StateHub.notify_selection`), so a toggle-on and
        a toggle-off inside one tick coalesce to the final truth. The MQTT
        publisher is told the new value as well, because it publishes that value
        straight to the broker and has no second chance to read it.

        :param entity_id: Home Assistant entity id whose selection changed.
        :param selected: the new value.
        """
        hub.notify_selection(entity_id=entity_id)
        chime.notify_selection(entity_id, selected)

    # Resolved here, not as a default argument: the default would be bound at
    # import time and replacing the factory afterwards would have no effect.
    resolved_wiring = wiring if wiring is not None else default_wiring()
    chime = ChimePipeline(
        store=store,
        hub=hub,
        health=health,
        config=config,
        wiring=resolved_wiring,
        selection=selection,
    )
    # The selection service is the only writer of D29 state; this is the seam that
    # turns a toggle into one ``entity_state`` frame and one retained MQTT state
    # publish, exactly like the health registry above. Installed after ``chime``
    # exists because the MQTT half forwards through it.
    selection.set_on_change(_on_selection_change)
    # Constructed, not started: the first scan (and the demo seeding) belongs to
    # the lifespan, which is why building an app still creates no directory.
    library = SoundLibrary(store, config.resolved_sounds_dir)
    downloader = SoundDownloader(
        store,
        config.resolved_sounds_dir,
        lock=library.write_lock,
        client=sound_client,
    )
    # One probe per process: the engine, the voice manager and the settings
    # routes share it, so a download and a press cannot disagree about RAM, the
    # optional extra or the configured model. The registry is constructed but
    # not started, so building an app still opens nothing.
    probe = PiperProbe(store)
    voices = VoiceManager(probe, config.resolved_voices_dir)
    downloads = VoiceDownloadRegistry(voices)

    def _started_audio_backend() -> AudioBackend | None:
        """Return the pipeline's started speaker, or ``None`` before startup.

        Deliberately lazy: the chime pipeline constructs and starts its backend
        inside the lifespan, after this probe set exists, so the accessor is
        called per request instead of binding ``None`` now.
        """
        return chime.backend

    app_state = AppState(
        config=config,
        store=store,
        hub=hub,
        health=health,
        started_monotonic=clock(),
        version=__version__,
        probes=(
            probes
            if probes is not None
            else LiveProbes(
                store,
                client=ha_client,
                audio_backend=_started_audio_backend,
                # Passing the wiring's factory keeps the default probe set offline
                # under the test suite's autouse offline wiring; the production
                # ``default_wiring()`` leaves it ``None``, so the real paho
                # factory is used.
                mqtt_client_factory=resolved_wiring.mqtt_client_factory,
            )
        ),
        chime=chime,
        sounds=library,
        downloader=downloader,
        clock=clock,
        probe_timeout=probe_timeout,
        ha_client=ha_client,
        ha_websocket=ha_websocket,
        piper_probe=probe,
        voices=voices,
        voice_downloads=downloads,
        bake_store=BakeStore(baked_root(config.data_dir)),
        speech_engines=speech_engines,
        selection=selection,
    )
    # The real D25 lookup (HSD-s856): the provider registry reads the live
    # configuration on every question, so a provider edited in Settings applies
    # to the very next save. The dataclass default stays NO_PAID_PROVIDERS for a
    # directly constructed state and for the tests that want D25 dormant.
    app_state.paid_providers = ConfiguredProviders(store)

    static_dir = config.resolved_static_dir
    has_static = static_dir.is_dir()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Start the pieces in order and stop them in reverse."""
        ha_unsubscribe: Callable[[], None] | None = None
        auth_unsubscribe: Callable[[], None] | None = None
        settings_unsubscribe: Callable[[], None] | None = None
        # The generation invariant: ``ha_generation`` is exactly the settings the
        # installed client was built from, and ``None`` exactly when no client is
        # installed. It is compared, never logged.
        ha_generation: tuple[str, str] | None = None
        ha_reload_lock = asyncio.Lock()

        async def _refresh_auth_gate(event: ConfigEvent) -> None:
            """Reload the shared token after a committed settings change.

            Refreshed on **any** settings change rather than filtered on
            ``server.token`` in ``payload["keys"]``: the read is one row, and a key
            filter would buy nothing while risking a stale gate for a future key
            that also feeds it. The handler re-reads the store inside the write,
            like :func:`chime_deck.ha.live.watch_tile_changes`, so no task is
            spawned.

            The ``try``/``except`` is required: this is an ``EventBus`` subscriber,
            so a store failure must be logged here instead of being raised into
            ``publish``.

            :param event: committed configuration change, from the store's bus.
            """
            if event.name is not ConfigEventName.SETTINGS_CHANGED:
                return
            try:
                await app_state.auth.refresh(store)
            except Exception as exc:
                _LOGGER.warning("could not reload the shared token from settings: %s", exc)
            try:
                await app_state.admin.refresh(store)
            except Exception as exc:
                _LOGGER.warning(
                    "could not reload the configuration-interface password from settings: %s",
                    exc,
                )

        def _on_ha_state(state: HaEntityState) -> None:
            """Forward one Home Assistant state change to the hub.

            :param state: the merged state the websocket client reports.
            """
            hub.notify_entity_state(
                entity_id=state.entity_id,
                state=state.state,
                attributes=state.attributes,
                last_changed=state.last_changed,
            )

        async def _read_ha_settings() -> tuple[str, str] | None:
            """Return the stored ``(base_url, token)``, or ``None`` when unconfigured.

            Reads exactly the two rows the client is built from, in one place, so
            the first client and every later rebuild agree on what "the settings"
            means. The token is never logged.

            :returns: ``(base_url, token)``; ``None`` when ``ha.base_url`` is blank
                or no ``ha.token`` is stored, i.e. when this install has no Home
                Assistant at all.
            :raises HaConfigurationError: if the stored token is not a string.
            """
            base_url = await store.get_setting("ha.base_url")
            base_url = base_url if isinstance(base_url, str) else ""
            if not base_url.strip() or not await store.has_secret("ha.token"):
                return None
            token = await store.get_setting("ha.token")
            if not isinstance(token, str):
                raise HaConfigurationError(
                    "the ha.token setting is not a string — add a long-lived access "
                    "token in Settings"
                )
            return base_url, token

        def _construct_ha_client(
            base_url: str, token: str, *, step: str
        ) -> tuple[HomeAssistantWebSocketClient | None, str | None]:
            """Build one client through the construction seam.

            Every path that cannot build a client logs the same ``configured but
            unusable`` WARNING, and no path mentions the token.

            :param base_url: stored ``ha.base_url``.
            :param token: stored ``ha.token``; never logged.
            :param step: log prefix, ``"startup: 6/7"`` during boot or
                ``"home assistant:"`` for a later rebuild.
            :returns: the client and ``None``, or ``None`` and the reason these
                settings are unusable.
            """
            factory: HaWebSocketFactory = (
                ha_websocket_factory
                if ha_websocket_factory is not None
                else HomeAssistantWebSocketClient
            )
            try:
                client = factory(
                    base_url=base_url,
                    token=token,
                    health=health,
                    on_state_change=_on_ha_state,
                )
            except HaConfigurationError as exc:
                _LOGGER.warning(
                    "%s Home Assistant is configured but unusable: %s; tiles show "
                    "configuration-only icons until ha.base_url is fixed",
                    step,
                    exc,
                )
                return None, str(exc)
            except Exception as exc:
                # Defensive: no traceback, and the same shape as the line above.
                _LOGGER.warning(
                    "%s Home Assistant is configured but unusable: %s; tiles show "
                    "configuration-only icons until ha.base_url is fixed",
                    step,
                    exc,
                )
                return None, str(exc)
            return client, None

        async def _install_ha_watch(
            client: HomeAssistantWebSocketClient,
            *,
            base_url: str,
            injected: bool,
            at_startup: bool,
            rebuilt: bool = False,
        ) -> None:
            """Subscribe the client to the tiles and install the live-data seam.

            :param client: the started-by-this-call websocket client.
            :param base_url: stored Home Assistant address, for the log line.
            :param injected: whether the client was injected rather than built.
            :param at_startup: whether this runs during the boot sequence.
            :param rebuilt: whether this replaces a running client after a
                committed ``ha.base_url``/``ha.token`` change, which has its own
                single log line instead of the late-start one.
            """
            nonlocal ha_unsubscribe
            # Installed for both branches: an injected client is a real client too,
            # and the hub only hears about state changes through this callback.
            client.set_on_state_change(_on_ha_state)
            entity_ids = await sync_tile_ids(store, client)
            ha_unsubscribe = watch_tile_changes(store, client)
            await client.start()
            app_state.live_entities = HaEntityInfo(client)
            hub.set_live_entities(app_state.live_entities, timeout_s=app_state.live_lookup_timeout)
            if rebuilt:
                _LOGGER.info(
                    "home assistant: rebuilt the client for the new settings; watching %d "
                    "entities at %s",
                    len(entity_ids),
                    base_url,
                )
            elif not at_startup:
                _LOGGER.info(
                    "home assistant: configured after startup; watching %d entities at %s",
                    len(entity_ids),
                    base_url,
                )
            elif injected:
                _LOGGER.info(
                    "startup: 6/7 watching Home Assistant for %d entities (injected client)",
                    len(entity_ids),
                )
            else:
                _LOGGER.info(
                    "startup: 6/7 watching Home Assistant at %s for %d entities",
                    base_url,
                    len(entity_ids),
                )

        async def _start_home_assistant() -> None:
            """Start the state client and install the live-data seam, when configured.

            Every path logs one ``startup: 6/7`` line and none of them mentions
            the token. An injected client is used as it is (a test's scripted
            factory), a stored ``ha.base_url`` plus ``ha.token`` build a real
            one, and anything else leaves the tiles configuration-only.

            :raises HaConfigurationError: if the stored token is not a string; the
                startup failure handling owns that, exactly as before.
            """
            nonlocal ha_generation
            if app_state.ha_websocket is not None:
                # An injected client (a test or the bench) is used as it is. Its
                # credentials are not necessarily the store's, so the generation
                # it stands for is the store's settings *if they can be read*; a
                # malformed stored token must not fail this startup.
                try:
                    ha_generation = await _read_ha_settings()
                except HaConfigurationError:
                    ha_generation = None
                await _install_ha_watch(
                    app_state.ha_websocket, base_url="", injected=True, at_startup=True
                )
                return
            settings = await _read_ha_settings()
            if settings is None:
                _LOGGER.info(
                    "startup: 6/7 Home Assistant is not configured; tiles show "
                    "configuration-only icons"
                )
                return
            client, _reason = _construct_ha_client(*settings, step="startup: 6/7")
            if client is None:
                return
            # Assigned before the install so a failure there still leaves the
            # client owned by the lifespan, which closes it at shutdown.
            app_state.ha_websocket = client
            await _install_ha_watch(client, base_url=settings[0], injected=False, at_startup=True)
            ha_generation = settings

        async def _apply_ha_settings(event: ConfigEvent) -> None:
            """Rebuild, start or stop the websocket client for a committed settings change.

            This is what makes the first-run wizard work without a restart *and*
            what makes a committed ``ha.base_url``/``ha.token`` change take effect
            in place: the old client is torn down first, then one new client is
            built from the settings as they now stand. Keeping the old client
            would silently keep old credentials, which is the exact lie this path
            exists to prevent.

            The ``try``/``except`` is required: this is an ``EventBus`` subscriber,
            so a failure must be logged here instead of being raised into
            ``publish``. The lock collapses the one-event-per-key burst of a single
            ``PATCH /api/settings``, and the generation guard drops the rest: a
            second event that resolves to the installed generation does nothing.

            :param event: committed configuration change, from the store's bus.
            """
            nonlocal ha_generation, ha_unsubscribe
            if event.name is not ConfigEventName.SETTINGS_CHANGED:
                return
            keys = event.payload.get("keys", ())
            if not isinstance(keys, tuple):
                return
            if "ha.base_url" not in keys and "ha.token" not in keys:
                return
            try:
                async with ha_reload_lock:
                    failure: str | None = None
                    try:
                        settings = await _read_ha_settings()
                    except HaConfigurationError as exc:
                        # A token that is not a string is a *failed* rebuild, not an
                        # exception out of this subscriber: the old client still has
                        # to go, and the header has to say why.
                        settings = None
                        failure = str(exc)
                    if ha_generation is not None and settings == ha_generation:
                        return
                    if failure is None and ha_generation is None and settings is None:
                        # No client installed and none configured: a repeated event
                        # has nothing to do. A failure is never swallowed here: it
                        # still has to reach the header.
                        return
                    # Torn down first, so the old client's ``stop()`` health write
                    # cannot clobber the new client's final status.
                    if ha_unsubscribe is not None:
                        ha_unsubscribe()
                        ha_unsubscribe = None
                    if app_state.ha_websocket is not None:
                        await app_state.ha_websocket.aclose()
                        app_state.ha_websocket = None
                    app_state.live_entities = NO_LIVE_ENTITY_INFO
                    hub.set_live_entities(NO_LIVE_ENTITY_INFO)
                    ha_generation = None
                    if settings is None:
                        if failure is not None:
                            health.set("ha", HealthStatus.ERROR, detail=failure)
                            _LOGGER.warning(
                                "home assistant: the stored settings are unusable: %s", failure
                            )
                            return
                        health.set(
                            "ha",
                            HealthStatus.NOT_CONFIGURED,
                            detail="home assistant is no longer configured; tiles show "
                            "configuration-only icons",
                        )
                        _LOGGER.info(
                            "home assistant: no longer configured; tiles show "
                            "configuration-only icons"
                        )
                        return
                    client, reason = _construct_ha_client(*settings, step="home assistant:")
                    if client is None:
                        # Unlike the MQTT reload, which keeps the running bridge, an
                        # unusable ``ha.*`` change must not keep the old client:
                        # keeping it would silently keep old credentials.
                        health.set(
                            "ha",
                            HealthStatus.ERROR,
                            detail=reason or "the stored Home Assistant settings are unusable",
                        )
                        return
                    # Assigned before the install so a failure there still leaves the
                    # client owned by the lifespan, which closes it at shutdown.
                    app_state.ha_websocket = client
                    await _install_ha_watch(
                        client,
                        base_url=settings[0],
                        injected=False,
                        at_startup=False,
                        rebuilt=True,
                    )
                    ha_generation = settings
            except Exception as exc:
                _LOGGER.warning(
                    "could not apply the Home Assistant settings change: %s: %s",
                    type(exc).__name__,
                    exc,
                )

        try:
            _LOGGER.info(
                "startup: 1/7 data directory %s (database %s)",
                config.data_dir,
                config.database_path,
            )
            # Loading libmpv is the bulk of a first audio start, so start it here
            # to overlap the store, the sound-library scan and the speech engine
            # instead of paying it inside the audio start budget.
            chime.warm_up_audio()
            _LOGGER.info("startup: 2/7 opening configuration store")
            await store.connect()
            # Before ``hub.start()``, so no client can ever be handed a stale value
            # and the clearing broadcast goes to nobody. Selection is volatile by
            # design (D29 §4): every boot starts with every tile unselected.
            app_state.selection.clear()
            _LOGGER.info(
                "startup: tile selection cleared (volatile by design, D29: every tile "
                "starts unselected)"
            )
            # Register the stored credentials before anything else can log them.
            try:
                await refresh_secret_scrubber(store)
            except Exception as exc:
                _LOGGER.warning("could not register stored secrets for log scrubbing: %s", exc)
            # Load the shared token once, then keep it in step with the store, so a
            # PATCH or an import takes effect on the next request with no restart.
            try:
                await app_state.auth.refresh(store)
            except Exception as exc:
                _LOGGER.warning("could not read the shared token from settings: %s", exc)
            # The admin listener's credential (HSD-n6wi) comes from the same two rows
            # plus the password hash; loading it here means the startup warning
            # below is the truth about the gate the first request will meet.
            try:
                await app_state.admin.refresh(store)
            except Exception as exc:
                _LOGGER.warning(
                    "could not read the configuration-interface password from settings: %s", exc
                )
            auth_unsubscribe = store.events.subscribe(_refresh_auth_gate)
            settings_unsubscribe = store.events.subscribe(_apply_ha_settings)
            try:
                applied_level = await apply_stored_log_level(config, store)
            except Exception as exc:
                _LOGGER.warning("could not apply the stored log level: %s", exc)
                applied_level = None
            if applied_level is not None:
                _LOGGER.info("log level set to %s by the system.log_level setting", applied_level)
            _LOGGER.info("startup: 3/7 starting state hub")
            await hub.start()
            try:
                if config.seed_demo_sounds:
                    known = tuple(s.path for s in await store.list_sounds() if s.path is not None)
                    await asyncio.to_thread(
                        copy_bundled_demo_sounds,
                        config.resolved_sounds_dir,
                        assets_dir=bundled_demo_sounds_dir(),
                        known_paths=known,
                    )
                report = await app_state.sounds.scan(budget_s=STARTUP_SCAN_BUDGET_S)
            except Exception as exc:
                _LOGGER.warning("startup: sound library not ready: %s: %s", type(exc).__name__, exc)
            else:
                _LOGGER.info(
                    "startup: sound library: %d sounds (added %d, missing %d)",
                    report.total,
                    report.added,
                    report.missing,
                )
            try:
                if app_state.voices is not None:
                    removed = app_state.voices.sweep_stale_temps()
                    _LOGGER.info(
                        "startup: voice directory %s: %d stale temporary director%s removed",
                        config.resolved_voices_dir,
                        removed,
                        "y" if removed == 1 else "ies",
                    )
            except Exception as exc:
                _LOGGER.warning("could not sweep the voice directory: %s", exc)
            try:
                app_state.speech = await build_speech_source(
                    store,
                    data_dir=config.data_dir,
                    health=health,
                    engines=app_state.speech_engines,
                    ha_client=app_state.ha_client,
                    voices_dir=config.resolved_voices_dir,
                    piper_probe=app_state.piper_probe,
                    bakes=app_state.bake_store,
                )
            except Exception as exc:
                _LOGGER.warning("could not prepare the speech engine: %s", exc)
            _LOGGER.info("startup: audio, the press path and the MQTT bridge")
            await chime.start(speech=app_state.speech)
            # Publish the health flips the new subsystems just recorded before any
            # client can connect. The snapshot already carries them, so a client
            # that connected mid-startup would otherwise get a stale ``health``
            # frame racing its first ``snapshot``. No client is registered yet, so
            # this clears the pending flag and delivers to nobody.
            await hub.flush_once()
            if has_static:
                _LOGGER.info("startup: 4/7 serving static UI from %s", static_dir)
            else:
                _LOGGER.warning(
                    "startup: 4/7 no built UI found at %s; serving placeholder page", static_dir
                )
            if is_loopback(config.host):
                _LOGGER.info("startup: 5/7 listening on http://%s:%d", config.host, config.port)
            elif app_state.auth.required():
                _LOGGER.warning(
                    "startup: 5/7 listening on http://%s:%d — warning: this address is reachable "
                    "from the network; the optional shared token is set (D20), but it is not "
                    "transport encryption",
                    config.host,
                    config.port,
                )
            else:
                _LOGGER.warning(
                    "startup: 5/7 listening on http://%s:%d — warning: this address is reachable "
                    "from the network and the API has no authentication (D20)",
                    config.host,
                    config.port,
                )
            if app_state.auth.required():
                # No value, no length, no prefix: the operator only needs to know
                # that the gate is on.
                _LOGGER.info(
                    "startup: shared token authentication is ON — every state-bearing request "
                    "needs the cookie from the UI prompt or a matching bearer token"
                )
            admin_address = admin_bind(config)
            if admin_address is not None:
                admin_host, admin_port = admin_address
                _LOGGER.info(
                    "admin listener on %s:%d — configuration only; the dashboard is not "
                    "served there",
                    admin_host,
                    admin_port,
                )
                if not app_state.admin.required():
                    # The banner in the UI and this line must say what "unprotected"
                    # actually exposes: GET /api/entities carries each tile's
                    # current state, not just its configuration.
                    _LOGGER.warning(
                        "the configuration interface on %s:%d is UNPROTECTED — anyone who can "
                        "reach that address can read and change these settings, including "
                        "each tile's current state; set a password",
                        admin_host,
                        admin_port,
                    )
            try:
                await _start_home_assistant()
            except Exception as exc:
                _LOGGER.warning(
                    "startup: 6/7 Home Assistant could not be started: %s: %s",
                    type(exc).__name__,
                    exc,
                )
            try:
                # After Home Assistant, so ``ha_tts`` has its client, and before
                # "ready", so the retry is scheduled by the time the UI can see
                # the page. A sweep that cannot be scheduled must not stop the
                # doorbell: no bake is ever a correctness requirement.
                if app_state.speech is not None and app_state.bake_store is not None:
                    app_state.bake_sweeper = BakeSweeper(
                        store=store, speech=app_state.speech, bakes=app_state.bake_store
                    )
                    await app_state.bake_sweeper.start()
                    _LOGGER.info(
                        "startup: pre-rendered speech sweep scheduled once, "
                        "%.0f-%.0f s after start",
                        JITTER_MIN_S,
                        JITTER_MAX_S,
                    )
            except Exception as exc:
                _LOGGER.warning(
                    "could not schedule the pre-rendered speech sweep: %s: %s",
                    type(exc).__name__,
                    exc,
                )
            _LOGGER.info("startup: 7/7 ready version=%s", app_state.version)
        except BaseException:
            _LOGGER.exception("startup failed; stopping the pieces that did start")
            with contextlib.suppress(Exception):
                if app_state.bake_sweeper is not None:
                    await app_state.bake_sweeper.stop()
            with contextlib.suppress(Exception):
                await chime.stop()
            with contextlib.suppress(Exception):
                await hub.stop()
            if ha_unsubscribe is not None:
                with contextlib.suppress(Exception):
                    ha_unsubscribe()
            if auth_unsubscribe is not None:
                with contextlib.suppress(Exception):
                    auth_unsubscribe()
            if settings_unsubscribe is not None:
                with contextlib.suppress(Exception):
                    settings_unsubscribe()
            if app_state.ha_websocket is not None:
                with contextlib.suppress(Exception):
                    await app_state.ha_websocket.aclose()
            with contextlib.suppress(Exception):
                await store.close()
            raise

        try:
            yield
        finally:
            _LOGGER.info("shutdown: 1/4 stopping state hub")
            try:
                # The registry stop is deliberately unnumbered: it belongs to
                # this same stage, alongside the other background workers.
                if app_state.voice_downloads is not None:
                    await _stop_quietly(
                        "the voice download jobs", app_state.voice_downloads.aclose()
                    )
                if app_state.bake_sweeper is not None:
                    # Before the resolver: the sweep renders through it, so it must
                    # be cancelled while its engine still exists.
                    await _stop_quietly("the pre-render sweep", app_state.bake_sweeper.stop())
                if app_state.speech is not None:
                    await _stop_quietly("the local Piper worker", app_state.speech.aclose())
                if app_state.voices is not None:
                    # Sync no-op today: the manager owns no client or thread of its own.
                    app_state.voices.close()
                await _stop_quietly("the chime pipeline", chime.stop())
                await _stop_quietly("the state hub", hub.stop())
            finally:
                # The Home Assistant websocket client is part of the documented
                # lifetime contract: the lifespan owns it (it is the only thing
                # that started it), so the lifespan closes it. Never a
                # ``ResourceWarning`` claim -- ``stop()`` is bounded by the
                # client's own ``shutdown_timeout_s``, so this cannot hang.
                _LOGGER.info("shutdown: 2/4 stopping the Home Assistant websocket client")
                try:
                    if ha_unsubscribe is not None:
                        ha_unsubscribe()
                    if auth_unsubscribe is not None:
                        # The gate's subscription belongs to the store's bus, which
                        # stage 3 closes, so it is dropped here.
                        auth_unsubscribe()
                    if settings_unsubscribe is not None:
                        settings_unsubscribe()
                    if app_state.ha_websocket is not None:
                        await _stop_quietly(
                            "the Home Assistant websocket client", app_state.ha_websocket.aclose()
                        )
                finally:
                    # Never skipped: an unclosed aiosqlite connection leaves a
                    # non-daemon worker thread that threading._shutdown joins with
                    # no timeout, so the interpreter would never exit (HSD-cwvc).
                    _LOGGER.info("shutdown: 3/4 closing configuration store")
                    await _stop_quietly("the configuration store", store.close())
            _LOGGER.info("shutdown: 4/4 shutdown complete")

    app = FastAPI(
        title="ChimeDeck",
        version=app_state.version,
        lifespan=lifespan,
    )
    app.state.doorbell = app_state
    # Registered before the catch-all route below, so no API path can fall through
    # to the UI handler and every error leaves in the standard envelope.
    install_error_handlers(app)
    # The shared-token gate (HSD-0jke) is a middleware rather than a dependency
    # because it must cover routes added later, the ``/ws`` handshake and the
    # ``/custom-icons`` files, none of which a per-route dependency reaches. It
    # shares the one ``TokenGate`` the lifespan refreshes.
    app.add_middleware(SharedTokenMiddleware, gate=app_state.auth)
    app.include_router(build_auth_router(app_state))
    app.include_router(build_settings_router(app_state))
    app.include_router(build_audio_router(app_state))
    app.include_router(build_mqtt_router(app_state))
    app.include_router(build_buttons_router(app_state))
    app.include_router(build_entities_router(app_state))
    app.include_router(build_tiles_router(app_state))
    app.include_router(build_sounds_router(app_state))
    app.include_router(build_ha_router(app_state))
    app.include_router(build_icons_router(config.resolved_icons_dir))
    app.include_router(build_tts_router(app_state))
    app.include_router(build_provider_router(app_state))
    app.include_router(build_history_router(app_state))
    app.include_router(build_config_router(app_state))
    app.include_router(build_about_router(app_state))

    async def _send_loop(websocket: WebSocket, client: HubClient) -> None:
        """Forward hub messages to one websocket, forever.

        :param websocket: accepted connection.
        :param client: hub client backing its queue.
        """
        while True:
            await websocket.send_text(await hub.next_payload(client))

    async def _receive_loop(websocket: WebSocket) -> None:
        """Wait for client frames only to notice a disconnect.

        The protocol is server-to-client in v1 (design 05 §4), so frames from the
        client are ignored; receiving is how the disconnect is observed. Starlette
        hands back the ``websocket.disconnect`` message rather than raising, so
        the loop returns instead of calling ``receive()`` again.

        :param websocket: accepted connection.
        """
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return

    @app.get("/api/health")
    async def api_health() -> JSONResponse:
        """Report that the process is serving, with its version and uptime.

        ``status`` here means "this process is up": per-component health travels
        over ``/ws`` (design 05 §3).
        """
        return JSONResponse(
            {
                "status": "ok",
                "version": app_state.version,
                "uptime_s": round(app_state.uptime_s(), 3),
            }
        )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Serve the live push channel described by design 05 §4."""
        await websocket.accept()
        client = hub.register()
        try:
            # Send the snapshot first, directly, so it is ordered before any
            # event that was already queued for this client.
            await websocket.send_text(json.dumps(await hub.snapshot_message()))

            # The concurrent receive() detector below is load-bearing for the
            # "SIGTERM exits within 3 seconds" acceptance criterion: a handler
            # blocked only on queue.get() never notices the disconnect and
            # stalls uvicorn's shutdown. Do not "simplify" it away.
            send_task = asyncio.create_task(_send_loop(websocket, client))
            receive_task = asyncio.create_task(_receive_loop(websocket))
            done, pending = await asyncio.wait(
                {send_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                try:
                    task.result()
                except (WebSocketDisconnect, WebSocketDisconnected):
                    _LOGGER.debug("websocket client disconnected")
                except Exception:
                    _LOGGER.warning("websocket connection failed", exc_info=True)
        except (WebSocketDisconnect, WebSocketDisconnected):
            _LOGGER.debug("websocket client disconnected before the snapshot was sent")
        finally:
            hub.unregister(client)

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> Response:
        """Serve the built UI, falling back to the placeholder page.

        Registered last so it can never shadow ``/api/*`` or ``/ws``.

        :param full_path: request path with the leading slash stripped.
        """
        if _is_reserved_path(full_path):
            raise HTTPException(status_code=404, detail="not found")
        if not has_static:
            return placeholder_page(app_state.version)

        root = static_dir.resolve()
        candidate = (root / full_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise HTTPException(status_code=404, detail="not found")
        if candidate.is_file():
            return FileResponse(candidate)
        index = root / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="not found")

    return app
