"""The icons API: the picker's names, their path data and the custom icon files.

Four routes make the icon picker possible without shipping 2.7 MB of JavaScript
or a webfont: ``GET /api/icons/search`` lists and filters the shipped Material
Design Icons set; ``GET /api/icon/{name}`` answers one icon's path data so a
tile whose ``mdi:`` reference is not in the UI's curated bundle can still
render; ``GET /api/icons/custom`` lists the user's own files in the configured
``data/icons`` directory for the editor; and ``GET /custom-icons/{filename}``
serves those files as bytes to the ``<img>`` tags that draw them. Home
Assistant has no public icon endpoint to borrow — ``Project_state/
glossary.md`` says so — so this build is the endpoint: the search routes read
the manifest generated from ``@mdi/svg`` (see
:mod:`chime_deck.icons.manifest`) and the custom routes read the configured
icon directory, nothing else. There is no live lookup here and no ``AppState``.
The shipped data is immutable and loaded once; the custom directory is checked
per request, because a missing one is a normal install and must be a clean
``404`` rather than a startup failure.

**Why the custom checks live at serve time.** A saved entity stores only a
*name* (``custom:cat.svg``); the bytes behind that name can be replaced on disk
at any moment, so a save-time content check is unenforceable. The content
invariant therefore belongs to the route that hands the file out: every SVG is
passed through :func:`chime_deck.icons.svg_safety.check_svg` immediately
before its bytes become a response, and the listing applies the same checks so
it can never advertise a file the server would refuse. A refused file travels
in ``skipped`` with a human reason, so the editor warns before the user picks
it. Nothing here caches, so a file dropped in while the process runs is visible
to the very next request.

**The search erratum.** Design 05 §3 lists ``GET /api/icons/search`` as a bare
array of ``{name, …}``. It answers with an envelope instead:
``{"version", "total", "matched", "icons"}``. ``version`` and ``total`` let the
picker say which icon set it is browsing, ``matched`` is the hit count *before*
the cap, so "50 of 137 — keep typing" is one response rather than a second
request, and each row carries its ``path`` so a result grid renders without one
request per row. ``GET /api/icon/{name}`` is not in design 05 at all; it exists
because a stored tile's icon may name anything in the set, not just what a search
happened to return.

**The custom-listing erratum.** Design 05 §3 names
``GET /custom-icons/{filename}`` but no listing route at all.
``GET /api/icons/custom`` is that erratum: it answers JSON, so it sits in the
selectors' family next to ``/api/icons/search``, while ``/custom-icons/…`` stays
the byte namespace an ``<img>`` uses.

Owning ticket: HSD-zyru; the custom-icon serving and listing were added by
HSD-i7pw.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from ..icons import (
    CUSTOM_ICON_EXTENSIONS,
    CUSTOM_PREFIX,
    MDI_PREFIX,
    MDI_VIEW_BOX,
    MdiManifest,
    custom_icon_path,
    load_mdi_manifest,
    normalise_icon_name,
)
from ..icons.svg_safety import check_svg
from .errors import ApiError, ErrorCode
from .limits import SEARCH_LIMIT_DEFAULT, SEARCH_LIMIT_MAX

__all__ = [
    "CUSTOM_ICON_MAX_BYTES",
    "CustomIconEntry",
    "CustomIconListResponse",
    "CustomIconSkip",
    "IconPathResponse",
    "IconSearchResponse",
    "IconSearchResult",
    "build_icons_router",
]

CUSTOM_ICON_MAX_BYTES: Final = 1024 * 1024
"""Largest custom icon served or advertised: one MiB.

A tile icon is a picture, not a bundle, so a file over this is refused rather
than buffered into memory and then into a response. The read below asks for one
byte more than this, which is how the cap is detected without trusting the
size a caller could race to change.
"""

_CONTENT_TYPES: Final = {".svg": "image/svg+xml", ".png": "image/png"}
"""Media type per custom-icon suffix; the suffix rule already limits the set."""

_HARDENING_HEADERS: Final = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}
"""Headers that stop a browser sniffing or running anything from a custom file.

``nosniff`` pins the response to the ``Content-Type`` above, so a ``.png``
holding HTML is never treated as HTML; the CSP's empty ``default-src`` plus
``sandbox`` is the second fence for the SVG case, where the document is
same-origin and would otherwise be able to run script.
"""

_NOT_FOUND_MESSAGE: Final = (
    "there is no usable custom icon at this path — a custom icon is an .svg or .png file "
    "directly inside the icons folder; add the file and reload the picker"
)
"""The 404 sentence shared by every unusable file name."""

_UNKNOWN_REASON: Final = "the custom icon reference is not a usable file"
"""Fallback detail when a rejection carries no sentence (it always should)."""

_READ_FAILED_REASON: Final = "the icon file could not be read"
"""Detail used when opening or reading a validated path fails at serve time."""

_EXACT_RANK: Final = 0
"""Sort rank of an icon whose name is exactly the search term."""

_PREFIX_RANK: Final = 1
"""Sort rank of an icon whose name starts with the search term."""

_SUBSTRING_RANK: Final = 2
"""Sort rank of a hit found anywhere else in the name."""


class IconSearchResult(BaseModel):
    """One picker hit: the reference an entity stores, and the data to draw it.

    ``name`` is the full ``mdi:<name>`` reference so the client can store it as
    an entity's icon without rebuilding the prefix, and ``path`` is the SVG path
    data in :data:`~chime_deck.icons.MDI_VIEW_BOX`'s coordinate system.
    """

    name: str
    path: str


class IconSearchResponse(BaseModel):
    """The picker's page of icons plus what it takes to keep paging.

    ``total`` is the size of the whole shipped set and ``matched`` is how many
    icons the term matched *before* ``limit`` was applied, so a truncated answer
    is self-describing: ``matched > len(icons)`` means "keep typing".
    """

    version: str
    total: int
    matched: int
    icons: list[IconSearchResult]


class IconPathResponse(BaseModel):
    """One icon's path data, ready to draw.

    ``name`` is echoed in its full ``mdi:<name>`` form whatever the caller sent,
    so ``door`` and ``mdi:door`` answer the same body and the client has one
    spelling to work with.
    """

    name: str
    path: str
    view_box: str = MDI_VIEW_BOX


class CustomIconEntry(BaseModel):
    """One usable custom icon, as the picker needs it.

    ``name`` is the file name inside the icons folder, ``reference`` is
    ``custom:<name>`` — the exact string an entity stores — and ``bytes`` is the
    file's size on disk, so the editor can show what the user is about to save
    without a second request.
    """

    name: str
    reference: str
    bytes: int


class CustomIconSkip(BaseModel):
    """One candidate file the server refuses, and the sentence saying why.

    A skipped file is never advertised as usable: the editor lists it so the
    user is told *before* picking that, say, a script-bearing SVG will be
    refused, instead of saving a reference that renders the fallback icon.
    """

    name: str
    reason: str


class CustomIconListResponse(BaseModel):
    """The custom-icon picker's complete answer.

    ``total`` is ``len(icons)`` — the number of files a tile may store — and
    ``skipped`` carries the files that were found but cannot be served. It is
    not part of ``total``: a skipped file is not an option.
    """

    total: int
    icons: list[CustomIconEntry]
    skipped: list[CustomIconSkip]


def _matches(name: str, q_folded: str) -> bool:
    """Whether *name* is a hit for the already-casefolded search term.

    An empty term matches everything: that is the picker's initial grid, not a
    search.

    :param name: bare icon name from the manifest.
    :param q_folded: ``q.strip().casefold()``, computed once per request.
    """
    if not q_folded:
        return True
    return q_folded in name


def _rank(name: str, q_folded: str) -> tuple[int, str]:
    """Return the picker's ordering key for a hit.

    An exact name first, then the names that start with the term, then any other
    substring hit, all alphabetically inside a rank — so typing ``door`` puts
    ``door`` on top and then ``door-closed``, ``door-open``, … in a stable order.

    :param name: bare icon name from the manifest.
    :param q_folded: ``q.strip().casefold()``, computed once per request.
    """
    if name == q_folded:
        rank = _EXACT_RANK
    elif name.startswith(q_folded):
        rank = _PREFIX_RANK
    else:
        rank = _SUBSTRING_RANK
    return (rank, name)


def _icon_not_found(name: str, manifest: MdiManifest) -> ApiError:
    """Build the 404 for a name the shipped icon set does not contain.

    The submitted value travels in ``detail``, not in the sentence, so a
    pathological name cannot make the message unreadable — but the sentence still
    names the icon-set version, because "unknown icon" without a version is not
    something a user can act on.

    :param name: the name as the caller submitted it.
    :param manifest: the loaded icon set, for the version in the sentence.
    """
    return ApiError(
        404,
        ErrorCode.NOT_FOUND,
        f"the bundled Material Design Icons set is {manifest.version} and does not contain "
        "this icon — search /api/icons/search for the names it ships",
        {"name": name},
    )


def _read_bounded(path: Path) -> tuple[bytes | None, str | None]:
    """Read *path*, refusing to buffer more than a custom icon is allowed to be.

    The read asks for one byte more than :data:`CUSTOM_ICON_MAX_BYTES`: that is
    how an oversized file is detected without stat'ing a size that could change
    between the check and the read, and without ever holding an unbounded file
    in memory on its way into a response.

    :param path: a file already validated by
        :func:`~chime_deck.icons.custom_icon_path`.
    :returns: the bytes and ``None``, or ``None`` and a rejection reason.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(CUSTOM_ICON_MAX_BYTES + 1)
    except OSError:
        return None, _READ_FAILED_REASON
    if len(data) > CUSTOM_ICON_MAX_BYTES:
        return None, f"the icon file is larger than the {CUSTOM_ICON_MAX_BYTES}-byte limit"
    return data, None


def _custom_icon_names(icons_dir: Path) -> list[str]:
    """List the candidate custom icon file names in *icons_dir*, case-insensitively.

    Existence is a per-request fact and the directory is never created: a
    missing or unreadable folder is simply an empty list. Names whose suffix is
    not in :data:`~chime_deck.icons.CUSTOM_ICON_EXTENSIONS` are not
    candidates at all, so ``README.md`` and ``.DS_Store`` appear in neither the
    ``icons`` nor the ``skipped`` list — they are noise, not broken icons.

    :param icons_dir: the configured custom icon directory.
    :returns: candidate file names, sorted case-insensitively.
    """
    try:
        entries = list(icons_dir.iterdir())
    except OSError:
        return []
    names = [entry.name for entry in entries if entry.suffix.casefold() in CUSTOM_ICON_EXTENSIONS]
    names.sort(key=str.casefold)
    return names


def _not_found(filename: str, reason: str | None) -> ApiError:
    """Build the 404 for a ``/custom-icons/`` name that is not a usable file.

    The detail names the submitted file name and the reason from the shared
    path rule; it never names the filesystem path, so the answer cannot leak
    where the icons folder is. The sentence is the same for every rejection —
    missing, escaping, a directory, a bad suffix — because it tells the user the
    one thing they can act on.

    :param filename: the file name as the caller submitted it.
    :param reason: rejection sentence from
        :func:`~chime_deck.icons.custom_icon_path`, or ``None``.
    """
    return ApiError(
        404,
        ErrorCode.NOT_FOUND,
        _NOT_FOUND_MESSAGE,
        {"filename": filename, "reason": reason or _UNKNOWN_REASON},
    )


def _unusable(filename: str, reason: str) -> ApiError:
    """Build the 422 for a file that exists but must not be served as it is.

    That is a read failure, an oversized file and every sanitisation refusal:
    the file *is* a custom icon by name, so this is not a 404, but its content
    (or size) means the server will not hand it out.

    :param filename: the file name as the caller submitted it.
    :param reason: the refusal sentence, safe to show and free of paths.
    """
    return ApiError(
        422,
        ErrorCode.VALIDATION_ERROR,
        f"this custom icon cannot be served — {reason}",
        {"filename": filename, "reason": reason},
    )


def build_icons_router(icons_dir: Path) -> APIRouter:
    """Build the icon search, path-data and custom-icon router.

    The manifest is shipped data, so the search routes take nothing but the
    request and the first request of the process pays the one manifest read.
    Only *icons_dir* is application configuration: it is closed over, never
    stat'ed here, so building an app with no ``data/icons`` directory opens
    nothing and starts nothing. V1 deliberately does not serve the bare
    ``/custom-icons`` (no trailing slash): the byte route owns
    ``/custom-icons/…``, and the bare namespace entry stays with the SPA, which
    is also where the route docstrings' no-trailing-slash note lives.

    :param icons_dir: the configured custom icon directory; a plain path, not
        resolved here because the directory may not exist yet.
    """
    router = APIRouter(tags=["icons"])

    @router.get("/api/icons/search")
    async def search_icons(
        q: str = "",
        limit: int = Query(SEARCH_LIMIT_DEFAULT, ge=1, le=SEARCH_LIMIT_MAX),
    ) -> IconSearchResponse:
        """Search the shipped icon set, ordered the way a picker wants it.

        ``q`` is matched case-insensitively as a substring of the icon name, and
        one leading ``mdi:`` is ignored so a pasted reference searches like the
        name it names; the ordering puts an exact name first, then prefix hits,
        then substring hits, alphabetically inside each rank, so the head of the
        list is what the user is typing towards. ``limit`` (default
        :data:`~chime_deck.api.limits.SEARCH_LIMIT_DEFAULT`, maximum
        :data:`~chime_deck.api.limits.SEARCH_LIMIT_MAX`) is applied *after*
        ordering, and ``matched`` reports the count before the cap, so the client
        can tell a complete answer from a truncated one.

        Never fails on ``q``: any string is a legal search term, and the only
        error is the standard ``422`` for a ``limit`` outside its range.
        """
        manifest = load_mdi_manifest()
        q_folded = q.strip().casefold().removeprefix(MDI_PREFIX)
        matches = [name for name in manifest.icons if _matches(name, q_folded)]
        matches.sort(key=lambda name: _rank(name, q_folded))
        return IconSearchResponse(
            version=manifest.version,
            total=len(manifest.icons),
            matched=len(matches),
            icons=[
                IconSearchResult(name=f"{MDI_PREFIX}{name}", path=manifest.icons[name])
                for name in matches[:limit]
            ],
        )

    @router.get("/api/icon/{name}")
    async def describe_icon(name: str) -> IconPathResponse:
        """Answer one icon's path data, or ``404`` when the set does not ship it.

        ``door`` and ``mdi:door`` are the same icon and get the same body. The
        name is normalised against the manifest's own name rule, so anything that
        is not a shipped icon — an unknown name, ``..``, an encoded traversal, a
        slash, a bare ``mdi:``, an uppercase name, a leading ``/`` — is a
        ``404`` and never a filesystem lookup: the only value that reaches the
        answer is the one the manifest returned.
        """
        manifest = load_mdi_manifest()
        normalised = normalise_icon_name(name)
        path = None if normalised is None else manifest.icons.get(normalised)
        if normalised is None or path is None:
            raise _icon_not_found(name, manifest)
        return IconPathResponse(name=f"{MDI_PREFIX}{normalised}", path=path)

    @router.get("/custom-icons/{filename:path}", response_class=Response)
    async def serve_custom_icon(filename: str) -> Response:
        """Serve one validated custom icon file, or answer a clean JSON error.

        The ``:path`` converter is load-bearing, not a convenience: the plain
        converter does not match a decoded ``/``, so a traversal-shaped name such
        as ``a/b.svg`` would not match this route at all and would fall through to
        :func:`~chime_deck.app.create_app`'s ``spa_fallback``, which answers
        the placeholder page (or the built UI's ``index.html``) with a ``200``.
        With ``:path`` the name reaches this handler and
        :func:`~chime_deck.icons.custom_icon_path` gets to reject it.

        The path rule and the file read are the same object: the route reads
        exactly the path that rule returned and does no second containment check
        and no re-derived path, so serve-time and save-time validation cannot
        drift apart. The directory is checked per request, so a ``data/icons``
        that does not exist is a clean ``404`` here rather than a startup
        failure; the directory is never created.

        Every answer carries :data:`_HARDENING_HEADERS`. ``nosniff`` stops the
        browser treating bytes as a type they are not, and the CSP's empty
        ``default-src`` plus ``sandbox`` is what keeps a same-origin SVG from
        running anything even if its content slipped past
        :func:`~chime_deck.icons.svg_safety.check_svg`. A ``404`` has to be
        clean and is deliberately not a placeholder page: the frontend's
        ``<img onerror>`` falls back to a built-in icon, and a ``200`` HTML body
        would render as a broken image instead.

        The bare ``/custom-icons`` (no trailing slash) is deliberately **not**
        served here — this route owns ``/custom-icons/…`` only and the bare
        namespace entry stays with the SPA. ``_is_reserved_path`` is not widened
        for it: that would be a behaviour change outside this ticket's route.

        :param filename: the decoded file name after ``/custom-icons/``.
        """
        path, problem = custom_icon_path(filename, icons_dir)
        if path is None:
            raise _not_found(filename, problem)
        data, read_problem = _read_bounded(path)
        if data is None:
            raise _unusable(filename, read_problem or _READ_FAILED_REASON)
        if path.suffix.casefold() == ".svg":
            svg_problem = check_svg(data)
            if svg_problem is not None:
                raise _unusable(filename, svg_problem)
        return Response(
            content=data,
            media_type=_CONTENT_TYPES.get(path.suffix.casefold(), "application/octet-stream"),
            headers=_HARDENING_HEADERS,
        )

    @router.get("/api/icons/custom")
    async def list_custom_icons() -> CustomIconListResponse:
        """List the custom icons a tile may store, and the files that were refused.

        The shape is an erratum: design 05 §3 names the byte route but no
        listing route at all, and the editor cannot offer the user's own files
        without one. The name is ``/api/icons/custom`` because this is a JSON
        selector in the same family as ``/api/icons/search``, while
        ``/custom-icons/…`` stays the byte namespace an ``<img>`` uses.

        Every candidate name goes through
        :func:`~chime_deck.icons.custom_icon_path` and then the same bounded
        read and :func:`~chime_deck.icons.svg_safety.check_svg` the serve
        route applies, so the listing can never advertise a file the server
        would refuse — the acceptance criterion is exactly that property. A file
        that fails goes into ``skipped`` with its reason, which is how the editor
        warns the user before they pick it; a file whose suffix is not ``.svg``
        or ``.png`` is not a candidate at all and appears in neither list.

        A missing or unreadable directory is an empty answer, not an error: a
        fresh install has no ``data/icons`` and that is a normal state. Names
        are ordered case-insensitively so ``Zed.svg`` and ``apple.png`` sort the
        way a person reads them.
        """
        icons: list[CustomIconEntry] = []
        skipped: list[CustomIconSkip] = []
        for name in _custom_icon_names(icons_dir):
            path, problem = custom_icon_path(name, icons_dir)
            if path is None:
                skipped.append(CustomIconSkip(name=name, reason=problem or _UNKNOWN_REASON))
                continue
            data, read_problem = _read_bounded(path)
            if data is None:
                skipped.append(
                    CustomIconSkip(name=name, reason=read_problem or _READ_FAILED_REASON)
                )
                continue
            if path.suffix.casefold() == ".svg":
                svg_problem = check_svg(data)
                if svg_problem is not None:
                    skipped.append(CustomIconSkip(name=name, reason=svg_problem))
                    continue
            icons.append(
                CustomIconEntry(name=name, reference=f"{CUSTOM_PREFIX}{name}", bytes=len(data))
            )
        return CustomIconListResponse(total=len(icons), icons=icons, skipped=skipped)

    return router
