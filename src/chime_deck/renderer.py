"""The renderer role: one doorbell renders speech for another (D24).

This module builds the renderer ASGI app and the policy objects around it. The
renderer is the **one place in this project that accepts arbitrary text from the
network and burns CPU on it**, so it is deliberately the smallest possible
process: no store UI, no broker, no Home Assistant client, no audio device. It
answers two routes — a token-gated health probe and ``POST /api/tts/render`` —
and nothing else.

``--role renderer`` is the switch. D24's ``tts.renderer.enabled`` setting is
deliberately **not** implemented: a setting can be flipped by accident (or by
anyone who reaches an unauthenticated settings page), while a command-line flag
has to be typed on purpose and is visible in the process table and the unit
file. For the same reason the role has **no environment variable** either; see
:class:`chime_deck.config.ServerConfig`.

**This module never imports** :mod:`chime_deck.api` **at module level.** The
renderer's own imports of :mod:`chime_deck.api.errors` and
:mod:`chime_deck.api.render` happen inside :func:`create_renderer_app`,
because ``api.render`` imports this module for the request body cap and the
queue exception; a module-level import in both directions would be a cycle.
Importing this module therefore pulls in no route, no error handler and no
FastAPI application, which is also what keeps the renderer's import cost (and
every test that only wants its policy objects) small.

The renderer reuses the ordinary ``--host``/``--port``, so the bind defaults to
loopback (:data:`chime_deck.config.DEFAULT_HOST`, D13) and serving the LAN
means saying so. The bearer token is read **once at startup**, so rotating
``tts.renderer.token`` needs a restart — exactly like ``system.log_level``.
:meth:`chime_deck.store.ConfigStore.connect` applies migrations, so the
renderer role creates and migrates a full ``doorbell.db`` in its data directory
even though it only ever reads one setting from it.

Owning ticket: HSD-5de8.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fastapi import FastAPI

from . import __version__
from .config import ServerConfig, is_loopback, refresh_secret_scrubber
from .store import ConfigStore
from .tts import PiperEngine, PiperProbe, TtsEngine
from .tts.speech import ClosableEngine, SettingsReader

__all__ = [
    "RENDERER_CONFIG_EXIT_CODE",
    "RENDER_CLIENT_HEADROOM_S",
    "RENDER_MAX_BODY_BYTES",
    "RENDER_MAX_TEXT_CHARS",
    "RENDER_QUEUE_MAX",
    "RENDER_QUEUE_WAIT_S",
    "RENDER_RATE_LIMIT_MAX_KEYS",
    "RENDER_RATE_LIMIT_PER_MINUTE",
    "RENDER_RATE_WINDOW_S",
    "RENDER_SPOOL_DIRNAME",
    "RENDER_SPOOL_MAX_AGE_S",
    "RENDER_SYNTHESIS_TIMEOUT_S",
    "RenderQueue",
    "RenderQueueFull",
    "RenderRateLimiter",
    "RendererState",
    "RendererTokenMissing",
    "build_renderer_engines",
    "create_renderer_app",
    "fetch_renderer_token",
    "sweep_spool",
]

_LOGGER = logging.getLogger(__name__)

RENDER_QUEUE_MAX: Final = 4
"""Most render requests in flight at once, **including** those waiting for the lock.

The counter is a plain ``int`` on the event loop and is checked and incremented
with no ``await`` in between, so two coroutines cannot both see a free slot; the
decrement runs in a ``finally``. A request arriving at a full queue is refused
with ``429`` and the queue therefore cannot grow at all.
"""

RENDER_RATE_LIMIT_PER_MINUTE: Final = 30
"""Render requests one source address may make inside :data:`RENDER_RATE_WINDOW_S`."""

RENDER_RATE_WINDOW_S: Final = 60.0
"""Length of the fixed rate-limit window, in seconds."""

RENDER_RATE_LIMIT_MAX_KEYS: Final = 1024
"""Hard ceiling on the number of addresses the rate limiter remembers.

The window map is keyed by source IP — never by ``X-Forwarded-For``, which any
client can forge — so a hostile client could otherwise make the map grow without
bound by rotating addresses. At the ceiling the entry whose window closes
soonest is evicted before the new one is inserted. That eviction is an
occasional ``O(n)`` scan at ``n <= 1024``; it must **not** be "optimised" into a
heap, because the map is rebuilt from scratch every window and a heap would only
add bookkeeping to protect a scan that already costs microseconds.
"""

RENDER_QUEUE_WAIT_S: Final = 1.0
"""Longest a request may wait for the synthesis lock, in seconds.

Expiring here means the render **never started**, so the answer is ``429``
(queue full, retry) rather than ``503`` (the environment failed).
"""

RENDER_SYNTHESIS_TIMEOUT_S: Final = 6.0
"""Bound on the render itself, in seconds; expiry is a ``503``.

The arithmetic is the whole point: ``RENDER_QUEUE_WAIT_S +
RENDER_SYNTHESIS_TIMEOUT_S = 7.0``, which is
:data:`~chime_deck.tts.remote_tts.REMOTE_RENDER_BUDGET_S` (7.5 s, the
client's per-attempt budget) minus :data:`RENDER_CLIENT_HEADROOM_S`. A queued or
slow render therefore always answers inside the caller's budget, so the caller
reports the renderer's real verdict instead of timing out against it.
"""

RENDER_CLIENT_HEADROOM_S: Final = 0.5
"""Seconds the client needs for the connect plus the WAV transfer."""

RENDER_MAX_TEXT_CHARS: Final = 500
"""Longest phrase the renderer accepts, in code points.

The same cap as :data:`~chime_deck.tts.templating.MAX_TEMPLATE_CHARS`
(design 04 §6), so a phrase that can be spoken locally can also be rendered
remotely.
"""

RENDER_MAX_BODY_BYTES: Final = 16 * 1024
"""Largest request body the renderer will read, in bytes.

A 500-code-point phrase plus a tiny options object is about 6 KiB in the worst
case (four UTF-8 bytes per code point, plus JSON escaping), so 16 KiB leaves
room without allowing a body to be buffered indefinitely.
"""

RENDER_SPOOL_MAX_AGE_S: Final = 3600.0
"""Seconds a rendered file may stay in the spool directory before the sweep removes it."""

RENDER_SPOOL_DIRNAME: Final = "render-spool"
"""Name of the spool directory inside the data directory.

Never inside ``tts-cache``: that directory's ``prune()`` deletes files it does
not recognise, and a spool file being written during another process's prune
would vanish under the renderer.
"""

RENDERER_CONFIG_EXIT_CODE: Final = 2
"""Exit status for a renderer that refused to start because it is misconfigured.

Deliberately the same number argparse uses for a usage error: an operator or a
``systemd`` unit only needs "non-zero, the configuration is wrong", and two
different non-zero numbers would invite a reader to look for a distinction that
does not exist.
"""

_CONFIG_HINT: Final = (
    "Run the doorbell once against this data directory (the server role), set "
    "tts.renderer.token in Settings to a long random string, then start the renderer "
    "again and put the same value in tts.remote_tts.token on the panel. The renderer "
    "needs no broker, Home Assistant or audio device, so a headless box is a valid host."
)
"""What an operator must do to make the renderer start; never contains the value."""


class RendererTokenMissing(RuntimeError):
    """The renderer has no bearer token, so it must not accept network traffic.

    The message names the database the token was read from and the exact fix; it
    never contains, quotes or partially shows any value.
    """

    def __init__(self, database_path: Path) -> None:
        """Build the whole sentence from the database path.

        :param database_path: ``doorbell.db`` the token setting was read from.
        """
        super().__init__(
            f"the renderer role will not start: tts.renderer.token is empty in "
            f"{database_path}, and this endpoint accepts arbitrary text from the network and "
            f"burns CPU on it. {_CONFIG_HINT}"
        )
        self.database_path = database_path


class RenderRateLimiter:
    """A fixed-window request limiter keyed by source address.

    The clock is injectable so a test never sleeps. A window is
    :attr:`window_s` seconds long and starts at the first request from a key;
    requests inside it count against :attr:`limit`.
    """

    def __init__(
        self,
        *,
        limit: int = RENDER_RATE_LIMIT_PER_MINUTE,
        window_s: float = RENDER_RATE_WINDOW_S,
        max_keys: int = RENDER_RATE_LIMIT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the limiter; nothing is allocated per address until it is seen.

        :param limit: requests allowed per window.
        :param window_s: window length in seconds.
        :param max_keys: hard ceiling on remembered addresses.
        :param clock: monotonic clock, injectable for tests.
        """
        self._limit = limit
        self._window_s = window_s
        self._max_keys = max_keys
        self._clock = clock
        # key -> (window ends at, requests counted in it)
        self._windows: dict[str, tuple[float, int]] = {}

    def allow(self, key: str) -> float | None:
        """Count one request from *key*.

        :param key: source address, or ``"unknown"`` when the client is unknown.
        :returns: ``None`` when the request is allowed, otherwise the seconds
            left in the window that refused it.
        """
        now = self._clock()
        entry = self._windows.get(key)
        if entry is not None and now >= entry[0]:
            del self._windows[key]
            entry = None
        if entry is not None:
            if entry[1] >= self._limit:
                return max(0.0, entry[0] - now)
            self._windows[key] = (entry[0], entry[1] + 1)
            return None
        self._make_room(now)
        self._windows[key] = (now + self._window_s, 1)
        return None

    def _make_room(self, now: float) -> None:
        """Drop expired windows, evicting the soonest-closing one if still full.

        :param now: current clock value.
        """
        if len(self._windows) < self._max_keys:
            return
        expired = [key for key, (ends_at, _) in self._windows.items() if now >= ends_at]
        for key in expired:
            del self._windows[key]
        if len(self._windows) < self._max_keys:
            return
        soonest = min(self._windows, key=lambda key: self._windows[key][0])
        del self._windows[soonest]


class RenderQueueFull(RuntimeError):
    """The render queue is at :data:`RENDER_QUEUE_MAX`, so the request is refused."""


class RenderQueue:
    """A hard ceiling on renders in flight, counted on the event loop.

    Every in-flight render holds one slot, including a render that is only
    waiting for the synthesis lock, so a burst of callers is refused rather than
    queued: the caller has a 7.5 s budget and an unbounded queue would answer
    inside it only by accident.
    """

    def __init__(self, *, max_concurrent: int = RENDER_QUEUE_MAX) -> None:
        """Create an empty queue.

        :param max_concurrent: most renders allowed in flight at once.
        """
        self._max = max_concurrent
        self._pending = 0

    @property
    def pending(self) -> int:
        """Return how many renders are in flight right now."""
        return self._pending

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one render slot for the duration of the ``async with`` block.

        The check and the increment happen with no ``await`` between them, so
        the ceiling cannot be overshot; the decrement is in a ``finally`` so a
        failed or cancelled render still frees its slot.

        :raises RenderQueueFull: if every slot is taken.
        """
        if self._pending >= self._max:
            raise RenderQueueFull(
                f"this renderer renders at most {self._max} phrases at once and all of them are "
                "busy — press the button again in a moment"
            )
        self._pending += 1
        try:
            yield
        finally:
            self._pending -= 1


@dataclass(frozen=True, slots=True)
class RendererState:
    """Everything the renderer routes need, built once per process.

    ``token`` is stored **pre-encoded** as bytes so
    :func:`hmac.compare_digest` never has to see a ``str``: a lone surrogate in
    the configured value would otherwise make the comparison raise and turn an
    authentication check into a ``500``.

    ``engines`` is keyed by engine name and ``hosted`` is the same set
    formatted for a human sentence, so an unsupported name can be refused with a
    message that says what this renderer actually hosts.

    ``queue_wait_s`` and ``synthesis_timeout_s`` are the two halves of the
    caller's budget; ``synthesis_lock`` serialises the actual synthesis because
    the engines behind it own one worker each.

    ``max_text_chars`` is a field rather than a constant read from this module so
    a test can tighten it without touching the production cap.
    """

    token: bytes
    engines: Mapping[str, TtsEngine]
    hosted: str
    spool_dir: Path
    limiter: RenderRateLimiter
    queue: RenderQueue
    queue_wait_s: float
    synthesis_timeout_s: float
    clock: Callable[[], float]
    version: str
    started_monotonic: float
    synthesis_lock: asyncio.Lock
    max_text_chars: int = RENDER_MAX_TEXT_CHARS


async def fetch_renderer_token(config: ServerConfig) -> str:
    """Read ``tts.renderer.token`` from the data directory's database.

    The store is opened and closed here, so the CLI can refuse to start before
    any socket exists.

    :param config: resolved configuration naming the database.
    :raises RendererTokenMissing: if the configured token is empty.
    """
    async with ConfigStore(config.database_path) as store:
        value = await store.get_setting("tts.renderer.token")
    token = value.strip() if isinstance(value, str) else ""
    if not token:
        raise RendererTokenMissing(config.database_path)
    return token


def build_renderer_engines(config: ServerConfig, store: SettingsReader) -> tuple[TtsEngine, ...]:
    """Return the engines this renderer hosts: exactly local ``piper``.

    Hosting only a local engine is also what makes the endpoint safe against a
    request that names ``remote_tts``: there is no client engine in a renderer
    process, so a render request can never make the renderer call another
    renderer (or itself). Production hosts **only** ``piper``, which is what the
    ticket specifies; a test injects a fake through ``engines=`` instead.

    :param config: resolved configuration, naming the voices directory.
    :param store: settings reader the probe reads ``tts.piper.voice`` through.
    """
    return (PiperEngine(PiperProbe(store), voices_dir=config.resolved_voices_dir),)


def sweep_spool(
    directory: Path,
    *,
    max_age_s: float = RENDER_SPOOL_MAX_AGE_S,
    clock: Callable[[], float] = time.time,
) -> int:
    """Delete spool files older than *max_age_s* and return how many were removed.

    A missing directory and a file that vanished between listing and unlinking
    are both tolerated: this runs at startup, where a half-cleaned spool must
    never stop the renderer.

    :param directory: spool directory to sweep.
    :param max_age_s: age in seconds beyond which a file is stale.
    :param clock: wall clock, injectable for tests; file times are wall-clock.
    """
    cutoff = clock() - max_age_s
    removed = 0
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return 0
    for entry in entries:
        try:
            if not entry.is_file() or entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            _LOGGER.warning("could not remove the stale render file %s: %s", entry, exc)
            continue
        removed += 1
    return removed


def _token_bytes(token: str) -> bytes:
    """Encode the configured token for a comparison that cannot raise.

    A lone surrogate is encoded with ``surrogatepass`` rather than rejected, so
    a corrupt stored value fails closed at request time instead of making the
    renderer unbuildable.

    :param token: configured ``tts.renderer.token`` value.
    """
    return token.encode("utf-8", "surrogatepass")


def _close_engine(engine: TtsEngine) -> None:
    """Close one engine that owns a worker, logging a failure instead of raising.

    :param engine: engine that may satisfy :class:`ClosableEngine`.
    """
    if not isinstance(engine, ClosableEngine):
        return
    try:
        engine.close()
    except Exception as exc:
        _LOGGER.warning("could not close a renderer engine: %s: %s", type(exc).__name__, exc)


def create_renderer_app(
    config: ServerConfig,
    *,
    token: str,
    store: ConfigStore | None = None,
    engines: Sequence[TtsEngine] | None = None,
    clock: Callable[[], float] = time.monotonic,
    rate_limit: int = RENDER_RATE_LIMIT_PER_MINUTE,
    rate_window_s: float = RENDER_RATE_WINDOW_S,
    rate_max_keys: int = RENDER_RATE_LIMIT_MAX_KEYS,
    queue_max: int = RENDER_QUEUE_MAX,
    queue_wait_s: float = RENDER_QUEUE_WAIT_S,
    synthesis_timeout_s: float = RENDER_SYNTHESIS_TIMEOUT_S,
    spool_dir: Path | None = None,
) -> FastAPI:
    """Build the renderer ASGI application for *config*.

    ``token`` is **required** and keyword-only: "no token, no service" is in the
    signature, so a caller cannot build a renderer without deciding what its
    token is. An empty token still builds the app — the lifespan refuses to
    serve — which is what lets a programmatic ``uvicorn`` launch fail loudly
    instead of quietly accepting unauthorised renders.

    Nothing is opened here: the store is constructed but idle until the lifespan
    connects it, so importing and building still touch no file.

    :param config: resolved host, port, data directory and log level.
    :param token: value the ``Authorization`` header must carry.
    :param store: store to use; ``None`` opens the configured database.
    :param engines: engines to host; ``None`` builds the production ``piper``.
    :param clock: monotonic clock for uptime and the limiter; injectable.
    :param rate_limit: requests allowed per source address per window.
    :param rate_window_s: rate-limit window length in seconds.
    :param rate_max_keys: ceiling on remembered source addresses.
    :param queue_max: most renders in flight at once.
    :param queue_wait_s: seconds a request may wait for the synthesis lock.
    :param synthesis_timeout_s: seconds the render itself may take.
    :param spool_dir: directory rendered files are written to and swept from.
    """
    # Imported here, not at module level: ``api.render`` imports this module for
    # the request body cap and the queue exception, so a module-level import in
    # both directions would be a cycle.
    from .api.errors import install_error_handlers
    from .api.render import build_render_router

    resolved_store = store if store is not None else ConfigStore(config.database_path)
    spool = spool_dir if spool_dir is not None else config.data_dir / RENDER_SPOOL_DIRNAME
    if engines is None:
        engine_list: tuple[TtsEngine, ...] = build_renderer_engines(config, resolved_store)
    else:
        engine_list = tuple(engines)
    state = RendererState(
        token=_token_bytes(token.strip()),
        engines={engine.name: engine for engine in engine_list},
        hosted=", ".join(engine.name for engine in engine_list),
        spool_dir=spool,
        limiter=RenderRateLimiter(
            limit=rate_limit,
            window_s=rate_window_s,
            max_keys=rate_max_keys,
            clock=clock,
        ),
        queue=RenderQueue(max_concurrent=queue_max),
        queue_wait_s=queue_wait_s,
        synthesis_timeout_s=synthesis_timeout_s,
        clock=clock,
        version=__version__,
        started_monotonic=clock(),
        synthesis_lock=asyncio.Lock(),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Refuse to serve without a token, then prepare and later release the pieces."""
        if not state.token:
            missing = RendererTokenMissing(config.database_path)
            _LOGGER.error("%s", missing)
            raise missing
        await resolved_store.connect()
        try:
            try:
                await refresh_secret_scrubber(resolved_store)
            except Exception as exc:
                _LOGGER.warning("could not register stored secrets for log scrubbing: %s", exc)
            try:
                spool.mkdir(parents=True, exist_ok=True)
                removed = sweep_spool(spool, max_age_s=RENDER_SPOOL_MAX_AGE_S)
            except Exception as exc:
                _LOGGER.warning("could not prepare the render spool %s: %s", spool, exc)
            else:
                _LOGGER.info(
                    "startup: render spool %s: %d stale file%s removed",
                    spool,
                    removed,
                    "" if removed == 1 else "s",
                )
            for engine in engine_list:
                try:
                    await engine.warmup()
                except Exception as exc:
                    _LOGGER.warning(
                        "the %s render engine did not warm up: %s: %s",
                        engine.name,
                        type(exc).__name__,
                        exc,
                    )
            if is_loopback(config.host):
                _LOGGER.info(
                    "renderer: listening on http://%s:%d — loopback only, so only this machine "
                    "can render; pass --host 0.0.0.0 to serve the LAN (token read from %s)",
                    config.host,
                    config.port,
                    config.database_path,
                )
            else:
                _LOGGER.warning(
                    "renderer: listening on http://%s:%d — every request must carry "
                    "tts.renderer.token, this endpoint accepts arbitrary text and burns CPU on "
                    "it, and it must never be port-forwarded (token read from %s)",
                    config.host,
                    config.port,
                    config.database_path,
                )
        except BaseException:
            _LOGGER.exception("renderer startup failed; closing the store")
            with suppress(Exception):
                await resolved_store.close()
            raise

        try:
            yield
        finally:
            for engine in engine_list:
                _close_engine(engine)
            try:
                await resolved_store.close()
            except Exception as exc:
                _LOGGER.warning(
                    "shutdown: the configuration store did not close cleanly: %s: %s",
                    type(exc).__name__,
                    exc,
                )

    app = FastAPI(
        title="ChimeDeck renderer",
        version=state.version,
        lifespan=lifespan,
        # The renderer is the only LAN-exposed app in the project, so its
        # generated documentation must not be reachable at all.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.renderer = state
    install_error_handlers(app)
    app.include_router(build_render_router(state))
    return app
