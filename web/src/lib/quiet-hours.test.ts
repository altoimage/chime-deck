/**
 * Unit tests for the quiet-hours window (HSD-wpm0).
 *
 * Node environment, no DOM. The three window sentences are acceptance criteria
 * and are asserted verbatim, because the panel shows them as its only
 * explanation of what the doorbell will do tonight. The parse table mirrors the
 * backend registry's pattern: anything this accepts is stored, so a coercion
 * here would move a rejection to the save.
 */

import { describe, expect, it } from 'vitest';

import {
  DEFAULT_QUIET_END,
  DEFAULT_QUIET_START,
  describeQuietWindow,
  formatClockTime,
  inQuietWindow,
  parseClockTime
} from './quiet-hours.js';

/** A local wall-clock reading at *hour*:*minute*, so the cases read like the setting. */
function at(hour: number, minute: number): Date {
  return new Date(2026, 0, 1, hour, minute, 0, 0);
}

describe('parseClockTime', () => {
  it('accepts a real 24-hour time', () => {
    expect(parseClockTime('22:00')).toEqual({ hour: 22, minute: 0 });
    expect(parseClockTime('07:00')).toEqual({ hour: 7, minute: 0 });
    expect(parseClockTime('00:00')).toEqual({ hour: 0, minute: 0 });
    expect(parseClockTime('23:59')).toEqual({ hour: 23, minute: 59 });
  });

  it('rejects every near miss instead of coercing it', () => {
    expect(parseClockTime('7:00')).toBeNull();
    expect(parseClockTime('24:00')).toBeNull();
    expect(parseClockTime('22:60')).toBeNull();
    expect(parseClockTime(' 22:00')).toBeNull();
    expect(parseClockTime('')).toBeNull();
    expect(parseClockTime('2200')).toBeNull();
  });

  it('has the registry defaults the backend ships', () => {
    expect(formatClockTime(DEFAULT_QUIET_START)).toBe('22:00');
    expect(formatClockTime(DEFAULT_QUIET_END)).toBe('07:00');
  });
});

describe('formatClockTime', () => {
  it('zero-pads both fields so the string is always HH:MM', () => {
    expect(formatClockTime({ hour: 7, minute: 5 })).toBe('07:05');
    expect(formatClockTime({ hour: 0, minute: 0 })).toBe('00:00');
    expect(formatClockTime({ hour: 23, minute: 59 })).toBe('23:59');
  });
});

describe('inQuietWindow', () => {
  // The backend reference is press.py::in_quiet_hours; every case here is one of
  // its three rules, so the panel can never promise a silent night the doorbell
  // does not honour.
  const NIGHT_START = { hour: 22, minute: 0 };
  const NIGHT_END = { hour: 7, minute: 0 };

  it('is true inside the window', () => {
    expect(inQuietWindow(at(23, 30), NIGHT_START, NIGHT_END)).toBe(true);
  });

  it('is half-open: the start minute is inside, the end minute is outside', () => {
    expect(inQuietWindow(at(22, 0), NIGHT_START, NIGHT_END)).toBe(true);
    expect(inQuietWindow(at(7, 0), NIGHT_START, NIGHT_END)).toBe(false);
  });

  it('treats an empty window as never applying', () => {
    expect(inQuietWindow(at(22, 0), NIGHT_START, { hour: 22, minute: 0 })).toBe(false);
  });

  it('crosses midnight when the start is later than the end', () => {
    expect(inQuietWindow(at(23, 30), NIGHT_START, NIGHT_END)).toBe(true);
    expect(inQuietWindow(at(6, 59), NIGHT_START, NIGHT_END)).toBe(true);
    expect(inQuietWindow(at(7, 0), NIGHT_START, NIGHT_END)).toBe(false);
  });

  it('handles a same-day window without touching midnight', () => {
    const start = { hour: 9, minute: 0 };
    const end = { hour: 17, minute: 0 };

    expect(inQuietWindow(at(8, 59), start, end)).toBe(false);
    expect(inQuietWindow(at(9, 0), start, end)).toBe(true);
    expect(inQuietWindow(at(12, 0), start, end)).toBe(true);
    expect(inQuietWindow(at(17, 0), start, end)).toBe(false);
    expect(inQuietWindow(at(23, 30), start, end)).toBe(false);
  });
});

describe('describeQuietWindow', () => {
  it('says the window crosses midnight when the start is later', () => {
    expect(describeQuietWindow({ hour: 22, minute: 0 }, { hour: 7, minute: 0 })).toBe(
      '22:00 → 07:00, next day'
    );
  });

  it('says the window is the same day when the start is earlier', () => {
    expect(describeQuietWindow({ hour: 9, minute: 0 }, { hour: 17, minute: 0 })).toBe(
      '09:00 → 17:00, same day'
    );
  });

  it('spells out that an empty window means quiet hours never apply', () => {
    expect(describeQuietWindow({ hour: 22, minute: 0 }, { hour: 22, minute: 0 })).toBe(
      '22:00 → 22:00 — the start and end are the same, so the window is empty and quiet hours never apply'
    );
  });
});
