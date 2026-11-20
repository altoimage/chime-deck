// @vitest-environment happy-dom
/**
 * Acceptance tests for the Entities panel set (HSD-4umw): the reorder list
 * (S09), the Add-entities picker (S07), the entity editor (S08) and the icon
 * sheet that editor opens.
 *
 * The describes are numbered one per acceptance criterion, in the ticket's own
 * order. Each `it` proves a semantic fact — the word on screen, the exact key
 * in a request body, the exact number of requests — never layout: a DOM has no
 * CSS engine, so the sticky-header and drag-box tests assert the source
 * declaration and the pointer maths, and real layout stays HSD-rj5j's job.
 *
 * Fully offline: every request goes through `setFetchImpl`, the websocket in
 * AC1 is an in-memory `FakeSocket`, and no browser is downloaded.
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import Dashboard from '../Dashboard.svelte';
import {
  listEntities,
  setFetchImpl,
  type EntityResponse,
  type EntityRow,
  type FetchLike,
  type FetchResponse,
  type HaEntityDetail,
  type HaEntityRow,
  type SnapshotData
} from '../api.js';
import { CONFIG_CHANGED_ENTITIES_FRAME } from '../fixtures/wire.js';
import { settings } from '../settings.svelte.js';
import { liveState } from '../stores.svelte.js';
import { ICON_SOURCE_LABELS, iconSourceLabel } from '../tile-view.js';
import { toastStore } from '../ui/toast.svelte.js';
import { WsClient, type SocketEvent, type SocketLike, type SocketListener } from '../ws.js';
import EntitiesPanel from './EntitiesPanel.svelte';
import EntityEditor from './EntityEditor.svelte';
import EntityPicker from './EntityPicker.svelte';

/* ---- fixtures ----------------------------------------------------------- */

/** A stored `GET /api/entities` row, with the resolver's fields spelled out. */
function entityRow(overrides: Partial<EntityResponse> = {}): EntityResponse {
  return {
    id: 1,
    entity_id: 'lock.front_door',
    display_name: 'Front Door',
    icon: 'mdi:lock',
    icon_source: 'ha-attribute',
    icon_override: null,
    accent_color: null,
    show_attributes: [],
    hide_when_unavailable: false,
    sort_order: 0,
    created_at: '2024-01-01T00:00:00.000Z',
    state: 'locked',
    displayed_state: 'Locked',
    unit_of_measurement: null,
    attribute_hints: [],
    tone: 'ok',
    state_tones: null,
    colour_by_state: true,
    warning: null,
    ...overrides
  };
}

/** One live Home Assistant search hit. */
function haRow(overrides: Partial<HaEntityRow> = {}): HaEntityRow {
  return {
    entity_id: 'lock.front_door',
    name: 'Front Door',
    state: 'locked',
    icon: 'mdi:door',
    icon_source: 'ha-attribute',
    domain: 'lock',
    device_class: null,
    ...overrides
  };
}

/** One `GET /api/ha/entities/{id}` answer (the editor's live detail). */
function haDetail(overrides: Partial<HaEntityDetail> = {}): HaEntityDetail {
  return {
    ...haRow(),
    attributes: {},
    last_changed: null,
    found: true,
    warning: null,
    ...overrides
  };
}

/** One `snapshot` frame row, the wire shape the dashboard renders. */
function snapshotRow(overrides: Partial<EntityRow> & Pick<EntityRow, 'entity_id'>): EntityRow {
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

function snapshotOf(entities: EntityRow[]): SnapshotData {
  return {
    entities,
    buttons: [],
    health: { ha: 'connected', mqtt: 'connected', audio: 'ready' },
    config_version: 1
  };
}

/* ---- fake backend ------------------------------------------------------- */

interface FakeCall {
  path: string;
  init?: RequestInit;
}

interface BackendOptions {
  /** The rows `GET /api/entities` answers, and the store POST/PATCH mutate. */
  rows?: EntityResponse[];
  /** `GET /api/ha/entities?q=` answer for one query. */
  search?: (q: string) => unknown;
  /** `GET /api/ha/entities/{id}` answer for one entity id. */
  detail?: (entityId: string) => unknown;
  /** When set, the search request fails instead of answering. */
  searchFailure?: { status: number; message: string } | 'network';
  /** When set, the reorder request fails with this sentence (422 envelope). */
  reorderError?: string;
  /** When set, every PATCH fails with this sentence (422 envelope). */
  patchError?: string;
  /** When set, every DELETE fails with this sentence (422 envelope). */
  deleteError?: string;
  /** The row a `POST /api/entities` answers with; default: derived from the body. */
  created?: EntityResponse;
  /** `GET /api/icons/search?q=` answer for one query. */
  icons?: (q: string) => unknown;
  /** `GET /api/icons/custom` answer. */
  custom?: unknown;
}

interface Backend {
  calls: FakeCall[];
  /** The mutable server truth, in `sort_order` order. */
  rows: EntityResponse[];
}

function jsonResponse(body: unknown, status = 200): FetchResponse {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function errorResponse(status: number, message: string): FetchResponse {
  return jsonResponse({ error: { code: 'test_error', message, detail: null } }, status);
}

function queryOf(path: string): string {
  const start = path.indexOf('?');
  return start === -1 ? '' : new URLSearchParams(path.slice(start + 1)).get('q') ?? '';
}

/**
 * One in-memory backend: `GET /api/entities` returns the stored order, a
 * reorder renumbers it, a PATCH merges the body. That is what lets AC3 assert
 * persistence as server truth rather than as local state.
 */
function fakeBackend(options: BackendOptions = {}): Backend {
  const calls: FakeCall[] = [];
  const rows: EntityResponse[] = [...(options.rows ?? [])];

  const impl: FetchLike = async (path, init) => {
    calls.push({ path, init });
    const method = init?.method ?? 'GET';

    if (path === '/api/icon/') {
      return jsonResponse({ name: 'x', path: 'M0 0h1v1H0z', view_box: '0 0 24 24' });
    }
    if (path.startsWith('/api/icon/')) {
      return jsonResponse({ name: 'x', path: 'M0 0h1v1H0z', view_box: '0 0 24 24' });
    }
    if (path === '/api/entities/state-tones') {
      return jsonResponse({ tones: ['ok', 'warn', 'alert', 'off'], defaults: { locked: 'ok' } });
    }

    if (path === '/api/entities/reorder' && method === 'POST') {
      if (options.reorderError !== undefined) {
        return errorResponse(422, options.reorderError);
      }
      const body = JSON.parse(String(init?.body)) as { ids: number[] };
      const byId = new Map(rows.map((row) => [row.id, row]));
      const next: EntityResponse[] = [];
      for (const id of body.ids) {
        const found = byId.get(id);
        if (found !== undefined) {
          next.push(found);
          byId.delete(id);
        }
      }
      for (const leftover of byId.values()) {
        next.push(leftover);
      }
      rows.splice(
        0,
        rows.length,
        ...next.map((row, index) => ({ ...row, sort_order: index }))
      );
      return jsonResponse({ ids: body.ids });
    }

    if (path === '/api/entities' && method === 'POST') {
      const body = JSON.parse(String(init?.body)) as { entity_id: string };
      const created = options.created ?? entityRow({ id: rows.length + 101, entity_id: body.entity_id });
      rows.push(created);
      return jsonResponse(created, 201);
    }

    if (path.startsWith('/api/entities/') && method === 'PATCH') {
      if (options.patchError !== undefined) {
        return errorResponse(422, options.patchError);
      }
      const id = Number(decodeURIComponent(path.slice('/api/entities/'.length)));
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      const index = rows.findIndex((row) => row.id === id);
      if (index === -1) {
        return errorResponse(404, `no stored entity with id ${id}`);
      }
      const updated = { ...rows[index], ...body } as EntityResponse;
      rows[index] = updated;
      return jsonResponse(updated);
    }

    if (path.startsWith('/api/entities/') && method === 'DELETE') {
      const id = Number(decodeURIComponent(path.slice('/api/entities/'.length)));
      if (options.deleteError !== undefined) {
        return errorResponse(422, options.deleteError);
      }
      const index = rows.findIndex((row) => row.id === id);
      if (index === -1) {
        return errorResponse(404, `no stored entity with id ${id}`);
      }
      const [removed] = rows.splice(index, 1);
      return jsonResponse({ deleted: true, id, entity_id: removed.entity_id });
    }

    if (path === '/api/entities') {
      return jsonResponse(rows);
    }

    if (path.startsWith('/api/ha/entities?')) {
      if (options.searchFailure === 'network') {
        throw new Error('connection refused');
      }
      if (options.searchFailure !== undefined) {
        return errorResponse(options.searchFailure.status, options.searchFailure.message);
      }
      return jsonResponse(options.search?.(queryOf(path)) ?? { entities: [], warning: null });
    }

    if (path.startsWith('/api/ha/entities/')) {
      const entityId = decodeURIComponent(path.slice('/api/ha/entities/'.length));
      return jsonResponse(options.detail?.(entityId) ?? haDetail());
    }

    if (path.startsWith('/api/icons/search')) {
      return jsonResponse(
        options.icons?.(queryOf(path)) ?? {
          version: '7.4.47',
          total: 1,
          matched: 1,
          icons: [{ name: 'mdi:door', path: 'M0 0h1v1H0z' }]
        }
      );
    }

    if (path === '/api/icons/custom') {
      return jsonResponse(options.custom ?? { total: 0, icons: [], skipped: [] });
    }

    throw new Error(`unexpected request ${method} ${path}`);
  };

  setFetchImpl(impl);
  return { calls, rows };
}

function callsTo(backend: Backend, path: string, method = 'GET'): FakeCall[] {
  return backend.calls.filter(
    (call) => call.path === path && (call.init?.method ?? 'GET') === method
  );
}

function bodyOf(call: FakeCall | undefined): unknown {
  if (call === undefined) {
    throw new Error('expected a request, found none');
  }
  return JSON.parse(String(call.init?.body));
}

/* ---- DOM helpers -------------------------------------------------------- */

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function appendTarget(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  return target;
}

function renderDashboard(): void {
  mounted.push(mount(Dashboard, { target: appendTarget(), props: {} }));
  flushSync();
}

function renderList(navigate: (path: string) => void = () => {}, rest = ''): void {
  mounted.push(mount(EntitiesPanel, { target: appendTarget(), props: { rest, navigate } }));
  flushSync();
}

function renderPicker(): void {
  mounted.push(mount(EntityPicker, { target: appendTarget(), props: { onclose: () => {} } }));
  flushSync();
}

function renderEditor(id: number): void {
  mounted.push(mount(EntityEditor, { target: appendTarget(), props: { id, onback: () => {} } }));
  flushSync();
}

function unmountAll(): void {
  for (const component of mounted.splice(0)) {
    unmount(component);
  }
  document.body.innerHTML = '';
}

function pageText(): string {
  return (document.body.textContent ?? '').replace(/\s+/g, ' ').trim();
}

function buttonByText(label: string): HTMLButtonElement {
  const button = [...document.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => (candidate.textContent ?? '').replace(/\s+/g, ' ').trim() === label
  );
  if (button === undefined) {
    const seen = [...document.querySelectorAll<HTMLButtonElement>('button')]
      .map((candidate) => JSON.stringify((candidate.textContent ?? '').replace(/\s+/g, ' ').trim()))
      .join(', ');
    throw new Error(`expected a button labelled "${label}"; saw ${seen}`);
  }
  return button;
}

function buttonByLabel(label: string): HTMLButtonElement {
  const button = document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  if (button === null) {
    throw new Error(`expected a button with the accessible name "${label}"`);
  }
  return button;
}

function inputByLabel(label: string): HTMLInputElement {
  const field = [...document.querySelectorAll('label')].find(
    (candidate) => (candidate.textContent ?? '').replace(/\s+/g, ' ').trim() === label
  );
  const id = field?.getAttribute('for') ?? null;
  const input = id === null ? null : document.getElementById(id);
  if (!(input instanceof HTMLInputElement)) {
    throw new Error(`expected an input labelled "${label}"`);
  }
  return input;
}

/** Type into a field the way a person would: set the value, then fire `input`. */
function typeInto(input: HTMLInputElement, value: string): void {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function checkboxByLabel(label: string): HTMLInputElement {
  const row = [...document.querySelectorAll('label.checkbox')].find(
    (candidate) => (candidate.querySelector('.label')?.textContent ?? '').trim() === label
  );
  const input = row?.querySelector('input[type="checkbox"]') ?? null;
  if (!(input instanceof HTMLInputElement)) {
    throw new Error(`expected a checkbox labelled "${label}"`);
  }
  return input;
}

/** The row names in rendered order (the reorder list's own `.name` spans). */
function renderedNames(): string[] {
  return [...document.querySelectorAll<HTMLElement>('ol.rows > li .name')].map((element) =>
    (element.textContent ?? '').trim()
  );
}

function tileElement(): HTMLElement {
  const tile = document.querySelector<HTMLElement>('li.tile');
  if (tile === null) {
    throw new Error('expected a rendered Tile');
  }
  return tile;
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

/**
 * Drain pending promise work under fake timers, then sync the DOM. A fake
 * clock stops `vi.waitFor`, so the fake-timer tests use this instead.
 */
async function settle(): Promise<void> {
  for (let attempt = 0; attempt < 10; attempt += 1) {
    await Promise.resolve();
  }
  flushSync();
}

async function settleUntil(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 20 && !predicate(); attempt += 1) {
    await Promise.resolve();
    flushSync();
  }
  flushSync();
}

/* ---- stacked-box geometry (not a layout engine) ------------------------- */

/**
 * happy-dom has no layout engine, so `getBoundingClientRect` returns zeros and
 * the drag maths would never be exercised. The stub below hands every row a
 * stacked 80px box (row i at `i*80`), which is the geometry the drag code
 * expects from a real panel. This proves the pointer -> index -> request rule;
 * it is NOT a substitute for the D26 finger test on the real panel (HSD-rj5j).
 */
let originalRect: (() => DOMRect) | null = null;

function stackRowsEightyPixels(): void {
  if (originalRect !== null) {
    return;
  }
  originalRect = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = function (this: Element): DOMRect {
    const siblings = this.parentElement === null ? [] : [...this.parentElement.children];
    const index = Math.max(0, siblings.indexOf(this));
    const top = index * 80;
    return {
      top,
      bottom: top + 80,
      height: 80,
      left: 0,
      right: 320,
      width: 320,
      x: 0,
      y: top,
      toJSON: () => ({})
    } as DOMRect;
  };
}

function restoreRects(): void {
  if (originalRect !== null) {
    Element.prototype.getBoundingClientRect = originalRect;
    originalRect = null;
  }
}

/* ---- the websocket fake (copied from ws.test.ts) ------------------------ */

class FakeSocket implements SocketLike {
  readyState = 0;
  closed = false;
  #listeners = new Map<string, Set<SocketListener>>();

  addEventListener(type: string, listener: SocketListener): void {
    const set = this.#listeners.get(type) ?? new Set<SocketListener>();
    set.add(listener);
    this.#listeners.set(type, set);
  }

  removeEventListener(type: string, listener: SocketListener): void {
    this.#listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.closed = true;
    this.readyState = 3;
  }

  emit(type: string, event: SocketEvent = {}): void {
    for (const listener of [...(this.#listeners.get(type) ?? [])]) {
      listener(event);
    }
  }

  open(): void {
    this.readyState = 1;
    this.emit('open');
  }

  send(frame: unknown): void {
    this.emit('message', { data: JSON.stringify(frame) });
  }
}

async function macrotask(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

beforeEach(() => {
  liveState.reset();
  liveState.setConfigRefreshHandler(null);
  settings.reset();
  setFetchImpl(null);
  unmountAll();
});

afterEach(() => {
  unmountAll();
  liveState.reset();
  liveState.setConfigRefreshHandler(null);
  settings.reset();
  setFetchImpl(null);
  toastStore.clear();
  vi.useRealTimers();
  restoreRects();
});

/* ---- AC1 ---------------------------------------------------------------- */

describe('1. AC1 — an entity that exists in HA appears on the dashboard within one websocket cycle', () => {
  it('refetches exactly once on config_changed and renders the new tile beside the old one', async () => {
    const backend = fakeBackend({
      rows: [
        entityRow({ id: 1, entity_id: 'lock.front_door', display_name: 'Front Door' }),
        entityRow({
          id: 2,
          entity_id: 'lock.back_door',
          display_name: 'Back Door',
          state: 'unlocked',
          displayed_state: 'Unlocked',
          tone: 'alert',
          sort_order: 1
        })
      ]
    });

    // Mount first, then replace the store's contents with one existing row.
    renderDashboard();
    liveState.reset();
    liveState.applySnapshot(
      snapshotOf([
        snapshotRow({
          id: 1,
          entity_id: 'lock.front_door',
          display_name: 'Front Door',
          state: 'locked',
          displayed_state: 'Locked',
          tone: 'ok'
        })
      ])
    );
    flushSync();
    expect(document.querySelector('[data-entity="lock.front_door"]')).not.toBeNull();
    expect(document.querySelector('[data-entity="lock.back_door"]')).toBeNull();

    // The wiring App.svelte installs: refetch the half the frame names.
    liveState.setConfigRefreshHandler(async (what) =>
      what === 'entities' ? { entities: await listEntities() } : {}
    );

    const socket = new FakeSocket();
    const ws = new WsClient(liveState, {
      createSocket: () => socket,
      view: null,
      net: null,
      schedule: () => () => {}
    });
    ws.start();
    socket.open();

    // One websocket cycle: the real recorded frame through the real client.
    socket.send(CONFIG_CHANGED_ENTITIES_FRAME);
    await macrotask();

    const rendered = [...document.querySelectorAll('li.tile')].map((tile) =>
      tile.getAttribute('data-entity')
    );
    expect(rendered).toEqual(['lock.front_door', 'lock.back_door']);
    expect(callsTo(backend, '/api/entities')).toHaveLength(1);

    ws.stop();
  });

  it('posts exactly one entity_id-only body when a live row is added from the picker', async () => {
    const backend = fakeBackend({
      rows: [],
      search: () => ({ entities: [haRow()], warning: null }),
      created: entityRow({ id: 1, entity_id: 'lock.front_door' })
    });

    renderPicker();
    await vi.waitFor(() => expect(pageText()).toContain('lock.front_door'));

    const box = document.querySelector<HTMLInputElement>('input[type="checkbox"]');
    expect(box).not.toBeNull();
    box?.click();
    flushSync();
    expect(pageText()).toContain('1 selected');

    buttonByText('Add 1 tile').click();
    await vi.waitFor(() => expect(pageText()).toContain('Added 1 tile'));

    const posts = callsTo(backend, '/api/entities', 'POST');
    expect(posts).toHaveLength(1);
    const body = bodyOf(posts[0]) as Record<string, unknown>;
    expect(body).toEqual({ entity_id: 'lock.front_door' });
    // No invented keys: the body names the entity and nothing else.
    expect(Object.keys(body)).toEqual(['entity_id']);
  });
});

/* ---- AC2 ---------------------------------------------------------------- */

describe('2. AC2 — a bogus entity_id warns but saves', () => {
  const BOGUS_SENTENCE =
    "Home Assistant does not know the entity 'lock.back_gate' — it was saved anyway. " +
    'Check the spelling, or make sure the integration that provides it is loaded in Home Assistant.';

  it('saves a typed id HA did not return and renders the warning verbatim with Done still available', async () => {
    const backend = fakeBackend({
      rows: [],
      search: () => ({ entities: [], warning: null }),
      created: entityRow({
        id: 1,
        entity_id: 'lock.back_gate',
        display_name: null,
        state: null,
        displayed_state: null,
        warning: BOGUS_SENTENCE
      })
    });

    renderPicker();
    await vi.waitFor(() => expect(pageText()).toContain('Search entities'));

    typeInto(inputByLabel('Search entities'), 'lock.back_gate');
    const anyway = buttonByText('Add “lock.back_gate” anyway');
    anyway.click();

    await vi.waitFor(() => expect(pageText()).toContain(BOGUS_SENTENCE));
    // The result screen says the tile was added, with the caveat as the heading.
    expect(pageText()).toContain('Add finished with warnings');
    expect(pageText()).toContain('Added');
    // The warning blocks nothing: Done is on screen and enabled.
    expect(buttonByText('Done').disabled).toBe(false);

    const posts = callsTo(backend, '/api/entities', 'POST');
    expect(posts).toHaveLength(1);
    expect(bodyOf(posts[0])).toEqual({ entity_id: 'lock.back_gate' });
  });

  it('renders a stored row warning verbatim in the editor and does not let it block Save', async () => {
    fakeBackend({
      rows: [entityRow({ warning: BOGUS_SENTENCE })],
      detail: () => haDetail()
    });

    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain(BOGUS_SENTENCE));

    // The warning is not a dirty state: an untouched draft is legitimately
    // disabled. Make a real edit and the warning must still not block it.
    expect(buttonByText('Save').disabled).toBe(true);
    typeInto(inputByLabel('Display name'), 'Porch');
    expect(pageText()).toContain(BOGUS_SENTENCE);
    expect(buttonByText('Save').disabled).toBe(false);
  });
});

/* ---- AC3 ---------------------------------------------------------------- */

describe('3. AC3 — reordering persists across reload', () => {
  function threeRows(): EntityResponse[] {
    return [
      entityRow({ id: 1, entity_id: 'lock.alpha', display_name: 'Alpha', sort_order: 0 }),
      entityRow({ id: 2, entity_id: 'lock.beta', display_name: 'Beta', sort_order: 1 }),
      entityRow({ id: 3, entity_id: 'lock.gamma', display_name: 'Gamma', sort_order: 2 })
    ];
  }

  it('arrow-moves one row, drag-moves another, and the reloaded list matches the server order', async () => {
    const backend = fakeBackend({ rows: threeRows(), detail: () => haDetail() });

    renderList();
    await vi.waitFor(() => expect(renderedNames()).toEqual(['Alpha', 'Beta', 'Gamma']));

    // Accessible names identify row and direction, and the edges are disabled.
    expect(buttonByLabel('Move Alpha up').disabled).toBe(true);
    expect(buttonByLabel('Move Alpha down').disabled).toBe(false);
    expect(buttonByLabel('Move Gamma down').disabled).toBe(true);

    buttonByLabel('Move Alpha down').click();
    await vi.waitFor(() =>
      expect(callsTo(backend, '/api/entities/reorder', 'POST')).toHaveLength(1)
    );
    // One request per tap, carrying the FULL new id order.
    expect(bodyOf(callsTo(backend, '/api/entities/reorder', 'POST')[0])).toEqual({
      ids: [2, 1, 3]
    });
    await vi.waitFor(() => expect(renderedNames()).toEqual(['Beta', 'Alpha', 'Gamma']));

    // Wait for the posted reorder to settle: while it is in flight the list is
    // disabled and a drag would (correctly) do nothing.
    await vi.waitFor(() => expect(buttonByLabel('Reorder Beta').disabled).toBe(false));

    // Now the long-press drag, for another row (see D26 for the mechanics).
    vi.useFakeTimers();
    stackRowsEightyPixels();
    const handle = buttonByLabel('Reorder Beta');
    handle.dispatchEvent(pointerEvent('pointerdown'));
    vi.advanceTimersByTime(400);
    flushSync();
    // Beta is at display position 0; drop it at the last row's midpoint.
    handle.dispatchEvent(pointerEvent('pointermove', { clientY: 200 }));
    handle.dispatchEvent(pointerEvent('pointerup'));
    await settleUntil(
      () => callsTo(backend, '/api/entities/reorder', 'POST').length === 2
    );

    expect(bodyOf(callsTo(backend, '/api/entities/reorder', 'POST')[1])).toEqual({
      ids: [1, 3, 2]
    });
    expect(renderedNames()).toEqual(['Alpha', 'Gamma', 'Beta']);

    // Persistence is server truth, not the panel's optimistic state.
    expect(backend.rows.map((row) => row.display_name)).toEqual(['Alpha', 'Gamma', 'Beta']);
    expect(backend.rows.map((row) => row.sort_order)).toEqual([0, 1, 2]);

    vi.useRealTimers();
    restoreRects();
    unmountAll();

    renderList();
    await vi.waitFor(() => expect(renderedNames()).toEqual(['Alpha', 'Gamma', 'Beta']));
    expect(renderedNames()).toEqual(backend.rows.map((row) => row.display_name));
  });

  it('restores the previous order and shows the rejected reorder message verbatim', async () => {
    const message = 'The tiles could not be reordered: the store is read-only right now.';
    fakeBackend({ rows: threeRows(), detail: () => haDetail(), reorderError: message });

    renderList();
    await vi.waitFor(() => expect(renderedNames()).toEqual(['Alpha', 'Beta', 'Gamma']));

    buttonByLabel('Move Alpha down').click();
    await vi.waitFor(() => expect(pageText()).toContain(message));

    const alert = document.querySelector<HTMLElement>('[role="alert"]');
    expect(alert?.textContent?.trim()).toBe(message);
    // The optimistic order was rolled back, because a list that kept an order
    // the backend refused would lie about what is stored.
    expect(renderedNames()).toEqual(['Alpha', 'Beta', 'Gamma']);
  });
});

/* ---- D26 drag mechanics -------------------------------------------------- */

describe('4. D26 — the long-press drag posts once per gesture', () => {
  function stacked(): Backend {
    const backend = fakeBackend({
      rows: [
        entityRow({ id: 1, entity_id: 'lock.alpha', display_name: 'Alpha', sort_order: 0 }),
        entityRow({ id: 2, entity_id: 'lock.beta', display_name: 'Beta', sort_order: 1 }),
        entityRow({ id: 3, entity_id: 'lock.gamma', display_name: 'Gamma', sort_order: 2 })
      ],
      detail: () => haDetail()
    });
    return backend;
  }

  it('does not activate before 400 ms, then posts exactly one reorder for the whole gesture', async () => {
    const backend = stacked();
    renderList();
    await vi.waitFor(() => expect(renderedNames()).toEqual(['Alpha', 'Beta', 'Gamma']));

    // No layout engine in happy-dom: the stub says every row is a stacked
    // 80px box so the midpoint maths is real. The finger test is HSD-rj5j's.
    vi.useFakeTimers();
    stackRowsEightyPixels();

    const handle = buttonByLabel('Reorder Alpha');
    handle.dispatchEvent(pointerEvent('pointerdown'));

    vi.advanceTimersByTime(399);
    flushSync();
    expect(document.querySelector('li.row[data-dragging="true"]')).toBeNull();
    expect(callsTo(backend, '/api/entities/reorder', 'POST')).toHaveLength(0);

    vi.advanceTimersByTime(1);
    flushSync();
    const dragging = document.querySelector<HTMLElement>('li.row[data-dragging="true"]');
    expect(dragging).not.toBeNull();
    expect(dragging?.textContent).toContain('Alpha');

    // Move through three positions before the drop: still ONE request.
    handle.dispatchEvent(pointerEvent('pointermove', { clientY: 40 }));
    handle.dispatchEvent(pointerEvent('pointermove', { clientY: 120 }));
    handle.dispatchEvent(pointerEvent('pointermove', { clientY: 200 }));
    expect(callsTo(backend, '/api/entities/reorder', 'POST')).toHaveLength(0);

    handle.dispatchEvent(pointerEvent('pointerup'));
    await settleUntil(() => callsTo(backend, '/api/entities/reorder', 'POST').length === 1);

    const reorders = callsTo(backend, '/api/entities/reorder', 'POST');
    expect(reorders).toHaveLength(1);
    expect(bodyOf(reorders[0])).toEqual({ ids: [2, 3, 1] });
    expect(renderedNames()).toEqual(['Beta', 'Gamma', 'Alpha']);
  });

  it('treats movement over 8 px before activation as a scroll, not a reorder', async () => {
    const backend = stacked();
    renderList();
    await vi.waitFor(() => expect(renderedNames()).toEqual(['Alpha', 'Beta', 'Gamma']));

    vi.useFakeTimers();
    stackRowsEightyPixels();

    const handle = buttonByLabel('Reorder Alpha');
    handle.dispatchEvent(pointerEvent('pointerdown', { clientY: 20 }));
    // 20 px down, before the long-press elapses: a scroll, and it cancels.
    handle.dispatchEvent(pointerEvent('pointermove', { clientY: 40 }));
    vi.advanceTimersByTime(500);
    flushSync();

    expect(document.querySelector('li.row[data-dragging="true"]')).toBeNull();
    handle.dispatchEvent(pointerEvent('pointerup'));
    await settle();

    expect(callsTo(backend, '/api/entities/reorder', 'POST')).toHaveLength(0);
    expect(renderedNames()).toEqual(['Alpha', 'Beta', 'Gamma']);
  });
});

/* ---- AC4 ---------------------------------------------------------------- */

describe('5. AC4 — the icon picker searches the MDI set and writes mdi:<name>', () => {
  it('renders the five source meanings, searches, and patches exactly the chosen mdi: icon', async () => {
    const backend = fakeBackend({
      rows: [
        entityRow({
          id: 1,
          entity_id: 'lock.front_door',
          display_name: 'Front Door',
          icon: 'mdi:lock',
          icon_source: 'ha-attribute',
          icon_override: null
        })
      ],
      detail: () => haDetail({ icon: 'mdi:lock' }),
      icons: () => ({
        version: '7.4.47',
        total: 100,
        matched: 2,
        icons: [{ name: 'mdi:door', path: 'M0 0h1v1H0z' }]
      })
    });

    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));

    // All five `icon_source` values, with the words `iconSourceLabel()` gives.
    const wires = [...document.querySelectorAll<HTMLElement>('.legend .wire')].map((element) =>
      (element.textContent ?? '').trim()
    );
    expect(wires).toEqual(Object.keys(ICON_SOURCE_LABELS));
    for (const [value, label] of Object.entries(ICON_SOURCE_LABELS)) {
      expect(iconSourceLabel(value)).toBe(label);
      expect(pageText()).toContain(label);
    }
    expect(pageText()).toContain('now: Home Assistant attribute');

    // Open the sheet from the editor's own trigger.
    document.querySelector<HTMLButtonElement>('button[aria-haspopup="dialog"]')?.click();
    flushSync();

    await vi.waitFor(() =>
      expect(callsTo(backend, '/api/icons/search?q=&limit=120')).toHaveLength(1)
    );
    await vi.waitFor(() => expect(pageText()).toContain('Showing 1 of 2 matches'));
    expect(document.querySelector('button[aria-label="door"]')).not.toBeNull();

    typeInto(inputByLabel('Search icons'), 'door');
    await vi.waitFor(() =>
      expect(callsTo(backend, '/api/icons/search?q=door&limit=120')).toHaveLength(1)
    );
    await vi.waitFor(() => expect(pageText()).toContain('Icon set 7.4.47'));

    buttonByLabel('door').click();
    flushSync();
    // The trigger now shows the override that was chosen.
    expect(
      (document.querySelector('.icon-trigger .icon-text')?.textContent ?? '').trim()
    ).toBe('mdi:door');

    buttonByText('Save').click();
    await vi.waitFor(() => expect(callsTo(backend, '/api/entities/1', 'PATCH')).toHaveLength(1));

    const patches = callsTo(backend, '/api/entities/1', 'PATCH');
    expect(patches).toHaveLength(1);
    const body = bodyOf(patches[0]) as Record<string, unknown>;
    expect(body).toEqual({ icon: 'mdi:door' });
    // The resolved icon must never be written back as the user's choice.
    expect(body.icon).not.toBe('mdi:lock');
  });
});

describe('5b. HSD-po1g — the entity icon picker still offers the user’s own files', () => {
  it('renders “Your icons” and fetches the custom listing', async () => {
    // The button editor turns this off; an entity icon is never a discovery
    // payload, so its picker keeps the whole list. Asserting both directions is
    // what proves the prop is a choice rather than a removal.
    const backend = fakeBackend({
      rows: [entityRow({ id: 1, icon: 'mdi:lock' })],
      detail: () => haDetail({ icon: 'mdi:lock' })
    });
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));

    document.querySelector<HTMLButtonElement>('button[aria-haspopup="dialog"]')?.click();
    flushSync();

    await vi.waitFor(() => expect(callsTo(backend, '/api/icons/custom')).toHaveLength(1));
    expect(pageText()).toContain('Your icons');
  });
});

/* ---- AC5 ---------------------------------------------------------------- */

describe('6. AC5 — a custom icon file is served from /custom-icons and is rendered', () => {
  const CUSTOM_LISTING = {
    total: 1,
    icons: [{ name: 'cat.svg', reference: 'custom:cat.svg', bytes: 812 }],
    skipped: [
      {
        name: 'bad.svg',
        reason:
          'the SVG contains a <script> element, which is never allowed in a custom icon'
      }
    ]
  };

  function openCustomSheet(backendOptions: BackendOptions = {}): void {
    fakeBackend({
      rows: [entityRow({ icon: 'mdi:lock' })],
      detail: () => haDetail({ icon: 'mdi:lock' }),
      ...backendOptions
    });
  }

  it('renders the /custom-icons thumbnail, the skipped reason, and stores the reference verbatim', async () => {
    const backend = fakeBackend({
      rows: [entityRow({ icon: 'mdi:lock' })],
      detail: () => haDetail({ icon: 'mdi:lock' }),
      custom: CUSTOM_LISTING
    });

    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));
    document.querySelector<HTMLButtonElement>('button[aria-haspopup="dialog"]')?.click();
    flushSync();

    await vi.waitFor(() => expect(buttonByLabel('cat.svg')).toBeTruthy());
    const thumbnail = document.querySelector<HTMLImageElement>('button[aria-label="cat.svg"] img');
    expect(thumbnail).not.toBeNull();
    expect(thumbnail?.getAttribute('src')).toBe('/custom-icons/cat.svg');

    // The refusal sentence is verbatim, and the file is never offered.
    const reason = CUSTOM_LISTING.skipped[0].reason;
    expect(pageText()).toContain(reason);
    expect(document.querySelector('button[aria-label="bad.svg"]')).toBeNull();
    expect(document.querySelector('img[src*="bad.svg"]')).toBeNull();

    buttonByLabel('cat.svg').click();
    flushSync();

    buttonByText('Save').click();
    await vi.waitFor(() => expect(callsTo(backend, '/api/entities/1', 'PATCH')).toHaveLength(1));

    const body = bodyOf(callsTo(backend, '/api/entities/1', 'PATCH')[0]) as Record<string, unknown>;
    // The listing's `reference` is what is stored — never rebuilt in the client.
    expect(body).toEqual({ icon: 'custom:cat.svg' });
    expect(body.icon).toBe(CUSTOM_LISTING.icons[0].reference);
  });

  it('swaps a failed custom thumbnail for the question-mark fallback', async () => {
    openCustomSheet({ custom: CUSTOM_LISTING });

    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));
    document.querySelector<HTMLButtonElement>('button[aria-haspopup="dialog"]')?.click();
    flushSync();
    await vi.waitFor(() => expect(buttonByLabel('cat.svg')).toBeTruthy());

    const thumbnail = document.querySelector<HTMLImageElement>('button[aria-label="cat.svg"] img');
    thumbnail?.dispatchEvent(new Event('error'));
    flushSync();

    const cell = buttonByLabel('cat.svg');
    expect(cell.querySelector('[data-custom-icon-failed]')).not.toBeNull();
    expect(cell.querySelector('img')).toBeNull();
  });

  it('treats an empty custom listing as the normal empty folder, not an error', async () => {
    openCustomSheet({ custom: { total: 0, icons: [], skipped: [] } });

    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));
    document.querySelector<HTMLButtonElement>('button[aria-haspopup="dialog"]')?.click();
    flushSync();

    await vi.waitFor(() => expect(pageText()).toContain('No files in data/icons yet'));
    expect(document.querySelector('dialog [role="alert"]')).toBeNull();
    expect(pageText()).not.toContain('could not be used');
  });
});

/* ---- 7. editor semantics ------------------------------------------------- */

describe('7. Editor semantics — the real Tile, the honest draft and the minimal patch', () => {
  function editorBackend(overrides: BackendOptions = {}): Backend {
    return fakeBackend({
      rows: [entityRow()],
      detail: () => haDetail(),
      ...overrides
    });
  }

  it('drives the real Tile from the row state and shows the draft name before saving', async () => {
    editorBackend();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));

    expect(tileElement().getAttribute('data-kind')).toBe('ok');
    expect((tileElement().querySelector('.word')?.textContent ?? '').trim()).toBe('Locked');

    typeInto(inputByLabel('Display name'), 'Porch');
    expect(
      (document.querySelector('li.tile .name')?.textContent ?? '').trim()
    ).toBe('Porch');
  });

  it('renders the unavailable kind and word from the row state', async () => {
    editorBackend({
      rows: [entityRow({ state: 'unavailable', displayed_state: 'Unavailable', tone: 'off' })],
      detail: () => haDetail({ state: 'unavailable' })
    });
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));

    expect(tileElement().getAttribute('data-kind')).toBe('unavailable');
    expect((tileElement().querySelector('.word')?.textContent ?? '').trim()).toBe('Unavailable');
  });

  it('shows the tile rule’s "No state" for a draft with no state — never Off or Missing', async () => {
    editorBackend({
      rows: [entityRow({ state: null, displayed_state: null, tone: null })],
      detail: () => haDetail({ state: null })
    });
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));

    expect(tileElement().getAttribute('data-kind')).toBe('unknown');
    const word = (tileElement().querySelector('.word')?.textContent ?? '').trim();
    expect(word).toBe('No state');
    expect(word).not.toBe('Off');
    expect(word).not.toBe('Missing');
  });

  it('marks the preview stale only when the describe call could not answer', async () => {
    editorBackend({
      detail: () =>
        haDetail({
          found: null,
          warning: 'Home Assistant could not be reached while describing this entity.'
        })
    });
    renderEditor(1);
    await vi.waitFor(() => expect(tileElement().getAttribute('data-stale')).toBe('true'));
    expect(tileElement().textContent).toContain('Stale');

    // `found: false` is an answer ("the entity is not in HA"), not a dead
    // connection, so the same tile must not claim it is stale.
    unmountAll();
    editorBackend({ detail: () => haDetail({ found: false, warning: null }) });
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));
    expect(tileElement().getAttribute('data-stale')).toBe('false');
    expect(tileElement().textContent).not.toContain('Stale');
  });

  it('builds attribute checkboxes from the union of live attributes and the stored list', async () => {
    const backend = editorBackend({
      rows: [entityRow({ show_attributes: ['last_changed'] })],
      detail: () => haDetail({ attributes: { battery: 90, room: 'Hall' } })
    });
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('battery'));

    const labels = [...document.querySelectorAll('label.checkbox .label')].map((element) =>
      (element.textContent ?? '').trim()
    );
    expect(labels).toEqual(['battery', 'last_changed', 'room']);

    checkboxByLabel('battery').click();
    flushSync();
    buttonByText('Save').click();
    await vi.waitFor(() => expect(callsTo(backend, '/api/entities/1', 'PATCH')).toHaveLength(1));

    // The stored "last_changed" is kept and "battery" is added: toggling one
    // entry never drops another, and the union's sorted order is preserved.
    expect(bodyOf(callsTo(backend, '/api/entities/1', 'PATCH')[0])).toEqual({
      show_attributes: ['battery', 'last_changed']
    });
  });

  it('sends {hide_when_unavailable:true} when the toggle is switched', async () => {
    const backend = editorBackend();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));

    const toggle = [...document.querySelectorAll<HTMLButtonElement>('button[role="switch"]')].find(
      (candidate) => candidate.textContent?.includes('Hide when unavailable') === true
    );
    expect(toggle).not.toBeNull();
    toggle?.click();
    flushSync();

    buttonByText('Save').click();
    await vi.waitFor(() => expect(callsTo(backend, '/api/entities/1', 'PATCH')).toHaveLength(1));
    expect(bodyOf(callsTo(backend, '/api/entities/1', 'PATCH')[0])).toEqual({
      hide_when_unavailable: true
    });
  });

  it('sends the preset accent, then clears it to null', async () => {
    const backend = editorBackend();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));

    buttonByLabel('Accent colour (Red, #ff5c5c)').click();
    flushSync();
    buttonByText('Save').click();
    await vi.waitFor(() => expect(callsTo(backend, '/api/entities/1', 'PATCH')).toHaveLength(1));
    expect(bodyOf(callsTo(backend, '/api/entities/1', 'PATCH')[0])).toEqual({
      accent_color: '#ff5c5c'
    });

    // The save is only done once it settles: a busy Save reads "Working Save".
    await settle();
    expect(buttonByText('Save').textContent?.trim()).toBe('Save');

    // A cleared accent is an explicit null, never an omitted key.
    buttonByText('No accent (use the theme)').click();
    flushSync();
    buttonByText('Save').click();
    await vi.waitFor(() => expect(callsTo(backend, '/api/entities/1', 'PATCH')).toHaveLength(2));
    expect(bodyOf(callsTo(backend, '/api/entities/1', 'PATCH')[1])).toEqual({
      accent_color: null
    });
  });

  it('sends a typed custom hex as accent_color', async () => {
    const backend = editorBackend();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));

    buttonByText('Custom colour…').click();
    flushSync();
    typeInto(inputByLabel('Custom colour'), '#ff5c5c');

    buttonByText('Save').click();
    await vi.waitFor(() => expect(callsTo(backend, '/api/entities/1', 'PATCH')).toHaveLength(1));
    expect(bodyOf(callsTo(backend, '/api/entities/1', 'PATCH')[0])).toEqual({
      accent_color: '#ff5c5c'
    });
  });

  it('blocks Save with the error line and sends nothing for an invalid accent colour', async () => {
    const backend = editorBackend({ rows: [entityRow({ accent_color: '#gggggg' })] });
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));

    expect(pageText()).toContain('Use #rgb or #rrggbb, for example #4f9cf9');

    // Dirty, so only the invalid accent can be what keeps Save disabled.
    typeInto(inputByLabel('Display name'), 'Porch');
    expect(buttonByText('Save').disabled).toBe(true);
    buttonByText('Save').click();
    await settle();

    expect(callsTo(backend, '/api/entities/1', 'PATCH')).toHaveLength(0);
  });

  it('blocks Save with the error line and sends nothing for a typed invalid hex, then saves the corrected colour', async () => {
    const backend = editorBackend();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));

    // Dirty, so only the typed colour can be what keeps Save disabled.
    typeInto(inputByLabel('Display name'), 'Porch');
    buttonByText('Custom colour…').click();
    flushSync();
    typeInto(inputByLabel('Custom colour'), '#zzzzzz');

    expect(pageText()).toContain('Use #rgb or #rrggbb, for example #4f9cf9');
    expect(buttonByText('Save').disabled).toBe(true);
    buttonByText('Save').click();
    await settle();
    expect(callsTo(backend, '/api/entities/1', 'PATCH')).toHaveLength(0);

    // Correcting the text clears the error and lets the same edit through.
    typeInto(inputByLabel('Custom colour'), '#ff5c5c');
    expect(buttonByText('Save').disabled).toBe(false);
    buttonByText('Save').click();
    await vi.waitFor(() => expect(callsTo(backend, '/api/entities/1', 'PATCH')).toHaveLength(1));
    expect(bodyOf(callsTo(backend, '/api/entities/1', 'PATCH')[0])).toEqual({
      display_name: 'Porch',
      accent_color: '#ff5c5c'
    });
  });

  it('leaves Save disabled and sends no empty {} patch for an unchanged draft', async () => {
    const backend = editorBackend();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));

    expect(buttonByText('Save').disabled).toBe(true);
    buttonByText('Save').click();
    await settle();

    expect(callsTo(backend, '/api/entities/1', 'PATCH')).toHaveLength(0);
  });

  it('keeps the draft and renders a rejected save’s message verbatim in role=alert', async () => {
    const message = 'display_name: this name is longer than the dashboard can show.';
    const backend = editorBackend({ patchError: message });
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));

    typeInto(inputByLabel('Display name'), 'A name the server refuses');
    buttonByText('Save').click();

    await vi.waitFor(() => expect(pageText()).toContain(message));
    const alert = document.querySelector<HTMLElement>('[role="alert"]');
    expect(alert?.textContent?.trim()).toBe(message);
    // The draft is kept: a rejected save must not throw away what was typed.
    expect(inputByLabel('Display name').value).toBe('A name the server refuses');
    expect(buttonByText('Save').disabled).toBe(false);
  });
});

/* ---- 8. picker states ---------------------------------------------------- */

describe('8. Picker states — each condition has its own honest words', () => {
  it('renders an unreachable Home Assistant as a verbatim warning with Retry, never as "no matches"', async () => {
    const sentence = 'Home Assistant is unreachable. No live states are available.';
    fakeBackend({ search: () => ({ entities: [], warning: sentence }) });

    renderPicker();
    await vi.waitFor(() => expect(pageText()).toContain(sentence));
    expect(buttonByText('Retry search')).toBeTruthy();
    expect(pageText()).not.toContain('Nothing matches');
    expect(pageText()).not.toContain('No entities available');
  });

  it('distinguishes an empty opening list from a query that matches nothing', async () => {
    fakeBackend({ search: () => ({ entities: [], warning: null }) });

    renderPicker();
    await vi.waitFor(() => expect(pageText()).toContain('No entities available'));
    expect(pageText()).not.toContain('Nothing matches');

    typeInto(inputByLabel('Search entities'), 'front door');
    await vi.waitFor(() => expect(pageText()).toContain('Nothing matches'));
    expect(pageText()).not.toContain('No entities available');
  });

  it('renders a rejected search’s message verbatim with Retry, for 500 and for a dead connection', async () => {
    const message = 'Search failed. Live entity results could not be loaded.';
    fakeBackend({ searchFailure: { status: 500, message } });

    renderPicker();
    await vi.waitFor(() => expect(pageText()).toContain(message));
    expect(buttonByText('Retry search')).toBeTruthy();
    expect(pageText()).not.toContain('Nothing matches');

    unmountAll();
    fakeBackend({ searchFailure: 'network' });
    renderPicker();
    await vi.waitFor(() =>
      expect(pageText()).toContain('the server could not be reached: connection refused')
    );
    expect(buttonByText('Retry search')).toBeTruthy();
    expect(pageText()).not.toContain('Nothing matches');
  });

  it('shows "Already added" with no checkbox, and the row cannot be selected', async () => {
    fakeBackend({
      rows: [entityRow()],
      search: () => ({ entities: [haRow()], warning: null })
    });

    renderPicker();
    await vi.waitFor(() => expect(pageText()).toContain('Already added'));
    expect(document.querySelector('input[type="checkbox"]')).toBeNull();

    document.querySelector<HTMLElement>('li.pick-row')?.click();
    flushSync();
    expect(pageText()).toContain('0 selected');
    expect(buttonByText('Add 0 tiles').disabled).toBe(true);
  });

  it('renders the search field inside the sticky header region, per the component’s own CSS', () => {
    fakeBackend();
    renderPicker();

    const input = inputByLabel('Search entities');
    // happy-dom has no layout: the sticky contract is asserted on the
    // component's CSS text. Real layout is HSD-rj5j's job.
    expect(input.closest('.search')).not.toBeNull();

    const componentPath = join(dirname(fileURLToPath(import.meta.url)), 'EntityPicker.svelte');
    const source = readFileSync(componentPath, 'utf8');
    expect(source).toMatch(/\.search\s*\{[^}]*position:\s*sticky/s);
  });
});

/* ---- 9. navigation ------------------------------------------------------- */

describe('9. Navigation — routes call the injected navigate, never a page reload', () => {
  it('sends the list to the add path and a row to its editor path', async () => {
    fakeBackend({ rows: [entityRow({ id: 7 })] });
    const navigated: string[] = [];
    renderList((path) => navigated.push(path));

    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));
    buttonByText('Add entities').click();
    expect(navigated).toEqual(['/settings/entities/add']);

    buttonByLabel('Edit Front Door').click();
    expect(navigated).toEqual(['/settings/entities/add', '/settings/entities/7']);
  });

  it('mounts the picker, the editor and the "No such entity" notice for rest = add / 7 / nonsense', async () => {
    fakeBackend({ search: () => ({ entities: [haRow()], warning: null }) });
    renderList(() => {}, 'add');
    await vi.waitFor(() => expect(document.querySelector('input[type="checkbox"]')).not.toBeNull());
    expect(inputByLabel('Search entities')).not.toBeNull();
    expect(pageText()).not.toContain('No such entity');

    unmountAll();
    fakeBackend({ rows: [entityRow({ id: 7 })], detail: () => haDetail() });
    renderList(() => {}, '7');
    await vi.waitFor(() => expect(pageText()).toContain('Live preview'));
    expect((document.querySelector('li.tile .name')?.textContent ?? '').trim()).toBe('Front Door');

    unmountAll();
    fakeBackend();
    renderList(() => {}, 'nonsense');
    await vi.waitFor(() => expect(pageText()).toContain('No such entity'));
    expect(pageText()).toContain('There is no entity at “nonsense”.');
  });

  it('navigates back to the list path when the picker’s Done is pressed', async () => {
    fakeBackend({
      rows: [],
      search: () => ({ entities: [haRow()], warning: null }),
      created: entityRow({ id: 1, entity_id: 'lock.front_door' })
    });
    const navigated: string[] = [];
    renderList((path) => navigated.push(path), 'add');

    await vi.waitFor(() => expect(pageText()).toContain('lock.front_door'));
    document.querySelector<HTMLInputElement>('input[type="checkbox"]')?.click();
    flushSync();
    buttonByText('Add 1 tile').click();
    await vi.waitFor(() => expect(pageText()).toContain('Added 1 tile'));

    buttonByText('Done').click();
    expect(navigated).toEqual(['/settings/entities']);
  });
});

/* ---- 10. HSD-p3pi — delete tiles --------------------------------------- */

describe('10. HSD-p3pi — a tile is deleted from the list behind a named confirmation', () => {
  const twoRows = (): EntityResponse[] => [
    entityRow({ id: 1, entity_id: 'lock.front_door', display_name: 'Front Door' }),
    entityRow({
      id: 2,
      entity_id: 'lock.back_door',
      display_name: 'Back Door',
      sort_order: 1
    })
  ];

  it('tapping the row delete asks first', async () => {
    const backend = fakeBackend({ rows: twoRows() });
    renderList();
    await vi.waitFor(() => expect(renderedNames()).toEqual(['Front Door', 'Back Door']));

    buttonByLabel('Delete Front Door').click();
    flushSync();

    expect(pageText()).toContain('Delete “Front Door”?');
    expect(pageText()).toContain('lock.front_door');
    expect(callsTo(backend, '/api/entities/1', 'DELETE')).toHaveLength(0);
  });

  it('cancel deletes nothing and leaves both rows on screen', async () => {
    const backend = fakeBackend({ rows: twoRows() });
    renderList();
    await vi.waitFor(() => expect(renderedNames()).toEqual(['Front Door', 'Back Door']));

    buttonByLabel('Delete Front Door').click();
    flushSync();
    buttonByText('Cancel').click();
    flushSync();

    await vi.waitFor(() => expect(document.querySelector('dialog.sheet')).toBeNull());
    expect(callsTo(backend, '/api/entities/1', 'DELETE')).toHaveLength(0);
    expect(renderedNames()).toEqual(['Front Door', 'Back Door']);
  });

  it('confirms exactly one delete, shows success, and refetches server truth', async () => {
    const backend = fakeBackend({ rows: twoRows() });
    renderList();
    await vi.waitFor(() => expect(renderedNames()).toEqual(['Front Door', 'Back Door']));

    buttonByLabel('Delete Front Door').click();
    flushSync();
    buttonByText('Delete tile').click();

    await vi.waitFor(() => expect(callsTo(backend, '/api/entities/1', 'DELETE')).toHaveLength(1));
    await vi.waitFor(() => expect(renderedNames()).toEqual(['Back Door']));
    expect(callsTo(backend, '/api/entities/1', 'DELETE')).toHaveLength(1);
    expect(toastStore.items.some((toast) => toast.kind === 'success' && toast.message === 'Tile deleted.')).toBe(true);
    expect(backend.rows.map((row) => row.display_name)).toEqual(['Back Door']);
  });

  it('leaves the sensible empty list after deleting the last tile', async () => {
    const backend = fakeBackend({ rows: [entityRow()] });
    renderList();
    await vi.waitFor(() => expect(renderedNames()).toEqual(['Front Door']));

    buttonByLabel('Delete Front Door').click();
    flushSync();
    buttonByText('Delete tile').click();

    await vi.waitFor(() => expect(pageText()).toContain('No entity tiles yet'));
    expect(backend.rows).toEqual([]);
    expect(document.querySelector('[role="alert"]')).toBeNull();
  });

  it('keeps the list and says why when the server refuses a delete', async () => {
    const message = 'The tile is still in use by a protected panel.';
    const backend = fakeBackend({ rows: twoRows(), deleteError: message });
    renderList();
    await vi.waitFor(() => expect(renderedNames()).toEqual(['Front Door', 'Back Door']));

    buttonByLabel('Delete Front Door').click();
    flushSync();
    buttonByText('Delete tile').click();

    await vi.waitFor(() => expect(document.querySelector('[role="alert"]')?.textContent).toBe(message));
    expect(renderedNames()).toEqual(['Front Door', 'Back Door']);
    expect(backend.rows).toHaveLength(2);
  });
});
