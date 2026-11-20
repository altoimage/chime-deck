/**
 * The pre-render rules shared by the buttons and About panels.
 *
 * A missing bake key means an older server build, not a zero-state button; failures take
 * precedence over stale digest fields; and the server's four batch outcomes stay separate
 * so none of the work or skips can disappear into a single count. Pure TS, no DOM or runes:
 * panels render these decisions, this module keeps them honest.
 */

import type { BakeAllResponse, BakeUsageResponse, ButtonDetail } from './api.js';
import { hasTemplateSyntax } from './button-action.js';

/** One stored row's bake state, or no state when this server does not expose the feature. */
export type BakeStatus =
  | { kind: 'baked'; sha: string; format: string | null; at: string }
  | { kind: 'not_baked' }
  | { kind: 'failed'; reason: string };

const BAKE_KEYS = ['baked_sha', 'baked_format', 'baked_at', 'baked_error'] as const;

/**
 * Read a row's bake state without confusing an older backend for an unbaked button.
 *
 * The own-key check is the feature signal: if every key is absent, panels render no control
 * at all. Once any key exists, an engine failure wins before the digest because stale values
 * must never be presented as a successful bake; blank metadata is normalized to the honest
 * empty value rather than treated as a status.
 */
export function bakeStateOf(row: ButtonDetail): BakeStatus | null {
  const hasBakeContract = BAKE_KEYS.some((key) => Object.prototype.hasOwnProperty.call(row, key));
  if (!hasBakeContract) {
    return null;
  }

  if (typeof row.baked_error === 'string' && row.baked_error.trim() !== '') {
    return { kind: 'failed', reason: row.baked_error };
  }

  if (typeof row.baked_sha === 'string' && row.baked_sha.trim() !== '') {
    const format = typeof row.baked_format === 'string' && row.baked_format.trim() !== ''
      ? row.baked_format
      : null;
    const at = typeof row.baked_at === 'string' && row.baked_at.trim() !== '' ? row.baked_at : '';
    return { kind: 'baked', sha: row.baked_sha, format, at };
  }

  return { kind: 'not_baked' };
}

/** Badge tones available to communicate the three bake states without colour alone. */
export type BakeBadgeKind = 'ok' | 'warn' | 'alert';

/**
 * Describe the stored state in the design's exact words.
 *
 * The sentence remains user-readable and an engine error is preserved verbatim after the
 * fixed `failed: ` prefix, because rewriting it could discard the useful failure detail.
 */
export function describeBakeStatus(status: BakeStatus): { kind: BakeBadgeKind; label: string } {
  switch (status.kind) {
    case 'baked':
      return { kind: 'ok', label: 'Pre-rendered' };
    case 'not_baked':
      return { kind: 'warn', label: 'Not pre-rendered' };
    case 'failed':
      return { kind: 'alert', label: `failed: ${status.reason}` };
  }
}

/**
 * Whether an existing row is eligible for a one-time pre-render.
 *
 * A templated phrase is rendered on every press so its live values remain current, and must
 * opt out of baking entirely; reusing `hasTemplateSyntax` keeps this decision identical to
 * the editor's template rules. `null` text means there is no template to inspect, though the
 * server still owns phrase validation and may refuse an empty phrase.
 */
export function canPreRender(row: ButtonDetail): boolean {
  return row.action_type === 'tts' && !hasTemplateSyntax(row.tts_text ?? '');
}

/** Whether an unknown JSON value is a finite, non-negative integer. */
function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && Number.isInteger(value) && value >= 0;
}

/** Whether an unknown value has the own fields needed for a JSON object shape. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Whether an own JSON field is present, rather than inherited from an object prototype. */
function hasOwn(record: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(record, key);
}

/**
 * Validate the server's device-measured usage response.
 *
 * Requiring both directories and integer counts prevents a missing/HTML/partial probe answer
 * from being rendered as a misleading zero; byte and file quantities cannot be fractional.
 */
export function isBakeUsage(value: unknown): value is BakeUsageResponse {
  if (!isRecord(value) || !hasOwn(value, 'baked') || !hasOwn(value, 'cache')) {
    return false;
  }
  const baked = value.baked;
  const cache = value.cache;
  return isRecord(baked) && hasOwn(baked, 'files') && hasOwn(baked, 'bytes') &&
    isNonNegativeInteger(baked.files) && isNonNegativeInteger(baked.bytes) &&
    isRecord(cache) && hasOwn(cache, 'files') && hasOwn(cache, 'bytes') &&
    isNonNegativeInteger(cache.files) && isNonNegativeInteger(cache.bytes);
}

/** Whether a response field is an array of server button ids. */
function isButtonIdList(value: unknown): value is number[] {
  return Array.isArray(value) && value.every(isNonNegativeInteger);
}

/** Whether an unknown value is one fully formed failed-button entry. */
function isBakeFailure(value: unknown): boolean {
  return isRecord(value) && hasOwn(value, 'button') && hasOwn(value, 'error') &&
    isNonNegativeInteger(value.button) && typeof value.error === 'string';
}

/**
 * Validate all four batch outcome lists as a complete response.
 *
 * Each category is required independently: accepting a partial body would silently hide a
 * skip or failure category, and numeric identifiers must be integer ids rather than coercible
 * strings or fractional values.
 */
export function isBakeAllResponse(value: unknown): value is BakeAllResponse {
  if (!isRecord(value) || !hasOwn(value, 'baked') || !hasOwn(value, 'skipped_dynamic') ||
    !hasOwn(value, 'skipped_current') || !hasOwn(value, 'failed')) {
    return false;
  }
  return isButtonIdList(value.baked) && isButtonIdList(value.skipped_dynamic) &&
    isButtonIdList(value.skipped_current) && Array.isArray(value.failed) && value.failed.every(isBakeFailure);
}

/** One separately labelled category in a completed batch report. */
export interface BakeReportSection {
  kind: 'baked' | 'current' | 'dynamic' | 'failed';
  label: string;
  entries: string[];
}

/** The human fallback when a response's button id has no name in the caller's snapshot. */
function buttonName(id: number, names?: ReadonlyMap<number, string>): string {
  return names?.get(id) ?? `Button #${id}`;
}

/**
 * Keep all four server outcome categories visible, in their response order.
 *
 * `skipped_current` and `skipped_dynamic` describe different reasons not to bake, so they
 * must never be combined or dropped; names improve recognition, while the id fallback keeps
 * an outcome identifiable when a caller's name snapshot is stale or incomplete.
 */
export function describeBakeReport(
  report: BakeAllResponse,
  names?: ReadonlyMap<number, string>
): BakeReportSection[] {
  return [
    {
      kind: 'baked',
      label: 'Baked now',
      entries: report.baked.map((id) => buttonName(id, names))
    },
    {
      kind: 'current',
      label: 'Already up to date',
      entries: report.skipped_current.map((id) => buttonName(id, names))
    },
    {
      kind: 'dynamic',
      label: 'Rendered on every press',
      entries: report.skipped_dynamic.map((id) => buttonName(id, names))
    },
    {
      kind: 'failed',
      label: 'Failed',
      entries: report.failed.map((failure) => `${buttonName(failure.button, names)} — ${failure.error}`)
    }
  ];
}

/** Whether the batch did no work and had no skips or failures in any category. */
export function bakeReportIsEmpty(report: BakeAllResponse): boolean {
  return report.baked.length === 0 && report.skipped_current.length === 0 &&
    report.skipped_dynamic.length === 0 && report.failed.length === 0;
}
