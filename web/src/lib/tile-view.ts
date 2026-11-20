/**
 * State → presentation for a status tile: the one place a raw backend token
 * becomes a kind, a word, a unit and a "does this belong on screen at all".
 *
 * Pure and dependency-free apart from the tile type, because three screens
 * share the rule: the dashboard, the entity editor's live preview (HSD-4umw)
 * and the settings tile list (HSD-rj5j). A rule stated once cannot drift
 * between them, and it can be tested without a DOM.
 *
 * The tone arrives with the word (HSD-d5x2): the backend resolves a state→tone
 * table per tile and sends the answer as `tone`, so this module paints what it
 * was sent instead of keeping a fixed table of its own. There is exactly one
 * presentation decision left here, and it is not about states at all: a reading
 * stays a `value` (see `NUMERIC_STATE` below).
 *
 * The honesty rules, which every branch below encodes:
 *   - an `unavailable` or `unknown` entity never shows a unit or a
 *     plausible-looking value. A stale reading is worse than no reading,
 *     because it looks live.
 *   - `state: null` is "No state", never "Off". The snapshot sends `state: null`
 *     until the live wiring ticket (HSD-qr3k) lands, and "Off" would be a claim
 *     the backend never made.
 *   - a numeric reading is a `value`, never `ok` — 21.4 °C is neither good nor
 *     bad, so it must not be green.
 *   - a tone outside the four-word vocabulary is neutral (`off`), never green:
 *     green means "good", and an uninterpretable tone is not a claim we can
 *     make. The fallback never consults the state string to invent one.
 *   - colour is never the only carrier; every kind comes with its word
 *     (design 05 §1).
 */

import type { EntityTile } from './api.js';

export type TileKind = 'ok' | 'warn' | 'alert' | 'off' | 'unknown' | 'unavailable' | 'value';

/** Everything a tile needs to render, already decided. */
export interface TileView {
  kind: TileKind;
  /** The word under the name; never empty, never a raw token we do not know. */
  word: string;
  /** The measurement unit, or null when the tile must not print one. */
  unit: string | null;
  /** True when the word is a reading, so the tile can use the numeric font. */
  isNumeric: boolean;
  /** True when the tile must not be rendered at all (`hide_when_unavailable`). */
  hidden: boolean;
}

/** A plain decimal reading; HA never sends exponents for measurement states. */
const NUMERIC_STATE = /^-?\d+(?:\.\d+)?$/;

/**
 * The tile kind a backend tone means.
 *
 * This interprets no state string: it only refuses a value the four-tone
 * vocabulary does not contain, which can happen for exactly two reasons — a
 * snapshot cached in `localStorage` before `tone` existed, which restores a
 * tile whose field is `undefined`, and a row with no tile mapping at all (the
 * add-entities picker's live Home Assistant search hits). `off` is the only
 * safe fallback, because "unmapped" *is* grey; guessing green would invent a
 * claim nobody made.
 */
function toneKind(tone: string | null | undefined): TileKind {
  switch (tone) {
    case 'ok':
      return 'ok';
    case 'warn':
      return 'warn';
    case 'alert':
      return 'alert';
    default:
      return 'off';
  }
}

/**
 * Work out how one tile presents itself.
 *
 * @param tile the entity's current tile, live or from the REST row
 * @param hideWhenUnavailable overrides the tile's own flag; omitted means "use
 *   the tile's", so a caller with no opinion cannot silently unhide a tile.
 */
export function tileView(tile: EntityTile, hideWhenUnavailable?: boolean): TileView {
  const hide = hideWhenUnavailable ?? tile.hideWhenUnavailable;

  if (tile.state === 'unavailable') {
    return { kind: 'unavailable', word: 'Unavailable', unit: null, isNumeric: false, hidden: hide };
  }

  if (tile.state === 'unknown') {
    return { kind: 'unknown', word: 'Unknown', unit: null, isNumeric: false, hidden: false };
  }

  if (tile.state === null || tile.state === '') {
    return { kind: 'unknown', word: 'No state', unit: null, isNumeric: false, hidden: false };
  }

  // The backend already wordifies via `presentation_hints`; nothing here
  // invents a word. An empty `displayed_state` falls back rather than printing
  // a blank tile.
  const word = tile.displayedState !== null && tile.displayedState !== '' ? tile.displayedState : tile.state;

  // The unit is only ever taken from the tile; a unit on an `on`/`off` tile is
  // whatever the backend said, and an empty one is no unit at all.
  const unit = tile.unitOfMeasurement === null || tile.unitOfMeasurement === '' ? null : tile.unitOfMeasurement;

  // A reading is not a state. A temperature is the same reading whatever the
  // entity's state means, so it keeps the plain text colour (`data-kind="value"`)
  // — design 05 §1's "colour carries meaning, not decoration". This is the one
  // presentation decision left on this side of the wire, and it is a fact about
  // the FORMAT of the value (a number and a unit), not a state→colour table: the
  // tone travelling in the payload is deliberately ignored for this branch.
  if (NUMERIC_STATE.test(tile.state.trim())) {
    return { kind: 'value', word, unit, isNumeric: true, hidden: false };
  }

  // Everything else wears the tone the backend resolved for this state. Nothing
  // here reads the state string: the word and the colour both come from the
  // same backend resolution, so they cannot disagree.
  return { kind: toneKind(tile.tone), word, unit, isNumeric: false, hidden: false };
}

/**
 * What each D16 icon source means, in the words the entity editor shows.
 *
 * All five wire values (`IconSource` in `src/chime_deck/icons/resolver.py`)
 * are listed so a resolved guess is never mistaken for a deliberate choice.
 */
export const ICON_SOURCE_LABELS: Readonly<Record<string, string>> = {
  user: 'your own saved icon',
  'ha-attribute': 'Home Assistant attribute',
  'ha-registry': 'Home Assistant entity registry',
  fallback: 'built-in default for this device type',
  default: 'nothing matched — showing the question-mark placeholder'
};

/**
 * Describe one icon source. Never returns `undefined`: a server that grows a
 * sixth source must still produce a readable line rather than a blank one.
 */
export function iconSourceLabel(source: string): string {
  return ICON_SOURCE_LABELS[source] ?? 'an icon source this UI does not know';
}

/**
 * Describe one press outcome. An outcome this UI does not know is printed
 * verbatim: a status is never invented.
 */
export function pressOutcomeLabel(outcome: string): string {
  switch (outcome) {
    case 'queued':
      return 'Sending…';
    case 'played':
      return 'Played';
    case 'failed':
      return 'Failed';
    case 'deduped':
      return 'Ignored (duplicate)';
    case 'suppressed_quiet_hours':
      return 'Quiet hours';
    case 'suppressed_muted':
      return 'Muted';
    default:
      return outcome;
  }
}
