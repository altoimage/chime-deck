/**
 * The chime-button editor's decisions, in one pure place.
 *
 * The server's button contract is a **diff PATCH**: `PATCH /api/buttons/{id}` merges the
 * submitted keys over the stored row, then re-validates the whole merged row
 * (`api/buttons.py::_merged_button`). Two consequences drive everything here:
 *
 *   - omitting a key keeps its stored value, while an explicit `null` **clears** a nullable
 *     one. So a body may say "this changed" and must never say "this is unchanged" — that
 *     is why `changeBody` only ever carries keys that differ from the stored row.
 *   - re-validating the *merged* row means a patch can fail on a field it never sent (a
 *     TTS button cannot be patched at all while `tts.default_engine` is `'none'`, D27). A
 *     client that only validated its own diff would be surprised; the `ApiError` is the
 *     answer, and this module does not try to predict it.
 *
 * What must never be sent:
 *   - `enabled` — the list's toggle owns it, and a stale draft would silently switch a
 *     button off (or back on) every time someone renamed it.
 *   - `sort_order` — there is no reorder in this ticket, and it is not in `ButtonWriteBody`.
 *
 * The client-side rules below do not invent policy; they mirror the server's own sentences
 * from `api/buttons.py` so the user reads the same words whether the check ran here or
 * there. Two rules are ours alone, and their wording is deliberately different from the
 * server's so nobody mistakes one for the other:
 *   - a blank name: the server derives a slug and answers 422 "this name produced no usable
 *     slug" — which is not what happened, so we say what is actually missing.
 *   - a blank slug in edit mode: the server treats `slug: null` as "keep the stored slug",
 *     so an empty field is not an error there, it is a silent no-op. We refuse it instead of
 *     pretending the rename happened.
 *
 * Pure TS, no DOM and no runes: the panels hold the state, this module holds the rules, and
 * a node test can check every one of them.
 */

import type { ButtonCreateBody, ButtonDetail, ButtonWriteBody } from './api.js';

/** The two action types this UI offers ('sound' and 'tts' are the server's `Literal`). */
export type ButtonAction = 'sound' | 'tts';

/**
 * Read a stored `action_type`.
 *
 * `'tts'` is `'tts'` and *anything* else is `'sound'`, because those are the only two
 * values `POST`/`PATCH` accept. A row whose stored value this UI does not know therefore
 * opens as a sound button — the safe half of the pair — rather than crashing the panel.
 */
export function parseAction(value: string): ButtonAction {
  return value === 'tts' ? 'tts' : 'sound';
}

/** What the editor is holding while the user types — camelCase, strings, no nulls for text. */
export interface ButtonDraft {
  name: string;
  /** `''` in create mode means "derive it from the name"; an edit never opens blank. */
  slug: string;
  icon: string | null;
  actionType: ButtonAction;
  /** `''` = no sound chosen; otherwise the sound id as a string (the picker's value is a string). */
  soundChoice: string;
  /** `''` = no phrase stored. */
  ttsText: string;
  /** `''` = use `tts.default_engine`. */
  ttsEngine: string;
  volumeOverride: number | null;
  dedupeSeconds: number | null;
  showOnDashboard: boolean;
  quietHoursExempt: boolean;
}

/** A new button's draft: no choices made, sound action, both overrides untouched. */
export const EMPTY_DRAFT: ButtonDraft = Object.freeze({
  name: '',
  slug: '',
  icon: null,
  actionType: 'sound',
  soundChoice: '',
  ttsText: '',
  ttsEngine: '',
  volumeOverride: null,
  dedupeSeconds: null,
  showOnDashboard: false,
  quietHoursExempt: false
});

/** A fresh copy of the empty draft — a new form must never share state with an old one. */
export function newDraft(): ButtonDraft {
  return { ...EMPTY_DRAFT };
}

/** The draft a stored row opens as. `null` becomes `''`, a numeric id becomes a string. */
export function draftFrom(row: ButtonDetail): ButtonDraft {
  return {
    name: row.name,
    slug: row.slug,
    icon: row.icon,
    actionType: parseAction(row.action_type),
    soundChoice: row.sound_id === null ? '' : String(row.sound_id),
    ttsText: row.tts_text ?? '',
    ttsEngine: row.tts_engine ?? '',
    volumeOverride: row.volume_override,
    dedupeSeconds: row.dedupe_seconds,
    showOnDashboard: row.show_on_dashboard,
    quietHoursExempt: row.quiet_hours_exempt
  };
}

/**
 * `draft.soundChoice` as a wire value: `''` → `null` (nothing chosen), a sound id → the
 * number, and anything else → `undefined`, meaning "no value we are willing to send".
 *
 * The picker's value is a string, so a blank or hand-edited value is reachable. `Number()`
 * of `'abc'` is `NaN`, and `JSON.stringify` turns `NaN` into `null` — a **silent clear** of
 * the button's sound. `undefined` keeps `sound_id` out of the body entirely and leaves the
 * explanation to `clientProblems()`.
 */
function soundIdFrom(choice: string): number | null | undefined {
  const trimmed = choice.trim();
  if (trimmed === '') {
    return null;
  }
  const value = Number(trimmed);
  return Number.isInteger(value) && value > 0 ? value : undefined;
}

/** The stored form of an optional text field: trimmed, and `''` collapses to `null`. */
function trimmedOrNull(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/**
 * Only the keys whose value differs from the stored row, shaped as a `ButtonWriteBody`.
 *
 * An unchanged draft produces `{}`, so a save can never rewrite a field the user did not
 * touch. `enabled` and `sort_order` are never in the answer (see the module comment).
 *
 * `slug` is sent only in an edit **and** only when it differs; a blank field sends nothing
 * at all, because the server reads `slug: null` as "keep the stored slug". `sound_id` is
 * skipped when the picker's value is not a positive integer (see `soundIdFrom`).
 */
export function changeBody(draft: ButtonDraft, row: ButtonDetail): ButtonWriteBody {
  const body: ButtonWriteBody = {};

  const name = draft.name.trim();
  if (name !== row.name) {
    body.name = name;
  }

  const slug = draft.slug.trim();
  if (slug !== '' && slug !== row.slug) {
    body.slug = slug;
  }

  if (draft.icon !== row.icon) {
    body.icon = draft.icon;
  }

  if (draft.actionType !== row.action_type) {
    body.action_type = draft.actionType;
  }

  const soundId = soundIdFrom(draft.soundChoice);
  if (soundId !== undefined && soundId !== row.sound_id) {
    body.sound_id = soundId;
  }

  const ttsText = trimmedOrNull(draft.ttsText);
  if (ttsText !== row.tts_text) {
    body.tts_text = ttsText;
  }

  const ttsEngine = trimmedOrNull(draft.ttsEngine);
  if (ttsEngine !== row.tts_engine) {
    body.tts_engine = ttsEngine;
  }

  if (draft.volumeOverride !== row.volume_override) {
    body.volume_override = draft.volumeOverride;
  }

  if (draft.dedupeSeconds !== row.dedupe_seconds) {
    body.dedupe_seconds = draft.dedupeSeconds;
  }

  if (draft.showOnDashboard !== row.show_on_dashboard) {
    body.show_on_dashboard = draft.showOnDashboard;
  }

  if (draft.quietHoursExempt !== row.quiet_hours_exempt) {
    body.quiet_hours_exempt = draft.quietHoursExempt;
  }

  return body;
}

/**
 * The whole `POST /api/buttons` body: every field, because there is no stored row to merge
 * over yet.
 *
 * A create keeps the inactive branch's value too — the API deliberately preserves a value
 * the user did not ask to drop (a TTS button may still carry a `sound_id`, the server keeps
 * it and the press path ignores it), so sending what the form holds is the honest body.
 * `enabled: true` because a newly created button nobody switched off is enabled.
 *
 * An unusable `soundChoice` becomes `null` here: the create has to send *something*, and
 * `null` is the value the server already understands. `clientProblems()` is what stops the
 * save with a sentence about picking a sound.
 */
export function createBody(draft: ButtonDraft): ButtonCreateBody {
  return {
    name: draft.name.trim(),
    action_type: draft.actionType,
    slug: trimmedOrNull(draft.slug),
    icon: draft.icon,
    sound_id: soundIdFrom(draft.soundChoice) ?? null,
    tts_text: trimmedOrNull(draft.ttsText),
    tts_engine: trimmedOrNull(draft.ttsEngine),
    volume_override: draft.volumeOverride,
    dedupe_seconds: draft.dedupeSeconds,
    show_on_dashboard: draft.showOnDashboard,
    quiet_hours_exempt: draft.quietHoursExempt,
    enabled: true
  };
}

/** Whether the draft would send anything at all — what gates the Save button. */
export function isDirty(draft: ButtonDraft, row: ButtonDetail): boolean {
  return Object.keys(changeBody(draft, row)).length > 0;
}

/** A rule the client can state before the server sees the body. */
export interface ClientProblem {
  field: 'name' | 'slug' | 'sound_id' | 'tts_text';
  /** A whole sentence, rendered verbatim under that field. */
  message: string;
}

const NAME_REQUIRED = 'the name cannot be empty — it is what names the button in Home Assistant';
const SLUG_REQUIRED =
  'type a slug — an existing button keeps its slug unless you change it, and an empty field would not rename anything';
const SOUND_REQUIRED =
  'a sound button requires a sound_id — choose a sound from the library, or make this button speak';
const PHRASE_REQUIRED = 'a TTS button requires non-empty tts_text — type the phrase to speak';

/**
 * Everything this editor can say is wrong before a request is made, in field order.
 *
 * The last two messages are the server's own sentences, verbatim, so the same problem reads
 * the same way whichever side caught it. The first two are the client's, and the module
 * comment says why they are worded differently from the server's answers.
 */
export function clientProblems(draft: ButtonDraft, mode: 'create' | 'edit'): ClientProblem[] {
  const problems: ClientProblem[] = [];

  if (draft.name.trim() === '') {
    problems.push({ field: 'name', message: NAME_REQUIRED });
  }

  if (mode === 'edit' && draft.slug.trim() === '') {
    problems.push({ field: 'slug', message: SLUG_REQUIRED });
  }

  if (draft.actionType === 'sound') {
    const soundId = soundIdFrom(draft.soundChoice);
    if (soundId === null || soundId === undefined) {
      problems.push({ field: 'sound_id', message: SOUND_REQUIRED });
    }
  }

  if (draft.actionType === 'tts' && trimmedOrNull(draft.ttsText) === null) {
    problems.push({ field: 'tts_text', message: PHRASE_REQUIRED });
  }

  return problems;
}

/**
 * The backend's D25 template test, mirrored: Jinja expression (`{{ … }}`) or statement
 * (`{% … %}`) delimiters, matched across line breaks.
 *
 * `src/chime_deck/tts/providers.py::_TEMPLATE_PATTERN` is the authority
 * (`{{.*?}}|{%.*?%}` with `re.DOTALL`, i.e. exactly this pattern with `[\s\S]` in place of
 * the flag). A bare `{{` with no closing `}}` is **not** template syntax — the backend only
 * matches a complete pair, so flagging an unclosed brace here would refuse a phrase the
 * server would happily accept.
 */
export function hasTemplateSyntax(text: string): boolean {
  return /\{\{[\s\S]*?\}\}|\{%[\s\S]*?%\}/.test(text);
}
