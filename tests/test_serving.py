"""The serving seam: what the CLI asks uvicorn to run, and how it stops (HSD-n6wi).

Two things are pinned here and nothing else:

* :func:`chime_deck.serving.build_configs` is **pure** — a test can inspect every
  ``uvicorn.Config`` the CLI would run, so the old "assert the kwargs of
  ``uvicorn.run``" checks became assertions on the objects themselves.
* :func:`chime_deck.serving._serve_servers` coordinates two servers: a bind
  failure on the admin listener must not take the doorbell down (uvicorn 0.54
  calls ``sys.exit(1)`` from ``Server.startup``), a bind failure on the doorbell
  listener must end the process non-zero, and a clean stop on either one stops the
  other. The fake servers below never bind a socket; each wait is bounded by
  ``asyncio.wait_for``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from pathlib import Path
from typing import cast

import pytest
import uvicorn
from starlette.types import Receive, Scope, Send

from chime_deck.config import (
    DEFAULT_ADMIN_HOST,
    DEFAULT_ADMIN_PORT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    ServerConfig,
)
from chime_deck.serving import (
    GRACEFUL_SHUTDOWN_TIMEOUT_S,
    _serve_servers,
    build_configs,
    serve,
)

pytestmark = pytest.mark.asyncio

_BOUND_S = 5.0
"""Every await in this module is bounded, so a coordination bug fails fast."""


async def _app(scope: Scope, receive: Receive, send: Send) -> None:
    """Stand in for the application: this module never serves it.

    :param scope: ASGI scope, unused.
    :param receive: ASGI receive callable, unused.
    :param send: ASGI send callable, unused.
    """
    del scope, receive, send


def _config(
    tmp_path: Path,
    *,
    log_level: str = "INFO",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    admin_host: str = DEFAULT_ADMIN_HOST,
    admin_port: int = DEFAULT_ADMIN_PORT,
    admin_enabled: bool = True,
) -> ServerConfig:
    """Build a resolved server config for the tests here.

    :param tmp_path: pytest temporary directory.
    :param log_level: log level to resolve.
    :param host: doorbell bind address.
    :param port: doorbell bind port.
    :param admin_host: admin listener bind address.
    :param admin_port: admin listener bind port.
    :param admin_enabled: whether the admin listener is on.
    """
    return ServerConfig(
        data_dir=tmp_path,
        seed_demo_sounds=False,
        log_level=log_level,
        host=host,
        port=port,
        admin_host=admin_host,
        admin_port=admin_port,
        admin_enabled=admin_enabled,
    )


async def test_build_configs_with_the_admin_listener_disabled(tmp_path: Path) -> None:
    """One listener: the doorbell, with the app's own lifespan.

    :param tmp_path: pytest temporary directory.
    """
    config = _config(tmp_path, admin_enabled=False)

    configs = build_configs(config, _app)

    assert len(configs) == 1
    doorbell = configs[0]
    assert doorbell.app is _app
    assert doorbell.host == DEFAULT_HOST
    assert doorbell.port == DEFAULT_PORT
    assert doorbell.lifespan == "auto"
    assert doorbell.log_config is None
    assert doorbell.timeout_graceful_shutdown == GRACEFUL_SHUTDOWN_TIMEOUT_S == 1


async def test_build_configs_with_both_listeners(tmp_path: Path) -> None:
    """Two listeners, doorbell first, and only the doorbell runs the lifespan.

    :param tmp_path: pytest temporary directory.
    """
    config = _config(tmp_path, log_level="DEBUG")

    configs = build_configs(config, _app, admin_app=_app)

    assert len(configs) == 2
    doorbell, admin = configs
    assert doorbell.host == DEFAULT_HOST
    assert doorbell.port == DEFAULT_PORT
    assert doorbell.lifespan == "auto"
    assert admin.host == DEFAULT_ADMIN_HOST
    assert admin.port == DEFAULT_ADMIN_PORT
    assert admin.lifespan == "off"
    for built in configs:
        assert built.log_config is None
        assert built.timeout_graceful_shutdown == GRACEFUL_SHUTDOWN_TIMEOUT_S
        assert built.log_level == "debug"


async def test_build_configs_uses_the_configured_binds(tmp_path: Path) -> None:
    """The two binds come from the resolved configuration, not from constants.

    :param tmp_path: pytest temporary directory.
    """
    config = _config(
        tmp_path,
        host="127.0.0.1",
        port=9100,
        admin_host="127.0.0.1",
        admin_port=9101,
    )

    doorbell, admin = build_configs(config, _app, admin_app=_app)

    assert (doorbell.host, doorbell.port) == ("127.0.0.1", 9100)
    assert (admin.host, admin.port) == ("127.0.0.1", 9101)


async def test_build_configs_without_an_admin_app_builds_one_listener(tmp_path: Path) -> None:
    """An enabled listener with no application is not built: nothing to serve.

    :param tmp_path: pytest temporary directory.
    """
    config = _config(tmp_path, admin_enabled=True)

    assert len(build_configs(config, _app)) == 1


class _FakeServer:
    """A :class:`uvicorn.Server` stand-in that never binds anything."""

    def __init__(self, *, fail: bool = False, run_for: float | None = None) -> None:
        """Configure how this fake ends.

        :param fail: raise ``SystemExit(1)`` immediately, like a failed bind.
        :param run_for: stop on its own after this many seconds; ``None`` waits
            until ``should_exit`` is set.
        """
        self.should_exit = False
        self._fail = fail
        self._run_for = run_for
        self.exit_reason: str | None = None

    async def serve(self) -> None:
        """Serve until one of the configured endings happens."""
        if self._fail:
            self.exit_reason = "bind"
            raise SystemExit(1)
        loop = asyncio.get_running_loop()
        deadline = None if self._run_for is None else loop.time() + self._run_for
        while not self.should_exit:
            if deadline is not None and loop.time() >= deadline:
                self.exit_reason = "deadline"
                return
            await asyncio.sleep(0.005)
        self.exit_reason = "asked"


async def test_a_failed_admin_bind_does_not_take_the_doorbell_down() -> None:
    """A busy admin port is logged and the doorbell keeps serving.

    The doorbell fake ends on its own deadline: if the admin failure had stopped
    it, ``exit_reason`` would be ``"asked"`` instead.
    """
    primary = _FakeServer(run_for=0.05)
    admin = _FakeServer(fail=True)

    status = await asyncio.wait_for(_serve_servers([primary, admin]), timeout=_BOUND_S)

    assert status == 0
    assert primary.exit_reason == "deadline"
    assert admin.exit_reason == "bind"
    assert primary.should_exit is True


async def test_a_failed_doorbell_bind_ends_the_process_non_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The doorbell's own bind failure stops the other listener and returns 1.

    :param caplog: pytest log capture fixture.
    """
    primary = _FakeServer(fail=True)
    admin = _FakeServer()

    with caplog.at_level(logging.ERROR, logger="chime_deck.serving"):
        status = await asyncio.wait_for(_serve_servers([primary, admin]), timeout=_BOUND_S)

    assert status == 1
    assert admin.exit_reason == "asked"
    assert admin.should_exit is True
    messages = [record.getMessage() for record in caplog.records]
    assert any("doorbell listener could not start" in message for message in messages)
    assert not any("admin listener could not start" in message for message in messages)


async def test_a_clean_stop_on_one_listener_stops_the_other(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SIGTERM reaches one server; the other is stopped here, not left running.

    uvicorn installs one handler per server on one loop, so only one of them sees
    the signal. That one returns cleanly; the coordination must stop the rest and
    report a clean status.

    :param caplog: pytest log capture fixture.
    """
    primary = _FakeServer()
    admin = _FakeServer(run_for=0.05)

    with caplog.at_level(logging.ERROR, logger="chime_deck.serving"):
        status = await asyncio.wait_for(_serve_servers([primary, admin]), timeout=_BOUND_S)

    assert status == 0
    assert admin.exit_reason == "deadline"
    assert primary.exit_reason == "asked"
    assert primary.should_exit is True
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


async def test_a_failed_admin_bind_is_reported_with_the_admin_role(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The failure line names the admin listener and says the doorbell continues.

    :param caplog: pytest log capture fixture.
    """
    primary = _FakeServer(run_for=0.02)
    admin = _FakeServer(fail=True)

    with caplog.at_level(logging.ERROR, logger="chime_deck.serving"):
        await asyncio.wait_for(_serve_servers([primary, admin]), timeout=_BOUND_S)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "admin listener could not start" in message and "keeps serving" in message
        for message in messages
    )


async def test_serve_builds_one_uvicorn_server_per_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve`` builds a server per config and runs the coordinator on a new loop.

    Nothing is driven here: ``asyncio.run`` is replaced by a recorder that closes
    the coroutine, so this pins the wiring without binding a socket. The real
    coordination is exercised by the fake-server tests above.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    built: list[uvicorn.Config] = []

    class _Recorder:
        """Capture the config it was built from."""

        def __init__(self, config: uvicorn.Config) -> None:
            """Record one server for *config*.

            :param config: uvicorn configuration handed to the server.
            """
            self.config = config
            self.should_exit = False
            built.append(config)

        async def serve(self) -> None:
            """Never called: the loop is recorded, not run."""

    monkeypatch.setattr("chime_deck.serving.uvicorn.Server", _Recorder)
    driven: list[object] = []

    def _run(coro: object) -> int:
        """Record the coroutine and close it instead of running a loop.

        :param coro: coroutine ``serve`` built.
        """
        driven.append(coro)
        cast("Coroutine[object, object, int]", coro).close()
        return 0

    monkeypatch.setattr("chime_deck.serving.asyncio.run", _run)
    configs = build_configs(_config(tmp_path), _app, admin_app=_app)

    assert serve(configs) == 0
    assert built == configs
    assert len(driven) == 1
