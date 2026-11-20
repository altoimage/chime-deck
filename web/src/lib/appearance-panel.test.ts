// @vitest-environment happy-dom
/**
 * DOM tests for the Appearance panel (HSD-16le).
 *
 * Fully offline: every request goes through `setFetchImpl`, and the fake serves
 * `GET /api/settings` from a mutable map that a PATCH merges into, so the echo a
 * flow depends on is the fake's own state rather than a second stub.
 *
 * They prove semantics — the words rendered, the key in the PATCH, the store's
 * adopted value — never layout geometry; the D26 sizing lives in the styles and
 * the contrast gate checks the tokens.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { readAccent } from './accent.js';
import { setFetchImpl, type FetchLike, type FetchResponse } from './api.js';
import { TEXT_CONTRAST_MIN, formatContrastRatio } from './contrast.js';
import AppearancePanel from './settings/AppearancePanel.svelte';
import { settings } from './settings.svelte.js';
import { toastStore } from './ui/toast.svelte.js';

/* ---- fetch fake ---------------------------------------------------------- */

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
  const impl: FetchLike = async (path, init) => {
    calls.push({ path, init });
    return handler(path, init);
  };
  setFetchImpl(impl);
  return calls;
}

interface BackendOptions {
  settings?: Record<string, unknown>;
  /** Answer every PATCH with this failure envelope instead of storing it. */
  patchError?: { status: number; body: unknown };
}

interface Backend {
  calls: FakeCall[];
  settings: Record<string, unknown>;
}

/** The in-memory settings backend: GET echoes, PATCH merges and echoes. */
function fakeBackend(options: BackendOptions = {}): Backend {
  const stored: Record<string, unknown> = { ...(options.settings ?? {}) };
  const calls = fakeFetch((path, init) => {
    if (path !== '/api/settings') {
      throw new Error(`unexpected request ${path}`);
    }
    if (init?.method === 'PATCH') {
      if (options.patchError) {
        return jsonResponse(options.patchError.body, options.patchError.status);
      }
      Object.assign(stored, JSON.parse(String(init.body)) as Record<string, unknown>);
      return jsonResponse({ settings: { ...stored }, secrets: {} });
    }
    return jsonResponse({ settings: { ...stored }, secrets: {} });
  });
  return { calls, settings: stored };
}

/* ---- DOM helpers --------------------------------------------------------- */

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function renderPanel(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(AppearancePanel, { target }));
  flushSync();
  return target;
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
    (candidate) => candidate.textContent?.trim() === label
  );
  if (button === undefined) {
    throw new Error(`expected a button labelled "${label}"`);
  }
  return button;
}

function saveButton(): HTMLButtonElement {
  return buttonByText('Save appearance');
}

function accentPreset(value: string): HTMLButtonElement {
  const button = [...document.querySelectorAll<HTMLButtonElement>('button[aria-label]')].find(
    (candidate) => candidate.getAttribute('aria-label')?.includes(value)
  );
  if (button === undefined) {
    throw new Error(`expected an accent swatch for "${value}"`);
  }
  return button;
}

function switches(): HTMLButtonElement[] {
  return [...document.querySelectorAll<HTMLButtonElement>('[role="switch"]')];
}

function switchByLabel(label: string): HTMLButtonElement {
  const toggle = switches().find(
    (candidate) => candidate.querySelector('.label')?.textContent?.trim() === label
  );
  if (toggle === undefined) {
    throw new Error(`expected a switch labelled "${label}"`);
  }
  return toggle;
}

function radioByValue(value: string): HTMLInputElement {
  const radio = [...document.querySelectorAll<HTMLInputElement>('input[type="radio"]')].find(
    (candidate) => candidate.value === value
  );
  if (radio === undefined) {
    throw new Error(`expected a radio with value "${value}"`);
  }
  return radio;
}

function selectTrigger(label: string): HTMLButtonElement {
  const button = [...document.querySelectorAll<HTMLButtonElement>('button.trigger')].find(
    (candidate) => {
      const ids = candidate.getAttribute('aria-labelledby')?.split(/\s+/) ?? [];
      return document.getElementById(ids[0] ?? '')?.textContent?.trim() === label;
    }
  );
  if (button === undefined) {
    throw new Error(`expected a Select trigger labelled "${label}"`);
  }
  return button;
}

function chooseColumn(value: string): void {
  selectTrigger('Dashboard columns').click();
  flushSync();
  radioByValue(value).click();
  flushSync();
}

function inputByLabel(label: string): HTMLInputElement {
  const row = [...document.querySelectorAll<HTMLLabelElement>('label')].find(
    (candidate) => candidate.textContent?.trim() === label
  );
  const id = row?.getAttribute('for') ?? '';
  const input = document.getElementById(id);
  if (!(input instanceof HTMLInputElement)) {
    throw new Error(`expected an input labelled "${label}"`);
  }
  return input;
}

function type(input: HTMLInputElement, value: string): void {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function patches(calls: FakeCall[]): FakeCall[] {
  return calls.filter((call) => call.path === '/api/settings' && call.init?.method === 'PATCH');
}

function patchBody(call: FakeCall): Record<string, unknown> {
  return JSON.parse(String(call.init?.body)) as Record<string, unknown>;
}

/** The hex spelling and happy-dom's `rgb(…)` spelling of one colour. */
function colourSpellings(hex: string): string[] {
  const digits = hex.toLowerCase().slice(1);
  const channels = [0, 2, 4].map((start) => Number.parseInt(digits.slice(start, start + 2), 16));
  return [hex.toLowerCase(), `rgb(${channels.join(', ')})`];
}

function expectColour(actual: string, hex: string): void {
  expect(colourSpellings(hex)).toContain(actual.toLowerCase());
}

/**
 * A pure grey whose best ink ratio falls just short of 4.5:1.
 *
 * Computed through `readAccent` rather than hardcoded: the band that fails is a
 * property of the contrast formula, and a guessed hex would drift with it.
 */
function failingGrey(): { hex: string; ratio: number } {
  let best: { hex: string; ratio: number } | null = null;
  for (let channel = 0; channel <= 255; channel += 1) {
    const hex = `#${channel.toString(16).padStart(2, '0').repeat(3)}`;
    const { ratio } = readAccent(hex);
    if (ratio < TEXT_CONTRAST_MIN && (best === null || ratio > best.ratio)) {
      best = { hex, ratio };
    }
  }
  if (best === null) {
    throw new Error('expected a grey whose best ink falls short of 4.5:1');
  }
  return best;
}

async function renderLoaded(options: BackendOptions = {}): Promise<Backend> {
  const backend = fakeBackend(options);
  await settings.load();
  renderPanel();
  return backend;
}

beforeEach(() => {
  settings.reset();
  setFetchImpl(null);
  unmountAll();
});

afterEach(() => {
  unmountAll();
  settings.reset();
  setFetchImpl(null);
  toastStore.clear();
  vi.restoreAllMocks();
});

/* ---- 1. loading ---------------------------------------------------------- */

describe('1. loading', () => {
  it('renders only the spinner while the settings request is in flight', async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    fakeFetch(async () => {
      await gate;
      return jsonResponse({ settings: {}, secrets: {} });
    });

    const loading = settings.load();
    renderPanel();

    const status = document.querySelector('[role="status"]');
    expect(status?.textContent).toContain('Loading appearance settings');
    expect(document.querySelectorAll('[role="switch"]')).toHaveLength(0);
    expect(document.querySelectorAll('input[type="radio"]')).toHaveLength(0);
    expect(document.querySelectorAll('button')).toHaveLength(0);
    // No guessed defaults while loading: the registry default accent is not on the page.
    expect(pageText()).not.toContain('#4f9cf9');

    release();
    await loading;
    flushSync();

    expect(document.querySelectorAll('[role="switch"]')).toHaveLength(3);
  });
});

/* ---- 2. load failure ----------------------------------------------------- */

describe('2. load failure', () => {
  it('renders the failed load verbatim and Retry re-issues the GET', async () => {
    const sentence = 'the settings store could not be read';
    let attempts = 0;
    const calls = fakeFetch(() => {
      attempts += 1;
      if (attempts === 1) {
        return jsonResponse(
          { error: { code: 'server_error', message: sentence, detail: null } },
          500
        );
      }
      return jsonResponse({ settings: { 'ui.theme': 'dark' }, secrets: {} });
    });

    await settings.load();
    renderPanel();

    const alert = document.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain(sentence);
    expect(document.querySelectorAll('[role="switch"]')).toHaveLength(0);

    buttonByText('Retry').click();
    await vi.waitFor(() => expect(document.querySelectorAll('[role="switch"]')).toHaveLength(3));

    expect(calls.filter((call) => call.path === '/api/settings')).toHaveLength(2);
    expect(document.querySelector('[role="alert"]')).toBeNull();
    expect(pageText()).not.toContain(sentence);
  });
});

/* ---- 3. the draft is seeded from the echo -------------------------------- */

describe('3. seeded draft', () => {
  it('shows the echoed values and reports nothing dirty', async () => {
    await renderLoaded({
      settings: {
        'ui.theme': 'light',
        'ui.accent_color': '#2ec4b6',
        'ui.dashboard_columns': 4,
        'ui.show_clock': false,
        'ui.hide_cursor': false,
        'ui.show_control_rail': false
      }
    });

    expect(radioByValue('light').checked).toBe(true);
    expect(pageText()).toContain('#2ec4b6');
    expect(selectTrigger('Dashboard columns').textContent).toContain('4');

    const toggles = switches();
    expect(toggles).toHaveLength(3);
    for (const toggle of toggles) {
      expect(toggle.getAttribute('aria-checked')).toBe('false');
    }

    expect(saveButton().disabled).toBe(true);
  });
});

/* ---- 4. theme + save + echo ---------------------------------------------- */

describe('4. theme', () => {
  it('saves only ui.theme and the page adopts the echoed theme', async () => {
    const backend = await renderLoaded({ settings: { 'ui.theme': 'dark' } });

    radioByValue('light').click();
    flushSync();
    expect(saveButton().disabled).toBe(false);

    saveButton().click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));

    flushSync();
    expect(patchBody(patches(backend.calls)[0])).toEqual({ 'ui.theme': 'light' });
    await vi.waitFor(() =>
      expect(toastStore.items.map((item) => item.message)).toContain('Appearance saved.')
    );
    // The echo is the only writer of the theme: the attribute flips when the
    // PATCH resolves, not when the segment is tapped.
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(saveButton().disabled).toBe(true);
  });
});

/* ---- 5. accent + preview ink --------------------------------------------- */

describe('5. accent', () => {
  it('patches the preset alone and the preview follows its ink', async () => {
    const backend = await renderLoaded();

    accentPreset('#2ec4b6').click();
    flushSync();

    const tiles = [...document.querySelectorAll<HTMLElement>('[data-testid="accent-preview"] .preview-tile')];
    expect(tiles.length).toBeGreaterThan(0);
    const reading = readAccent('#2ec4b6');
    expectColour(tiles[0].style.backgroundColor, reading.accent);
    expectColour(tiles[0].style.color, reading.ink);

    saveButton().click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));

    flushSync();
    expect(patchBody(patches(backend.calls)[0])).toEqual({ 'ui.accent_color': '#2ec4b6' });
    expectColour(tiles[0].style.color, readAccent('#2ec4b6').ink);
  });
});

/* ---- 6. the runtime contrast gate ---------------------------------------- */

describe('6. contrast gate', () => {
  it('blocks Save for an accent below 4.5:1 and sends nothing', async () => {
    const backend = await renderLoaded();
    const grey = failingGrey();

    buttonByText('Custom colour…').click();
    flushSync();
    type(inputByLabel('Custom colour'), grey.hex);

    expect(saveButton().disabled).toBe(true);
    expect(pageText()).toContain(formatContrastRatio(grey.ratio));
    expect(pageText()).toContain('4.5:1');
    // The words carry the failure; colour is never the only signal.
    expect(pageText()).toContain('must reach 4.5:1');
    expect(document.querySelector('.contrast')?.getAttribute('role')).toBe('alert');

    saveButton().click();
    flushSync();
    await Promise.resolve();
    expect(patches(backend.calls)).toHaveLength(0);
  });
});

/* ---- 7. malformed hex ---------------------------------------------------- */

describe('7. malformed hex', () => {
  it('blocks Save and shows the field error', async () => {
    const backend = await renderLoaded();

    accentPreset('#2ec4b6').click();
    flushSync();

    buttonByText('Custom colour…').click();
    flushSync();
    type(inputByLabel('Custom colour'), '#zzz');

    expect(saveButton().disabled).toBe(true);
    expect(pageText()).toContain('Use #rgb or #rrggbb, for example #4f9cf9');

    saveButton().click();
    flushSync();
    await Promise.resolve();
    expect(patches(backend.calls)).toHaveLength(0);
  });
});

/* ---- 8. dashboard columns ------------------------------------------------ */

describe('8. dashboard columns', () => {
  it('patches the chosen count as a number', async () => {
    const backend = await renderLoaded();

    chooseColumn('3');
    saveButton().click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));

    flushSync();
    const body = patchBody(patches(backend.calls)[0]);
    expect(body).toEqual({ 'ui.dashboard_columns': 3 });
    // A string here is rejected by the registry: the assertion is the type.
    expect(body['ui.dashboard_columns']).toBe(3);
  });
});

/* ---- 9. the three switches ----------------------------------------------- */

describe('9. switches', () => {
  const cases = [
    { label: 'Show clock', key: 'ui.show_clock' },
    { label: 'Hide cursor', key: 'ui.hide_cursor' },
    { label: 'Control rail', key: 'ui.show_control_rail' }
  ];

  for (const testCase of cases) {
    it(`patches only ${testCase.key} when "${testCase.label}" flips`, async () => {
      unmountAll();
      settings.reset();
      const backend = fakeBackend();
      await settings.load();
      renderPanel();

      switchByLabel(testCase.label).click();
      flushSync();

      saveButton().click();
      await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));

      flushSync();
      expect(patchBody(patches(backend.calls)[0])).toEqual({ [testCase.key]: false });
    });
  }
});

/* ---- 10. a rejected PATCH ------------------------------------------------ */

describe('10. rejected patch', () => {
  it('shows the server sentence verbatim and keeps the draft', async () => {
    const sentence = 'ui.theme must be one of dark, light or auto';
    const backend = await renderLoaded({
      settings: { 'ui.theme': 'dark' },
      patchError: {
        status: 422,
        body: { error: { code: 'validation_error', message: sentence, detail: { key: 'ui.theme' } } }
      }
    });

    radioByValue('light').click();
    flushSync();
    saveButton().click();

    await vi.waitFor(() => expect(pageText()).toContain(sentence));
    expect(document.querySelector('[role="alert"]')?.textContent).toContain(sentence);
    // F5: the edit stays on screen and Save stays usable.
    expect(radioByValue('light').checked).toBe(true);
    expect(saveButton().disabled).toBe(false);
    expect(settings.text('ui.theme', 'dark')).toBe('dark');
    expect(patchBody(patches(backend.calls)[0])).toEqual({ 'ui.theme': 'light' });
  });
});

/* ---- 11. only registry keys ---------------------------------------------- */

describe('11. registry keys', () => {
  it('writes only keys the registry holds and never the volume control key', async () => {
    const allowed = [
      'ui.theme',
      'ui.accent_color',
      'ui.dashboard_columns',
      'ui.show_clock',
      'ui.hide_cursor',
      'ui.show_control_rail'
    ];
    const backend = await renderLoaded();

    radioByValue('light').click();
    accentPreset('#2ec4b6').click();
    flushSync();
    chooseColumn('3');
    switchByLabel('Show clock').click();
    switchByLabel('Hide cursor').click();
    switchByLabel('Control rail').click();
    flushSync();

    saveButton().click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));

    flushSync();
    const written = patches(backend.calls).flatMap((call) => Object.keys(patchBody(call)));
    expect(written.length).toBeGreaterThan(0);
    for (const key of written) {
      expect(allowed).toContain(key);
    }
    expect(written.sort()).toEqual([...allowed].sort());
    expect(pageText()).not.toContain('ui.show_volume_control');
  });
});

/* ---- 12. the preview follows the columns choice -------------------------- */

describe('12. preview', () => {
  it('draws the candidate count and says it in words', async () => {
    await renderLoaded();

    const preview = document.querySelector('[data-testid="accent-preview"]');
    expect(preview?.getAttribute('data-preview-columns')).toBe('0');
    expect(pageText()).toContain('Preview: auto-fit tiles');

    chooseColumn('3');
    expect(preview?.getAttribute('data-preview-columns')).toBe('3');
    expect(pageText()).toContain('Preview: 3 columns');
    expect(preview?.querySelectorAll('.preview-tile')).toHaveLength(3);
  });
});
