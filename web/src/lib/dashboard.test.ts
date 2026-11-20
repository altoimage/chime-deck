// @vitest-environment happy-dom
/**
 * DOM tests for the dashboard layer (HSD-ckhh).
 *
 * The whole suite stays offline: every request goes through `setFetchImpl`, no
 * test opens a socket or a browser. happy-dom provides the DOM per file while
 * the default `node` environment (and therefore the rest of the suite) is
 * untouched.
 *
 * What these tests can prove is semantics — the right word, kind, label and
 * request for a given frame. They cannot prove layout: a DOM has no CSS
 * engine, so the reflow contract is asserted on the built CSS by
 * `scripts/check-dist.mjs`, and real overflow is HSD-rj5j's Playwright job.
 *
 * happy-dom does implement `PointerEvent`, so the long-press test dispatches a
 * real pointer event rather than a MouseEvent shim.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import Dashboard from './Dashboard.svelte';
import Tile from './Tile.svelte';
import {
  ApiError,
  listEntities,
  setFetchImpl,
  type ButtonRow,
  type EntityResponse,
  type EntityRow,
  type EntityTile,
  type FetchResponse,
  type SnapshotData
} from './api.js';
import { formatStamp } from './clock.js';
import { iconSourceLabel } from './tile-view.js';
import { settings } from './settings.svelte.js';
import { liveState } from './stores.svelte.js';
import { toastStore } from './ui/toast.svelte.js';
import type { CachePayload } from './snapshot-cache.js';

/* ---- fixtures ---------------------------------------------------------- */

function entityRow(overrides: Partial<EntityRow> & Pick<EntityRow, 'entity_id'>): EntityRow {
  return {
    id: 1,
    display_name: null,
    state: null,
    attributes: {},
    icon: 'mdi:door',
    icon_source: 'user',
    accent_color: null,
    last_changed: null,
    displayed_state: null,
    unit_of_measurement: null,
    attribute_hints: [],
    tone: null,
    state_tones: null,
    colour_by_state: true,
    ...overrides
  };
}

function snapshotOf(entities: EntityRow[], buttons: ButtonRow[] = []): SnapshotData {
  return {
    entities,
    buttons,
    health: { ha: 'connected', mqtt: 'connected', audio: 'ready' },
    config_version: 1
  };
}

function tileOf(overrides: Partial<EntityTile> & Pick<EntityTile, 'entityId'>): EntityTile {
  return {
    id: 1,
    displayName: null,
    state: null,
    attributes: {},
    icon: 'mdi:door',
    iconSource: 'user',
    accentColor: null,
    lastChanged: null,
    displayedState: null,
    unitOfMeasurement: null,
    attributeHints: [],
    tone: null,
    hideWhenUnavailable: false,
    selected: false,
    ...overrides
  };
}

function restEntityRow(
  overrides: Partial<EntityResponse> & Pick<EntityResponse, 'entity_id'>
): EntityResponse {
  return {
    id: 1,
    display_name: null,
    icon: 'mdi:door',
    icon_source: 'user',
    icon_override: null,
    accent_color: null,
    show_attributes: [],
    hide_when_unavailable: false,
    sort_order: 0,
    created_at: '2026-01-01T00:00:00+00:00',
    state: null,
    displayed_state: null,
    unit_of_measurement: null,
    attribute_hints: [],
    tone: null,
    state_tones: null,
    colour_by_state: true,
    warning: null,
    ...overrides
  };
}

/* ---- fetch + DOM helpers ----------------------------------------------- */

interface FakeCall {
  path: string;
  init?: RequestInit;
}

function jsonResponse(body: unknown, status = 200): FetchResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body
  };
}

function fakeFetch(
  handler: (path: string, init?: RequestInit) => FetchResponse | Promise<FetchResponse>
): FakeCall[] {
  const calls: FakeCall[] = [];
  setFetchImpl(async (path, init) => {
    calls.push({ path, init });
    return handler(path, init);
  });
  return calls;
}

function mustFind<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (element === null) {
    throw new Error(`expected to find ${selector}`);
  }
  return element;
}

/**
 * The settings map the fake backend serves. A `PATCH /api/settings` merges into
 * it and echoes the merged map, exactly like the real backend, so a second tap
 * sees the value the first one saved instead of the original fixture.
 */
let settingsStore: Record<string, unknown> = {};

/** When set, a settings PATCH answers this message as a `400` envelope. */
let patchError: string | null = null;

/** A settings fake that honours `audio.volume` and `audio.muted` patches. */
function settingsFetch(): FakeCall[] {
  return fakeFetch((path, init) => {
    if (path !== '/api/settings') {
      throw new Error(`unexpected request ${path}`);
    }
    if (init?.method === 'PATCH') {
      if (patchError !== null) {
        return jsonResponse(
          { error: { code: 'validation_error', message: patchError, detail: null } },
          400
        );
      }
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      settingsStore = { ...settingsStore, ...body };
      return jsonResponse({ settings: settingsStore, secrets: {} });
    }
    return jsonResponse({ settings: settingsStore, secrets: {} });
  });
}

/** Every PATCH body the fake saw, in order. */
function patchBodies(calls: FakeCall[]): Array<Record<string, unknown>> {
  return calls
    .filter((call) => call.init?.method === 'PATCH')
    .map((call) => JSON.parse(String(call.init?.body)) as Record<string, unknown>);
}

function pointerEvent(type: string, options: PointerEventInit = {}): PointerEvent {
  return new PointerEvent(type, {
    bubbles: true,
    cancelable: true,
    pointerId: 1,
    pointerType: 'touch',
    isPrimary: true,
    button: 0,
    clientX: 20,
    clientY: 20,
    ...options
  });
}

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function renderDashboard(props: { kiosk?: boolean } = {}): void {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(Dashboard, { target, props }));
  flushSync();
}

function renderTile(props: { tile: EntityTile; stale?: boolean; interactive?: boolean }): void {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(Tile, { target, props }));
  flushSync();
}

function unmountAll(): void {
  for (const component of mounted.splice(0)) {
    unmount(component);
  }
  document.body.innerHTML = '';
}

beforeEach(() => {
  liveState.reset();
  settings.reset();
  settingsStore = {};
  patchError = null;
  setFetchImpl(null);
  unmountAll();
});

afterEach(() => {
  unmountAll();
  setFetchImpl(null);
  toastStore.clear();
  vi.useRealTimers();
});

/* ---- 1. state rendering ------------------------------------------------ */

describe('state rendering', () => {
  // `kind` is the TONE the backend resolved, never a colour this side derives
  // from the state string: the same `locked` is green or amber depending on the
  // tile's mapping (HSD-d5x2).
  const cases: Array<{ row: EntityRow; kind: string; word: string }> = [
    {
      row: entityRow({
        id: 1,
        entity_id: 'lock.front_door',
        display_name: 'Front Door',
        state: 'locked',
        displayed_state: 'Locked',
        icon: 'mdi:lock',
        tone: 'ok'
      }),
      kind: 'ok',
      word: 'Locked'
    },
    {
      row: entityRow({
        id: 2,
        entity_id: 'lock.back_door',
        display_name: 'Back Door',
        state: 'unlocked',
        displayed_state: 'Unlocked',
        icon: 'mdi:lock-open',
        tone: 'alert'
      }),
      kind: 'alert',
      word: 'Unlocked'
    },
    {
      row: entityRow({
        id: 3,
        entity_id: 'cover.garage',
        display_name: 'Garage',
        state: 'open',
        displayed_state: 'Open',
        icon: 'mdi:garage',
        tone: 'warn'
      }),
      kind: 'warn',
      word: 'Open'
    },
    {
      row: entityRow({
        id: 4,
        entity_id: 'cover.gate',
        display_name: 'Gate',
        state: 'closed',
        displayed_state: 'Closed',
        icon: 'mdi:door-closed',
        tone: 'ok'
      }),
      kind: 'ok',
      word: 'Closed'
    },
    {
      row: entityRow({
        id: 5,
        entity_id: 'light.hall',
        display_name: 'Hall Light',
        state: 'on',
        displayed_state: 'On',
        icon: 'mdi:lightbulb',
        tone: 'warn'
      }),
      kind: 'warn',
      word: 'On'
    },
    {
      row: entityRow({
        id: 6,
        entity_id: 'light.porch',
        display_name: 'Porch Light',
        state: 'off',
        displayed_state: 'Off',
        icon: 'mdi:lightbulb-outline',
        tone: 'off'
      }),
      kind: 'off',
      word: 'Off'
    },
    {
      row: entityRow({
        id: 7,
        entity_id: 'sensor.temperature',
        display_name: 'Temperature',
        state: '21.4',
        displayed_state: '21.4',
        unit_of_measurement: '°C',
        icon: 'mdi:thermometer',
        // A reading is not a state: whatever tone the payload carries for it,
        // the tile keeps the plain text colour (design 05 §1).
        tone: 'ok'
      }),
      kind: 'value',
      word: '21.4'
    },
    {
      row: entityRow({
        id: 8,
        entity_id: 'sensor.humidity',
        display_name: 'Humidity',
        state: 'unavailable',
        displayed_state: 'Unavailable',
        icon: 'mdi:water',
        tone: 'off'
      }),
      kind: 'unavailable',
      word: 'Unavailable'
    },
    {
      row: entityRow({
        id: 9,
        entity_id: 'sensor.co2',
        display_name: 'CO2',
        state: 'unknown',
        displayed_state: 'Unknown',
        icon: 'mdi:gauge',
        tone: 'off'
      }),
      kind: 'unknown',
      word: 'Unknown'
    }
  ];

  it('renders the right word and kind for every state through the store', () => {
    liveState.applySnapshot(snapshotOf(cases.map((entry) => entry.row)));
    renderDashboard();

    for (const entry of cases) {
      const tile = mustFind<HTMLElement>(`[data-entity="${entry.row.entity_id}"]`);
      expect(tile.getAttribute('data-kind')).toBe(entry.kind);
      expect(tile.textContent).toContain(entry.word);
    }
  });

  it('keeps a numeric reading and its unit as separate text, never ok/alert', () => {
    liveState.applySnapshot(snapshotOf(cases.map((entry) => entry.row)));
    renderDashboard();

    const tile = mustFind<HTMLElement>('[data-entity="sensor.temperature"]');
    const word = mustFind<HTMLElement>('[data-entity="sensor.temperature"] .word');
    const unit = mustFind<HTMLElement>('[data-entity="sensor.temperature"] .unit');

    expect(word.textContent).toBe('21.4');
    expect(unit.textContent).toBe('°C');
    // Two distinct elements: a screen reader hears both.
    expect(word).not.toBe(unit);

    const kind = tile.getAttribute('data-kind');
    expect(kind).toBe('value');
    expect(kind).not.toBe('ok');
    expect(kind).not.toBe('alert');
  });
});

/* ---- 2. unavailable never implies a value ------------------------------ */

describe('unavailable tiles', () => {
  it('never prints a unit or an attribute hint', () => {
    liveState.applySnapshot(
      snapshotOf([
        entityRow({
          id: 1,
          entity_id: 'sensor.battery',
          display_name: 'Battery',
          state: 'unavailable',
          displayed_state: 'Unavailable',
          unit_of_measurement: '°C',
          attribute_hints: [{ name: 'battery_level', label: 'Battery level', value: '87' }]
        })
      ])
    );
    renderDashboard();

    const tile = mustFind<HTMLElement>('[data-entity="sensor.battery"]');
    expect(tile.textContent).toContain('Unavailable');
    expect(tile.textContent).not.toContain('87');
    expect(tile.textContent).not.toContain('°C');
  });
});

/* ---- 3. hide_when_unavailable ----------------------------------------- */

describe('hide_when_unavailable', () => {
  it('hides the tile once the REST flag is hydrated and shows it again when live', async () => {
    liveState.setConfigRefreshHandler(async (what) => {
      if (what !== 'entities') {
        return {};
      }
      return { entities: await listEntities() };
    });

    fakeFetch((path) => {
      expect(path).toBe('/api/entities');
      return jsonResponse([
        restEntityRow({
          id: 1,
          entity_id: 'sensor.battery',
          display_name: 'Battery',
          state: null,
          hide_when_unavailable: true
        })
      ]);
    });

    liveState.applySnapshot(
      snapshotOf([
        entityRow({
          id: 1,
          entity_id: 'sensor.battery',
          display_name: 'Battery',
          state: 'unavailable',
          displayed_state: 'Unavailable'
        })
      ])
    );
    renderDashboard();

    // The snapshot row never carries the REST-only flag, so it is visible for
    // a moment — the documented transient.
    expect(document.querySelector('[data-entity="sensor.battery"]')).not.toBeNull();

    liveState.refreshConfig('entities');
    await vi.waitFor(() => {
      expect(document.querySelector('[data-entity="sensor.battery"]')).toBeNull();
    });

    liveState.applyEntityState({ entity_id: 'sensor.battery', state: 'locked' });
    flushSync();
    expect(document.querySelector('[data-entity="sensor.battery"]')).not.toBeNull();
  });
});

/* ---- 4. stale is a word, not only opacity ----------------------------- */

describe('stale', () => {
  it('shows the word Stale after the socket drops', () => {
    liveState.applySnapshot(
      snapshotOf([
        entityRow({
          id: 1,
          entity_id: 'lock.front_door',
          display_name: 'Front Door',
          state: 'locked',
          displayed_state: 'Locked'
        })
      ])
    );
    renderDashboard();

    expect(mustFind<HTMLElement>('[data-entity="lock.front_door"]').dataset.stale).toBe('false');

    liveState.markDisconnected();
    flushSync();

    const tile = mustFind<HTMLElement>('[data-entity="lock.front_door"]');
    expect(tile.dataset.stale).toBe('true');
    expect(tile.textContent).toContain('Stale');
  });
});

/* ---- 5. long-press popover -------------------------------------------- */

describe('long-press popover', () => {
  const lastChanged = '2026-11-15T02:11:04.512+00:00';
  const DOOR_CARD = 'li.tile[data-entity="lock.front_door"] button.card';

  function doorTile(iconSource: string, icon = 'mdi:lock'): EntityTile {
    return tileOf({
      entityId: 'lock.front_door',
      displayName: 'Front Door',
      state: 'unlocked',
      displayedState: 'Unlocked',
      icon,
      iconSource,
      lastChanged
    });
  }

  it('opens after 400 ms with the entity id, last change and icon source', () => {
    vi.useFakeTimers();
    renderTile({ tile: doorTile('fallback') });

    const card = mustFind<HTMLButtonElement>(DOOR_CARD);
    card.dispatchEvent(pointerEvent('pointerdown'));
    vi.advanceTimersByTime(400);
    flushSync();

    const dialog = mustFind<HTMLElement>('[role="dialog"]');
    expect(dialog.textContent).toContain('lock.front_door');
    expect(dialog.textContent).toContain(formatStamp(lastChanged));
    expect(dialog.textContent).toContain(iconSourceLabel('fallback'));
  });

  it('does nothing on a plain tap', () => {
    vi.useFakeTimers();
    renderTile({ tile: doorTile('fallback') });

    const card = mustFind<HTMLButtonElement>(DOOR_CARD);
    card.dispatchEvent(pointerEvent('pointerdown'));
    card.dispatchEvent(pointerEvent('pointerup'));
    vi.advanceTimersByTime(400);
    flushSync();

    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it('closes on Escape', () => {
    vi.useFakeTimers();
    renderTile({ tile: doorTile('fallback') });

    const card = mustFind<HTMLButtonElement>(DOOR_CARD);
    card.dispatchEvent(pointerEvent('pointerdown'));
    vi.advanceTimersByTime(400);
    flushSync();

    const dialog = mustFind<HTMLElement>('[role="dialog"]');
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    flushSync();

    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it('prints different labels for icon_source fallback and default', () => {
    vi.useFakeTimers();
    renderTile({ tile: doorTile('fallback') });

    mustFind<HTMLButtonElement>(DOOR_CARD).dispatchEvent(pointerEvent('pointerdown'));
    vi.advanceTimersByTime(400);
    flushSync();
    const fallbackText = mustFind<HTMLElement>('[role="dialog"]').textContent ?? '';

    unmountAll();
    renderTile({ tile: doorTile('default', 'mdi:help-circle-outline') });

    // Shift+Enter is the keyboard equivalent of a long-press (an accessibility
    // addition, not a control): plain Enter/Space now activate the toggle.
    mustFind<HTMLButtonElement>(DOOR_CARD).dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true })
    );
    flushSync();
    const defaultText = mustFind<HTMLElement>('[role="dialog"]').textContent ?? '';

    expect(fallbackText).toContain(iconSourceLabel('fallback'));
    expect(defaultText).toContain(iconSourceLabel('default'));
    expect(iconSourceLabel('fallback')).not.toBe(iconSourceLabel('default'));
  });

  it('stays open through the click the opening gesture leaves on the tile', () => {
    vi.useFakeTimers();
    renderTile({ tile: doorTile('fallback') });

    const card = mustFind<HTMLButtonElement>(DOOR_CARD);
    card.dispatchEvent(pointerEvent('pointerdown'));
    vi.advanceTimersByTime(400);
    flushSync();
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();

    // The browser still sends the press's own click, targeted at the control
    // that owns the popover — that is what used to close it in the same gesture.
    card.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();

    // A click on the page outside the owning tile is still an outside click.
    document.body.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });
});

/* ---- 7. control rail --------------------------------------------------- */

describe('control rail', () => {
  it('shows the volume buttons when the setting is omitted (default on)', async () => {
    settingsStore = { 'audio.volume': 70 };
    const calls = settingsFetch();
    await settings.load();
    expect(calls[0].path).toBe('/api/settings');

    renderDashboard();
    expect(document.querySelector('[data-testid="rail-volume-up"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="rail-volume-down"]')).not.toBeNull();
  });

  it('hides the volume buttons when ui.show_control_rail is false', async () => {
    settingsStore = { 'ui.show_control_rail': false };
    settingsFetch();
    await settings.load();

    renderDashboard();
    expect(document.querySelector('[data-testid="rail-volume-up"]')).toBeNull();
    expect(document.querySelector('[data-testid="rail-volume-down"]')).toBeNull();
    // Mute and settings do not depend on the volume-control setting.
    expect(document.querySelector('[data-testid="rail-mute"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="rail-settings"]')).not.toBeNull();
  });

  it('patches exactly one five-point step per volume tap', async () => {
    settingsStore = { 'audio.volume': 70 };
    const calls = settingsFetch();
    await settings.load();

    renderDashboard();
    const up = mustFind<HTMLButtonElement>('[data-testid="rail-volume-up"]');

    up.click();
    await vi.waitFor(() => {
      expect(patchBodies(calls)).toEqual([{ 'audio.volume': 75 }]);
    });
    await vi.waitFor(() => {
      expect(settings.volume).toBe(75);
    });

    // The echoed 75 is adopted, so the second tap steps from there, not from 70.
    up.click();
    await vi.waitFor(() => {
      expect(patchBodies(calls)).toEqual([{ 'audio.volume': 75 }, { 'audio.volume': 80 }]);
    });
    expect(
      calls
        .filter((call) => call.init?.method === 'PATCH')
        .every((call) => call.path === '/api/settings')
    ).toBe(true);
  });

  it('takes volume down to zero and never below it', async () => {
    settingsStore = { 'audio.volume': 3 };
    const calls = settingsFetch();
    await settings.load();

    renderDashboard();
    const down = mustFind<HTMLButtonElement>('[data-testid="rail-volume-down"]');

    down.click();
    await vi.waitFor(() => {
      expect(patchBodies(calls)).toEqual([{ 'audio.volume': 0 }]);
    });

    down.click();
    await vi.waitFor(() => {
      expect(patchBodies(calls)).toEqual([{ 'audio.volume': 0 }, { 'audio.volume': 0 }]);
    });
  });

  it('toggles the device-wide mute and flips its label', async () => {
    settingsStore = { 'audio.volume': 70, 'audio.muted': false };
    const calls = settingsFetch();
    await settings.load();

    renderDashboard();
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

  it('keeps settings as a real link inside the rail', () => {
    renderDashboard();

    const rail = mustFind<HTMLElement>('[data-testid="control-rail"]');
    const link = mustFind<HTMLAnchorElement>(
      '[data-testid="control-rail"] a[data-testid="rail-settings"]'
    );
    expect(rail.contains(link)).toBe(true);
    expect(link.getAttribute('href')).toBe('/settings');
    expect(link.querySelector('svg')).not.toBeNull();
  });

  it('shows the server message when a patch fails', async () => {
    settingsStore = { 'audio.volume': 70 };
    patchError = 'the audio device is busy';
    const calls = settingsFetch();
    await settings.load();

    renderDashboard();
    expect(document.querySelector('p.rail-error')).toBeNull();

    mustFind<HTMLButtonElement>('[data-testid="rail-volume-up"]').click();
    await vi.waitFor(() => {
      expect(settings.error).toBe('the audio device is busy');
    });
    flushSync();

    const error = mustFind<HTMLElement>('p.rail-error');
    expect(error.getAttribute('role')).toBe('status');
    expect(error.textContent).toBe('the audio device is busy');
    // One PATCH, and the last good value is still what the rail holds.
    expect(patchBodies(calls)).toEqual([{ 'audio.volume': 75 }]);
    expect(settings.volume).toBe(70);
  });
});

/* ---- 7b. settings changes land without a reload ----------------------- */

/*
 * A PATCH echoes the whole settings snapshot, and the dashboard is derived off
 * the store, so a saved setting has to reach the page that is already mounted.
 * Every case here flips one key and asserts on the SAME element the first render
 * produced: a remount in between would hide the failure this suite exists for.
 */
describe('settings changes land without a reload', () => {
  it('applies a column change to the grid that is already mounted', async () => {
    settingsStore = { 'ui.dashboard_columns': 0 };
    const calls = settingsFetch();
    await settings.load();
    liveState.applySnapshot(
      snapshotOf([entityRow({ entity_id: 'lock.front_door', display_name: 'Front Door' })])
    );

    renderDashboard();
    const grid = mustFind<HTMLElement>('ul.tiles');
    expect(grid.getAttribute('data-columns')).toBeNull();

    await expect(settings.save({ 'ui.dashboard_columns': 3 })).resolves.toBe(true);
    flushSync();

    expect(mustFind<HTMLElement>('ul.tiles')).toBe(grid);
    expect(grid.getAttribute('data-columns')).toBe('3');
    expect(patchBodies(calls)).toEqual([{ 'ui.dashboard_columns': 3 }]);
  });

  it('derives a balanced auto column count from the tile count', async () => {
    settingsStore = { 'ui.dashboard_columns': 0 };
    settingsFetch();
    await settings.load();

    // The grid itself is the observable: `--tiles-columns` is what the CSS
    // counts with, while `data-columns` stays the echo of the *setting* — so the
    // automatic arrangement is visible only through the custom property. 4 → 2
    // is the ticket's case (a 3+1 auto-fit left a third of the last row empty);
    // 6 → 3, 9 → 3 and 12 → 4 are the other rectangles.
    const expected: readonly (readonly [number, number])[] = [
      [1, 1],
      [2, 2],
      [3, 3],
      [4, 2],
      [5, 5],
      [6, 3],
      [7, 7],
      [8, 4],
      [9, 3],
      [10, 5],
      [11, 11],
      [12, 4]
    ];

    for (const [count, columns] of expected) {
      unmountAll();
      liveState.applySnapshot(
        snapshotOf(
          Array.from({ length: count }, (_, index) =>
            entityRow({ id: index + 1, entity_id: `lock.tile_${index}` })
          )
        )
      );
      renderDashboard();

      const grid = mustFind<HTMLElement>('ul.tiles');
      expect(grid.getAttribute('data-columns')).toBeNull();
      expect(grid.style.getPropertyValue('--tiles-columns')).toBe(String(columns));
    }
  });

  it('drops the clock from the header that is already mounted', async () => {
    settingsStore = { 'ui.show_clock': true };
    settingsFetch();
    await settings.load();
    liveState.applySnapshot(snapshotOf([]));

    renderDashboard();
    const header = mustFind<HTMLElement>('header');
    expect(document.querySelector('time.clock')).not.toBeNull();

    await expect(settings.save({ 'ui.show_clock': false })).resolves.toBe(true);
    flushSync();

    expect(mustFind<HTMLElement>('header')).toBe(header);
    expect(document.querySelector('time.clock')).toBeNull();
  });

  it('turns off only the volume steps, keeping mute and settings', async () => {
    settingsStore = { 'audio.volume': 70, 'ui.show_control_rail': true };
    settingsFetch();
    await settings.load();
    liveState.applySnapshot(snapshotOf([]));

    renderDashboard();
    const rail = mustFind<HTMLElement>('[data-testid="control-rail"]');
    expect(rail.querySelector('[data-testid="rail-volume-up"]')).not.toBeNull();

    await expect(settings.save({ 'ui.show_control_rail': false })).resolves.toBe(true);
    flushSync();

    expect(mustFind<HTMLElement>('[data-testid="control-rail"]')).toBe(rail);
    expect(rail.querySelector('[data-testid="rail-volume-up"]')).toBeNull();
    expect(rail.querySelector('[data-testid="rail-volume-down"]')).toBeNull();
    // D28: the mute button and the gear are the shell, never gated.
    expect(rail.querySelector('[data-testid="rail-mute"]')).not.toBeNull();
    expect(rail.querySelector('[data-testid="rail-settings"]')).not.toBeNull();
  });
});

/* ---- 8. kiosk ---------------------------------------------------------- */

describe('kiosk mode', () => {
  it('drops only the masthead, keeping the rail and the appliance mark', () => {
    renderDashboard({ kiosk: true });

    expect(document.querySelector('h1')).toBeNull();
    expect(document.querySelector('[data-kiosk="true"]')).not.toBeNull();
    // Functional chrome stays in kiosk: the rail, its mute and its settings link.
    const rail = mustFind<HTMLElement>('[data-testid="control-rail"]');
    expect(rail.querySelector('[data-testid="rail-mute"]')).not.toBeNull();
    const gear = mustFind<HTMLAnchorElement>('[data-testid="rail-settings"]');
    expect(rail.contains(gear)).toBe(true);
    expect(gear.getAttribute('href')).toBe('/settings');
  });

  it('keeps the masthead outside kiosk mode', () => {
    renderDashboard({ kiosk: false });

    expect(mustFind<HTMLElement>('h1').textContent).toBe('ChimeDeck');
    expect(document.querySelector('[data-kiosk="true"]')).toBeNull();
  });

  it('offers a real settings link in the rail outside kiosk mode', () => {
    renderDashboard({ kiosk: false });

    const rail = mustFind<HTMLElement>('[data-testid="control-rail"]');
    const link = mustFind<HTMLAnchorElement>('[data-testid="rail-settings"]');
    expect(rail.contains(link)).toBe(true);
    expect(link.getAttribute('href')).toBe('/settings');
    expect(link.querySelector('svg')).not.toBeNull();
  });
});

/* ---- 9. header badges -------------------------------------------------- */

describe('header badges', () => {
  const MINUTE_MS = 60_000;

  /** `HH:MM` in local wall-clock time, the form the registry stores. */
  function clockOf(date: Date): string {
    const hour = String(date.getHours()).padStart(2, '0');
    const minute = String(date.getMinutes()).padStart(2, '0');
    return `${hour}:${minute}`;
  }

  function shifted(date: Date, minutes: number): Date {
    return new Date(date.getTime() + minutes * MINUTE_MS);
  }

  /** A window that contains the clock reading the mounted component will take. */
  function quietWindowAroundNow(): { start: string; end: string } {
    const now = new Date();
    return { start: clockOf(shifted(now, -60)), end: clockOf(shifted(now, 60)) };
  }

  async function renderWithBadgeSettings(overrides: Record<string, unknown>): Promise<void> {
    settingsStore = { 'audio.volume': 70, ...overrides };
    settingsFetch();
    await settings.load();
    renderDashboard();
    flushSync();
  }

  it('shows the crossed speaker when audio.muted is on', async () => {
    await renderWithBadgeSettings({ 'audio.muted': true });

    expect(mustFind<HTMLElement>('[data-testid="badge-muted"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="badge-quiet-hours"]')).toBeNull();
  });

  it('hides both badges when the device is audible', async () => {
    await renderWithBadgeSettings({ 'audio.muted': false });

    expect(document.querySelector('[data-testid="badge-muted"]')).toBeNull();
    expect(document.querySelector('[data-testid="badge-quiet-hours"]')).toBeNull();
  });

  it('shows the crossed speaker at zero gain even with audio.muted off', async () => {
    await renderWithBadgeSettings({ 'audio.muted': false, 'audio.volume': 0 });

    expect(mustFind<HTMLElement>('[data-testid="badge-muted"]')).not.toBeNull();
  });

  it('shows the moon while quiet hours suppress sound at zero volume', async () => {
    const window = quietWindowAroundNow();
    await renderWithBadgeSettings({
      'audio.quiet_hours_enabled': true,
      'audio.quiet_hours_start': window.start,
      'audio.quiet_hours_end': window.end,
      'audio.quiet_hours_volume': 0
    });

    expect(mustFind<HTMLElement>('[data-testid="badge-quiet-hours"]')).not.toBeNull();
  });

  it('shows no moon when quiet hours play at a positive volume', async () => {
    const window = quietWindowAroundNow();
    await renderWithBadgeSettings({
      'audio.quiet_hours_enabled': true,
      'audio.quiet_hours_start': window.start,
      'audio.quiet_hours_end': window.end,
      'audio.quiet_hours_volume': 40
    });

    expect(document.querySelector('[data-testid="badge-quiet-hours"]')).toBeNull();
  });

  it('follows the settings state without a remount', async () => {
    await renderWithBadgeSettings({ 'audio.muted': false });
    expect(document.querySelector('[data-testid="badge-muted"]')).toBeNull();

    // Same mounted tree: only the store's payload changes.
    settingsStore = { ...settingsStore, 'audio.muted': true };
    await settings.load();
    flushSync();
    expect(mustFind<HTMLElement>('[data-testid="badge-muted"]')).not.toBeNull();

    settingsStore = { ...settingsStore, 'audio.muted': false };
    await settings.load();
    flushSync();
    expect(document.querySelector('[data-testid="badge-muted"]')).toBeNull();
  });
});

/* ---- 10. cached-snapshot honesty -------------------------------------- */

describe('cached snapshot honesty', () => {
  function cachedPayload(entities: EntityTile[]): CachePayload {
    return {
      configVersion: 7,
      entities,
      buttons: [],
      health: { ha: 'connected', mqtt: 'connected', audio: 'ready', detail: null }
    };
  }

  beforeEach(() => {
    liveState.reset();
    liveState.setConfigRefreshHandler(null);
  });

  afterEach(() => {
    liveState.reset();
    liveState.setConfigRefreshHandler(null);
  });

  it('renders restored tiles as stale and shows the last-known banner', () => {
    setFetchImpl(async () => {
      throw new Error('offline');
    });

    liveState.restoreCached(
      cachedPayload([
        tileOf({
          entityId: 'lock.front_door',
          displayName: 'Front Door',
          state: 'locked',
          displayedState: 'Locked'
        })
      ])
    );
    renderDashboard();

    expect(mustFind<HTMLElement>('.page').dataset.restored).toBe('true');
    expect(document.body.textContent).toContain('Showing last known state');

    const tile = mustFind<HTMLElement>('li.tile[data-entity="lock.front_door"]');
    expect(tile.dataset.stale).toBe('true');
    expect(tile.querySelector('.stale')).not.toBeNull();
    expect(tile.textContent).toContain('Stale');

    // Nothing on screen claims the tiles are live.
    expect(document.body.textContent).not.toContain('Live');
  });

  it('drops the banner and the stale chips once a real snapshot lands', () => {
    setFetchImpl(async () => {
      throw new Error('offline');
    });

    liveState.restoreCached(
      cachedPayload([
        tileOf({
          entityId: 'lock.front_door',
          displayName: 'Front Door',
          state: 'locked',
          displayedState: 'Locked'
        })
      ])
    );
    renderDashboard();
    expect(mustFind<HTMLElement>('.page').dataset.restored).toBe('true');

    liveState.applySnapshot(
      snapshotOf([
        entityRow({
          id: 1,
          entity_id: 'lock.front_door',
          display_name: 'Front Door',
          state: 'locked',
          displayed_state: 'Locked'
        })
      ])
    );
    flushSync();

    expect(mustFind<HTMLElement>('.page').dataset.restored).toBeUndefined();
    expect(document.body.textContent).not.toContain('Showing last known state');

    const tile = mustFind<HTMLElement>('li.tile[data-entity="lock.front_door"]');
    expect(tile.dataset.stale).toBe('false');
    expect(tile.querySelector('.stale')).toBeNull();
  });
});

/* ---- 11. tile selection ----------------------------------------------- */

/*
 * D29: a tap toggles exactly one tile's selection. The tap paints first
 * (optimistic) and the POST is the reconciliation; a broadcast frame carries
 * the absolute value, so replaying it is idempotent. Long-press and scroll are
 * the gestures that must NOT toggle.
 */
describe('tile selection', () => {
  const DOOR = 'lock.front_door';
  const DOOR_CARD = 'li.tile[data-entity="lock.front_door"] button.card';
  const CUE = '[data-testid="tile-selection-cue"]';

  function selectionSnapshot(lockSelected = false): SnapshotData {
    return snapshotOf([
      entityRow({
        id: 1,
        entity_id: DOOR,
        display_name: 'Front Door',
        state: 'locked',
        displayed_state: 'Locked',
        icon: 'mdi:lock',
        selected: lockSelected
      }),
      entityRow({
        id: 2,
        entity_id: 'light.hall',
        display_name: 'Hall Light',
        state: 'off',
        displayed_state: 'Off',
        icon: 'mdi:lightbulb'
      })
    ]);
  }

  function toggleResponse(selected: boolean): FetchResponse {
    return jsonResponse({ id: 1, entity_id: DOOR, selected });
  }

  /** Dispatch the press a real tap produces, up to and including its click. */
  function tap(): void {
    const card = mustFind<HTMLButtonElement>(DOOR_CARD);
    card.dispatchEvent(pointerEvent('pointerdown'));
    card.dispatchEvent(pointerEvent('pointerup'));
    card.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    flushSync();
  }

  it('a long-press opens the debug popover and does not change the selection', () => {
    vi.useFakeTimers();
    const calls = fakeFetch(() => {
      throw new Error('a long-press must not toggle the selection');
    });
    liveState.applySnapshot(selectionSnapshot());
    renderDashboard();

    const card = mustFind<HTMLButtonElement>(DOOR_CARD);
    card.dispatchEvent(pointerEvent('pointerdown'));
    vi.advanceTimersByTime(400);
    flushSync();
    card.dispatchEvent(pointerEvent('pointerup'));
    card.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    flushSync();

    expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1);
    expect(card.getAttribute('aria-pressed')).toBe('false');
    expect(calls).toHaveLength(0);
  });

  it('a tap toggles the selection and opens no popover', async () => {
    const calls = fakeFetch((path, init) => {
      expect(path).toBe('/api/tiles/1/selection');
      expect(init?.method).toBe('POST');
      return toggleResponse(true);
    });
    liveState.applySnapshot(selectionSnapshot());
    renderDashboard();
    const card = mustFind<HTMLButtonElement>(DOOR_CARD);

    tap();

    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(card.getAttribute('aria-pressed')).toBe('true');
    await vi.waitFor(() => expect(calls).toHaveLength(1));
  });

  it('a scroll changes nothing', () => {
    const calls = fakeFetch(() => {
      throw new Error('a scroll must not toggle the selection');
    });
    liveState.applySnapshot(selectionSnapshot());
    renderDashboard();

    const card = mustFind<HTMLButtonElement>(DOOR_CARD);
    card.dispatchEvent(pointerEvent('pointerdown'));
    card.dispatchEvent(pointerEvent('pointermove', { clientX: 60 }));
    card.dispatchEvent(pointerEvent('pointerup'));
    card.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    flushSync();

    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(card.getAttribute('aria-pressed')).toBe('false');
    expect(calls).toHaveLength(0);
  });

  it('toggles on, then off', async () => {
    const answers = [true, false];
    const calls = fakeFetch((path, init) => {
      expect(path).toBe('/api/tiles/1/selection');
      expect(init?.method).toBe('POST');
      return toggleResponse(answers.shift() ?? false);
    });
    liveState.applySnapshot(selectionSnapshot());
    renderDashboard();

    const card = mustFind<HTMLButtonElement>(DOOR_CARD);
    const door = mustFind<HTMLElement>(`li.tile[data-entity="${DOOR}"]`);

    tap();

    expect(card.getAttribute('aria-pressed')).toBe('true');
    expect(door.dataset.selected).toBe('true');
    const cue = mustFind<HTMLElement>(`${DOOR_CARD} ${CUE}`);
    expect(card.contains(cue)).toBe(true);
    expect(cue.querySelector('svg')).not.toBeNull();

    await vi.waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].path).toBe('/api/tiles/1/selection');
    expect(calls[0].init?.method).toBe('POST');
    expect(calls[0].init?.body).toBeUndefined();

    tap();

    expect(card.getAttribute('aria-pressed')).toBe('false');
    expect(door.dataset.selected).toBe('false');
    expect(document.querySelector(CUE)).toBeNull();

    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].path).toBe('/api/tiles/1/selection');
    expect(calls[1].init?.method).toBe('POST');
    expect(calls[1].init?.body).toBeUndefined();
  });

  it('paints the selection optimistically, before the request answers', async () => {
    let resolveToggle: ((response: FetchResponse) => void) | null = null;
    const calls = fakeFetch(
      () =>
        new Promise<FetchResponse>((resolve) => {
          resolveToggle = resolve;
        })
    );
    liveState.applySnapshot(selectionSnapshot());
    renderDashboard();
    const card = mustFind<HTMLButtonElement>(DOOR_CARD);

    tap();

    // The request is in flight: the tap is already on screen.
    expect(calls).toHaveLength(1);
    expect(card.getAttribute('aria-pressed')).toBe('true');
    expect(mustFind<HTMLElement>(`${DOOR_CARD} ${CUE}`)).not.toBeNull();

    expect(resolveToggle).not.toBeNull();
    resolveToggle!(toggleResponse(true));
    await vi.waitFor(() => expect(liveState.isSelected(DOOR)).toBe(true));
  });

  it('rolls back a failed toggle and surfaces the server message', async () => {
    const calls = fakeFetch(() => {
      throw new ApiError({
        status: 503,
        code: 'internal_error',
        message: 'the selection could not be saved',
        detail: null
      });
    });
    liveState.applySnapshot(selectionSnapshot());
    renderDashboard();
    const card = mustFind<HTMLButtonElement>(DOOR_CARD);
    const door = mustFind<HTMLElement>(`li.tile[data-entity="${DOOR}"]`);

    tap();
    await vi.waitFor(() => expect(toastStore.items).toHaveLength(1));
    flushSync();

    expect(card.getAttribute('aria-pressed')).toBe('false');
    expect(door.dataset.selected).toBe('false');
    expect(document.querySelector(CUE)).toBeNull();
    const toast = toastStore.items.at(-1);
    expect(toast).toMatchObject({ kind: 'error' });
    expect(toast?.message).toContain('the selection could not be saved');
    expect(calls).toHaveLength(1);
  });

  it('surfaces a failed toggle even when a broadcast moved the value under it', async () => {
    let failToggle: ((cause: unknown) => void) | null = null;
    const calls = fakeFetch(
      () =>
        new Promise<FetchResponse>((_resolve, reject) => {
          failToggle = reject;
        })
    );
    liveState.applySnapshot(selectionSnapshot());
    renderDashboard();
    const card = mustFind<HTMLButtonElement>(DOOR_CARD);

    tap();
    // The tap is painted optimistically and its request is still in flight.
    expect(card.getAttribute('aria-pressed')).toBe('true');

    // A late `entity_state` frame from an earlier toggle lands first: the value
    // is the server's now, but this tap still failed.
    liveState.applyEntityState({ entity_id: DOOR, selected: false });
    flushSync();
    expect(card.getAttribute('aria-pressed')).toBe('false');

    expect(failToggle).not.toBeNull();
    failToggle!(
      new ApiError({
        status: 503,
        code: 'internal_error',
        message: 'the selection could not be saved',
        detail: null
      })
    );
    await vi.waitFor(() => expect(toastStore.items).toHaveLength(1));
    flushSync();

    const toast = toastStore.items.at(-1);
    expect(toast).toMatchObject({ kind: 'error' });
    expect(toast?.message).toContain('the selection could not be saved');
    // Only the revert is conditional; the store keeps the value the broadcast
    // gave it instead of undoing it.
    expect(liveState.isSelected(DOOR)).toBe(false);
    expect(card.getAttribute('aria-pressed')).toBe('false');
    expect(calls).toHaveLength(1);
  });

  it('applies a broadcast idempotently and lets an absolute frame clear it', async () => {
    const calls = fakeFetch((path, init) => {
      expect(path).toBe('/api/tiles/1/selection');
      expect(init?.method).toBe('POST');
      return toggleResponse(true);
    });
    liveState.applySnapshot(selectionSnapshot());
    renderDashboard();
    const card = mustFind<HTMLButtonElement>(DOOR_CARD);

    tap();
    await vi.waitFor(() => expect(calls).toHaveLength(1));

    // The hub echoes the optimistic value: same answer, no second POST.
    liveState.applyEntityState({ entity_id: DOOR, selected: true });
    flushSync();
    expect(card.getAttribute('aria-pressed')).toBe('true');
    expect(document.querySelector(CUE)).not.toBeNull();
    expect(calls).toHaveLength(1);

    // The server is the truth: an absolute `false` clears the tile.
    liveState.applyEntityState({ entity_id: DOOR, selected: false });
    flushSync();
    expect(card.getAttribute('aria-pressed')).toBe('false');
    expect(document.querySelector(CUE)).toBeNull();
  });

  it('keeps the state word and unit when a selection-only frame arrives', () => {
    liveState.applySnapshot(
      snapshotOf([
        entityRow({
          id: 1,
          entity_id: DOOR,
          display_name: 'Front Door',
          state: 'locked',
          displayed_state: 'Locked',
          icon: 'mdi:lock'
        }),
        entityRow({
          id: 2,
          entity_id: 'sensor.hallway_temperature',
          display_name: 'Hallway Temperature',
          state: '21.4',
          displayed_state: '21.4',
          unit_of_measurement: '°C',
          icon: 'mdi:thermometer'
        })
      ])
    );
    renderDashboard();

    // Regression guard: the minimal frame carries only `{entity_id, selected}`;
    // assigning `state` unconditionally used to blank the word.
    liveState.applyEntityState({ entity_id: DOOR, selected: true });
    flushSync();

    expect(mustFind<HTMLElement>(`li.tile[data-entity="${DOOR}"] .word`).textContent).toBe(
      'Locked'
    );
    expect(
      mustFind<HTMLElement>('li.tile[data-entity="sensor.hallway_temperature"] .word').textContent
    ).toBe('21.4');
    expect(
      mustFind<HTMLElement>('li.tile[data-entity="sensor.hallway_temperature"] .unit').textContent
    ).toBe('°C');
  });

  it('announces state, selection and staleness in the accessible name', () => {
    liveState.applySnapshot(selectionSnapshot());
    renderDashboard();
    const card = mustFind<HTMLButtonElement>(DOOR_CARD);

    expect(card.getAttribute('aria-label')).toBe('Front Door, Locked, not selected');

    liveState.setSelected(DOOR, true);
    flushSync();
    expect(card.getAttribute('aria-label')).toBe('Front Door, Locked, selected');

    liveState.setSelected(DOOR, false);
    liveState.markDisconnected();
    flushSync();
    expect(card.getAttribute('aria-label')).toBe('Front Door, Locked, not selected, not live');
  });

  it('keeps the editor preview inert', () => {
    renderTile({
      tile: tileOf({
        entityId: DOOR,
        displayName: 'Front Door',
        state: 'locked',
        displayedState: 'Locked'
      }),
      interactive: false
    });

    const tile = mustFind<HTMLElement>(`li.tile[data-entity="${DOOR}"]`);
    expect(tile.querySelector('button')).toBeNull();
    expect(document.querySelector('[aria-pressed]')).toBeNull();
    const card = mustFind<HTMLElement>(`li.tile[data-entity="${DOOR}"] .card`);
    expect(card.tagName).toBe('DIV');
  });

  it('keeps the cue on a stale tile and on an unavailable tile', () => {
    liveState.applySnapshot(selectionSnapshot(true));
    renderDashboard();
    liveState.markDisconnected();
    flushSync();

    const stale = mustFind<HTMLElement>(`li.tile[data-entity="${DOOR}"]`);
    expect(stale.dataset.stale).toBe('true');
    expect(stale.dataset.selected).toBe('true');
    expect(stale.querySelector(CUE)).not.toBeNull();

    liveState.applySnapshot(
      snapshotOf([
        entityRow({
          id: 3,
          entity_id: 'sensor.battery',
          display_name: 'Battery',
          state: 'unavailable',
          displayed_state: 'Unavailable',
          icon: 'mdi:water',
          selected: true
        })
      ])
    );
    flushSync();

    const unavailable = mustFind<HTMLElement>('li.tile[data-entity="sensor.battery"]');
    expect(unavailable.dataset.kind).toBe('unavailable');
    expect(unavailable.dataset.selected).toBe('true');
    expect(unavailable.querySelector(CUE)).not.toBeNull();
  });
});

describe('empty dashboard', () => {
  it('points the owner to settings when no status tiles are configured', () => {
    liveState.applySnapshot(snapshotOf([]));
    renderDashboard();

    const text = (document.body.textContent ?? '').replace(/\s+/g, ' ').trim();
    expect(text).toContain('No status tiles configured');
    expect(text).toContain('Open the settings gear in the control rail to add your first tile.');
    expect(text).not.toContain('The entity editor arrives in a later update.');
    expect(document.querySelector('[role="alert"]')).toBeNull();
    expect(document.querySelector('li.tile')).toBeNull();
    expect(mustFind<HTMLElement>('[data-testid="control-rail"]')).not.toBeNull();
  });
});
