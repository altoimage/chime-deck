/**
 * The configuration-transfer comparison, in the plain node environment.
 *
 * These pin the merge accounting the About panel prints and, above all, the
 * promise that a malformed document can never make the comparison throw: the
 * server is the validator, and its 422 is the message a user must see.
 */

import { describe, expect, it } from 'vitest';

import { compareDocuments, formatUptime, totalWritten } from './config-transfer.js';

/** The configuration the device already holds, as a redacted export sends it. */
const CURRENT = {
  format: 'chime-deck/config',
  version: 1,
  settings: {
    'ha.base_url': 'http://homeassistant.local:8123',
    'audio.volume': 70,
    'theme.name': 'dark'
  },
  entities: [
    { entity_id: 'lock.front_door', display_name: 'Front Door', icon: 'mdi:door' },
    { entity_id: 'light.hall', display_name: 'Hall', icon: 'mdi:lightbulb' }
  ],
  sounds: [
    { name: 'Ding', source_type: 'file', path: 'data/sounds/ding.mp3', url: null },
    { name: 'Dong', source_type: 'url', path: null, url: 'https://example.local/dong.mp3' }
  ],
  buttons: [{ slug: 'front-door', name: 'Front Door', sound: 'data/sounds/ding.mp3' }]
};

/** A backup that adds, changes and (never) removes: import is a merge. */
const INCOMING = {
  format: 'chime-deck/config',
  version: 1,
  settings: {
    'ha.base_url': 'http://homeassistant.local:8123',
    'audio.volume': 60,
    'mqtt.host': 'mqtt.local',
    // The current export omits secrets, so a token that is already set looks new.
    'ha.token': 'a-long-lived-token'
  },
  entities: [
    { entity_id: 'lock.front_door', display_name: 'Front Door', icon: 'mdi:door' },
    { entity_id: 'light.hall', display_name: 'Hallway', icon: 'mdi:lightbulb' },
    { entity_id: 'light.kitchen', display_name: 'Kitchen', icon: 'mdi:lightbulb' }
  ],
  sounds: [
    { name: 'Ding', source_type: 'file', path: 'data/sounds/ding.mp3', url: null },
    { name: 'Dong', source_type: 'url', path: null, url: 'https://example.local/dong.mp3', format: 'ogg' },
    { name: 'New chime', source_type: 'file', path: 'data/sounds/new.ogg', url: null }
  ],
  buttons: [
    { slug: 'front-door', name: 'Front Door Chime', sound: 'data/sounds/ding.mp3' },
    { slug: 'back-door', name: 'Back Door', sound: 'data/sounds/new.ogg' }
  ]
};

describe('compareDocuments', () => {
  it('counts settings by key: added, changed, and never removed', () => {
    const diff = compareDocuments(CURRENT, INCOMING);
    expect(diff.settings.added).toBe(2);
    expect(diff.settings.changed).toBe(1);
    expect(diff.settings.removed).toBe(0);
    expect(diff.settings.note).toContain('merge keeps absent keys');
  });

  it('says in the settings note that a set secret appears as added', () => {
    const diff = compareDocuments(CURRENT, INCOMING);
    expect(diff.settings.note).toContain('secrets are never exported');
    expect(diff.settings.note).toContain('appears as added');
    // "ha.token" is in the incoming document but absent from the current export.
    expect(CURRENT.settings).not.toHaveProperty('ha.token');
  });

  it('counts entities by entity_id', () => {
    const diff = compareDocuments(CURRENT, INCOMING);
    expect(diff.entities).toEqual({
      added: 1,
      changed: 1,
      removed: 0,
      note: 'merge keeps absent rows'
    });
  });

  it('counts sounds by path for a file sound and by url for a downloaded one', () => {
    const diff = compareDocuments(CURRENT, INCOMING);
    expect(diff.sounds).toEqual({
      added: 1,
      changed: 1,
      removed: 0,
      note: 'merge keeps absent rows'
    });
  });

  it('counts buttons by slug', () => {
    const diff = compareDocuments(CURRENT, INCOMING);
    expect(diff.buttons).toEqual({
      added: 1,
      changed: 1,
      removed: 0,
      note: 'merge keeps absent rows'
    });
  });

  it('keeps removed at zero everywhere, because a merge never deletes', () => {
    const diff = compareDocuments(CURRENT, INCOMING);
    for (const section of [diff.settings, diff.entities, diff.sounds, diff.buttons]) {
      expect(section.removed).toBe(0);
    }
  });

  it('treats an unchanged document as nothing to write', () => {
    const diff = compareDocuments(CURRENT, CURRENT);
    expect(totalWritten(diff)).toBe(0);
  });

  it('never throws on a malformed document and yields zero counts', () => {
    for (const [current, incoming] of [
      [null, null],
      ['x', 'x'],
      [{}, {}],
      [undefined, 42],
      [{ settings: 'nope', entities: 7 }, { sounds: null, buttons: 'no' }]
    ] as const) {
      const diff = compareDocuments(current, incoming);
      expect(totalWritten(diff)).toBe(0);
      expect(diff.settings.removed).toBe(0);
      expect(diff.entities.note).toBe('merge keeps absent rows');
    }
  });

  it('ignores rows without a natural key, which cannot be compared', () => {
    const diff = compareDocuments(
      { entities: [{ display_name: 'no id' }] },
      { entities: [{ display_name: 'still no id' }] }
    );
    expect(diff.entities).toEqual({
      added: 0,
      changed: 0,
      removed: 0,
      note: 'merge keeps absent rows'
    });
  });
});

describe('totalWritten', () => {
  it('is the sum of added and changed over the four sections', () => {
    const diff = compareDocuments(CURRENT, INCOMING);
    const sections = [diff.settings, diff.entities, diff.sounds, diff.buttons];
    const expected = sections.reduce(
      (total, section) => total + section.added + section.changed,
      0
    );
    expect(totalWritten(diff)).toBe(expected);
    expect(totalWritten(diff)).toBe(9);
  });
});

describe('formatUptime', () => {
  it('prints two units at most, largest first', () => {
    expect(formatUptime(8040)).toBe('2 h 14 min');
    expect(formatUptime(90)).toBe('1 min 30 s');
    expect(formatUptime(45)).toBe('45 s');
  });

  it('rolls over into days', () => {
    expect(formatUptime(90000)).toBe('1 d 1 h');
  });

  it('never prints NaN or a negative duration', () => {
    expect(formatUptime(0)).toBe('0 s');
    expect(formatUptime(-5)).toBe('0 s');
    expect(formatUptime(Number.NaN)).toBe('0 s');
  });
});
