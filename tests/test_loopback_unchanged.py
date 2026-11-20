"""The loopback listener and the kiosk are unchanged by the admin listener (trap 8).

Listener 1 is the identical :func:`chime_deck.app.create_app` application with the
identical middleware; the admin policy lives in a wrapper around it that
``create_app`` never installs. The pins here are the ones that would break if
someone "simplified" the two listeners into one:

* with a configuration password **set**, the loopback dashboard and API still
  answer 200 with no credential at all — the kiosk must never show a prompt;
* the ASGI-transport scope, which every existing test uses, is **loopback by
  construction**, so no existing test needed a credential added;
* the same running application behind the admin wrapper still redirects ``GET /``,
  which is the positive control that the two listeners really differ.

Owning ticket: HSD-n6wi.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state
from starlette.types import ASGIApp

from chime_deck.admin import PASSWORD_SETTING_KEY, create_admin_app, hash_password
from chime_deck.app import AppState, create_app
from chime_deck.config import ServerConfig

pytestmark = pytest.mark.asyncio

PASSWORD = "correct horse battery staple"
TOKEN = "shared-token-0123456789abcdef"

_INDEX_HTML = '<!doctype html><html><body><div id="app"></div></body></html>'


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Restore the root logger and the registered secrets after every test."""
    with preserve_log_state():
        yield


@dataclass(slots=True)
class _Running:
    """The handles one running application needs here."""

    loopback: httpx.AsyncClient
    admin: httpx.AsyncClient
    state: AppState


def _build_app(tmp_path: Path) -> tuple[FastAPI, AppState, Path]:
    """Build an app serving a minimal built UI from a temporary directory.

    :param tmp_path: pytest temporary directory.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    config = ServerConfig(data_dir=tmp_path / "data", static_dir=dist, seed_demo_sounds=False)
    app = create_app(config)
    return app, cast(AppState, app.state.doorbell), dist


@asynccontextmanager
async def _client(app: ASGIApp) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client bound to *app*.

    :param app: ASGI application under test.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@asynccontextmanager
async def _running(tmp_path: Path, *, with_password: bool = True) -> AsyncIterator[_Running]:
    """Run the lifespan and yield clients for both listeners.

    :param tmp_path: pytest temporary directory.
    :param with_password: whether to store a configuration password first.
    """
    app, state, _dist = _build_app(tmp_path)
    admin_app = create_admin_app(app, gate=state.admin)
    async with (
        app.router.lifespan_context(app),
        _client(app) as loopback,
        _client(admin_app) as admin,
    ):
        if with_password:
            async with state.store.transaction():
                await state.store.set_setting(PASSWORD_SETTING_KEY, hash_password(PASSWORD))
            await state.admin.refresh(state.store)
        yield _Running(loopback=loopback, admin=admin, state=state)


async def test_the_loopback_dashboard_and_api_stay_open_with_a_password_set(
    tmp_path: Path,
) -> None:
    """A stored password gates the admin listener only, never the kiosk.

    :param tmp_path: pytest temporary directory.
    """
    async with _running(tmp_path) as run:
        assert run.state.admin.required() is True

        dashboard = await run.loopback.get("/")
        settings = await run.loopback.get("/api/settings")
        entities = await run.loopback.get("/api/entities")
        health = await run.loopback.get("/api/health")

    assert dashboard.status_code == 200
    assert 'id="app"' in dashboard.text
    assert settings.status_code == 200
    assert entities.status_code == 200
    assert health.status_code == 200


async def test_the_same_app_behind_the_admin_wrapper_redirects_the_dashboard(
    tmp_path: Path,
) -> None:
    """The positive control: one app, two listeners, one different decision.

    :param tmp_path: pytest temporary directory.
    """
    async with _running(tmp_path) as run:
        loopback_root = await run.loopback.get("/")
        admin_root = await run.admin.get("/")

    assert loopback_root.status_code == 200
    assert 'id="app"' in loopback_root.text
    assert admin_root.status_code == 302
    assert admin_root.headers["location"] == "/settings"


async def test_an_asgi_transport_scope_is_loopback_by_default(tmp_path: Path) -> None:
    """A request from any test transport is the trusted listener, by construction.

    This is why the whole existing suite keeps its behaviour: no stamp means
    :data:`chime_deck.listener.LOOPBACK`, and only the admin wrapper stamps.

    :param tmp_path: pytest temporary directory.
    """
    async with _running(tmp_path, with_password=False) as run:
        loopback = await run.loopback.get("/api/auth/status")
        admin = await run.admin.get("/api/auth/status")

    assert cast("dict[str, object]", loopback.json())["listener"] == "loopback"
    assert cast("dict[str, object]", admin.json())["listener"] == "admin"


async def test_the_loopback_token_gate_is_unchanged(tmp_path: Path) -> None:
    """With ``server.token`` set, the loopback gate behaves exactly as before.

    :param tmp_path: pytest temporary directory.
    """
    async with _running(tmp_path, with_password=False) as run:
        await run.loopback.patch("/api/settings", json={"server.token": TOKEN})

        refused = await run.loopback.get("/api/settings")
        allowed = await run.loopback.get(
            "/api/settings", headers={"Authorization": f"Bearer {TOKEN}"}
        )

    assert refused.status_code == 401
    assert refused.headers["www-authenticate"] == "Bearer"
    assert allowed.status_code == 200
