/**
 * Unit tests for the tile editor's rules (HSD-umw).
 *
 * Node environment, no DOM and no fetch: `entity-preview.ts` is where every
 * editing decision lives, so this is where they are pinned. The load-bearing one
 * is the icon: a saved row carries the resolver's guess in `icon` and the user's
 * choice in `icon_override`, and a request body must only ever carry the latter.
 */

import { describe, expect, it } from 'vitest';

import type { EntityWriteBody } from './api.js';
import {
  PRESET_ACCENT_COLORS,
  changeBody,
  draftFrom,
  isDirty,
  isValidAccentColor,
  previewTile,
  type EntityDraft,
  type EntityRowLike
} from './entity-preview.js';

function rowOf(overrides: Partial<EntityRowLike> = {}): EntityRowLike {
  return {
    id: 7,
    entity_id: 'lock.front_door',
    display_name: 'Front Door',
    icon: 'mdi:lock',
    icon_override: null,
    icon_source: 'ha-registry',
    accent_color: null,
    show_attributes: ['battery_level'],
    hide_when_unavailable: false,
    state: 'locked',
    displayed_state: 'Locked',
    unit_of_measurement: null,
    attribute_hints: [{ name: 'battery_level', label: 'Battery', value: '87 %' }],
    warning: null,
    last_changed: '2026-01-01T00:00:00+00:00',
    tone: 'ok',
    state_tones: null,
    colour_by_state: true,
    ...overrides
  };
}

function draftOf(overrides: Partial<EntityDraft> = {}): EntityDraft {
  return {
    displayName: 'Front Door',
    iconOverride: null,
    accentColor: null,
    showAttributes: ['battery_level'],
    hideWhenUnavailable: false,
    stateTones: {},
    colourByState: true,
    ...overrides
  };
}

describe('draftFrom', () => {
  it('copies the stored values into an editable draft', () => {
    const row = rowOf({ display_name: null, icon_override: 'custom:cat.svg', accent_color: '#4f9cf9' });

    expect(draftFrom(row)).toEqual({
      displayName: '',
      iconOverride: 'custom:cat.svg',
      accentColor: '#4f9cf9',
      showAttributes: ['battery_level'],
      hideWhenUnavailable: false,
      stateTones: {},
      colourByState: true
    });
  });

  it('copies the state→tone map, so editing the draft cannot edit the row', () => {
    const row = rowOf({ state_tones: { locked: 'ok' } });
    const draft = draftFrom(row);

    draft.stateTones.unlocked = 'alert';

    expect(row.state_tones).toEqual({ locked: 'ok' });
    expect(draft.stateTones).toEqual({ locked: 'ok', unlocked: 'alert' });
  });

  it('reads a null map as the empty one, never as a copy of the defaults', () => {
    // The defaults live in the backend. A draft that pre-filled them would save
    // today's table into the database the first time the user touched anything.
    expect(draftFrom(rowOf({ state_tones: null })).stateTones).toEqual({});
  });

  it('copies the attribute list, so editing the draft cannot edit the row', () => {
    const row = rowOf();
    const draft = draftFrom(row);

    draft.showAttributes.push('battery_health');

    expect(row.show_attributes).toEqual(['battery_level']);
  });
});

describe('changeBody', () => {
  it('sends the raw override, never the resolved icon', () => {
    const row = rowOf({ icon: 'mdi:lock', icon_override: null });

    const body = changeBody(draftOf({ iconOverride: 'mdi:door' }), row);

    expect(body).toEqual({ icon: 'mdi:door' });
    expect(body.icon).not.toBe('mdi:lock');
  });

  it('clears the override with an explicit null', () => {
    const row = rowOf({ icon: 'mdi:help-circle-outline', icon_override: 'mdi:door', icon_source: 'user' });

    expect(changeBody(draftOf({ iconOverride: null }), row)).toEqual({ icon: null });
  });

  it('sends only the display name when that is the only change', () => {
    expect(changeBody(draftOf({ displayName: 'Front door lock' }), rowOf())).toEqual({
      display_name: 'Front door lock'
    });
  });

  it('treats a blank display name as "no override"', () => {
    expect(changeBody(draftOf({ displayName: '   ' }), rowOf())).toEqual({ display_name: null });
  });

  it('trims the display name before comparing it', () => {
    expect(changeBody(draftOf({ displayName: '  Front Door  ' }), rowOf())).toEqual({});
  });

  it('sends nothing at all for an untouched draft', () => {
    expect(changeBody(draftOf(), rowOf())).toEqual({});
    expect(changeBody(draftFrom(rowOf()), rowOf())).toEqual({});
  });

  it('reports the accent, the attribute list and the hide flag when they change', () => {
    const row = rowOf({ accent_color: '#4f9cf9', show_attributes: ['battery_level'], hide_when_unavailable: false });

    const body: EntityWriteBody = changeBody(
      draftOf({ accentColor: null, showAttributes: ['battery_health', 'battery_level'], hideWhenUnavailable: true }),
      row
    );

    expect(body).toEqual({
      accent_color: null,
      show_attributes: ['battery_health', 'battery_level'],
      hide_when_unavailable: true
    });
    expect(Object.keys(body).sort()).toEqual(['accent_color', 'hide_when_unavailable', 'show_attributes']);
  });

  it('treats a reordered attribute list as a change', () => {
    const row = rowOf({ show_attributes: ['a', 'b'] });

    expect(changeBody(draftOf({ showAttributes: ['b', 'a'] }), row)).toEqual({ show_attributes: ['b', 'a'] });
  });

  it('never puts entity_id in the body: the row is edited, not renamed', () => {
    const body = changeBody(draftOf({ displayName: 'Anything' }), rowOf());

    expect(Object.prototype.hasOwnProperty.call(body, 'entity_id')).toBe(false);
  });

  it('sends no state_tones when the map says the same thing in another key order', () => {
    // `!==` on two objects is always true, so a naive comparison would rewrite
    // the whole table on every save.
    const row = rowOf({ state_tones: { locked: 'ok', unlocked: 'alert' } });
    const draft = draftOf({ stateTones: { unlocked: 'alert', locked: 'ok' } });

    expect(changeBody(draft, row)).toEqual({});
    expect(isDirty(draft, row)).toBe(false);
  });

  it('sends the whole map when a value changes', () => {
    const row = rowOf({ state_tones: { locked: 'ok', unlocked: 'alert' } });
    const draft = draftOf({ stateTones: { locked: 'warn', unlocked: 'alert' } });

    expect(changeBody(draft, row)).toEqual({ state_tones: { locked: 'warn', unlocked: 'alert' } });
  });

  it('sends the map when a state is added', () => {
    const row = rowOf({ state_tones: { locked: 'ok' } });
    const draft = draftOf({ stateTones: { locked: 'ok', jammed: 'alert' } });

    expect(changeBody(draft, row)).toEqual({ state_tones: { locked: 'ok', jammed: 'alert' } });
  });

  it('sends the map when a state is removed, and a null map counts as empty', () => {
    const row = rowOf({ state_tones: { locked: 'ok', jammed: 'alert' } });

    expect(changeBody(draftOf({ stateTones: { locked: 'ok' } }), row)).toEqual({
      state_tones: { locked: 'ok' }
    });
    expect(changeBody(draftOf({ stateTones: { locked: 'ok' } }), rowOf({ state_tones: null }))).toEqual({
      state_tones: { locked: 'ok' }
    });
    // Removing every override sends the empty map: "none", not "unchanged".
    expect(changeBody(draftOf({ stateTones: {} }), row)).toEqual({ state_tones: {} });
  });

  it('sends colour_by_state only when it flips', () => {
    expect(changeBody(draftOf({ colourByState: true }), rowOf({ colour_by_state: true }))).toEqual({});
    expect(changeBody(draftOf({ colourByState: false }), rowOf({ colour_by_state: true }))).toEqual({
      colour_by_state: false
    });
  });

  it('hands the map over as a copy, so the body cannot alias the draft', () => {
    const draft = draftOf({ stateTones: { locked: 'warn' } });
    const body = changeBody(draft, rowOf());

    draft.stateTones.locked = 'ok';

    expect(body.state_tones).toEqual({ locked: 'warn' });
  });
});

describe('isDirty', () => {
  it('is false for an unchanged draft and true after any change', () => {
    expect(isDirty(draftOf(), rowOf())).toBe(false);
    expect(isDirty(draftOf({ displayName: 'Other' }), rowOf())).toBe(true);
    expect(isDirty(draftOf({ accentColor: '#ff5c5c' }), rowOf())).toBe(true);
    expect(isDirty(draftOf({ iconOverride: 'mdi:door' }), rowOf())).toBe(true);
    expect(isDirty(draftOf({ hideWhenUnavailable: true }), rowOf())).toBe(true);
    expect(isDirty(draftOf({ stateTones: { locked: 'warn' } }), rowOf())).toBe(true);
    expect(isDirty(draftOf({ colourByState: false }), rowOf())).toBe(true);
  });
});

describe('previewTile', () => {
  it('uses the draft override for the icon and marks its source as the user', () => {
    const tile = previewTile(rowOf(), draftOf({ iconOverride: 'mdi:door' }), {});

    expect(tile.icon).toBe('mdi:door');
    expect(tile.iconSource).toBe('user');
  });

  it('falls back to the row icon and source while no override is set', () => {
    const tile = previewTile(rowOf(), draftOf(), {});

    expect(tile.icon).toBe('mdi:lock');
    expect(tile.iconSource).toBe('ha-registry');
  });

  it('keeps the live facts from the row, never from the draft', () => {
    const row = rowOf({
      state: 'unlocked',
      displayed_state: 'Unlocked',
      unit_of_measurement: '%',
      last_changed: '2026-05-05T10:00:00+00:00'
    });

    const tile = previewTile(row, draftOf(), { attributes: { battery_level: 87 } });

    expect(tile.id).toBe(7);
    expect(tile.entityId).toBe('lock.front_door');
    expect(tile.state).toBe('unlocked');
    expect(tile.displayedState).toBe('Unlocked');
    expect(tile.unitOfMeasurement).toBe('%');
    expect(tile.lastChanged).toBe('2026-05-05T10:00:00+00:00');
    expect(tile.attributes).toEqual({ battery_level: 87 });
  });

  it('trims the draft name and treats a blank one as no override', () => {
    expect(previewTile(rowOf(), draftOf({ displayName: '  Porch  ' }), {}).displayName).toBe('Porch');
    expect(previewTile(rowOf(), draftOf({ displayName: '   ' }), {}).displayName).toBeNull();
  });

  it('carries the draft accent and hide flag onto the tile', () => {
    const tile = previewTile(rowOf(), draftOf({ accentColor: '#3ddc84', hideWhenUnavailable: true }), {});

    expect(tile.accentColor).toBe('#3ddc84');
    expect(tile.hideWhenUnavailable).toBe(true);
  });

  it('carries the row’s tone and never resolves one of its own', () => {
    // The preview paints what the backend decided. Editing the draft's map does
    // not recolour the preview: the PATCH response is what moves `row.tone`.
    const row = rowOf({ tone: 'alert', state: 'unlocked', displayed_state: 'Unlocked' });

    expect(previewTile(row, draftOf(), {}).tone).toBe('alert');
    expect(previewTile(row, draftOf({ stateTones: { unlocked: 'ok' } }), {}).tone).toBe('alert');
    expect(previewTile(rowOf({ tone: null }), draftOf(), {}).tone).toBeNull();
  });

  it('filters and reorders the row hints to the draft list', () => {
    const row = rowOf({
      show_attributes: ['battery_level', 'battery_health'],
      attribute_hints: [
        { name: 'battery_level', label: 'Battery', value: '87 %' },
        { name: 'battery_health', label: 'Battery health', value: 'good' }
      ]
    });

    const tile = previewTile(row, draftOf({ showAttributes: ['battery_health'] }), {});

    expect(tile.attributeHints).toEqual([{ name: 'battery_health', label: 'Battery health', value: 'good' }]);
  });

  it('synthesises a hint for a name the server did not resolve, from the live attributes', () => {
    const tile = previewTile(rowOf(), draftOf({ showAttributes: ['battery_level', 'battery_voltage'] }), {
      attributes: { battery_voltage: 3.1 }
    });

    expect(tile.attributeHints).toEqual([
      { name: 'battery_level', label: 'Battery', value: '87 %' },
      { name: 'battery_voltage', label: 'battery_voltage', value: '3.1' }
    ]);
  });

  it('drops a name with no hint and no live value instead of inventing one', () => {
    const tile = previewTile(rowOf(), draftOf({ showAttributes: ['battery_level', 'battery_temperature'] }), {
      attributes: {}
    });

    expect(tile.attributeHints).toEqual([{ name: 'battery_level', label: 'Battery', value: '87 %' }]);
  });

  it('renders no hints when the live attributes are not supplied yet', () => {
    const tile = previewTile(rowOf(), draftOf({ showAttributes: [] }), {});

    expect(tile.attributeHints).toEqual([]);
  });
});

describe('isValidAccentColor', () => {
  it('accepts #rgb and #rrggbb in either case', () => {
    for (const value of ['#abc', '#ABC', '#4f9cf9', '#4F9CF9']) {
      expect(isValidAccentColor(value)).toBe(true);
    }
  });

  it('rejects everything else', () => {
    for (const value of ['', '#', '4f9cf9', '#abcd', '#4f9cf', 'rgb(1,2,3)', '#4F9CF9FF', ' #4f9cf9']) {
      expect(isValidAccentColor(value)).toBe(false);
    }
  });
});

describe('PRESET_ACCENT_COLORS', () => {
  it('offers eight distinct, valid, named swatches', () => {
    expect(PRESET_ACCENT_COLORS).toHaveLength(8);
    expect(new Set(PRESET_ACCENT_COLORS.map((preset) => preset.value)).size).toBe(8);
    expect(new Set(PRESET_ACCENT_COLORS.map((preset) => preset.label)).size).toBe(8);
    for (const preset of PRESET_ACCENT_COLORS) {
      expect(isValidAccentColor(preset.value)).toBe(true);
      expect(preset.label.length).toBeGreaterThan(0);
    }
  });
});
