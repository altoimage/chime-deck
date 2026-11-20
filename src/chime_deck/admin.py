"""The LAN configuration interface: its password, its policy and its gate.

The doorbell's admin listener (HSD-n6wi) serves **configuration only**. This
module owns the three things that make that listener a separate, safe surface
without forking the application:

* **The password** — stdlib ``hashlib.scrypt`` with a per-install salt, stored as
  a self-describing ``scrypt$<n>$<r>$<p>$<salt>$<key>`` string in
  ``server.admin_password_hash``. The plaintext is never stored, returned or
  logged, and verification never raises on a corrupt row: it fails closed.
* **The policy** — :func:`admin_decision` is one table, deny by default. It names
  what the listener may serve (the settings shell, the wizard, the assets they
  need, and the API they call), what it redirects (``GET /`` never renders the
  dashboard from an admin URL) and what it refuses (the live ``/ws`` stream, the
  D29 tile selection, and everything else).
* **The gate** — :class:`AdminGate` caches the credential state so no request
  reads the store, and :func:`create_admin_app` is a pure-ASGI wrapper that stamps
  the scope (:mod:`chime_deck.listener`), applies the policy and then the gate.

One credential per listener, never stacked: the admin listener accepts its
password when one is set, otherwise the shared ``server.token`` (so an existing
token-protected LAN install is not silently unprotected), otherwise nothing —
which is the owner's deliberately-unprotected default and why the banner exists.
:class:`~chime_deck.auth.SharedTokenMiddleware` skips admin-stamped scopes, so a
password login can reach the config API while ``server.token`` is also set.

The cookie is the HSD-0jke signed marker under a second name, keyed by
:meth:`AdminGate.signing_material` — the stored hash when a password is set, else
the token. Changing or clearing the password therefore invalidates every issued
cookie, which is what "sign out everywhere" means. A distinct cookie name is
deliberate: cookies are not port-scoped, so both credentials would otherwise share
one jar entry.

Owning ticket: HSD-n6wi.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, cast

from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import (
    AUTH_REQUIRED_MESSAGE,
    bearer_token,
    clock,
    cookie_is_valid,
    matches,
    session_cookie,
)
from .auth import SETTING_KEY as TOKEN_SETTING_KEY
from .listener import ADMIN, LISTENER_SCOPE_KEY
from .store import ConfigStore

__all__ = [
    "ADMIN_COOKIE_NAME",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "PASSWORD_SETTING_KEY",
    "AdminDecision",
    "AdminGate",
    "admin_credential_is_valid",
    "admin_decision",
    "create_admin_app",
    "hash_password",
    "verify_password",
]

_LOGGER = logging.getLogger(__name__)

ADMIN_COOKIE_NAME: Final = "doorbell_admin_session"
"""Name of the admin listener's session cookie; the credential is never its value."""

PASSWORD_SETTING_KEY: Final = "server.admin_password_hash"
"""The stored password hash; empty means "no password set"."""

MIN_PASSWORD_LENGTH: Final = 8
"""Shortest accepted configuration password: a guardrail, not a policy engine."""

MAX_PASSWORD_LENGTH: Final = 1024
"""Longest accepted configuration password, so a login cannot be made expensive."""

_PHC_SCHEME: Final = "scrypt"
"""Name of the only accepted hash format, so a future scheme can be recognised."""

_SCRYPT_N: Final = 2**14
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_SCRYPT_SALT_BYTES: Final = 16
_SCRYPT_DKLEN: Final = 32

_REFUSE_CLOSE_CODE: Final = 1008
"""WebSocket close code for a refused handshake: 1008 is "policy violation"."""

_REFUSE_REASON: Final = "not served on the configuration interface"
"""Close reason for a refused socket; it names the policy, never a credential."""

_NOT_SERVED_MESSAGE: Final = (
    "this path is not served on the configuration interface — it serves the settings page "
    "and its API only, and the dashboard is not served here"
)
"""The one 404 sentence for a path the policy refuses; it names no value."""

_SETTINGS_PATH: Final = "/settings"
"""Where ``GET /`` on the admin listener redirects: the settings shell."""

_WEBSOCKET_PATH: Final = "/ws"
_TILES_PATH: Final = "/api/tiles"
_LOGIN_PATH: Final = "/api/auth/login"

_OPEN_GET_PATHS: Final = frozenset(
    {_SETTINGS_PATH, "/setup", "/setup/", "/api/health", "/api/auth/status"}
)
"""GET paths served without a credential: the shell, the wizard and two probes."""

_OPEN_GET_PREFIXES: Final = ("/settings/", "/assets/")
"""Client routes under the settings shell, and the two files ``index.html`` uses."""

_AUTH_PATHS: Final = (
    "/api/auth",
    "/api/settings",
    "/api/audio",
    "/api/mqtt",
    "/api/buttons",
    "/api/entities",
    "/api/sounds",
    "/api/tts",
    "/api/ha",
    "/api/history",
    "/api/config",
    "/api/about",
    "/api/icons",
    "/api/icon",
    "/custom-icons",
)
"""The settings shell's and the wizard's whole configuration API surface.

``/api/icon`` is the icon *file* route (``GET /api/icon/<name>``) that the icon
picker renders as ``<img>``; the plan's ``/api/icons`` row covers the icon API,
and leaving this one out would refuse the picker's images.
"""


class AdminDecision(StrEnum):
    """What the admin listener does with one ``(method, path)`` pair."""

    OPEN = "open"
    """Serve it without a credential."""

    AUTH = "auth"
    """Serve it only with the admin listener's credential."""

    REDIRECT_SETTINGS = "redirect_settings"
    """Redirect to the settings shell; the dashboard is not served here."""

    REFUSE = "refuse"
    """Answer 404 (or close the socket): deny by default."""


def _is_under(path: str, root: str) -> bool:
    """Return whether *path* is *root* itself or one of its children.

    :param path: request path, with the leading slash.
    :param root: path prefix without a trailing slash.
    """
    return path == root or path.startswith(f"{root}/")


def admin_decision(method: str, path: str) -> AdminDecision:
    """Return what the admin listener does with *method* and *path*.

    One table, deny by default: a route added under ``/api`` later is served only
    if it falls under a configuration prefix named here, and anything nobody
    thought about is refused rather than exposed.

    ``HEAD`` is decided as ``GET``: one method family, one decision. A HEAD
    carries no body, so it adds no surface — refusing it while the GET is served
    only made a health probe or a cache revalidation answer the policy's 404 on
    the LAN listener. The route layer then answers on both listeners alike (a
    Starlette static handler serves a HEAD; FastAPI's GET-only routes answer 405
    there too, which is the application's business, not this table's).

    :param method: request method; a websocket scope has none and passes ``GET``.
    :param path: request path, with the leading slash.
    """
    upper = method.upper()
    if upper == "HEAD":
        upper = "GET"
    if upper == "POST" and path == _LOGIN_PATH:
        return AdminDecision.OPEN
    if upper == "GET":
        if path == "/":
            return AdminDecision.REDIRECT_SETTINGS
        if path in _OPEN_GET_PATHS or path.startswith(_OPEN_GET_PREFIXES):
            return AdminDecision.OPEN
    if path == _WEBSOCKET_PATH or _is_under(path, _TILES_PATH):
        return AdminDecision.REFUSE
    if any(_is_under(path, root) for root in _AUTH_PATHS):
        return AdminDecision.AUTH
    return AdminDecision.REFUSE


def _b64encode(value: bytes) -> str:
    """Encode *value* as unpadded base64url, the form the stored hash uses.

    :param value: raw bytes to encode.
    """
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    """Decode unpadded base64url, raising on anything that is not that.

    :param value: text from a stored hash.
    :raises ValueError: if *value* is not valid base64url.
    """
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("not base64url") from exc


def _derive(password: str, salt: bytes, *, n: int, r: int, p: int, dklen: int) -> bytes:
    """Run one scrypt derivation over the UTF-8 form of *password*.

    :param password: the plaintext being hashed or checked.
    :param salt: per-hash salt.
    :param n: CPU/memory cost parameter.
    :param r: block size parameter.
    :param p: parallelisation parameter.
    :param dklen: length of the derived key.
    """
    return hashlib.scrypt(
        password.encode("utf-8", "surrogatepass"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=dklen,
    )


def hash_password(password: str) -> str:
    """Return the self-describing scrypt hash to store for *password*.

    The result is ``scrypt$<n>$<r>$<p>$<salt_b64>$<key_b64>``: every parameter is
    inside the value, so a future cost change can read old rows instead of
    guessing.

    :param password: the plaintext password; never stored or logged.
    """
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    derived = _derive(password, salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
    return (
        f"{_PHC_SCHEME}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${_b64encode(salt)}${_b64encode(derived)}"
    )


def verify_password(password: str, stored: str | None) -> bool:
    """Return whether *password* matches the stored hash.

    Total by construction: an empty, corrupt, truncated or absurdly expensive
    stored value is ``False`` and never an exception, because the stored value
    comes from a database row that a hand edit or a bad upgrade can damage, and a
    login must fail closed rather than answer 500. An over-long *password* is
    refused before any derivation runs.

    :param password: the plaintext password presented by the client.
    :param stored: the stored hash, or ``None`` when no password is set.
    """
    if not stored or len(password) > MAX_PASSWORD_LENGTH:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != _PHC_SCHEME:
        return False
    try:
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = _b64decode(parts[4])
        expected = _b64decode(parts[5])
    except ValueError:
        return False
    if n < 2 or r < 1 or p < 1 or not salt or not expected:
        return False
    try:
        derived = _derive(password, salt, n=n, r=r, p=p, dklen=len(expected))
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)


@dataclass(slots=True)
class AdminGate:
    """The admin listener's credential state, cached so no request reads the store.

    ``password_hash`` is the stored scrypt hash, or ``None`` when no password is
    set; ``token`` mirrors :attr:`~chime_deck.auth.TokenGate.token`, so the
    listener can fall back to the shared token. Like that gate, the fail-closed
    contract holds: a failed refresh leaves the previous values in place, and the
    only state that turns the gate off is "no password and no token".
    """

    password_hash: str | None = None
    """The stored password hash; ``None`` means no password is set."""

    token: bytes | None = None
    """The shared ``server.token``, or ``None`` when it is not set."""

    async def refresh(self, store: ConfigStore) -> None:
        """Re-read both credentials so a change applies to the next request.

        :param store: open configuration store to read the settings from.
        """
        stored_hash = await store.get_setting(PASSWORD_SETTING_KEY)
        password = stored_hash.strip() if isinstance(stored_hash, str) else ""
        self.password_hash = password or None
        stored_token = await store.get_setting(TOKEN_SETTING_KEY)
        token = stored_token.strip() if isinstance(stored_token, str) else ""
        self.token = token.encode("utf-8", "surrogatepass") if token else None

    def required(self) -> bool:
        """Return whether the admin listener demands a credential at all."""
        return self.credential_kind() is not None

    def credential_kind(self) -> Literal["password", "token"] | None:
        """Return which credential the admin listener demands, or ``None``.

        The password wins when one is set: the OR is not decoration, because an
        install that sets ``server.token`` and binds the LAN has a token-gated
        surface today, and a password-only listener would silently open it.
        """
        if self.password_hash is not None:
            return "password"
        if self.token is not None:
            return "token"
        return None

    def signing_material(self) -> bytes:
        """Return the bytes the admin cookie is signed with.

        The stored **hash** when a password is set — so changing or clearing it
        invalidates every issued cookie — else the shared token, else ``b""``
        (which can never produce a valid signature).
        """
        if self.password_hash is not None:
            return self.password_hash.encode("utf-8", "surrogatepass")
        return self.token if self.token is not None else b""


def _header(scope: Scope, name: bytes) -> str | None:
    """Return the first header called *name* from *scope*, or ``None``.

    :param scope: ASGI scope carrying ``headers`` as raw byte pairs.
    :param name: lowercase header name to look for.
    """
    raw_headers = cast("Sequence[tuple[bytes, bytes]]", scope.get("headers") or ())
    for raw_name, raw_value in raw_headers:
        if raw_name.lower() == name:
            return raw_value.decode("latin-1")
    return None


async def admin_credential_is_valid(scope: Scope, gate: AdminGate) -> bool:
    """Return whether the request carries the credential the admin listener wants.

    The one place the admin credential is checked, so the wrapper and the status
    route cannot disagree. A signed cookie is the normal path; a bearer value is
    accepted too (the password itself when a password is set, the shared token
    when the token is the required credential), which is what makes a plain
    ``curl -H 'Authorization: Bearer …'`` usable for the documented smoke test.
    Only that bearer-password fallback pays for a derivation, and only on the
    request that uses it; the cookie path is a MAC check. The derivation runs in a
    worker thread (:func:`asyncio.to_thread`) because ``scrypt`` costs ~26 ms here
    and ~0.2 s on the reference panel, and this function is reached from
    ``GET /api/auth/status``, which is open on the admin listener: doing it inline
    would let any LAN client stall the event loop (the kiosk included) with a
    bearer password.

    :param scope: ASGI scope of the request being gated.
    :param gate: the admin listener's credential state.
    """
    kind = gate.credential_kind()
    if kind is None:
        return True
    material = gate.signing_material()
    if cookie_is_valid(session_cookie(scope, name=ADMIN_COOKIE_NAME), material, now=clock()):
        return True
    presented = bearer_token(_header(scope, b"authorization"))
    if presented is None:
        return False
    if kind == "token":
        return matches(presented, material)
    stored = gate.password_hash
    if stored is None:
        return False
    return await asyncio.to_thread(
        verify_password, presented.decode("utf-8", "surrogatepass"), stored
    )


async def _redirect(scope: Scope, receive: Receive, send: Send, location: str) -> None:
    """Answer with a ``302`` to *location*, with no body of its own.

    :param scope: ASGI scope of the request.
    :param receive: ASGI receive callable, unused.
    :param send: ASGI send callable.
    :param location: absolute path to redirect to.
    """
    await RedirectResponse(url=location, status_code=302)(scope, receive, send)


async def _close_socket(send: Send) -> None:
    """Close a refused websocket before ``accept()``.

    ASGI turns a close here into a rejected upgrade, so the socket route never
    runs: the same refusal shape the shared-token gate uses.

    :param send: ASGI send callable.
    """
    await send(
        {
            "type": "websocket.close",
            "code": _REFUSE_CLOSE_CODE,
            "reason": _REFUSE_REASON,
        }
    )


async def _refuse_collection(scope: Scope, receive: Receive, send: Send) -> None:
    """Refuse a path the admin listener does not serve at all: 404, deny by default.

    :param scope: ASGI scope of the refused request.
    :param receive: ASGI receive callable, unused.
    :param send: ASGI send callable.
    """
    if scope["type"] == "websocket":
        await _close_socket(send)
        return
    # Imported inside the function on purpose: ``chime_deck.api.errors`` lives in a
    # package whose ``__init__`` imports every router, and those import
    # :mod:`chime_deck.auth`; a module-level import here would make that a
    # circular import whenever this module is imported first.
    from .api.errors import ErrorCode, error_response

    response = error_response(404, ErrorCode.NOT_FOUND, _NOT_SERVED_MESSAGE)
    await response(scope, receive, send)


async def _refuse_credential(
    scope: Scope, receive: Receive, send: Send, *, gate: AdminGate
) -> None:
    """Refuse a request that carries no usable admin credential.

    A websocket is closed before ``accept()``, and an HTTP request answers the
    standard 401 envelope. The ``WWW-Authenticate`` challenge is sent only when
    the *shared token* is the required credential, because that is the only one it
    honestly describes.

    :param scope: ASGI scope of the refused request.
    :param receive: ASGI receive callable, unused.
    :param send: ASGI send callable.
    :param gate: the admin listener's credential state.
    """
    if scope["type"] == "websocket":
        await _close_socket(send)
        return

    # Imported inside the function on purpose; see :func:`_refuse_collection`.
    from .api.errors import ErrorCode, error_response

    headers = {"WWW-Authenticate": "Bearer"} if gate.credential_kind() == "token" else None
    response = error_response(
        401,
        ErrorCode.UNAUTHORIZED,
        AUTH_REQUIRED_MESSAGE,
        headers=headers,
    )
    await response(scope, receive, send)


class _AdminApp:
    """The pure-ASGI wrapper that turns the doorbell app into the admin app.

    It never imports the routes: it stamps the scope, applies
    :func:`admin_decision`, applies :func:`admin_credential_is_valid` and
    delegates. Everything it refuses never reaches the application at all.
    """

    def __init__(self, app: ASGIApp, *, gate: AdminGate) -> None:
        """Wrap *app* and read the credential state from *gate*.

        :param app: the doorbell application to protect.
        :param gate: shared state the lifespan refreshes.
        """
        self._app = app
        self._gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Stamp the scope, apply the policy and the gate, then delegate.

        :param scope: ASGI scope.
        :param receive: ASGI receive callable.
        :param send: ASGI send callable.
        """
        scope[LISTENER_SCOPE_KEY] = ADMIN
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        decision = admin_decision(method, path)
        if decision is AdminDecision.OPEN:
            await self._app(scope, receive, send)
            return
        if decision is AdminDecision.REDIRECT_SETTINGS:
            await _redirect(scope, receive, send, _SETTINGS_PATH)
            return
        # Method and path only: never a credential, never its length.
        _LOGGER.debug("admin listener saw %s %s as %s", method, path, decision)
        if decision is AdminDecision.AUTH:
            if await admin_credential_is_valid(scope, self._gate):
                await self._app(scope, receive, send)
                return
            await _refuse_credential(scope, receive, send, gate=self._gate)
            return
        await _refuse_collection(scope, receive, send)


def create_admin_app(app: ASGIApp, *, gate: AdminGate) -> ASGIApp:
    """Build the admin listener's application around the doorbell *app*.

    ``create_app`` never calls this: the admin socket exists only when the CLI (or
    a test) asks for it, which is what keeps every existing single-listener test
    and the renderer role untouched.

    :param app: the doorbell application built by :func:`chime_deck.app.create_app`.
    :param gate: the gate the lifespan refreshes (``app.state.doorbell.admin``).
    """
    return _AdminApp(app, gate=gate)
