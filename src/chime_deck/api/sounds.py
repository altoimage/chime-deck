"""Sounds API: list the library, rescan the folder, download a URL, rename, preview and delete.

These routes are deliberately thin. They translate HTTP into calls on
:class:`~chime_deck.sounds.SoundLibrary` and
:class:`~chime_deck.sounds.SoundDownloader` — the same objects the
application lifespan uses — so the API has no scanning, naming or download
logic of its own.

Canonical paths have no trailing slash: ``GET /api/sounds``, ``POST
/api/sounds/rescan``, ``POST /api/sounds/from_url``, ``PATCH /api/sounds/{id}``,
``GET /api/sounds/{id}/audio`` and ``DELETE /api/sounds/{id}``.

Every rejected submitted value is a **422** (``Code`` says why); there is no 400
in this API. The sound errors map like this:

===============================  ==============================  ================
sound error                      HTTP status                     envelope code
===============================  ==============================  ================
``INVALID_URL``                  422                              ``INVALID_URL``
``INVALID_NAME``                 422                              ``VALIDATION_ERROR``
``SOUND_TOO_LARGE``              413                              ``SOUND_TOO_LARGE``
``SOUND_NOT_AUDIO``              415                              ``SOUND_NOT_AUDIO``
``SOUND_FORMAT_UNSUPPORTED``     415                              ``SOUND_FORMAT_UNSUPPORTED``
``DOWNLOAD_FAILED``              502                              ``DOWNLOAD_FAILED``
===============================  ==============================  ================

Name-collision exhaustion (a name and all its ``-N`` variants are taken) reuses
409 ``CONFLICT``, the same status and code as a store conflict; the message
disambiguates the two by telling the user to send a different name.

``created=false`` from ``from_url`` means no new row was created. Either the
identical bytes were already inside the library (nothing was rewritten or
announced), or the request's own URL already had a row and its file was
refreshed because the bytes changed; the response carries the row with its new
``sha256``/``bytes`` in that case. A no-op that changes nothing emits **no** hub
event.

``PATCH /api/sounds/{id}`` (rename) and ``GET /api/sounds/{id}/audio``
(preview) were added by HSD-w9eq. The preview is served under ``/api/``
deliberately, so ``_is_reserved_path`` keeps it away from ``spa_fallback``;
design 05 §3 spells it ``/sounds/{id}/audio`` without the ``/api`` and that is
an erratum. ``DELETE /api/sounds/{id}`` lands here instead, because HSD-1drv's
acceptance criteria require the affected-buttons warning: without
``?confirm=true`` a referenced sound is a **409** listing every button that uses
it, and with ``?confirm=true`` those buttons are **deleted** with the sound (that
is what ``delete_sound(force=True)`` does — it does not disable them), so the
response returns the same list. Deleting a sound removes its index row; the file
on disk is never touched, because it belongs to the library.

Messages are whole actionable sentences and never echo the submitted URL: it may
carry credentials or fragments the caller did not intend to repeat.

Owning ticket: HSD-cwvc; the delete route was added by HSD-1drv, the rename and
preview routes by HSD-w9eq.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, NoReturn

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..sounds import MAX_NAME_STEM, SoundDownloadError, SoundErrorCode, sound_file_path
from ..store import SoundInUseError, SoundUpdate
from .errors import ApiError, ErrorCode

if TYPE_CHECKING:
    # ``app.py`` imports ``.api`` at module level, so a runtime import of
    # ``..app`` here would close the cycle (the same pattern as ``api/buttons.py``).
    from ..app import AppState
    from ..store import Button, Sound

__all__ = [
    "AffectedButtonResponse",
    "DeleteSoundResponse",
    "FromUrlRequest",
    "FromUrlResponse",
    "RenameSoundRequest",
    "RescanResponse",
    "SoundResponse",
    "build_sounds_router",
]

_ERROR_MAP: Final[Mapping[SoundErrorCode, tuple[int, ErrorCode]]] = MappingProxyType(
    {
        SoundErrorCode.INVALID_URL: (422, ErrorCode.INVALID_URL),
        SoundErrorCode.INVALID_NAME: (422, ErrorCode.VALIDATION_ERROR),
        SoundErrorCode.SOUND_TOO_LARGE: (413, ErrorCode.SOUND_TOO_LARGE),
        SoundErrorCode.SOUND_NOT_AUDIO: (415, ErrorCode.SOUND_NOT_AUDIO),
        SoundErrorCode.SOUND_FORMAT_UNSUPPORTED: (415, ErrorCode.SOUND_FORMAT_UNSUPPORTED),
        SoundErrorCode.DOWNLOAD_FAILED: (502, ErrorCode.DOWNLOAD_FAILED),
    }
)
"""Status and envelope code for each sound error; unknown codes fail as a download."""

_NAME_EXHAUSTED_REASON: Final = "name_exhausted"
"""``detail`` marker for a name whose every ``-N`` variant was taken."""

_CONTENT_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }
)
"""Media type served for each library extension; anything else is octet-stream."""

_PREVIEW_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
)
"""Headers every preview response carries.

``Content-Disposition`` is stated here instead of relying on ``FileResponse``'s
``content_disposition_type``: Starlette only emits that header when a
``filename=`` is passed, and no preview response may carry a filesystem name.
"""


class SoundResponse(BaseModel):
    """One indexed sound, in the shape the UI consumes."""

    id: int
    name: str
    source_type: str
    path: str | None
    url: str | None
    sha256: str | None
    format: str | None
    duration_ms: int | None
    bytes: int | None
    missing: bool
    created_at: str

    @classmethod
    def from_sound(cls, sound: Sound) -> SoundResponse:
        """Build the response body from a stored sound row.

        :param sound: row returned by the store.
        """
        return cls(
            id=sound.id,
            name=sound.name,
            source_type=sound.source_type,
            path=sound.path,
            url=sound.url,
            sha256=sound.sha256,
            format=sound.format,
            duration_ms=sound.duration_ms,
            bytes=sound.bytes,
            missing=sound.missing,
            created_at=sound.created_at,
        )


class RescanResponse(BaseModel):
    """What one folder scan changed, exactly as :class:`ScanReport` counts it."""

    added: int
    removed: int
    missing: int


class FromUrlRequest(BaseModel):
    """Body of a URL download."""

    url: str
    name: str | None = None


class FromUrlResponse(BaseModel):
    """Outcome of a URL download.

    ``created=false`` means no new row was created: either the identical bytes
    were already present (nothing changed) or this URL's existing row and file
    were refreshed because its bytes changed. Compare ``sha256`` with any value
    held before the call to tell the two apart.
    """

    created: bool
    sound: SoundResponse


class AffectedButtonResponse(BaseModel):
    """The part of a button a sound deletion has to name."""

    id: int
    name: str
    slug: str

    @classmethod
    def list_from_buttons(cls, buttons: Iterable[Button]) -> list[AffectedButtonResponse]:
        """Build one entry per button that references the sound.

        :param buttons: buttons returned by the store.
        """
        return [cls(id=button.id, name=button.name, slug=button.slug) for button in buttons]


class RenameSoundRequest(BaseModel):
    """Body of a sound rename; the only field a caller may change."""

    name: str


class DeleteSoundResponse(BaseModel):
    """What deleting one sound did to the buttons that used it."""

    affected_buttons: list[AffectedButtonResponse]


def _sound_in_use(sound_id: int, buttons: Iterable[Button]) -> ApiError:
    """Build the 409 that lists the buttons a sound deletion would take with it.

    The same envelope is produced by the pre-check and by a
    :class:`~chime_deck.store.SoundInUseError` raised by the store, so a
    concurrent edit cannot change the answer.

    :param sound_id: database id of the sound.
    :param buttons: buttons whose ``sound_id`` points at it.
    """
    affected = AffectedButtonResponse.list_from_buttons(buttons)
    count = len(affected)
    noun = "button" if count == 1 else "buttons"
    return ApiError(
        409,
        ErrorCode.CONFLICT,
        f"sound {sound_id} is used by {count} {noun} — resend with ?confirm=true to delete "
        f"the sound and those {noun}, or re-point the {noun} at another sound first; "
        "they are deleted, not disabled",
        {
            "affected_buttons": [entry.model_dump() for entry in affected],
            "requires": "confirm",
            "count": count,
        },
    )


def _sound_file_missing(sound_id: int) -> ApiError:
    """Build the 404 raised when a sound's file is not servable from the library.

    A row can outlive its file: the scanner only flags it ``missing``, it never
    deletes the row. The detail carries the id and nothing else — never the
    stored path, the resolved path or the library directory.

    :param sound_id: database id of the sound whose file is unavailable.
    """
    return ApiError(
        404,
        ErrorCode.SOUND_MISSING,
        "this sound's file is not in the sound library folder — put the file back or "
        "rescan the folder; it cannot be previewed or assigned to a button until then",
        {"sound_id": sound_id},
    )


def _raise_for_sound_error(exc: SoundDownloadError) -> NoReturn:
    """Translate one sound error into the API's status, code and message.

    :param exc: error raised by the sounds package.
    :raises ApiError: always.
    """
    if exc.detail.get("reason") == _NAME_EXHAUSTED_REASON:
        raise ApiError(409, ErrorCode.CONFLICT, exc.message, exc.detail) from None
    status, code = _ERROR_MAP.get(exc.code, (502, ErrorCode.DOWNLOAD_FAILED))
    raise ApiError(status, code, exc.message, exc.detail) from None


def build_sounds_router(state: AppState) -> APIRouter:
    """Build the ``/api/sounds`` router bound to *state*.

    Handlers are closures over *state*, following the house style in
    :mod:`chime_deck.app`: no handler annotates ``AppState``, so FastAPI
    never has to resolve a forward reference at import time.

    :param state: live application state providing the store, the library and
        the downloader.
    """
    router = APIRouter(prefix="/api/sounds", tags=["sounds"])

    @router.get("")
    async def list_sounds() -> list[SoundResponse]:
        """Return every indexed sound, in ``list_sounds()`` order (id ascending)."""
        return [SoundResponse.from_sound(sound) for sound in await state.store.list_sounds()]

    @router.post("/rescan")
    async def rescan() -> RescanResponse:
        """Scan the library folder and report what changed.

        ``added`` is the number of rows created by this scan; ``removed`` is the
        number of rows that transitioned to missing during it (a vanished file is
        flagged, never deleted); ``missing`` is the total number of rows flagged
        missing after the scan.
        """
        report = await state.sounds.scan()
        return RescanResponse(added=report.added, removed=report.removed, missing=report.missing)

    @router.post("/from_url")
    async def from_url(body: FromUrlRequest) -> FromUrlResponse:
        """Download one HTTP(S) audio file into the library.

        :param body: URL to download and an optional file name.
        :raises ApiError: if the URL, name, size or content type is rejected.
        """
        try:
            result = await state.downloader.download(body.url, name=body.name)
        except SoundDownloadError as exc:
            _raise_for_sound_error(exc)
        return FromUrlResponse(
            created=result.created,
            sound=SoundResponse.from_sound(result.sound),
        )

    @router.delete("/{sound_id}")
    async def delete_sound(sound_id: int, confirm: bool = False) -> DeleteSoundResponse:
        """Delete one sound, reporting the buttons that used it.

        Without ``confirm`` a referenced sound is refused with a 409 listing the
        buttons that would be deleted with it. With ``confirm`` the sound and
        those buttons are removed in one store transaction. The file on disk is
        never touched.

        :param sound_id: database id of the sound to delete.
        :param confirm: accept deleting the buttons that reference the sound.
        :raises ApiError: if the sound does not exist or is still in use.
        """
        await state.store.get_sound(sound_id)
        if not confirm:
            affected = await state.store.buttons_using_sound(sound_id)
            if affected:
                raise _sound_in_use(sound_id, affected)
        try:
            result = await state.store.delete_sound(sound_id, force=confirm)
        except SoundInUseError as exc:
            # The pre-check above is a race-prone optimisation; the store is the
            # authority, so its answer is turned into the identical 409.
            raise _sound_in_use(sound_id, exc.affected_buttons) from None
        return DeleteSoundResponse(
            affected_buttons=AffectedButtonResponse.list_from_buttons(result.affected_buttons)
        )

    @router.get("/{sound_id}/audio")
    async def preview_sound_audio(sound_id: int) -> FileResponse:
        """Stream one sound's file so the browser can preview it.

        The route streams with :class:`~fastapi.responses.FileResponse`, which
        answers ``accept-ranges: bytes`` and serves a ``Range`` request as a 206
        with ``Content-Range``, so an ``<audio>`` element can seek. A malformed
        or unsatisfiable ``Range`` is answered by Starlette (a plain-text 400 or
        416) rather than this API's JSON envelope; that is a deliberate, stated
        limitation, and only the missing or unreadable case is required to be a
        clean JSON envelope. The bytes on disk are trusted rather than the row's
        ``missing`` flag, so a file that came back before a rescan still
        previews.

        :param sound_id: database id of the sound to preview.
        :raises ApiError: if the sound has no servable file in the library.
        """
        sound = await state.store.get_sound(sound_id)
        path = sound_file_path(sound.path, state.sounds.directory)
        if path is None:
            raise _sound_file_missing(sound_id)
        try:
            stat_result = await asyncio.to_thread(path.stat)
        except OSError:
            # The file vanished between the containment check and the stat, or
            # cannot be statted: still a clean envelope, never a 500.
            raise _sound_file_missing(sound_id) from None
        return FileResponse(
            path,
            media_type=_CONTENT_TYPES.get(path.suffix.casefold(), "application/octet-stream"),
            stat_result=stat_result,
            headers=_PREVIEW_HEADERS,
            content_disposition_type="inline",
        )

    @router.patch("/{sound_id}")
    async def rename_sound(sound_id: int, body: RenameSoundRequest) -> SoundResponse:
        """Rename one sound; only its display name changes.

        Renaming is display metadata only: the stored file name, ``path``,
        ``format``, ``duration_ms``, ``bytes`` and ``missing`` are untouched and
        the file on disk is never renamed. Names are not unique (the schema's
        only unique index is on ``path``), so a duplicate name is a legal
        answer, not a conflict. Surrounding whitespace is stripped; a name that
        is only whitespace is refused rather than stored empty.

        :param sound_id: database id of the sound to rename.
        :param body: the requested display name.
        :raises ApiError: if the name is empty, has control characters or is
            longer than a file-name stem.
        """
        stripped = body.name.strip()
        if not stripped:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                "a sound needs a name — type the name you want to see in the sound library",
                {"field": "name", "reason": "empty"},
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in stripped):
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                "a sound name cannot contain control characters — use letters, digits, "
                "spaces, dots, dashes or underscores",
                {"field": "name", "reason": "control_characters"},
            )
        if len(stripped) > MAX_NAME_STEM:
            raise ApiError(
                422,
                ErrorCode.VALIDATION_ERROR,
                f"a sound name is limited to {MAX_NAME_STEM} characters — shorten it",
                {"field": "name", "reason": "too_long", "max": MAX_NAME_STEM},
            )
        await state.store.get_sound(sound_id)
        row = await state.store.update_sound(sound_id, SoundUpdate(name=stripped))
        return SoundResponse.from_sound(row)

    return router
