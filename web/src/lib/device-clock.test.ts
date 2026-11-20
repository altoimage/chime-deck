/**
 * Unit tests for the device-clock comparison (HSD-wpm0).
 *
 * Node environment, no DOM, no network: the `Date` header is passed in as a
 * string, so the parser is pinned against a fixed header rather than against
 * whatever this machine's clock reports. `formatDeviceClock` is asserted with an
 * explicit zone offset for the same reason: the reader prints a reading in a
 * zone, and a test must not inherit the machine's `TZ`.
 */

import { describe, expect, it } from 'vitest';

import {
  describeDrift,
  driftSeconds,
  formatDeviceClock,
  parseHttpDate
} from './device-clock.js';

const DATE_HEADER = 'Wed, 12 Nov 2026 21:14:07 GMT';
const DATE_EPOCH_MS = Date.UTC(2026, 10, 12, 21, 14, 7);

describe('parseHttpDate', () => {
  it('parses an IMF-fixdate header to the exact epoch milliseconds', () => {
    expect(parseHttpDate(DATE_HEADER)).toBe(DATE_EPOCH_MS);
  });

  it('answers null for a missing, blank or unusable header instead of inventing a time', () => {
    expect(parseHttpDate(null)).toBeNull();
    expect(parseHttpDate(undefined)).toBeNull();
    expect(parseHttpDate('')).toBeNull();
    expect(parseHttpDate('   ')).toBeNull();
    expect(parseHttpDate('not a date')).toBeNull();
  });
});

describe('driftSeconds', () => {
  it('is positive when the device is ahead and negative when it is behind', () => {
    expect(driftSeconds(DATE_EPOCH_MS + 5000, DATE_EPOCH_MS)).toBe(5);
    expect(driftSeconds(DATE_EPOCH_MS - 5000, DATE_EPOCH_MS)).toBe(-5);
    expect(driftSeconds(DATE_EPOCH_MS, DATE_EPOCH_MS)).toBe(0);
  });
});

describe('describeDrift', () => {
  it('calls a difference of one second or less agreement, not drift', () => {
    const agreement = 'the device clock agrees with this browser to within a second';

    expect(describeDrift(-0.4)).toBe(agreement);
    expect(describeDrift(0)).toBe(agreement);
    expect(describeDrift(0.4)).toBe(agreement);
  });

  it('says the device is behind, without negating a negative twice', () => {
    expect(describeDrift(-5)).toBe('the device clock is 5 s behind this browser');
  });

  it('says the device is ahead', () => {
    expect(describeDrift(5)).toBe('the device clock is 5 s ahead of this browser');
  });
});

describe('formatDeviceClock', () => {
  it('zero-pads to HH:MM:SS in the zone it is handed', () => {
    const reading = Date.UTC(2026, 10, 12, 4, 5, 6);

    expect(formatDeviceClock(reading, 0, 0)).toBe('04:05:06');
    expect(formatDeviceClock(reading, 0, 60)).toBe('05:05:06');
    expect(formatDeviceClock(reading, 0, -300)).toBe('23:05:06');
  });

  it('rolls over the day when the elapsed time crosses midnight', () => {
    const endOfDay = Date.UTC(2026, 10, 12, 23, 59, 59);

    // 23:59:59.000 + 1.5 s is 00:00:00.500, so the reading is the new day's first second.
    expect(formatDeviceClock(endOfDay, 1500, 0)).toBe('00:00:00');
    expect(formatDeviceClock(endOfDay, 2000, 0)).toBe('00:00:01');
  });

  it("defaults to this browser's own zone, which is the zone the panel compares against", () => {
    const reading = Date.UTC(2026, 10, 12, 21, 14, 7);
    const local = new Date(reading);
    const expected =
      `${String(local.getHours()).padStart(2, '0')}:` +
      `${String(local.getMinutes()).padStart(2, '0')}:` +
      `${String(local.getSeconds()).padStart(2, '0')}`;

    expect(formatDeviceClock(reading, 0)).toBe(expected);
  });
});
