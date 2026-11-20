// @vitest-environment happy-dom
/**
 * DOM tests for the dashboard control rail (HSD-snsm, D28).
 *
 * The rail is the dashboard's only control surface, and it is present in kiosk
 * mode too, so it is mounted on its own rather than only through the dashboard:
 * a tap that sends the wrong PATCH, a label that does not flip, or a fifth
 * control appearing where four belong fails here first, with nothing else on
 * the page to blame.
 *
 * The suite stays offline: every request goes through `setFetchImpl`, and the
 * fake merges a PATCH into the map it serves so a second tap steps from the
 * value the first one saved.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ControlRail from './ControlRail.svelte';
import { setFetchImpl, type FetchResponse } from './api.js';
import { settings } from './settings.svelte.js';

/* ---- fetch + DOM helpers ----------------------------------------------- */

interface FakeCall {
  path: string;
  init?: RequestInit;
}

/** The settings map the fake serves and a PATCH merges into. */
let settingsStore: Record<string, unknown> = {};

/** Every request the rail made, in order. */
let calls: FakeCall[] = [];

function jsonResponse(body: unknown, status = 200): FetchResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body
  };
}

function settingsFetch(): FakeCall[] {
  const seen: FakeCall[] = [];
  setFetchImpl(async (path, init) => {
    seen.push({ path, init });
    if (path !== '/api/settings') {
      throw new Error(`unexpected request ${path}`);
    }
    if (init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      settingsStore = { ...settingsStore, ...body };
      return jsonResponse({ settings: settingsStore, secrets: {} });
    }
    return jsonResponse({ settings: settingsStore, secrets: {} });
  });
  return seen;
}

/** Every PATCH body the fake saw, in order. */
function patchBodies(seen: FakeCall[]): Array<Record<string, unknown>> {
  return seen
    .filter((call) => call.init?.method === 'PATCH')
    .map((call) => JSON.parse(String(call.init?.body)) as Record<string, unknown>);
}

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function mustFind<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (element === null) {
    throw new Error(`expected to find ${selector}`);
  }
  return element;
}

/** Mount the rail once and return its root element. */
function render(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(ControlRail, { target, props: {} }));
  flushSync();
  return mustFind<HTMLElement>('[data-testid="control-rail"]');
}

function unmountAll(): void {
  for (const component of mounted.splice(0)) {
    unmount(component);
  }
  document.body.innerHTML = '';
}

/** Point the fake at *overrides* and load it, so the store holds a real value. */
async function loadWith(overrides: Record<string, unknown>): Promise<void> {
  settingsStore = { 'audio.volume': 70, ...overrides };
  calls = settingsFetch();
  await settings.load();
}

beforeEach(() => {
  settings.reset();
  setFetchImpl(null);
  settingsStore = {};
  calls = [];
  unmountAll();
});

afterEach(() => {
  unmountAll();
  setFetchImpl(null);
});

/* ---- 1. structure ------------------------------------------------------ */

describe('control rail structure', () => {
  it('renders exactly four controls in order, with their roles and labels', async () => {
    await loadWith({});
    const rail = render();

    expect(rail.getAttribute('role')).toBe('group');
    expect(rail.getAttribute('aria-label')).toBe('Dashboard controls');

    const controls = [...rail.querySelectorAll<HTMLElement>('.control')];
    expect(controls.map((control) => control.dataset.testid)).toEqual([
      'rail-volume-up',
      'rail-volume-down',
      'rail-mute',
      'rail-settings'
    ]);
    // Buttons act; settings navigates, so it is a real anchor.
    expect(controls.map((control) => control.tagName)).toEqual([
      'BUTTON',
      'BUTTON',
      'BUTTON',
      'A'
    ]);
    expect(controls.map((control) => control.getAttribute('aria-label'))).toEqual([
      'Turn volume up',
      'Turn volume down',
      'Mute',
      'Settings'
    ]);
    for (const button of controls.slice(0, 3)) {
      expect(button.getAttribute('type')).toBe('button');
    }
  });

  it('leaves exactly mute and settings when ui.show_control_rail is false', async () => {
    await loadWith({ 'ui.show_control_rail': false });
    const rail = render();

    const controls = [...rail.querySelectorAll<HTMLElement>('.control')];
    expect(controls.map((control) => control.dataset.testid)).toEqual([
      'rail-mute',
      'rail-settings'
    ]);
  });
});

/* ---- 2. volume steps --------------------------------------------------- */

describe('volume taps', () => {
  it('steps up by five points from the echoed value', async () => {
    await loadWith({});
    render();

    const up = mustFind<HTMLButtonElement>('[data-testid="rail-volume-up"]');
    up.click();
    await vi.waitFor(() => {
      expect(patchBodies(calls)).toEqual([{ 'audio.volume': 75 }]);
    });
    await vi.waitFor(() => {
      expect(settings.volume).toBe(75);
    });

    up.click();
    await vi.waitFor(() => {
      expect(patchBodies(calls)).toEqual([{ 'audio.volume': 75 }, { 'audio.volume': 80 }]);
    });
  });

  it('clamps at 100: the end sticks instead of running past the range', async () => {
    await loadWith({ 'audio.volume': 95 });
    render();

    const up = mustFind<HTMLButtonElement>('[data-testid="rail-volume-up"]');
    up.click();
    await vi.waitFor(() => {
      expect(patchBodies(calls)).toEqual([{ 'audio.volume': 100 }]);
    });
    await vi.waitFor(() => {
      expect(settings.volume).toBe(100);
    });

    up.click();
    await vi.waitFor(() => {
      expect(patchBodies(calls)).toEqual([{ 'audio.volume': 100 }, { 'audio.volume': 100 }]);
    });
  });

  it('clamps at 0: the end sticks instead of going negative', async () => {
    await loadWith({ 'audio.volume': 3 });
    render();

    const down = mustFind<HTMLButtonElement>('[data-testid="rail-volume-down"]');
    down.click();
    await vi.waitFor(() => {
      expect(patchBodies(calls)).toEqual([{ 'audio.volume': 0 }]);
    });
    await vi.waitFor(() => {
      expect(settings.volume).toBe(0);
    });

    down.click();
    await vi.waitFor(() => {
      expect(patchBodies(calls)).toEqual([{ 'audio.volume': 0 }, { 'audio.volume': 0 }]);
    });
  });
});

/* ---- 3. mute ----------------------------------------------------------- */

describe('mute', () => {
  it('toggles audio.muted both ways and flips its own label', async () => {
    await loadWith({ 'audio.muted': false });
    render();

    const mute = mustFind<HTMLButtonElement>('[data-testid="rail-mute"]');
    expect(mute.getAttribute('aria-label')).toBe('Mute');

    mute.click();
    await vi.waitFor(() => {
      expect(patchBodies(calls)).toEqual([{ 'audio.muted': true }]);
      expect(mute.getAttribute('aria-label')).toBe('Unmute');
    });

    mute.click();
    await vi.waitFor(() => {
      expect(patchBodies(calls)).toEqual([
        { 'audio.muted': true },
        { 'audio.muted': false }
      ]);
      expect(mute.getAttribute('aria-label')).toBe('Mute');
    });
  });
});

/* ---- 4. settings ------------------------------------------------------- */

describe('settings', () => {
  it('is a real link to /settings, inside the rail', async () => {
    await loadWith({});
    const rail = render();

    const gear = mustFind<HTMLAnchorElement>('[data-testid="rail-settings"]');
    expect(rail.contains(gear)).toBe(true);
    expect(gear.getAttribute('href')).toBe('/settings');
    expect(gear.querySelector('svg')).not.toBeNull();
  });
});
