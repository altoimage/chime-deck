// @vitest-environment happy-dom
/**
 * Offline DOM coverage for the paid-provider section (HSD-s856). Every API route is served
 * by the in-memory fake through `setFetchImpl`; no request reaches a network or a speech
 * engine. These assertions pin the request bodies and the words a person sees, not layout.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  setFetchImpl,
  type FetchLike,
  type FetchResponse,
  type TtsPresetSummary,
  type TtsProvider,
  type TtsProviderTestResponse,
  type TtsProvidersResponse
} from '../api.js';
import { settings } from '../settings.svelte.js';
import SpeechProviders from './SpeechProviders.svelte';
import { toastStore } from '../ui/toast.svelte.js';

const OPENAI: TtsPresetSummary = {
  id: 'openai',
  label: 'OpenAI',
  description: 'OpenAI hosted speech synthesis.',
  paid: true,
  format: 'mp3',
  cost_per_1k_chars_usd: 0.016,
  pricing_note: 'Estimate from the published tts-1 rate.',
  requires_base_url: false,
  shape: {
    method: 'POST',
    path: '/v1/audio/speech',
    headers: {},
    auth: { style: 'bearer', name: 'Authorization', prefix: 'Bearer ' },
    body_kind: 'json',
    body: { input: '{text}', voice: '{voice}' },
    response: 'raw audio',
    format: 'mp3'
  }
};

const CUSTOM: TtsPresetSummary = {
  id: 'custom',
  label: 'Custom',
  description: 'A configurable HTTP speech endpoint.',
  paid: false,
  format: 'wav',
  cost_per_1k_chars_usd: null,
  pricing_note: 'No published price is available.',
  requires_base_url: true,
  shape: {
    method: 'POST',
    path: '/tts',
    headers: {},
    auth: { style: 'none', name: '', prefix: '' },
    body_kind: 'json',
    body: { text: '{text}' },
    response: 'raw audio',
    format: 'wav'
  }
};

function provider(overrides: Partial<TtsProvider> = {}): TtsProvider {
  return {
    id: 'paid-voice',
    name: 'Hallway voice',
    preset: 'openai',
    base_url: '',
    voice: 'alloy',
    language: '',
    paid: true,
    cost_per_1k_chars_usd: 0.016,
    has_api_key: true,
    overrides: {},
    ...overrides
  };
}

function freeProvider(overrides: Partial<TtsProvider> = {}): TtsProvider {
  return provider({
    id: 'free-voice',
    name: 'Local speech endpoint',
    preset: 'custom',
    base_url: 'http://speech.local',
    voice: '',
    paid: false,
    cost_per_1k_chars_usd: null,
    has_api_key: false,
    ...overrides
  });
}

function envelope(
  providers: TtsProvider[] = [provider()],
  warning: string | null = null
): TtsProvidersResponse {
  return {
    providers,
    presets: [OPENAI, CUSTOM],
    selected: providers[0]?.id ?? '',
    warning
  };
}

function testResult(
  overrides: Partial<TtsProviderTestResponse> = {}
): TtsProviderTestResponse {
  return {
    ok: true,
    provider: 'paid-voice',
    latency_ms: 137,
    chars: 42,
    format: 'mp3',
    cost_per_100_chars_usd: 0.0016,
    estimated_cost_usd: 0.000672,
    error: null,
    ...overrides
  };
}

interface Call {
  path: string;
  init?: RequestInit;
}

interface FakeOptions {
  list?: TtsProvidersResponse;
  listGate?: Promise<void>;
  createError?: { status: number; message: string };
  testResults?: Record<string, TtsProviderTestResponse>;
}

interface FakeBackend {
  calls: Call[];
  providers: TtsProvider[];
  settings: Record<string, unknown>;
}

function jsonResponse(body: unknown, status = 200): FetchResponse {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function errorResponse(status: number, message: string): FetchResponse {
  return jsonResponse({ error: { code: 'test_error', message, detail: null } }, status);
}

function fakeBackend(options: FakeOptions = {}): FakeBackend {
  const initial = options.list ?? envelope();
  const backend: FakeBackend = {
    calls: [],
    providers: initial.providers.map((row) => ({ ...row })),
    settings: { ...(options.list ? {} : {}), 'tts.allow_dynamic_paid': false }
  };
  let generated = 0;
  const impl: FetchLike = async (path, init) => {
    backend.calls.push({ path, init });
    const method = init?.method ?? 'GET';

    if (path === '/api/settings') {
      if (method === 'PATCH') {
        Object.assign(backend.settings, JSON.parse(String(init?.body)) as Record<string, unknown>);
      }
      return jsonResponse({ settings: { ...backend.settings }, secrets: {} });
    }

    if (path === '/api/tts/providers' && method === 'GET') {
      if (options.listGate !== undefined) {
        await options.listGate;
      }
      return jsonResponse({ ...initial, providers: backend.providers.map((row) => ({ ...row })) });
    }

    if (path === '/api/tts/providers' && method === 'POST') {
      if (options.createError !== undefined) {
        return errorResponse(options.createError.status, options.createError.message);
      }
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      generated += 1;
      const presetId = String(body.preset ?? 'openai');
      const preset = initial.presets.find((row) => row.id === presetId) ?? OPENAI;
      const created = provider({
        id: `created-${generated}`,
        name: String(body.name ?? ''),
        preset: presetId,
        base_url: String(body.base_url ?? ''),
        voice: String(body.voice ?? ''),
        language: String(body.language ?? ''),
        paid: body.paid === true,
        cost_per_1k_chars_usd:
          typeof body.cost_per_1k_chars_usd === 'number'
            ? body.cost_per_1k_chars_usd
            : preset.cost_per_1k_chars_usd,
        has_api_key: typeof body.api_key === 'string'
      });
      backend.providers.push(created);
      return jsonResponse(created, 201);
    }

    const providerPath = /^\/api\/tts\/providers\/([^/]+)$/.exec(path);
    if (providerPath !== null && method === 'PATCH') {
      const id = decodeURIComponent(providerPath[1]);
      const index = backend.providers.findIndex((row) => row.id === id);
      if (index < 0) {
        return errorResponse(404, 'the provider could not be found');
      }
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      const old = backend.providers[index];
      const updated = provider({
        ...old,
        name: typeof body.name === 'string' ? body.name : old.name,
        preset: typeof body.preset === 'string' ? body.preset : old.preset,
        base_url: typeof body.base_url === 'string' ? body.base_url : old.base_url,
        voice: typeof body.voice === 'string' ? body.voice : old.voice,
        language: typeof body.language === 'string' ? body.language : old.language,
        paid: typeof body.paid === 'boolean' ? body.paid : old.paid,
        cost_per_1k_chars_usd:
          body.cost_per_1k_chars_usd === null
            ? null
            : typeof body.cost_per_1k_chars_usd === 'number'
              ? body.cost_per_1k_chars_usd
              : old.cost_per_1k_chars_usd,
        has_api_key:
          body.api_key === null
            ? false
            : typeof body.api_key === 'string'
              ? true
              : old.has_api_key
      });
      backend.providers[index] = updated;
      return jsonResponse(updated);
    }

    if (providerPath !== null && method === 'DELETE') {
      const id = decodeURIComponent(providerPath[1]);
      backend.providers = backend.providers.filter((row) => row.id !== id);
      return jsonResponse({ deleted: true, id });
    }

    const testPath = /^\/api\/tts\/providers\/([^/]+)\/test$/.exec(path);
    if (testPath !== null && method === 'POST') {
      const id = decodeURIComponent(testPath[1]);
      return jsonResponse(options.testResults?.[id] ?? testResult({ provider: id }));
    }

    throw new Error(`unexpected request ${method} ${path}`);
  };
  setFetchImpl(impl);
  return backend;
}

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function render(): void {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(SpeechProviders, { target }));
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
    (candidate) => candidate.textContent?.replace(/\s+/g, ' ').trim() === label
  );
  if (button === undefined) {
    throw new Error(`expected a button labelled "${label}"`);
  }
  return button;
}

function inputByLabel(label: string): HTMLInputElement {
  const row = [...document.querySelectorAll<HTMLLabelElement>('label')].find(
    (candidate) => candidate.textContent?.replace(/\*/g, '').replace(/\s+/g, ' ').trim() === label
  );
  const input = row?.htmlFor ? document.getElementById(row.htmlFor) : null;
  if (!(input instanceof HTMLInputElement)) {
    throw new Error(`expected an input labelled "${label}"`);
  }
  return input;
}

function typeInto(label: string, value: string): void {
  const input = inputByLabel(label);
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function selectTrigger(label: string): HTMLButtonElement {
  const trigger = [...document.querySelectorAll<HTMLButtonElement>('button.trigger')].find(
    (candidate) => {
      const id = candidate.getAttribute('aria-labelledby')?.split(/\s+/)[0] ?? '';
      return document.getElementById(id)?.textContent?.trim() === label;
    }
  );
  if (trigger === undefined) {
    throw new Error(`expected a picker labelled "${label}"`);
  }
  return trigger;
}

function choosePreset(value: string): void {
  selectTrigger('Preset').click();
  flushSync();
  const choice = document.querySelector<HTMLInputElement>(`input[type="radio"][value="${value}"]`);
  if (choice === null) {
    throw new Error(`expected preset option ${value}`);
  }
  choice.click();
  flushSync();
}

function record(name: string): HTMLElement {
  const found = [...document.querySelectorAll<HTMLElement>('.provider-record')].find(
    (candidate) => candidate.querySelector('h4')?.textContent === name
  );
  if (found === undefined) {
    throw new Error(`expected provider record ${name}`);
  }
  return found;
}

function bodyOf(call: Call): Record<string, unknown> {
  return JSON.parse(String(call.init?.body)) as Record<string, unknown>;
}

function callsTo(calls: Call[], path: string, method: string): Call[] {
  return calls.filter((call) => call.path === path && call.init?.method === method);
}

async function ready(options: FakeOptions = {}): Promise<FakeBackend> {
  const backend = fakeBackend(options);
  await settings.load();
  render();
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

describe('provider list lifecycle and records', () => {
  it('renders loading before success and keeps a paid word badge off the free record', async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const backend = fakeBackend({ list: envelope([provider(), freeProvider()]), listGate: gate });
    await settings.load();
    render();

    expect(pageText()).toContain('Loading external providers');
    expect(document.querySelectorAll('.provider-record')).toHaveLength(0);

    release();
    await vi.waitFor(() => expect(document.querySelectorAll('.provider-record')).toHaveLength(2));
    expect(record('Hallway voice').querySelector('.badge')?.textContent).toContain('paid');
    expect(record('Local speech endpoint').querySelector('.badge')).toBeNull();
    expect(pageText()).toContain('A key is stored.');
    expect(pageText()).toContain('No key stored.');
    expect(callsTo(backend.calls, '/api/tts/providers', 'GET')).toHaveLength(1);
  });

  it('shows an EmptyState and Add provider for a successful empty response', async () => {
    await ready({ list: envelope([]) });
    await vi.waitFor(() => expect(pageText()).toContain('No external providers are configured.'));

    expect(document.querySelector('.empty-state')).not.toBeNull();
    expect(buttonByText('Add provider').disabled).toBe(false);
  });

  it('renders an envelope warning verbatim above the list', async () => {
    const warning = 'one stored provider could not be read and was skipped';
    await ready({ list: envelope([provider()], warning) });
    await vi.waitFor(() => expect(pageText()).toContain(warning));

    expect(document.querySelector('.warning-row .badge')?.textContent).toContain('warn');
    expect(document.querySelector('.warning-row')?.textContent).toContain(warning);
    expect(record('Hallway voice')).toBeTruthy();
  });
});

describe('provider sheet and write-only API keys', () => {
  it('creates through the sheet and omits api_key when it was untouched', async () => {
    const backend = await ready({ list: envelope([]) });
    await vi.waitFor(() => expect(buttonByText('Add provider').disabled).toBe(false));
    buttonByText('Add provider').click();
    flushSync();

    expect(selectTrigger('Preset').textContent).toContain('OpenAI');
    expect(pageText()).toContain(OPENAI.description);
    expect(pageText()).toContain(OPENAI.pricing_note);
    expect(inputByLabel('Base URL').placeholder).toBe('Preset default');
    expect(pageText()).toContain("When this field is blank, the server uses this preset's default base URL.");
    expect(inputByLabel('API key').value).toBe('');
    expect(inputByLabel('API key').type).toBe('password');
    expect(pageText()).toContain('No key stored.');
    expect(document.querySelector('button[aria-label="Show value"]')).not.toBeNull();
    expect(pageText()).not.toContain('Clear stored key');

    typeInto('Name', 'New hallway voice');
    typeInto('Voice', 'alloy');
    buttonByText('Save provider').click();
    await vi.waitFor(() => expect(callsTo(backend.calls, '/api/tts/providers', 'POST')).toHaveLength(1));

    const body = bodyOf(callsTo(backend.calls, '/api/tts/providers', 'POST')[0]);
    expect(body).toEqual({
      name: 'New hallway voice',
      preset: 'openai',
      base_url: '',
      voice: 'alloy',
      language: '',
      paid: true,
      cost_per_1k_chars_usd: null
    });
    expect(Object.keys(body)).not.toContain('api_key');
  });

  it('shows the stored-key and pending-clear hints and sends null for a clear', async () => {
    const backend = await ready({ list: envelope([provider({ has_api_key: true })]) });
    await vi.waitFor(() => expect(record('Hallway voice')).toBeTruthy());
    buttonByText('Edit').click();
    flushSync();

    expect(inputByLabel('Name').value).toBe('Hallway voice');
    expect(inputByLabel('Voice').value).toBe('alloy');
    expect(inputByLabel('API key').value).toBe('');
    expect(pageText()).toContain('A key is stored. Type a new one to replace it.');
    expect(buttonByText('Clear stored key').disabled).toBe(false);

    buttonByText('Clear stored key').click();
    flushSync();
    expect(pageText()).toContain('The stored key will be cleared when you save.');
    expect(buttonByText('Clear stored key').disabled).toBe(true);
    expect(inputByLabel('API key').disabled).toBe(true);

    buttonByText('Save provider').click();
    await vi.waitFor(() => expect(callsTo(backend.calls, '/api/tts/providers/paid-voice', 'PATCH')).toHaveLength(1));
    expect(bodyOf(callsTo(backend.calls, '/api/tts/providers/paid-voice', 'PATCH')[0]).api_key).toBeNull();
  });

  it('sends a newly typed API key as a string and never echoes a stored value', async () => {
    const backend = await ready({ list: envelope([provider({ has_api_key: true })]) });
    await vi.waitFor(() => expect(record('Hallway voice')).toBeTruthy());
    buttonByText('Edit').click();
    flushSync();

    typeInto('API key', 'replacement-key');
    buttonByText('Save provider').click();
    await vi.waitFor(() => expect(callsTo(backend.calls, '/api/tts/providers/paid-voice', 'PATCH')).toHaveLength(1));
    expect(bodyOf(callsTo(backend.calls, '/api/tts/providers/paid-voice', 'PATCH')[0]).api_key).toBe('replacement-key');
    expect(pageText()).not.toContain('replacement-key');
  });

  it('offers Clear stored key only for an existing key and delays problems until a save attempt', async () => {
    await ready({ list: envelope([freeProvider()]) });
    await vi.waitFor(() => expect(record('Local speech endpoint')).toBeTruthy());
    buttonByText('Edit').click();
    flushSync();
    expect(pageText()).not.toContain('Clear stored key');
    expect(pageText()).toContain('No key stored.');

    buttonByText('Cancel').click();
    flushSync();
    buttonByText('Add provider').click();
    flushSync();
    expect(document.querySelector('.problems')).toBeNull();
    expect(buttonByText('Save provider').disabled).toBe(true);

    inputByLabel('Name').dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
    );
    flushSync();
    expect(document.querySelector('.problems')?.textContent).toContain('This provider needs a name.');
    expect(buttonByText('Save provider').disabled).toBe(true);
  });

  it('keeps a rejected 422 sentence and the entered draft in the open sheet', async () => {
    const message = 'this provider name is already in use';
    const backend = await ready({ list: envelope([]), createError: { status: 422, message } });
    await vi.waitFor(() => expect(buttonByText('Add provider').disabled).toBe(false));
    buttonByText('Add provider').click();
    flushSync();
    typeInto('Name', 'Duplicated voice');
    typeInto('Voice', 'alloy');

    buttonByText('Save provider').click();
    await vi.waitFor(() => expect(callsTo(backend.calls, '/api/tts/providers', 'POST')).toHaveLength(1));
    await vi.waitFor(() => expect(document.querySelector('[role="alert"]')?.textContent).toContain(message));

    expect(document.querySelector('dialog[open]')).not.toBeNull();
    expect(inputByLabel('Name').value).toBe('Duplicated voice');
    expect(inputByLabel('Voice').value).toBe('alloy');
  });

  it('PATCHes edits only through updateTtsProvider', async () => {
    const backend = await ready({ list: envelope([provider()]) });
    await vi.waitFor(() => expect(record('Hallway voice')).toBeTruthy());
    buttonByText('Edit').click();
    flushSync();
    typeInto('Name', 'Updated hallway voice');
    buttonByText('Save provider').click();
    await vi.waitFor(() => expect(callsTo(backend.calls, '/api/tts/providers/paid-voice', 'PATCH')).toHaveLength(1));

    expect(callsTo(backend.calls, '/api/tts/providers', 'POST')).toHaveLength(0);
    expect(bodyOf(callsTo(backend.calls, '/api/tts/providers/paid-voice', 'PATCH')[0]).name).toBe('Updated hallway voice');
  });
});

describe('provider delete and Test', () => {
  it('deletes only after confirmation; Cancel and Escape call oncancel without a request', async () => {
    const backend = await ready({ list: envelope([provider()]) });
    await vi.waitFor(() => expect(record('Hallway voice')).toBeTruthy());

    buttonByText('Delete').click();
    flushSync();
    expect(document.querySelector('dialog[open]')?.textContent).toContain('Delete “Hallway voice”?');
    expect(buttonByText('Delete Hallway voice').textContent?.trim()).toBe('Delete Hallway voice');
    buttonByText('Cancel').click();
    flushSync();
    expect(callsTo(backend.calls, '/api/tts/providers/paid-voice', 'DELETE')).toHaveLength(0);

    buttonByText('Delete').click();
    flushSync();
    const openDialog = document.querySelector<HTMLDialogElement>('dialog[open]');
    expect(openDialog).not.toBeNull();
    openDialog?.dispatchEvent(new Event('cancel', { bubbles: true, cancelable: true }));
    flushSync();
    expect(callsTo(backend.calls, '/api/tts/providers/paid-voice', 'DELETE')).toHaveLength(0);

    buttonByText('Delete').click();
    flushSync();
    buttonByText('Delete Hallway voice').click();
    await vi.waitFor(() => expect(callsTo(backend.calls, '/api/tts/providers/paid-voice', 'DELETE')).toHaveLength(1));
  });

  it('reports measured latency and cost per 100 characters for a priced provider', async () => {
    const backend = await ready({
      list: envelope([provider()]),
      testResults: { 'paid-voice': testResult() }
    });
    await vi.waitFor(() => expect(record('Hallway voice')).toBeTruthy());
    buttonByText('Test').click();

    await vi.waitFor(() => expect(record('Hallway voice').querySelector('.test-result')).not.toBeNull());
    const result = record('Hallway voice').querySelector<HTMLElement>('.test-result');
    expect(result?.getAttribute('aria-live')).toBe('polite');
    expect(result?.textContent).toContain('137 ms');
    expect(result?.textContent).toContain('$0.0016 per 100 characters');
    expect(result?.textContent).toContain('Estimated cost per 100 characters: $0.0016');
    expect(callsTo(backend.calls, '/api/tts/providers/paid-voice/test', 'POST')).toHaveLength(1);
  });

  it('calls an unpriced result unknown and never renders $0', async () => {
    const backend = await ready({
      list: envelope([freeProvider()]),
      testResults: {
        'free-voice': testResult({
          provider: 'free-voice',
          cost_per_100_chars_usd: null,
          estimated_cost_usd: null
        })
      }
    });
    await vi.waitFor(() => expect(record('Local speech endpoint')).toBeTruthy());
    buttonByText('Test').click();
    await vi.waitFor(() => expect(record('Local speech endpoint').querySelector('.test-result')).not.toBeNull());

    expect(pageText().toLowerCase()).toContain('unknown');
    expect(pageText()).not.toContain('$0');
  });

  it('shows a failed test badge and the server error sentence verbatim', async () => {
    const message = 'the provider returned an audio format this server cannot decode';
    await ready({
      list: envelope([provider()]),
      testResults: {
        'paid-voice': testResult({ ok: false, error: message, latency_ms: null, cost_per_100_chars_usd: null })
      }
    });
    await vi.waitFor(() => expect(record('Hallway voice')).toBeTruthy());
    buttonByText('Test').click();
    await vi.waitFor(() => expect(record('Hallway voice').querySelector('.test-result')?.textContent).toContain(message));

    expect(record('Hallway voice').querySelector('.test-result .badge')?.textContent).toContain('Test failed');
    expect(record('Hallway voice').querySelector('.test-result')?.textContent).toContain(message);
  });
});

describe('shared paid-template policy', () => {
  it('PATCHes exactly the tts.allow_dynamic_paid key', async () => {
    const backend = await ready({ list: envelope([]) });
    await vi.waitFor(() => expect(document.querySelector('[role="switch"]')).not.toBeNull());

    document.querySelector<HTMLButtonElement>('[role="switch"]')?.click();
    await vi.waitFor(() => expect(callsTo(backend.calls, '/api/settings', 'PATCH')).toHaveLength(1));

    expect(bodyOf(callsTo(backend.calls, '/api/settings', 'PATCH')[0])).toEqual({
      'tts.allow_dynamic_paid': true
    });
  });
});
