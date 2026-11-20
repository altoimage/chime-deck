/**
 * The tile editor's decisions, in one pure place.
 *
 * A saved row carries two icon fields with confusingly similar names: `icon` is
 * what the D16 resolver produced (possibly a guess or a fallback) and
 * `icon_override` is what the user actually chose. Sending the resolved one back
 * would make the guess permanent, so exactly one function here builds a request
 * body — `changeBody` — and it is the only place that names the `icon` key. The
 * preview builds an `EntityTile` from the same draft, so what the editor shows
 * and what it saves cannot drift apart.
 *
 * The state→tone mapping follows the same shape (HSD-d5x2): `state_tones` on a
 * row is the tile's EXPLICIT overrides only — the built-in defaults the resolver
 * applies live in the backend and are never copied into a draft, because saving a
 * copy would freeze today's default into the database. The preview carries the
 * row's `tone` and never resolves one itself.
 *
 * Pure TS, no DOM and no runes: the editor components hold the state, this module
 * holds the rules, and a node test can check every one of them.
 */

import type { AttributeHint, EntityTile, EntityWriteBody } from './api.js';

/**
 * The subset of `api.ts`'s `EntityResponse` the editor needs.
 *
 * Structural, not an import of the wire type, because the editor is handed rows
 * from several places (the list, the detail of a just-saved row) and must not
 * care which. `last_changed` is optional: the live detail route supplies it, a
 * bare list row does not.
 */
export interface EntityRowLike {
  id: number;
  entity_id: string;
  display_name: string | null;
  icon: string;
  icon_override: string | null;
  icon_source: string;
  accent_color: string | null;
  show_attributes: string[];
  hide_when_unavailable: boolean;
  state: string | null;
  displayed_state: string | null;
  unit_of_measurement: string | null;
  attribute_hints: AttributeHint[];
  warning: string | null;
  last_changed?: string | null;
  /** The backend's tone for the row's current state; the preview passes it through. */
  tone: string | null;
  /** The tile's explicit overrides only, or `null` when it has none. */
  state_tones: Record<string, string> | null;
  colour_by_state: boolean;
}

/** What the editor is holding while the user types — camelCase, strings, no nulls for text. */
export interface EntityDraft {
  /** `''` means "no override" → the tile falls back to the Home Assistant name/entity id. */
  displayName: string;
  /** The raw stored form: `mdi:<name>`, `custom:<file>` or `null`. Never a resolved icon. */
  iconOverride: string | null;
  accentColor: string | null;
  showAttributes: string[];
  hideWhenUnavailable: boolean;
  /**
   * The explicit state→tone overrides, keyed by the raw Home Assistant state.
   * Never a copy of the built-in table: the draft holds what the user typed, so
   * an untouched mapping saves nothing and a default revised in a later release
   * still applies.
   */
  stateTones: Record<string, string>;
  /** Whether this tile's colour follows its state. */
  colourByState: boolean;
}

/** Eight touch-sized swatches on the dark surface, each with a name a person reads. */
export const PRESET_ACCENT_COLORS: readonly { value: string; label: string }[] = [
  { value: '#4f9cf9', label: 'Blue' },
  { value: '#2ec4b6', label: 'Teal' },
  { value: '#3ddc84', label: 'Green' },
  { value: '#ffb020', label: 'Amber' },
  { value: '#ff8a3d', label: 'Orange' },
  { value: '#ff5c5c', label: 'Red' },
  { value: '#b388ff', label: 'Purple' },
  { value: '#9aa7b6', label: 'Grey' }
];

/**
 * Whether `value` is a usable accent colour: `#rgb` or `#rrggbb`, nothing else.
 *
 * The backend stores `accent_color` verbatim, so this is the only thing standing
 * between a typo and a tile that renders nothing — hence one rule, imported by
 * every control that edits the field.
 */
export function isValidAccentColor(value: string): boolean {
  return /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(value);
}

/** The draft a row opens as. Text stays a string so the field is never `null`-valued. */
export function draftFrom(row: EntityRowLike): EntityDraft {
  return {
    displayName: row.display_name ?? '',
    iconOverride: row.icon_override,
    accentColor: row.accent_color,
    showAttributes: [...row.show_attributes],
    hideWhenUnavailable: row.hide_when_unavailable,
    // A fresh object: the draft must never alias the row, or an abandoned edit
    // would quietly change the row the editor is comparing against.
    stateTones: { ...(row.state_tones ?? {}) },
    colourByState: row.colour_by_state
  };
}

/** The name a draft would save: trimmed, and `''` collapses to `null` ("no override"). */
function draftDisplayName(draft: EntityDraft): string | null {
  const trimmed = draft.displayName.trim();
  return trimmed === '' ? null : trimmed;
}

function sameAttributes(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((name, index) => name === right[index]);
}

/**
 * Whether two state→tone maps say the same thing.
 *
 * Key ORDER is not something the user edits, so a map the editor rebuilt in a
 * different order is the same map and must not be saved. `!==` on two objects is
 * always true, so without this every save would rewrite the whole table — and a
 * patch that always fires is a configuration change nobody made.
 */
function sameToneMap(left: Record<string, string>, right: Record<string, string>): boolean {
  const keys = Object.keys(left);
  if (keys.length !== Object.keys(right).length) {
    return false;
  }
  return keys.every(
    (key) => Object.prototype.hasOwnProperty.call(right, key) && left[key] === right[key]
  );
}

/**
 * Only the keys whose value differs from the stored row, shaped as an
 * `EntityWriteBody`.
 *
 * `icon` is ALWAYS the raw override (or `null` to clear it) — never the row's
 * resolved `icon`. A patch therefore says "the user chose mdi:door" or "the user
 * chose nothing", which is exactly what the backend stores; sending the resolved
 * value would silently convert a fallback into a choice. An unchanged draft
 * produces `{}`, so a save can never rewrite a field the user did not touch —
 * which is why `state_tones` is compared key by key rather than by identity: an
 * always-dirty map would turn every save into a rewriting of the table.
 */
export function changeBody(draft: EntityDraft, row: EntityRowLike): EntityWriteBody {
  const body: EntityWriteBody = {};

  const displayName = draftDisplayName(draft);
  if (displayName !== (row.display_name ?? null)) {
    body.display_name = displayName;
  }

  if (draft.iconOverride !== row.icon_override) {
    body.icon = draft.iconOverride;
  }

  if (draft.accentColor !== row.accent_color) {
    body.accent_color = draft.accentColor;
  }

  if (!sameAttributes(draft.showAttributes, row.show_attributes)) {
    body.show_attributes = [...draft.showAttributes];
  }

  if (draft.hideWhenUnavailable !== row.hide_when_unavailable) {
    body.hide_when_unavailable = draft.hideWhenUnavailable;
  }

  if (!sameToneMap(draft.stateTones, row.state_tones ?? {})) {
    body.state_tones = { ...draft.stateTones };
  }

  if (draft.colourByState !== row.colour_by_state) {
    body.colour_by_state = draft.colourByState;
  }

  return body;
}

/** Whether the draft would send anything at all — what gates the Save button. */
export function isDirty(draft: EntityDraft, row: EntityRowLike): boolean {
  return Object.keys(changeBody(draft, row)).length > 0;
}

/**
 * The hints the draft's `show_attributes` would render.
 *
 * The row's hints are the server's resolution for the *stored* list, so they are
 * filtered and reordered to the draft's list. A name the server never resolved
 * (the user just added it) is synthesised from the live attributes — but only
 * when the entity really has that attribute, because a hint with an invented
 * value is worse than no hint.
 */
function previewHints(
  row: EntityRowLike,
  showAttributes: readonly string[],
  attributes: Record<string, unknown>
): AttributeHint[] {
  const hints: AttributeHint[] = [];
  for (const name of showAttributes) {
    const resolved = row.attribute_hints.find((hint) => hint.name === name);
    if (resolved !== undefined) {
      hints.push(resolved);
      continue;
    }
    const value = attributes[name];
    if (value === undefined) {
      continue;
    }
    hints.push({ name, label: name, value: String(value) });
  }
  return hints;
}

/**
 * The real `EntityTile` the dashboard would render for this draft, merged with
 * the live row facts.
 *
 * The live facts (`state`, `displayed_state`, `unit_of_measurement`,
 * `last_changed`) are the server's, never the draft's: a preview that invented
 * them would promise a reading the tile cannot show. The icon follows the draft
 * when the user has chosen one — a set override *is* D16 step 1, which is why it
 * also forces `iconSource` to `'user'` — and otherwise the row's resolved icon
 * and source stand.
 *
 * A preview is never selected: selection is runtime intent the panel owns
 * (D29), not a property of the entity, so there is nothing for a preview to
 * show until someone taps the real tile.
 */
export function previewTile(
  row: EntityRowLike,
  draft: EntityDraft,
  live: { attributes?: Record<string, unknown> }
): EntityTile {
  const attributes = live.attributes ?? {};
  return {
    id: row.id,
    entityId: row.entity_id,
    displayName: draftDisplayName(draft),
    state: row.state,
    attributes,
    icon: draft.iconOverride ?? row.icon,
    iconSource: draft.iconOverride === null ? row.icon_source : 'user',
    accentColor: draft.accentColor,
    lastChanged: row.last_changed ?? null,
    displayedState: row.displayed_state,
    unitOfMeasurement: row.unit_of_measurement,
    attributeHints: previewHints(row, draft.showAttributes, attributes),
    // The preview carries the backend's decision for the tile's current state
    // and does NOT re-implement tone resolution. A TypeScript mirror of the
    // resolver would be the second source of truth this ticket exists to
    // remove: the mapping's own colours are shown by the editor's tone
    // controls, and the preview refreshes from the PATCH response after a save.
    tone: row.tone,
    hideWhenUnavailable: draft.hideWhenUnavailable,
    selected: false
  };
}
