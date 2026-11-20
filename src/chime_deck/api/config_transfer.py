"""Configuration transfer API: download the whole config, upload it back.

``GET /api/config/export`` hands the operator one JSON file that restores an
installation, and ``POST /api/config/import`` accepts that file back. Both are
thin: the document's shape, its validation and the merge semantics live in
:mod:`chime_deck.store.transfer`, and this module only translates HTTP into
those two functions plus the two side effects an import needs.

``include_secrets=False`` is the default because the export is a file that lands
in a download folder: it ships redacted credentials, and the response carries
``Cache-Control: no-store`` either way since a body with ``true`` contains them.
The redaction is the store's (``all_settings(redact_secrets=...)``), never this
module's.

``ImportRequest.dry_run`` sits beside ``document`` rather than inside it on
purpose: the document is validated against the settings registry, so a key named
``dry_run`` inside it would either collide with a future setting of that name or
be rejected as unknown. The flag is a property of the *request*, not of the
configuration being restored, so it stays out.

An import is applied before the response is built, so the two follow-up steps are
part of the answer and never of the write: the sound library is rescanned,
because :func:`import_config` writes index rows and never the files, and the
secret scrubber is refreshed, because the document may have written
``ha.token``/``mqtt.password`` and :class:`~chime_deck.config.SecretScrubber`
says every writer of a secret must refresh it. A dry run does neither, because it
wrote nothing.

Owning ticket: HSD-l22f.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ..config import refresh_secret_scrubber
from ..store import ImportReport, ImportValidationError, export_config, import_config
from .errors import ApiError, ErrorCode

if TYPE_CHECKING:
    # ``app.py`` imports ``.api`` at module level, so a runtime import of
    # ``..app`` here would close the cycle (the same pattern as ``api/buttons.py``).
    from ..app import AppState

__all__ = [
    "EXPORT_FILENAME",
    "ImportReportResponse",
    "ImportRequest",
    "build_config_router",
]

_LOGGER = logging.getLogger(__name__)

EXPORT_FILENAME: Final = "chime-deck-config.json"
"""Name the browser saves an export under."""

_EXPORT_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Content-Disposition": f'attachment; filename="{EXPORT_FILENAME}"',
        "Cache-Control": "no-store",
    }
)
"""Headers every export carries: an attachment that no cache may keep.

``no-store`` is unconditional: with ``include_secrets=true`` the body carries
``ha.token`` and ``mqtt.password``, and a cached copy would outlive the request.
"""


class ImportRequest(BaseModel):
    """Body of ``POST /api/config/import``.

    ``dry_run`` is a request flag, not a document field, so it cannot collide
    with a settings key: it is validated, reported and then thrown away rather
    than handed to :func:`~chime_deck.store.transfer.import_config`.
    """

    dry_run: bool = False
    document: dict[str, object]


class ImportReportResponse(BaseModel):
    """What one import wrote, exactly as :class:`ImportReport` counts it."""

    dry_run: bool
    settings_written: int
    entities: int
    sounds: int
    buttons: int

    @classmethod
    def from_report(cls, report: ImportReport) -> ImportReportResponse:
        """Build the response body from an import report.

        :param report: counts returned by ``import_config``.
        """
        return cls(
            dry_run=report.dry_run,
            settings_written=report.settings_written,
            entities=report.entities,
            sounds=report.sounds,
            buttons=report.buttons,
        )


def _import_rejected(exc: ImportValidationError) -> ApiError:
    """Build the 422 that lists every problem the document had.

    :param exc: validation failure raised by ``import_config``.
    """
    return ApiError(
        422,
        ErrorCode.VALIDATION_ERROR,
        str(exc),
        {"problems": list(exc.problems)},
    )


def build_config_router(state: AppState) -> APIRouter:
    """Build the ``/api/config`` router bound to *state*.

    Handlers are closures over *state*, following the house style in
    :mod:`chime_deck.app`: no handler annotates ``AppState``, so FastAPI
    never has to resolve a forward reference at import time.

    :param state: live application state providing the store and the library.
    """
    router = APIRouter(prefix="/api/config", tags=["config"])

    @router.get("/export")
    async def export(include_secrets: bool = False) -> Response:
        """Return the whole configuration as one downloadable JSON document.

        :param include_secrets: include ``ha.token`` and ``mqtt.password``
            verbatim instead of redacting them.
        """
        document = await export_config(state.store, include_secrets=include_secrets)
        return JSONResponse(document, headers=_EXPORT_HEADERS)

    @router.post("/import")
    async def import_document(body: ImportRequest) -> ImportReportResponse:
        """Validate a document, then apply it unless ``dry_run`` is set.

        Validation happens before anything is written and reports every problem
        at once, so a rejected import leaves the store exactly as it was.

        :param body: the document and whether to apply it or only check it.
        :raises ApiError: 422 listing the problems the document has.
        """
        try:
            report = await import_config(state.store, body.document, dry_run=body.dry_run)
        except ImportValidationError as exc:
            raise _import_rejected(exc) from exc
        if not body.dry_run:
            # The store wrote index rows only, never sound files, so the library
            # has to be re-indexed before the new rows can be played. A failure
            # here must not fail an import that already committed, hence the
            # warning rather than a raise.
            try:
                await state.sounds.scan()
            except Exception as exc:
                _LOGGER.warning(
                    "import: the sound library could not be rescanned: %s: %s",
                    type(exc).__name__,
                    exc,
                )
            # The document may have written a secret setting, and nothing else
            # refreshes the registry, so a token that arrived with the import
            # would otherwise reach the log verbatim.
            await refresh_secret_scrubber(state.store)
        return ImportReportResponse.from_report(report)

    return router
