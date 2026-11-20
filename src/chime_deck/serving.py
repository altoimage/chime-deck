"""Serving mechanics: one application, one or two listeners, one bounded shutdown.

The doorbell is one process with one event loop (decision D01) that may serve
**two** sockets: the loopback listener (the whole app) and the LAN admin listener
(configuration only, HSD-n6wi). ``uvicorn.run`` cannot do that — it builds and
runs exactly one :class:`uvicorn.Server` from a host and a port — so the CLI
builds a list of :class:`uvicorn.Config` objects here and :func:`serve` drives the
servers itself.

Two behaviours are load-bearing and easy to lose:

* **Only the doorbell listener runs the lifespan.** The admin config is built with
  ``lifespan="off"``, so the store, hub, Home Assistant client and MQTT bridge are
  started once, by the listener the process lives for.
* **A bind failure on one listener is not the end of the process.** uvicorn 0.54
  calls ``sys.exit(1)`` from ``Server.startup`` when its bind fails, and on a shared
  loop that ``SystemExit`` would kill the doorbell because the admin port was busy.
  :func:`_serve_one` catches it per listener and reports it: a failed admin
  listener is logged and the doorbell keeps serving, while a failed doorbell
  listener ends the process with a non-zero status.

Shutdown is coordinated here too. uvicorn installs one signal handler per server on
the same loop (last one wins), so SIGTERM reaches exactly one server: when a
listener stops because the process was asked to stop, the ``finally`` sets
``should_exit`` on every other server and gathers them, each bounded by
:data:`GRACEFUL_SHUTDOWN_TIMEOUT_S`.

Owning ticket: HSD-n6wi.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

import uvicorn
from starlette.types import ASGIApp

from .config import ServerConfig, admin_bind

__all__ = ["GRACEFUL_SHUTDOWN_TIMEOUT_S", "build_configs", "serve"]

_LOGGER = logging.getLogger(__name__)

GRACEFUL_SHUTDOWN_TIMEOUT_S: Final = 1
"""Seconds uvicorn waits for connections to drain on shutdown."""

_FAILED_START_STATUS: Final = 1
"""Process status used when a listener's bind failure reports no usable code."""

_PRIMARY_ROLE: Final = "doorbell"
"""Log name of the listener the process lives as long as."""

_ADMIN_ROLE: Final = "admin"
"""Log name of the configuration-only listener."""


class _Server(Protocol):
    """The slice of :class:`uvicorn.Server` this module drives.

    A protocol rather than a concrete type so a test can drive :func:`serve`'s
    coordination with fake servers, without binding a socket.
    """

    should_exit: bool
    """Set to ``True`` to ask the server to stop serving."""

    async def serve(self) -> None:
        """Serve until stopped, raising ``SystemExit`` when the bind fails."""
        ...


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What one listener's ``serve()`` call ended with."""

    status: int
    """Process status this listener contributes; ``0`` after a clean stop."""

    bind_failure: bool
    """Whether it ended by raising ``SystemExit`` instead of stopping cleanly."""


def build_configs(
    config: ServerConfig, app: ASGIApp, *, admin_app: ASGIApp | None = None
) -> list[uvicorn.Config]:
    """Build one :class:`uvicorn.Config` per listener, doorbell first.

    Pure: nothing is bound, nothing is served and no environment is read, so a
    test can inspect exactly what the CLI would start.

    The admin config is appended only when ``config`` enables the admin listener
    **and** an admin application was supplied. It carries ``lifespan="off"`` so the
    lifespan runs once, on the doorbell listener.

    :param config: resolved server configuration naming both binds.
    :param app: the doorbell application, serving the whole app on the loopback
        listener.
    :param admin_app: the admin-only wrapper, or ``None`` when the listener is off.
    :returns: the configs, doorbell first.
    """
    configs = [_uvicorn_config(config, app, host=config.host, port=config.port, lifespan="auto")]
    bind = admin_bind(config)
    if admin_app is not None and bind is not None:
        admin_host, admin_port = bind
        configs.append(
            _uvicorn_config(config, admin_app, host=admin_host, port=admin_port, lifespan="off")
        )
    return configs


def _uvicorn_config(
    config: ServerConfig,
    app: ASGIApp,
    *,
    host: str,
    port: int,
    lifespan: Literal["auto", "off"],
) -> uvicorn.Config:
    """Build the one uvicorn config the two listeners share all settings with.

    :param config: resolved server configuration supplying the log settings.
    :param app: ASGI application this listener serves.
    :param host: bind address of this listener.
    :param port: bind port of this listener.
    :param lifespan: ``"auto"`` for the doorbell listener, ``"off"`` for the admin
        one, which must not start the application's lifespan a second time.
    """
    return uvicorn.Config(
        app,
        host=host,
        port=port,
        # Keep our own logging configuration and handler instead of uvicorn's.
        log_level=config.log_level.lower(),
        log_config=None,
        # Bounded graceful shutdown: the acceptance criteria require a clean exit
        # within 3 seconds of SIGTERM even with websocket clients attached.
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_S,
        lifespan=lifespan,
    )


def serve(configs: Sequence[uvicorn.Config]) -> int:
    """Serve every config until the process is asked to stop.

    The one serving seam the CLI tests patch, so no test ever binds a socket.

    :param configs: configs from :func:`build_configs`, doorbell first; never empty.
    :returns: process exit status: ``0`` after a clean shutdown, non-zero when the
        doorbell listener itself could not start.
    """
    servers: list[_Server] = [uvicorn.Server(config) for config in configs]
    return asyncio.run(_serve_servers(servers))


async def _serve_servers(servers: Sequence[_Server]) -> int:
    """Run every server on this loop and coordinate their shutdown.

    The process ends when the doorbell listener ends. An admin listener that
    failed to bind is logged and simply drops out, so a busy admin port cannot take
    the doorbell down; an admin listener that stopped *cleanly* was the one that
    observed SIGTERM, which is a request to stop the whole process.

    :param servers: servers to run, doorbell first.
    :returns: the doorbell listener's status.
    """
    tasks: list[asyncio.Task[_Outcome]] = [
        asyncio.create_task(
            _serve_one(
                server, role=_PRIMARY_ROLE if index == 0 else _ADMIN_ROLE, primary=index == 0
            )
        )
        for index, server in enumerate(servers)
    ]
    primary = tasks[0]
    try:
        pending: set[asyncio.Task[_Outcome]] = set(tasks)
        while True:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if primary in done or not pending:
                break
            if not all(task.result().bind_failure for task in done):
                # A listener stopped because the process was asked to stop; the
                # other one is stopped by the ``finally`` below.
                break
    finally:
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
    return primary.result().status


async def _serve_one(server: _Server, *, role: str, primary: bool) -> _Outcome:
    """Serve one listener, turning a bind failure into a reported outcome.

    :param server: the server to run until it stops.
    :param role: human name of the listener, used in the one log line.
    :param primary: whether this is the doorbell listener the process lives for.
    """
    try:
        await server.serve()
    except SystemExit as exc:
        status = exc.code if isinstance(exc.code, int) and exc.code else _FAILED_START_STATUS
        if primary:
            _LOGGER.error(
                "the %s listener could not start on its configured address (exit %d); stopping",
                role,
                status,
            )
        else:
            _LOGGER.error(
                "the %s listener could not start on its configured address (exit %d); the "
                "doorbell keeps serving without it",
                role,
                status,
            )
        return _Outcome(status=status, bind_failure=True)
    return _Outcome(status=0, bind_failure=False)
