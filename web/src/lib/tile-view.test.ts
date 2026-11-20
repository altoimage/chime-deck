import { describe, expect, it } from 'vitest';

import { type EntityTile } from './api.js';
import {
  ICON_SOURCE_LABELS,
  iconSourceLabel,
  pressOutcomeLabel,
  tileView,
  type TileView
} from './tile-view.js';

/** A tile with nothing set but the id, so each test states only what it means. */
function tile(overrides: Partial<EntityTile> = {}): EntityTile {
  return {
    id: 1,
    entityId: 'lock.front_door',
    displayName: 'Front Door',
    state: 'locked',
    attributes: {},
    icon: 'mdi:lock',
    iconSource: 'user',
    accentColor: null,
    lastChanged: null,
    displayedState: null,
    unitOfMeasurement: null,
    attributeHints: [],
    tone: null,
    hideWhenUnavailable: false,
    selected: false,
    ...overrides
  };
}

describe('tileView tone', () => {
  it('renders each of the four tones as its kind, with the word the backend sent', () => {
    const tones = [
      ['ok', 'ok'],
      ['warn', 'warn'],
      ['alert', 'alert'],
      ['off', 'off']
    ] as const;

    for (const [tone, kind] of tones) {
      const view = tileView(tile({ tone, state: 'anything', displayedState: 'Anything' }));

      expect(view.kind, tone).toBe(kind);
    }

    expect(
      tileView(tile({ tone: 'ok', state: 'anything', displayedState: 'Anything' }))
    ).toEqual<TileView>({
      kind: 'ok',
      word: 'Anything',
      unit: null,
      isNumeric: false,
      hidden: false
    });
  });

  it('never consults the state string: an ok tone on a meaningless state is still ok', () => {
    const view = tileView(tile({ tone: 'ok', state: 'weird', displayedState: null }));

    expect(view.kind).toBe('ok');
    expect(view.word).toBe('weird');
  });

  it('never consults the old fixed table: a warn tone on locked is warn', () => {
    // The regression guard for HSD-d5x2: `locked` used to be green here no
    // matter what the backend said, so an amber lock is the whole ticket.
    const view = tileView(tile({ tone: 'warn', state: 'locked', displayedState: 'Locked' }));

    expect(view.kind).toBe('warn');
    expect(view.kind).not.toBe('ok');
    expect(view.word).toBe('Locked');
  });

  it('falls back to grey for a null tone, an unknown tone and a missing tone', () => {
    // `null` is "no state, no colour"; `'purple'` is outside the four-word
    // vocabulary; a missing field is a snapshot cached before `tone` existed.
    expect(tileView(tile({ tone: null, state: 'locked' })).kind).toBe('off');
    expect(tileView(tile({ tone: 'purple', state: 'locked' })).kind).toBe('off');
    expect(tileView(tile({ tone: undefined, state: 'locked' })).kind).toBe('off');
  });

  it('keeps the word and unit of a stateful tile whatever the tone says', () => {
    const view = tileView(
      tile({ tone: 'alert', state: 'on', displayedState: 'On', unitOfMeasurement: '%' })
    );

    expect(view.word).toBe('On');
    expect(view.unit).toBe('%');
    expect(view.isNumeric).toBe(false);
  });
});

describe('numeric readings', () => {
  it('shows a numeric reading as value with its unit, never as ok', () => {
    const view = tileView(tile({ state: '21.4', displayedState: '21.4', unitOfMeasurement: '°C' }));

    expect(view).toEqual<TileView>({
      kind: 'value',
      word: '21.4',
      unit: '°C',
      isNumeric: true,
      hidden: false
    });
    expect(view.kind).not.toBe('ok');
  });

  it('accepts a negative reading', () => {
    const view = tileView(tile({ state: '-3', unitOfMeasurement: '°C' }));

    expect(view.kind).toBe('value');
    expect(view.isNumeric).toBe(true);
  });

  it('ignores the tone the payload carries for a reading', () => {
    // A reading is not a state: whatever tone the backend resolved for this
    // tile, the numeric format keeps the plain text colour.
    for (const tone of ['ok', 'warn', 'alert', 'off', null]) {
      const view = tileView(tile({ tone, state: '21.4', unitOfMeasurement: '°C' }));

      expect(view.kind, String(tone)).toBe('value');
    }
  });
});

describe('unavailable and unknown', () => {
  it('says Unavailable with no unit and no value, whatever the tile claims', () => {
    const view = tileView(
      tile({
        state: 'unavailable',
        unitOfMeasurement: '°C',
        displayedState: '21.4',
        attributes: { battery_level: 87 }
      })
    );

    expect(view).toEqual<TileView>({
      kind: 'unavailable',
      word: 'Unavailable',
      unit: null,
      isNumeric: false,
      hidden: false
    });
  });

  it('says Unknown with no unit and no value', () => {
    const view = tileView(
      tile({ state: 'unknown', unitOfMeasurement: '°C', displayedState: '21.4', attributes: { x: 1 } })
    );

    expect(view).toEqual<TileView>({
      kind: 'unknown',
      word: 'Unknown',
      unit: null,
      isNumeric: false,
      hidden: false
    });
  });

  it('never calls a null state Off', () => {
    const view = tileView(tile({ state: null, displayedState: null }));

    expect(view).toEqual<TileView>({
      kind: 'unknown',
      word: 'No state',
      unit: null,
      isNumeric: false,
      hidden: false
    });
  });

  it('treats an empty state as No state too', () => {
    expect(tileView(tile({ state: '' })).word).toBe('No state');
  });
});

describe('hidden', () => {
  it('hides an unavailable tile only when the flag is set', () => {
    expect(tileView(tile({ state: 'unavailable' }), true).hidden).toBe(true);
    expect(tileView(tile({ state: 'unavailable' }), false).hidden).toBe(false);
  });

  it('falls back to the tile flag when the caller has no opinion', () => {
    expect(tileView(tile({ state: 'unavailable', hideWhenUnavailable: true })).hidden).toBe(true);
  });

  it('never hides a tile that is not unavailable, even with the flag set', () => {
    for (const state of ['locked', 'unlocked', 'open', 'off', '21.4', 'unknown', null]) {
      expect(tileView(tile({ state }), true).hidden, String(state)).toBe(false);
    }
  });
});

describe('icon sources', () => {
  it('labels all five wire values distinctly', () => {
    const values = ['user', 'ha-attribute', 'ha-registry', 'fallback', 'default'];
    const labels = values.map(iconSourceLabel);

    expect(new Set(labels).size).toBe(values.length);
    for (const label of labels) {
      expect(label.length).toBeGreaterThan(0);
    }
    expect(Object.keys(ICON_SOURCE_LABELS).sort()).toEqual(values.sort());
  });

  it('never returns undefined for an unknown source', () => {
    const label = iconSourceLabel('a-source-from-the-future');

    expect(typeof label).toBe('string');
    expect(label.length).toBeGreaterThan(0);
  });
});

describe('press outcomes', () => {
  it('words every known outcome', () => {
    expect(pressOutcomeLabel('queued')).toBe('Sending…');
    expect(pressOutcomeLabel('played')).toBe('Played');
    expect(pressOutcomeLabel('failed')).toBe('Failed');
    expect(pressOutcomeLabel('deduped')).toBe('Ignored (duplicate)');
    expect(pressOutcomeLabel('suppressed_quiet_hours')).toBe('Quiet hours');
    expect(pressOutcomeLabel('suppressed_muted')).toBe('Muted');
  });

  it('prints an unknown outcome verbatim rather than inventing a status', () => {
    expect(pressOutcomeLabel('something_new')).toBe('something_new');
  });
});
