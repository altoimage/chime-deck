"""Durable pre-rendered speech, one artefact per static phrase.

A phrase with no Jinja syntax can never render to anything else, so it is baked
exactly once into ``<data_dir>/tts/baked/<button_id>/<sha>.<ext>`` and replayed
from there for the life of the button (design 04 §2.1, D21). Beside it sits a
``<sha>.json`` sidecar recording the engine, the canonical engine options and the
real container, so the directory is self-describing after a restart.

The digest is :func:`~chime_deck.tts.cache.cache_key`'s, reused rather than
re-derived: the bake directory and the ephemeral cache must agree on one
canonicalisation, or a phrase would hash differently in each.

Unlike the cache, nothing here is LRU-pruned: a bake is only replaced when the
text, the engine or an option changes, and only deleted when the button is
deleted or the directory is orphaned. :class:`BakeSweeper` retries every missing
or stale bake at most once per boot, jittered so a fleet of doorbells does not
stampede a cloud endpoint (design 04 §2.4).

Owning ticket: HSD-dzek.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import random
import re
import shutil
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol
from uuid import uuid4

from ..store import Button
from ..store.store import utcnow_iso
from .cache import cache_key
from .engines import TtsOptions, sniff_container
from .errors import TtsRenderError
from .providers import has_template_syntax

if TYPE_CHECKING:
    from .speech import TtsSpeechSource

__all__ = [
    "BAKED_DIRNAME",
    "JITTER_MAX_S",
    "JITTER_MIN_S",
    "BakeArtefact",
    "BakeBatch",
    "BakeFailure",
    "BakeStore",
    "BakeSweepReport",
    "BakeSweeper",
    "BakeUsage",
    "ButtonReader",
    "baked_root",
    "directory_usage",
    "expected_bake_sha",
    "is_static",
]

BAKED_DIRNAME: Final = ("tts", "baked")
"""Path segments of the bake root under the data directory (design 01 §1)."""

_SHA_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
"""The only file-name shape a digest may take; this is the path-traversal guard."""

_FORMAT_PATTERN: Final = re.compile(r"[a-z0-9]{1,8}")
"""Container token a sidecar may name, matching a cache suffix."""


def baked_root(data_dir: Path) -> Path:
    """Return ``<data_dir>/tts/baked``, the durable bake directory.

    :param data_dir: the install's data directory.
    """
    return data_dir.joinpath(*BAKED_DIRNAME)


def is_static(text: str) -> bool:
    """Return whether *text* can be pre-rendered and replayed verbatim.

    A phrase with no Jinja syntax can never render to anything else (design 04
    §2.1), so it is baked once instead of synthesised on every press.

    :param text: stripped TTS phrase.
    """
    return not has_template_syntax(text)


def expected_bake_sha(engine: str, options: TtsOptions, text: str) -> str:
    """Return the digest the current settings ask a bake to have.

    Reusing :func:`~chime_deck.tts.cache.cache_key` is deliberate: there is
    one canonicalisation, so the digest is identical across restarts and between
    the cache and the bake directory, and it is never reimplemented. A phrase
    whose digest differs from the artefact on disk is stale, and the press path
    re-bakes it.

    :param engine: engine *name*, for example ``"espeak_ng"``.
    :param options: sub-engine options for this render.
    :param text: exact static phrase, already stripped.
    """
    return cache_key(engine, options, text)


def directory_usage(root: Path) -> tuple[int, int]:
    """Return how many files *root* holds and how many bytes they occupy.

    A missing directory is ``(0, 0)``: a dynamic-only install has no bake root at
    all, and asking about it must not be an error. Files that vanish between the
    scan and the stat are skipped rather than counted.

    :param root: directory to measure; walked recursively.
    """
    files = 0
    total = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        files += 1
        total += size
    return files, total


@dataclass(frozen=True, slots=True)
class BakeArtefact:
    """One durable pre-rendered clip and the facts its sidecar records."""

    sha: str
    path: Path
    format: str
    bytes: int


@dataclass(frozen=True, slots=True)
class BakeFailure:
    """One button whose phrase could not be pre-rendered, and why."""

    button_id: int
    error: str


@dataclass(frozen=True, slots=True)
class BakeBatch:
    """What one :meth:`~TtsSpeechSource.bake_all` run did, button id by id.

    Every field defaults to empty so a caller can start from a plain constructor
    and grow the tuple it owns, or build one with :func:`dataclasses.replace`.
    """

    baked: tuple[int, ...] = ()
    skipped_dynamic: tuple[int, ...] = ()
    skipped_current: tuple[int, ...] = ()
    failed: tuple[BakeFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class BakeUsage:
    """Disk occupied by the durable bakes and, for comparison, the cache."""

    baked_files: int
    baked_bytes: int
    cache_files: int
    cache_bytes: int


@dataclass(frozen=True, slots=True)
class BakeSweepReport:
    """What one startup sweep did: orphan directories removed, then the batch."""

    orphans_removed: int
    batch: BakeBatch


class BakeStore:
    """The delete-anytime directory of durable pre-rendered clips.

    One directory per button, named by its id, holding the current artefact and
    its sidecar. ``__init__`` deliberately does **not** create the root: an
    install with no static phrase must leave no ``baked/`` on disk, which
    ``tests/test_tts_templating.py::test_p_a_templated_press_bakes_nothing``
    pins.

    One :class:`asyncio.Lock` guards :meth:`write`, :meth:`discard` and
    :meth:`sweep`, so the startup sweep can never race an in-flight bake (design
    01 §3.6). A bake holds the lock across the engine render on purpose: the
    engines serialise synthesis anyway, and a sweep waiting behind one render is
    cheaper than a directory vanishing under a writer.
    """

    def __init__(self, root: Path, *, clock: Callable[[], str] = utcnow_iso) -> None:
        """Create the store without touching the filesystem.

        :param root: bake root, normally :func:`baked_root` of the data directory.
        :param clock: sidecar ``created_at`` source; injectable for tests.
        """
        self._root = root
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        """Directory holding one subdirectory per baked button."""
        return self._root

    def path_for(self, button_id: int, sha: str) -> Path | None:
        """Return the artefact for *sha*, or ``None`` when it is not on disk.

        The sidecar's ``format`` field is preferred, so the real container wins
        over the requested suffix; a corrupt sidecar falls back to the single
        non-``.json`` sibling called ``<sha>.*``. The digest is validated here,
        which is the path-traversal guard: no caller can name a directory or a
        file outside this button's own directory.

        :param button_id: database id of the button.
        :param sha: digest from :func:`expected_bake_sha`.
        :raises ValueError: if *sha* is not 64 lowercase hex digits.
        """
        _check_sha(sha)
        directory = self._root / str(button_id)
        if not directory.is_dir():
            return None
        sidecar = directory / f"{sha}.json"
        try:
            recorded = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            recorded = None
        if isinstance(recorded, dict):
            fmt = recorded.get("format")
            if isinstance(fmt, str) and _FORMAT_PATTERN.fullmatch(fmt) is not None:
                candidate = directory / f"{sha}.{fmt}"
                if candidate.is_file():
                    return candidate
        matches = [
            path for path in directory.glob(f"{sha}.*") if path.is_file() and path.suffix != ".json"
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    async def write(
        self,
        *,
        button_id: int,
        sha: str,
        render: Callable[[Path], Awaitable[None]],
        engine: str,
        options: TtsOptions,
        text: str,
    ) -> BakeArtefact:
        """Render one phrase to a temporary file and publish it as the artefact.

        The clip is written to a hidden sibling and published with
        :func:`os.replace`, so a reader never observes a partial file. The real
        container is sniffed from the leading bytes and becomes the file's
        suffix: engines answer with what they have, so a ``.wav`` request may
        still produce MP3 and is never transcoded (design 04 §2.1, D25).
        Any artefact other than *sha* is then pruned, which is what makes a
        changed text, engine or option delete the old bake.

        The engines own the audible-output check —
        :func:`~chime_deck.tts.engines.validate_rendered_audio` is
        deliberately not called here: an engine that produced silence has
        already rejected its own render, and a non-WAV container cannot be
        measured without a decoder, so the leading bytes are the only fact this
        layer can honestly check.

        :param button_id: database id of the button.
        :param sha: digest from :func:`expected_bake_sha`.
        :param render: callback that writes the clip at the path it receives.
        :param engine: engine name recorded in the sidecar.
        :param options: engine options recorded in the sidecar.
        :param text: exact static phrase recorded in the sidecar.
        :raises ValueError: if *sha* is not 64 lowercase hex digits.
        :raises TtsRenderError: if the render produced no audio or bytes that are
            not a recognised audio container.
        """
        _check_sha(sha)
        async with self._lock:
            directory = self._root / str(button_id)
            directory.mkdir(parents=True, exist_ok=True)
            temp = directory / f".{sha}.{uuid4().hex}.tmp"
            try:
                await render(temp)
                size = temp.stat().st_size if temp.is_file() else 0
                if size == 0:
                    raise TtsRenderError(f"{engine} produced no audio for this phrase")
                with temp.open("rb") as handle:
                    container = sniff_container(handle.read(12))
                if container is None:
                    raise TtsRenderError(
                        f"{engine} produced bytes that are not a recognised audio container, "
                        "so they cannot be saved as a pre-rendered clip"
                    )
                final = directory / f"{sha}.{container}"
                os.replace(temp, final)
            finally:
                with suppress(FileNotFoundError):
                    temp.unlink()
            self._write_sidecar(
                directory / f"{sha}.json",
                {
                    "sha256": sha,
                    "engine": engine,
                    "engine_options": dataclasses.asdict(options),
                    "text": text,
                    "format": container,
                    "bytes": size,
                    "created_at": self._clock(),
                },
            )
            self._prune_others(directory, sha)
        return BakeArtefact(sha=sha, path=final, format=container, bytes=size)

    async def discard(self, button_id: int) -> int:
        """Delete one button's bake directory and everything in it.

        :param button_id: database id of the button.
        :returns: number of files removed; ``0`` when the directory is absent.
        """
        async with self._lock:
            directory = self._root / str(button_id)
            removed, _bytes = directory_usage(directory)
            shutil.rmtree(directory, ignore_errors=True)
            return removed

    async def sweep(self, live_ids: Iterable[int]) -> int:
        """Delete every bake directory whose button no longer exists.

        Only entries whose name parses as an integer are candidates; anything
        else is left alone, because a stray non-numeric entry was not created by
        this store and is not this store's to remove.

        :param live_ids: ids of the buttons that still exist.
        :returns: number of directories removed; ``0`` when the root is absent.
        """
        live = set(live_ids)
        async with self._lock:
            if not self._root.is_dir():
                return 0
            removed = 0
            for entry in self._root.iterdir():
                if not entry.is_dir():
                    continue
                try:
                    button_id = int(entry.name)
                except ValueError:
                    continue
                if button_id in live:
                    continue
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            return removed

    def usage(self, cache_root: Path | None = None) -> BakeUsage:
        """Return the disk footprint of the bakes and, optionally, the cache.

        Both roots are walked recursively: a bake directory is a tree, so the
        recursive walk is already needed, and reusing it for the flat cache
        directory costs nothing.

        :param cache_root: the ephemeral cache root to measure, or ``None``.
        """
        baked_files, baked_bytes = directory_usage(self._root)
        if cache_root is None:
            return BakeUsage(
                baked_files=baked_files,
                baked_bytes=baked_bytes,
                cache_files=0,
                cache_bytes=0,
            )
        cache_files, cache_bytes = directory_usage(cache_root)
        return BakeUsage(
            baked_files=baked_files,
            baked_bytes=baked_bytes,
            cache_files=cache_files,
            cache_bytes=cache_bytes,
        )

    def _write_sidecar(self, path: Path, payload: dict[str, object]) -> None:
        """Publish *payload* at *path* with a temporary file and ``os.replace``.

        :param path: sidecar file to write.
        :param payload: JSON-serialisable sidecar contents.
        """
        temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temp, path)
        finally:
            with suppress(FileNotFoundError):
                temp.unlink()

    @staticmethod
    def _prune_others(directory: Path, sha: str) -> None:
        """Remove every artefact and sidecar in *directory* except *sha*'s.

        This is the "changing the text, the engine or an option deletes the old
        bake" rule: a new digest is written first, so a failed render leaves the
        previous artefact playable.

        :param directory: the button's bake directory.
        :param sha: digest of the artefact that must survive.
        """
        for path in directory.iterdir():
            if path.stem == sha or path.is_dir():
                continue
            with suppress(FileNotFoundError):
                path.unlink()


def _check_sha(sha: str) -> None:
    """Refuse a digest that could escape a bake directory.

    :param sha: candidate digest.
    :raises ValueError: if *sha* is not 64 lowercase hex digits.
    """
    if _SHA_PATTERN.fullmatch(sha) is None:
        raise ValueError(f"invalid bake digest {sha!r}; expected 64 lowercase hex digits")


JITTER_MIN_S: Final = 2.0
"""Shortest delay before the once-per-boot bake sweep."""

JITTER_MAX_S: Final = 15.0
"""Longest delay before the once-per-boot bake sweep."""


class ButtonReader(Protocol):
    """The narrow button port the sweep needs, satisfied by ``ConfigStore``."""

    async def list_buttons(self) -> Sequence[Button]:
        """Return every configured button."""
        ...


class BakeSweeper:
    """Retry every missing or stale bake, exactly once per boot.

    Design 04 §2.4 asks for one jittered retry at startup and no more: a fleet of
    doorbells rebooting together must not stampede a cloud TTS endpoint, and a
    phrase that fails to bake is re-attempted by the next press anyway. ``sleep``
    and ``rng`` are injectable, so a test observes the delay without waiting for
    it and the loop reaches its end deterministically.
    """

    def __init__(
        self,
        *,
        store: ButtonReader,
        speech: TtsSpeechSource,
        bakes: BakeStore,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        min_delay_s: float = JITTER_MIN_S,
        max_delay_s: float = JITTER_MAX_S,
    ) -> None:
        """Create the sweeper without starting it.

        :param store: reads the buttons that still exist.
        :param speech: resolves and bakes phrases; its ``bake_all`` is the retry.
        :param bakes: bake store whose orphan directories are removed.
        :param sleep: delay function; injectable for tests.
        :param rng: randomness source for the jitter; ``None`` builds a fresh one.
        :param min_delay_s: shortest delay before the sweep.
        :param max_delay_s: longest delay before the sweep.
        """
        self._store = store
        self._speech = speech
        self._bakes = bakes
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()
        self._min_delay_s = min_delay_s
        self._max_delay_s = max_delay_s
        self._task: asyncio.Task[None] | None = None
        self._logger = logging.getLogger(__name__)

    async def start(self) -> None:
        """Start the one-shot sweep in the background; idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="tts-bake-sweep")

    async def stop(self) -> None:
        """Cancel the sweep and wait for it; safe on a never-started sweeper."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def run_once(self) -> BakeSweepReport:
        """Sweep orphaned directories, then retry missing and stale bakes.

        No sleeping: the jitter belongs to :meth:`_run`, so a caller may also use
        this as an explicit "bake everything now" action.

        :raises SpeechUnavailable: if speech is deliberately switched off (D27);
            there is nothing to bake and no fallback chain to walk.
        """
        buttons = await self._store.list_buttons()
        orphans = await self._bakes.sweep(button.id for button in buttons)
        batch = await self._speech.bake_all(buttons)
        self._logger.info(
            "TTS bake sweep: %d orphan director%s removed, %d phrase(s) baked, "
            "%d dynamic and %d current skipped, %d failed",
            orphans,
            "y" if orphans == 1 else "ies",
            len(batch.baked),
            len(batch.skipped_dynamic),
            len(batch.skipped_current),
            len(batch.failed),
        )
        return BakeSweepReport(orphans_removed=orphans, batch=batch)

    async def _run(self) -> None:
        """Wait one jittered delay, then sweep once and log any failure.

        The delay is drawn once and the sweep never repeats: design 04 §2.4 makes
        the startup retry a one-shot, not a poll. A failure is logged rather than
        raised, because nobody awaits this task and an unobserved exception would
        die silently; a cancellation is not caught, so :meth:`stop` still works.
        """
        delay = self._rng.uniform(self._min_delay_s, self._max_delay_s)
        await self._sleep(delay)
        try:
            await self.run_once()
        except Exception:
            self._logger.warning("the TTS bake sweep failed", exc_info=True)
