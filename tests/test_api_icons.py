"""``/api/icons/search`` and ``/api/icon/{name}``: the shipped icon set, served.

Every test drives the real application through ``httpx.ASGITransport``, so the
routes are proven to be registered, and reads the committed manifest straight off
disk with ``json`` and ``importlib.resources``: the point of this suite is that
what the server answers **is** what the package ships. Nothing here touches the
network, Node or a regenerated file.

The version pin is asserted three ways — the manifest the package ships, the
loader's answer, and the exact ``@mdi/svg`` devDependency in ``web/package.json``
— because those three drifting apart is exactly how the picker starts offering
names whose path data the server does not have.

Owning ticket: HSD-zyru.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI

from chime_deck.api import SEARCH_LIMIT_DEFAULT, SEARCH_LIMIT_MAX
from chime_deck.api.errors import ErrorEnvelope
from chime_deck.api.icons import IconPathResponse, IconSearchResponse
from chime_deck.app import create_app
from chime_deck.config import ServerConfig
from chime_deck.icons import (
    MANIFEST_INDEX_FILENAME,
    MDI_DATA_DIRECTORY,
    MDI_PREFIX,
    MDI_VIEW_BOX,
    icon_path,
    load_mdi_manifest,
    normalise_icon_name,
    validate_icon_reference,
)

pytestmark = pytest.mark.asyncio

PINNED_VERSION = "7.4.47"
"""The icon set this build ships, pinned by ``web/package.json``.

Update this together with the generated data (``npm run gen:icons``) and the
``@mdi/svg`` devDependency: this test is what stops the three drifting apart.
"""

PINNED_ICON_COUNT = 7447
"""Icons in :data:`PINNED_VERSION`; a bump changes this too, on purpose."""

SIZE_LIMIT = 500_000
"""The repository's committed-file policy (``check-added-large-files``, 500 kB).

Every shipped shard must stay under it. When a future icon set crosses this, the
fix is to re-shard the generator, never to raise the limit.
"""

_HTTP_TIMEOUT_S = 5.0
"""Bound for one in-process request."""

_SEARCH_ROUTE = "/api/icons/search"
"""The search endpoint under test."""

_REJECTED_NAMES = [
    "no-such-icon-in-any-set",
    "..",
    "%2e%2e%2f..",
    "a/b",
    "mdi:",
    "DOOR",
    "door bell",
]
"""Names ``/api/icon/{name}`` must refuse, from an unknown icon to a traversal.

Some of them never reach the route — Starlette decodes ``%2e%2e%2f`` into extra
path segments and the client normalises a bare ``..`` — so the contract asserted
here is the one a client can rely on: a ``404`` in the standard error envelope,
always, and never a filesystem path.
"""


def _manifest_dir() -> Path:
    """Return the shipped manifest directory as a filesystem path.

    ``importlib.resources`` is how the loader finds it, so the tests read the same
    directory an installed wheel would serve from.
    """
    resource = resources.files("chime_deck.icons").joinpath("data", MDI_DATA_DIRECTORY)
    return Path(str(resource))


def _read_index() -> dict[str, object]:
    """Return the shipped ``index.json`` as a plain mapping."""
    text = (_manifest_dir() / MANIFEST_INDEX_FILENAME).read_text(encoding="utf-8")
    return cast("dict[str, object]", json.loads(text))


def _shard_maps() -> dict[str, dict[str, str]]:
    """Return every shipped shard as ``file name -> {name: path}``, read off disk."""
    return {
        path.name: cast("dict[str, str]", json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(_manifest_dir().glob("*.json"))
        if path.name != MANIFEST_INDEX_FILENAME
    }


def _shipped_icons() -> dict[str, str]:
    """Return the union of every shipped shard, i.e. what the loader must return."""
    icons: dict[str, str] = {}
    for shard in _shard_maps().values():
        icons.update(shard)
    return icons


def _sample_names(icons: Mapping[str, str]) -> list[str]:
    """Return a deterministic spread of names: one per shard plus the set's edges."""
    names: list[str] = []
    for shard in _shard_maps().values():
        ordered = sorted(shard)
        names.append(ordered[len(ordered) // 2])
    longest_path = max(icons, key=lambda name: (len(icons[name]), name))
    shortest_name = min(icons, key=lambda name: (len(name), name))
    names.extend(
        [
            "door",  # the ticket's own example, and a prefix of many names
            "door-open",  # multi-word
            "battery-10",  # the name @mdi/js's `mdiBattery10` cannot be inverted into
            "android",  # deprecated in 7.4.47; still shipped, still drawable
            longest_path,
            shortest_name,
        ]
    )
    return names


def _build_app(tmp_path: Path) -> FastAPI:
    """Build the real application with an isolated data directory and no built UI.

    :param tmp_path: pytest temporary directory used as the data directory.
    """
    return create_app(ServerConfig(data_dir=tmp_path, static_dir=tmp_path / "no-dist"))


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


def _rank_key(name: str, term: str) -> tuple[int, str]:
    """Return the ordering the picker promises: exact, then prefix, then substring.

    :param name: bare icon name.
    :param term: the already-normalised search term.
    """
    if name == term:
        return (0, name)
    if name.startswith(term):
        return (1, name)
    return (2, name)


async def _search(client: httpx.AsyncClient, url: str) -> IconSearchResponse:
    """Request *url* and return the parsed search envelope.

    :param client: client bound to the application under test.
    :param url: the search URL, including its query string.
    """
    response = await client.get(url)
    assert response.status_code == 200, response.text
    return IconSearchResponse.model_validate(response.json())


# --------------------------------------------------------------------------- #
# The shipped manifest, read straight off disk
# --------------------------------------------------------------------------- #


async def test_shipped_index_pins_the_bundled_icon_set(tmp_path: Path) -> None:
    """The manifest's version is the pinned one, the loader's and web/'s."""
    index = _read_index()
    manifest = load_mdi_manifest()
    package = json.loads((Path(__file__).resolve().parents[1] / "web/package.json").read_text())

    assert index["version"] == PINNED_VERSION
    assert manifest.version == index["version"]
    assert package["devDependencies"]["@mdi/svg"] == PINNED_VERSION
    assert index["source"] == "@mdi/svg"
    assert index["icon_count"] == PINNED_ICON_COUNT
    assert len(manifest.icons) == PINNED_ICON_COUNT


async def test_shipped_shard_files_match_the_index(tmp_path: Path) -> None:
    """The index's shard list is the shard set: no orphan file, no missing one."""
    index = _read_index()
    shards = cast("list[str]", index["shards"])
    on_disk = set(_shard_maps())

    assert shards == sorted(shards)
    assert set(shards) == on_disk
    assert all(name.endswith(".json") for name in shards)


async def test_every_shipped_file_is_under_the_large_file_policy(tmp_path: Path) -> None:
    """No shipped file may trip the repository's 500 kB committed-file policy."""
    for path in sorted(_manifest_dir().iterdir()):
        size = path.stat().st_size
        assert size < SIZE_LIMIT, (
            f"{path.name} is {size} bytes, over the {SIZE_LIMIT}-byte committed-file policy — "
            "re-shard the generator rather than raising the limit"
        )


async def test_loader_returns_exactly_the_shipped_data(tmp_path: Path) -> None:
    """The served name set is the shipped one: the loader's map equals the files."""
    manifest = load_mdi_manifest()

    assert dict(manifest.icons) == _shipped_icons()
    assert len(manifest.icons) == PINNED_ICON_COUNT


# --------------------------------------------------------------------------- #
# One icon's path data
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("spelled_with_prefix", [False, True])
async def test_every_sampled_icon_is_served_byte_for_byte(
    tmp_path: Path, spelled_with_prefix: bool
) -> None:
    """``/api/icon/{name}`` answers the shipped path data for a spread of names."""
    app = _build_app(tmp_path)
    shipped = _shipped_icons()
    samples = _sample_names(shipped)

    async with _running(app) as client:
        for name in samples:
            reference = f"{MDI_PREFIX}{name}" if spelled_with_prefix else name
            response = await client.get(f"/api/icon/{reference}")
            assert response.status_code == 200, response.text
            body = IconPathResponse.model_validate(response.json())
            assert body.name == f"{MDI_PREFIX}{name}"
            assert body.path == shipped[name]
            assert body.view_box == MDI_VIEW_BOX
    assert len(samples) >= 20


async def test_prefixed_and_bare_names_answer_identically(tmp_path: Path) -> None:
    """``door`` and ``mdi:door`` are the same icon and get the same body."""
    app = _build_app(tmp_path)

    async with _running(app) as client:
        bare = await client.get("/api/icon/door")
        prefixed = await client.get("/api/icon/mdi:door")

    assert bare.status_code == prefixed.status_code == 200
    assert bare.json() == prefixed.json()
    assert bare.json()["path"] == load_mdi_manifest().icons["door"]


async def test_manifest_names_are_all_valid_mdi_references(tmp_path: Path) -> None:
    """The manifest and ``validate_icon_reference`` cannot disagree about a name."""
    manifest = load_mdi_manifest()

    rejected = [
        name
        for name in manifest.icons
        if validate_icon_reference(f"{MDI_PREFIX}{name}", None) is not None
    ]
    assert rejected == []


async def test_icon_path_is_total(tmp_path: Path) -> None:
    """``icon_path`` answers the shipped data and ``None``, never an exception."""
    manifest = load_mdi_manifest()

    assert icon_path("door") == manifest.icons["door"]
    assert icon_path("mdi:door") == manifest.icons["door"]
    assert icon_path("no-such-icon-in-any-set") is None
    assert icon_path("..") is None
    assert icon_path("mdi:mdi:door") is None


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


async def test_search_filters_on_a_substring(tmp_path: Path) -> None:
    """``q`` filters the set, and every hit really contains the term."""
    app = _build_app(tmp_path)
    icons = load_mdi_manifest().icons
    expected = sorted(name for name in icons if "door" in name)

    async with _running(app) as client:
        body = await _search(client, f"{_SEARCH_ROUTE}?q=door")

    assert body.version == PINNED_VERSION
    assert body.total == len(icons)
    assert body.matched == len(expected)
    served = [icon.name.removeprefix(MDI_PREFIX) for icon in body.icons]
    assert set(served) == set(expected)
    assert all("door" in name for name in served)
    # The exact name is the type-ahead's first hit, ahead of the prefix and
    # substring matches; the rest of the order is the route's own business.
    assert served[0] == "door"
    assert all(name in icons for name in served)


async def test_search_is_case_insensitive(tmp_path: Path) -> None:
    """``DOOR`` and ``door`` are the same search."""
    app = _build_app(tmp_path)

    async with _running(app) as client:
        upper = await client.get(f"{_SEARCH_ROUTE}?q=DOOR&limit=5")
        lower = await client.get(f"{_SEARCH_ROUTE}?q=door&limit=5")

    assert upper.status_code == lower.status_code == 200
    assert upper.json() == lower.json()


async def test_search_ignores_a_pasted_mdi_prefix(tmp_path: Path) -> None:
    """``mdi:door`` searches like ``door``, matching what ``/api/icon/{name}`` accepts."""
    app = _build_app(tmp_path)

    async with _running(app) as client:
        pasted = await client.get(f"{_SEARCH_ROUTE}?q=mdi:door&limit=5")
        bare = await client.get(f"{_SEARCH_ROUTE}?q=door&limit=5")

    assert pasted.status_code == bare.status_code == 200
    assert pasted.json() == bare.json()
    first = cast("dict[str, object]", pasted.json())["icons"]
    assert cast("list[dict[str, object]]", first)[0]["name"] == f"{MDI_PREFIX}door"


async def test_empty_q_is_the_first_page_alphabetically(tmp_path: Path) -> None:
    """An empty ``q`` is the picker's initial grid: the alphabetical head of the set."""
    app = _build_app(tmp_path)
    names = sorted(load_mdi_manifest().icons)

    async with _running(app) as client:
        body = await _search(client, _SEARCH_ROUTE)

    assert body.total == len(names)
    assert body.matched == len(names)
    assert [icon.name for icon in body.icons] == [
        f"{MDI_PREFIX}{name}" for name in names[:SEARCH_LIMIT_DEFAULT]
    ]


async def test_limit_is_applied_after_ordering(tmp_path: Path) -> None:
    """``limit`` cuts the ordered list, so a page is a prefix of the full answer."""
    app = _build_app(tmp_path)

    async with _running(app) as client:
        full = await _search(client, f"{_SEARCH_ROUTE}?q=door")
        page = await _search(client, f"{_SEARCH_ROUTE}?q=door&limit=2")

    assert page.matched == full.matched
    assert page.icons == full.icons[:2]


@pytest.mark.parametrize("limit", [0, SEARCH_LIMIT_MAX + 1])
async def test_limit_out_of_range_is_the_standard_422(tmp_path: Path, limit: int) -> None:
    """A ``limit`` outside 1..SEARCH_LIMIT_MAX is rejected, not clamped."""
    app = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get(f"{_SEARCH_ROUTE}?limit={limit}")

    assert response.status_code == 422
    body = ErrorEnvelope.model_validate(response.json())
    assert body.error.code == "validation_error"
    assert body.error.detail["fields"][0]["loc"] == ["query", "limit"]  # type: ignore[index]


async def test_matched_reports_the_count_before_the_cap(tmp_path: Path) -> None:
    """A truncated page says how many icons matched, so the picker can keep typing."""
    app = _build_app(tmp_path)

    async with _running(app) as client:
        body = await _search(client, f"{_SEARCH_ROUTE}?q=door&limit=1")

    assert body.matched > len(body.icons) == 1


async def test_an_exact_name_comes_first(tmp_path: Path) -> None:
    """The term's own icon leads, ahead of a substring hit that sorts earlier."""
    app = _build_app(tmp_path)
    names = load_mdi_manifest().icons

    assert "bell" in names
    assert "alarm-bell" in names  # a hit that sorts alphabetically BEFORE "bell"

    async with _running(app) as client:
        body = await _search(client, f"{_SEARCH_ROUTE}?q=bell")

    expected = sorted(
        (name for name in names if "bell" in name), key=lambda n: _rank_key(n, "bell")
    )
    assert [icon.name for icon in body.icons] == [f"{MDI_PREFIX}{name}" for name in expected]
    assert body.icons[0].name == f"{MDI_PREFIX}bell"


async def test_every_search_result_pair_is_shipped(tmp_path: Path) -> None:
    """Each returned name/path pair is a manifest entry with exactly that path."""
    app = _build_app(tmp_path)
    icons = load_mdi_manifest().icons

    async with _running(app) as client:
        body = await _search(client, f"{_SEARCH_ROUTE}?q=door&limit={SEARCH_LIMIT_MAX}")

    assert body.icons
    for icon in body.icons:
        assert icon.name.startswith(MDI_PREFIX)
        assert icons[icon.name.removeprefix(MDI_PREFIX)] == icon.path


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", _REJECTED_NAMES)
async def test_icon_refusals_answer_the_error_envelope(tmp_path: Path, name: str) -> None:
    """Anything not shipped is a ``404`` in the standard envelope, never a path."""
    app = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get(f"/api/icon/{name}")

    assert response.status_code == 404
    body = ErrorEnvelope.model_validate(response.json())
    assert body.error.code == "not_found"
    assert "/home/" not in response.text
    # No shipped filesystem location may leak into the answer; the submitted name
    # itself is echoed in ``detail``, so the needle is the data directory path.
    assert f"data/{MDI_DATA_DIRECTORY}" not in response.text


@pytest.mark.parametrize("name", ["no-such-icon-in-any-set", "mdi:", "DOOR"])
async def test_icon_refusals_name_the_icon_set_version(tmp_path: Path, name: str) -> None:
    """The 404 sentence says which icon set was searched and where to search it."""
    app = _build_app(tmp_path)

    async with _running(app) as client:
        response = await client.get(f"/api/icon/{name}")

    body = ErrorEnvelope.model_validate(response.json())
    assert PINNED_VERSION in body.error.message
    assert _SEARCH_ROUTE in body.error.message
    assert body.error.detail["name"] == name


# --------------------------------------------------------------------------- #
# Name normalisation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("door", "door"),
        ("mdi:door", "door"),
        ("battery-10", "battery-10"),
        ("mdi:battery-10", "battery-10"),
    ],
)
async def test_normalise_icon_name_accepts_both_spellings(name: str, expected: str) -> None:
    """A bare name and its ``mdi:`` spelling normalise to the same icon."""
    assert normalise_icon_name(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "",
        "mdi:",
        "..",
        "a/b",
        "a\\b",
        "Door",
        "door bell",
        " door",
        "door ",
        "/door",
        "-leading",
        "trailing-",
        "mdi:mdi:door",
        "door.svg",
    ],
)
async def test_normalise_icon_name_rejects_anything_else(name: str) -> None:
    """Every near miss is ``None``: the manifest lookup is a key, never a path."""
    assert normalise_icon_name(name) is None
