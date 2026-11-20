/**
 * Unit tests for icon resolution (HSD-umw).
 *
 * Node environment: `icon.ts` is pure data plus a module-level cache. The rules
 * worth pinning are the ones a later change could silently break — bundled paths
 * must win over fetched ones (a dashboard tile must not change appearance when a
 * background fetch lands), one spelling per icon, and an unknown name is the
 * fallback rather than a throw.
 */

import { beforeEach, describe, expect, it } from 'vitest';

import { FALLBACK_ICON, forgetIconPaths, rememberIconPath, resolveIcon } from './icon.js';

beforeEach(() => {
  forgetIconPaths();
});

describe('resolveIcon', () => {
  it('resolves the bundled set in any of its three spellings', () => {
    const bundled = resolveIcon('mdi:door-open');

    expect(bundled).not.toBe(FALLBACK_ICON);
    expect(resolveIcon('door-open')).toBe(bundled);
    expect(resolveIcon('doorOpen')).toBe(bundled);
  });

  it('resolves the Entities nav icon, which must never be a question mark', () => {
    expect(resolveIcon('mdi:devices')).not.toBe(FALLBACK_ICON);
  });

  it('resolves the Sounds nav icon and its preview controls', () => {
    // The section nav draws on the first paint, so none of these may fall back.
    for (const name of ['mdi:music-note', 'mdi:stop', 'mdi:pencil', 'mdi:delete-outline']) {
      expect(resolveIcon(name)).not.toBe(FALLBACK_ICON);
    }
  });

  it('resolves the dashboard rail controls and the moon badge (D28)', () => {
    // Rail buttons and header badges draw on the first paint, so none of these
    // may be a question mark.
    for (const name of ['mdi:volume-plus', 'mdi:volume-minus', 'mdi:weather-night']) {
      expect(resolveIcon(name)).not.toBe(FALLBACK_ICON);
    }
  });

  it('answers the fallback for a name the bundle does not carry', () => {
    expect(resolveIcon('mdi:not-a-real-icon')).toBe(FALLBACK_ICON);
    expect(resolveIcon('')).toBe(FALLBACK_ICON);
  });
});

describe('rememberIconPath', () => {
  it('remembers a fetched path under either spelling', () => {
    rememberIconPath('mdi:robot-vacuum', 'M0 0H24V24');

    expect(resolveIcon('mdi:robot-vacuum')).toBe('M0 0H24V24');
    expect(resolveIcon('robot-vacuum')).toBe('M0 0H24V24');
    expect(resolveIcon('robotVacuum')).toBe('M0 0H24V24');
  });

  it('normalises a bare name the same way as the mdi: form', () => {
    rememberIconPath('robot-vacuum', 'BARE');

    expect(resolveIcon('mdi:robot-vacuum')).toBe('BARE');
    expect(resolveIcon('robotVacuum')).toBe('BARE');
  });

  it('lets a bundled path win over a remembered one', () => {
    const bundled = resolveIcon('mdi:door');

    rememberIconPath('mdi:door', 'REMEMBERED-SHOULD-NOT-WIN');

    expect(resolveIcon('mdi:door')).toBe(bundled);
    expect(resolveIcon('door')).toBe(bundled);
  });
});

describe('forgetIconPaths', () => {
  it('drops every remembered path and leaves the bundle alone', () => {
    rememberIconPath('mdi:robot-vacuum', 'M0 0H24V24');
    expect(resolveIcon('mdi:robot-vacuum')).toBe('M0 0H24V24');

    forgetIconPaths();

    expect(resolveIcon('mdi:robot-vacuum')).toBe(FALLBACK_ICON);
    expect(resolveIcon('mdi:door')).not.toBe(FALLBACK_ICON);
  });
});
