import { describe, expect, it } from 'vitest';

import {
  applyKioskFlags,
  isKiosk,
  KIOSK_STORAGE_KEY,
  parseKiosk,
  readKiosk,
  type StorageLike
} from './kiosk.js';

describe('parseKiosk', () => {
  it('is on for 1 and true', () => {
    expect(parseKiosk('?kiosk=1')).toBe(true);
    expect(parseKiosk('?kiosk=true')).toBe(true);
  });

  it('accepts the other true words, case-insensitively', () => {
    expect(parseKiosk('?kiosk=yes')).toBe(true);
    expect(parseKiosk('?kiosk=on')).toBe(true);
    expect(parseKiosk('?kiosk=TRUE')).toBe(true);
  });

  it('is off for an explicit false value', () => {
    expect(parseKiosk('?kiosk=0')).toBe(false);
    expect(parseKiosk('?kiosk=false')).toBe(false);
  });

  it('is off when the flag is absent', () => {
    expect(parseKiosk('?other=1')).toBe(false);
    expect(parseKiosk('')).toBe(false);
  });

  it('reads the flag alongside others', () => {
    expect(parseKiosk('?kiosk=1&x=2')).toBe(true);
    expect(parseKiosk('?x=2&kiosk=1')).toBe(true);
  });

  it('never throws on garbage', () => {
    expect(parseKiosk('?kiosk=%')).toBe(false);
    expect(parseKiosk('??&&==')).toBe(false);
  });
});

describe('isKiosk', () => {
  it('is false outside a browser', () => {
    expect(isKiosk()).toBe(false);
  });
});

/** An in-memory `StorageLike` plus a switch to make every access throw. */
function fakeStorage(
  initial: Record<string, string> = {},
  throwing = false
): { storage: StorageLike; data: Record<string, string> } {
  const data: Record<string, string> = { ...initial };
  const guard = (): void => {
    if (throwing) {
      throw new Error('storage denied');
    }
  };
  return {
    data,
    storage: {
      getItem(key: string): string | null {
        guard();
        return key in data ? data[key] : null;
      },
      setItem(key: string, value: string): void {
        guard();
        data[key] = value;
      },
      removeItem(key: string): void {
        guard();
        delete data[key];
      }
    }
  };
}

describe('readKiosk', () => {
  it('remembers an explicit on', () => {
    const { storage, data } = fakeStorage();
    expect(readKiosk('?kiosk=1', storage)).toBe(true);
    expect(data[KIOSK_STORAGE_KEY]).toBe('1');
  });

  it('clears the memory on an explicit off', () => {
    const { storage, data } = fakeStorage({ [KIOSK_STORAGE_KEY]: '1' });
    expect(readKiosk('?kiosk=0', storage)).toBe(false);
    expect(data[KIOSK_STORAGE_KEY]).toBeUndefined();
  });

  it('falls back to the remembered value when the URL is silent', () => {
    const { storage } = fakeStorage({ [KIOSK_STORAGE_KEY]: '1' });
    expect(readKiosk('?other=1', storage)).toBe(true);

    const empty = fakeStorage();
    expect(readKiosk('', empty.storage)).toBe(false);
  });

  it('is URL only with a null store', () => {
    expect(readKiosk('?kiosk=1', null)).toBe(true);
    expect(readKiosk('', null)).toBe(false);
  });

  it('swallows a store that throws and still answers from the URL', () => {
    const { storage } = fakeStorage({}, true);
    expect(() => readKiosk('?kiosk=1', storage)).not.toThrow();
    expect(readKiosk('?kiosk=1', storage)).toBe(true);
    expect(readKiosk('', storage)).toBe(false);
  });
});

describe('applyKioskFlags', () => {
  function doc(): { dataset: Record<string, string | undefined> } {
    return { dataset: {} };
  }

  it('sets the dashboard layout and the kiosk flag together', () => {
    const target = doc();
    applyKioskFlags(target, { kiosk: true, dashboard: true });
    expect(target.dataset.layout).toBe('dashboard');
    expect(target.dataset.kiosk).toBe('true');
  });

  it('sets the page layout and leaves the kiosk flag off when not kiosk', () => {
    const target = doc();
    applyKioskFlags(target, { kiosk: false, dashboard: false });
    expect(target.dataset.layout).toBe('page');
    expect(target.dataset.kiosk).toBeUndefined();
  });

  it('handles the two mixed combinations', () => {
    const kioskOnly = doc();
    applyKioskFlags(kioskOnly, { kiosk: true, dashboard: false });
    expect(kioskOnly.dataset.layout).toBe('page');
    expect(kioskOnly.dataset.kiosk).toBe('true');

    const dashboardOnly = doc();
    applyKioskFlags(dashboardOnly, { kiosk: false, dashboard: true });
    expect(dashboardOnly.dataset.layout).toBe('dashboard');
    expect(dashboardOnly.dataset.kiosk).toBeUndefined();
  });

  it('deletes a previous kiosk flag and leaves other attributes alone', () => {
    const target = { dataset: { kiosk: 'true', theme: 'light' } as Record<string, string> };
    applyKioskFlags(target, { kiosk: false, dashboard: true });
    expect(target.dataset.kiosk).toBeUndefined();
    expect(target.dataset.theme).toBe('light');
  });
});
