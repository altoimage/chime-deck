/**
 * What the Speech settings panel offers, and how it words what the server reported.
 *
 * This module decides, the panel renders. Three things live here because they are rules
 * rather than markup:
 *
 *   - the **default engine** list. The six wire names mirror `_TTS_ENGINE_CHOICES` in
 *     `src/chime_deck/store/settings_registry.py`, in registry order with `none` last,
 *     and have a test. The `none` wording is the installation default's (D27), so it
 *     deliberately differs from `tts-engines.ts`, which words a *button's* engine: a button
 *     saying "this button stays silent" is a different sentence from an installation saying
 *     "sound files only".
 *   - the **voice rows**. `GET /api/tts/voices` reports what is installed beside the curated
 *     catalogue, and a saved voice that has since vanished must still appear, so the picker
 *     can show it and the user can edit it away instead of seeing a blank field.
 *   - the **download progress** words. Bytes and MiB are converted once, here, in the unit
 *     the server's own `size_mb` uses.
 *
 * Nothing here invents a reason: a `problem`, `warning` or `unavailable_reason` is the
 * server's own whole sentence and is carried through verbatim.
 *
 * Pure TS, no DOM and no runes: the panel renders, this module decides.
 */

import type { TtsVoicesResponse } from './api.js';

/** `tts.default_engine` default (settings_registry.py). */
export const DEFAULT_ENGINE = 'ha_tts';

/** The stored name of the deliberate D27 off state. */
export const SPEECH_OFF = 'none';

/** One row of the Default engine picker. */
export interface EngineOption {
  value: string;
  label: string;
  description: string;
}

/**
 * The six engine choices, in registry order with `none` last.
 *
 * All six are always selectable: the server accepts every one of them, and
 * `tts.piper.min_ram_mb` is the operator's own override, so the picker must not invent a
 * refusal of its own. `http_tts` speaks through whichever external provider the Speech
 * panel has selected, so it stays a choice even before one is configured — the provider
 * list, not this picker, is what explains a missing one.
 */
export const DEFAULT_ENGINE_OPTIONS: readonly EngineOption[] = [
  {
    value: 'ha_tts',
    label: "Home Assistant's voices (ha_tts)",
    description: 'The voices your Home Assistant already exposes.'
  },
  {
    value: 'piper',
    label: 'Piper on this device (piper)',
    description: 'The neural voice running here; it needs a downloaded voice model.'
  },
  {
    value: 'remote_tts',
    label: 'A remote TTS server (remote_tts)',
    description: 'A TTS server this device sends the phrase to.'
  },
  {
    value: 'espeak_ng',
    label: 'eSpeak NG, the built-in voice (espeak_ng)',
    description: 'The built-in voice — instant and robotic, good for alerts.'
  },
  {
    value: 'http_tts',
    label: 'External TTS service (http_tts)',
    description: 'Speaks through the external service you choose in this panel.'
  },
  {
    value: SPEECH_OFF,
    label: 'No speech — sound files only',
    description: 'Sound-file chimes keep working. Leaving speech off is a supported configuration (D27).'
  }
];

/** A measured synthesis time, or an em dash when the engine has not spoken yet. */
export function formatSynthesisMs(ms: number | null): string {
  return ms === null ? '—' : `${Math.round(ms)} ms`;
}

/** Bytes as the MiB figure the server's own `size_mb` uses, to one decimal. */
function asMib(bytes: number): number {
  return bytes / (1024 * 1024);
}

/**
 * Download progress in the unit the server's `size_mb` uses (MiB).
 *
 * `percent` is an integer a bar can draw; it is 0 when the server has not reported a total
 * yet, which is the same state the words name by omitting "of".
 */
export function formatProgress(
  done: number,
  total: number | null
): { label: string; percent: number } {
  const doneMib = `${asMib(done).toFixed(1)} MiB`;
  if (total === null || total <= 0) {
    return { label: doneMib, percent: 0 };
  }
  const percent = Math.min(100, Math.max(0, Math.floor((done / total) * 100)));
  return { label: `${doneMib} of ${asMib(total).toFixed(1)} MiB`, percent };
}

export const DOWNLOAD_POLL_INTERVAL_MS = 500;

/** 240 attempts x 500 ms = 2 minutes, a hard ceiling: the poll is never unbounded. */
export const DOWNLOAD_POLL_MAX_ATTEMPTS = 240;

/** Whether a download job has stopped: a terminal state needs no further poll. */
export function isDownloadFinished(state: string): boolean {
  return state === 'done' || state === 'failed' || state === 'rejected';
}

/** A state word a person can read; an unknown state stays exactly as the server sent it. */
export function downloadStateLabel(state: string): string {
  switch (state) {
    case 'running':
      return 'Downloading';
    case 'done':
      return 'Installed';
    case 'failed':
      return 'Failed';
    case 'rejected':
      return 'Refused';
    default:
      return state;
  }
}

/**
 * What the response says about one voice, as the picker's second line.
 *
 * Only facts the response carries are named — a hand-installed model has no curated
 * metadata and so gets a shorter line rather than a made-up quality.
 */
function describeVoice(voice: {
  installed: boolean;
  quality: string | null;
  size_mb: number;
}): string | undefined {
  const facts: string[] = [];
  if (voice.installed) {
    facts.push('installed');
  }
  if (voice.quality !== null) {
    facts.push(voice.quality);
  }
  if (voice.size_mb > 0) {
    facts.push(`${voice.size_mb} MiB`);
  }
  return facts.length === 0 ? undefined : facts.join(' · ');
}

/** The voice rows the picker offers: installed first, then curated, deduped by id. */
export function voiceChoices(
  voices: TtsVoicesResponse | null,
  stored: string
): { value: string; label: string; description?: string; disabled: boolean; reason?: string }[] {
  if (voices === null) {
    return [];
  }

  const rows: {
    value: string;
    label: string;
    description?: string;
    disabled: boolean;
    reason?: string;
  }[] = [];
  const seen = new Set<string>();

  for (const voice of [...voices.installed, ...voices.suggested]) {
    if (seen.has(voice.id)) {
      continue;
    }
    seen.add(voice.id);
    const row: {
      value: string;
      label: string;
      description?: string;
      disabled: boolean;
      reason?: string;
    } = {
      value: voice.id,
      label: voice.id,
      disabled: voice.problem !== null
    };
    const description = describeVoice(voice);
    if (description !== undefined) {
      row.description = description;
    }
    if (voice.problem !== null) {
      // The engine's sentence, verbatim. A row without one is simply selectable.
      row.reason = voice.problem;
    }
    rows.push(row);
  }

  // A saved voice that is neither installed nor curated is still the stored value: listing
  // it keeps the field legible instead of blanking a setting the user can then not see.
  if (stored !== '' && !seen.has(stored)) {
    rows.push({ value: stored, label: stored, disabled: false });
  }

  return rows;
}
