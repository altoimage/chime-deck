// @vitest-environment happy-dom
/**
 * DOM tests for the Speech settings panel (HSD-31dk).
 *
 * Fully offline: every request goes through `setFetchImpl`, and the fake serves all of
 * the routes this panel reads — the settings pair, the engine list, the voice catalogue
 * and the two download routes — so no test opens a socket, a real audio device or a TTS
 * engine.
 *
 * They prove semantics — the words rendered, the key in the PATCH, the store's adopted
 * value, the poll's bound — never layout geometry; the D26 sizing lives in the styles
 * and the contrast gate checks the tokens. Every `reason`, `warning` and `error` here is
 * the server's own whole sentence, so the assertions quote it verbatim: this panel
 * invents none of them.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  setFetchImpl,
  type FetchLike,
  type FetchResponse,
  type TtsEnginesResponse,
  type TtsPresetSummary,
  type TtsProvider,
  type TtsProvidersResponse,
  type TtsVoicesResponse,
  type VoiceDownloadStatus
} from './api.js';
import SpeechPanel from './settings/SpeechPanel.svelte';
import { settings } from './settings.svelte.js';
import {
  DEFAULT_ENGINE,
  DEFAULT_ENGINE_OPTIONS,
  DOWNLOAD_POLL_INTERVAL_MS,
  DOWNLOAD_POLL_MAX_ATTEMPTS,
  SPEECH_OFF,
  downloadStateLabel,
  formatProgress,
  formatSynthesisMs,
  isDownloadFinished
} from './speech.js';
import { toastStore } from './ui/toast.svelte.js';

/* ---- fetch fake ---------------------------------------------------------- */

interface FakeCall {
  path: string;
  init?: RequestInit;
}

const MIB = 1024 * 1024;

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

/* ---- the server's own sentences ------------------------------------------ */

/** The piper engine's refusal, as the resolver words it. */
const PIPER_REASON =
  'piper-tts is not installed; run `uv sync --extra piper` and restart the doorbell';

/** The espeak-ng engine's refusal. */
const ESPEAK_REASON = 'espeak-ng is not installed; install it with `apt install espeak-ng`';

/** The remote renderer's refusal. */
const REMOTE_REASON = 'set tts.remote_tts.url to the renderer this device should use';

/** The D22 RAM sentence: a small board still gets *why*, even when refused elsewhere. */
const MEMORY_WARNING =
  'this device has 967 MB of RAM and local Piper needs about 2048 MB; use Home Assistant’s voices or espeak-ng, or lower tts.piper.min_ram_mb if you know better';

/** The whole refusal sentence a small board is offered instead of a download. */
const UNAVAILABLE_REASON = 'local Piper voice models cannot be installed on this device';

/**
 * The stored renderer token's value. The backend answers only the *flag*
 * (`secrets['tts.remote_tts.token']`) and never this string, so finding it anywhere on
 * the page is the failure this constant exists to catch.
 */
const STORED_TOKEN = 'stored-renderer-token-must-not-render';

const DEFAULT_ENGINES: TtsEnginesResponse = {
  disabled: false,
  engines: [
    { name: 'ha_tts', available: true, reason: null, last_synthesis_ms: 18 },
    { name: 'piper', available: false, reason: PIPER_REASON, last_synthesis_ms: null },
    { name: 'espeak_ng', available: false, reason: ESPEAK_REASON, last_synthesis_ms: null },
    { name: 'remote_tts', available: false, reason: REMOTE_REASON, last_synthesis_ms: null }
  ],
  warning: null
};

const PROVIDER_PRESET: TtsPresetSummary = {
  id: 'openai',
  label: 'OpenAI',
  description: 'OpenAI hosted speech.',
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

const PAID_PROVIDER: TtsProvider = {
  id: 'provider-paid',
  name: 'Doorbell voice',
  preset: 'openai',
  base_url: '',
  voice: 'alloy',
  language: '',
  paid: true,
  cost_per_1k_chars_usd: 0.016,
  has_api_key: true,
  overrides: {}
};

function providerEnvelope(providers: TtsProvider[] = [PAID_PROVIDER]): TtsProvidersResponse {
  return { providers, presets: [PROVIDER_PRESET], selected: providers[0]?.id ?? '', warning: null };
}

const DEFAULT_VOICES: TtsVoicesResponse = {
  disabled: false,
  can_download: false,
  memory_warning: MEMORY_WARNING,
  unavailable_reason: UNAVAILABLE_REASON,
  suggested: [
    {
      id: 'en_GB-alan-low',
      language: 'en_GB',
      quality: 'low',
      size_mb: 22.1,
      sample_rate_hz: 16000,
      num_speakers: 1,
      licence: 'CC0',
      licence_url: null,
      installed: false,
      problem: null
    },
    {
      id: 'en_US-lessac-medium',
      language: 'en_US',
      quality: 'medium',
      size_mb: 63.4,
      sample_rate_hz: 22050,
      num_speakers: 1,
      licence: 'MIT',
      licence_url: null,
      installed: false,
      problem: null
    },
    {
      id: 'en_GB-jenny_dioco-medium',
      language: 'en_GB',
      quality: 'medium',
      size_mb: 63.0,
      sample_rate_hz: 22050,
      num_speakers: 1,
      licence: null,
      licence_url: null,
      installed: false,
      problem: null
    }
  ],
  installed: [],
  warning: null
};

/** A job that is still transferring. */
function voiceDownloadRunning(
  bytesDone: number,
  bytesTotal: number | null = null
): VoiceDownloadStatus {
  return {
    job_id: 'job-1',
    voice: 'en_GB-alan-low',
    state: 'running',
    bytes_done: bytesDone,
    bytes_total: bytesTotal,
    error: null,
    detail: {},
    started_at: '2026-01-01T00:00:00Z',
    finished_at: null
  };
}

/**
 * A job that has stopped. A terminal job has written its last byte (or given up), which
 * is why it reports the full size: only `done` is ever read for its words.
 */
function voiceDownloadTerminal(state: string, error: string | null = null): VoiceDownloadStatus {
  return {
    ...voiceDownloadRunning(60 * MIB, 60 * MIB),
    state,
    error,
    finished_at: '2026-01-01T00:01:00Z'
  };
}

interface BackendOptions {
  settings?: Record<string, unknown>;
  secrets?: Record<string, boolean>;
  engines?: TtsEnginesResponse;
  voices?: TtsVoicesResponse;
  providers?: TtsProvidersResponse;
  patchError?: { status: number; body: unknown };
  /** Answer every `GET /api/tts/engines` with this sentence in a 500 envelope. */
  engineError?: string;
  /** Answer every `GET /api/tts/voices` with this sentence in a 500 envelope. */
  voicesError?: string;
  downloadStart?: unknown;
  downloadError?: { status: number; body: unknown };
  /** Snapshots served in order by the download route; the last one repeats. */
  downloadStates?: VoiceDownloadStatus[];
}

interface Backend {
  calls: FakeCall[];
  settings: Record<string, unknown>;
}

/**
 * The panel's routes, as one handler.
 *
 * Separate from `fakeBackend` so a test can wrap it (group 1 holds every answer behind a
 * gate) without restating the payloads. Any other path throws, so a request the panel is
 * not supposed to make fails loudly instead of silently answering `undefined`.
 */
function backendRoutes(
  options: BackendOptions,
  stored: Record<string, unknown>
): (path: string, init?: RequestInit) => FetchResponse | Promise<FetchResponse> {
  const secrets = { ...(options.secrets ?? {}) };
  const engines = options.engines ?? DEFAULT_ENGINES;
  const voices = options.voices ?? DEFAULT_VOICES;
  const providers = options.providers ?? providerEnvelope([]);
  const states = options.downloadStates ?? [voiceDownloadRunning(0, null)];
  let served = 0;

  return (path, init) => {
    if (path === '/api/settings') {
      if (init?.method === 'PATCH') {
        if (options.patchError) {
          return jsonResponse(options.patchError.body, options.patchError.status);
        }
        Object.assign(stored, JSON.parse(String(init.body)) as Record<string, unknown>);
      }
      return jsonResponse({ settings: { ...stored }, secrets: { ...secrets } });
    }

    if (path === '/api/tts/engines') {
      if (options.engineError !== undefined) {
        return jsonResponse(
          {
            error: {
              code: 'engine_list_unavailable',
              message: options.engineError,
              detail: null
            }
          },
          500
        );
      }
      return jsonResponse(engines);
    }

    if (path === '/api/tts/providers') {
      return jsonResponse(providers);
    }

    if (path === '/api/tts/voices') {
      if (options.voicesError !== undefined) {
        return jsonResponse(
          {
            error: {
              code: 'voice_list_unavailable',
              message: options.voicesError,
              detail: null
            }
          },
          500
        );
      }
      return jsonResponse(voices);
    }

    if (path.startsWith('/api/tts/voices/downloads/')) {
      const snapshot = states[Math.min(served, states.length - 1)];
      served += 1;
      return jsonResponse(snapshot);
    }

    const started = /^\/api\/tts\/voices\/([^/]+)\/download$/.exec(path);
    if (started !== null && init?.method === 'POST') {
      if (options.downloadError) {
        return jsonResponse(options.downloadError.body, options.downloadError.status);
      }
      return jsonResponse(
        options.downloadStart ?? {
          job_id: 'job-1',
          voice: decodeURIComponent(String(started[1])),
          state: 'running'
        },
        202
      );
    }

    throw new Error(`unexpected request ${path}`);
  };
}

/** The in-memory backend: GET echoes, PATCH merges and echoes, jobs progress in order. */
function fakeBackend(options: BackendOptions = {}): Backend {
  const stored: Record<string, unknown> = { ...(options.settings ?? {}) };
  const calls = fakeFetch(backendRoutes(options, stored));
  return { calls, settings: stored };
}

/* ---- DOM helpers --------------------------------------------------------- */

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function renderPanel(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(SpeechPanel, { target }));
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

function buttonByAriaLabel(label: string): HTMLButtonElement {
  const button = document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  if (button === null) {
    throw new Error(`expected a button with aria-label "${label}"`);
  }
  return button;
}

function buttonsMatching(prefix: string): HTMLButtonElement[] {
  return [...document.querySelectorAll<HTMLButtonElement>('button')].filter((candidate) =>
    (candidate.textContent ?? '').trim().startsWith(prefix)
  );
}

function saveButton(): HTMLButtonElement {
  return buttonByText('Save speech');
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

function radioByValue(value: string): HTMLInputElement {
  const radio = [...document.querySelectorAll<HTMLInputElement>('input[type="radio"]')].find(
    (candidate) => candidate.value === value
  );
  if (radio === undefined) {
    throw new Error(`expected a radio with value "${value}"`);
  }
  return radio;
}

function radioValues(): string[] {
  return [...document.querySelectorAll<HTMLInputElement>('input[type="radio"]')].map(
    (input) => input.value
  );
}

/** Open a Select's sheet and tap one row — the two taps a person makes. */
function chooseOption(triggerLabel: string, value: string): void {
  selectTrigger(triggerLabel).click();
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

function hasInputLabel(label: string): boolean {
  return [...document.querySelectorAll<HTMLLabelElement>('label')].some(
    (candidate) => candidate.textContent?.trim() === label
  );
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

function pathCalls(calls: FakeCall[], path: string): FakeCall[] {
  return calls.filter((call) => call.path === path);
}

function downloadStarts(calls: FakeCall[]): FakeCall[] {
  return calls.filter(
    (call) => call.path.endsWith('/download') && call.init?.method === 'POST'
  );
}

function downloadPolls(calls: FakeCall[]): FakeCall[] {
  return calls.filter((call) => call.path.startsWith('/api/tts/voices/downloads/'));
}

function spinnerLabels(): string[] {
  return [...document.querySelectorAll('[role="status"]')].map(
    (node) => node.textContent?.trim() ?? ''
  );
}

/**
 * Load the store from a fresh fake, then mount.
 *
 * Not `await vi.waitFor`: the engines and voices fetches start on mount and the tests
 * that care about them wait for their own observable result.
 */
async function renderLoaded(options: BackendOptions = {}): Promise<Backend> {
  const backend = fakeBackend(options);
  await settings.load();
  renderPanel();
  return backend;
}

/**
 * Let every resolved promise (and Svelte's own microtask flush) settle while `setTimeout`
 * is faked. `vi.waitFor` polls with a timer, so it can never settle under fake timers.
 */
async function flushMicrotasks(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
  flushSync();
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
  vi.useRealTimers();
});

/* ---- 1. loading ---------------------------------------------------------- */

describe('1. loading', () => {
  it('renders only the spinner until the settings echo arrives', async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const routes = backendRoutes({}, {});
    fakeFetch(async (path, init) => {
      await gate;
      return routes(path, init);
    });

    const loading = settings.load();
    renderPanel();

    expect(spinnerLabels().join(' ')).toContain('Loading speech settings');
    expect(document.querySelectorAll('button.trigger')).toHaveLength(0);
    expect(document.querySelectorAll('input')).toHaveLength(0);
    // No guessed defaults while loading: neither the registry default engine nor the
    // label a person would read for it is on the page before the server has spoken.
    expect(pageText()).not.toContain(DEFAULT_ENGINE);
    expect(pageText()).not.toContain("Home Assistant's voices");

    release();
    await loading;
    flushSync();

    expect(selectTrigger('Default engine')).toBeTruthy();
  });
});

/* ---- 2. load failure ----------------------------------------------------- */

describe('2. load failure', () => {
  it('renders the failed load verbatim and Retry re-issues the GET', async () => {
    const sentence = 'the settings store could not be read';
    let attempts = 0;
    const routes = backendRoutes({}, {});
    const calls = fakeFetch((path, init) => {
      if (path === '/api/settings' && init?.method !== 'PATCH') {
        attempts += 1;
        if (attempts === 1) {
          return jsonResponse(
            { error: { code: 'server_error', message: sentence, detail: null } },
            500
          );
        }
      }
      return routes(path, init);
    });

    await settings.load();
    renderPanel();

    expect(document.querySelector('[role="alert"]')?.textContent).toContain(sentence);
    expect(document.querySelectorAll('button.trigger')).toHaveLength(0);

    buttonByText('Retry').click();
    await vi.waitFor(() => expect(document.querySelectorAll('button.trigger')).toHaveLength(1));

    expect(pathCalls(calls, '/api/settings')).toHaveLength(2);
    expect(document.querySelector('[role="alert"]')).toBeNull();
    expect(pageText()).not.toContain(sentence);
    expect(saveButton().disabled).toBe(true);
  });
});

/* ---- 3. the draft is seeded from the echo -------------------------------- */

describe('3. seeded draft', () => {
  it('opens on the stored piper engine with only its field and the catalogue fetched', async () => {
    const backend = await renderLoaded({ settings: { 'tts.default_engine': 'piper' } });
    // The stored voice is the baseline once the catalogue has landed, not a draft.
    await vi.waitFor(() =>
      expect(selectTrigger('Piper voice').textContent).toContain('en_GB-alan-low')
    );

    expect(selectTrigger('Default engine').textContent).toContain('Piper on this device');
    expect(pathCalls(backend.calls, '/api/tts/voices')).toHaveLength(1);
    expect(hasInputLabel('Home Assistant engine id')).toBe(false);
    expect(hasInputLabel('Voice')).toBe(false);
    expect(hasInputLabel('Server URL')).toBe(false);
    expect(document.querySelector('[data-testid="speech-voices"]')).not.toBeNull();
    // The stored value is the baseline: nothing has changed yet.
    expect(saveButton().disabled).toBe(true);
  });

  it('never asks for voices while the stored engine is ha_tts', async () => {
    const backend = await renderLoaded({ settings: { 'tts.default_engine': 'ha_tts' } });
    await vi.waitFor(() => expect(inputByLabel('Home Assistant engine id').value).toBe('tts.piper'));

    expect(inputByLabel('Language').value).toBe('en_GB');
    // The voices route is the Piper catalogue only — no other engine downloads models.
    expect(pathCalls(backend.calls, '/api/tts/voices')).toHaveLength(0);
    expect(document.querySelector('[data-testid="speech-voices"]')).toBeNull();
    expect(saveButton().disabled).toBe(true);
  });
});

/* ---- 4. the D27 off round-trip ------------------------------------------- */

describe('4. D27 off round-trip', () => {
  it('drafts the off state, patches none and adopts the echo as its baseline', async () => {
    const backend = await renderLoaded();
    await vi.waitFor(() =>
      expect(document.querySelector('[data-testid="speech-engines"]')).not.toBeNull()
    );

    chooseOption('Default engine', 'none');

    expect(selectTrigger('Default engine').textContent).toContain('No speech — sound files only');
    expect(document.querySelector('[data-testid="speech-engines"]')).toBeNull();
    expect(document.querySelector('[data-testid="speech-voices"]')).toBeNull();
    expect(hasInputLabel('Home Assistant engine id')).toBe(false);

    expect(document.querySelector('[data-testid="speech-off"]')?.textContent).toContain(
      'Speech is off'
    );
    expect(pageText()).toContain(
      'No speech — sound files only. Sound-file chimes remain available.'
    );
    // Only the STORED value is a health state: the resolver learns about `none` at
    // startup, so an unsaved draft is not something health can report yet.
    expect(pageText()).not.toContain('Health: disabled');

    expect(saveButton().disabled).toBe(false);
    saveButton().click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));

    // Only the changed key travels, and the whole body is checked.
    expect(patchBody(patches(backend.calls)[0])).toEqual({ 'tts.default_engine': 'none' });

    await vi.waitFor(() => expect(saveButton().disabled).toBe(true));
    expect(pageText()).toContain('Health: disabled — this is not a degraded status.');

    // D27 is a configuration, not a fault: one action, no dismiss and no second path.
    const off = document.querySelector<HTMLElement>('[data-testid="speech-off"]');
    expect(off?.querySelectorAll('button')).toHaveLength(1);
    expect(buttonByText('Enable speech')).toBeTruthy();
    expect(pageText()).not.toContain('Retry');
  });
});

/* ---- 5. Enable speech is one tap ----------------------------------------- */

describe('5. Enable speech is one tap', () => {
  it('patches the default engine back on without a second confirmation', async () => {
    const backend = await renderLoaded({ settings: { 'tts.default_engine': 'none' } });

    expect(pageText()).toContain('Speech is off');
    expect(pageText()).toContain('Health: disabled — this is not a degraded status.');
    expect(saveButton().disabled).toBe(true);

    buttonByText('Enable speech').click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));

    expect(patchBody(patches(backend.calls)[0])).toEqual({ 'tts.default_engine': 'ha_tts' });
    await vi.waitFor(() =>
      expect(toastStore.items.map((item) => item.message)).toContain('Speech settings saved.')
    );
    await vi.waitFor(() => expect(inputByLabel('Home Assistant engine id')).toBeTruthy());

    expect(document.querySelector('[data-testid="speech-off"]')).toBeNull();
    expect(pageText()).not.toContain('Health: disabled');
    expect(saveButton().disabled).toBe(true);
  });
});

/* ---- 6. per-engine persistence ------------------------------------------- */

describe('6. per-engine persistence', () => {
  it('patches only the two Home Assistant keys the draft changed', async () => {
    const backend = await renderLoaded();

    type(inputByLabel('Home Assistant engine id'), 'tts.cloud_say');
    type(inputByLabel('Language'), 'fr-FR');
    saveButton().click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));

    expect(patchBody(patches(backend.calls)[0])).toEqual({
      'tts.ha.engine_id': 'tts.cloud_say',
      'tts.ha.language': 'fr-FR'
    });
  });

  it('patches the espeak voice and speed with the count left a number', async () => {
    // Stored, not drafted: the engine key must not travel with these two.
    const backend = await renderLoaded({ settings: { 'tts.default_engine': 'espeak_ng' } });

    type(inputByLabel('Voice'), 'fr-fr');
    buttonByAriaLabel('Increase Speed').click();
    flushSync();
    expect(pageText()).toContain('161 wpm');

    saveButton().click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));

    const body = patchBody(patches(backend.calls)[0]);
    expect(body).toEqual({ 'tts.espeak_voice': 'fr-fr', 'tts.espeak_speed': 161 });
    // A string here is rejected by the registry: the assertion is the type.
    expect(typeof body['tts.espeak_speed']).toBe('number');
  });

  it('patches the remote URL and timeout and never a token key', async () => {
    const backend = await renderLoaded({ settings: { 'tts.default_engine': 'remote_tts' } });

    type(inputByLabel('Server URL'), 'http://renderer.local:5002');
    buttonByAriaLabel('Increase Timeout').click();
    flushSync();

    saveButton().click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));

    const body = patchBody(patches(backend.calls)[0]);
    expect(body).toEqual({
      'tts.remote_tts.url': 'http://renderer.local:5002',
      'tts.remote_tts.timeout_s': 21
    });
    // Nothing typed in the token field, so the token key must not travel at all.
    expect(Object.keys(body)).not.toContain('tts.remote_tts.token');
  });

  it('sends a typed token and never renders the stored one', async () => {
    const backend = await renderLoaded({ secrets: { 'tts.remote_tts.token': true } });
    chooseOption('Default engine', 'remote_tts');

    const token = inputByLabel('Token');
    // Secrets are write-only: the field starts empty on every mount, and the store holds
    // the flag rather than the value, so the stored token can never be on the page.
    expect(token.value).toBe('');
    expect(token.type).toBe('password');
    expect(pageText()).not.toContain(STORED_TOKEN);
    expect(pageText()).toContain('A token is stored. Type a new one to replace it.');

    // The reveal control exists and flips its own wording — it never prints the value.
    buttonByAriaLabel('Show value').click();
    flushSync();
    expect(buttonByAriaLabel('Hide value')).toBeTruthy();
    expect(token.type).toBe('text');
    expect(pageText()).not.toContain(STORED_TOKEN);

    type(token, 'fresh-renderer-token');
    saveButton().click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));

    expect(patchBody(patches(backend.calls)[0])).toEqual({
      'tts.default_engine': 'remote_tts',
      'tts.remote_tts.token': 'fresh-renderer-token'
    });
  });

  it('clears a stored token with null and never with an empty string', async () => {
    const backend = await renderLoaded({ secrets: { 'tts.remote_tts.token': true } });
    chooseOption('Default engine', 'remote_tts');

    expect(pageText()).not.toContain(STORED_TOKEN);
    buttonByText('Clear stored token').click();
    flushSync();
    expect(pageText()).toContain('The stored token will be cleared when you save.');

    saveButton().click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));

    const body = patchBody(patches(backend.calls)[0]);
    expect(body).toEqual({ 'tts.default_engine': 'remote_tts', 'tts.remote_tts.token': null });
    // `''` is what the backend rejects, so the clear is spelled `null` and nothing else.
    expect(body['tts.remote_tts.token']).not.toBe('');
  });

  it('offers no Clear control when nothing is stored', async () => {
    await renderLoaded();
    chooseOption('Default engine', 'remote_tts');

    expect(pageText()).toContain('No token stored.');
    expect(buttonsMatching('Clear stored token')).toHaveLength(0);
  });
});

/* ---- 7. engine availability ---------------------------------------------- */

describe('7. engine availability', () => {
  it('lists every engine in the response order with its own sentence and last time', async () => {
    await renderLoaded();
    await vi.waitFor(() => expect(document.querySelectorAll('.engine-row')).toHaveLength(4));

    const rows = [...document.querySelectorAll<HTMLElement>('.engine-row')];
    expect(rows.map((row) => row.querySelector('.engine-name')?.textContent?.trim())).toEqual([
      'ha_tts',
      'piper',
      'espeak_ng',
      'remote_tts'
    ]);
    expect(rows.map((row) => row.querySelector('.badge')?.textContent?.trim())).toEqual([
      'available',
      'unavailable',
      'unavailable',
      'unavailable'
    ]);
    // A measured time, and an em dash for an engine that has not spoken yet.
    expect(rows.map((row) => row.querySelector('.engine-last')?.textContent?.trim())).toEqual([
      'last: 18 ms',
      'last: —',
      'last: —',
      'last: —'
    ]);

    // The server's own whole sentence, verbatim; an available engine gets none.
    expect(rows[1].querySelector('.engine-reason')?.textContent).toBe(PIPER_REASON);
    expect(rows[2].querySelector('.engine-reason')?.textContent).toBe(ESPEAK_REASON);
    expect(rows[3].querySelector('.engine-reason')?.textContent).toBe(REMOTE_REASON);
    expect(rows[0].querySelector('.engine-reason')).toBeNull();
  });

  it('renders the response warning verbatim above the rows', async () => {
    await renderLoaded({
      engines: { ...DEFAULT_ENGINES, warning: 'the remote renderer did not answer' }
    });
    await vi.waitFor(() => expect(pageText()).toContain('the remote renderer did not answer'));

    expect(pageText()).toContain('warn');
    expect(document.querySelectorAll('.engine-row')).toHaveLength(4);
  });

  it('reports an empty list without inventing a reason', async () => {
    await renderLoaded({ engines: { disabled: false, engines: [], warning: null } });
    await vi.waitFor(() => expect(pageText()).toContain('No engines reported'));

    expect(document.querySelectorAll('.engine-row')).toHaveLength(0);
    expect(document.querySelectorAll('.engine-reason')).toHaveLength(0);
    expect(pageText()).toContain(
      'The response contains an empty engines list and no explanation.'
    );
    expect(pageText()).not.toContain('not installed');
  });

  it('shows a failed engine list verbatim and Retry refetches it', async () => {
    const sentence = 'the speech engines could not be listed';
    const backend = await renderLoaded({ engineError: sentence });
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toContain(sentence)
    );
    expect(document.querySelectorAll('.engine-row')).toHaveLength(0);

    buttonByText('Retry').click();
    await vi.waitFor(() => expect(pathCalls(backend.calls, '/api/tts/engines')).toHaveLength(2));
    // Retry clears the sentence while the second request is in flight…
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toContain(sentence)
    );
  });

  it('shows a failed catalogue verbatim with its own Retry', async () => {
    const sentence = 'the Piper voices could not be listed';
    const backend = await renderLoaded({
      settings: { 'tts.default_engine': 'piper' },
      voicesError: sentence
    });
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toContain(sentence)
    );

    buttonByText('Retry').click();
    await vi.waitFor(() => expect(pathCalls(backend.calls, '/api/tts/voices')).toHaveLength(2));
  });
});

/* ---- 8. a small board never gets an offer -------------------------------- */

describe('8. small board never gets an offer', () => {
  it('keeps both sentences and disables every download button', async () => {
    const backend = await renderLoaded({ settings: { 'tts.default_engine': 'piper' } });
    await vi.waitFor(() => expect(document.querySelectorAll('.voice-row')).toHaveLength(3));

    // The RAM sentence stands beside the refusal: they answer different questions.
    expect(pageText()).toContain(MEMORY_WARNING);
    expect(pageText()).toContain(UNAVAILABLE_REASON);

    const downloads = buttonsMatching('Download ');
    expect(downloads).toHaveLength(3);
    for (const button of downloads) {
      expect(button.disabled).toBe(true);
    }

    downloads[0].click();
    flushSync();
    await Promise.resolve();
    expect(downloadStarts(backend.calls)).toHaveLength(0);
  });
});

/* ---- 9. download progress ------------------------------------------------ */

describe('9. download progress', () => {
  it('starts one job, words its progress and refetches the catalogue when it finishes', async () => {
    const backend = await renderLoaded({
      settings: { 'tts.default_engine': 'piper' },
      voices: { ...DEFAULT_VOICES, can_download: true, unavailable_reason: null },
      downloadStates: [
        voiceDownloadRunning(12 * MIB, 60 * MIB),
        voiceDownloadRunning(30 * MIB, 60 * MIB),
        voiceDownloadTerminal('done')
      ]
    });
    await vi.waitFor(() => expect(buttonByText('Download en_GB-alan-low').disabled).toBe(false));

    // Only the poll's sleep is faked, and only once the initial fetches have settled:
    // `vi.waitFor` polls with a timer, so it can never be used from here on.
    vi.useFakeTimers({ toFake: ['setTimeout'] });
    buttonByText('Download en_GB-alan-low').click();
    await flushMicrotasks();

    const starts = downloadStarts(backend.calls);
    expect(starts).toHaveLength(1);
    expect(starts[0].path).toBe('/api/tts/voices/en_GB-alan-low/download');
    expect(starts[0].init?.method).toBe('POST');

    const live = (): string =>
      document.querySelector('[aria-live="polite"]')?.textContent ?? '';
    expect(live()).toContain('en_GB-alan-low');
    expect(live()).toContain('12.0 MiB of 60.0 MiB');
    expect(downloadPolls(backend.calls)).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(DOWNLOAD_POLL_INTERVAL_MS);
    flushSync();
    expect(live()).toContain('30.0 MiB of 60.0 MiB');
    expect(downloadPolls(backend.calls)).toHaveLength(2);

    await vi.advanceTimersByTimeAsync(DOWNLOAD_POLL_INTERVAL_MS);
    await flushMicrotasks();

    expect(downloadPolls(backend.calls)).toHaveLength(3);
    expect(toastStore.items.map((item) => item.message)).toContain('Voice installed.');
    // The install changed what is on disk, so the catalogue is re-read for its badge.
    expect(pathCalls(backend.calls, '/api/tts/voices')).toHaveLength(2);
    expect(document.querySelector('[aria-live="polite"]')).toBeNull();
    vi.useRealTimers();
  });
});

/* ---- 10. a job that ends in failure -------------------------------------- */

describe('10. download refused by the job', () => {
  it('shows the job failure verbatim and claims no success', async () => {
    const sentence = 'the connection closed before the model finished transferring';
    const backend = await renderLoaded({
      settings: { 'tts.default_engine': 'piper' },
      voices: { ...DEFAULT_VOICES, can_download: true, unavailable_reason: null },
      downloadStates: [voiceDownloadTerminal('failed', sentence)]
    });
    await vi.waitFor(() => expect(buttonByText('Download en_GB-alan-low').disabled).toBe(false));

    buttonByText('Download en_GB-alan-low').click();
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toContain(sentence)
    );

    expect(toastStore.items.map((item) => item.message)).not.toContain('Voice installed.');
    expect(toastStore.items.map((item) => item.message)).not.toContain(
      'Speech settings saved.'
    );
    // A failed install does not move the catalogue.
    expect(pathCalls(backend.calls, '/api/tts/voices')).toHaveLength(1);
  });
});

/* ---- 11. a refused start ------------------------------------------------- */

describe('11. download start refused', () => {
  it('shows the 422 sentence verbatim', async () => {
    const message = 'speech is switched off, so no voice model can be installed';
    const backend = await renderLoaded({
      settings: { 'tts.default_engine': 'piper' },
      voices: { ...DEFAULT_VOICES, can_download: true, unavailable_reason: null },
      downloadError: {
        status: 422,
        body: { error: { code: 'voice_unavailable', message, detail: null } }
      }
    });
    await vi.waitFor(() => expect(buttonByText('Download en_GB-alan-low').disabled).toBe(false));

    buttonByText('Download en_GB-alan-low').click();
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toContain(message)
    );

    expect(downloadPolls(backend.calls)).toHaveLength(0);
    expect(toastStore.items.map((item) => item.message)).not.toContain('Voice installed.');
  });
});

/* ---- 12. the poll is bounded --------------------------------------------- */

describe('12. the poll is bounded', () => {
  it('stops after DOWNLOAD_POLL_MAX_ATTEMPTS and says the download continues', async () => {
    const backend = await renderLoaded({
      settings: { 'tts.default_engine': 'piper' },
      voices: { ...DEFAULT_VOICES, can_download: true, unavailable_reason: null },
      downloadStates: [voiceDownloadRunning(MIB, 60 * MIB)]
    });
    await vi.waitFor(() => expect(buttonByText('Download en_GB-alan-low').disabled).toBe(false));

    vi.useFakeTimers({ toFake: ['setTimeout'] });
    buttonByText('Download en_GB-alan-low').click();
    await flushMicrotasks();
    expect(downloadPolls(backend.calls)).toHaveLength(1);

    // Two extra steps: the ceiling must hold even one tick past the last attempt.
    for (let step = 0; step < DOWNLOAD_POLL_MAX_ATTEMPTS + 2; step += 1) {
      await vi.advanceTimersByTimeAsync(DOWNLOAD_POLL_INTERVAL_MS);
    }
    flushSync();

    // Exactly the ceiling — this count *is* the proof that no poll outlives it.
    expect(downloadPolls(backend.calls)).toHaveLength(DOWNLOAD_POLL_MAX_ATTEMPTS);
    expect(pageText()).toContain(
      'This panel stopped checking after 2 minutes. The download continues on the device — reload to see the result.'
    );
    vi.useRealTimers();
  });
});

/* ---- 13. registry keys only ---------------------------------------------- */

describe('13. registry keys only', () => {
  it('writes every speech setting key it owns and keeps renderer settings absent', async () => {
    const allowed = [
      'tts.default_engine',
      'tts.ha.engine_id',
      'tts.ha.language',
      'tts.piper.voice',
      'tts.espeak_voice',
      'tts.espeak_speed',
      'tts.remote_tts.url',
      'tts.remote_tts.token',
      'tts.remote_tts.timeout_s',
      'tts.http_tts.provider',
      'tts.allow_dynamic_paid'
    ];
    const backend = await renderLoaded({ providers: providerEnvelope() });
    await vi.waitFor(() =>
      expect(document.querySelector('[data-testid="speech-providers"] .provider-record .badge')?.textContent).toContain('paid')
    );

    // One draft touches every engine branch; the shared paid-policy toggle saves its own key.
    type(inputByLabel('Home Assistant engine id'), 'tts.cloud_say');
    type(inputByLabel('Language'), 'fr-FR');

    chooseOption('Default engine', 'piper');
    await vi.waitFor(() => expect(radioValues()).toContain('en_US-lessac-medium'));
    chooseOption('Piper voice', 'en_US-lessac-medium');

    chooseOption('Default engine', 'espeak_ng');
    type(inputByLabel('Voice'), 'fr-fr');
    buttonByAriaLabel('Increase Speed').click();
    flushSync();

    chooseOption('Default engine', 'remote_tts');
    type(inputByLabel('Server URL'), 'http://renderer.local:5002');
    type(inputByLabel('Token'), 'token-1');
    buttonByAriaLabel('Increase Timeout').click();
    flushSync();

    const dynamicToggle = document.querySelector<HTMLButtonElement>(
      '[data-testid="speech-providers"] [role="switch"]'
    );
    expect(dynamicToggle).not.toBeNull();
    dynamicToggle?.click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));
    expect(patchBody(patches(backend.calls)[0])).toEqual({ 'tts.allow_dynamic_paid': true });

    chooseOption('Default engine', 'http_tts');
    await vi.waitFor(() => expect(selectTrigger('Provider')).toBeTruthy());
    chooseOption('Provider', PAID_PROVIDER.id);

    saveButton().click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(2));

    const written = patches(backend.calls).flatMap((call) => Object.keys(patchBody(call)));
    expect(written.length).toBeGreaterThan(0);
    for (const key of written) {
      expect(allowed).toContain(key);
    }
    expect([...written].sort()).toEqual([...allowed].sort());

    // HSD-31dk's paid-marker deferral is lifted by HSD-s856: the real section,
    // paid fixture badge and per-provider Test control are now present.
    expect(document.querySelector('[data-testid="speech-providers"]')).not.toBeNull();
    expect(buttonByText('Add provider')).toBeTruthy();
    expect(document.querySelector('[data-testid="speech-providers"] .provider-record .badge')?.textContent).toContain('paid');
    expect(buttonByText('Test')).toBeTruthy();
    const words = pageText().toLowerCase();
    expect(words).not.toContain('tts.renderer.token');
  });
});

/* ---- 14. the http_tts provider setting ----------------------------------- */

describe('14. the http_tts provider branch', () => {
  it('patches the selected provider id and shows its paid badge beside the picker', async () => {
    const backend = await renderLoaded({
      settings: { 'tts.default_engine': 'http_tts', 'tts.http_tts.provider': '' },
      providers: providerEnvelope()
    });
    await vi.waitFor(() => expect(selectTrigger('Provider')).toBeTruthy());

    chooseOption('Provider', PAID_PROVIDER.id);
    expect(selectTrigger('Provider').textContent).toContain(PAID_PROVIDER.name);
    const picker = document.querySelector('.provider-picker');
    expect(picker?.querySelector('.badge')?.textContent).toContain('paid');

    saveButton().click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));
    expect(patchBody(patches(backend.calls)[0])).toEqual({
      'tts.http_tts.provider': PAID_PROVIDER.id
    });
  });

  it('points to provider setup instead of rendering a dead picker when the list is empty', async () => {
    await renderLoaded({
      settings: { 'tts.default_engine': 'http_tts' },
      providers: providerEnvelope([])
    });
    await vi.waitFor(() =>
      expect(pageText()).toContain('No external provider is configured yet.')
    );

    expect(pageText()).toContain('Add one in the External TTS providers section below.');
    expect(document.querySelector('.provider-picker')).toBeNull();
    expect(document.querySelectorAll('button.trigger')).toHaveLength(1);
  });
});

/* ---- 15. speech.ts is pure and mirrors the registry ---------------------- */

describe('15. speech.ts rules', () => {
  it('offers the six engines in registry order with the D27 off row last', () => {
    expect(DEFAULT_ENGINE_OPTIONS.map((option) => option.value)).toEqual([
      'ha_tts',
      'piper',
      'remote_tts',
      'espeak_ng',
      'http_tts',
      'none'
    ]);
    expect(DEFAULT_ENGINE_OPTIONS.at(-1)?.label).toBe('No speech — sound files only');
    expect(DEFAULT_ENGINE).toBe('ha_tts');
    expect(SPEECH_OFF).toBe('none');
  });

  it('words a measured synthesis time, or an em dash when there is none', () => {
    expect(formatSynthesisMs(null)).toBe('—');
    expect(formatSynthesisMs(18)).toBe('18 ms');
  });

  it('converts bytes to the MiB the server itself reports', () => {
    expect(formatProgress(0, null)).toEqual({ label: '0.0 MiB', percent: 0 });
    expect(formatProgress(12.6 * MIB, 60 * MIB)).toEqual({
      label: '12.6 MiB of 60.0 MiB',
      percent: 21
    });
    // A bar can never draw past its track, whatever the server reports.
    expect(formatProgress(200, 100).percent).toBe(100);
  });

  it('knows which states are terminal and words them readably', () => {
    expect(isDownloadFinished('done')).toBe(true);
    expect(isDownloadFinished('failed')).toBe(true);
    expect(isDownloadFinished('rejected')).toBe(true);
    expect(isDownloadFinished('running')).toBe(false);
    expect(isDownloadFinished('queued')).toBe(false);
    expect(downloadStateLabel('running')).toBe('Downloading');
    // An unknown state is never silently relabelled.
    expect(downloadStateLabel('mystery')).toBe('mystery');
  });
});
