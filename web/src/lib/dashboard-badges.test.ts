/**
 * Unit tests for the dashboard header badges (HSD-snsm, D28).
 *
 * Node environment, no DOM: `dashboard-badges.ts` is pure, and it is the only
 * thing that decides whether the moon and crossed-speaker badges draw. The rules
 * worth pinning are the ones that would make the header lie — a moon while the
 * doorbell still chimes (a positive quiet volume, quiet hours disabled, an
 * unreadable time) and a crossed speaker at zero gain with `audio.muted` off.
 *
 * The quiet-hours window is built by shifting the current clock, never by
 * hard-coding `22:00 → 07:00`: these tests must pass at any hour of the day.
 */

import { describe, expect, it } from 'vitest';

import { type BadgeInput, headerBadges } from './dashboard-badges.js';

const MINUTE_MS = 60_000;

/** `HH:MM` in local wall-clock time, the form the registry stores. */
function clockOf(date: Date): string {
  const hour = String(date.getHours()).padStart(2, '0');
  const minute = String(date.getMinutes()).padStart(2, '0');
  return `${hour}:${minute}`;
}

/** *date* moved by *minutes*, which may be negative. */
function shifted(date: Date, minutes: number): Date {
  return new Date(date.getTime() + minutes * MINUTE_MS);
}

/** A clock reading and a one-hour-either-side window that always contains it. */
const NOW = new Date();
const QUIET_START = clockOf(shifted(NOW, -60));
const QUIET_END = clockOf(shifted(NOW, 60));

/** A settings snapshot with both badges inactive, overridden per case. */
function badgeInput(overrides: Partial<BadgeInput> = {}): BadgeInput {
  return {
    now: NOW,
    quietEnabled: false,
    quietStart: QUIET_START,
    quietEnd: QUIET_END,
    quietVolume: 0,
    muted: false,
    volume: 70,
    ...overrides
  };
}

describe('headerBadges', () => {
  it('shows neither badge when nothing is suppressing sound', () => {
    expect(headerBadges(badgeInput())).toEqual({ quietHours: false, muted: false });
  });

  it('shows the moon only, when quiet hours are suppressing at zero volume', () => {
    expect(headerBadges(badgeInput({ quietEnabled: true }))).toEqual({
      quietHours: true,
      muted: false
    });
  });

  it('shows the crossed speaker only, when muted outside the window', () => {
    expect(headerBadges(badgeInput({ muted: true }))).toEqual({
      quietHours: false,
      muted: true
    });
  });

  it('shows both when muted inside a suppressing window', () => {
    expect(headerBadges(badgeInput({ quietEnabled: true, muted: true }))).toEqual({
      quietHours: true,
      muted: true
    });
  });

  it('shows no moon for a positive quiet volume: that plays quieter, it does not suppress', () => {
    expect(headerBadges(badgeInput({ quietEnabled: true, quietVolume: 30 })).quietHours).toBe(
      false
    );
  });

  it('shows no moon when quiet hours are disabled, even inside the window', () => {
    expect(headerBadges(badgeInput({ quietEnabled: false })).quietHours).toBe(false);
  });

  it('shows no moon for a time the registry would reject, never promising a silent night', () => {
    // `7:00` and `24:00` both fail the registry's own pattern (press.py would
    // never enter a window it could not read).
    expect(headerBadges(badgeInput({ quietEnabled: true, quietStart: '7:00' })).quietHours).toBe(
      false
    );
    expect(headerBadges(badgeInput({ quietEnabled: true, quietEnd: '24:00' })).quietHours).toBe(
      false
    );
  });

  it('shows the crossed speaker at zero gain even when audio.muted is off', () => {
    expect(headerBadges(badgeInput({ volume: 0 }))).toEqual({ quietHours: false, muted: true });
  });
});
