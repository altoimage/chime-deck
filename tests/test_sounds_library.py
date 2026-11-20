"""Sound library scanning, missing-row handling and demo seeding.

Every test uses a temporary directory, a temporary SQLite store and a fake
duration probe. No file is probed with a real binary and no network is touched.

Owning ticket: HSD-cwvc.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from chime_deck.sounds import ScanReport
from chime_deck.sounds.library import (
    LICENCE_FILENAME,
    SoundLibrary,
    copy_bundled_demo_sounds,
)
from chime_deck.store import (
    ConfigEvent,
    ConfigStore,
    Sound,
    SoundIn,
    StoreConflict,
)

_LIBRARY_LOGGER = "chime_deck.sounds.library"


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


class RecordingProbe:
    """A duration probe that records calls and returns a fixed value."""

    def __init__(self, duration_ms: int | None = 500) -> None:
        """Create the probe.

        :param duration_ms: value every call returns.
        """
        self.duration_ms = duration_ms
        self.calls: list[Path] = []

    def __call__(self, path: Path) -> int | None:
        """Record the path and return the configured duration.

        :param path: file being probed.
        """
        self.calls.append(path)
        return self.duration_ms


def _drain(queue: asyncio.Queue[ConfigEvent]) -> list[ConfigEvent]:
    """Return and clear every event currently queued.

    :param queue: queue subscribed to the store's event bus.
    """
    events: list[ConfigEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def _write(directory: Path, name: str, payload: bytes) -> Path:
    """Write *payload* to *directory*/*name* and return the path.

    :param directory: destination directory.
    :param name: file name, possibly with subdirectories.
    :param payload: bytes to write.
    """
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


async def _rows(store: ConfigStore) -> list[Sound]:
    """Return every sound row ordered by id.

    :param store: connected store.
    """
    return await store.list_sounds()


@pytest.mark.asyncio
async def test_missing_directory_is_created_and_empty(store: ConfigStore, tmp_path: Path) -> None:
    """A library whose directory does not exist scans cleanly and creates it."""
    directory = tmp_path / "new-sounds"
    library = SoundLibrary(store, directory, probe=RecordingProbe())

    report = await library.scan()

    assert report == ScanReport(added=0, removed=0, missing=0, total=0)
    assert directory.is_dir()
    assert await _rows(store) == []


@pytest.mark.asyncio
async def test_new_file_is_indexed_and_second_scan_is_a_no_op(
    store: ConfigStore, tmp_path: Path
) -> None:
    """A supported file gets one row; an unchanged rescan writes nothing."""
    directory = tmp_path / "sounds"
    directory.mkdir()
    payload = b"RIFF....WAVEfmt "
    _write(directory, "ding.wav", payload)
    probe = RecordingProbe(500)
    library = SoundLibrary(store, directory, probe=probe)
    queue = store.events.subscribe_queue()

    report = await library.scan()

    assert report == ScanReport(added=1, removed=0, missing=0, total=1)
    rows = await _rows(store)
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "ding"
    assert row.source_type == "file"
    assert row.path == "ding.wav"
    assert row.format == "wav"
    assert row.bytes == len(payload)
    assert row.duration_ms == 500
    assert row.sha256 is None
    assert row.missing is False
    assert probe.calls == [directory / "ding.wav"]
    _drain(queue)

    second = await library.scan()

    assert second == ScanReport(added=0, removed=0, missing=0, total=1)
    assert await _rows(store) == rows
    assert _drain(queue) == []
    assert len(probe.calls) == 1


@pytest.mark.asyncio
async def test_filters_extension_case_hidden_and_in_progress(
    store: ConfigStore, tmp_path: Path
) -> None:
    """Uppercase extensions index; unsupported, hidden and partial files do not."""
    directory = tmp_path / "sounds"
    directory.mkdir()
    _write(directory, "UP.MP3", b"ID3")
    _write(directory, "notes.txt", b"hello")
    _write(directory, "voice.m4a", b"m4a")
    _write(directory, ".ding.wav", b"hidden")
    _write(directory, "ding.mp3.part", b"partial")
    _write(directory, "song.ogg.tmp", b"temporary")
    _write(directory, ".cache/nested.wav", b"hidden directory")
    library = SoundLibrary(store, directory, probe=RecordingProbe())

    report = await library.scan()

    assert report.added == 1
    rows = await _rows(store)
    assert [(row.name, row.path, row.format) for row in rows] == [("UP", "UP.MP3", "mp3")]


@pytest.mark.asyncio
async def test_deleted_file_is_marked_missing_and_reappearing_clears_it(
    store: ConfigStore, tmp_path: Path
) -> None:
    """A vanished file is flagged, never deleted; re-adding it clears the flag."""
    directory = tmp_path / "sounds"
    directory.mkdir()
    path = _write(directory, "ding.wav", b"one")
    library = SoundLibrary(store, directory, probe=RecordingProbe())

    await library.scan()
    path.unlink()
    report = await library.scan()

    assert report == ScanReport(added=0, removed=1, missing=1, total=1)
    row = (await _rows(store))[0]
    assert row.missing is True

    _write(directory, "ding.wav", b"one")
    report = await library.scan()

    assert report == ScanReport(added=0, removed=0, missing=0, total=1)
    row = (await _rows(store))[0]
    assert row.missing is False


@pytest.mark.asyncio
async def test_vanished_directory_marks_every_row_missing(
    store: ConfigStore, tmp_path: Path
) -> None:
    """Deleting the whole directory is just every file being missing."""
    directory = tmp_path / "sounds"
    directory.mkdir()
    _write(directory, "a.wav", b"a")
    _write(directory, "b.mp3", b"b")
    library = SoundLibrary(store, directory, probe=RecordingProbe())
    await library.scan()

    shutil.rmtree(directory)
    report = await library.scan()

    assert directory.is_dir()
    assert report == ScanReport(added=0, removed=2, missing=2, total=2)


@pytest.mark.asyncio
async def test_url_row_with_same_path_is_not_duplicated(store: ConfigStore, tmp_path: Path) -> None:
    """An existing URL row whose file appears is updated, not inserted again."""
    directory = tmp_path / "sounds"
    directory.mkdir()
    _write(directory, "ding.wav", b"data")
    await store.create_sound(
        SoundIn(name="Ding", source_type="url", path="ding.wav", url="https://x/y")
    )
    library = SoundLibrary(store, directory, probe=RecordingProbe())

    report = await library.scan()

    assert report == ScanReport(added=0, removed=0, missing=0, total=1)
    assert len(await _rows(store)) == 1


@pytest.mark.asyncio
async def test_url_row_without_path_is_silently_skipped(
    store: ConfigStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A remote-only row is not a missing file and warns about nothing."""
    directory = tmp_path / "sounds"
    await store.create_sound(SoundIn(name="Remote", source_type="url", url="https://x/y.mp3"))
    library = SoundLibrary(store, directory, probe=RecordingProbe())
    caplog.set_level(logging.WARNING, logger=_LIBRARY_LOGGER)

    report = await library.scan()

    assert report == ScanReport(added=0, removed=0, missing=0, total=1)
    assert (await _rows(store))[0].missing is False
    assert caplog.records == []


@pytest.mark.asyncio
async def test_file_row_without_path_is_marked_missing(store: ConfigStore, tmp_path: Path) -> None:
    """A file row with no path can never be found, so it is flagged."""
    directory = tmp_path / "sounds"
    await store.create_sound(SoundIn(name="Ghost", source_type="file"))
    library = SoundLibrary(store, directory, probe=RecordingProbe())

    report = await library.scan()

    assert report == ScanReport(added=0, removed=1, missing=1, total=1)
    assert (await _rows(store))[0].missing is True


@pytest.mark.asyncio
async def test_absolute_row_path_is_never_touched(
    store: ConfigStore,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An absolute path is left alone, keeps its flag and warns exactly once."""
    directory = tmp_path / "sounds"
    directory.mkdir()
    outside = _write(tmp_path, "outside.wav", b"outside")
    row = await store.create_sound(SoundIn(name="Outside", source_type="file", path=str(outside)))
    library = SoundLibrary(store, directory, probe=RecordingProbe())
    caplog.set_level(logging.WARNING, logger=_LIBRARY_LOGGER)

    report = await library.scan()

    assert report == ScanReport(added=0, removed=0, missing=0, total=1)
    unchanged = await store.get_sound(row.id)
    assert unchanged.path == str(outside)
    assert unchanged.missing is False
    warnings = caplog.records
    assert len(warnings) == 1
    assert "not a relative path" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_scan_never_rewrites_url_identity_fields(store: ConfigStore, tmp_path: Path) -> None:
    """Name, URL, hash, path and source type survive a metadata refresh."""
    directory = tmp_path / "sounds"
    directory.mkdir()
    _write(directory, "ding.wav", b"data")
    original = await store.create_sound(
        SoundIn(
            name="Custom name",
            source_type="url",
            path="ding.wav",
            url="https://example.invalid/ding.wav",
            sha256="a" * 64,
        )
    )
    library = SoundLibrary(store, directory, probe=RecordingProbe(700))

    await library.scan()

    row = await store.get_sound(original.id)
    assert (row.name, row.source_type, row.path, row.url, row.sha256) == (
        "Custom name",
        "url",
        "ding.wav",
        "https://example.invalid/ding.wav",
        "a" * 64,
    )
    assert row.bytes == 4
    assert row.format == "wav"
    assert row.duration_ms == 700


@pytest.mark.asyncio
async def test_size_change_refreshes_metadata(store: ConfigStore, tmp_path: Path) -> None:
    """A replaced file of a different size is reprobed and its bytes updated."""
    directory = tmp_path / "sounds"
    directory.mkdir()
    path = _write(directory, "ding.wav", b"abc")
    probe = RecordingProbe(300)
    library = SoundLibrary(store, directory, probe=probe)
    await library.scan()

    path.write_bytes(b"abcdef")
    probe.duration_ms = 600
    report = await library.scan()

    row = (await _rows(store))[0]
    assert report.added == 0
    assert row.bytes == 6
    assert row.duration_ms == 600
    assert len(probe.calls) == 2


@pytest.mark.asyncio
async def test_store_conflict_on_one_file_does_not_stop_the_scan(
    store: ConfigStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One conflicting insert is logged and the remaining files still index."""
    directory = tmp_path / "sounds"
    directory.mkdir()
    _write(directory, "a.wav", b"a")
    _write(directory, "b.wav", b"b")
    original = ConfigStore.create_sound
    calls = {"count": 0}

    async def create_sound(self: ConfigStore, sound: SoundIn) -> Sound:
        """Fail the first insert and delegate the rest.

        :param self: store the method was patched on.
        :param sound: write input.
        """
        calls["count"] += 1
        if calls["count"] == 1:
            raise StoreConflict("simulated race")
        return await original(self, sound)

    monkeypatch.setattr(ConfigStore, "create_sound", create_sound)
    library = SoundLibrary(store, directory, probe=RecordingProbe())
    caplog.set_level(logging.WARNING, logger=_LIBRARY_LOGGER)

    report = await library.scan()

    assert report.added == 1
    assert len(await _rows(store)) == 1
    warnings = caplog.records
    assert len(warnings) == 1
    assert "skipping" in warnings[0].getMessage()
    assert "StoreConflict" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_concurrent_scans_create_only_one_row(store: ConfigStore, tmp_path: Path) -> None:
    """The write lock serialises two scans and the second sees the first's row."""
    directory = tmp_path / "sounds"
    directory.mkdir()
    _write(directory, "ding.wav", b"data")
    library = SoundLibrary(store, directory, probe=RecordingProbe())

    reports = await asyncio.gather(library.scan(), library.scan())

    assert len(await _rows(store)) == 1
    assert sum(report.added for report in reports) == 1
    assert all(report.total == 1 for report in reports)


@pytest.mark.asyncio
async def test_budget_exhaustion_indexes_files_without_probing(
    store: ConfigStore, tmp_path: Path
) -> None:
    """A zero budget starts no probe and leaves both durations unknown."""
    directory = tmp_path / "sounds"
    directory.mkdir()
    _write(directory, "a.wav", b"a")
    _write(directory, "b.wav", b"b")
    probe = RecordingProbe(500)
    library = SoundLibrary(store, directory, probe=probe)

    report = await library.scan(budget_s=0.0)

    assert report.added == 2
    assert probe.calls == []
    assert [row.duration_ms for row in await _rows(store)] == [None, None]


def _assets(tmp_path: Path) -> Path:
    """Create a small fake bundled-assets directory.

    :param tmp_path: pytest temporary directory.
    """
    assets = tmp_path / "bundled"
    assets.mkdir()
    (assets / "demo_ding.wav").write_bytes(b"bundled wav")
    (assets / "demo_dong.mp3").write_bytes(b"bundled mp3")
    (assets / LICENCE_FILENAME).write_text("CC0 1.0 Universal", encoding="utf-8")
    (assets / "README.txt").write_text("not a sound", encoding="utf-8")
    return assets


def test_seed_copies_only_when_absent_and_never_overwrites(
    tmp_path: Path,
) -> None:
    """Seeding copies audio and the licence once, leaving existing bytes intact."""
    assets = _assets(tmp_path)
    directory = tmp_path / "sounds"
    directory.mkdir()
    sentinel = _write(directory, "demo_ding.wav", b"user replaced this")
    source_before = {path.name: path.read_bytes() for path in assets.iterdir()}

    copied = copy_bundled_demo_sounds(directory, assets_dir=assets)

    assert set(copied) == {"demo_dong.mp3", LICENCE_FILENAME}
    assert sentinel.read_bytes() == b"user replaced this"
    assert (directory / "demo_dong.mp3").read_bytes() == b"bundled mp3"
    assert (directory / LICENCE_FILENAME).read_text(encoding="utf-8").startswith("CC0")
    assert {path.name: path.read_bytes() for path in assets.iterdir()} == source_before
    assert copy_bundled_demo_sounds(directory, assets_dir=assets) == ()


def test_seed_skips_known_rows_by_basename(tmp_path: Path) -> None:
    """A known relative row path suppresses the same-named demo by basename."""
    assets = _assets(tmp_path)
    directory = tmp_path / "sounds"

    copied = copy_bundled_demo_sounds(
        directory,
        assets_dir=assets,
        known_paths=["nested/demo_ding.wav"],
    )

    assert "demo_ding.wav" not in copied
    assert not (directory / "demo_ding.wav").exists()
    assert "demo_dong.mp3" in copied


def test_absolute_row_path_does_not_suppress_a_demo(tmp_path: Path) -> None:
    """An inert legacy absolute row cannot permanently block a real demo."""
    assets = _assets(tmp_path)
    directory = tmp_path / "sounds"

    copied = copy_bundled_demo_sounds(
        directory,
        assets_dir=assets,
        known_paths=[str(tmp_path / "elsewhere" / "demo_ding.wav")],
    )

    assert "demo_ding.wav" in copied
    assert (directory / "demo_ding.wav").read_bytes() == b"bundled wav"


def test_unmappable_row_does_not_suppress_a_demo(tmp_path: Path) -> None:
    """A Windows-style path is not a basename match on POSIX."""
    assets = _assets(tmp_path)
    directory = tmp_path / "sounds"

    copied = copy_bundled_demo_sounds(
        directory,
        assets_dir=assets,
        known_paths=["C:\\sounds\\demo_ding.wav"],
    )

    assert "demo_ding.wav" in copied
    assert (directory / "demo_ding.wav").exists()


def test_seed_without_assets_is_a_no_op(tmp_path: Path) -> None:
    """No bundled directory means nothing to seed and no directory is made."""
    directory = tmp_path / "sounds"

    assert copy_bundled_demo_sounds(directory, assets_dir=None) == ()
    assert not directory.exists()
