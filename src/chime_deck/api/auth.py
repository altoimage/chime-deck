"""The ``/api/auth`` routes: turn a credential into the cookie the browser sends.

Four routes, one scheme:

* ``POST /api/auth/login`` exchanges the credential this listener requires for a
  session cookie. The cookie exists because the browser has to attach a credential
  to requests that cannot carry an ``Authorization`` header at all — the ``/ws``
  handshake and ``<img src="/custom-icons/...">``. On the loopback listener the
  credential is the shared ``server.token`` and nothing changed with HSD-n6wi; on
  the admin listener it is the configuration password, else the shared token, else
  nothing at all (:class:`chime_deck.admin.AdminGate`).
* ``GET /api/auth/status`` reports booleans, so the login prompt knows *which*
  credential to ask for before it has one. It reveals nothing a 401 does not.
* ``POST /api/auth/password`` and ``POST /api/auth/password/clear`` write and
  remove the configuration password. Both require the **current** password
  whenever one is set, on **both** listeners: that is an authorisation rule
  enforced here, not a UI convention (a loopback browser must not be able to
  rotate the password behind the operator's back, and neither must a stolen
  admin cookie). ``current`` is never compared to anything else.

Four properties are load-bearing:

* **Exempt from the gate** (:func:`chime_deck.auth.is_exempt`), because login is
  the only way to obtain a credential and the status route only returns booleans.
* **The body is read by hand, with a 4 KiB cap**, not by a Pydantic model: the
  credential is compared before anything is buffered or validated, the same rule
  :mod:`chime_deck.api.render` documents. A malformed body is a clean ``422``.
* **Nothing is echoed.** A success body is booleans, and a ``401`` is the
  module-level sentence from :mod:`chime_deck.auth` — never the submitted value,
  never the expected one, never a length or a prefix. Setting a password answers
  with the same booleans as the status route, so it can never repeat the value.
* **A password check never blocks the event loop.** ``scrypt`` costs ~26 ms here
  and ~0.2 s on the reference panel, so every verification and every hash runs in
  a worker thread (:func:`asyncio.to_thread`).

Owning ticket: HSD-0jke; the password routes and the status route are HSD-n6wi.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Final, cast

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..admin import (
    ADMIN_COOKIE_NAME,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PASSWORD_SETTING_KEY,
    admin_credential_is_valid,
    hash_password,
    verify_password,
)
from ..auth import (
    AUTH_REQUIRED_MESSAGE,
    bearer_token,
    clear_session_cookie_header,
    clock,
    cookie_is_valid,
    issue_cookie,
    matches,
    session_cookie,
    session_cookie_header,
)
from ..config import refresh_secret_scrubber
from ..listener import ADMIN, LOOPBACK, listener_of
from .errors import ApiError, ErrorCode

if TYPE_CHECKING:
    from ..app import AppState

__all__ = ["LOGIN_MAX_BODY_BYTES", "build_auth_router"]

_LOGGER = logging.getLogger(__name__)

LOGIN_MAX_BODY_BYTES: Final = 4096
"""Largest login body accepted.

A ``{"token": "..."}`` or ``{"password": "..."}`` object is never long:
``server.token`` is capped at 1024 characters, the password at
:data:`~chime_deck.admin.MAX_PASSWORD_LENGTH`, and JSON escaping cannot double
either. Anything bigger is refused unread past the cap, so a flood cannot make the
server buffer.
"""

_BODY_TOO_LARGE: Final = (
    f"the login body is larger than {LOGIN_MAX_BODY_BYTES} bytes — "
    "send one JSON object with a 'token' or 'password' field"
)
"""Sentence for a body over the read cap."""

_BAD_BODY: Final = (
    "the body must be one JSON object with a 'token' or 'password' string — "
    "the credential itself is never echoed back"
)
"""Sentence for a body that is not UTF-8, not JSON or not an object."""

_PASSWORD_REQUIRED: Final = (
    "the body must be one JSON object with a 'new' string — "
    "the password itself is never echoed back"
)
"""Sentence for a set-password body with no ``new`` string."""

_PASSWORD_LENGTH: Final = (
    f"the configuration-interface password must be between {MIN_PASSWORD_LENGTH} and "
    f"{MAX_PASSWORD_LENGTH} characters"
)
"""Sentence for a ``new`` value outside the accepted length."""


def _unauthorised(*, www_authenticate: bool = True) -> ApiError:
    """Build the ``401`` a failed credential check answers with.

    The sentence is :data:`chime_deck.auth.AUTH_REQUIRED_MESSAGE`, the same one
    the middleware sends, so a rejected login and a gated request explain
    themselves identically — and neither can echo a submitted value.

    :param www_authenticate: whether to send the ``Bearer`` challenge. It is sent
        only when the shared token is the required credential, because that is the
        only scheme it honestly describes.
    """
    headers = {"WWW-Authenticate": "Bearer"} if www_authenticate else None
    return ApiError(401, ErrorCode.UNAUTHORIZED, AUTH_REQUIRED_MESSAGE, headers=headers)


def _declared_length(request: Request) -> int | None:
    """Return the declared ``Content-Length``, or ``None`` when absent or unusable.

    :param request: incoming request.
    """
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


async def _read_body(request: Request) -> bytes:
    """Read the request body, refusing anything larger than the cap.

    A declared length over the cap is refused without reading a byte; a body that
    lies about its length is abandoned as soon as the cap is crossed.

    :param request: incoming request.
    :raises ApiError: ``422`` for a body over the cap.
    """
    declared = _declared_length(request)
    if declared is not None and declared > LOGIN_MAX_BODY_BYTES:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, _BODY_TOO_LARGE)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > LOGIN_MAX_BODY_BYTES:
            raise ApiError(422, ErrorCode.VALIDATION_ERROR, _BODY_TOO_LARGE)
    return bytes(body)


async def _read_json_object(request: Request) -> dict[str, object]:
    """Read the body and decode it as one JSON object.

    :param request: incoming request.
    :raises ApiError: ``422`` for an over-cap, non-UTF-8, non-JSON or non-object
        body.
    """
    raw = await _read_body(request)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, _BAD_BODY) from None
    if not isinstance(payload, dict):
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, _BAD_BODY)
    return cast("dict[str, object]", payload)


def _submitted_string(payload: dict[str, object], key: str) -> str | None:
    """Return the string *payload* carries under *key*, or ``None``.

    A missing or non-string field is a missing credential, never a malformed
    request: the answer must not distinguish "wrong" from "the wrong shape".

    :param payload: decoded request body.
    :param key: field name to read.
    """
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _submitted_token(payload: dict[str, object]) -> bytes:
    """Pull the submitted shared token out of *payload*.

    :param payload: decoded request body.
    :raises ApiError: ``401`` when the body carries no ``token`` string.
    """
    submitted = _submitted_string(payload, "token")
    if submitted is None:
        raise _unauthorised()
    return submitted.encode("utf-8", "surrogatepass")


def build_auth_router(state: AppState) -> APIRouter:
    """Build the ``/api/auth`` router bound to *state*.

    Handlers are closures over *state*, following the house style of
    :mod:`chime_deck.app`: no handler annotates the state type, so FastAPI
    never has to resolve a forward reference at import time.

    :param state: application state holding both credential gates and the store.
    """
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    def _loopback_authenticated(request: Request) -> bool:
        """Return whether a loopback request carries a valid session.

        ``True`` when no token is configured, because then nothing is required:
        the dashboard and the whole API are open on loopback by design (D13).

        :param request: incoming request.
        """
        token = state.auth.token
        if token is None:
            return True
        presented = bearer_token(request.headers.get("authorization"))
        if presented is not None and matches(presented, token):
            return True
        return cookie_is_valid(session_cookie(request.scope), token, now=clock())

    async def _status(
        request: Request, *, authenticated_override: bool | None = None
    ) -> dict[str, object]:
        """Build the booleans both the status route and the two password routes answer.

        ``protected`` describes *this* listener: on the admin listener it is
        :meth:`chime_deck.admin.AdminGate.required`, and on loopback it is the
        shared token, because the password never gates the loopback dashboard.
        ``password_set`` and ``token_required`` describe the install, so the
        settings panel can show the security state from either listener.

        :param request: incoming request.
        :param authenticated_override: the ``authenticated`` answer to report
            instead of checking the request, used by the two password routes.
            They answer with the state their own write produced: the caller has
            just proved the current password (or the interface was open), and the
            cookie this request carried is the one that write superseded, so
            re-checking it would report the caller as signed out while handing
            them a fresh valid cookie.
        """
        gate = state.admin
        on_admin = listener_of(request.scope) == ADMIN
        token_required = state.auth.token is not None
        if on_admin:
            protected = gate.required()
            authenticated = await admin_credential_is_valid(request.scope, gate)
        else:
            protected = token_required
            authenticated = _loopback_authenticated(request)
        return {
            "listener": ADMIN if on_admin else LOOPBACK,
            "password_set": gate.password_hash is not None,
            "token_required": token_required,
            "protected": protected,
            "authenticated": (
                authenticated if authenticated_override is None else authenticated_override
            ),
        }

    async def _require_current_password(payload: dict[str, object]) -> None:
        """Refuse unless the body carries the current password, when one is set.

        The rule is the same on both listeners: there is no path that rotates or
        removes a password without knowing it. With no password set there is
        nothing to prove — the caller is either the open first-run install or an
        already-authenticated one.

        :param payload: decoded request body.
        :raises ApiError: ``401`` when a password is set and ``current`` does not
            match (or is missing).
        """
        stored = state.admin.password_hash
        if stored is None:
            return
        current = _submitted_string(payload, "current")
        if current is None:
            raise _unauthorised(www_authenticate=False)
        if not await asyncio.to_thread(verify_password, current, stored):
            raise _unauthorised(www_authenticate=False)

    async def _write_password_hash(value: str) -> None:
        """Store a new hash, refresh the scrubber and reload the gate.

        The scrubber and the gate are both refreshed here, not left to the store's
        settings-change subscriber: the very next request (the one whose response
        carries the fresh cookie) has to see the new signing material.

        :param value: the hash to store; "" clears the password.
        """
        async with state.store.transaction():
            await state.store.set_setting(PASSWORD_SETTING_KEY, value)
        await refresh_secret_scrubber(state.store)
        await state.admin.refresh(state.store)

    @router.post("/login")
    async def login(request: Request) -> Response:
        """Exchange this listener's credential for a session cookie.

        Order is load-bearing: "nothing is required" is answered before a byte is
        read (a no-op login), then the body is read with its cap, then the
        credential is checked — the shared token in constant time, the password in
        a worker thread. A mismatch, like a missing credential, is the same
        ``401`` — the route is not an oracle for "close" guesses.
        """
        gate = state.admin
        if listener_of(request.scope) == ADMIN:
            kind = gate.credential_kind()
            if kind is None:
                # Open by the owner's choice: the UI must learn that nothing is
                # required rather than be handed a credential for nothing.
                return JSONResponse({"authenticated": True, "required": False})
            payload = await _read_json_object(request)
            if kind == "password":
                password = _submitted_string(payload, "password")
                if password is None:
                    raise _unauthorised(www_authenticate=False)
                accepted = await asyncio.to_thread(verify_password, password, gate.password_hash)
                if not accepted:
                    raise _unauthorised(www_authenticate=False)
            else:
                if not matches(_submitted_token(payload), gate.signing_material()):
                    raise _unauthorised()
            # Signed with the credential's material, so a rotation invalidates it.
            response = JSONResponse({"authenticated": True})
            response.headers["set-cookie"] = session_cookie_header(
                issue_cookie(gate.signing_material(), now=clock()),
                name=ADMIN_COOKIE_NAME,
            )
            return response

        expected = state.auth.token
        if expected is None:
            # Auth is off, so logging in is a no-op: the UI cannot wedge itself by
            # posting to a route that has nothing to check.
            return JSONResponse({"authenticated": True, "required": False})
        submitted = _submitted_token(await _read_json_object(request))
        if not matches(submitted, expected):
            raise _unauthorised()
        response = JSONResponse({"authenticated": True})
        response.headers["set-cookie"] = session_cookie_header(issue_cookie(expected, now=clock()))
        return response

    @router.get("/status")
    async def status(request: Request) -> JSONResponse:
        """Report which listener answered, and whether it is protected.

        Booleans only: the login prompt needs to know which credential to ask for,
        and the settings panel needs to know whether to show the unprotected
        warning. Nothing here reveals a stored value.
        """
        return JSONResponse(await _status(request))

    @router.post("/password")
    async def set_password(request: Request) -> Response:
        """Set or rotate the configuration password, then keep the caller signed in.

        Order is load-bearing: the current password is proved **before** the new
        one is validated, so an unauthenticated caller cannot use the ``422`` as a
        probe of the validator, and the route reads as "authorise, then validate".
        The response carries a fresh admin cookie signed with the new hash, so the
        caller is not logged out by their own rotation — and the same booleans the
        status route answers, never the password.
        """
        payload = await _read_json_object(request)
        await _require_current_password(payload)
        new_password = _submitted_string(payload, "new")
        if new_password is None:
            raise ApiError(422, ErrorCode.VALIDATION_ERROR, _PASSWORD_REQUIRED)
        if not MIN_PASSWORD_LENGTH <= len(new_password) <= MAX_PASSWORD_LENGTH:
            raise ApiError(422, ErrorCode.VALIDATION_ERROR, _PASSWORD_LENGTH)
        hashed = await asyncio.to_thread(hash_password, new_password)
        await _write_password_hash(hashed)
        # No value, no length, no prefix: the operator only needs to know it is on.
        _LOGGER.info("the configuration-interface password was set")
        # ``authenticated`` is the state this write produced, not the one the
        # request's stale cookie describes.
        response = JSONResponse(await _status(request, authenticated_override=True))
        response.headers["set-cookie"] = session_cookie_header(
            issue_cookie(state.admin.signing_material(), now=clock()),
            name=ADMIN_COOKIE_NAME,
        )
        return response

    @router.post("/password/clear")
    async def clear_password(request: Request) -> Response:
        """Remove the configuration password and the cookie that stood for it.

        The answer describes the state the removal left: nothing is required of
        the password any more, so ``authenticated`` is ``True`` rather than the
        answer to a request whose cookie was just cleared.
        """
        payload = await _read_json_object(request)
        await _require_current_password(payload)
        await _write_password_hash("")
        _LOGGER.info("the configuration-interface password was cleared")
        response = JSONResponse(await _status(request, authenticated_override=True))
        response.headers["set-cookie"] = clear_session_cookie_header(name=ADMIN_COOKIE_NAME)
        return response

    return router
