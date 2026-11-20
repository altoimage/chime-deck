"""Async client for the Home Assistant REST endpoints this project needs.

It wraps exactly five endpoints — ``GET /api/`` (:meth:`HomeAssistantClient.api_status`),
``GET /api/states`` (:meth:`~HomeAssistantClient.states`), ``GET /api/states/<entity_id>``
(:meth:`~HomeAssistantClient.state`), ``POST /api/template``
(:meth:`~HomeAssistantClient.render_template`) and ``POST /api/tts_get_url``
(:meth:`~HomeAssistantClient.tts_get_url`) — and turns every failure into a typed error
from :mod:`.errors`, so callers never see a raw :mod:`httpx` exception.

**Client lifetime.** The object is cheap and short-lived. ``base_url`` and the token are
frozen at construction, which is what makes the header safe to build once per request:
after a settings change, build a new client instead of reusing the old one. Nothing here
opens a socket until a method is awaited.

**Retry rule.** Exactly one retry is attempted, and only for the idempotent GETs, and only
on a transient failure: a connect/transport error, a timeout, or a ``408``/``429``/``5xx``
answer. It is never attempted for ``401``/``403``/``404``/other ``4xx``, and never for a
``POST``, because ``/api/tts_get_url`` makes Home Assistant synthesise speech and write a
file, and a request that timed out may already have been executed on the other side.

**Timeout budget arithmetic.** One GET can take up to ``2 x (connect + read) +
retry_delay`` because of that single retry. ``timeout_s=`` on a call is the *total* budget
for the call, retry included, and overrides all four httpx timeout slots when it is given.

**Token secrecy.** The token is only ever placed in the ``Authorization`` header. A request
that is formatted for a log line goes through :func:`redact_headers`, and a message built
from a response body goes through the process-wide scrubber, so the token cannot reach a
log line, an exception message or a ``repr``.

Owning ticket: HSD-8qf6.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol, Self
from urllib.parse import quote, urlsplit

import httpx

from ..config import SECRET_SCRUBBER
from .errors import (
    HaAuthError,
    HaBadRequestError,
    HaBadResponseError,
    HaConfigurationError,
    HaConnectionError,
    HaError,
    HaNotFoundError,
    HaServerError,
    HaTimeoutError,
)

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_READ_TIMEOUT_S",
    "DEFAULT_RETRY_DELAY_S",
    "RETRYABLE_METHODS",
    "HaApiStatus",
    "HaState",
    "HaTtsUrl",
    "HomeAssistantClient",
    "SettingsReader",
    "normalise_base_url",
    "redact_headers",
]

_LOGGER = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_S: Final = 3.0
"""Seconds allowed for opening a connection to Home Assistant."""

DEFAULT_READ_TIMEOUT_S: Final = 10.0
"""Seconds allowed for Home Assistant to answer once connected."""

DEFAULT_RETRY_DELAY_S: Final = 0.25
"""Seconds slept between the first attempt and its single retry."""

RETRYABLE_METHODS: Final = frozenset({"GET"})
"""The methods the retry rule covers; a ``POST`` is never repeated."""

_BODY_CLIP: Final = 200
"""Maximum characters of a response body echoed into an error message."""

_BODY_JSON_LIMIT: Final = 65_536
"""Largest body that is parsed while looking for Home Assistant's ``message`` key."""

_REDACTED_TOKEN: Final = "***"
_REDACTED_HEADER: Final = "Bearer ***"


def normalise_base_url(raw: str) -> str:
    """Turn a stored ``ha.base_url`` into the prefix the endpoints are appended to.

    The result has no trailing slash, so ``f"{base}{path}"`` always works, and a
    trailing ``/api`` is removed: the Home Assistant documentation hands out base
    URLs with ``/api/`` appended, and copying one of those into Settings is a
    mistake worth absorbing. Nothing here opens a socket.

    :param raw: the value stored in ``ha.base_url``.
    :raises HaConfigurationError: if the value cannot be an http(s) base URL.
    """
    value = raw.strip()
    if not value:
        raise HaConfigurationError(
            "no Home Assistant URL is configured — set ha.base_url in Settings"
        )
    if any(char.isspace() for char in value):
        raise HaConfigurationError("ha.base_url must not contain spaces")
    if "?" in value or "#" in value:
        raise HaConfigurationError("ha.base_url must not contain a query string or fragment")

    # Detected by hand rather than with ``urlsplit``: ``localhost:8123`` parses as the
    # scheme ``localhost`` with no host, so the parser cannot tell a missing scheme from
    # a scheme that happens to look like a host name.
    scheme_end = value.find("://")
    if scheme_end == -1:
        value = f"http://{value}"
    else:
        scheme = value[:scheme_end].lower()
        if scheme not in {"http", "https"}:
            raise HaConfigurationError(
                f"ha.base_url must be an http:// or https:// URL — {value!r} uses {scheme}://"
            )
        value = f"{scheme}{value[scheme_end:]}"

    value = value.rstrip("/")
    # Segment match, not a suffix match, so ``/myapi`` and ``/apiary`` stay untouched.
    # The length guard keeps the loop from eating the ``/api`` that is part of
    # ``http://api``: the scheme delimiter must never be stripped away.
    while value.endswith("/api") and len(value) - 4 > value.find("://") + 3:
        value = value[:-4]

    try:
        split = urlsplit(value)
        hostname = split.hostname
        port = split.port
        username = split.username
        password = split.password
    except ValueError as exc:
        raise HaConfigurationError(f"ha.base_url has an invalid port — {exc}") from exc
    if username is not None or password is not None:
        raise HaConfigurationError(
            "ha.base_url must not contain a username or password — remove the "
            "'user:password@' part of the URL; credentials belong in ha.token"
        )
    if split.netloc.endswith(":"):
        raise HaConfigurationError("ha.base_url has an invalid port — the URL ends with ':'")
    if not hostname:
        # The original input, not the rewritten value, so the user can see what they typed.
        raise HaConfigurationError(f"ha.base_url must include a host name — {raw.strip()!r}")
    if port == 0:
        raise HaConfigurationError("ha.base_url has an invalid port — 0 is not a usable port")
    return value.rstrip("/")


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of *headers* with any authorisation value replaced.

    :param headers: headers that are about to be logged.
    """
    return {
        key: _REDACTED_HEADER if key.lower() == "authorization" else value
        for key, value in headers.items()
    }


@dataclass(frozen=True, slots=True)
class HaApiStatus:
    """The answer of ``GET /api/``: HA's own "API running." message."""

    message: str


@dataclass(frozen=True, slots=True)
class HaState:
    """One Home Assistant state object, reduced to the fields this project uses.

    ``attributes`` is a read-only view, so a frozen dataclass cannot hand out a
    mapping that the caller could mutate underneath it.
    """

    entity_id: str
    state: str
    attributes: Mapping[str, object]
    last_changed: str | None = None
    last_updated: str | None = None


@dataclass(frozen=True, slots=True)
class HaTtsUrl:
    """The answer of ``POST /api/tts_get_url``: where Home Assistant put the audio.

    The caller joins :attr:`path` onto the configured base URL: design 04 §3 prefers
    ``path`` because ``url`` may be Home Assistant's internal or external address,
    which can be unreachable from this device.

    :attr:`path` is ``None`` when Home Assistant did not send one. This client never
    fetches ``/api/tts_proxy/...`` itself; that endpoint takes no bearer token and is
    fetched by whatever plays the audio.
    """

    url: str
    path: str | None = None


class SettingsReader(Protocol):
    """The narrow settings port satisfied structurally by ``ConfigStore``."""

    async def get_setting(self, key: str) -> object:
        """Return the stored value of *key*, or its registry default.

        :param key: registered setting key.
        """
        ...


def _bad_shape(what: str, why: str) -> HaBadResponseError:
    """Build the error raised when a 2xx body cannot be used.

    :param what: short description of the thing that was requested.
    :param why: what is wrong with the answer.
    """
    return HaBadResponseError(f"Home Assistant returned {what} in an unexpected form — {why}")


def _optional_str(payload: Mapping[object, object], key: str, *, what: str) -> str | None:
    """Read an optional string field from an already-narrowed JSON object.

    :param payload: the JSON object that was checked with ``isinstance``.
    :param key: field to read.
    :param what: short description used in the error message.
    :raises HaBadResponseError: if the field is present but is not a string.
    """
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _bad_shape(what, f'its "{key}" is not a string')
    return value


def _parse_state(payload: object, *, what: str) -> HaState:
    """Reduce one ``/api/states`` object to the fields this project uses.

    Unknown keys (``context``, ``last_reported``) are ignored.

    :param payload: decoded JSON for one state object.
    :param what: short description used in the error message.
    :raises HaBadResponseError: if the payload is not a usable state object.
    """
    if not isinstance(payload, Mapping):
        raise _bad_shape(what, "it is not a JSON object")
    entity_id = payload.get("entity_id")
    state = payload.get("state")
    if not isinstance(entity_id, str) or not isinstance(state, str):
        raise _bad_shape(what, 'it has no "entity_id" and "state" strings')
    attributes = payload.get("attributes", {})
    if not isinstance(attributes, Mapping):
        raise _bad_shape(what, 'its "attributes" is not a JSON object')
    return HaState(
        entity_id=entity_id,
        state=state,
        attributes=MappingProxyType(dict(attributes)),
        last_changed=_optional_str(payload, "last_changed", what=what),
        last_updated=_optional_str(payload, "last_updated", what=what),
    )


def _parse_status(payload: object, *, what: str) -> HaApiStatus:
    """Reduce the ``/api/`` answer to its message.

    :param payload: decoded JSON for the API status.
    :param what: short description used in the error message.
    :raises HaBadResponseError: if the payload carries no ``message`` string.
    """
    if not isinstance(payload, Mapping):
        raise _bad_shape(what, "it is not a JSON object")
    message = payload.get("message")
    if not isinstance(message, str):
        raise _bad_shape(what, 'it has no "message" string')
    return HaApiStatus(message=message)


def _parse_tts_url(payload: object, *, what: str) -> HaTtsUrl:
    """Reduce the ``/api/tts_get_url`` answer to the URL and the relative path.

    :param payload: decoded JSON for the TTS answer.
    :param what: short description used in the error message.
    :raises HaBadResponseError: if the payload carries no ``url`` string.
    """
    if not isinstance(payload, Mapping):
        raise _bad_shape(what, "it is not a JSON object")
    url = payload.get("url")
    if not isinstance(url, str):
        raise _bad_shape(what, 'it has no "url" string')
    return HaTtsUrl(url=url, path=_optional_str(payload, "path", what=what))


def _clean(text: str) -> str:
    """Collapse whitespace, scrub registered secrets, then clip to ``_BODY_CLIP``.

    Scrubbing happens before the clip so a credential that straddles the
    200-character boundary is redacted instead of being cut in half past the rules,
    which is why callers hand this function a window wider than ``_BODY_CLIP``.

    :param text: text taken from a response body, about to enter an error message.
    """
    return SECRET_SCRUBBER.scrub(" ".join(text.split()))[:_BODY_CLIP]


def _detail_from(response: httpx.Response) -> str:
    """HA's own ``{"message": ...}`` when the body is small JSON, else cleaned raw text.

    Deliberate deviation from ``api/errors.py``'s "never echo a submitted value" rule:
    for a broken jinja template, Home Assistant's own message is the only actionable
    text the user can see, so it is included — through :func:`_clean`, which scrubs
    registered secrets first and clips afterwards.

    The plain-text branch hands :func:`_clean` a wide window — not the 200-character
    clip — so a credential straddling that boundary is still scrubbed whole before the
    clip is applied, and decoding a slice taken at ``_BODY_JSON_LIMIT`` never splits a
    UTF-8 sequence. The slice keeps the memory bound; the JSON branch already passes
    the whole ``message``.

    :param response: the non-2xx answer to describe.
    """
    content = response.content
    if len(content) <= _BODY_JSON_LIMIT:
        try:
            payload = json.loads(content)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            message = payload.get("message")
            if isinstance(message, str):
                return _clean(message)
    return _clean(content[:_BODY_JSON_LIMIT].decode("utf-8", errors="replace"))


def _http_phrase(status_code: int, detail: str) -> str:
    """Render ``HTTP <code>`` with Home Assistant's detail appended when there is one.

    :param status_code: status code of the answer.
    :param detail: already cleaned detail text, possibly empty.
    """
    return f"HTTP {status_code}: {detail}" if detail else f"HTTP {status_code}"


class HomeAssistantClient:
    """Call the Home Assistant REST API and translate every failure into a ``HaError``.

    Build one per settings generation (see the module docstring) and close it with
    :meth:`aclose` or by using it as an async context manager.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        client: httpx.AsyncClient | None = None,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
    ) -> None:
        """Validate the settings and prepare a lazily created HTTP client.

        :param base_url: the stored ``ha.base_url``; normalised before use.
        :param token: the stored ``ha.token``; never logged and never re-exposed.
        :param client: an ``httpx.AsyncClient`` to borrow, for tests. When omitted one
            is created on first use and closed by :meth:`aclose`.
        :param connect_timeout_s: seconds allowed to open a connection.
        :param read_timeout_s: seconds allowed to wait for the answer.
        :param retry_delay_s: seconds slept before the single retry.
        :raises HaConfigurationError: if a setting or a timeout cannot be used.
        """
        self._base_url = normalise_base_url(base_url)
        stripped_token = token.strip()
        if not stripped_token:
            raise HaConfigurationError(
                "no Home Assistant token is configured — add a long-lived access token "
                "to ha.token in Settings"
            )
        self._token = stripped_token
        if connect_timeout_s <= 0:
            raise HaConfigurationError(
                f"connect_timeout_s must be greater than zero; got {connect_timeout_s:g}"
            )
        if read_timeout_s <= 0:
            raise HaConfigurationError(
                f"read_timeout_s must be greater than zero; got {read_timeout_s:g}"
            )
        if retry_delay_s < 0:
            raise HaConfigurationError(f"retry_delay_s must not be negative; got {retry_delay_s:g}")
        self._connect_timeout_s = connect_timeout_s
        self._read_timeout_s = read_timeout_s
        self._retry_delay_s = retry_delay_s
        self._client = client
        self._owns_client = client is None
        self._closed = False

    def __repr__(self) -> str:
        """Return a representation that cannot contain the token."""
        return f"<{type(self).__name__} base_url={self._base_url!r} token={_REDACTED_TOKEN!r}>"

    @property
    def base_url(self) -> str:
        """Return the normalised base URL this client was built with."""
        return self._base_url

    async def __aenter__(self) -> Self:
        """Return this client, unchanged."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the client, if this object created it.

        :param exc_info: the exception triple, unused.
        """
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client and make this object unusable.

        A client passed to the constructor was borrowed, so its socket is left
        open, but this object still refuses further calls: after :meth:`aclose`
        every method raises :class:`HaConfigurationError`. That covers a borrowed
        client too, so a stale object fails loudly instead of doing I/O with the
        settings it was built from.
        """
        self._closed = True
        client = self._client
        self._client = None
        if self._owns_client and client is not None:
            await client.aclose()

    @classmethod
    async def from_store(
        cls, store: SettingsReader, *, client: httpx.AsyncClient | None = None
    ) -> HomeAssistantClient:
        """Build a client from the stored settings.

        This reads the settings through the same accessor the rest of the
        application uses. ``get_setting`` rather than the redacting
        ``all_settings`` is correct here because the client genuinely needs the
        secret to build the ``Authorization`` header.

        The result is frozen at this settings generation, so after a settings
        change, build a new client instead of reusing this one.

        :param store: any object satisfying :class:`SettingsReader`, normally
            ``ConfigStore``.
        :param client: an ``httpx.AsyncClient`` to borrow, for tests.
        :raises HaConfigurationError: if either stored value is not a string.
        """
        base_url = await store.get_setting("ha.base_url")
        if not isinstance(base_url, str):
            raise HaConfigurationError(
                "the ha.base_url setting is not a string — set a Home Assistant URL in Settings"
            )
        token = await store.get_setting("ha.token")
        if not isinstance(token, str):
            raise HaConfigurationError(
                "the ha.token setting is not a string — add a long-lived access token in Settings"
            )
        return cls(base_url=base_url, token=token, client=client)

    async def api_status(self, *, timeout_s: float | None = None) -> HaApiStatus:
        """Return Home Assistant's own API status message.

        The trailing slash matters: Home Assistant answers ``404`` for ``/api``
        without it, which is why the base URL is normalised rather than trusted.

        :param timeout_s: total budget for this call, retry included.
        :raises HaError: any failure, already classified.
        """
        what = "the API status"
        response = await self._request("GET", "/api/", what=what, timeout_s=timeout_s)
        return _parse_status(_decode_json(response, what=what), what=what)

    async def states(self, *, timeout_s: float | None = None) -> list[HaState]:
        """Return every entity state Home Assistant knows about.

        :param timeout_s: total budget for this call, retry included.
        :raises HaError: any failure, already classified.
        """
        what = "the entity list"
        response = await self._request("GET", "/api/states", what=what, timeout_s=timeout_s)
        payload = _decode_json(response, what=what)
        if not isinstance(payload, list):
            raise _bad_shape(what, "it is not a JSON array")
        return [_parse_state(entry, what="an entry in the entity list") for entry in payload]

    async def state(self, entity_id: str, *, timeout_s: float | None = None) -> HaState:
        """Return one entity state.

        The entity id is percent-quoted into the path.

        :param entity_id: for example ``"sensor.front_door"``.
        :param timeout_s: total budget for this call, retry included.
        :raises HaError: any failure, already classified.
        """
        what = f"entity {entity_id!r}"
        path = f"/api/states/{quote(entity_id, safe='')}"
        response = await self._request("GET", path, what=what, timeout_s=timeout_s)
        return _parse_state(_decode_json(response, what=what), what=what)

    async def render_template(
        self,
        template: str,
        *,
        variables: Mapping[str, object] | None = None,
        timeout_s: float | None = None,
    ) -> str:
        """Render a jinja template through ``POST /api/template``.

        The answer is plain text, not JSON, so ``response.text`` is used as-is. A
        ``200`` with an empty body is a *valid empty render* and returns ``""``:
        design 04 §6 relies on ``trigger.*`` phrases rendering empty, so an empty
        body is never treated as malformed.

        The endpoint needs an **admin** token, so a correct but non-admin token
        produces a ``401`` here. The returned text is Home Assistant's output
        verbatim; the caller applies design 04 §6 step 3 (whitespace collapse,
        500-character cap, control-character removal).

        :param template: the jinja template to render.
        :param variables: optional variables made available to the template.
        :param timeout_s: total budget for this call. There is no retry: a POST may
            already have run on the Home Assistant side.
        :raises HaError: any failure, already classified.
        """
        body: dict[str, object] = {"template": template}
        if variables is not None:
            body["variables"] = dict(variables)
        response = await self._request(
            "POST", "/api/template", what="the template", body=body, timeout_s=timeout_s
        )
        return response.text

    async def tts_get_url(
        self,
        *,
        engine_id: str,
        message: str,
        language: str | None = None,
        options: Mapping[str, object] | None = None,
        timeout_s: float | None = None,
    ) -> HaTtsUrl:
        """Ask Home Assistant to synthesise speech and return where it put the audio.

        ``options`` is where ``{"preferred_format": "wav"}`` goes (design 04 §3);
        there is no separate parameter for it. There is no retry: this POST makes
        Home Assistant write a file, so a timed-out call may already have run.

        :param engine_id: the Home Assistant TTS entity id.
        :param message: the text to speak.
        :param language: optional language tag.
        :param options: optional extra options passed to the engine.
        :param timeout_s: total budget for this call.
        :raises HaError: any failure, already classified.
        """
        what = "the text-to-speech URL"
        body: dict[str, object] = {"engine_id": engine_id, "message": message}
        if language is not None:
            body["language"] = language
        if options is not None:
            body["options"] = dict(options)
        response = await self._request(
            "POST", "/api/tts_get_url", what=what, body=body, timeout_s=timeout_s
        )
        return _parse_tts_url(_decode_json(response, what=what), what=what)

    def _url(self, path: str) -> str:
        """Join *path* onto the normalised base URL.

        :param path: endpoint path starting with ``/``.
        """
        return f"{self._base_url}{path}"

    def _headers(self) -> dict[str, str]:
        """Build the request headers, placing the token only in ``Authorization``."""
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _http(self) -> httpx.AsyncClient:
        """Return the HTTP client, creating one on first use.

        Redirects are not followed: a cross-origin or port-changing redirect makes
        httpx drop ``Authorization``, which would turn a wrong base URL into a
        confusing ``401`` instead of the configuration error it really is.

        :raises HaConfigurationError: if this client was already closed.
        """
        if self._closed:
            raise HaConfigurationError(
                "this Home Assistant client was closed — build a new one after a settings change"
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._default_timeout(),
                follow_redirects=False,
            )
            self._owns_client = True
        return self._client

    def _default_timeout(self) -> httpx.Timeout:
        """Return the phased httpx timeout used when no per-call budget is given."""
        return httpx.Timeout(
            connect=self._connect_timeout_s,
            read=self._read_timeout_s,
            write=self._read_timeout_s,
            pool=self._connect_timeout_s,
        )

    def _request_timeout(self, budget: float | None) -> httpx.Timeout:
        """Return the httpx timeout for one call.

        :param budget: total per-call budget, or ``None`` to use the phased default.
        """
        if budget is None:
            return self._default_timeout()
        return httpx.Timeout(budget)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        what: str,
        body: Mapping[str, object] | None = None,
        timeout_s: float | None = None,
    ) -> httpx.Response:
        """Send one request, retrying once when the rule allows it.

        :param method: HTTP method, compared against :data:`RETRYABLE_METHODS`.
        :param path: endpoint path, joined onto the base URL.
        :param what: short description used in error messages.
        :param body: JSON body for a ``POST``.
        :param timeout_s: total budget for the whole call, retry included.
        :raises HaError: any failure, already classified.
        """
        if timeout_s is not None and timeout_s <= 0:
            raise HaConfigurationError(f"timeout_s must be greater than zero; got {timeout_s:g}")
        url = self._url(path)
        timeout = self._request_timeout(timeout_s)
        attempts = 2 if method in RETRYABLE_METHODS else 1
        if timeout_s is None:
            return await self._attempt(
                method,
                url,
                what=what,
                body=body,
                timeout=timeout,
                attempts=attempts,
                budget=None,
            )
        # The outer bound counts the retry too. ``httpx.TimeoutException`` is a
        # ``TransportError``, not a builtin ``TimeoutError``, so both branches of the
        # translation are needed and this one is caught first.
        try:
            async with asyncio.timeout(timeout_s):
                return await self._attempt(
                    method,
                    url,
                    what=what,
                    body=body,
                    timeout=timeout,
                    attempts=attempts,
                    budget=timeout_s,
                )
        except TimeoutError as exc:
            raise HaTimeoutError(
                f"Home Assistant did not answer within {timeout_s:g} s — check that "
                f"{self._base_url} is reachable and that Home Assistant is healthy",
                timeout_s=timeout_s,
            ) from exc

    async def _attempt(
        self,
        method: str,
        url: str,
        *,
        what: str,
        body: Mapping[str, object] | None,
        timeout: httpx.Timeout,
        attempts: int,
        budget: float | None,
    ) -> httpx.Response:
        """Run the attempt loop: send, classify, and retry once when allowed.

        :param method: HTTP method of the call.
        :param url: absolute URL of the call.
        :param what: short description used in error messages.
        :param body: JSON body for a ``POST``.
        :param timeout: httpx timeout for one attempt.
        :param attempts: ``2`` for a retryable method, ``1`` otherwise.
        :param budget: total per-call budget, or ``None`` when there is none.
        :raises HaError: any failure, already classified.
        """
        error: HaError = HaConnectionError(f"the request to {url} was never sent")
        cause: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self._send_once(method, url, body=body, timeout=timeout)
            except httpx.HTTPError as exc:
                error = self._translate(exc, budget=budget)
                cause = exc
            else:
                try:
                    self._raise_for_status(response, what=what)
                except HaError as exc:
                    error = exc
                    cause = None
                else:
                    return response
            if not error.transient or attempt == attempts:
                raise error from cause
            _LOGGER.debug(
                "HA %s %s failed transiently (%s); retrying in %.2f s",
                method,
                url,
                type(error).__name__,
                self._retry_delay_s,
            )
            await asyncio.sleep(self._retry_delay_s)
        raise AssertionError("the retry loop returned without a response")

    async def _send_once(
        self,
        method: str,
        url: str,
        *,
        body: Mapping[str, object] | None,
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        """Send exactly one request, with no retry and no error translation.

        :param method: HTTP method of the call.
        :param url: absolute URL of the call.
        :param body: JSON body for a ``POST``; ``None`` sends no body.
        :param timeout: httpx timeout for this attempt.
        """
        client = self._http()
        headers = self._headers()
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("HA request %s %s headers=%s", method, url, redact_headers(headers))
        return await client.request(
            method,
            url,
            headers=headers,
            json=dict(body) if body is not None else None,
            timeout=timeout,
        )

    def _raise_for_status(self, response: httpx.Response, *, what: str) -> None:
        """Raise the matching typed error for a non-2xx answer.

        Every sentence names *what* was requested and carries Home Assistant's own
        clipped detail, because that text is the only thing the user can act on.

        :param response: an answer from Home Assistant.
        :param what: short description of the thing that was requested.
        :raises HaConfigurationError: for a redirect; this exists for a misconfigured
            reverse proxy, because real Home Assistant never redirects these endpoints.
        :raises HaAuthError: for ``401`` and ``403``.
        :raises HaNotFoundError: for ``404``.
        :raises HaBadRequestError: for any other ``4xx``.
        :raises HaServerError: for any ``5xx``.
        :raises HaBadResponseError: for anything else.
        """
        status = response.status_code
        if 200 <= status < 300:
            return
        detail = _detail_from(response)
        http = _http_phrase(status, detail)
        if 300 <= status < 400:
            location = _clean(response.headers.get("location", "")) or "an address it did not name"
            raise HaConfigurationError(
                f"Home Assistant answered {response.request.method} {response.request.url} "
                f"with a redirect to {location} ({http}) — set ha.base_url to that "
                "address; a redirect is not followed because the authorisation header "
                "would be dropped"
            )
        if status in {401, 403}:
            raise HaAuthError(
                f"Home Assistant rejected the token ({http}) — the long-lived token in "
                "ha.token is wrong, expired, revoked, or belongs to a user without admin "
                "rights; the template API needs an admin token",
                status_code=status,
                detail=detail,
            )
        if status == 404:
            raise HaNotFoundError(
                f"Home Assistant has no {what} ({http}) — check the name in Settings",
                status_code=status,
                detail=detail,
            )
        if 400 <= status < 500:
            raise HaBadRequestError(
                f"Home Assistant refused {what} ({http}) — check the request; a 400 from "
                "the template endpoint carries Home Assistant's own explanation of the "
                "bad template",
                status_code=status,
                detail=detail,
            )
        if status >= 500:
            raise HaServerError(
                f"Home Assistant failed to serve {what} ({http}) — this is a fault on the "
                "Home Assistant side; check its log",
                status_code=status,
                detail=detail,
            )
        raise HaBadResponseError(
            f"Home Assistant answered {what} with an unexpected HTTP status ({http})"
        )

    def _translate(self, exc: httpx.HTTPError, *, budget: float | None = None) -> HaError:
        """Map a transport error to the class the caller catches.

        The cause is kept by the ``raise ... from exc`` at the call site; the leak
        test proves no token travels with it.

        :param exc: the error httpx raised.
        :param budget: total per-call budget, when the caller set one, so a timeout
            message names the number the caller actually asked for.
        """
        if isinstance(exc, httpx.InvalidURL):
            return HaConfigurationError(f"ha.base_url is not a usable URL — {_clean(str(exc))}")
        if isinstance(exc, httpx.UnsupportedProtocol):
            return HaConfigurationError(
                f"ha.base_url must be an http:// or https:// URL — {_clean(str(exc))}"
            )
        if isinstance(exc, httpx.TimeoutException):
            seconds = self._read_timeout_s if budget is None else budget
            return HaTimeoutError(
                f"Home Assistant did not answer within {seconds:g} s — check that "
                f"{self._base_url} is reachable and that Home Assistant is healthy",
                timeout_s=seconds,
            )
        if isinstance(exc, httpx.ConnectError):
            return HaConnectionError(
                f"cannot connect to Home Assistant at {self._base_url} — the host or port "
                f"in ha.base_url may be wrong, or the server may be down "
                f"({_clean(str(exc))})"
            )
        return HaConnectionError(
            f"the request to {self._base_url} failed: {_clean(str(exc)) or type(exc).__name__}"
        )


def _decode_json(response: httpx.Response, *, what: str) -> object:
    """Decode a 2xx response body as JSON.

    :param response: the successful answer to decode.
    :param what: short description used in the error message.
    :raises HaBadResponseError: if the body is not valid JSON.
    """
    try:
        return json.loads(response.content)
    except ValueError as exc:
        raise _bad_shape(what, f"it is not valid JSON ({_clean(str(exc))})") from exc
