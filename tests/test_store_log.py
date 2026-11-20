"""Press log insertion, ordering and 1000-row retention."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from chime_deck.store import (
    PRESS_LOG_RETENTION,
    ConfigStore,
    PressLogIn,
    StoreConflict,
    ValidationError,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ConfigStore]:
    """Yield a connected store backed by a temporary database.

    :param tmp_path: pytest temporary directory.
    """
    async with ConfigStore(tmp_path / "doorbell.db") as instance:
        yield instance


async def test_insert_and_newest_first(store: ConfigStore) -> None:
    """Entries come back newest first and accept a default timestamp."""
    await store.log_press(
        PressLogIn(
            button_id=None,
            button_name="Older",
            source="mqtt",
            outcome="played",
            pressed_at="2026-01-01T10:00:00.000000Z",
        )
    )
    await store.log_press(
        PressLogIn(
            button_id=None,
            button_name="Newer",
            source="ui_test",
            outcome="failed",
            detail="no audio device",
        )
    )

    entries = await store.recent_presses()
    assert [entry.button_name for entry in entries] == ["Newer", "Older"]
    assert entries[0].pressed_at.endswith("Z")
    assert entries[0].detail == "no audio device"
    assert await store.recent_presses(limit=1) == [entries[0]]


async def test_log_with_missing_button_is_a_typed_conflict(store: ConfigStore) -> None:
    """A foreign-key violation becomes a store error, not a sqlite3 error."""
    with pytest.raises(StoreConflict):
        await store.log_press(
            PressLogIn(
                button_id=999,
                button_name="Missing",
                source="mqtt",
                outcome="played",
            )
        )
    assert await store.recent_presses() == []


async def test_log_emits_no_config_event(store: ConfigStore) -> None:
    """Log writes are deliberately excluded from the config event bus."""
    queue = store.events.subscribe_queue(maxsize=10)
    await store.log_press(
        PressLogIn(button_id=None, button_name="Test", source="mqtt", outcome="played")
    )
    assert queue.empty()


@pytest.mark.slow
async def test_retention_keeps_newest_thousand(store: ConfigStore) -> None:
    """Inserting 1005 entries trims exactly the five oldest rows."""
    for index in range(1005):
        await store.log_press(
            PressLogIn(
                button_id=None,
                button_name=f"Press {index}",
                source="mqtt",
                outcome="played",
                pressed_at=f"2026-01-01T00:00:00.{index:06d}Z",
            )
        )

    entries = await store.recent_presses(limit=2000)
    assert len(entries) == 1000
    assert entries[0].button_name == "Press 1004"
    assert entries[-1].button_name == "Press 5"
    assert [entry.button_name for entry in entries] == [
        f"Press {index}" for index in range(1004, 4, -1)
    ]


async def test_timestamps_are_normalised_to_utc(store: ConfigStore) -> None:
    """Offset and second-precision values are stored in the canonical form."""
    await store.log_press(
        PressLogIn(
            button_id=None,
            button_name="Offset",
            source="mqtt",
            outcome="played",
            pressed_at="2026-01-01T15:00:00+05:00",
        )
    )
    await store.log_press(
        PressLogIn(
            button_id=None,
            button_name="Seconds",
            source="mqtt",
            outcome="played",
            pressed_at="2026-01-01T09:30:00",
        )
    )
    await store.log_press(
        PressLogIn(button_id=None, button_name="Default", source="mqtt", outcome="played")
    )

    entries = await store.recent_presses()
    assert [(entry.button_name, entry.pressed_at) for entry in entries] == [
        ("Default", entries[0].pressed_at),
        ("Offset", "2026-01-01T10:00:00.000000Z"),
        ("Seconds", "2026-01-01T09:30:00.000000Z"),
    ]
    assert entries[0].pressed_at.endswith("Z")


async def test_invalid_timestamp_is_a_typed_error(store: ConfigStore) -> None:
    """A malformed ``pressed_at`` raises ``ValidationError`` and writes nothing."""
    with pytest.raises(ValidationError, match="pressed_at"):
        await store.log_press(
            PressLogIn(
                button_id=None,
                button_name="Broken",
                source="mqtt",
                outcome="played",
                pressed_at="yesterday",
            )
        )
    assert await store.recent_presses() == []


async def _log_queued(
    store: ConfigStore, *, job_id: str, detail: str | None = "engine espeak_ng"
) -> None:
    """Insert one press row in the ``queued`` state the press path leaves behind.

    :param store: connected configuration store.
    :param job_id: id the playback worker will look the row up by.
    :param detail: detail string the press path wrote, or ``None``.
    """
    await store.log_press(
        PressLogIn(
            button_id=None,
            button_name="Front Door",
            source="mqtt",
            outcome="queued",
            pressed_at="2026-01-01T12:00:00.000000Z",
            detail=detail,
            job_id=job_id,
        )
    )


async def test_record_playback_marks_the_queued_row_played(store: ConfigStore) -> None:
    """A successful playback fills in the timings and keeps the press detail."""
    await _log_queued(store, job_id="job-1")

    recorded = await store.record_playback(
        job_id="job-1", outcome="played", duration_ms=1200, latency_ms=35
    )

    assert recorded is True
    (entry,) = await store.recent_presses()
    assert entry.outcome == "played"
    assert entry.duration_ms == 1200
    assert entry.latency_ms == 35
    assert entry.job_id == "job-1"
    assert entry.detail == "engine espeak_ng"


async def test_record_playback_replaces_the_detail_on_failure(store: ConfigStore) -> None:
    """The failure path stores its own detail, because it explains the failure."""
    await _log_queued(store, job_id="job-2")

    recorded = await store.record_playback(
        job_id="job-2", outcome="failed", detail="audio_error: sound card stopped talking"
    )

    assert recorded is True
    (entry,) = await store.recent_presses()
    assert entry.outcome == "failed"
    assert entry.detail == "audio_error: sound card stopped talking"
    assert entry.duration_ms is None
    assert entry.latency_ms is None


async def test_record_playback_appends_a_note_to_a_null_detail(store: ConfigStore) -> None:
    """A press row with no detail stores exactly the playback note."""
    await _log_queued(store, job_id="job-note", detail=None)

    recorded = await store.record_playback(
        job_id="job-note", outcome="played", duration_ms=1200, note="wake lead-in 1.5 s"
    )

    assert recorded is True
    (entry,) = await store.recent_presses()
    assert entry.detail == "wake lead-in 1.5 s"


async def test_record_playback_appends_a_note_after_the_press_detail(store: ConfigStore) -> None:
    """The note is appended, so the press-time detail stays visible next to it."""
    await _log_queued(store, job_id="job-note-2")

    await store.record_playback(job_id="job-note-2", outcome="played", note="wake lead-in 1.5 s")

    (entry,) = await store.recent_presses()
    assert entry.detail == "engine espeak_ng; wake lead-in 1.5 s"


async def test_record_playback_keeps_the_press_detail_without_a_note(store: ConfigStore) -> None:
    """No note appends nothing, and never erases what the press path wrote."""
    await _log_queued(store, job_id="job-note-3")

    await store.record_playback(job_id="job-note-3", outcome="played", duration_ms=10)

    (entry,) = await store.recent_presses()
    assert entry.detail == "engine espeak_ng"


async def test_record_playback_detail_still_replaces_the_note(store: ConfigStore) -> None:
    """A non-``None`` detail replaces the whole string, replacement and all."""
    await _log_queued(store, job_id="job-note-4")

    await store.record_playback(
        job_id="job-note-4",
        outcome="failed",
        detail="audio_error: sound card stopped talking",
        note="wake lead-in 1.5 s",
    )

    (entry,) = await store.recent_presses()
    assert entry.detail == "audio_error: sound card stopped talking"


async def test_record_playback_with_unknown_job_writes_nothing(store: ConfigStore) -> None:
    """An unknown job id reports ``False`` and never inserts a row."""
    await _log_queued(store, job_id="job-3")

    recorded = await store.record_playback(job_id="nobody", outcome="played", duration_ms=10)

    assert recorded is False
    assert len(await store.recent_presses()) == 1


async def test_record_playback_never_overwrites_a_finished_row(store: ConfigStore) -> None:
    """A row that already carries an outcome is left exactly as it was."""
    await _log_queued(store, job_id="job-4")
    assert await store.record_playback(job_id="job-4", outcome="failed", detail="audio_error: gone")

    recorded = await store.record_playback(
        job_id="job-4", outcome="played", duration_ms=500, latency_ms=5
    )

    assert recorded is False
    (entry,) = await store.recent_presses()
    assert entry.outcome == "failed"
    assert entry.detail == "audio_error: gone"
    assert entry.duration_ms is None
    assert entry.latency_ms is None


async def test_record_playback_emits_no_config_event(store: ConfigStore) -> None:
    """Revising a press row is log traffic, not configuration traffic."""
    queue = store.events.subscribe_queue(maxsize=10)
    await _log_queued(store, job_id="job-5")

    await store.record_playback(job_id="job-5", outcome="played", duration_ms=10, latency_ms=1)

    assert queue.empty()


async def test_log_press_round_trips_the_synthesis_time(store: ConfigStore) -> None:
    """A press row stores the synthesis time it was written with.

    :param store: connected configuration store.
    """
    await store.log_press(
        PressLogIn(
            button_id=None,
            button_name="Front Door",
            source="mqtt",
            outcome="queued",
            pressed_at="2026-01-01T12:00:00.000000Z",
            synthesis_ms=240,
        )
    )

    (entry,) = await store.recent_presses()
    assert entry.synthesis_ms == 240


async def test_a_row_written_without_a_synthesis_time_reads_none(store: ConfigStore) -> None:
    """A press that synthesised nothing stores ``NULL``, never ``0``.

    :param store: connected configuration store.
    """
    await _log_queued(store, job_id="job-no-synthesis")

    (entry,) = await store.recent_presses()
    assert entry.synthesis_ms is None


async def test_record_playback_leaves_the_synthesis_time_intact(store: ConfigStore) -> None:
    """The playback worker revises the row without touching the press's own timing.

    :param store: connected configuration store.
    """
    await store.log_press(
        PressLogIn(
            button_id=None,
            button_name="Front Door",
            source="mqtt",
            outcome="queued",
            pressed_at="2026-01-01T12:00:00.000000Z",
            detail="engine espeak_ng",
            job_id="job-synthesis",
            synthesis_ms=240,
        )
    )

    recorded = await store.record_playback(
        job_id="job-synthesis", outcome="played", duration_ms=1200, latency_ms=35
    )

    assert recorded is True
    (entry,) = await store.recent_presses()
    assert entry.synthesis_ms == 240
    assert entry.duration_ms == 1200
    assert entry.latency_ms == 35


async def test_recent_presses_filters_by_outcome(store: ConfigStore) -> None:
    """An ``outcome`` filter keeps matching rows only, newest first, then limits."""
    for index, outcome in enumerate(("failed", "played", "failed", "played", "failed")):
        await store.log_press(
            PressLogIn(
                button_id=None,
                button_name=f"{outcome} {index}",
                source="mqtt",
                outcome=outcome,
                pressed_at=f"2026-01-01T00:00:00.{index:06d}Z",
            )
        )

    failed = await store.recent_presses(outcome="failed")
    assert [entry.button_name for entry in failed] == ["failed 4", "failed 2", "failed 0"]

    # The limit applies to the filtered rows, not to the whole table.
    newest_failed = await store.recent_presses(limit=1, outcome="failed")
    assert [entry.button_name for entry in newest_failed] == ["failed 4"]
    assert await store.recent_presses(outcome="deduped") == []
    assert len(await store.recent_presses()) == 5


@pytest.mark.slow
async def test_retention_bound_is_the_exported_constant(store: ConfigStore) -> None:
    """The trimmed table holds exactly ``PRESS_LOG_RETENTION`` rows."""
    assert PRESS_LOG_RETENTION == 1000

    for index in range(PRESS_LOG_RETENTION + 3):
        await store.log_press(
            PressLogIn(
                button_id=None,
                button_name=f"Press {index}",
                source="mqtt",
                outcome="played",
                pressed_at=f"2026-01-01T00:00:00.{index:06d}Z",
            )
        )

    async with store.connection.execute("SELECT COUNT(*) FROM press_log") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert int(row[0]) == PRESS_LOG_RETENTION

    entries = await store.recent_presses(limit=PRESS_LOG_RETENTION)
    assert len(entries) == PRESS_LOG_RETENTION
    assert entries[0].button_name == f"Press {PRESS_LOG_RETENTION + 2}"
    assert entries[-1].button_name == "Press 3"
