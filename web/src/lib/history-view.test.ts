/**
 * The press-log rules, in the plain node environment: no DOM, no runes, no fetch.
 *
 * These pin the wording the History panel prints and the paging arithmetic that
 * keeps a request inside the server's retention cap. The badge kinds are compared
 * against `press-report.ts` directly, because the one thing that must not drift is
 * the two views disagreeing about what colour an outcome deserves.
 */

import { describe, expect, it } from 'vitest';

import type { HistoryRow } from './api.js';
import {
  OUTCOME_OPTIONS,
  PAGE,
  failedReason,
  formatDurationMs,
  formatHistoryTime,
  formatLatencyMs,
  formatPressSynthesisMs,
  historyOutcomeLabel,
  loadMoreLimit,
  outcomeKind,
  retentionNote
} from './history-view.js';
import { pressOutcomeKind } from './press-report.js';

function row(overrides: Partial<HistoryRow> = {}): HistoryRow {
  return {
    id: 1,
    button_id: 3,
    button_name: 'Front Door',
    pressed_at: '2026-11-12T09:04:00Z',
    source: 'mqtt',
    outcome: 'played',
    duration_ms: 1800,
    latency_ms: 18,
    synthesis_ms: 240,
    detail: null,
    ...overrides
  };
}

describe('OUTCOME_OPTIONS', () => {
  it('offers every outcome the server can store, plus "all"', () => {
    expect(OUTCOME_OPTIONS.map((option) => option.value)).toEqual([
      '',
      'played',
      'queued',
      'failed',
      'deduped',
      'suppressed_quiet_hours',
      'suppressed_muted'
    ]);
  });

  it('gives every option a non-empty label that is a noun phrase, not a live status', () => {
    for (const option of OUTCOME_OPTIONS) {
      expect(option.label.trim()).not.toBe('');
    }
    const queued = OUTCOME_OPTIONS.find((option) => option.value === 'queued');
    expect(queued?.label).toBe('Queued for playback');
    // The chime strip's live wording must not leak into a finished press.
    expect(OUTCOME_OPTIONS.some((option) => option.label === 'Sending…')).toBe(false);
  });
});

describe('historyOutcomeLabel', () => {
  it('reuses the option labels and keeps an unknown outcome verbatim', () => {
    expect(historyOutcomeLabel('played')).toBe('Played');
    expect(historyOutcomeLabel('queued')).toBe('Queued for playback');
    expect(historyOutcomeLabel('suppressed_muted')).toBe('Suppressed by mute');
    expect(historyOutcomeLabel('exploded')).toBe('exploded');
  });
});

describe('formatDurationMs', () => {
  it('reads an absent duration as a dash, never as zero', () => {
    expect(formatDurationMs(null)).toBe('—');
  });

  it('prints sub-second durations in whole milliseconds', () => {
    expect(formatDurationMs(0)).toBe('0 ms');
    expect(formatDurationMs(840)).toBe('840 ms');
  });

  it('prints second durations with one decimal, stripping a trailing .0', () => {
    expect(formatDurationMs(1800)).toBe('1.8 s');
    expect(formatDurationMs(2000)).toBe('2 s');
  });

  it('never emits NaN or a negative duration', () => {
    expect(formatDurationMs(-1)).toBe('—');
    expect(formatDurationMs(Number.NaN)).toBe('—');
  });
});

describe('formatLatencyMs', () => {
  it('reads an absent latency as a dash, not as zero', () => {
    expect(formatLatencyMs(null)).toBe('—');
  });

  it('prints a zero latency as an immediate start, never a dash', () => {
    expect(formatLatencyMs(0)).toBe('immediate');
  });

  it('treats a sub-millisecond latency as the same immediate start', () => {
    expect(formatLatencyMs(0.4)).toBe('immediate');
  });

  it('prints sub-second latencies in whole milliseconds', () => {
    expect(formatLatencyMs(1)).toBe('1 ms');
    expect(formatLatencyMs(250)).toBe('250 ms');
    expect(formatLatencyMs(999)).toBe('999 ms');
  });

  it('prints second latencies with one decimal, stripping a trailing .0', () => {
    expect(formatLatencyMs(1800)).toBe('1.8 s');
    expect(formatLatencyMs(2000)).toBe('2 s');
  });

  it('never emits NaN or a negative latency', () => {
    expect(formatLatencyMs(-1)).toBe('—');
    expect(formatLatencyMs(Number.NaN)).toBe('—');
    expect(formatLatencyMs(Number.POSITIVE_INFINITY)).toBe('—');
  });

  it('keeps an unmeasured latency distinct from an immediate one, so they cannot be unified', () => {
    // `null` is "the job never started, nothing was measured"; `0` is a real zero.
    expect(formatLatencyMs(0)).not.toBe(formatLatencyMs(null));
  });
});

describe('formatPressSynthesisMs', () => {
  it('reads an unmeasured synthesis as a dash, never as a fabricated cause', () => {
    // `null` is a cache hit, a bake hit, a sound button, a failed press or an engine
    // that keeps no measurement — "cached" would be a guess for four of those.
    expect(formatPressSynthesisMs(null)).toBe('—');
  });

  it('prints a synthesis rounded to zero as under a millisecond, never as a dash', () => {
    expect(formatPressSynthesisMs(0)).toBe('<1 ms');
  });

  it('treats a sub-millisecond synthesis as the same under-a-millisecond render', () => {
    expect(formatPressSynthesisMs(0.4)).toBe('<1 ms');
  });

  it('prints sub-second syntheses in whole milliseconds', () => {
    expect(formatPressSynthesisMs(1)).toBe('1 ms');
    expect(formatPressSynthesisMs(250)).toBe('250 ms');
    expect(formatPressSynthesisMs(999)).toBe('999 ms');
  });

  it('prints second syntheses with one decimal, stripping a trailing .0', () => {
    expect(formatPressSynthesisMs(1800)).toBe('1.8 s');
    expect(formatPressSynthesisMs(2000)).toBe('2 s');
  });

  it('never emits NaN or a negative synthesis', () => {
    expect(formatPressSynthesisMs(-1)).toBe('—');
    expect(formatPressSynthesisMs(Number.NaN)).toBe('—');
    expect(formatPressSynthesisMs(Number.POSITIVE_INFINITY)).toBe('—');
  });

  it('keeps an unmeasured synthesis distinct from one rounded to zero, so they cannot be unified', () => {
    // `null` is "nothing was synthesised for this press"; `0` is a real, very fast render.
    expect(formatPressSynthesisMs(0)).not.toBe(formatPressSynthesisMs(null));
  });
});

describe('formatHistoryTime', () => {
  it('uses the one locale-free timestamp format', () => {
    expect(formatHistoryTime('2026-11-12T09:04:00')).toBe('2026-11-12 09:04');
  });

  it('prints an unparseable timestamp verbatim rather than "Invalid Date"', () => {
    expect(formatHistoryTime('not a date')).toBe('not a date');
  });
});

describe('retentionNote', () => {
  it('states the cap the server reported', () => {
    expect(retentionNote(1000)).toBe(
      'The newest 1000 press-log rows are kept; older presses are discarded.'
    );
  });
});

describe('loadMoreLimit', () => {
  it('adds one page', () => {
    expect(loadMoreLimit(PAGE, 1000)).toBe(PAGE * 2);
  });

  it('never asks for more than the retention cap', () => {
    expect(loadMoreLimit(980, 1000)).toBe(1000);
    expect(loadMoreLimit(PAGE, 10)).toBeNull();
  });

  it('reports the cap reached instead of a page size', () => {
    expect(loadMoreLimit(1000, 1000)).toBeNull();
    expect(loadMoreLimit(1001, 1000)).toBeNull();
  });
});

describe('outcomeKind', () => {
  it('matches the press report for every outcome, so the two views cannot disagree', () => {
    const outcomes = [
      'played',
      'queued',
      'failed',
      'deduped',
      'suppressed_quiet_hours',
      'suppressed_muted'
    ];

    for (const outcome of outcomes) {
      expect(outcomeKind(outcome)).toBe(pressOutcomeKind(outcome));
    }
  });

  it('badges an unknown outcome neutrally, never green', () => {
    expect(outcomeKind('exploded')).toBe('neutral');
  });
});

describe('failedReason', () => {
  it('returns the server detail verbatim', () => {
    expect(failedReason(row({ outcome: 'failed', detail: 'mpv exited with status 1' }))).toBe(
      'mpv exited with status 1'
    );
  });

  it('returns null when the server recorded no reason', () => {
    expect(failedReason(row({ outcome: 'failed', detail: null }))).toBeNull();
    expect(failedReason(row({ outcome: 'failed', detail: '   ' }))).toBeNull();
  });
});
