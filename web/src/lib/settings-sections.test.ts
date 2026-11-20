import { describe, expect, it } from 'vitest';

import {
  DEFAULT_SECTION,
  SECTIONS,
  isSettingsPath,
  parseSettingsPath,
  settingsPath,
  type SettingsTarget
} from './settings/sections.js';

/* The routing truth table, node environment: pure functions, no DOM. */

describe('isSettingsPath', () => {
  it('accepts the settings root and every path below it', () => {
    expect(isSettingsPath('/settings')).toBe(true);
    expect(isSettingsPath('/settings/')).toBe(true);
    expect(isSettingsPath('/settings/connection')).toBe(true);
    expect(isSettingsPath('/settings/entities/deeper')).toBe(true);
  });

  it('rejects everything else', () => {
    expect(isSettingsPath('/')).toBe(false);
    expect(isSettingsPath('/gallery')).toBe(false);
    expect(isSettingsPath('/settingsx')).toBe(false);
    expect(isSettingsPath('')).toBe(false);
  });
});

describe('parseSettingsPath', () => {
  it('maps the root and its trailing-slash form onto the default section', () => {
    const expected: SettingsTarget = { kind: 'section', id: DEFAULT_SECTION, rest: '' };
    expect(parseSettingsPath('/settings')).toEqual(expected);
    expect(parseSettingsPath('/settings/')).toEqual(expected);
    expect(parseSettingsPath('')).toEqual(expected);
  });

  it('maps a known section, with or without a trailing slash', () => {
    expect(parseSettingsPath('/settings/connection')).toEqual({
      kind: 'section',
      id: 'connection',
      rest: ''
    });
    expect(parseSettingsPath('/settings/connection/')).toEqual({
      kind: 'section',
      id: 'connection',
      rest: ''
    });
  });

  it('maps the entities section, which owns sub-routes of its own', () => {
    expect(parseSettingsPath('/settings/entities')).toEqual({
      kind: 'section',
      id: 'entities',
      rest: ''
    });
    expect(parseSettingsPath('/settings/entities/')).toEqual({
      kind: 'section',
      id: 'entities',
      rest: ''
    });
  });

  it('carries the sub-route tail verbatim as rest', () => {
    expect(parseSettingsPath('/settings/entities/add')).toEqual({
      kind: 'section',
      id: 'entities',
      rest: 'add'
    });
    expect(parseSettingsPath('/settings/entities/7')).toEqual({
      kind: 'section',
      id: 'entities',
      rest: '7'
    });
    expect(parseSettingsPath('/settings/entities/a/b')).toEqual({
      kind: 'section',
      id: 'entities',
      rest: 'a/b'
    });
    // The trailing-slash rule still trims the end, not the tail's middle.
    expect(parseSettingsPath('/settings/entities/7/')).toEqual({
      kind: 'section',
      id: 'entities',
      rest: '7'
    });
  });

  it('carries an unimplemented section as unknown with the raw segment', () => {
    expect(parseSettingsPath('/settings/diagnostics')).toEqual({
      kind: 'unknown',
      requested: 'diagnostics'
    });
    expect(parseSettingsPath('/settings/diagnostics/')).toEqual({
      kind: 'unknown',
      requested: 'diagnostics'
    });
    expect(parseSettingsPath('/settings/diagnostics/new')).toEqual({
      kind: 'unknown',
      requested: 'diagnostics/new'
    });
  });

  it('parses the Sounds section and keeps its tail', () => {
    expect(parseSettingsPath('/settings/sounds')).toEqual({
      kind: 'section',
      id: 'sounds',
      rest: ''
    });
    expect(parseSettingsPath('/settings/sounds/12')).toEqual({
      kind: 'section',
      id: 'sounds',
      rest: '12'
    });
  });

  it('never silently redirects a non-settings path', () => {
    expect(parseSettingsPath('/')).toEqual({ kind: 'unknown', requested: '/' });
    expect(parseSettingsPath('/gallery')).toEqual({ kind: 'unknown', requested: '/gallery' });
  });
});

describe('settingsPath', () => {
  it('round-trips every section', () => {
    for (const section of SECTIONS) {
      expect(settingsPath(section.id)).toBe(section.path);
      expect(parseSettingsPath(settingsPath(section.id))).toEqual({
        kind: 'section',
        id: section.id,
        rest: ''
      });
    }
  });
});

describe('SECTIONS', () => {
  it('has unique ids and unique paths', () => {
    const ids = SECTIONS.map((section) => section.id);
    const paths = SECTIONS.map((section) => section.path);
    expect(new Set(ids).size).toBe(ids.length);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it('is exactly the implemented set, including Security', () => {
    expect(SECTIONS).toEqual([
      {
        id: 'connection',
        label: 'Connection',
        icon: 'mdi:lan-connect',
        path: '/settings/connection'
      },
      {
        id: 'security',
        label: 'Security',
        icon: 'mdi:shield-lock-outline',
        path: '/settings/security'
      },
      {
        id: 'entities',
        label: 'Entities',
        icon: 'mdi:devices',
        path: '/settings/entities'
      },
      {
        id: 'buttons',
        label: 'Buttons',
        icon: 'mdi:bell',
        path: '/settings/buttons'
      },
      {
        id: 'sounds',
        label: 'Sounds',
        icon: 'mdi:music-note',
        path: '/settings/sounds'
      },
      {
        id: 'speech',
        label: 'Speech',
        icon: 'mdi:speaker',
        path: '/settings/speech'
      },
      {
        id: 'audio',
        label: 'Audio',
        icon: 'mdi:volume-high',
        path: '/settings/audio'
      },
      {
        id: 'appearance',
        label: 'Appearance',
        icon: 'mdi:palette-outline',
        path: '/settings/appearance'
      },
      {
        id: 'history',
        label: 'History',
        icon: 'mdi:history',
        path: '/settings/history'
      },
      {
        id: 'about',
        label: 'About',
        icon: 'mdi:information-outline',
        path: '/settings/about'
      }
    ]);
  });

  it('keeps the default section first, so /settings lands on Connection', () => {
    expect(SECTIONS[0].id).toBe(DEFAULT_SECTION);
  });
});
