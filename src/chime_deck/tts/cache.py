"""The ephemeral speech cache.

A TTS press resolves to one file keyed by engine, options and text. The cache
keeps that file under a flat directory named by the SHA-256 of the key, so a
repeated phrase — a doorbell's phrase never changes — replays instantly instead
of paying for synthesis on every press.

The cache is LRU by modification time and safe to delete at any time: every hit
touches the file's mtime, and a missing directory is simply a cold cache. A miss
renders to a hidden sibling and publishes with :func:`os.replace`, so readers
never observe a partial file.

Two limits keep a long-running device from filling its disk: at most
:data:`CACHE_MAX_FILES` files and :data:`CACHE_MAX_BYTES` bytes. Pruning also
runs on a miss, throttled to once a minute, because a UI template with a
timestamp can mint a fresh entry every minute and never hit at all.

Owning ticket: HSD-bw8h.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Final
from uuid import uuid4

from .engines import TtsOptions
from .errors import TtsRenderError

__all__ = [
    "CACHE_MAX_BYTES",
    "CACHE_MAX_FILES",
    "TtsCache",
    "cache_key",
    "suffix_for_format",
]

CACHE_MAX_FILES: Final = 200
"""Most cached clips kept on disk."""

CACHE_MAX_BYTES: Final = 200 * 1024 * 1024
"""Most bytes cached clips may occupy."""

_TEMP_MAX_AGE_S: Final = 3600.0
"""A hidden temporary file older than this is left over from a crash."""

_PRUNE_INTERVAL_S: Final = 60.0
"""Minimum gap between opportunistic prunes triggered by a miss."""

_FORMAT_PATTERN: Final = re.compile(r"[a-z0-9]{1,8}")
_SUFFIX_PATTERN: Final = re.compile(r"\.[a-z0-9]{1,8}")
_KEY_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")


def cache_key(engine: str, options: TtsOptions, text: str) -> str:
    """Return the cache key for one render.

    The key is the UTF-8 SHA-256 hex digest of a canonical JSON document
    ``{"engine": engine, "options": asdict(options), "text": text}`` serialised
    with sorted keys, ``(",":":")`` separators and ``ensure_ascii=False``. The
    canonicalisation is pinned so a key written by one version of this module
    keeps hitting in the next.

    :param engine: engine *name*, for example ``"espeak_ng"``.
    :param options: sub-engine options for this render.
    :param text: exact phrase, already stripped by the resolver.
    """
    payload = json.dumps(
        {"engine": engine, "options": dataclasses.asdict(options), "text": text},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def suffix_for_format(preferred_format: str) -> str:
    """Return the dotted file suffix for a preferred audio format.

    The token is lowercased, trimmed and stripped of leading dots; an empty
    token means ``"wav"``. The token must be 1-8 characters of ``[a-z0-9]``,
    which also makes this function the path-traversal guard: ``"../x"`` and
    ``"a/b"`` cannot become a file name.

    :param preferred_format: format token such as ``"wav"`` or ``"MP3"``.
    :raises ValueError: if the token is not a short alphanumeric format name.
    """
    fmt = preferred_format.strip().lower().lstrip(".")
    if not fmt:
        fmt = "wav"
    if _FORMAT_PATTERN.fullmatch(fmt) is None:
        raise ValueError(
            f"unsupported audio format {preferred_format!r}; "
            "expected a short alphanumeric token such as 'wav'"
        )
    return f".{fmt}"


class TtsCache:
    """A bounded, delete-anytime directory of rendered speech files."""

    def __init__(
        self,
        root: Path,
        *,
        max_files: int = CACHE_MAX_FILES,
        max_bytes: int = CACHE_MAX_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the cache and its directory.

        :param root: directory holding the clips; created when missing.
        :param max_files: most files kept after pruning.
        :param max_bytes: most bytes kept after pruning.
        :param clock: wall clock used for age checks; injectable for tests.
        """
        self._root = root
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._clock = clock
        self._last_prune = float("-inf")
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """Directory holding the cached clips."""
        return self._root

    def path_for(self, key: str, suffix: str) -> Path:
        """Return the final path for *key* and *suffix*.

        Both parts are validated here, so no caller can escape :attr:`root`: the
        key must be 64 lowercase hex characters and the suffix a dotted 1-8
        character alphanumeric token.

        :param key: cache key from :func:`cache_key`.
        :param suffix: dotted suffix from :func:`suffix_for_format`.
        :raises ValueError: if the key or suffix is not in canonical form.
        """
        if _KEY_PATTERN.fullmatch(key) is None:
            raise ValueError(f"invalid speech cache key {key!r}; expected 64 lowercase hex digits")
        normalised = suffix.lower()
        if _SUFFIX_PATTERN.fullmatch(normalised) is None:
            raise ValueError(
                f"unsupported audio suffix {suffix!r}; expected a format token such as '.wav'"
            )
        return self._root / f"{key}{normalised}"

    def lookup(self, key: str, suffix: str) -> Path | None:
        """Return a cached clip, or ``None`` on a miss.

        A zero-byte stub is a miss: it can only be the remains of an interrupted
        write. A hit touches the file's mtime so LRU pruning keeps it hot.

        :param key: cache key from :func:`cache_key`.
        :param suffix: dotted suffix from :func:`suffix_for_format`.
        """
        path = self.path_for(key, suffix)
        try:
            stat = path.stat()
        except OSError:
            return None
        if not path.is_file() or stat.st_size <= 0:
            return None
        with suppress(OSError):
            os.utime(path, None)
        return path

    async def get_or_render(
        self,
        key: str,
        suffix: str,
        render: Callable[[Path], Awaitable[None]],
    ) -> Path:
        """Return the cached clip, rendering it first on a miss.

        The renderer writes at the temporary path it is given; the file is
        verified and published with :func:`os.replace` only after it returns. A
        failure removes the temporary file and leaves any previously published
        clip untouched.

        :param key: cache key from :func:`cache_key`.
        :param suffix: dotted suffix from :func:`suffix_for_format`.
        :param render: callback that writes the clip at the path it receives.
        :raises TtsRenderError: if the renderer produced no output.
        """
        final = self.path_for(key, suffix)
        hit = self.lookup(key, suffix)
        if hit is not None:
            return hit
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = self._root / f".{key}.{uuid4().hex}.tmp{suffix}"
        try:
            await render(tmp)
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise TtsRenderError("speech renderer produced no output")
            os.replace(tmp, final)
        finally:
            with suppress(FileNotFoundError):
                tmp.unlink()
        self._maybe_prune()
        return final

    def prune(self) -> int:
        """Delete stale temporary files and the oldest clips over the limits.

        Hidden names starting ``"."`` are temporary files: they are removed only
        when older than :data:`_TEMP_MAX_AGE_S` and are never counted towards
        the file or byte limits. Normal files are ordered oldest first and
        deleted until both limits hold. Missing files are tolerated between the
        scan and the unlink.

        :returns: number of files removed, temporary files included.
        """
        now = self._clock()
        try:
            scan = list(self._root.iterdir())
        except FileNotFoundError:
            return 0
        entries: list[tuple[float, int, Path]] = []
        total = 0
        removed = 0
        for path in scan:
            with suppress(FileNotFoundError):
                stat = path.stat()
                if path.name.startswith("."):
                    if now - stat.st_mtime > _TEMP_MAX_AGE_S:
                        with suppress(FileNotFoundError):
                            path.unlink()
                            removed += 1
                    continue
                if not path.is_file():
                    continue
                entries.append((stat.st_mtime, stat.st_size, path))
                total += stat.st_size
        entries.sort(key=lambda entry: entry[0])
        while entries and (len(entries) > self._max_files or total > self._max_bytes):
            _mtime, size, path = entries.pop(0)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            else:
                removed += 1
            total -= size
        return removed

    def _maybe_prune(self) -> None:
        """Prune at most once per :data:`_PRUNE_INTERVAL_S` after a miss."""
        now = self._clock()
        if now - self._last_prune < _PRUNE_INTERVAL_S:
            return
        self._last_prune = now
        self.prune()
