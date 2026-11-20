// @vitest-environment happy-dom
/**
 * Offline unit and DOM acceptance tests for the per-tile state→tone editor.
 * The component's only request is stubbed through the API fetch seam; no test
 * can contact Home Assistant or any other service.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  setFetchImpl,
  type EntityResponse,
  type FetchLike,
  type FetchResponse,
  type HaEntityDetail,
  type StateToneDefaults
} from '../api.js';
import EntityEditor from './EntityEditor.svelte';
import StateToneTableTestHost from './StateToneTableTestHost.svelte';
import {
  isNeverMappedState,
  mergeStateTones,
  stateToneRows,
  validateNewState
} from './state-tones.js';

const toneDefaults: StateToneDefaults = {
  tones: ['ok', 'warn', 'alert', 'off'],
  defaults: { locked: 'ok', active: 'warn', closed: 'ok', open: 'warn' }
};

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function okResponse(body: unknown, status = 200): FetchResponse {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function stubToneDefaults(
  defaults: StateToneDefaults = toneDefaults,
  fetcher: FetchLike = async () => okResponse(defaults)
): void {
  setFetchImpl(fetcher);
}

function renderTable(props: {
  currentState?: string | null;
  currentWord?: string | null;
  currentTone?: string | null;
} = {}): void {
  mounted.push(mount(StateToneTableTestHost, { target: appendTarget(), props }));
  flushSync();
}

function appendTarget(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  return target;
}

function pageText(): string {
  return (document.body.textContent ?? '').replace(/\s+/g, ' ').trim();
}

function buttonByText(label: string): HTMLButtonElement {
  const button = [...document.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => (candidate.textContent ?? '').replace(/\s+/g, ' ').trim() === label
  );
  if (button === undefined) {
    throw new Error(`expected button "${label}"; saw ${[...document.querySelectorAll('button')]
      .map((item) => (item.textContent ?? '').replace(/\s+/g, ' ').trim())
      .join(', ')}`);
  }
  return button;
}

function fieldset(label: string): HTMLFieldSetElement {
  const found = [...document.querySelectorAll<HTMLFieldSetElement>('fieldset.segmented')].find(
    (candidate) => candidate.querySelector('legend')?.textContent === label
  );
  if (found === undefined) {
    throw new Error(`expected tone group "${label}"`);
  }
  return found;
}

function chooseTone(groupLabel: string, tone: string): void {
  const radio = fieldset(groupLabel).querySelector<HTMLInputElement>(
    `input[type="radio"][value="${tone}"]`
  );
  if (radio === null) {
    throw new Error(`expected ${tone} in "${groupLabel}"`);
  }
  radio.click();
  flushSync();
}

function enterRawState(value: string): HTMLInputElement {
  const input = [...document.querySelectorAll<HTMLInputElement>('input[type="text"]')].find(
    (candidate) => candidate.labels?.[0]?.textContent?.trim() === 'Raw state'
  );
  if (input === undefined) {
    throw new Error('expected the Raw state input');
  }
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  return input;
}

function boundDraft(): { stateTones: Record<string, string>; colourByState: boolean } {
  const output = document.querySelector<HTMLOutputElement>('[data-testid="bound-state-tone-draft"]');
  if (output === null) {
    throw new Error('expected the harness to expose the bound draft');
  }
  return JSON.parse(output.textContent?.trim() ?? '{}') as {
    stateTones: Record<string, string>;
    colourByState: boolean;
  };
}

function unmountAll(): void {
  for (const component of mounted.splice(0)) {
    unmount(component);
  }
  document.body.innerHTML = '';
}

function editorEntity(): EntityResponse {
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
    warning: null
  };
}

function editorDetail(): HaEntityDetail {
  return {
    entity_id: 'lock.front_door',
    name: 'Front Door',
    state: 'locked',
    icon: 'mdi:lock',
    icon_source: 'ha-attribute',
    domain: 'lock',
    device_class: null,
    attributes: {},
    last_changed: null,
    found: true,
    warning: null
  };
}

function renderEditor(): void {
  mounted.push(
    mount(EntityEditor, {
      target: appendTarget(),
      props: { id: 1, onback: () => {} }
    })
  );
  flushSync();
}

beforeEach(() => {
  stubToneDefaults();
});

afterEach(() => {
  unmountAll();
  setFetchImpl(null);
});

describe('state-tone table rules', () => {
  it('merges defaults with overrides without mutating either input', () => {
    const defaults = { locked: 'ok', open: 'warn', unavailable: 'alert' };
    const overrides = { locked: 'alert', custom: 'off', unknown: 'warn' };

    expect(mergeStateTones(defaults, overrides)).toEqual({
      locked: 'alert',
      open: 'warn',
      custom: 'off'
    });
    expect(defaults).toEqual({ locked: 'ok', open: 'warn', unavailable: 'alert' });
    expect(overrides).toEqual({ locked: 'alert', custom: 'off', unknown: 'warn' });
  });

  it('orders current state first, explicit custom rows next, then remaining defaults', () => {
    const rows = stateToneRows(
      { locked: 'ok', open: 'warn', closed: 'ok' },
      { open: 'alert', custom_b: 'off', custom_a: 'warn' },
      'custom_a'
    );

    expect(rows.map((row) => [row.state, row.provenance])).toEqual([
      ['custom_a', 'custom'],
      ['open', 'custom'],
      ['custom_b', 'custom'],
      ['locked', 'default'],
      ['closed', 'default']
    ]);
  });

  it('never exposes protected states as choices and validates raw input', () => {
    expect(isNeverMappedState('unavailable')).toBe(true);
    expect(isNeverMappedState('UNKNOWN')).toBe(true);
    expect(validateNewState('   ', {}, {})).toBe('Enter a state value.');
    expect(validateNewState('unavailable', {}, {})).toContain('always shows them grey');
    expect(validateNewState('locked', { locked: 'ok' }, {})).toContain('already has');
    expect(validateNewState('armed', {}, {})).toBeNull();
  });
});

describe('StateToneTable DOM', () => {
  it('shows default provenance, makes an override custom, and reset removes the override', async () => {
    renderTable({ currentState: 'locked', currentWord: 'Locked', currentTone: 'ok' });
    await viWaitForTable();

    const lockedRow = [...document.querySelectorAll<HTMLElement>('.tone-row')].find(
      (row) => row.querySelector('.state-token')?.textContent === 'locked'
    );
    expect(lockedRow?.querySelector('.provenance')?.textContent).toBe('default');
    expect(lockedRow?.textContent).not.toContain('Reset to default');
    expect(fieldset('Tone for locked').textContent).toContain('OK');
    expect(fieldset('Tone for locked').textContent).toContain('Warn');
    expect(fieldset('Tone for locked').querySelectorAll('.swatch')).toHaveLength(4);

    chooseTone('Tone for locked', 'alert');
    expect(lockedRow?.querySelector('.provenance')?.textContent).toBe('custom');
    expect(boundDraft().stateTones).toEqual({ locked: 'alert' });
    expect(buttonByText('Reset to default')).toBeTruthy();

    buttonByText('Reset to default').click();
    flushSync();
    expect(boundDraft().stateTones).toEqual({});
    expect(lockedRow?.querySelector('.provenance')?.textContent).toBe('default');
  });

  it('adds a custom state and rejects empty, duplicate, and unavailable values without clearing input', async () => {
    renderTable();
    await viWaitForTable();

    const emptyInput = enterRawState('');
    expect(emptyInput.getAttribute('autocapitalize')).toBe('none');
    expect(emptyInput.getAttribute('autocorrect')).toBe('off');
    expect(emptyInput.getAttribute('spellcheck')).toBe('false');
    buttonByText('Add state').click();
    flushSync();
    expect(pageText()).toContain('Enter a state value.');
    expect(emptyInput.value).toBe('');

    const duplicateInput = enterRawState('locked');
    buttonByText('Add state').click();
    flushSync();
    expect(pageText()).toContain('That state already has a tone mapping.');
    expect(duplicateInput.value).toBe('locked');

    const protectedInput = enterRawState('unavailable');
    buttonByText('Add state').click();
    flushSync();
    expect(pageText()).toContain(
      'Unavailable and unknown are never coloured; the tile always shows them grey.'
    );
    expect(protectedInput.value).toBe('unavailable');

    enterRawState('armed');
    chooseTone('Tone for new state', 'warn');
    buttonByText('Add state').click();
    flushSync();

    expect(boundDraft().stateTones).toEqual({ armed: 'warn' });
    const customRow = [...document.querySelectorAll<HTMLElement>('.tone-row')].find(
      (row) => row.querySelector('.state-token')?.textContent === 'armed'
    );
    expect(customRow?.querySelector('.provenance')?.textContent).toBe('custom');
    expect(buttonByText('Remove state')).toBeTruthy();
  });

  it('offers the unmistakable current-state assignment and removes the hint after a tone is chosen', async () => {
    renderTable({ currentState: 'motion_detected', currentWord: 'Motion detected', currentTone: 'off' });
    await viWaitForTable();

    expect(pageText()).toContain('This state has no tone — assign one');
    buttonByText('Assign a tone').click();
    flushSync();

    const pendingRow = [...document.querySelectorAll<HTMLElement>('.tone-row')].find(
      (row) => row.querySelector('.state-token')?.textContent === 'motion_detected'
    );
    expect(pendingRow).toBeTruthy();
    expect(pageText()).toContain('Choose a tone to finish assigning this current state.');
    expect(pendingRow?.contains(document.activeElement)).toBe(true);
    expect((document.activeElement as HTMLInputElement).type).toBe('radio');

    chooseTone('Tone for motion_detected', 'alert');
    expect(pageText()).not.toContain('This state has no tone — assign one');
    expect(pageText()).toContain('Current state: Motion detected — Alert');
    expect(boundDraft().stateTones).toEqual({ motion_detected: 'alert' });
  });

  it('keeps mappings visible and editable when Colour by state is off', async () => {
    renderTable({ currentState: 'locked', currentWord: 'Locked', currentTone: 'ok' });
    await viWaitForTable();

    const toggle = document.querySelector<HTMLButtonElement>('button[role="switch"]');
    expect(toggle).not.toBeNull();
    toggle?.click();
    flushSync();

    expect(toggle?.getAttribute('aria-checked')).toBe('false');
    expect(pageText()).toContain('The tile stays neutral right now');
    expect(pageText()).toContain('would be OK');
    expect(document.querySelector('.mapping-content.deemphasized')).not.toBeNull();
    expect(document.querySelectorAll('.tone-row').length).toBeGreaterThan(0);
    expect(fieldset('Tone for locked')).toBeTruthy();
  });

  it('does not invent an empty defaults table on load failure and allows retry', async () => {
    let attempts = 0;
    stubToneDefaults(toneDefaults, async () => {
      attempts += 1;
      return attempts === 1
        ? okResponse({ error: { code: 'offline', message: 'Defaults are temporarily unavailable.' } }, 503)
        : okResponse(toneDefaults);
    });

    renderTable();
    await viWaitForTableError();
    expect(pageText()).toContain('Defaults are temporarily unavailable.');
    expect(document.querySelector('.tone-row')).toBeNull();

    buttonByText('Retry state tones').click();
    await viWaitForTable();
    expect(document.querySelector('.tone-row')).not.toBeNull();
    expect(attempts).toBe(2);
  });
});

describe('EntityEditor integration', () => {
  it('renders the state tone section without any native select elements', async () => {
    const row = editorEntity();
    const detail = editorDetail();
    setFetchImpl(async (path) => {
      if (path === '/api/entities') {
        return okResponse([row]);
      }
      if (path === '/api/entities/state-tones') {
        return okResponse(toneDefaults);
      }
      if (path === '/api/ha/entities/lock.front_door') {
        return okResponse(detail);
      }
      if (path.startsWith('/api/icon/')) {
        return okResponse({ name: 'lock', path: 'M0 0h1v1H0z', view_box: '0 0 24 24' });
      }
      throw new Error(`unexpected offline test request: ${path}`);
    });

    renderEditor();
    await viWaitForTable();

    expect(pageText()).toContain('State tones');
    expect(pageText()).toContain('Tone for locked');
    expect(document.querySelector('select')).toBeNull();
  });
});

async function viWaitForTable(): Promise<void> {
  await vi.waitFor(() => expect(document.querySelector('.tone-row')).not.toBeNull());
}

async function viWaitForTableError(): Promise<void> {
  await vi.waitFor(() => expect(pageText()).toContain('Defaults are temporarily unavailable.'));
}
