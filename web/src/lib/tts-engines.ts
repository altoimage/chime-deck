/**
 * The speech-engine options a button editor offers, and whether speech is on at all.
 *
 * Three sources disagree about what an engine is, so this module keeps them apart:
 *
 *   - the **wire** names come from the settings registry — `_TTS_ENGINE_CHOICES` in
 *     `src/chime_deck/store/settings_registry.py`. That tuple is the authority; the list
 *     here is a mirror and must be updated with it (a name this file forgets is a value the
 *     UI cannot store, so the mirror has a test).
 *   - **availability** comes from `GET /api/tts/engines`, which lists only the engines a
 *     press would actually try. An engine missing from that answer is therefore *unknown*,
 *     not unavailable: `ha_tts` and `remote_tts` are absent whenever the backend has no
 *     reason to probe them.
 *   - **"is speech on"** is that route's `disabled` (D27) when it answered at all, and
 *     `tts.default_engine === 'none'` from `GET /api/settings` when it did not. The live
 *     route is authoritative; the setting is the documented fallback (the hand-off note in
 *     `api/buttons.py` describes exactly that pair).
 *
 * Nothing here invents a reason: an unavailable engine's `reason` is the server's own whole
 * sentence, rendered verbatim.
 *
 * Pure TS, no DOM and no runes: the panel renders, this module decides.
 */

import type { TtsEnginesResponse } from './api.js';

/**
 * Human labels for the five stored engine names, each carrying the wire name so the
 * settings screen and the log can be matched up by eye.
 *
 * Mirrors `_TTS_ENGINE_CHOICES = ("ha_tts", "piper", "remote_tts", "espeak_ng", "none")`.
 */
export const ENGINE_LABELS: Record<string, string> = {
  ha_tts: "Home Assistant's voices (ha_tts)",
  piper: 'Piper on this device (piper)',
  remote_tts: 'A remote TTS server (remote_tts)',
  espeak_ng: 'eSpeak NG, the built-in voice (espeak_ng)',
  none: 'No speech — this button stays silent (none)'
};

/**
 * One selectable row: the value stored in `tts_engine`, its label, and why it may be refused.
 *
 * `disabled` is a UI hint, not a guarantee — the server re-validates on save.
 */
export interface EngineChoice {
  value: string;
  label: string;
  description?: string;
  disabled: boolean;
  /** The server's own sentence, verbatim, when `disabled`. */
  reason?: string;
}

/** The wire choices in registry order; `'none'` last, because it is a state, not a fallback. */
const WIRE_ENGINES: readonly string[] = ['ha_tts', 'piper', 'remote_tts', 'espeak_ng', 'none'];

/** What each engine is, before any availability verdict is added to it. */
const ENGINE_DESCRIPTIONS: Record<string, string> = {
  ha_tts: 'The voices your Home Assistant already exposes.',
  piper: 'The neural voice running on this device; it needs a downloaded voice model.',
  remote_tts: 'A TTS server this device sends the phrase to.',
  espeak_ng: 'The built-in eSpeak NG voice — the robotic fallback when nothing else can speak.',
  none: 'The button keeps the rest of its configuration, and a press reports that speech is off.'
};

/** The stored name of the deliberate D27 off state. */
const SPEECH_OFF = 'none';

/** The unknown-availability sentence for a route that did not answer (or failed). */
const NOT_LOADED = 'This build did not report it, so its availability is unknown.';

/**
 * A label for any engine name, including one this build has no description for.
 *
 * `GET /api/tts/engines` reports whatever the resolver holds, so a sixth engine must still
 * produce a readable row rather than a blank one.
 */
function engineLabel(name: string): string {
  return ENGINE_LABELS[name] ?? `${name} (an engine this UI has no description of)`;
}

/**
 * The rows the editor offers, in a fixed order: the "use the default" row, then the five
 * wire choices.
 *
 * The default row (`value: ''`) is how a button says "whatever Settings currently names",
 * so a later change of default follows the button instead of pinning a stale engine. `none`
 * is a first-class choice and is never disabled: speech being off is a supported state
 * (D27), and existing TTS buttons must stay editable rather than be deleted.
 *
 * @param status the live engine list, or `null` while it is loading or after it failed
 * @param defaultEngine `tts.default_engine` from `GET /api/settings`, named in the default row
 */
export function engineChoices(status: TtsEnginesResponse | null, defaultEngine: string): EngineChoice[] {
  const reported = new Map<string, { available: boolean; reason: string | null }>();
  for (const engine of status?.engines ?? []) {
    reported.set(engine.name, { available: engine.available, reason: engine.reason });
  }
  const availableNames = (status?.engines ?? [])
    .filter((engine) => engine.available)
    .map((engine) => engine.name);

  const choices: EngineChoice[] = [
    {
      value: '',
      label: `Use the default engine (${defaultEngine})`,
      description: 'Whichever engine Settings names as the default.',
      disabled: false
    }
  ];

  for (const name of WIRE_ENGINES) {
    const base = ENGINE_DESCRIPTIONS[name] ?? '';

    if (name === SPEECH_OFF) {
      choices.push({
        value: name,
        label: engineLabel(name),
        description: base,
        disabled: false
      });
      continue;
    }

    const row = reported.get(name);
    if (row === undefined) {
      choices.push({
        value: name,
        label: engineLabel(name),
        description: `${base} ${NOT_LOADED}`.trim(),
        disabled: false
      });
      continue;
    }

    if (row.available) {
      choices.push({ value: name, label: engineLabel(name), description: base, disabled: false });
      continue;
    }

    // The alternatives come from the same answer, so the pointer cannot name an engine the
    // backend did not just call available. With none available there is nothing to point at.
    const alternatives = availableNames.filter((other) => other !== name);
    const pointer =
      alternatives.length === 0 ? '' : ` — available instead: ${alternatives.join(', ')}`;
    const choice: EngineChoice = {
      value: name,
      label: engineLabel(name),
      description: `${base}${pointer}`.trim(),
      disabled: true
    };
    if (row.reason !== null) {
      // The server's sentence, verbatim. A missing reason stays missing: inventing one
      // would be worse than the row simply having nothing to add.
      choice.reason = row.reason;
    }
    choices.push(choice);
  }

  return choices;
}

/**
 * Whether speech is switched off for this device.
 *
 * The engines route is authoritative: its `disabled` flag is the resolver's own answer, and
 * a `tts_engine` of `'none'` per button (also supported, D27) is a different question this
 * function does not answer. When the route has not answered — still loading, or it failed —
 * the stored `tts.default_engine` is the documented fallback, so the panel can already hide
 * the TTS action without waiting for a second request.
 */
export function speechDisabled(status: TtsEnginesResponse | null, defaultEngine: string): boolean {
  return status !== null ? status.disabled : defaultEngine === SPEECH_OFF;
}
