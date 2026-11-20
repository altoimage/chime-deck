"""Optional shared-token authentication: the gate, the cookie and the login route.

Every test drives the real application in process: ``httpx.ASGITransport`` for
HTTP and a hand-built ASGI websocket session for ``/ws`` (``starlette.testclient``
is unusable here — it warns about ``httpx`` under ``filterwarnings = ["error"]``),
so nothing opens a socket or leaves a task behind. The gate is exercised through
its public surface — status codes, envelopes, headers and log records — never by
reaching into the middleware.

The two "absence" assertions are positive controls where they can be: the 401 body
is compared with the module's fixed sentence (so a leak cannot pass), and the
never-logged test proves the scrubber is live by requiring ``[REDACTED]`` in the
same captured text.

Owning ticket: HSD-0jke.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator, MutableMapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from log_isolation import preserve_log_state
from starlette.types import ASGIApp, Message, Scope

from chime_deck.api import ErrorCode, ErrorEnvelope
from chime_deck.app import AppState, create_app
from chime_deck.auth import (
    AUTH_REQUIRED_MESSAGE,
    COOKIE_MAX_AGE_S,
    COOKIE_NAME,
    SETTING_KEY,
    cookie_is_valid,
    is_exempt,
    issue_cookie,
    matches,
    session_cookie_header,
    signing_key,
)
from chime_deck.config import REDACTED, ServerConfig, install_log_scrubber

pytestmark = pytest.mark.asyncio

TOKEN = "shared-token-0123456789abcdef"
OTHER_TOKEN = "other-token-fedcba9876543210"
HA_TOKEN = "ha-long-lived-token-0123456789abcdef"

_LOGGER = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Keep the root logger and the registered secrets isolated per test."""
    with preserve_log_state():
        yield


def _build_app(tmp_path: Path) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory.
    """
    config = ServerConfig(
        data_dir=tmp_path,
        static_dir=tmp_path / "no-dist",
        seed_demo_sounds=False,
    )
    app = create_app(config)
    return app, cast("AppState", app.state.doorbell)


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield a client bound to it.

    :param app: application under test.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _bearer(token: str) -> dict[str, str]:
    """Return the ``Authorization`` header carrying *token*.

    :param token: credential to send.
    """
    return {"Authorization": f"Bearer {token}"}


def _cookie_header(value: str) -> dict[str, str]:
    """Return the ``Cookie`` header carrying a session cookie.

    :param value: cookie value from :func:`chime_deck.auth.issue_cookie`.
    """
    return {"Cookie": f"{COOKIE_NAME}={value}"}


def _websocket_scope(*, headers: Sequence[tuple[bytes, bytes]] = ()) -> Scope:
    """Build the ASGI scope of one ``/ws`` handshake.

    :param headers: extra raw header pairs, e.g. a bearer or a cookie.
    """
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
        "root_path": "",
        "path": "/ws",
        "raw_path": b"/ws",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), *headers],
    }


async def _websocket_session(
    app: ASGIApp, *, headers: Sequence[tuple[bytes, bytes]] = ()
) -> list[Message]:
    """Drive one ``/ws`` handshake through *app* and return what it sent.

    The receive callable answers ``websocket.connect`` once and then a clean
    disconnect, so an accepted connection sends its snapshot and tears itself down
    instead of leaving a task behind. The whole session is bounded, so a gate that
    hung would fail the test rather than hang the suite.

    :param app: application under test.
    :param headers: extra raw header pairs for the handshake.
    """
    sent: list[Message] = []
    pending: list[Message] = [{"type": "websocket.connect"}]

    async def receive() -> Message:
        """Answer the handshake, then report the client going away."""
        return pending.pop(0) if pending else {"type": "websocket.disconnect", "code": 1000}

    async def send(message: Message) -> None:
        """Record one message the app emitted."""
        sent.append(message)

    await asyncio.wait_for(app(_websocket_scope(headers=headers), receive, send), timeout=5.0)
    return sent


def _error_body(response: httpx.Response) -> dict[str, object]:
    """Return the decoded ``error`` object of a failed response.

    :param response: response whose body is the standard envelope.
    """
    envelope = ErrorEnvelope.model_validate(cast("dict[str, object]", response.json()))
    return {"code": envelope.error.code, "message": envelope.error.message}


def _assert_accepted_with_snapshot(session: list[MutableMapping[str, Any]]) -> None:
    """Assert the handshake was accepted and the first payload is a snapshot.

    Deliberately NOT an exact frame list. The hub is a push channel: a settings
    write that precedes the connect can be delivered as a ``config_changed``
    frame shortly afterwards, and whether that lands inside the recorded session
    depends on host speed. On the aarch64 board the session reliably contains
    accept, snapshot, config_changed; on x86_64 it usually contains two frames.
    Asserting the exact sequence made the tests a timing coin-flip (see the
    matching fix for the cancellation-suppression tests).

    What matters, and what this still proves: the socket was accepted, and the
    FIRST payload is the snapshot. A duplicated snapshot would fail here.

    :param session: the recorded handshake messages, in arrival order.
    """
    assert session, "the session recorded no frames"
    assert session[0]["type"] == "websocket.accept", session[0]
    sends = [message for message in session if message["type"] == "websocket.send"]
    assert sends, "an accepted socket must send a snapshot"
    snapshot = cast("dict[str, object]", json.loads(cast("str", sends[0]["text"])))
    assert snapshot["type"] == "snapshot"


async def test_no_token_set_serves_everything(tmp_path: Path) -> None:
    """With ``server.token`` empty, every surface answers exactly as it did before.

    A gated read, a gated write, a user-content path and the websocket all work
    with no credential at all: the default install is untouched.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        assert state.auth.required() is False
        read = await client.get("/api/settings")
        written = await client.patch("/api/settings", json={"audio.volume": 40})
        icon = await client.get("/custom-icons/not-here.svg")
        session = await _websocket_session(app)

    assert read.status_code == 200
    assert written.status_code == 200
    # The file does not exist, so the icons route answers its own 404: what
    # matters is that the gate did not answer 401 first.
    assert icon.status_code != 401
    _assert_accepted_with_snapshot(session)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/settings"),
        ("PATCH", "/api/settings"),
        ("GET", "/api/entities"),
        ("GET", "/api/config/export?include_secrets=true"),
        ("GET", "/api/about/log"),
        ("GET", "/custom-icons/x.svg"),
    ],
)
async def test_gated_paths_answer_401(tmp_path: Path, method: str, path: str) -> None:
    """Every state-bearing path answers the standard 401 envelope without a credential.

    The list spans a read, a write, entity data, the secret-bearing export, the
    log excerpt and user-supplied icon content, so a route added outside the gate
    would fail here rather than in production.

    :param tmp_path: pytest temporary directory.
    :param method: HTTP method to use.
    :param path: path under test.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        response = await client.request(method, path)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    error = _error_body(response)
    assert error["code"] is ErrorCode.UNAUTHORIZED
    assert error["message"] == AUTH_REQUIRED_MESSAGE


async def test_login_is_exempt_and_health_is_exempt(tmp_path: Path) -> None:
    """``POST /api/auth/login`` and ``GET /api/health`` answer without a credential.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        # Before any login: the login below sets a cookie on this client, which
        # would otherwise unlock the gated read for the wrong reason.
        gated = await client.get("/api/settings")
        health = await client.get("/api/health")
        login = await client.post("/api/auth/login", json={"token": TOKEN})

    assert health.status_code == 200
    assert login.status_code == 200
    assert gated.status_code == 401


@pytest.mark.parametrize("headers", [{}, _bearer(OTHER_TOKEN)])
async def test_the_401_body_never_leaks_the_token(tmp_path: Path, headers: dict[str, str]) -> None:
    """The 401 body is the module's fixed sentence, whatever was presented.

    A missing credential and a wrong one answer byte-identically, so the route is
    not an oracle for "close" guesses, and neither the configured token nor the
    presented value appears anywhere in the response.

    :param tmp_path: pytest temporary directory.
    :param headers: credential to present, empty for none.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        response = await client.get("/api/settings", headers=headers)

    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthorized", "message": AUTH_REQUIRED_MESSAGE, "detail": {}}
    }
    assert TOKEN not in response.text
    assert OTHER_TOKEN not in response.text
    assert str(len(TOKEN)) not in response.text


async def test_a_bearer_header_unlocks_the_api(tmp_path: Path) -> None:
    """A matching ``Authorization: Bearer`` header is enough for every gated route.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        response = await client.get("/api/settings", headers=_bearer(TOKEN))

    assert response.status_code == 200


async def test_a_wrong_bearer_is_refused(tmp_path: Path) -> None:
    """A token that is not the configured one is the same 401 as no token.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        response = await client.get("/api/settings", headers=_bearer(OTHER_TOKEN))

    assert response.status_code == 401
    assert _error_body(response)["code"] is ErrorCode.UNAUTHORIZED


@pytest.mark.parametrize("body", [{}, {"token": OTHER_TOKEN}, {"token": ""}, {"token": 5}])
async def test_a_failed_login_is_the_same_401_envelope(
    tmp_path: Path, body: dict[str, object]
) -> None:
    """A rejected login answers exactly what the middleware answers, and echoes nothing.

    A missing credential is not a second error shape: the same fixed sentence
    travels, so neither the submitted value nor its type leaks.

    :param tmp_path: pytest temporary directory.
    :param body: login body to send.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        response = await client.post("/api/auth/login", json=body)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": {"code": "unauthorized", "message": AUTH_REQUIRED_MESSAGE, "detail": {}}
    }
    assert OTHER_TOKEN not in response.text


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[1, 2]", b'{"token": ', b"", json.dumps({"token": "x" * 5000}).encode()],
)
async def test_a_malformed_login_body_is_a_422(tmp_path: Path, raw: bytes) -> None:
    """A body that is not one small JSON object is a 422, never a 500 or an echo.

    The last case crosses the 4 KiB cap, so a flood cannot make the route buffer.

    :param tmp_path: pytest temporary directory.
    :param raw: raw request body.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        response = await client.post(
            "/api/auth/login", content=raw, headers={"content-type": "application/json"}
        )

    assert response.status_code == 422
    assert _error_body(response)["code"] is ErrorCode.VALIDATION_ERROR
    assert "x" * 5000 not in response.text


async def test_login_with_no_token_configured_is_a_no_op(tmp_path: Path) -> None:
    """With no token set, logging in answers 200 and sets no cookie.

    A UI that posts to the route anyway must learn that nothing is required rather
    than being handed a credential for an empty token.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.post("/api/auth/login", json={"token": TOKEN})

    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "required": False}
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("scheme", ["bearer", "Bearer", "BEARER"])
async def test_the_bearer_scheme_is_case_insensitive(tmp_path: Path, scheme: str) -> None:
    """The scheme is matched case-insensitively, as RFC 7235 requires.

    :param tmp_path: pytest temporary directory.
    :param scheme: scheme spelling to send.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        response = await client.get("/api/settings", headers={"Authorization": f"{scheme} {TOKEN}"})

    assert response.status_code == 200


async def test_login_sets_a_cookie_that_unlocks_the_api(tmp_path: Path) -> None:
    """A successful login answers 200 and sets a cookie that is the credential.

    The attribute set is pinned exactly, ``Secure`` is pinned absent (the
    documented deployment is plain HTTP), and the cookie value is a signed
    timestamp rather than the token.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        login = await client.post("/api/auth/login", json={"token": TOKEN})
        assert login.status_code == 200
        assert cast("dict[str, object]", login.json()) == {"authenticated": True}

        header = login.headers["set-cookie"]
        value = header.split(";", 1)[0].split("=", 1)[1]
        unlocked = await client.get("/api/settings", headers=_cookie_header(value))

    assert header.startswith(f"{COOKIE_NAME}=v1.")
    assert "; Path=/" in header
    assert f"; Max-Age={COOKIE_MAX_AGE_S}" in header
    assert "; HttpOnly" in header
    assert "; SameSite=Lax" in header
    assert "Secure" not in header
    assert "Domain=" not in header
    assert TOKEN not in value
    assert len(value.split(".")) == 3
    assert unlocked.status_code == 200


async def test_a_forged_cookie_is_refused(tmp_path: Path) -> None:
    """A tampered MAC and a cookie signed by another token are both refused.

    The second case is the proof that the signing key is derived from the token:
    an attacker who knows the cookie format still cannot mint one.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        honest = issue_cookie(TOKEN.encode(), now=time.time())
        version, issued, mac = honest.split(".")
        flipped = f"{version}.{issued}.{mac[:-1]}{'A' if mac[-1] != 'A' else 'B'}"
        other_key = issue_cookie(OTHER_TOKEN.encode(), now=time.time())
        tampered = await client.get("/api/settings", headers=_cookie_header(flipped))
        foreign = await client.get("/api/settings", headers=_cookie_header(other_key))

    assert tampered.status_code == 401
    assert foreign.status_code == 401


async def test_rotating_the_token_invalidates_existing_cookies(tmp_path: Path) -> None:
    """Rotating ``server.token`` signs every issued cookie out on the next request.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        login = await client.post("/api/auth/login", json={"token": TOKEN})
        old_value = login.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        reused = await client.get("/api/settings", headers=_cookie_header(old_value))
        assert reused.status_code == 200

        rotated = await client.patch(
            "/api/settings", json={SETTING_KEY: OTHER_TOKEN}, headers=_bearer(TOKEN)
        )
        assert rotated.status_code == 200

        stale = await client.get("/api/settings", headers=_cookie_header(old_value))
        fresh_login = await client.post("/api/auth/login", json={"token": OTHER_TOKEN})
        fresh_value = fresh_login.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        fresh = await client.get("/api/settings", headers=_cookie_header(fresh_value))

    assert stale.status_code == 401
    assert fresh_login.status_code == 200
    assert fresh.status_code == 200


async def test_clearing_the_token_disables_auth_on_the_next_request(tmp_path: Path) -> None:
    """Clearing the setting turns the gate off with no restart.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        assert state.auth.required() is True
        gated = await client.get("/api/settings")

        cleared = await client.patch(
            "/api/settings", json={SETTING_KEY: None}, headers=_bearer(TOKEN)
        )
        assert cleared.status_code == 200
        assert state.auth.required() is False
        open_again = await client.get("/api/settings")

    assert gated.status_code == 401
    assert open_again.status_code == 200


async def test_the_token_is_compared_in_constant_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The credential is compared with ``hmac.compare_digest``, accepted or not.

    The spy delegates to the real function and records every call, so a future
    ``==`` would leave the call list empty and fail here.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher installing the spy.
    """
    calls: list[tuple[bytes, bytes]] = []
    real_compare = hmac.compare_digest

    def spy(presented: bytes, expected: bytes) -> bool:
        """Record one comparison and delegate to the real one."""
        calls.append((presented, expected))
        return real_compare(presented, expected)

    monkeypatch.setattr("chime_deck.auth.hmac.compare_digest", spy)
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        calls.clear()
        accepted = await client.get("/api/settings", headers=_bearer(TOKEN))
        after_accept = list(calls)
        calls.clear()
        rejected = await client.get("/api/settings", headers=_bearer(OTHER_TOKEN))
        after_reject = list(calls)

    assert accepted.status_code == 200
    assert after_accept == [(TOKEN.encode(), TOKEN.encode())]
    assert rejected.status_code == 401
    assert after_reject == [(OTHER_TOKEN.encode(), TOKEN.encode())]


async def test_an_expired_cookie_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cookie older than ``COOKIE_MAX_AGE_S`` is refused even with a valid MAC.

    The clock is moved rather than slept: a browser that ignores ``Max-Age`` still
    cannot use an ancient cookie, because the issue time is inside the signature.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: patcher moving the gate's clock.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        login = await client.post("/api/auth/login", json={"token": TOKEN})
        value = login.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]

        fresh = await client.get("/api/settings", headers=_cookie_header(value))
        expired_at = time.time() + COOKIE_MAX_AGE_S + 60
        monkeypatch.setattr("chime_deck.auth.clock", lambda: expired_at)
        expired = await client.get("/api/settings", headers=_cookie_header(value))

    assert fresh.status_code == 200
    assert expired.status_code == 401


async def test_the_websocket_is_refused_without_the_token(tmp_path: Path) -> None:
    """A gated ``/ws`` handshake is closed with 1008 before it is ever accepted.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        session = await _websocket_session(app)

    assert [message["type"] for message in session] == ["websocket.close"]
    assert session[0]["code"] == 1008


@pytest.mark.parametrize("scheme", ["cookie", "bearer"])
async def test_the_websocket_accepts_a_valid_credential(tmp_path: Path, scheme: str) -> None:
    """The socket accepts the session cookie or a bearer header and sends a snapshot.

    The cookie is the browser's transport (a handshake cannot carry an
    ``Authorization`` header), and the header is the one a non-browser client sets.

    :param tmp_path: pytest temporary directory.
    :param scheme: which credential transport to use.
    """
    app, _state = _build_app(tmp_path)
    value = issue_cookie(TOKEN.encode(), now=time.time())
    headers = (
        [(b"cookie", f"{COOKIE_NAME}={value}".encode())]
        if scheme == "cookie"
        else [(b"authorization", f"Bearer {TOKEN}".encode())]
    )

    async with _running(app) as client:
        await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        session = await _websocket_session(app, headers=headers)

    _assert_accepted_with_snapshot(session)


async def test_the_shared_token_is_never_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Neither the shared token nor the Home Assistant token reaches the log.

    ``[REDACTED]`` is required as well, so this is a positive control: the
    scrubber is proven live by the same captured text the tokens must be absent
    from. The middleware's own refusal line is checked separately, because it is
    the one line this ticket adds.

    :param tmp_path: pytest temporary directory.
    :param caplog: captured log records.
    """
    install_log_scrubber()
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        assert (await client.patch("/api/settings", json={"ha.token": HA_TOKEN})).status_code == 200
        assert (await client.patch("/api/settings", json={SETTING_KEY: TOKEN})).status_code == 200
        # Raised after startup on purpose: the lifespan applies the stored
        # ``system.log_level``, which would otherwise drop every DEBUG record
        # this test needs (including the middleware's own refusal line).
        caplog.set_level(logging.DEBUG)
        login = await client.post("/api/auth/login", json={"token": TOKEN})
        assert login.status_code == 200
        assert (await client.get("/api/settings", headers=_bearer(TOKEN))).status_code == 200
        # The login above stored a valid cookie on this client, so the refusal
        # has to come from a client that carries no cookie at all.
        client.cookies.clear()
        refused = await client.get("/api/settings", headers=_bearer(OTHER_TOKEN))
        assert refused.status_code == 401
        # The one line that proves the scrubber is registered and running.
        _LOGGER.debug("positive control: %s and %s", TOKEN, HA_TOKEN)

    refusals = [
        record.getMessage() for record in caplog.records if "refused" in record.getMessage()
    ]
    assert TOKEN not in caplog.text
    assert HA_TOKEN not in caplog.text
    assert REDACTED in caplog.text
    assert len(refusals) == 1
    assert "/api/settings" in refusals[0]
    assert TOKEN not in refusals[0]
    assert HA_TOKEN not in refusals[0]
    assert OTHER_TOKEN not in refusals[0]


async def test_settings_never_returns_the_shared_token(tmp_path: Path) -> None:
    """The token is write-only: only its presence travels back, never its value.

    :param tmp_path: pytest temporary directory.
    """
    app, _state = _build_app(tmp_path)

    async with _running(app) as client:
        before = await client.get("/api/settings")
        stored = await client.patch("/api/settings", json={SETTING_KEY: TOKEN})
        after = await client.get("/api/settings", headers=_bearer(TOKEN))

    reported: list[object] = []
    for response in (before, stored, after):
        body = cast("dict[str, object]", response.json())
        assert SETTING_KEY not in cast("dict[str, object]", body["settings"])
        assert TOKEN not in response.text
        reported.append(cast("dict[str, object]", body["secrets"])["server.token"])
    assert reported == [False, True, True]


async def test_is_exempt_names_exactly_the_documented_cases() -> None:
    """Only health, the two auth probes and the UI shell are ungated; nothing else is.

    Written against the predicate directly so the deny-by-default rule cannot be
    lost in a refactor of the middleware.
    """
    assert is_exempt("GET", "/api/health") is True
    assert is_exempt("POST", "/api/auth/login") is True
    # Booleans only, so the login prompt knows which credential to ask for
    # (HSD-n6wi); it reveals nothing a 401 does not.
    assert is_exempt("GET", "/api/auth/status") is True
    assert is_exempt("GET", "/") is True
    assert is_exempt("GET", "/settings/audio") is True
    assert is_exempt("GET", "/assets/app.js") is True

    # A route this ticket never saw is gated, because it lives under /api.
    assert is_exempt("GET", "/api/something-added-later") is False
    assert is_exempt("GET", "/api/health/extra") is False
    assert is_exempt("POST", "/api/health") is False
    assert is_exempt("GET", "/api/auth/login") is False
    assert is_exempt("POST", "/api/auth/status") is False
    assert is_exempt("GET", "/api/auth/password") is False
    assert is_exempt("GET", "/ws") is False
    assert is_exempt("GET", "/custom-icons/a.svg") is False


async def test_an_empty_configured_token_never_matches() -> None:
    """The comparison fails closed: an empty expected value matches nothing."""
    assert matches(b"", b"") is False
    assert matches(b"anything", b"") is False
    assert matches(b"same", b"same") is True


async def test_the_cookie_header_is_pinned() -> None:
    """The ``Set-Cookie`` string is fixed, and ``Secure`` is deliberately absent.

    A ``Secure`` cookie would be discarded on the documented plain-HTTP origin, so
    its absence is a decision recorded here rather than an oversight.
    """
    assert session_cookie_header("v1.1.abc") == (
        f"{COOKIE_NAME}=v1.1.abc; Path=/; Max-Age={COOKIE_MAX_AGE_S}; HttpOnly; SameSite=Lax"
    )
    assert "Secure" not in session_cookie_header("v1.1.abc")


async def test_cookie_validation_never_raises_on_malformed_input() -> None:
    """Every malformed cookie is a plain ``False``, because the value comes from the network."""
    token = TOKEN.encode()
    now = time.time()
    honest = issue_cookie(token, now=now)
    assert cookie_is_valid(honest, token, now=now) is True
    assert cookie_is_valid(None, token, now=now) is False
    assert cookie_is_valid("", token, now=now) is False
    assert cookie_is_valid("garbage", token, now=now) is False
    assert cookie_is_valid("v1.not-a-number.abc", token, now=now) is False
    assert cookie_is_valid("v2.1.abc", token, now=now) is False
    assert cookie_is_valid(honest, b"", now=now) is False
    # Issued in the future: a clock that moved backwards is not a valid cookie.
    assert cookie_is_valid(issue_cookie(token, now=now + 600), token, now=now) is False
    # The signature is keyed by the token, so another token's cookie is not ours.
    assert cookie_is_valid(issue_cookie(OTHER_TOKEN.encode(), now=now), token, now=now) is False


async def test_the_signing_key_is_derived_from_the_token() -> None:
    """Two tokens produce two keys, and neither key is the token."""
    first = signing_key(TOKEN.encode())
    second = signing_key(OTHER_TOKEN.encode())
    assert first != second
    assert first != TOKEN.encode()
    assert len(first) == 32
