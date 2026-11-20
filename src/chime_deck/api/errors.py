"""Error envelope and exception handlers for the HTTP API.

Every error response has the same shape, so the frontend can rely on one
contract::

    {"error": {"code": "invalid_setting", "message": "…", "detail": {...}}}

``code`` is a stable machine-readable :class:`ErrorCode` member, ``message`` is
human text safe to show in the UI, and ``detail`` carries structured context
(which field failed, which id was missing). Error models are the contract the
API tests validate with ``ErrorEnvelope.model_validate``.

One code set covers every route, including the renderer role: the renderer reuses
this :class:`ErrorCode` enum rather than minting a second one, so one envelope
contract (and one client-side parser) describes every endpoint under ``/api``,
whether this process renders speech or asks another one to.

No handler ever echoes a submitted value: validation errors are stripped of
pydantic's ``input``/``ctx`` (which do contain the submitted value, and a secret
setting's value is exactly what must never travel back), and the store's own
messages are written to avoid values too.

Owning ticket: HSD-91z1; the renderer codes, and the response headers
:class:`ApiError` carries, were added by HSD-5de8.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..store import (
    ConfigStoreError,
    NotFoundError,
    StoreConflict,
    StoreNotConnected,
    UnknownSettingKey,
    WriteError,
)
from ..store import (
    ValidationError as StoreValidationError,
)

__all__ = [
    "ApiError",
    "ErrorCode",
    "ErrorDetail",
    "ErrorEnvelope",
    "error_response",
    "install_error_handlers",
]

_LOGGER = logging.getLogger(__name__)

_INTERNAL_ERROR_MESSAGE: Final = "the server hit an unexpected error; check the log for details"

_LOCATION_PREFIXES: Final = frozenset({"body", "query", "path", "header", "cookie"})


class ErrorCode(StrEnum):
    """Machine-readable reason an API request failed.

    This is the single code set for the whole HTTP surface: the renderer role
    reuses it instead of minting a second one, so one envelope contract covers
    every route, the local ones and the renderer's ``POST /api/tts/render``
    alike.
    """

    VALIDATION_ERROR = "validation_error"
    UNKNOWN_SETTING = "unknown_setting"
    INVALID_SETTING = "invalid_setting"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONFLICT = "conflict"
    STORE_UNAVAILABLE = "store_unavailable"
    STORE_WRITE_FAILED = "store_write_failed"
    INVALID_URL = "invalid_url"
    SOUND_TOO_LARGE = "sound_too_large"
    SOUND_NOT_AUDIO = "sound_not_audio"
    SOUND_FORMAT_UNSUPPORTED = "sound_format_unsupported"
    SOUND_MISSING = "sound_missing"  # the row exists but its file is not in the library
    DOWNLOAD_FAILED = "download_failed"
    VOICE_UNAVAILABLE = "voice_unavailable"
    VOICE_TOO_LARGE = "voice_too_large"
    UNAUTHORIZED = "unauthorized"  # missing or wrong bearer token, 401/403
    RATE_LIMITED = "rate_limited"  # per-IP rate limit, 429
    QUEUE_FULL = "queue_full"  # bounded render queue full, 429
    ENGINE_UNAVAILABLE = "engine_unavailable"  # the renderer has no usable engine, 503
    HTTP_ERROR = "http_error"
    INTERNAL_ERROR = "internal_error"


class ErrorDetail(BaseModel):
    """Body of a failed response: a stable code, human text and structured context."""

    code: ErrorCode
    message: str
    detail: dict[str, object] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    """The single error body shape every failing request returns."""

    error: ErrorDetail


class ApiError(Exception):
    """Raised by a route to answer with a specific status, code and message."""

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        detail: dict[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Record the response the route wants to produce.

        :param status_code: HTTP status to send.
        :param code: machine-readable error code for the body.
        :param message: human readable text; must never contain a secret value.
        :param detail: structured context, empty when there is none.
        :param headers: extra response headers the route must send, such as
            ``WWW-Authenticate`` on a 401 or ``Retry-After`` on a 429; ``None``
            sends none. They are folded into the envelope by the handler.
        """
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.headers: Mapping[str, str] = headers if headers is not None else {}
        super().__init__(message)


def error_response(
    status: int,
    code: ErrorCode,
    message: str,
    detail: dict[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build the JSON error response shared by every handler.

    Public because one caller answers before the handlers can: the shared-token
    middleware sits outside the application and its exception handlers, so it
    builds the same envelope itself (:mod:`chime_deck.auth`) rather than
    minting a second error shape.

    :param status: HTTP status code.
    :param code: machine-readable error code.
    :param message: human readable text.
    :param detail: structured context, omitted from the body when empty.
    :param headers: extra response headers, such as ``Allow`` on 405.
    """
    return JSONResponse(
        {"error": {"code": code.value, "message": message, "detail": detail or {}}},
        status_code=status,
        headers=dict(headers) if headers is not None else None,
    )


def _field_name(loc: Sequence[object]) -> str:
    """Turn a validation error location into the field name a user would recognise.

    :param loc: pydantic error location, e.g. ``("body", "audio.volume")``.
    """
    parts = [str(part) for part in loc]
    if len(parts) > 1 and parts[0] in _LOCATION_PREFIXES:
        parts = parts[1:]
    return ".".join(parts) if parts else "body"


def _not_found_detail(exc: ConfigStoreError) -> dict[str, object]:
    """Extract the identifier of the row a :class:`NotFoundError` reports, when it has one.

    :param exc: store error raised for a missing row.
    """
    for attribute in ("entity", "button", "sound"):
        value = getattr(exc, attribute, None)
        if value is not None:
            return {"id": value}
    return {}


def install_error_handlers(app: FastAPI) -> None:
    """Register the API's exception handlers on *app*.

    The generic ``Exception`` handler only produces a clean 500 body: Starlette
    re-raises the exception after the response is sent, so a test that asserts on
    that body has to build its client with
    ``httpx.ASGITransport(app=app, raise_app_exceptions=False)``.

    :param app: application the handlers are registered on.
    """

    @app.exception_handler(ApiError)
    async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        """Answer with the status, code, message and headers the route asked for."""
        return error_response(exc.status_code, exc.code, exc.message, exc.detail, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Report a malformed request without echoing the submitted values."""
        # Only loc/type/msg are copied: "input" and "ctx" repeat the submitted
        # value, which for a secret setting must never come back to the browser.
        names: list[str] = []
        messages: list[str] = []
        fields: list[dict[str, object]] = []
        for error in exc.errors():
            loc = [str(part) for part in error.get("loc", ())]
            msg = str(error.get("msg", ""))
            names.append(_field_name(loc))
            messages.append(msg)
            fields.append({"loc": loc, "type": str(error.get("type", "")), "msg": msg})
        if names:
            message = f"the request field {names[0]!r} is not valid: {messages[0]}"
        else:
            message = "the request is not valid"
        return error_response(422, ErrorCode.VALIDATION_ERROR, message, {"fields": fields})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Wrap Starlette's HTTP errors, keeping headers such as ``Allow``."""
        if exc.status_code == 404:
            code = ErrorCode.NOT_FOUND
        elif exc.status_code == 405:
            code = ErrorCode.METHOD_NOT_ALLOWED
        else:
            code = ErrorCode.HTTP_ERROR
        # Starlette annotates ``detail`` as ``str`` internally, but FastAPI's
        # ``HTTPException`` accepts any object, so only echo it when it is text.
        detail: object = exc.detail
        if isinstance(detail, str):
            message = detail
        else:
            message = f"the request failed with HTTP {exc.status_code}"
        return error_response(exc.status_code, code, message, headers=exc.headers)

    async def _store_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        """Map a store error to the status code and code the API promises."""
        if isinstance(exc, UnknownSettingKey):
            return error_response(422, ErrorCode.UNKNOWN_SETTING, str(exc), {"field": exc.key})
        if isinstance(exc, StoreValidationError):
            field = getattr(exc, "key", None)
            return error_response(422, ErrorCode.INVALID_SETTING, str(exc), {"field": field})
        if isinstance(exc, NotFoundError):
            return error_response(404, ErrorCode.NOT_FOUND, str(exc), _not_found_detail(exc))
        if isinstance(exc, StoreConflict):
            slug = getattr(exc, "slug", None)
            detail = {"slug": slug} if slug is not None else {}
            return error_response(409, ErrorCode.CONFLICT, str(exc), detail)
        if isinstance(exc, StoreNotConnected):
            return error_response(503, ErrorCode.STORE_UNAVAILABLE, str(exc))
        # WriteError is the only registered class left; anything else escaping
        # the store is still a failed write from the caller's point of view.
        return error_response(500, ErrorCode.STORE_WRITE_FAILED, str(exc))

    for store_error in (
        UnknownSettingKey,
        StoreValidationError,
        NotFoundError,
        StoreConflict,
        StoreNotConnected,
        WriteError,
    ):
        app.add_exception_handler(store_error, _store_error_handler)

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, _exc: Exception) -> JSONResponse:
        """Answer with a generic 500 body; the traceback goes to the log only."""
        _LOGGER.exception("unhandled error serving %s %s", request.method, request.url.path)
        return error_response(500, ErrorCode.INTERNAL_ERROR, _INTERNAL_ERROR_MESSAGE)
