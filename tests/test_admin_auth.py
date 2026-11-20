"""The configuration-interface credential: hashing, the gate, the routes (HSD-n6wi).

Every test drives the real application in process — ``httpx.ASGITransport`` for
HTTP — so nothing opens a socket. The four "absence" assertions are positive
controls where they can be: the log test requires the flow's own INFO line in the
same captured text it searches for secrets, and the cookie tests compare against
the exact header the module builds rather than a prefix.

Owning ticket: HSD-n6wi.
"""

from __future__ import annotations

import asyncio
import logging
import threading
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

from chime_deck.admin import (
    ADMIN_COOKIE_NAME,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PASSWORD_SETTING_KEY,
    AdminGate,
    create_admin_app,
    hash_password,
    verify_password,
)
from chime_deck.api import ErrorEnvelope
from chime_deck.app import AppState, create_app
from chime_deck.auth import clock, issue_cookie
from chime_deck.cli import reset_admin_password
from chime_deck.config import ServerConfig
from chime_deck.store import ConfigStore

PASSWORD = "correct horse battery staple"
OTHER_PASSWORD = "another long passphrase"
TOKEN = "shared-token-0123456789abcdef"

_LOGGER = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _isolated_log_state() -> Iterator[None]:
    """Keep the root logger and the registered secrets isolated per test."""
    with preserve_log_state():
        yield


@dataclass(slots=True)
class _Running:
    """The three handles one running application needs here."""

    loopback: httpx.AsyncClient
    admin: httpx.AsyncClient
    state: AppState


def _build_app(tmp_path: Path) -> tuple[FastAPI, AppState]:
    """Build an app with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory.
    """
    config = ServerConfig(
        data_dir=tmp_path / "data",
        static_dir=tmp_path / "no-dist",
        seed_demo_sounds=False,
    )
    app = create_app(config)
    return app, cast(AppState, app.state.doorbell)


@asynccontextmanager
async def _client(app: ASGIApp) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client bound to *app*.

    :param app: ASGI application under test.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@asynccontextmanager
async def _running(tmp_path: Path) -> AsyncIterator[_Running]:
    """Run the lifespan and yield clients for both listeners.

    Both use the same application and therefore the same store and gates, which
    is what production does: one process, one lifespan, two sockets.

    :param tmp_path: pytest temporary directory.
    """
    app, state = _build_app(tmp_path)
    admin_app = create_admin_app(app, gate=state.admin)
    async with (
        app.router.lifespan_context(app),
        _client(app) as loopback,
        _client(admin_app) as admin,
    ):
        yield _Running(loopback=loopback, admin=admin, state=state)


def _cookie_value(response: httpx.Response, name: str = ADMIN_COOKIE_NAME) -> str:
    """Return the value of one ``Set-Cookie`` header the response carries.

    :param response: response that set the cookie.
    :param name: cookie name to look for.
    """
    for raw in response.headers.get_list("set-cookie"):
        key, _, value = raw.split(";", 1)[0].partition("=")
        if key == name:
            return value
    raise AssertionError(f"the response set no {name} cookie: {response.headers}")


def _cookie_header(value: str, name: str = ADMIN_COOKIE_NAME) -> dict[str, str]:
    """Return the ``Cookie`` header carrying *value*.

    :param value: cookie value from a ``Set-Cookie`` header.
    :param name: cookie name to send.
    """
    return {"Cookie": f"{name}={value}"}


def _error_code(response: httpx.Response) -> str:
    """Return the machine-readable code of a failed response.

    :param response: response carrying the standard envelope.
    """
    envelope = ErrorEnvelope.model_validate(cast("dict[str, object]", response.json()))
    return envelope.error.code.value


async def _status(client: httpx.AsyncClient) -> dict[str, object]:
    """Fetch ``/api/auth/status`` and return its body.

    :param client: client for one listener.
    """
    response = await client.get("/api/auth/status")
    assert response.status_code == 200
    return cast("dict[str, object]", response.json())


async def _set_password(client: httpx.AsyncClient, password: str) -> httpx.Response:
    """Set the configuration password through the route.

    :param client: listener to set it on (either one may make the first write).
    :param password: password to set.
    """
    return await client.post("/api/auth/password", json={"new": password})


def test_hash_and_verify_round_trip() -> None:
    """A hash verifies its password and nothing else, and never contains it."""
    stored = hash_password(PASSWORD)

    assert stored.startswith("scrypt$16384$8$1$")
    assert stored.count("$") == 5
    assert PASSWORD not in stored
    assert verify_password(PASSWORD, stored) is True
    assert verify_password(OTHER_PASSWORD, stored) is False
    assert verify_password(f"{PASSWORD} ", stored) is False


def test_every_hash_carries_its_own_salt() -> None:
    """Two hashes of one password differ, so a stolen row is not a rainbow-table key."""
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)

    assert first != second
    assert verify_password(PASSWORD, first) is True
    assert verify_password(PASSWORD, second) is True


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "",
        "plaintext",
        "scrypt$16384$8$1$abc",
        "scrypt$notanumber$8$1$AAAA$AAAA",
        "scrypt$0$8$1$AAAA$AAAA",
        "scrypt$16384$8$1$!!!$!!!",
        "argon2$16384$8$1$AAAA$AAAA",
        # A stored cost the machine cannot pay: a hand edit must not make a login
        # allocate gigabytes, it must fail closed.
        "scrypt$1073741824$8$1$AAAA$AAAA",
    ],
)
def test_verify_fails_closed_on_a_malformed_hash(stored: str | None) -> None:
    """A corrupt or hostile stored value answers ``False`` instead of raising."""
    assert verify_password(PASSWORD, stored) is False


def test_verify_refuses_an_over_long_password_before_deriving() -> None:
    """A password over the accepted cap is refused without running scrypt."""
    stored = hash_password(PASSWORD)

    assert verify_password("x" * (MAX_PASSWORD_LENGTH + 1), stored) is False


@pytest.mark.asyncio
async def test_the_password_login_unlocks_the_configuration_api(tmp_path: Path) -> None:
    """The first password is a successful 200 that signs the caller in.

    Nothing is required before it (fresh install), and afterwards the cookie it
    set is the credential: a new client without it gets the 401 envelope.

    :param tmp_path: pytest temporary directory.
    """
    async with _running(tmp_path) as run:
        before = await _status(run.admin)
        assert before == {
            "listener": "admin",
            "password_set": False,
            "token_required": False,
            "protected": False,
            "authenticated": True,
        }

        set_response = await _set_password(run.admin, PASSWORD)

        assert set_response.status_code == 200
        assert PASSWORD not in set_response.text
        body = cast("dict[str, object]", set_response.json())
        assert body["password_set"] is True
        assert body["protected"] is True
        # The caller has just set the password and is handed a fresh cookie; the
        # answer must not describe the request's now-superseded (absent) cookie.
        assert body["authenticated"] is True
        cookie = _cookie_value(set_response)
        assert cookie.startswith("v1.")
        assert PASSWORD not in cookie
        assert run.state.admin.password_hash is not None
        assert PASSWORD not in run.state.admin.password_hash

        run.admin.cookies.clear()
        gated = await run.admin.get("/api/settings")
        assert gated.status_code == 401
        assert "www-authenticate" not in gated.headers
        assert _error_code(gated) == "unauthorized"
        assert PASSWORD not in gated.text

        with_cookie = await run.admin.get("/api/settings", headers=_cookie_header(cookie))
        assert with_cookie.status_code == 200

        wrong = await run.admin.post("/api/auth/login", json={"password": "not the password"})
        assert wrong.status_code == 401
        assert PASSWORD not in wrong.text

        login = await run.admin.post("/api/auth/login", json={"password": PASSWORD})
        assert login.status_code == 200
        assert login.json() == {"authenticated": True}


@pytest.mark.asyncio
async def test_rotating_the_password_invalidates_the_old_cookie(tmp_path: Path) -> None:
    """A rotation is a "sign out everywhere": the old marker no longer verifies.

    The response to the rotation carries a fresh cookie, so the caller who did the
    rotation stays signed in.

    :param tmp_path: pytest temporary directory.
    """
    async with _running(tmp_path) as run:
        first = _cookie_value(await _set_password(run.admin, PASSWORD))
        run.admin.cookies.clear()

        rotated = await run.admin.post(
            "/api/auth/password",
            json={"current": PASSWORD, "new": OTHER_PASSWORD},
            headers=_cookie_header(first),
        )

        assert rotated.status_code == 200
        # The rotation proved the current password and set a fresh cookie, so the
        # answer describes the state after the write, not the stale cookie's.
        assert cast("dict[str, object]", rotated.json())["authenticated"] is True
        fresh = _cookie_value(rotated)
        assert fresh != first

        stale = await run.admin.get("/api/settings", headers=_cookie_header(first))
        assert stale.status_code == 401
        current = await run.admin.get("/api/settings", headers=_cookie_header(fresh))
        assert current.status_code == 200

        old_login = await run.admin.post("/api/auth/login", json={"password": PASSWORD})
        assert old_login.status_code == 401
        new_login = await run.admin.post("/api/auth/login", json={"password": OTHER_PASSWORD})
        assert new_login.status_code == 200


@pytest.mark.asyncio
async def test_changing_or_clearing_needs_the_current_password_on_both_listeners(
    tmp_path: Path,
) -> None:
    """The current-password rule is a server-side authorisation rule.

    A loopback browser and an admin listener with a valid cookie both have to know
    the current password to rotate or remove it; a missing or wrong ``current`` is
    the same 401 and writes nothing.

    :param tmp_path: pytest temporary directory.
    """
    async with _running(tmp_path) as run:
        assert (await _set_password(run.loopback, PASSWORD)).status_code == 200
        stored = run.state.admin.password_hash
        assert stored is not None
        login = await run.admin.post("/api/auth/login", json={"password": PASSWORD})
        assert login.status_code == 200
        cookie = _cookie_value(login)
        run.admin.cookies.clear()

        for client, headers in (
            (run.loopback, {}),
            (run.admin, _cookie_header(cookie)),
        ):
            for body in ({"new": OTHER_PASSWORD}, {"current": None, "new": OTHER_PASSWORD}):
                refused = await client.post("/api/auth/password", json=body, headers=headers)
                assert refused.status_code == 401
                cleared = await client.post("/api/auth/password/clear", json={}, headers=headers)
                assert cleared.status_code == 401
            wrong = await client.post(
                "/api/auth/password",
                json={"current": "not it at all", "new": OTHER_PASSWORD},
                headers=headers,
            )
            assert wrong.status_code == 401

        # Nothing was written by any of the refusals.
        assert run.state.admin.password_hash == stored
        assert verify_password(PASSWORD, run.state.admin.password_hash) is True

        # And with the current password, the same calls succeed.
        changed = await run.admin.post(
            "/api/auth/password",
            json={"current": PASSWORD, "new": OTHER_PASSWORD},
            headers=_cookie_header(cookie),
        )
        assert changed.status_code == 200
        rotated_cookie = _cookie_value(changed)
        removed = await run.admin.post(
            "/api/auth/password/clear",
            json={"current": OTHER_PASSWORD},
            headers=_cookie_header(rotated_cookie),
        )
        assert removed.status_code == 200
        # Nothing is required of the password afterwards, so the answer says so
        # rather than describing the request whose cookie the response clears.
        assert cast("dict[str, object]", removed.json())["authenticated"] is True
        assert removed.headers["set-cookie"].startswith(f"{ADMIN_COOKIE_NAME}=;")
        assert "Max-Age=0" in removed.headers["set-cookie"]
        # ``cast`` because the earlier ``assert stored is not None`` narrows this
        # attribute for mypy, which cannot see the route rewriting it.
        assert cast("str | None", run.state.admin.password_hash) is None
        assert (await _status(run.admin))["password_set"] is False


@pytest.mark.parametrize(
    "new",
    [
        "short",
        "x" * (MIN_PASSWORD_LENGTH - 1),
        "x" * (MAX_PASSWORD_LENGTH + 1),
        12345678,
        None,
    ],
)
@pytest.mark.asyncio
async def test_a_password_outside_the_accepted_shape_is_a_422(tmp_path: Path, new: object) -> None:
    """Too short, too long or not a string is a 422 that echoes no value.

    :param tmp_path: pytest temporary directory.
    :param new: submitted ``new`` value.
    """
    async with _running(tmp_path) as run:
        response = await run.loopback.post("/api/auth/password", json={"new": new})

        assert response.status_code == 422
        assert _error_code(response) == "validation_error"
        if isinstance(new, str) and new:
            assert new not in response.text
        assert run.state.admin.password_hash is None
        assert await run.state.store.get_setting(PASSWORD_SETTING_KEY) == ""


@pytest.mark.asyncio
async def test_the_shortest_accepted_password_works(tmp_path: Path) -> None:
    """The guardrail is the boundary, not a policy engine: exactly 8 is accepted.

    :param tmp_path: pytest temporary directory.
    """
    boundary = "12345678"

    async with _running(tmp_path) as run:
        response = await _set_password(run.loopback, boundary)

        assert response.status_code == 200
        assert verify_password(boundary, run.state.admin.password_hash) is True


@pytest.mark.asyncio
async def test_the_status_route_reports_booleans_on_both_listeners(tmp_path: Path) -> None:
    """The status route describes the install and the listener, never a value.

    :param tmp_path: pytest temporary directory.
    """
    async with _running(tmp_path) as run:
        loopback = await _status(run.loopback)
        assert loopback == {
            "listener": "loopback",
            "password_set": False,
            "token_required": False,
            "protected": False,
            "authenticated": True,
        }

        await _set_password(run.loopback, PASSWORD)
        await run.loopback.patch("/api/settings", json={"server.token": TOKEN})

        after = await _status(run.loopback)
        assert after == {
            "listener": "loopback",
            "password_set": True,
            "token_required": True,
            "protected": True,
            "authenticated": False,
        }
        authenticated = await run.loopback.get(
            "/api/auth/status", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert authenticated.status_code == 200
        assert cast("dict[str, object]", authenticated.json())["authenticated"] is True

        admin = await _status(run.admin)
        assert admin == {
            "listener": "admin",
            "password_set": True,
            "token_required": True,
            "protected": True,
            "authenticated": False,
        }
        assert PASSWORD not in str(admin)
        assert TOKEN not in str(admin)


@pytest.mark.asyncio
async def test_the_open_status_route_derives_a_bearer_password_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bearer password on the open status route never blocks the event loop.

    ``GET /api/auth/status`` is open on the admin listener, so any LAN client can
    reach the bearer-password branch; the scrypt derivation must therefore run in
    a worker thread, or one request stalls every other connection — the kiosk's
    included — for the length of the derivation.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    threads: list[threading.Thread] = []

    def _record(password: str, stored: str | None) -> bool:
        threads.append(threading.current_thread())
        return False

    async with _running(tmp_path) as run:
        assert (await _set_password(run.loopback, PASSWORD)).status_code == 200
        run.admin.cookies.clear()
        monkeypatch.setattr("chime_deck.admin.verify_password", _record)
        response = await run.admin.get(
            "/api/auth/status", headers={"Authorization": f"Bearer {PASSWORD}"}
        )

    assert response.status_code == 200
    assert len(threads) == 1
    assert threads[0] is not threading.main_thread()


@pytest.mark.asyncio
async def test_authorisation_precedes_the_new_password_validator(tmp_path: Path) -> None:
    """A wrong ``current`` is a 401 even when ``new`` is also outside the accepted shape.

    Order is the rule: if the length check ran first, a caller without the current
    password could use the ``422`` to probe the validator.

    :param tmp_path: pytest temporary directory.
    """
    async with _running(tmp_path) as run:
        assert (await _set_password(run.loopback, PASSWORD)).status_code == 200

        wrong_current = await run.loopback.post(
            "/api/auth/password", json={"current": "not it at all", "new": "abc"}
        )
        short_new = await run.loopback.post(
            "/api/auth/password", json={"current": PASSWORD, "new": "abc"}
        )

    assert wrong_current.status_code == 401
    assert _error_code(wrong_current) == "unauthorized"
    assert short_new.status_code == 422
    assert _error_code(short_new) == "validation_error"


@pytest.mark.asyncio
async def test_the_admin_listener_falls_back_to_the_shared_token(tmp_path: Path) -> None:
    """A token-protected install is not silently opened by the admin listener.

    The OR is load-bearing: password, else shared token, else open. With no
    password the admin listener accepts the token (and its cookie), and a body
    carrying the token logs in exactly like the loopback route does.

    :param tmp_path: pytest temporary directory.
    """
    async with _running(tmp_path) as run:
        await run.loopback.patch("/api/settings", json={"server.token": TOKEN})

        status = await _status(run.admin)
        assert status["protected"] is True
        assert status["password_set"] is False
        assert status["token_required"] is True

        wrong = await run.admin.post("/api/auth/login", json={"token": "nope"})
        assert wrong.status_code == 401
        assert wrong.headers["www-authenticate"] == "Bearer"

        login = await run.admin.post("/api/auth/login", json={"token": TOKEN})
        assert login.status_code == 200
        cookie = _cookie_value(login)
        unlocked = await run.admin.get("/api/settings", headers=_cookie_header(cookie))
        assert unlocked.status_code == 200


@pytest.mark.asyncio
async def test_a_password_install_does_not_accept_the_token(tmp_path: Path) -> None:
    """When a password is set, the token is not a second way into the admin API.

    :param tmp_path: pytest temporary directory.
    """
    async with _running(tmp_path) as run:
        await _set_password(run.loopback, PASSWORD)
        await run.loopback.patch("/api/settings", json={"server.token": TOKEN})
        run.admin.cookies.clear()

        refused = await run.admin.post("/api/auth/login", json={"token": TOKEN})

        assert refused.status_code == 401
        assert "www-authenticate" not in refused.headers


@pytest.mark.asyncio
async def test_the_admin_stamp_bypasses_the_shared_token_gate(tmp_path: Path) -> None:
    """The two credentials never stack on one request ([R2-1]).

    With both set, the admin cookie (signed by the password hash) must reach the
    config API, while the loopback gate still refuses everything that is not the
    token. If ``SharedTokenMiddleware`` did not skip admin-stamped scopes, the
    admin login would be unusable on this install.

    :param tmp_path: pytest temporary directory.
    """
    async with _running(tmp_path) as run:
        assert (await _set_password(run.loopback, PASSWORD)).status_code == 200
        assert (
            await run.loopback.patch("/api/settings", json={"server.token": TOKEN})
        ).status_code == 200

        login = await run.admin.post("/api/auth/login", json={"password": PASSWORD})
        assert login.status_code == 200
        cookie = _cookie_value(login)
        run.admin.cookies.clear()

        admin_read = await run.admin.get("/api/settings", headers=_cookie_header(cookie))
        assert admin_read.status_code == 200

        run.loopback.cookies.clear()
        loopback_marker = await run.loopback.get("/api/settings", headers=_cookie_header(cookie))
        assert loopback_marker.status_code == 401
        loopback_open = await run.loopback.get(
            "/api/settings", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert loopback_open.status_code == 200


@pytest.mark.asyncio
async def test_the_flow_never_logs_a_password_a_hash_or_a_cookie(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Set, login, refuse, rotate and clear log no secret value.

    The same captured text must contain the flow's own INFO line, so the absence
    assertions cannot pass because nothing was captured.

    :param tmp_path: pytest temporary directory.
    :param caplog: pytest log capture fixture.
    """
    with caplog.at_level(logging.DEBUG):
        async with _running(tmp_path) as run:
            first = await _set_password(run.loopback, PASSWORD)
            first_cookie = _cookie_value(first)
            stored = run.state.admin.password_hash
            assert stored is not None

            await run.admin.post("/api/auth/login", json={"password": "wrong password"})
            await run.admin.post("/api/auth/login", json={"password": PASSWORD})
            await run.admin.get("/api/settings")
            await run.admin.post(
                "/api/auth/password",
                json={"current": PASSWORD, "new": OTHER_PASSWORD},
                headers=_cookie_header(first_cookie),
            )
            await run.admin.post("/api/auth/password/clear", json={"current": OTHER_PASSWORD})

    text = caplog.text
    assert "the configuration-interface password was set" in text
    assert "the configuration-interface password was cleared" in text
    for secret in (PASSWORD, OTHER_PASSWORD, stored, first_cookie, TOKEN):
        assert secret not in text


async def _seed_password_hash(path: Path, value: str) -> None:
    """Write a password hash straight into a store, as an earlier run would have.

    :param path: database path.
    :param value: hash to store.
    """
    async with ConfigStore(path) as store, store.transaction():
        await store.set_setting(PASSWORD_SETTING_KEY, value)


async def _stored_hash(path: Path) -> object:
    """Read the stored password hash.

    :param path: database path.
    """
    async with ConfigStore(path) as store:
        return await store.get_setting(PASSWORD_SETTING_KEY)


def test_reset_admin_password_clears_the_hash_and_serves_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--reset-admin-password`` is the physical recovery path: write, log, exit 0.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: pytest monkeypatch fixture.
    :param caplog: pytest log capture fixture.
    """
    from chime_deck import cli

    config = ServerConfig(data_dir=tmp_path, seed_demo_sounds=False)
    stored = hash_password(PASSWORD)
    asyncio.run(_seed_password_hash(config.database_path, stored))

    def _forbid(_configs: object) -> int:
        raise AssertionError("the serving layer must not be reached")

    monkeypatch.setattr("chime_deck.serving.serve", _forbid)

    with caplog.at_level(logging.INFO, logger="chime_deck.cli"):
        status = cli.main(["--data-dir", str(tmp_path), "--reset-admin-password", "--no-admin"])

    assert status == 0
    assert asyncio.run(_stored_hash(config.database_path)) == ""
    reset = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and "restart the service" in record.getMessage()
    ]
    assert reset == ["the configuration-interface password was cleared; restart the service"]
    assert stored not in caplog.text
    assert PASSWORD not in caplog.text


def test_reset_admin_password_reports_failure_as_status_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A store failure is one ERROR line naming the exception, and exit status 1.

    The failure is injected at the coroutine rather than by breaking the file,
    because the pre-existing schema preflight runs first and owns *its* failures.

    :param tmp_path: pytest temporary directory.
    :param monkeypatch: pytest monkeypatch fixture.
    :param caplog: pytest log capture fixture.
    """
    from chime_deck import cli

    async def _explode(_config: ServerConfig) -> None:
        raise RuntimeError("the store is not writable")

    monkeypatch.setattr("chime_deck.cli._clear_stored_password", _explode)

    with caplog.at_level(logging.ERROR, logger="chime_deck.cli"):
        status = cli.main(["--data-dir", str(tmp_path), "--reset-admin-password", "--no-admin"])

    assert status == 1
    errors = [
        record
        for record in caplog.records
        if record.name == "chime_deck.cli" and record.levelno == logging.ERROR
    ]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "RuntimeError" in message
    assert "the store is not writable" in message
    assert "Traceback" not in caplog.text


def test_reset_admin_password_is_a_plain_function_and_a_documented_flag() -> None:
    """The recovery path returns a status, and the parser documents it."""
    from chime_deck import cli

    assert callable(reset_admin_password)
    assert reset_admin_password.__doc__ is not None
    help_text = cli.build_parser().format_help()
    assert "--reset-admin-password" in help_text
    assert "--no-admin" in help_text
    assert "--admin-host" in help_text
    assert "--admin-port" in help_text


def test_admin_gate_credential_kind_and_signing_material() -> None:
    """The gate's OR is password > token > open, and the key follows the same order."""
    assert AdminGate().credential_kind() is None
    assert AdminGate().signing_material() == b""

    token_gate = AdminGate(token=b"tok")
    assert token_gate.credential_kind() == "token"
    assert token_gate.signing_material() == b"tok"
    assert token_gate.required() is True

    password_gate = AdminGate(password_hash="stored", token=b"tok")
    assert password_gate.credential_kind() == "password"
    assert password_gate.signing_material() == b"stored"
    assert password_gate.required() is True


def test_an_admin_cookie_is_signed_with_the_gate_material() -> None:
    """A cookie built by the shared machinery verifies against the admin gate."""
    gate = AdminGate(password_hash=hash_password(PASSWORD))
    value = issue_cookie(gate.signing_material(), now=clock())

    assert value.startswith("v1.")
    assert PASSWORD not in value
    assert verify_password(PASSWORD, gate.password_hash) is True
