"""``/custom-icons/{filename}`` and ``/api/icons/custom``: the user's own icons.

Every test drives the real application through ``httpx.ASGITransport`` and a
temporary ``data_dir``, so the routes are proven to be registered ahead of the
SPA catch-all and the shared path rule is proven to be the one the resolver
already uses. Nothing here touches the network and every file lives under
``tmp_path``.

Owning ticket: HSD-i7pw.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from chime_deck.api.errors import ErrorEnvelope
from chime_deck.api.icons import CUSTOM_ICON_MAX_BYTES, CustomIconListResponse
from chime_deck.app import create_app
from chime_deck.config import ServerConfig
from chime_deck.icons import (
    CUSTOM_PREFIX,
    custom_icon_path,
    validate_icon_reference,
)

pytestmark = pytest.mark.asyncio

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_SAFE_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>'
)
"""A minimal safe icon; every "valid" case starts from these exact bytes."""

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
"""A PNG signature and padding; the server never parses PNGs, only types them."""

_REFUSED_URLS = [
    "/custom-icons/missing.svg",
    "/custom-icons/dir.svg",
    "/custom-icons/escape.svg",
    # The encoded dot segment is how a client can actually send a bare ``..``:
    # httpx collapses a literal ``/custom-icons/..`` to ``/`` before the request.
    "/custom-icons/%2e%2e",
    "/custom-icons/a%2Fb.svg",
    "/custom-icons/%2e%2e.svg",
    "/custom-icons/no-suffix",
    "/custom-icons/bad%00.svg",
    "/custom-icons/",
]
"""Every refusal shape the byte route must answer with a clean 404 envelope."""


def _build_app(tmp_path: Path, static_dir: Path | None = None) -> FastAPI:
    """Build the real application with an isolated data directory.

    :param tmp_path: pytest temporary directory used as the data directory.
    :param static_dir: built-UI directory; defaults to an absent one.
    """
    return create_app(
        ServerConfig(
            data_dir=tmp_path,
            static_dir=static_dir if static_dir is not None else tmp_path / "no-dist",
        )
    )


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield a client bound to it.

    :param app: application under test.
    """
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=_HTTP_TIMEOUT_S
        ) as client:
            yield client


@pytest.fixture
def icons_dir(tmp_path: Path) -> Path:
    """Create the real ``data_dir/icons`` used by every custom-icon test.

    The fixture holds one good SVG, one good PNG, a directory whose name looks
    like an icon, and a symlink escaping the folder, because those are the four
    shapes the shared path rule has to separate.

    :param tmp_path: pytest temporary directory used as the data directory.
    """
    directory = tmp_path / "icons"
    directory.mkdir()
    (directory / "cat.svg").write_bytes(_SAFE_SVG)
    (directory / "dot.png").write_bytes(_PNG_BYTES)
    (directory / "dir.svg").mkdir()
    outside = tmp_path / "outside.svg"
    outside.write_bytes(_SAFE_SVG)
    (directory / "escape.svg").symlink_to(outside)
    return directory


def _add_listing_extras(icons_dir: Path) -> None:
    """Add the refused files the listing tests must report and never advertise.

    :param icons_dir: the fixture's icons directory.
    """
    (icons_dir / "bad.svg").write_bytes(b"<svg><script>alert(1)</script></svg>")
    (icons_dir / "big.svg").write_bytes(b"<svg/>" + b" " * CUSTOM_ICON_MAX_BYTES)
    (icons_dir / "README.md").write_text("not an icon", encoding="utf-8")


async def _error_response(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """Request *url* and assert it failed with the standard JSON error envelope.

    :param client: client bound to the application under test.
    :param url: URL expected to fail.
    """
    response = await client.get(url)
    assert response.status_code >= 400, response.text
    assert response.headers["content-type"].startswith("application/json"), response.text
    ErrorEnvelope.model_validate(response.json())
    return response


# --------------------------------------------------------------------------- #
# Serving the bytes
# --------------------------------------------------------------------------- #


async def test_safe_svg_is_served_byte_for_byte(tmp_path: Path, icons_dir: Path) -> None:
    """The response body is the file, typed as SVG and hardened against sniffing."""
    app = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/custom-icons/cat.svg")

    assert response.status_code == 200, response.text
    assert response.content == (icons_dir / "cat.svg").read_bytes()
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"


async def test_png_is_served_as_png(tmp_path: Path, icons_dir: Path) -> None:
    """A PNG is served untouched with its own media type and the same headers."""
    app = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/custom-icons/dot.png")

    assert response.status_code == 200, response.text
    assert response.content == _PNG_BYTES
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"


async def test_uppercase_suffix_is_served(tmp_path: Path, icons_dir: Path) -> None:
    """``cat.SVG`` is accepted: the shared predicate's casefolded suffix decides."""
    (icons_dir / "loud.SVG").write_bytes(_SAFE_SVG)
    app = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/custom-icons/loud.SVG")

    assert response.status_code == 200, response.text
    assert response.content == _SAFE_SVG
    assert response.headers["content-type"] == "image/svg+xml"


@pytest.mark.parametrize("url", _REFUSED_URLS)
async def test_unusable_names_answer_a_clean_404(tmp_path: Path, icons_dir: Path, url: str) -> None:
    """Every traversal-shaped, absent or unusable name is a JSON 404, never a path.

    :param icons_dir: the fixture's icons directory.
    :param url: refused request URL.
    """
    app = _build_app(tmp_path)

    async with _running(app) as client:
        response = await _error_response(client, url)

    assert response.status_code == 404
    body = ErrorEnvelope.model_validate(response.json())
    assert body.error.code == "not_found"
    assert "/home/" not in response.text
    assert str(tmp_path) not in response.text


# --------------------------------------------------------------------------- #
# An absent icons directory is a normal install
# --------------------------------------------------------------------------- #


async def test_absent_icons_directory_is_not_a_startup_failure(tmp_path: Path) -> None:
    """No ``data/icons`` still builds, serves a 404 and lists nothing."""
    assert not (tmp_path / "icons").exists()
    app = _build_app(tmp_path)

    async with _running(app) as client:
        refused = await _error_response(client, "/custom-icons/anything.svg")
        listing = await client.get("/api/icons/custom")

    assert refused.status_code == 404
    assert listing.status_code == 200, listing.text
    assert listing.json() == {"total": 0, "icons": [], "skipped": []}
    body = CustomIconListResponse.model_validate(listing.json())
    assert body.total == len(body.icons) == 0
    assert body.skipped == []


# --------------------------------------------------------------------------- #
# The route pair sits ahead of the SPA catch-all
# --------------------------------------------------------------------------- #


def _built_ui(tmp_path: Path) -> Path:
    """Create a real static directory whose ``index.html`` is recognisable.

    :param tmp_path: pytest temporary directory.
    """
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("built ui", encoding="utf-8")
    return static_dir


async def test_byte_route_never_falls_through_to_the_spa(tmp_path: Path) -> None:
    """A traversal-shaped name is our JSON 404, not the SPA's 200 placeholder.

    The catch-all is proven live first (``/settings`` answers the built UI), so a
    pass cannot come from the SPA being absent.
    """
    app = _build_app(tmp_path, _built_ui(tmp_path))

    async with _running(app) as client:
        settings = await client.get("/settings")
        trailing_nope = await _error_response(client, "/custom-icons/nope.svg")
        empty_name = await _error_response(client, "/custom-icons/")

    assert settings.status_code == 200
    assert "built ui" in settings.text
    assert trailing_nope.status_code == empty_name.status_code == 404
    assert "built ui" not in trailing_nope.text
    assert "built ui" not in empty_name.text


async def test_bare_custom_icons_namespace_stays_with_the_spa(tmp_path: Path) -> None:
    """``/custom-icons`` (no trailing slash) is deliberately not a byte route.

    The route owns ``/custom-icons/…`` only; the bare namespace entry is a UI
    path, so with a built UI it answers ``index.html`` — not a 500 and not our
    JSON envelope. Widening ``_is_reserved_path`` for it would be a separate
    behaviour change.
    """
    app = _build_app(tmp_path, _built_ui(tmp_path))

    async with _running(app) as client:
        response = await client.get("/custom-icons")

    assert response.status_code == 200
    assert "built ui" in response.text
    assert not response.headers["content-type"].startswith("application/json")


# --------------------------------------------------------------------------- #
# Sanitisation over HTTP
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("filename", "payload", "leak"),
    [
        ("script.svg", b"<svg><script>alert(1)</script></svg>", "alert(1)"),
        ("onload.svg", b'<svg onload="alert(1)"/>', "alert(1)"),
        (
            "external.svg",
            b'<svg xmlns:xlink="http://www.w3.org/1999/xlink">'
            b'<image xlink:href="https://evil.example/x.png"/></svg>',
            "evil.example",
        ),
        ("doctype.svg", b'<!DOCTYPE svg [<!ENTITY x "y">]><svg>&x;</svg>', "ENTITY"),
        ("malformed.svg", b"<svg", "<svg"),
    ],
)
async def test_unsafe_svg_content_is_a_422_that_never_echoes_payload(
    tmp_path: Path, icons_dir: Path, filename: str, payload: bytes, leak: str
) -> None:
    """A file that passes the path rule but fails sanitisation is a 422 envelope.

    :param icons_dir: the fixture's icons directory.
    :param filename: name the unsafe payload is written under.
    :param payload: unsafe SVG bytes.
    :param leak: text the response must never contain.
    """
    (icons_dir / filename).write_bytes(payload)
    app = _build_app(tmp_path)

    async with _running(app) as client:
        response = await _error_response(client, f"/custom-icons/{filename}")

    assert response.status_code == 422
    body = ErrorEnvelope.model_validate(response.json())
    assert body.error.code == "validation_error"
    assert body.error.detail["reason"]
    assert leak not in response.text


# --------------------------------------------------------------------------- #
# The listing
# --------------------------------------------------------------------------- #


async def test_listing_advertises_only_servable_files(tmp_path: Path, icons_dir: Path) -> None:
    """The listing is exactly the files the serve route would accept."""
    _add_listing_extras(icons_dir)
    app = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/icons/custom")

    assert response.status_code == 200, response.text
    listing = CustomIconListResponse.model_validate(response.json())
    assert listing.total == 2 == len(listing.icons)
    assert [icon.name for icon in listing.icons] == ["cat.svg", "dot.png"]
    for icon in listing.icons:
        assert icon.reference == f"{CUSTOM_PREFIX}{icon.name}"
        assert icon.bytes == (icons_dir / icon.name).stat().st_size
    assert [skip.name for skip in listing.skipped] == [
        "bad.svg",
        "big.svg",
        "dir.svg",
        "escape.svg",
    ]
    assert "README.md" not in {icon.name for icon in listing.icons}
    assert "README.md" not in {skip.name for skip in listing.skipped}
    for skip in listing.skipped:
        assert skip.reason
        assert "/home/" not in skip.reason
        assert str(tmp_path) not in skip.reason


async def test_listing_orders_names_case_insensitively(tmp_path: Path, icons_dir: Path) -> None:
    """``Zed.svg`` sorts after ``apple.png``: the comparison folds case first.

    A plain ASCII sort would put ``Zed.svg`` first (``Z`` is 90, ``a`` is 97),
    so this pair is what proves the ordering is the casefolded one.
    """
    (icons_dir / "Zed.svg").write_bytes(_SAFE_SVG)
    (icons_dir / "apple.png").write_bytes(_PNG_BYTES)
    app = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get("/api/icons/custom")

    listing = CustomIconListResponse.model_validate(response.json())
    assert [icon.name for icon in listing.icons] == ["apple.png", "cat.svg", "dot.png", "Zed.svg"]


async def test_every_advertised_name_is_served(tmp_path: Path, icons_dir: Path) -> None:
    """The acceptance criterion: the listing can never advertise an unservable file."""
    _add_listing_extras(icons_dir)
    app = _build_app(tmp_path)

    async with _running(app) as client:
        listing = CustomIconListResponse.model_validate(
            (await client.get("/api/icons/custom")).json()
        )
        assert listing.icons
        for icon in listing.icons:
            response = await client.get(f"/custom-icons/{icon.name}")
            assert response.status_code == 200, (icon.name, response.text)
            assert response.headers["content-type"] == (
                "image/svg+xml" if icon.name.casefold().endswith(".svg") else "image/png"
            )


# --------------------------------------------------------------------------- #
# One predicate for save and serve
# --------------------------------------------------------------------------- #


async def test_serve_and_save_rules_are_one_predicate(tmp_path: Path, icons_dir: Path) -> None:
    """``custom_icon_path`` and ``validate_icon_reference`` agree on every candidate.

    The byte route reads the path the first returns; the entity save uses the
    second. If they ever disagreed, a saved reference could render as a broken
    image (or a served file could be unsavable), so both the verdict and the
    sentence must match exactly.
    """
    (icons_dir / "LOUD.SVG").write_bytes(_SAFE_SVG)
    candidates = [
        "cat.svg",
        "dot.png",
        "LOUD.SVG",
        "missing.svg",
        "dir.svg",
        "..",
        "a/b.svg",
        "no-suffix",
        "bad\x00.svg",
        "escape.svg",
    ]
    for name in candidates:
        path, problem = custom_icon_path(name, icons_dir)
        save_problem = validate_icon_reference(f"{CUSTOM_PREFIX}{name}", icons_dir)
        assert (path is not None) == (save_problem is None), name
        assert problem == save_problem, name
