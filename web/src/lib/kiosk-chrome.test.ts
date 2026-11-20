import { describe, expect, it } from 'vitest';

import { KIOSK_IDLE_MS, KioskChrome, type KioskDocument } from './kiosk-chrome.js';
import type { Scheduler } from './ws.js';

/** One recorded timer: `active` is the cancel flag the scheduler returns. */
interface FakeTimer {
  fn: () => void;
  active: boolean;
}

/** A scheduler the test runs by hand, tracking how many timers are pending. */
function makeScheduler(): {
  schedule: Scheduler;
  timers: FakeTimer[];
  pending: () => FakeTimer[];
  fire: () => void;
} {
  const timers: FakeTimer[] = [];
  const schedule: Scheduler = (fn) => {
    const timer: FakeTimer = { fn, active: true };
    timers.push(timer);
    return () => {
      timer.active = false;
    };
  };
  return {
    schedule,
    timers,
    pending: () => timers.filter((timer) => timer.active),
    fire: () => {
      const next = timers.find((timer) => timer.active);
      if (next === undefined) {
        throw new Error('no pending timer to fire');
      }
      next.active = false;
      next.fn();
    }
  };
}

interface FakeChromeDocument {
  doc: KioskDocument;
  dataset: Record<string, string | undefined>;
  passiveCalls: Array<{ type: string; passive: boolean }>;
  listenerCount: () => number;
  dispatch: (type: string) => void;
  setQuery: (query: (selector: string) => unknown | null) => void;
}

/** A DOM-free document that records listeners and answers `querySelector`. */
function makeDocument(
  initialQuery: (selector: string) => unknown | null = () => null
): FakeChromeDocument {
  const listeners = new Map<string, Set<() => void>>();
  const dataset: Record<string, string | undefined> = {};
  const passiveCalls: Array<{ type: string; passive: boolean }> = [];
  let query = initialQuery;

  const doc: KioskDocument = {
    documentElement: { dataset },
    addEventListener(type, listener, options): void {
      const set = listeners.get(type) ?? new Set<() => void>();
      set.add(listener);
      listeners.set(type, set);
      passiveCalls.push({ type, passive: options?.passive === true });
    },
    removeEventListener(type, listener): void {
      listeners.get(type)?.delete(listener);
    },
    querySelector(selector: string): unknown | null {
      return query(selector);
    }
  };

  return {
    doc,
    dataset,
    passiveCalls,
    listenerCount: () =>
      [...listeners.values()].reduce((total, set) => total + set.size, 0),
    dispatch: (type: string): void => {
      for (const listener of listeners.get(type) ?? []) {
        listener();
      }
    },
    setQuery: (next): void => {
      query = next;
    }
  };
}

describe('KioskChrome', () => {
  it('hides the cursor after the idle window', () => {
    const scheduler = makeScheduler();
    const fake = makeDocument();
    const chrome = new KioskChrome({ doc: fake.doc, schedule: scheduler.schedule });

    expect(chrome.idleMs).toBe(KIOSK_IDLE_MS);
    chrome.start();
    expect(fake.dataset.cursor).toBeUndefined();

    scheduler.fire();

    expect(chrome.hidden).toBe(true);
    expect(fake.dataset.cursor).toBe('hidden');
  });

  it('shows the cursor again for every activity type', () => {
    for (const type of ['pointermove', 'pointerdown', 'keydown', 'wheel', 'touchstart']) {
      const scheduler = makeScheduler();
      const fake = makeDocument();
      const chrome = new KioskChrome({ doc: fake.doc, schedule: scheduler.schedule });
      chrome.start();
      scheduler.fire();
      expect(chrome.hidden).toBe(true);

      fake.dispatch(type);

      expect(chrome.hidden).toBe(false);
      expect(fake.dataset.cursor).toBeUndefined();
      chrome.stop();
    }
  });

  it('re-arms instead of stacking timers across an activity burst', () => {
    const scheduler = makeScheduler();
    const fake = makeDocument();
    const chrome = new KioskChrome({ doc: fake.doc, schedule: scheduler.schedule });
    chrome.start();
    expect(scheduler.pending()).toHaveLength(1);

    for (let index = 0; index < 10; index += 1) {
      fake.dispatch('pointermove');
      expect(scheduler.pending()).toHaveLength(1);
    }
    // One initial timer plus exactly one per activity event, and every older
    // timer was cancelled — so the live count stays at one.
    expect(scheduler.timers).toHaveLength(11);
  });

  it('does not hide while a role=dialog surface is open', () => {
    const scheduler = makeScheduler();
    let dialogOpen = true;
    const fake = makeDocument((selector) =>
      dialogOpen && selector === '[role="dialog"]' ? { open: true } : null
    );
    const chrome = new KioskChrome({ doc: fake.doc, schedule: scheduler.schedule });
    chrome.start();

    scheduler.fire();
    expect(chrome.hidden).toBe(false);
    expect(fake.dataset.cursor).toBeUndefined();
    expect(scheduler.pending()).toHaveLength(1);

    dialogOpen = false;
    scheduler.fire();
    expect(chrome.hidden).toBe(true);
    expect(fake.dataset.cursor).toBe('hidden');
  });

  it('does not hide while a native dialog[open] surface is open', () => {
    const scheduler = makeScheduler();
    let dialogOpen = true;
    const fake = makeDocument((selector) =>
      dialogOpen && selector === 'dialog[open]' ? { open: true } : null
    );
    const chrome = new KioskChrome({ doc: fake.doc, schedule: scheduler.schedule });
    chrome.start();

    scheduler.fire();
    expect(chrome.hidden).toBe(false);

    dialogOpen = false;
    scheduler.fire();
    expect(chrome.hidden).toBe(true);
  });

  it('cleans up every listener, timer and flag on stop', () => {
    const scheduler = makeScheduler();
    const fake = makeDocument();
    const chrome = new KioskChrome({ doc: fake.doc, schedule: scheduler.schedule });
    chrome.start();
    expect(fake.listenerCount()).toBe(5);

    scheduler.fire();
    expect(chrome.hidden).toBe(true);

    chrome.stop();

    expect(fake.listenerCount()).toBe(0);
    expect(scheduler.pending()).toHaveLength(0);
    expect(fake.dataset.cursor).toBeUndefined();
    expect(chrome.hidden).toBe(false);
  });

  it('is idempotent on start and installs passive listeners', () => {
    const scheduler = makeScheduler();
    const fake = makeDocument();
    const chrome = new KioskChrome({ doc: fake.doc, schedule: scheduler.schedule });

    chrome.start();
    chrome.start();

    expect(fake.listenerCount()).toBe(5);
    expect(scheduler.pending()).toHaveLength(1);
    expect(fake.passiveCalls.every((call) => call.passive)).toBe(true);
    chrome.stop();
  });
});
