/**
 * The sound library's rules: every string the panel prints about a sound, decided here.
 *
 * The panel renders, this module decides. Keeping the wording out of the component means the
 * one thing that must not drift — the copy the storyboard pins, and the difference between
 * "flagged missing" and "deleted" — is testable without a DOM. Two of these rules are
 * honesty rules rather than formatting ones:
 *
 *   - A rescan's `removed` counts rows that *became missing*, never rows deleted, because the
 *     scan flags a vanished file and keeps its row.
 *   - A sound's delete route DELETES the buttons that use it and never touches the file on
 *     disk, so the confirm copy has to say both plainly or the user agrees to something else.
 *
 * Pure TS, no runes, no DOM and no fetch.
 */

import type { AffectedButton, ApiError, SoundRescanResponse, SoundRow } from './api.js';

/** The placeholder for a duration nobody can state, so a cell never collapses. */
const UNKNOWN_DURATION = '—';

/** The separator the F6 rescan line uses between its three counts. */
const SUMMARY_SEPARATOR = ' · ';

/**
 * A duration in milliseconds as the storyboard prints it: seconds with one decimal.
 *
 * Very long sounds stay in seconds (`125.0 s`) on purpose: the library is doorbell chimes, so
 * a `2:05` form would invent a minutes/hours vocabulary no frame of the storyboard uses, and
 * one unit is one rule to keep right.
 *
 * `null` is the server saying it could not probe the file, and a negative or non-finite value
 * is a value that must never reach the user as `NaN s` or `-0.4 s` — all three read `—`.
 */
export function formatSoundDuration(durationMs: number | null): string {
  if (durationMs === null || !Number.isFinite(durationMs) || durationMs < 0) {
    return UNKNOWN_DURATION;
  }
  return `${(durationMs / 1000).toFixed(1)} s`;
}

/**
 * How a row's stored `source_type` is described to a reader.
 *
 * `'file'` reads `'folder'` because that is what it means to the user: the sound was found in
 * the library folder rather than fetched. `'url'` reads `'URL download'` because that is how it
 * got here. Any other value is returned verbatim — the server may grow a third source type, and
 * inventing a label for a value this build does not know would be a claim about it.
 */
export function describeSourceType(sourceType: string): string {
  switch (sourceType) {
    case 'file':
      return 'folder';
    case 'url':
      return 'URL download';
    default:
      return sourceType;
  }
}

/**
 * The one-line result of a rescan, in the F6 frame's wording.
 *
 * `removed` is printed as the server counts it — rows newly flagged missing — so the summary
 * must never be read as a deletion count. The separators are the frame's own: a middle dot with
 * one space either side.
 */
export function rescanSummary(report: SoundRescanResponse): string {
  return [
    `added: ${report.added}`,
    `removed: ${report.removed}`,
    `missing: ${report.missing}`
  ].join(SUMMARY_SEPARATOR);
}

/**
 * The buttons that a delete would take with it, as one readable phrase.
 *
 * Two names join with `and`, three or more with commas and a final `and` — no Oxford comma,
 * because the sentence reads as speech. Names are used exactly as stored: slugging or
 * title-casing them would print something the user never named.
 */
export function affectedButtonNames(buttons: readonly AffectedButton[]): string {
  const names = buttons.map((button) => button.name);
  if (names.length === 0) {
    return '';
  }
  if (names.length === 1) {
    return names[0];
  }
  const last = names[names.length - 1];
  return `${names.slice(0, -1).join(', ')} and ${last}`;
}

/** The confirm dialog's title for one sound, in the typographic quotes the other panels use. */
export function deleteConfirmTitle(name: string): string {
  return `Delete “${name}”?`;
}

/**
 * The confirm dialog's body: what confirming actually does.
 *
 * The attached case names the buttons because the route DELETES them — `delete_sound(force=True)`
 * does not disable them — so "AND these buttons" is literally true and must stay in the copy.
 * The unattached case still has to be right about the file: deleting a sound removes its index
 * row, and the route never touches the file on disk, because the file belongs to the library.
 *
 * `row` is accepted so a caller builds all three confirm strings from one row, and it is
 * never read: a stale row is deleted exactly like a live one, and the name already travelled
 * in the title, so no field of it can change the sentence.
 */
export function deleteConfirmMessage(row: SoundRow, affected: readonly AffectedButton[]): string {
  if (affected.length > 0) {
    return `This sound is used by: ${affectedButtonNames(affected)}. Confirming deletes the sound AND these buttons.`;
  }
  return 'Deleting this sound removes it from the library. The file on disk is not deleted.';
}

/**
 * What the confirm button says, so it names exactly what the click does.
 *
 * The long label is F9's, and it is only honest when buttons really will go with the sound; an
 * unattached sound gets the short one rather than a warning about buttons that do not exist.
 */
export function deleteConfirmLabel(affected: readonly AffectedButton[]): string {
  return affected.length > 0 ? 'Delete sound and buttons' : 'Delete sound';
}

/**
 * The buttons named by a refused delete's `detail`, or an empty list when it names none.
 *
 * A 409 for a referenced sound is the only place the affected button names exist, so this is
 * where they are read — and it runs inside a click handler, where a shape the server changed
 * must degrade to "no names" rather than throw. So `detail` is `unknown` (that is what
 * `ApiError.detail` is), every level is checked, and the result carries the three fields the UI
 * needs copied out one by one: spreading the raw entry into component state would let an
 * unexpected field become UI state.
 */
export function affectedButtonsFromError(detail: ApiError['detail']): AffectedButton[] {
  if (typeof detail !== 'object' || detail === null) {
    return [];
  }
  const named: unknown = (detail as { affected_buttons?: unknown }).affected_buttons;
  if (!Array.isArray(named)) {
    return [];
  }

  const buttons: AffectedButton[] = [];
  for (const entry of named) {
    if (typeof entry !== 'object' || entry === null) {
      continue;
    }
    const { id, name, slug } = entry as { id?: unknown; name?: unknown; slug?: unknown };
    if (typeof id !== 'number' || typeof name !== 'string' || typeof slug !== 'string') {
      continue;
    }
    buttons.push({ id, name, slug });
  }
  return buttons;
}
