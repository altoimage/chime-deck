/**
 * The press log's rules: how a stored press is worded, badged and paged.
 *
 * The History panel renders, this module decides. A `HistoryRow` is the table's
 * own record — `pressed_at`, `source`, `outcome`, `duration_ms`, `latency_ms`,
 * `synthesis_ms` and a free-text `detail` the press path wrote — and none of
 * those is a sentence, so every wording the panel prints (an outcome's label, a
 * duration, a start delay, a synthesis time, the retention note, a failed row's
 * reason) is decided here and testable without a DOM.
 *
 * Two rules are honesty rules rather than formatting ones:
 *
 *   - The badge colour is `pressOutcomeKind` from `press-report.ts`, not a second
 *     copy of the mapping: a press must not be green in the test-press report and
 *     amber in the log for the same outcome.
 *   - A failed row's reason is `detail` verbatim, and when the server stored none
 *     the panel says so instead of inventing a cause or hiding the failure.
 *
 * Pure TS, no runes, no DOM and no fetch.
 */

import type { HistoryRow } from './api.js';
import { formatStamp } from './clock.js';
import { pressOutcomeKind } from './press-report.js';
import { pressOutcomeLabel } from './tile-view.js';

/** One row of the outcome filter sheet. */
export interface OutcomeOption {
  value: string;
  label: string;
}

/** The page size one History request asks for, and one "Load more" adds. */
export const PAGE = 50;

/** The placeholder for a fact the server did not record, so a row never collapses. */
const UNKNOWN = '—';

/**
 * The outcome filter's options, in the storyboard's order.
 *
 * Labels are defined here rather than derived wholesale from
 * `pressOutcomeLabel`, because that function is written for a press that is
 * still in flight: its `queued` answer is `'Sending…'`, which is wrong for a
 * finished press the log already stored. Every other label matches it, including
 * an outcome this build does not know (it stays verbatim — no label is invented
 * for it).
 */
export const OUTCOME_OPTIONS: OutcomeOption[] = [
  { value: '', label: 'All outcomes' },
  { value: 'played', label: 'Played' },
  { value: 'queued', label: 'Queued for playback' },
  { value: 'failed', label: 'Failed' },
  { value: 'deduped', label: 'Ignored (duplicate)' },
  { value: 'suppressed_quiet_hours', label: 'Suppressed by quiet hours' },
  { value: 'suppressed_muted', label: 'Suppressed by mute' }
];

/**
 * The word a row's badge carries for *outcome*.
 *
 * The filter's own labels are reused so the filter and the rows cannot disagree
 * about what a stored outcome is called; anything not in that list falls back to
 * `pressOutcomeLabel`, which returns an unknown outcome verbatim.
 */
export function historyOutcomeLabel(outcome: string): string {
  const option = OUTCOME_OPTIONS.find((candidate) => candidate.value === outcome);
  return option === undefined ? pressOutcomeLabel(outcome) : option.label;
}

/**
 * The badge colour an outcome deserves.
 *
 * Delegated to `press-report.ts` so the History panel and the test-press report
 * cannot badge the same outcome differently.
 */
export function outcomeKind(outcome: string): 'ok' | 'warn' | 'alert' | 'neutral' {
  return pressOutcomeKind(outcome);
}

/**
 * A recorded press duration, in `sound-library.ts`'s duration style.
 *
 * `null` is the server saying it recorded no duration (a press that failed before
 * playback, or an outcome with nothing to time); a negative or non-finite value is
 * a value that must never reach the user as `NaN s`. All three read `—`. At or
 * above one second the number is seconds with one decimal and a trailing `.0`
 * stripped (`1.8 s`, `2 s`), because whole seconds need no decimal to look exact.
 */
export function formatDurationMs(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms) || ms < 0) {
    return UNKNOWN;
  }
  if (ms < 1000) {
    return `${Math.round(ms)} ms`;
  }
  const seconds = (ms / 1000).toFixed(1);
  return `${seconds.endsWith('.0') ? seconds.slice(0, -2) : seconds} s`;
}

/**
 * The scale both timings share: whole milliseconds below one second, seconds with
 * one decimal and a trailing `.0` stripped from one second up (`1.8 s`, `2 s`).
 *
 * Private because the two public formatters differ at the ends of the scale, and
 * only they may choose those words (`immediate` for a latency, `<1 ms` for a
 * synthesis); the middle of the scale must not drift between them.
 */
function formatMeasuredMs(ms: number): string {
  if (ms < 1000) {
    return `${Math.round(ms)} ms`;
  }
  const seconds = (ms / 1000).toFixed(1);
  return `${seconds.endsWith('.0') ? seconds.slice(0, -2) : seconds} s`;
}

/**
 * A press's start delay, where `null` and `0` are deliberately different answers.
 *
 * `null` is "no measurement exists": the playback job never started, so there is
 * nothing to time, and it reads `—`. `0` is "the measurement is zero": the worker
 * started at once, which is a real result and reads `immediate`. Collapsing the
 * two would hide the distinction this field exists to show, which is why this
 * function must not call `formatDurationMs`: that one maps `null` onto the same
 * `—` it uses for a bad value, and reusing it here (or from the panel) would erase
 * exactly the difference. The positive renderings still match `formatDurationMs`
 * on purpose, so the two facts read alike, and a negative or non-finite value is
 * the same defensive `—` rather than a `NaN s`.
 */
export function formatLatencyMs(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms) || ms < 0) {
    return UNKNOWN;
  }
  if (Math.round(ms) === 0) {
    return 'immediate';
  }
  return formatMeasuredMs(ms);
}

/**
 * How long the press's own speech synthesis took.
 *
 * Two deliberate differences from `formatLatencyMs`, both honesty rules rather
 * than style:
 *
 *   - `null` reads `—`, never `cached`. `null` covers five different facts — a
 *     cache hit, a bake hit, a sound button, a failed press, and an engine that
 *     keeps no measurement — so printing "cached" would fabricate a cause for
 *     four of them. A hit is a good outcome, and the dash states the one true
 *     thing: no synthesis was measured for this press.
 *   - `0` reads `<1 ms`, never `immediate`. A synthesis that rounded to zero at
 *     1 ms resolution is a real measurement of a very fast render, while
 *     `immediate` is `latency_ms`'s word for "the worker started at once";
 *     borrowing it here would conflate the two fields the ticket keeps apart.
 *
 * Everything else is shared with `formatLatencyMs` through `formatMeasuredMs`, so
 * the `N ms` / `1.8 s` renderings cannot drift, and a negative or non-finite
 * value is the same defensive `—` as its siblings.
 *
 * Named for the press rather than the engine because that is what it words: one
 * stored row's own render, fixed when the press was written. `speech.ts` words
 * the other reading — an engine's most recently measured synthesis, re-read on
 * every status refresh — and the two must not be mistaken for each other.
 */
export function formatPressSynthesisMs(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms) || ms < 0) {
    return UNKNOWN;
  }
  if (Math.round(ms) === 0) {
    return '<1 ms';
  }
  return formatMeasuredMs(ms);
}

/**
 * A press timestamp as the settings UI prints any timestamp (`YYYY-MM-DD HH:MM`).
 *
 * `clock.ts::formatStamp` already owns that one locale-free format, including the
 * honest answer for a value that does not parse (it is printed verbatim), so
 * this is a name for the panel rather than a second formatting rule.
 */
export function formatHistoryTime(iso: string): string {
  return formatStamp(iso);
}

/** The one sentence that explains what retention means to the rows on screen. */
export function retentionNote(retention: number): string {
  return `The newest ${retention} press-log rows are kept; older presses are discarded.`;
}

/**
 * The size of the next page after "Load more", or `null` at the retention cap.
 *
 * The server's `limit` is 1..`retention`, so the request may never exceed the cap:
 * the next size is the current one plus one page, clamped to `retention`, and
 * `null` once the current page already covers everything retained. `null` is the
 * panel's cue to replace the button with an explanation rather than leave a dead
 * control that cannot ask for more rows.
 */
export function loadMoreLimit(current: number, retention: number): number | null {
  if (current >= retention) {
    return null;
  }
  return Math.min(current + PAGE, retention);
}

/**
 * The reason to print for a failed row, or `null` when the server recorded none.
 *
 * `detail` is the press path's own sentence and is returned verbatim, never
 * paraphrased; an empty or whitespace-only detail is the same as no detail, so it
 * reads `null` and the panel prints the honest "no reason" sentence instead.
 */
export function failedReason(row: HistoryRow): string | null {
  if (row.detail === null || row.detail.trim() === '') {
    return null;
  }
  return row.detail;
}
