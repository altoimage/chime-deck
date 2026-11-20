"""The admin listener's allowlist: one table, deny by default (HSD-n6wi).

Everything here drives :func:`chime_deck.admin.create_admin_app` in process, over
``httpx.ASGITransport`` for HTTP and a hand-built ASGI websocket session for
``/ws``, so nothing opens a socket. The assertions are about the *policy* the
listener applies before the doorbell application is reached at all, which is why
the shell, the static assets and the refusals can be checked without starting the
application lifespan (and therefore without a store, a hub or a broker).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chime_deck.admin import AdminDecision, AdminGate, admin_decision, create_admin_app
from chime_deck.api import ErrorEnvelope
from chime_deck.app import create_app
from chime_deck.config import ServerConfig
from chime_deck.listener import ADMIN, LOOPBACK, listener_of

pytestmark = pytest.mark.asyncio

_INDEX_HTML = (
    '<!doctype html><html lang="en"><body><div id="app"></div>'
    '<script src="/assets/index-abc.js"></script></body></html>'
)
"""A stand-in for ``web/dist/index.html``: the one marker ``id="app"`` matters."""

_SCRIPT = "console.log('chime deck');\n"
"""A stand-in for ``/assets/index-*.js``."""

_PATH_PARAMETER = re.compile(r"\{[^}]*\}")
"""Matches one path placeholder such as ``{button_id}`` or ``{full_path:path}``."""

_REFUSED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        # The live entity-status stream: never on the LAN.
        ("GET", "/ws"),
        # D29 tile selection is actuation, and the dashboard is not served here.
        ("POST", "/api/tiles/1/selection"),
        # The catch-all UI route, concretised to one client route.
        ("GET", "/1"),
    }
)
"""The only routes on the admin listener that are refused outright."""


def _dist(tmp_path: Path) -> Path:
    """Write a minimal built UI and return its directory.

    :param tmp_path: pytest temporary directory.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(_INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "index-abc.js").write_text(_SCRIPT, encoding="utf-8")
    return dist


def _build_apps(tmp_path: Path, *, gate: AdminGate | None = None) -> tuple[ASGIApp, AdminGate]:
    """Build the admin wrapper over a real application.

    :param tmp_path: pytest temporary directory.
    :param gate: credential state to hand the wrapper; ``None`` builds an open one.
    :returns: the admin application and the gate it reads.
    """
    config = ServerConfig(
        data_dir=tmp_path / "data",
        static_dir=_dist(tmp_path),
        seed_demo_sounds=False,
    )
    app = create_app(config)
    resolved_gate = gate if gate is not None else AdminGate()
    return create_admin_app(app, gate=resolved_gate), resolved_gate


@asynccontextmanager
async def _client(app: ASGIApp) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client bound to *app* without running the application lifespan.

    :param app: ASGI application under test.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _websocket_scope(path: str) -> Scope:
    """Build the ASGI scope of one websocket handshake.

    :param path: request path of the handshake.
    """
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
        "root_path": "",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
    }


async def _websocket_session(app: ASGIApp, path: str) -> list[Message]:
    """Drive one handshake through *app* and return everything it sent.

    :param app: application under test.
    :param path: path of the handshake.
    """
    sent: list[Message] = []
    pending: list[Message] = [{"type": "websocket.connect"}]

    async def receive() -> Message:
        """Answer the handshake, then report the client going away."""
        return pending.pop(0) if pending else {"type": "websocket.disconnect", "code": 1000}

    async def send(message: Message) -> None:
        """Record one message the application emitted."""
        sent.append(message)

    await asyncio.wait_for(app(_websocket_scope(path), receive, send), timeout=5.0)
    return sent


def _error_code(response: httpx.Response) -> str:
    """Return the machine-readable code of a failed response.

    :param response: response carrying the standard envelope.
    """
    envelope = ErrorEnvelope.model_validate(cast("dict[str, object]", response.json()))
    return envelope.error.code.value


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        # The ticket's whole point: an admin URL never renders the dashboard.
        ("GET", "/", AdminDecision.REDIRECT_SETTINGS),
        # HEAD is decided like GET: no body, no extra surface.
        ("HEAD", "/", AdminDecision.REDIRECT_SETTINGS),
        ("POST", "/", AdminDecision.REFUSE),
        # The settings shell and the first-run wizard, with their client routes.
        ("GET", "/settings", AdminDecision.OPEN),
        ("HEAD", "/settings", AdminDecision.OPEN),
        ("GET", "/settings/audio", AdminDecision.OPEN),
        ("GET", "/setup", AdminDecision.OPEN),
        ("GET", "/setup/", AdminDecision.OPEN),
        ("POST", "/settings", AdminDecision.REFUSE),
        # The two files web/dist/index.html references, and their directory.
        ("GET", "/assets/index-abc.js", AdminDecision.OPEN),
        ("HEAD", "/assets/index-abc.js", AdminDecision.OPEN),
        ("GET", "/assets/index-abc.css", AdminDecision.OPEN),
        # Liveness and the login prompt's own booleans.
        ("GET", "/api/health", AdminDecision.OPEN),
        ("HEAD", "/api/health", AdminDecision.OPEN),
        ("GET", "/api/auth/status", AdminDecision.OPEN),
        ("POST", "/api/auth/login", AdminDecision.OPEN),
        # Every configuration credential operation needs the credential.
        ("POST", "/api/auth/password", AdminDecision.AUTH),
        ("POST", "/api/auth/password/clear", AdminDecision.AUTH),
        # The settings shell's and the wizard's whole API surface.
        ("GET", "/api/settings", AdminDecision.AUTH),
        ("HEAD", "/api/settings", AdminDecision.AUTH),
        ("PATCH", "/api/settings", AdminDecision.AUTH),
        ("POST", "/api/settings/test/ha", AdminDecision.AUTH),
        ("GET", "/api/audio/devices", AdminDecision.AUTH),
        ("POST", "/api/mqtt/republish", AdminDecision.AUTH),
        ("POST", "/api/buttons", AdminDecision.AUTH),
        ("PATCH", "/api/entities/e1", AdminDecision.AUTH),
        ("GET", "/api/sounds", AdminDecision.AUTH),
        ("GET", "/api/tts/engines", AdminDecision.AUTH),
        ("GET", "/api/ha/entities", AdminDecision.AUTH),
        ("GET", "/api/icons/search", AdminDecision.AUTH),
        ("GET", "/api/icon/power", AdminDecision.AUTH),
        ("GET", "/api/history", AdminDecision.AUTH),
        ("GET", "/api/config/export", AdminDecision.AUTH),
        ("GET", "/api/about", AdminDecision.AUTH),
        ("GET", "/custom-icons/a.svg", AdminDecision.AUTH),
        # Never on the LAN.
        ("GET", "/ws", AdminDecision.REFUSE),
        ("POST", "/api/tiles/1/selection", AdminDecision.REFUSE),
        ("GET", "/api/tiles/1/selection", AdminDecision.REFUSE),
        # Deny by default: the dashboard shell, its aliases and everything else.
        ("GET", "/index.html", AdminDecision.REFUSE),
        ("GET", "/foo", AdminDecision.REFUSE),
        ("GET", "/gallery", AdminDecision.REFUSE),
        ("GET", "/dashboard", AdminDecision.REFUSE),
        ("GET", "/api/not-a-route", AdminDecision.REFUSE),
        ("GET", "/openapi.json", AdminDecision.REFUSE),
    ],
)
async def test_the_allowlist_table(method: str, path: str, expected: AdminDecision) -> None:
    """Every row of the plan's table, including the defaults.

    :param method: request method.
    :param path: request path.
    :param expected: the decision the listener must take.
    """
    assert admin_decision(method, path) is expected


async def test_the_dashboard_url_redirects_and_carries_no_shell(tmp_path: Path) -> None:
    """``GET /`` on an admin URL is a 302 to the settings shell, with no HTML body.

    The positive control is the next test: the same marker *is* served at
    ``/settings``. Without that, this one would pass on a broken static setup
    that serves nothing at all.

    :param tmp_path: pytest temporary directory.
    """
    admin_app, _gate = _build_apps(tmp_path)

    async with _client(admin_app) as client:
        response = await client.get("/")

    assert response.status_code == 302
    assert response.headers["location"] == "/settings"
    assert 'id="app"' not in response.text


async def test_a_head_request_is_not_refused_where_the_get_is_served(tmp_path: Path) -> None:
    """One method family, one decision: a HEAD is classified exactly like a GET.

    A health probe or a cache revalidation sends ``HEAD``; refusing it while the
    matching ``GET`` is served would answer the policy's 404 where the loopback
    listener answers whatever the route does. The redirect on ``HEAD /`` is the
    one that must stay a redirect, with no body of its own.

    :param tmp_path: pytest temporary directory.
    """
    config = ServerConfig(
        data_dir=tmp_path / "data",
        static_dir=_dist(tmp_path),
        seed_demo_sounds=False,
    )
    app = create_app(config)
    admin_app = create_admin_app(app, gate=AdminGate())

    async with _client(app) as loopback, _client(admin_app) as admin:
        redirect = await admin.head("/")
        served = ["/settings", "/assets/index-abc.js"]
        loopback_statuses = [(await loopback.head(path)).status_code for path in served]
        admin_statuses = [(await admin.head(path)).status_code for path in served]

    assert redirect.status_code == 302
    assert redirect.headers["location"] == "/settings"
    assert redirect.text == ""
    # The route layer, not the policy, decides the status on both listeners.
    assert admin_statuses == loopback_statuses
    assert 404 not in admin_statuses


@pytest.mark.parametrize("path", ["/settings", "/settings/audio", "/setup", "/setup/"])
async def test_the_settings_shell_is_served_with_the_app_mount(tmp_path: Path, path: str) -> None:
    """The settings shell and the wizard answer 200 HTML and mount the UI.

    :param tmp_path: pytest temporary directory.
    :param path: client route under test.
    """
    admin_app, _gate = _build_apps(tmp_path)

    async with _client(admin_app) as client:
        response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="app"' in response.text


async def test_a_real_asset_is_served(tmp_path: Path) -> None:
    """The JS bundle ``index.html`` references is served.

    :param tmp_path: pytest temporary directory.
    """
    admin_app, _gate = _build_apps(tmp_path)

    async with _client(admin_app) as client:
        response = await client.get("/assets/index-abc.js")

    assert response.status_code == 200
    assert response.text == _SCRIPT


@pytest.mark.parametrize(
    "path",
    [
        "/index.html",
        "/foo",
        "/gallery",
        "/dashboard",
        "/settings/../../etc/passwd",
        # Percent-encoded, so httpx does not collapse it before it leaves the
        # client: the app's own containment must refuse it, not the policy table.
        "/settings/%2e%2e/%2e%2e/etc/passwd",
    ],
)
async def test_a_path_the_listener_does_not_serve_is_a_404_envelope(
    tmp_path: Path, path: str
) -> None:
    """Everything outside the table is refused with the standard 404 envelope.

    Both traversal spellings must land on 404 and never on the file's contents:
    the plain form and the encoded form are refused by different layers (the
    policy and the static route's own containment), and both are deny by default.

    :param tmp_path: pytest temporary directory.
    :param path: path under test.
    """
    admin_app, _gate = _build_apps(tmp_path)

    async with _client(admin_app) as client:
        response = await client.get(path)

    assert response.status_code == 404
    assert _error_code(response) == "not_found"
    assert "root:" not in response.text
    assert 'id="app"' not in response.text


async def test_the_live_stream_is_refused_before_accept(tmp_path: Path) -> None:
    """``/ws`` is closed with 1008 before ``accept()``, so the route never runs.

    :param tmp_path: pytest temporary directory.
    """
    admin_app, _gate = _build_apps(tmp_path)

    session = await _websocket_session(admin_app, "/ws")

    assert [message["type"] for message in session] == ["websocket.close"]
    assert session[0]["code"] == 1008


async def test_the_tile_selection_route_is_refused(tmp_path: Path) -> None:
    """D29 tile selection is actuation and stays on the dashboard's listener.

    :param tmp_path: pytest temporary directory.
    """
    admin_app, _gate = _build_apps(tmp_path)

    async with _client(admin_app) as client:
        response = await client.post("/api/tiles/1/selection", json={"selected": True})

    assert response.status_code == 404
    assert _error_code(response) == "not_found"


class _StampRecorder:
    """An ASGI app that records the listener stamp it was handed."""

    def __init__(self) -> None:
        """Create a recorder with no observations."""
        self.stamps: list[str] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Record the stamp, then answer a small JSON body.

        :param scope: ASGI scope handed to the wrapped application.
        :param receive: ASGI receive callable, unused.
        :param send: ASGI send callable.
        """
        self.stamps.append(listener_of(scope))
        response = JSONResponse({"ok": True})
        await response(scope, receive, send)


async def test_the_wrapper_stamps_the_scope_as_admin(tmp_path: Path) -> None:
    """The wrapper stamps ``chime_deck.listener`` before it delegates.

    This is the whole "which socket did I arrive on?" mechanism: an absent stamp
    is [:data:`chime_deck.listener.LOOPBACK`], so the admin listener must set it
    itself and nothing a client sends can change it.

    :param tmp_path: pytest temporary directory.
    """
    recorder = _StampRecorder()
    admin_app = create_admin_app(recorder, gate=AdminGate())

    async with _client(admin_app) as client:
        response = await client.get("/api/settings")

    assert response.status_code == 200
    assert recorder.stamps == [ADMIN]
    assert listener_of({"type": "http"}) == LOOPBACK


def _iter_api_routes(routes: Sequence[object]) -> Iterator[APIRoute | APIWebSocketRoute]:
    """Walk every API and websocket route of an application, expanding routers.

    FastAPI 0.162 keeps an included router as a lazy wrapper, so the effective
    routes are asked for through its own ``effective_candidates`` API rather than
    reimplementing prefix handling here.

    :param routes: ``app.router.routes``, or a nested candidate list.
    """
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            for candidate in candidates():
                inner = getattr(candidate, "original_route", None)
                if isinstance(inner, (APIRoute, APIWebSocketRoute)):
                    yield inner
                else:
                    yield from _iter_api_routes([candidate])
            continue
        if isinstance(route, (APIRoute, APIWebSocketRoute)):
            yield route


async def test_every_route_is_allowed_or_explicitly_refused(tmp_path: Path) -> None:
    """A route added later cannot silently appear on the LAN, or vanish from it.

    The property, not a list: every ``APIRoute`` and websocket route the app
    registers is concretised and classified, and the *only* refused API paths are
    the tile selection and the live stream. A new router under ``/api`` therefore
    fails this test until someone decides what the admin listener does with it.

    :param tmp_path: pytest temporary directory.
    """
    config = ServerConfig(data_dir=tmp_path / "data", static_dir=tmp_path / "no-dist")
    app = create_app(config)

    refused: set[tuple[str, str]] = set()
    for route in _iter_api_routes(app.router.routes):
        path = _PATH_PARAMETER.sub("1", route.path)
        methods = getattr(route, "methods", None)
        pairs = [(method, path) for method in sorted(methods or {"GET"})]
        for method, concrete in pairs:
            decision = admin_decision(method, concrete)
            if (method, concrete) in _REFUSED_ROUTES:
                assert decision is AdminDecision.REFUSE, (method, concrete)
                refused.add((method, concrete))
                continue
            served_prefixes = ("/api/", "/custom-icons/")
            if concrete == "/ws" or concrete.startswith(served_prefixes):
                assert decision in {AdminDecision.OPEN, AdminDecision.AUTH}, (
                    method,
                    concrete,
                )
            else:
                # Everything that is not an API, a custom icon or the socket is
                # denied by default, the catch-all UI route included.
                assert decision is AdminDecision.REFUSE, (method, concrete)

    api_refused = {pair for pair in refused if pair[1].startswith("/api/") or pair[1] == "/ws"}
    assert api_refused == {("GET", "/ws"), ("POST", "/api/tiles/1/selection")}
    assert refused == _REFUSED_ROUTES
