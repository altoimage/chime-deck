"""Which listener accepted a request: a scope stamp, not a port guess.

The doorbell serves one application on two sockets — an unauthenticated loopback
listener (the dashboard, the kiosk) and a LAN admin listener that serves
configuration only (HSD-n6wi). A request therefore has to be able to answer "which
socket did I arrive on?", and the honest answer is *the listener that accepted the
connection*, not anything the client controls.

So the admin listener's own entry point stamps the ASGI scope with
:data:`LISTENER_SCOPE_KEY` before the application sees it:

* a client cannot forge it — it is not a header, a cookie or the ``Host``, and the
  loopback listener never sets it;
* everything that reaches the app through a transport that does not stamp it
  (``httpx.ASGITransport`` in the test suite, the renderer, the Playwright bench)
  is :data:`LOOPBACK` **by construction**, which is what keeps the existing
  suite's behaviour identical;
* it does not read hosts at all, so it cannot be confused by a listener bound to
  ``0.0.0.0`` whose ``scope["server"]`` is the concrete interface address.

The defaults are the security-relevant part: an absent stamp means the trusted,
unauthenticated listener, so adding a new entry point can never accidentally
*remove* the admin policy.

This module deliberately imports nothing from the project (only the ASGI scope
type), because both :mod:`chime_deck.auth` and :mod:`chime_deck.admin` read it and
they must not import each other.

Owning ticket: HSD-n6wi.
"""

from __future__ import annotations

from typing import Final

from starlette.types import Scope

__all__ = ["ADMIN", "LISTENER_SCOPE_KEY", "LOOPBACK", "listener_of"]

LISTENER_SCOPE_KEY: Final = "chime_deck.listener"
"""Scope key holding the listener that accepted the connection."""

LOOPBACK: Final = "loopback"
"""The trusted, unauthenticated listener — the whole app, kiosk included."""

ADMIN: Final = "admin"
"""The LAN listener: configuration only, behind a password once one is set."""


def listener_of(scope: Scope) -> str:
    """Return the listener that accepted *scope*'s connection.

    An absent or unrecognised stamp is :data:`LOOPBACK`, never :data:`ADMIN`:
    defaulting to the trusted listener means a new entry point that forgets to
    stamp its scope keeps today's behaviour instead of silently growing a
    configuration surface with no policy in front of it.

    :param scope: ASGI scope of the request being served.
    """
    stamped = scope.get(LISTENER_SCOPE_KEY)
    return stamped if isinstance(stamped, str) else LOOPBACK
