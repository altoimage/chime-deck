"""Optional shared-token authentication: one gate in front of every state-bearing route.

``server.token`` is empty by default, and an empty token means **no
authentication**: the middleware passes every request through untouched, so an
install that never sets it behaves exactly as it did before this module existed.
A non-empty token switches the gate on, and then every request under ``/api``,
the ``/ws`` websocket and the user-supplied icons under ``/custom-icons`` must
carry either ``Authorization: Bearer <token>`` or the session cookie issued by
``POST /api/auth/login`` (:mod:`chime_deck.api.auth`).

Three rules make this a gate rather than a decoration:

* **Deny by default.** :func:`is_exempt` names the four cases that are served
  without a credential; everything else is gated, so a router added later under
  ``/api`` is protected without anyone remembering to add it here.
* **One comparison.** :func:`matches` is the only place a credential is
  compared and it uses :func:`hmac.compare_digest`, so a stray ``==`` cannot
  reintroduce a timing side channel.
* **Nothing is logged or echoed.** No function here logs, returns or embeds the
  token or the presented value; the one refusal line carries the method and the
  path only.

This gate is the *loopback* listener's credential. A request stamped as arriving
on the admin listener (:mod:`chime_deck.listener`, HSD-n6wi) passes through
untouched, because that listener has its own gate
(:class:`chime_deck.admin.AdminGate`) and two credentials must never stack: a
password login could not otherwise reach the configuration API while
``server.token`` is set.

The cookie is a signed, timestamped marker, never the token itself:
``doorbell_session=v1.<issued_epoch>.<mac>``, where the MAC is HMAC-SHA256 keyed
by a value derived from the token. Rotating the token therefore invalidates
every cookie ever issued, which is what "sign out" means in the docs.

Owning ticket: HSD-0jke.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, cast

from starlette.types import ASGIApp, Receive, Scope, Send

from .listener import ADMIN, listener_of
from .store import ConfigStore

__all__ = [
    "AUTH_REQUIRED_MESSAGE",
    "COOKIE_MAX_AGE_S",
    "COOKIE_NAME",
    "COOKIE_PATH",
    "SETTING_KEY",
    "SharedTokenMiddleware",
    "TokenGate",
    "bearer_token",
    "clear_session_cookie_header",
    "clock",
    "cookie_is_valid",
    "is_exempt",
    "issue_cookie",
    "matches",
    "session_cookie",
    "session_cookie_header",
    "signing_key",
]

_LOGGER = logging.getLogger(__name__)

COOKIE_NAME: Final = "doorbell_session"
"""Name of the session cookie; the token itself is never its value."""

COOKIE_PATH: Final = "/"
"""Path scope: the whole application, ``/ws`` and ``/custom-icons`` included."""

COOKIE_MAX_AGE_S: Final = 400 * 24 * 60 * 60
"""Cookie lifetime in seconds (34_560_000 = 400 days), enforced on both ends.

400 days is the longest lifetime Chrome will honour, so asking for more would be
a promise the browser does not keep. The issue time is inside the signed payload
and checked against this same constant, so a browser that ignores ``Max-Age``
still cannot use an ancient cookie.
"""

SETTING_KEY: Final = "server.token"
"""The setting this module reads; empty means "no authentication"."""

_SIGNING_CONTEXT: Final = b"chime-deck session cookie v1"
"""Domain separation for the cookie key, so it is not the token under another name."""

_PAYLOAD_VERSION: Final = "v1"
"""Cookie format version; a future format can be recognised instead of misparsed."""

_API_PREFIX: Final = "/api"
_HEALTH_PATH: Final = "/api/health"
_LOGIN_PATH: Final = "/api/auth/login"
_STATUS_PATH: Final = "/api/auth/status"
_CUSTOM_ICONS_PREFIX: Final = "/custom-icons"
_WEBSOCKET_PATH: Final = "/ws"
_GATED_EXACT_PATHS: Final = frozenset({_API_PREFIX, _CUSTOM_ICONS_PREFIX, _WEBSOCKET_PATH})

_REFUSE_CLOSE_CODE: Final = 1008
"""WebSocket close code for a refused handshake: 1008 is "policy violation"."""

_REFUSE_REASON: Final = "authentication required"
"""Close reason sent before ``accept()``; it says nothing about the credential."""

AUTH_REQUIRED_MESSAGE: Final = (
    "this doorbell requires a shared token — open the UI and enter it, or send "
    "'Authorization: Bearer <token>' matching server.token in settings"
)
"""The one 401 sentence, shared by the middleware and the login route.

It names the fix and never the value, the length or a prefix of it.
"""


def clock() -> float:
    """Return the wall clock the cookie lifetime is measured against.

    A function rather than a bare ``time.time`` call so a test can move the clock
    past :data:`COOKIE_MAX_AGE_S` without sleeping.
    """
    return time.time()


def matches(presented: bytes, expected: bytes) -> bool:
    """Compare a presented credential with the configured one in constant time.

    The **only** comparison in this module, and it fails closed: an empty
    *expected* never matches, so an empty credential can never unlock a
    misconfigured install. Callers pass pre-encoded bytes so the comparison runs
    over a fixed shape.

    :param presented: credential the request carried.
    :param expected: configured token; empty means "no credential can match".
    """
    if not expected:
        return False
    return hmac.compare_digest(presented, expected)


def bearer_token(header: str | None) -> bytes | None:
    """Extract the token from an ``Authorization`` header, or ``None``.

    The scheme is matched case-insensitively, as HTTP requires. Only the
    credential part is returned; a header that carries no credential is ``None``.

    :param header: raw ``Authorization`` header value, or ``None`` when absent.
    """
    if not header:
        return None
    parts = header.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    presented = parts[1].strip()
    if not presented:
        return None
    return presented.encode("utf-8", "surrogatepass")


def signing_key(token: bytes) -> bytes:
    """Derive the cookie's HMAC key from *token*.

    Deriving rather than storing a second secret means there is nothing extra for
    an operator to lose, "rotate the token" also means "sign out everywhere", and
    an empty token can never produce a valid signature.

    :param token: the configured shared token.
    """
    return hmac.new(token, _SIGNING_CONTEXT, hashlib.sha256).digest()


def _mac(key: bytes, payload: str) -> str:
    """Return the unpadded base64url MAC of *payload*.

    :param key: signing key from :func:`signing_key`.
    :param payload: the exact ``version.issued`` text that is signed.
    """
    digest = hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue_cookie(token: bytes, *, now: float) -> str:
    """Build a session-cookie value for *token*, valid from *now*.

    The value is ``v1.<issued_epoch>.<mac>``: it proves the holder was given the
    token, without ever containing it.

    :param token: the configured shared token.
    :param now: current wall clock, used as the issue time.
    """
    payload = f"{_PAYLOAD_VERSION}.{int(now)}"
    return f"{payload}.{_mac(signing_key(token), payload)}"


def cookie_is_valid(value: str | None, token: bytes, *, now: float) -> bool:
    """Return whether *value* is a session cookie this token issued, still fresh.

    Every failure — absent, malformed, wrong key, expired, issued in the future —
    is ``False``; nothing here raises on malformed input, because the value comes
    from the network.

    :param value: the ``doorbell_session`` cookie value, or ``None``.
    :param token: the configured shared token.
    :param now: current wall clock.
    """
    if not value or not token:
        return False
    parts = value.split(".")
    if len(parts) != 3:
        return False
    version, raw_issued, mac = parts
    if version != _PAYLOAD_VERSION:
        return False
    try:
        issued = int(raw_issued)
    except ValueError:
        return False
    expected = _mac(signing_key(token), f"{version}.{raw_issued}")
    if not matches(mac.encode("utf-8", "surrogatepass"), expected.encode("ascii")):
        return False
    return 0 <= now - issued <= COOKIE_MAX_AGE_S


def session_cookie_header(value: str, *, name: str = COOKIE_NAME) -> str:
    """Build the ``Set-Cookie`` header that hands *value* to the browser.

    ``HttpOnly`` keeps the marker away from any script, ``SameSite=Lax`` keeps it
    off cross-site mutating requests (the CSRF class that could actually reach
    ``PATCH``/``DELETE``), ``Path=/`` covers the whole app including ``/ws`` and
    ``/custom-icons``, and there is no ``Domain`` so the cookie is host-only.

    ``Secure`` is deliberately **absent**: the documented deployment is plain
    HTTP — loopback by default, optionally a LAN bind — and a ``Secure`` cookie
    is discarded on an ``http://`` origin, so setting it would make the kiosk
    prompt again on every page load, breaking the feature exactly where it is
    used. This cookie is not transport security; the app still must not be
    exposed to the internet.

    The *name* parameter exists for the admin listener's second cookie
    (:data:`chime_deck.admin.ADMIN_COOKIE_NAME`); the default keeps this header
    byte-identical for the shared-token session.

    :param value: the cookie value from :func:`issue_cookie`.
    :param name: cookie name to set; the shared-token session cookie by default.
    """
    return f"{name}={value}; Path={COOKIE_PATH}; Max-Age={COOKIE_MAX_AGE_S}; HttpOnly; SameSite=Lax"


def clear_session_cookie_header(*, name: str = COOKIE_NAME) -> str:
    """Build the ``Set-Cookie`` header that deletes a session cookie.

    Used when the credential a cookie stands for is removed: after the admin
    password is cleared there is nothing left for the old cookie to prove, and
    leaving it in the browser would be a marker for a credential that no longer
    exists.

    :param name: cookie name to clear; the shared-token session cookie by default.
    """
    return f"{name}=; Path={COOKIE_PATH}; Max-Age=0; HttpOnly; SameSite=Lax"


def is_exempt(method: str, path: str) -> bool:
    """Return whether a request is served **without** a credential.

    Four exemptions, and nothing else:

    1. ``GET /api/health`` — liveness only (``status``, ``version``,
       ``uptime_s``): it reads no configuration, no entity and no user content.
       It is also the call the UI makes before any credential can exist, the
       documented install smoke test and the kiosk reachability check, so gating
       it would break a documented verification step to protect a version string
       that ships in the JS bundle anyway.
    2. ``POST /api/auth/login`` — the only way to obtain a cookie, so it cannot
       require one. It has no side effect but ``Set-Cookie``, reads no state and
       compares the submitted value in constant time. Nothing else under
       ``/api/auth`` is exempt.
    3. ``GET /api/auth/status`` — booleans only (which listener, whether a
       password and a token are set, whether this listener is protected and
       whether this request is authenticated). It reveals nothing a 401 does not
       already: the login prompt has to know which credential to ask for before
       it can send one (HSD-n6wi).
    4. Everything that is not ``/api...``, not ``/ws`` and not
       ``/custom-icons...`` — exactly the paths ``spa_fallback`` serves: the
       built UI shell, its assets and the client-side routes. It ships inside the
       wheel and holds no configuration, entity data, audio or user content; the
       prompt cannot be shown unless the shell loads, which is why this is exempt
       rather than solved with a server-rendered login page.

    Everything else is gated: deny by default, so a new ``/api/...`` route is
    protected automatically.

    :param method: request method; a websocket scope has none and passes ``GET``.
    :param path: request path, with the leading slash.
    """
    upper = method.upper()
    if upper == "GET" and path in {_HEALTH_PATH, _STATUS_PATH}:
        return True
    if upper == "POST" and path == _LOGIN_PATH:
        return True
    return not _is_gated_path(path)


def _is_gated_path(path: str) -> bool:
    """Return whether *path* must carry a credential: the deny-by-default set.

    :param path: request path, with the leading slash.
    """
    if path in _GATED_EXACT_PATHS:
        return True
    if path.startswith(f"{_API_PREFIX}/"):
        return True
    return path.startswith(f"{_CUSTOM_ICONS_PREFIX}/")


@dataclass(slots=True)
class TokenGate:
    """The configured shared token, cached so a request never reads the store.

    ``token`` is ``None`` — authentication off — until a non-empty
    ``server.token`` is loaded, and it returns to ``None`` the moment the setting
    is cleared. A request therefore never opens the database, and a rotate or a
    clear takes effect on the next request with no restart.

    The fail-closed contract: a refusal is the default answer, an absent or empty
    :attr:`token` is the only state that turns the gate off, and a failed refresh
    leaves the previous value in place (``None`` before the first successful
    read, which is the documented "no authentication" default). Failure to
    *read* the setting is never treated as "the token is empty" while a token is
    known to be configured.
    """

    token: bytes | None = None
    """The token every state-bearing request must present; ``None`` means off."""

    async def refresh(self, store: ConfigStore) -> None:
        """Re-read ``server.token`` and cache it as bytes or ``None``.

        Surrounding whitespace is dropped, exactly like the renderer's own token
        read, because the bearer parser drops it from the presented value too: a
        stored value that could never be presented would lock the install out.

        :param store: open configuration store to read the setting from.
        """
        stored = await store.get_setting(SETTING_KEY)
        value = stored.strip() if isinstance(stored, str) else ""
        self.token = value.encode("utf-8", "surrogatepass") if value else None

    def required(self) -> bool:
        """Return whether a credential is required: ``True`` once a token is set."""
        return self.token is not None


def _raw_headers(scope: Scope) -> Sequence[tuple[bytes, bytes]]:
    """Return the raw header pairs of *scope*, or an empty sequence.

    :param scope: ASGI scope carrying ``headers`` as raw byte pairs.
    """
    return cast("Sequence[tuple[bytes, bytes]]", scope.get("headers") or ())


def _header(scope: Scope, name: bytes) -> str | None:
    """Return the first header called *name* from *scope*, or ``None``.

    :param scope: ASGI scope carrying ``headers`` as raw byte pairs.
    :param name: lowercase header name to look for.
    """
    for raw_name, raw_value in _raw_headers(scope):
        if raw_name.lower() == name:
            return raw_value.decode("latin-1")
    return None


def session_cookie(scope: Scope, *, name: str = COOKIE_NAME) -> str | None:
    """Return the session-cookie value called *name* from *scope*, or ``None``.

    Several ``Cookie`` headers are legal, so every one of them is searched. The
    admin listener reads its own, differently named cookie through this same
    function.

    :param scope: ASGI scope carrying ``headers`` as raw byte pairs.
    :param name: cookie name to look for; the shared-token session by default.
    """
    for raw_name, raw_value in _raw_headers(scope):
        if raw_name.lower() != b"cookie":
            continue
        for chunk in raw_value.decode("latin-1").split(";"):
            cookie_name, _, value = chunk.strip().partition("=")
            if cookie_name == name:
                return value
    return None


def _credential_is_valid(scope: Scope, token: bytes) -> bool:
    """Return whether the request carries a valid bearer token or session cookie.

    :param scope: ASGI scope of the request being gated.
    :param token: the configured shared token.
    """
    presented = bearer_token(_header(scope, b"authorization"))
    if presented is not None and matches(presented, token):
        return True
    return cookie_is_valid(session_cookie(scope), token, now=clock())


async def _send_unauthorized(scope: Scope, receive: Receive, send: Send) -> None:
    """Answer an HTTP request with the standard 401 envelope.

    The request body, if any, is deliberately not read: a gated request is refused
    before anything is buffered, so a flood with a large body costs no memory.

    :param scope: ASGI scope of the refused request.
    :param receive: ASGI receive callable, unused.
    :param send: ASGI send callable.
    """
    # Imported inside the function on purpose: ``chime_deck.api.errors`` lives
    # in a package whose ``__init__`` imports ``api.render``, which imports
    # :func:`bearer_token` from this module. A module-level import here would make
    # that a circular import whenever this module is imported first.
    from .api.errors import ErrorCode, error_response

    response = error_response(
        401,
        ErrorCode.UNAUTHORIZED,
        AUTH_REQUIRED_MESSAGE,
        headers={"WWW-Authenticate": "Bearer"},
    )
    await response(scope, receive, send)


class SharedTokenMiddleware:
    """Refuse every state-bearing request that carries no valid credential.

    A **pure ASGI** middleware rather than ``BaseHTTPMiddleware``: the app serves
    ``FileResponse`` and streamed audio, which ``BaseHTTPMiddleware`` handles
    badly, and it cannot see ``websocket`` scopes at all — and the socket is one
    of the surfaces being gated.

    One gate for every route, so no future route can be added "outside" it.
    """

    def __init__(self, app: ASGIApp, *, gate: TokenGate) -> None:
        """Wrap *app* and read the credential state from *gate*.

        :param app: the application to protect.
        :param gate: shared state the lifespan refreshes; the middleware never
            reads the store itself.
        """
        self._app = app
        self._gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass an authorised request through and refuse everything else.

        :param scope: ASGI scope.
        :param receive: ASGI receive callable.
        :param send: ASGI send callable.
        """
        if scope["type"] not in {"http", "websocket"}:
            # ``lifespan`` and anything else ASGI adds later is not a request.
            await self._app(scope, receive, send)
            return
        if listener_of(scope) == ADMIN:
            # The admin listener has its own credential (admin.AdminGate); the two
            # must never stack on one request, or a password login could not reach
            # the config API while server.token is set.
            await self._app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        if is_exempt(method, path):
            await self._app(scope, receive, send)
            return
        token = self._gate.token
        if token is None:
            # No token configured: every request passes through unchanged, so an
            # install without ``server.token`` is byte-for-byte what it was before
            # this module existed.
            await self._app(scope, receive, send)
            return
        if _credential_is_valid(scope, token):
            await self._app(scope, receive, send)
            return
        # Method and path only: never the credential, never its length.
        _LOGGER.debug("shared token authentication refused %s %s", method, path)
        if scope["type"] == "websocket":
            # Refuse the handshake before ``accept()``: ASGI turns a close here
            # into a rejected upgrade, so the socket route never runs.
            await send(
                {
                    "type": "websocket.close",
                    "code": _REFUSE_CLOSE_CODE,
                    "reason": _REFUSE_REASON,
                }
            )
            return
        await _send_unauthorized(scope, receive, send)
