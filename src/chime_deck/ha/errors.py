"""Typed errors raised by the Home Assistant client.

Callers of :mod:`chime_deck.ha` never see a raw :mod:`httpx` error. Every
failure mode the client can produce is represented by one of these classes, so
the code that talks to Home Assistant can report *why* a call failed without
parsing transport text.

Every message is a whole sentence the user can act on.

``transient`` is the retry predicate. Its current consumer is the client's own
retry loop (``client.py:725``), and HSD-bmqp remains the named downstream
consumer still to come. It describes the *failure*, not the feature: whether a
chime should fall back to another engine, and whether ``tts_disabled`` or
``tts_unavailable`` is reported, is decided upstream in ``press.py`` from D27 and
must never be derived from these classes.

Owning ticket: HSD-8qf6.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "HaAuthError",
    "HaBadRequestError",
    "HaBadResponseError",
    "HaCommandError",
    "HaConfigurationError",
    "HaConnectionError",
    "HaError",
    "HaNotFoundError",
    "HaServerError",
    "HaTimeoutError",
]


class HaError(Exception):
    """Base class for every error raised by the Home Assistant client."""

    @property
    def transient(self) -> bool:
        """Whether repeating the same call later could succeed."""
        return False


class HaConfigurationError(HaError):
    """Raised when the stored connection settings cannot produce a working client.

    Covers an empty or unusable ``ha.base_url`` (missing scheme, bad port, query or
    fragment), a missing ``ha.token``, a redirect answer, a non-positive timeout, and a
    client that is used after it was closed. Retrying cannot help: the user has to
    change a setting.
    """


class HaConnectionError(HaError):
    """Raised when Home Assistant could not be reached at all."""

    @property
    def transient(self) -> bool:
        return True


class HaTimeoutError(HaError):
    """Raised when Home Assistant did not answer in time.

    Carries ``timeout_s``, the number of seconds that expired, which is also named in the
    message so the user can compare it with their expectation.
    """

    def __init__(self, sentence: str, *, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        super().__init__(sentence)

    @property
    def transient(self) -> bool:
        return True


class HaBadResponseError(HaError):
    """Raised when a 2xx answer could not be used: malformed JSON or an unexpected shape."""


class HaCommandError(HaError):
    """Raised when Home Assistant answered a websocket command with ``success: false``.

    ``code`` is Home Assistant's own machine-readable error code
    (``unknown_command``, ``not_found``, ``unauthorized``, …) when it sent one, and
    ``detail`` is its reason after redaction. Not transient: the same command with
    the same arguments will fail the same way.
    """

    def __init__(self, sentence: str, *, code: str | None, detail: str) -> None:
        """Record the redacted failure.

        :param sentence: whole-sentence message safe to show a user.
        :param code: Home Assistant's error code, or ``None`` when it sent none.
        :param detail: Home Assistant's reason, already redacted and clipped.
        """
        self.code = code
        self.detail = detail
        super().__init__(sentence)


class _HaStatusError(HaError):
    """Shared parent for every answer with a non-2xx status code.

    Private on purpose: callers catch the four public subclasses. It exists so the status
    code, the clipped detail and the transient rule are written once.

    ``transient`` is true for ``408``, ``429`` and any ``5xx``: those say "try again later",
    while ``400``/``401``/``403``/``404`` say "something has to change first".
    """

    _TRANSIENT_STATUSES: Final = frozenset({408, 429})

    def __init__(self, sentence: str, *, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(sentence)

    @property
    def transient(self) -> bool:
        return self.status_code in self._TRANSIENT_STATUSES or self.status_code >= 500


class HaAuthError(_HaStatusError):
    """Raised when Home Assistant rejected the token (401 or 403).

    The long-lived token in ``ha.token`` is wrong, expired, revoked, or belongs to
    a user without the admin rights ``/api/template`` requires. Not transient:
    retrying with the same token cannot help.
    """


class HaNotFoundError(_HaStatusError):
    """Raised when Home Assistant has no such entity or endpoint (404).

    Not transient: the name has to change in Settings before a retry can work.
    """


class HaBadRequestError(_HaStatusError):
    """Raised when Home Assistant refused the request (a 4xx we model no closer than this).

    The common real case is ``400`` from ``/api/template`` with an invalid jinja template,
    which is why ``detail`` carries Home Assistant's own explanation.
    """


class HaServerError(_HaStatusError):
    """Raised when Home Assistant failed to serve the request (5xx).

    A fault on the Home Assistant side, so it is transient: the same call may
    succeed once Home Assistant recovers.
    """
