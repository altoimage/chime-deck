/**
 * One press, described for a human: the report a panel shows after a test press.
 *
 * A press answers `PressResponse` — an outcome, a code, and possibly a `detail` sentence
 * the press path itself built. The temptation is to render those fields raw; the honesty
 * problem is that some of what a reader wants to know (which engine spoke, which file
 * played) is **not in the response at all**. So every value below carries its provenance:
 *
 *   - `'press'` — the response said it (only an engine name can be: `press.py::press`
 *     appends `engine <name>` to its notes for a TTS press).
 *   - `'configured'` — it came from the button's saved row, which is what the button *would*
 *     use; it is not a claim about this press.
 *   - `'unknown'` — nothing we hold can answer it. Saying so is the point: a plausible guess
 *     about which voice spoke is worse than admitting nobody said.
 *
 * `PressResult`/`PressResponse` carry no `sound_name` — it is written to `press_log` only,
 * so a sound's file is always reported from the button's saved configuration and never as
 * something the response said.
 *
 * Pure TS, no DOM and no runes: the panel renders, this module decides.
 */

import type { PressResponse } from './api.js';
import { pressOutcomeLabel } from './tile-view.js';

/** Where a report's engine/file value came from, so the UI can hedge or not. */
export type PressProvenance = 'press' | 'configured' | 'unknown';

/** A finished press report; every field is already a string the UI prints unchanged. */
export interface PressReport {
  /** The outcome word, never empty. */
  outcomeLabel: string;
  /** Which `ui/Badge.svelte` colour the outcome deserves. */
  kind: 'ok' | 'warn' | 'alert' | 'neutral';
  /** `'Engine used'` or `'Sound used'` — the row's own label. */
  engineOrFileLabel: string;
  /** The engine or sound name, or an honest placeholder. */
  engineOrFileValue: string;
  engineOrFileProvenance: PressProvenance;
  /** The backend's `detail` sentence, verbatim, or `null`. */
  detail: string | null;
  /** How long the request took, as measured by the caller. */
  roundTripMs: number;
}

/** The phrase the press path appends for a TTS press (`press.py::press`'s `notes.append(f"engine {clip.engine}")`). */
const ENGINE_IN_DETAIL = /engine ([a-z0-9_]+)/i;

/** The sound a sound button plays, when the library does not name it. */
const UNKNOWN_SOUND = 'the sound this button plays';

/** The engine a TTS press pointed at nothing. */
const UNKNOWN_ENGINE = 'an engine this press did not name';

/**
 * The outcome word for a **finished** press.
 *
 * `pressOutcomeLabel('queued')` is `'Sending…'`, which is right while a press
 * request is still in flight and wrong for a report about a request that has
 * already come back — so `queued` is renamed here and every other outcome is
 * `pressOutcomeLabel`'s answer, including an outcome this UI does not know (it
 * stays verbatim; a status is never invented).
 */
function reportOutcomeLabel(outcome: string): string {
  return outcome === 'queued' ? 'Queued for playback' : pressOutcomeLabel(outcome);
}

/**
 * The badge colour for an outcome.
 *
 * `queued` is `ok` because the request succeeded in doing what it promised — the playback
 * has not finished, but nothing is wrong. `deduped`, `suppressed_quiet_hours` and
 * `suppressed_muted` are `warn` because the button did nothing on purpose and a reader must
 * not think it broke. An outcome this UI does not know is `neutral`, never green.
 *
 * Exported because the History panel badges a stored press by the same rule: one
 * mapping means a press cannot be green in the report and amber in the log.
 */
export function pressOutcomeKind(outcome: string): 'ok' | 'warn' | 'alert' | 'neutral' {
  switch (outcome) {
    case 'played':
    case 'queued':
      return 'ok';
    case 'failed':
      return 'alert';
    case 'deduped':
    case 'suppressed_quiet_hours':
    case 'suppressed_muted':
      return 'warn';
    default:
      return 'neutral';
  }
}

/**
 * Describe one press in the four facts a panel can honestly show.
 *
 * @param response what `POST /api/buttons/{id}/press` answered
 * @param button the pressed button's saved shape, or `null` when the panel no longer has it
 * @param soundName the name of `button.sound_id` from `GET /api/sounds`, or `null` when that
 *   id is not in the library list
 * @param roundTripMs the caller's own measurement of the request
 * @param defaultEngine `tts.default_engine` from `GET /api/settings`
 */
export function describePress(
  response: PressResponse,
  button: { action_type: string; tts_engine: string | null } | null,
  soundName: string | null,
  roundTripMs: number,
  defaultEngine: string
): PressReport {
  const outcomeLabel = reportOutcomeLabel(response.outcome);
  const kind = pressOutcomeKind(response.outcome);

  const engineName = response.detail === null ? null : ENGINE_IN_DETAIL.exec(response.detail)?.[1] ?? null;

  if (engineName !== null) {
    // The press path named the engine it used, so this is a fact about this press.
    return {
      outcomeLabel,
      kind,
      engineOrFileLabel: 'Engine used',
      engineOrFileValue: engineName,
      engineOrFileProvenance: 'press',
      detail: response.detail,
      roundTripMs
    };
  }

  if (button?.action_type === 'sound') {
    // A press detail never names the sound file, so a known name can only come from the
    // button's saved configuration — and a missing one is a library gap, not a press fact.
    return {
      outcomeLabel,
      kind,
      engineOrFileLabel: 'Sound used',
      engineOrFileValue: soundName ?? UNKNOWN_SOUND,
      engineOrFileProvenance: soundName === null ? 'unknown' : 'configured',
      detail: response.detail,
      roundTripMs
    };
  }

  // A TTS press whose detail names no engine (a `tts_disabled` failure is the common case):
  // the configured engine is what the button would have used, which is worth naming, but it
  // is not what this press did.
  const configured = button?.tts_engine || defaultEngine;
  const hasEngine = configured !== '';
  return {
    outcomeLabel,
    kind,
    engineOrFileLabel: 'Engine used',
    engineOrFileValue: hasEngine ? configured : UNKNOWN_ENGINE,
    engineOrFileProvenance: hasEngine ? 'configured' : 'unknown',
    detail: response.detail,
    roundTripMs
  };
}

/** A whole-millisecond latency line: `42.4` → `'42 ms'`, `0` → `'0 ms'`. */
export function formatLatency(ms: number): string {
  return `${Math.round(ms)} ms`;
}
