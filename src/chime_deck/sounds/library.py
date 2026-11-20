"""The folder-backed sound library: scan, mark missing and seed the demos.

The library is the single owner of the ``data/sounds`` directory's *metadata
projection*. It never deletes a row: a file that vanished is flagged
``missing`` so the UI can show a broken clip and the user can decide. A file
that reappears clears the flag on the next scan.

Scanning is split into an unlocked read/probe phase and a short locked write
phase. Probing can take seconds (one ``ffprobe`` per new or changed file) and
must not stop a URL download from publishing; the write phase only re-lists the
rows and writes the differences, so it stays in the millisecond range.

Every stored relative path is treated as untrusted. A row whose path is
absolute, contains ``..`` or a backslash is left completely alone and reported
once: silently rewriting it would either escape the library directory or hide a
row the user can still see.

Owning ticket: HSD-cwvc.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import logging
import os
import shutil
import uuid
from collections.abc import Callable, Collection
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from ..store import (
    UNSET,
    ConfigStore,
    Sound,
    SoundIn,
    SoundUpdate,
    StoreConflict,
)
from .probe import probe_duration_ms

__all__ = [
    "LICENCE_FILENAME",
    "SCAN_BUDGET_S",
    "SUPPORTED_FORMATS",
    "DurationProbe",
    "ScanReport",
    "SoundLibrary",
    "bundled_demo_sounds_dir",
    "copy_bundled_demo_sounds",
    "sound_file_path",
]

_LOGGER = logging.getLogger(__name__)

SUPPORTED_FORMATS: Final[tuple[str, ...]] = ("wav", "mp3", "ogg", "flac")
"""Extensions the library indexes, in a stable order used by messages."""

SCAN_BUDGET_S: Final = 15.0
"""Seconds a scan may spend starting probes; the rest get an unknown duration."""

LICENCE_FILENAME: Final = "LICENSES.md"
"""Name of the licence file shipped next to the bundled demo sounds."""

_SKIPPED_SUFFIXES: Final = (".tmp", ".part")
"""Suffixes of an in-progress copy; indexing one would show a truncated sound."""

DurationProbe = Callable[[Path], int | None]
"""Returns a file's duration in milliseconds, or ``None`` when unknown."""


@dataclass(frozen=True, slots=True)
class ScanReport:
    """What one scan changed.

    ``added`` is the number of rows created by this scan. ``removed`` is the
    number of rows that transitioned to missing during this scan: a vanished
    file is flagged, never deleted. ``missing`` is the total number of rows
    flagged missing after the scan, and ``total`` is the number of sound rows
    that exist after it.
    """

    added: int
    removed: int
    missing: int
    total: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One supported file seen in the unlock phase."""

    relative: str
    fmt: str
    size: int
    duration_ms: int | None


def _normalise_stored_path(raw: str) -> str | None:
    """Return a safe relative POSIX path, or ``None`` when it cannot be mapped.

    Repeated leading ``./`` is removed. Anything else that is absolute, empty,
    contains a backslash or a ``.``/``..`` component is rejected.

    :param raw: path text stored on a sound row.
    """
    candidate = raw
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if not candidate or "\\" in candidate:
        return None
    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return PurePosixPath(*parts).as_posix()


def _list_sound_files(directory: Path) -> dict[str, tuple[Path, str, int]]:
    """Return supported files keyed by their relative POSIX path.

    Hidden files and directories, in-progress copies and unreadable files are
    skipped. The directory need not exist; an absent one yields an empty map.

    :param directory: library directory to list recursively.
    """
    found: dict[str, tuple[Path, str, int]] = {}
    try:
        entries = list(directory.rglob("*"))
    except OSError:
        return found
    for path in entries:
        try:
            parts = path.relative_to(directory).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in parts):
            continue
        if path.name.endswith(_SKIPPED_SUFFIXES):
            continue
        try:
            if not path.is_file():
                continue
            fmt = path.suffix.lower().lstrip(".")
            if fmt not in SUPPORTED_FORMATS:
                continue
            size = path.stat().st_size
        except OSError:
            continue
        found[path.relative_to(directory).as_posix()] = (path, fmt, size)
    return found


def _index_rows(rows: list[Sound]) -> dict[str, Sound]:
    """Map safely relative row paths to their rows.

    :param rows: every sound row.
    """
    indexed: dict[str, Sound] = {}
    for row in rows:
        if row.path is None:
            continue
        relative = _normalise_stored_path(row.path)
        if relative is not None:
            indexed.setdefault(relative, row)
    return indexed


def sound_file_path(stored: str | None, directory: Path) -> Path | None:
    """Return the library file a stored sound row names, or ``None`` when unusable.

    This is the *one* place the containment rule is applied to a stored row's
    path: the row's path is normalised with the scanner's own
    :func:`_normalise_stored_path`, resolved against *directory* (so a symlink
    whose target leaves the library is refused too), and accepted only when it
    is an existing regular file below *directory*. It never creates anything and
    it never returns a path outside *directory*. Both validation steps go
    through the same normaliser the scanner uses, so serve-time and scan-time
    validation cannot drift apart.

    :param stored: path text stored on the row, possibly ``None``.
    :param directory: library directory the path has to stay inside.
    :returns: the resolved file, or ``None`` when the row names no servable file.
    """
    if stored is None:
        return None
    relative = _normalise_stored_path(stored)
    if relative is None:
        return None
    root = directory.resolve()
    resolved = (directory / relative).resolve()
    if resolved == root or root not in resolved.parents:
        return None
    try:
        if not resolved.is_file():
            return None
    except OSError:
        return None
    return resolved


class SoundLibrary:
    """Scans one directory into the ``sounds`` table."""

    def __init__(
        self,
        store: ConfigStore,
        directory: Path,
        *,
        probe: DurationProbe | None = None,
    ) -> None:
        """Create a library bound to a store and a directory.

        :param store: connected configuration store.
        :param directory: the library directory; created by the first scan.
        :param probe: duration probe; ``None`` uses :func:`probe_duration_ms`.
        """
        self._store = store
        self._directory = directory
        self._probe_file: DurationProbe = probe if probe is not None else probe_duration_ms
        self._write_lock = asyncio.Lock()

    @property
    def write_lock(self) -> asyncio.Lock:
        """The lock the write phase holds, shared with :class:`SoundDownloader`."""
        return self._write_lock

    @property
    def directory(self) -> Path:
        """Directory this library scans."""
        return self._directory

    async def scan(self, *, budget_s: float = SCAN_BUDGET_S) -> ScanReport:
        """Index supported files and mark vanished ones missing.

        :param budget_s: seconds that may be spent starting probes; once it is
            used up the remaining files are indexed with an unknown duration
            and a later scan fills them in.
        """
        self._directory.mkdir(parents=True, exist_ok=True)
        rows = await self._store.list_sounds()
        indexed = _index_rows(rows)
        listing = _list_sound_files(self._directory)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget_s
        candidates: dict[str, _Candidate] = {}
        for relative, (path, fmt, size) in listing.items():
            row = indexed.get(relative)
            needs_probe = row is None or row.bytes != size or row.duration_ms is None
            duration: int | None = None
            if needs_probe and loop.time() < deadline:
                duration = await asyncio.to_thread(self._probe_file, path)
            candidates[relative] = _Candidate(relative, fmt, size, duration)

        return await self._apply(candidates)

    async def _apply(self, candidates: dict[str, _Candidate]) -> ScanReport:
        """Write the differences between *candidates* and the stored rows.

        :param candidates: files found by the unlock phase, with durations.
        """
        async with self._write_lock:
            rows = await self._store.list_sounds()
            indexed = _index_rows(rows)
            fresh = _list_sound_files(self._directory)

            added = 0
            removed = 0
            missing = 0
            unmappable = 0

            for row in rows:
                if row.path is None:
                    if row.source_type == "url":
                        continue
                    if not row.missing:
                        await self._store.update_sound(row.id, SoundUpdate(missing=True))
                        removed += 1
                    missing += 1
                    continue

                relative = _normalise_stored_path(row.path)
                if relative is None:
                    unmappable += 1
                    continue

                entry = fresh.get(relative)
                if entry is None:
                    if not row.missing:
                        await self._store.update_sound(row.id, SoundUpdate(missing=True))
                        removed += 1
                    missing += 1
                    continue

                _path, fmt, size = entry
                candidate = candidates.get(relative)
                duration = None if candidate is None else candidate.duration_ms
                desired_duration = duration if duration is not None else row.duration_ms
                if (
                    row.missing
                    or size != row.bytes
                    or fmt != row.format
                    or desired_duration != row.duration_ms
                ):
                    await self._store.update_sound(
                        row.id,
                        SoundUpdate(
                            bytes=size if size != row.bytes else UNSET,
                            format=fmt if fmt != row.format else UNSET,
                            duration_ms=(
                                desired_duration if desired_duration != row.duration_ms else UNSET
                            ),
                            missing=False if row.missing else UNSET,
                        ),
                    )

            for relative, candidate in candidates.items():
                if relative in indexed or relative not in fresh:
                    # A file that vanished while its probe was running must not
                    # come back as a healthy row; the next scan would only have
                    # to mark it missing again.
                    continue
                try:
                    await self._store.create_sound(
                        SoundIn(
                            name=PurePosixPath(relative).stem,
                            source_type="file",
                            path=relative,
                            format=candidate.fmt,
                            bytes=candidate.size,
                            duration_ms=candidate.duration_ms,
                        )
                    )
                except StoreConflict as exc:
                    _LOGGER.warning("sound library: skipping %s: %s", relative, type(exc).__name__)
                    continue
                added += 1

            if unmappable:
                _LOGGER.warning(
                    "sound library: ignoring %d sound row(s) whose path is not a "
                    "relative path inside %s",
                    unmappable,
                    self._directory,
                )

            return ScanReport(
                added=added,
                removed=removed,
                missing=missing,
                total=len(rows) + added,
            )


def bundled_demo_sounds_dir() -> Path | None:
    """Return the bundled demo-sound directory, or ``None`` when absent.

    This is package-relative (``importlib.resources.files("chime_deck")``),
    unlike :func:`chime_deck.config.default_static_dir`, which walks up from
    its module with ``parents[2]`` to find the repository's ``web/dist``. The
    wheel ships ``src/chime_deck`` as one package (``packages`` in
    ``pyproject.toml``), so the demo sounds travel inside the installed package
    and must be found through the package, not through a repo layout.
    """
    resource = importlib.resources.files("chime_deck").joinpath("assets", "sounds")
    path = Path(str(resource))
    return path if path.is_dir() else None


def copy_bundled_demo_sounds(
    directory: Path,
    *,
    assets_dir: Path | None,
    known_paths: Collection[str] = (),
) -> tuple[str, ...]:
    """Copy bundled demos into *directory*, never overwriting anything.

    Copies are name-matched against *known_paths* as well as the destination
    directory. That means deleting a demo file but keeping its row leaves the
    row permanently broken (missing), because the row still knows the name and
    the seed will not resurrect it. Deleting both the row and the file lets the
    next boot seed it again. Nothing is ever overwritten, so a user's edited or
    replaced file is safe, and each copy lands through a unique hidden temp file
    plus :func:`os.replace` so a boot interrupted mid-copy leaves no partial
    demo in place.

    Only *mappable* row paths suppress a demo: each known path is normalised the
    same way the scanner normalises it and its basename is compared. An inert
    legacy row whose path is absolute (or otherwise unmappable) cannot block a
    real bundled demo, because no file inside the library could ever match it.

    :param directory: library directory to seed.
    :param assets_dir: bundled assets directory from :func:`bundled_demo_sounds_dir`.
    :param known_paths: row paths; only the basename of a mappable relative path
        is compared.
    :return: the file names copied, in sorted order.
    """
    if assets_dir is None or not assets_dir.is_dir():
        return ()
    known: set[str] = set()
    for path in known_paths:
        normalised = _normalise_stored_path(path)
        if normalised is not None:
            known.add(PurePosixPath(normalised).name)
    directory.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for entry in sorted(assets_dir.iterdir()):
        if not entry.is_file():
            continue
        is_audio = entry.suffix.lower().lstrip(".") in SUPPORTED_FORMATS
        if not is_audio and entry.name != LICENCE_FILENAME:
            continue
        if entry.name in known or (directory / entry.name).exists():
            continue
        temp = directory / f".{entry.name}.{uuid.uuid4().hex}.tmp"
        try:
            shutil.copyfile(entry, temp)
            os.replace(temp, directory / entry.name)
        finally:
            with suppress(OSError):
                temp.unlink()
        copied.append(entry.name)

    if copied:
        _LOGGER.info("seeded %d bundled demo file(s) into %s", len(copied), directory)
    return tuple(copied)
