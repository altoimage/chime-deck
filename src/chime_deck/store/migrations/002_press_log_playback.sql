-- ---------------------------------------------------------------------------
-- press_log: the two columns the playback worker (HSD-kfnd) needs.
--
-- job_id     The id the press path stamped on the job it dispatched. The
--            playback worker revises the row the press already wrote
--            ("queued" -> "played" / "failed") through this id, so the history
--            keeps exactly one row per press instead of gaining a second row
--            when the sound has actually finished.
--
-- latency_ms Enqueue-to-first-sound, in milliseconds. Design 03 §8 surfaces
--            this figure in the UI, and there was no column for it; the press
--            path writes NULL and the worker fills it in once playback starts.
--
-- Deliberately NO index on job_id: press_log is capped at 1000 rows, so a
-- lookup by job_id scans a table that is already tiny. An index would force the
-- pinned index set in tests/test_store_migrations.py to change for no gain.
-- ---------------------------------------------------------------------------
ALTER TABLE press_log ADD COLUMN job_id TEXT;
ALTER TABLE press_log ADD COLUMN latency_ms INTEGER;
