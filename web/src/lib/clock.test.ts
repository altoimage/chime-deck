import { describe, expect, it } from 'vitest';

import { formatChangedLine, formatClock, formatStamp } from './clock.js';

describe('formatClock', () => {
  it('zero pads a 24-hour local time', () => {
    expect(formatClock(new Date(2026, 10, 15, 9, 4))).toBe('09:04');
  });

  it('prints the afternoon hour as 24-hour', () => {
    expect(formatClock(new Date(2026, 10, 15, 21, 4))).toBe('21:04');
  });

  it('handles midnight and the last minute of the day', () => {
    expect(formatClock(new Date(2026, 10, 15, 0, 0))).toBe('00:00');
    expect(formatClock(new Date(2026, 10, 15, 23, 59))).toBe('23:59');
  });
});

describe('formatStamp', () => {
  it('prints a local date and time with zero padding', () => {
    const iso = new Date(2026, 0, 5, 7, 8).toISOString();

    expect(formatStamp(iso)).toBe('2026-01-05 07:08');
  });

  it('says never reported when there is no timestamp', () => {
    expect(formatStamp(null)).toBe('never reported');
    expect(formatStamp(undefined)).toBe('never reported');
    expect(formatStamp('')).toBe('never reported');
  });

  it('prints an unparseable string verbatim, never Invalid Date', () => {
    const stamp = formatStamp('not-a-date');

    expect(stamp).toBe('not-a-date');
    expect(stamp).not.toContain('NaN');
  });
});

describe('formatChangedLine', () => {
  const now = Date.parse('2026-11-15T12:00:00Z');
  const ago = (milliseconds: number): string => new Date(now - milliseconds).toISOString();

  it('uses the just-now wording for changes under a minute old', () => {
    expect(formatChangedLine(ago(59_999), now)).toBe('Changed just now');
  });

  it('uses singular and plural minute buckets', () => {
    expect(formatChangedLine(ago(60_000), now)).toBe('Changed 1 minute ago');
    expect(formatChangedLine(ago(8 * 60_000), now)).toBe('Changed 8 minutes ago');
  });

  it('uses singular and plural hour buckets', () => {
    expect(formatChangedLine(ago(60 * 60_000), now)).toBe('Changed 1 hour ago');
    expect(formatChangedLine(ago(6 * 60 * 60_000), now)).toBe('Changed 6 hours ago');
  });

  it('uses singular and plural day buckets', () => {
    expect(formatChangedLine(ago(24 * 60 * 60_000), now)).toBe('Changed 1 day ago');
    expect(formatChangedLine(ago(4 * 24 * 60 * 60_000), now)).toBe('Changed 4 days ago');
  });

  it('describes missing timestamps without inventing one', () => {
    expect(formatChangedLine(null, now)).toBe('Never reported');
    expect(formatChangedLine(undefined, now)).toBe('Never reported');
    expect(formatChangedLine('', now)).toBe('Never reported');
  });

  it('prints an unparseable timestamp verbatim', () => {
    expect(formatChangedLine('the backend sent this', now)).toBe(
      'Changed at the backend sent this'
    );
  });

  it('uses an absolute timestamp for a future clock-skew value', () => {
    const future = new Date(now + 61_000).toISOString();
    const line = formatChangedLine(future, now);

    expect(line).toBe(`Changed at ${formatStamp(future)}`);
    expect(line).not.toContain('ago');
  });
});
