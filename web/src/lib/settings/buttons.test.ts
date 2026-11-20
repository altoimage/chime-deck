// @vitest-environment happy-dom
/**
 * Acceptance tests for the chime-buttons section (HSD-wnkm): the button
 * editor (`ButtonEditor.svelte`), its shared Test result block
 * (`ButtonPressResult.svelte`) and the buttons list (`ButtonsPanel.svelte`).
 *
 * The describes are numbered one per acceptance criterion. Each `it` proves a
 * semantic fact — the word on screen, the exact key in a request body, the exact
 * number of requests — never layout: a DOM has no CSS engine, so sizing stays a
 * fingerprint test on the real panel.
 *
 * What is proven here and what is not:
 *
 *   - Proven: the client-side and server-side halves of "a sound button needs a
 *     sound" (the server's own rejection is proven by
 *     `tests/test_api_buttons.py::test_sound_button_without_a_sound_is_a_field_specific_422`;
 *     this file only proves the UI renders that sentence verbatim and sends the
 *     request exactly once), the action-type swap, the Test result's wording and
 *     measured latency, the pre-save slug warning and its PATCH, the derived MQTT
 *     line for enabled/disabled/unusable-broker rows, D27 (speech off), D22
 *     (unavailable engines cannot be chosen) and D26 (field conventions, no
 *     native number input, no pre-render control that cannot work).
 *   - Not proven: any real synthesis, playback, broker publish or server
 *     validation. Every request is answered by the in-memory fake below through
 *     `setFetchImpl`, so nothing in this file touches the network, a real timer
 *     or `window.location`.
 *
 * `ButtonPressResult` measures the round trip itself (`performance.now()` around
 * the request); AC3b stubs the clock so the rendered number is deterministic
 * where the environment allows it.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  setFetchImpl,
  type ButtonDetail,
  type FetchLike,
  type FetchResponse,
  type PressResponse,
  type SoundRow,
  type TtsEnginesResponse
} from '../api.js';
import { settings } from '../settings.svelte.js';
import { engineChoices } from '../tts-engines.js';
import { toastStore } from '../ui/toast.svelte.js';
import ButtonEditor from './ButtonEditor.svelte';
import ButtonsPanel from './ButtonsPanel.svelte';

/* ---- fixtures ----------------------------------------------------------- */

/** A complete stored `GET /api/buttons` row, with every field the API returns. */
function buttonRow(overrides: Partial<ButtonDetail> = {}): ButtonDetail {
  return {
    id: 1,
    slug: 'front-door',
    name: 'Front Door',
    icon: null,
    action_type: 'sound',
    sound_id: 1,
    tts_text: null,
    tts_engine: null,
    volume_override: null,
    dedupe_seconds: null,
    show_on_dashboard: true,
    quiet_hours_exempt: false,
    enabled: true,
    sort_order: 1,
    created_at: '2026-11-12T00:00:00Z',
    updated_at: '2026-11-12T00:00:00Z',
    mqtt: {
      command: 'chime_deck/default/button/front-door/set',
      discovery: 'homeassistant/button/chime_deck_default_front-door/config'
    },
    slug_change: null,
    ...overrides
  };
}

/** A complete `GET /api/sounds` row. */
function soundRow(overrides: Partial<SoundRow> = {}): SoundRow {
  return {
    id: 1,
    name: 'Ding dong',
    source_type: 'upload',
    path: 'data/sounds/ding-dong.mp3',
    url: null,
    sha256: null,
    format: 'mp3',
    duration_ms: 1200,
    bytes: 24000,
    missing: false,
    created_at: '2026-11-12T00:00:00Z',
    ...overrides
  };
}

/** The engine list AC7 pins: piper unavailable with the server's own reason. */
function engineFixture(): TtsEnginesResponse {
  return {
    disabled: false,
    warning: null,
    engines: [
      { name: 'ha_tts', available: true, reason: null, last_synthesis_ms: 120 },
      {
        name: 'piper',
        available: false,
        reason: 'Piper needs 420 MiB of RAM; this device has 180 MiB.',
        last_synthesis_ms: null
      },
      { name: 'espeak_ng', available: true, reason: null, last_synthesis_ms: null }
    ]
  };
}

/** A stored speech button: the row every TTS-side test starts from. */
function ttsRow(overrides: Partial<ButtonDetail> = {}): ButtonDetail {
  return buttonRow({
    id: 1,
    action_type: 'tts',
    sound_id: null,
    tts_text: 'Someone is at the door',
    tts_engine: null,
    ...overrides
  });
}

/*
 * The bake keys are opt-in: `buttonRow`/`ttsRow` carry none of them on purpose, so every
 * test written before the bake API keeps describing an older server build. Only these three
 * helpers add the keys a build with the bake contract sends.
 */

/** A stored speech row whose bake keys report a finished pre-render. */
function bakedRow(overrides: Partial<ButtonDetail> = {}): ButtonDetail {
  return ttsRow({
    baked_sha: 'a3f1c9d2',
    baked_format: 'mp3',
    baked_at: '2026-11-12T00:00:03Z',
    baked_error: null,
    ...overrides
  });
}

/** A stored speech row that carries the bake keys with nothing baked yet. */
function plainBakeRow(overrides: Partial<ButtonDetail> = {}): ButtonDetail {
  return ttsRow({
    baked_sha: null,
    baked_format: null,
    baked_at: null,
    baked_error: null,
    ...overrides
  });
}

/** A stored speech row whose last bake failed with the engine's own sentence. */
function failedRow(reason: string, overrides: Partial<ButtonDetail> = {}): ButtonDetail {
  return ttsRow({
    baked_sha: null,
    baked_format: null,
    baked_at: null,
    baked_error: reason,
    ...overrides
  });
}

/** The sentence the fake merges into `slug_change.warning` after a rename. */
function renameWarning(oldSlug: string, newSlug: string): string {
  return `The Home Assistant entity moved from ${oldSlug} to ${newSlug}; update any automation that referenced the old one.`;
}

/* ---- fake backend ------------------------------------------------------- */

interface FakeCall {
  path: string;
  init?: RequestInit;
}

interface BackendOptions {
  buttons?: ButtonDetail[];
  sounds?: SoundRow[];
  engines?: TtsEnginesResponse;
  settings?: Record<string, unknown>;
  press?: PressResponse;
  bakeError?: BakeError | null;
  /** When true, the FIRST PATCH is refused as a paid-template 422 and the next is served. */
  paidRefusalOnce?: boolean;
}

/** A refusal the fake bake route answers instead of baking, with the server's exact body. */
interface BakeError {
  status: number;
  message: string;
  detail?: unknown;
}

const DEFAULT_PRESS: PressResponse = {
  button_id: 1,
  button_name: 'Front Door',
  source: 'ui_test',
  outcome: 'queued',
  pressed_at: '2026-11-12T00:00:01Z',
  code: null,
  detail: 'engine ha_tts',
  synthesis_ms: null
};

/** The sentence the paid-template refusal carries (`api/buttons.py::_validate_save`). */
const PAID_TEMPLATE_REFUSAL =
  'the selected paid provider cannot save a templated phrase without confirmation';

/**
 * The `detail` that refusal carries. The engine is the one the route resolved, which for a
 * button with no override is this fake's default — the fixture's own `ha_tts`.
 */
const PAID_TEMPLATE_DETAIL = {
  field: 'tts_text',
  engine: 'ha_tts',
  requires: 'acknowledge_paid_template'
};

/** The paid-template 422 body, in the envelope every refusal in this app uses. */
function paidTemplateResponse(): FetchResponse {
  return jsonResponse(
    {
      error: {
        code: 'paid_template_confirmation_required',
        message: PAID_TEMPLATE_REFUSAL,
        detail: PAID_TEMPLATE_DETAIL
      }
    },
    422
  );
}

interface Backend {
  calls: FakeCall[];
  /** The mutable server truth: `GET /api/buttons` answers it, PATCH mutates it. */
  buttons: ButtonDetail[];
  sounds: SoundRow[];
  engines: TtsEnginesResponse;
  settings: Record<string, unknown>;
  /** What `POST /api/buttons/{id}/press` answers. */
  press: PressResponse;
  /** When set, `GET /api/buttons` answers 500 with this sentence. */
  listError: string | null;
  /** When set, `GET /api/buttons` waits on it before answering (a pending list). */
  listGate: Promise<void> | null;
  /** When set, `POST /api/buttons` answers 422 with this sentence. */
  createError: string | null;
  /** When set, `PATCH /api/buttons/{id}` answers 422 with this sentence. */
  patchError: string | null;
  /** While true, the next `PATCH /api/buttons/{id}` is the paid-template 422, then it clears. */
  pendingPaidRefusal: boolean;
  /** When set, `POST /api/buttons/{id}/bake` refuses with these words instead of baking. */
  bakeError: BakeError | null;
}

function jsonResponse(body: unknown, status = 200): FetchResponse {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function errorResponse(status: number, message: string): FetchResponse {
  return jsonResponse({ error: { code: 'test_error', message, detail: null } }, status);
}

/**
 * One in-memory backend. `GET /api/buttons` answers a copy of the stored rows (a
 * component's optimistic edit must not silently become server truth), a PATCH
 * merges the body and echoes the stored row, and a rename echoes the
 * `slug_change` envelope the real route adds.
 */
function fakeBackend(options: BackendOptions = {}): Backend {
  const backend: Backend = {
    calls: [],
    buttons: (options.buttons ?? []).map((row) => ({ ...row })),
    sounds: (options.sounds ?? [soundRow()]).map((row) => ({ ...row })),
    engines: options.engines ?? engineFixture(),
    settings: {
      'tts.default_engine': 'ha_tts',
      'audio.volume': 70,
      'buttons.default_dedupe_seconds': 3,
      ...(options.settings ?? {})
    },
    press: options.press ?? { ...DEFAULT_PRESS },
    listError: null,
    listGate: null,
    createError: null,
    patchError: null,
    pendingPaidRefusal: options.paidRefusalOnce === true,
    bakeError: options.bakeError ?? null
  };

  const impl: FetchLike = async (path, init) => {
    backend.calls.push({ path, init });
    const method = init?.method ?? 'GET';

    if (path === '/api/settings' && method === 'GET') {
      return jsonResponse({ settings: { ...backend.settings }, secrets: {} });
    }

    if (path === '/api/buttons' && method === 'GET') {
      if (backend.listGate !== null) {
        await backend.listGate;
      }
      if (backend.listError !== null) {
        return errorResponse(500, backend.listError);
      }
      return jsonResponse(backend.buttons.map((row) => ({ ...row })));
    }

    if (path === '/api/sounds' && method === 'GET') {
      return jsonResponse(backend.sounds.map((row) => ({ ...row })));
    }

    if (path === '/api/tts/engines' && method === 'GET') {
      return jsonResponse(backend.engines);
    }

    // HSD-po1g: the icon sheet always searches; `allowCustom={false}` means the
    // custom listing is never requested, so these two routes differ on purpose.
    if (path.startsWith('/api/icons/search')) {
      return jsonResponse({
        version: '7.4.47',
        total: 1,
        matched: 1,
        icons: [{ name: 'mdi:doorbell', path: 'M0 0h1v1H0z' }]
      });
    }

    if (path === '/api/icons/custom') {
      return jsonResponse({
        total: 1,
        icons: [{ name: 'cat.svg', reference: 'custom:cat.svg', bytes: 812 }],
        skipped: []
      });
    }

    if (path === '/api/buttons' && method === 'POST') {
      if (backend.createError !== null) {
        return errorResponse(422, backend.createError);
      }
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      const slug = typeof body.slug === 'string' && body.slug !== '' ? body.slug : 'created-button';
      const created = buttonRow({
        id: 9,
        name: String(body.name ?? ''),
        slug,
        action_type: String(body.action_type ?? 'sound'),
        sound_id: typeof body.sound_id === 'number' ? body.sound_id : null,
        tts_text: typeof body.tts_text === 'string' ? body.tts_text : null,
        tts_engine: typeof body.tts_engine === 'string' ? body.tts_engine : null,
        show_on_dashboard: body.show_on_dashboard === true,
        quiet_hours_exempt: body.quiet_hours_exempt === true,
        sort_order: backend.buttons.length + 1,
        mqtt: {
          command: `chime_deck/default/button/${slug}/set`,
          discovery: `homeassistant/button/chime_deck_default_${slug}/config`
        }
      });
      backend.buttons = [...backend.buttons, created];
      return jsonResponse({ ...created }, 201);
    }

    const patchMatch = /^\/api\/buttons\/(\d+)$/.exec(path);
    if (patchMatch !== null && method === 'PATCH') {
      // HSD-s856: the first save is refused, the acknowledged retry is served normally.
      if (backend.pendingPaidRefusal) {
        backend.pendingPaidRefusal = false;
        return paidTemplateResponse();
      }
      if (backend.patchError !== null) {
        return errorResponse(422, backend.patchError);
      }
      const id = Number(patchMatch[1]);
      const index = backend.buttons.findIndex((row) => row.id === id);
      if (index === -1) {
        return errorResponse(404, `there is no button with the id ${id}`);
      }
      const stored = backend.buttons[index];
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      // The acknowledgement is request-scoped, so the route never stores it.
      delete body.acknowledge_paid_template;
      const merged: ButtonDetail = { ...stored, ...(body as Partial<ButtonDetail>) };
      merged.updated_at = '2026-11-12T00:00:02Z';
      merged.slug_change =
        typeof body.slug === 'string'
          ? {
              old_slug: stored.slug,
              new_slug: body.slug,
              old_topics: stored.mqtt,
              new_topics: merged.mqtt,
              warning: renameWarning(stored.slug, body.slug)
            }
          : null;
      backend.buttons[index] = merged;
      return jsonResponse({ ...merged });
    }

    if (patchMatch !== null && method === 'DELETE') {
      const id = Number(patchMatch[1]);
      const found = backend.buttons.find((row) => row.id === id);
      if (found === undefined) {
        return errorResponse(404, `there is no button with the id ${id}`);
      }
      backend.buttons = backend.buttons.filter((row) => row.id !== id);
      return jsonResponse({ deleted: true, id, slug: found.slug });
    }

    const pressMatch = /^\/api\/buttons\/(\d+)\/press$/.exec(path);
    if (pressMatch !== null && method === 'POST') {
      return jsonResponse({ ...backend.press, button_id: Number(pressMatch[1]) });
    }

    // A bake answers the refreshed STORED row and stores it, exactly like the route: the
    // caller therefore sees the new state without a reload and a later list read agrees.
    const bakeMatch = /^\/api\/buttons\/(\d+)\/bake$/.exec(path);
    if (bakeMatch !== null && method === 'POST') {
      if (backend.bakeError !== null) {
        const refusal = backend.bakeError;
        return {
          ok: false,
          status: refusal.status,
          json: async () => ({
            error: { code: 'test_error', message: refusal.message, detail: refusal.detail ?? null }
          })
        };
      }
      const id = Number(bakeMatch[1]);
      const index = backend.buttons.findIndex((row) => row.id === id);
      if (index === -1) {
        return errorResponse(404, `there is no button with the id ${id}`);
      }
      const baked: ButtonDetail = {
        ...backend.buttons[index],
        baked_sha: 'bakedc0ffee',
        baked_format: 'mp3',
        baked_at: '2026-11-12T00:00:04Z',
        baked_error: null
      };
      backend.buttons[index] = baked;
      return jsonResponse({ ...baked });
    }

    throw new Error(`unexpected request ${method} ${path}`);
  };

  setFetchImpl(impl);
  return backend;
}

/* ---- request helpers ---------------------------------------------------- */

function callsTo(calls: FakeCall[], path: string, method = 'GET'): FakeCall[] {
  return calls.filter(
    (call) => call.path === path && (call.init?.method ?? 'GET') === method
  );
}

/** Every PATCH body in order, so a test can assert the diffs exactly. */
function patchBodies(calls: FakeCall[]): Array<Record<string, unknown>> {
  return calls
    .filter((call) => (call.init?.method ?? 'GET') === 'PATCH')
    .map((call) => JSON.parse(String(call.init?.body)) as Record<string, unknown>);
}

function bodyOf(call: FakeCall | undefined): Record<string, unknown> {
  if (call === undefined) {
    throw new Error('expected a request, found none');
  }
  return JSON.parse(String(call.init?.body)) as Record<string, unknown>;
}

/** One deferred, so a test can hold a request open without a real timer. */
function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve: () => void = () => {};
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

/* ---- DOM helpers -------------------------------------------------------- */

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function appendTarget(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  return target;
}

function renderEditor(id: number | null): void {
  mounted.push(
    mount(ButtonEditor, {
      target: appendTarget(),
      props: { id, onback: () => {} }
    })
  );
  flushSync();
}

function renderPanel(navigate: (path: string) => void = () => {}): void {
  mounted.push(mount(ButtonsPanel, { target: appendTarget(), props: { navigate } }));
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

function alertTexts(): string[] {
  return [...document.querySelectorAll<HTMLElement>('[role="alert"]')].map((element) =>
    (element.textContent ?? '').replace(/\s+/g, ' ').trim()
  );
}

/** Whether a button with exactly this visible label exists — no throwing. */
function hasButton(label: string): boolean {
  return [...document.querySelectorAll<HTMLButtonElement>('button')].some(
    (candidate) => (candidate.textContent ?? '').replace(/\s+/g, ' ').trim() === label
  );
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

/** The segmented control's radio for one action, or the picker's for one engine. */
function radio(value: string): HTMLInputElement {
  const input = document.querySelector<HTMLInputElement>(
    `input[type="radio"][value="${value}"]`
  );
  if (input === null) {
    throw new Error(`expected a radio whose value is "${value}"`);
  }
  return input;
}

/**
 * The `Select` trigger inside the field whose visible label is `label`.
 *
 * `Select` renders `<span class="field-label">{label}</span>` beside a
 * `button[aria-haspopup="dialog"]`; the editor's icon trigger has the same
 * `aria-haspopup`, so a test must scope by the label rather than take the first.
 */
function pickerTrigger(label: string): HTMLButtonElement {
  const fieldLabel = [...document.querySelectorAll<HTMLElement>('.field-label')].find(
    (candidate) => (candidate.textContent ?? '').replace(/\s+/g, ' ').trim() === label
  );
  const trigger = fieldLabel?.parentElement?.querySelector<HTMLButtonElement>(
    'button[aria-haspopup="dialog"]'
  );
  if (trigger === undefined || trigger === null) {
    throw new Error(`expected a picker labelled "${label}"`);
  }
  return trigger;
}

function openPicker(label: string): void {
  pickerTrigger(label).click();
  flushSync();
}

/** Click one row of the open picker sheet by the text it shows. */
function chooseOption(text: string): void {
  const row = [...document.querySelectorAll<HTMLLabelElement>('label.row')].find((candidate) =>
    (candidate.textContent ?? '').includes(text)
  );
  if (row === undefined) {
    throw new Error(`expected a picker row showing "${text}"`);
  }
  row.click();
  flushSync();
}

/** The list row (`<li>`) whose heading is `name`. */
function rowFor(name: string): HTMLLIElement {
  const row = [...document.querySelectorAll<HTMLLIElement>('li.row')].find(
    (candidate) => (candidate.querySelector('h3')?.textContent ?? '').trim() === name
  );
  if (row === undefined) {
    throw new Error(`expected a rendered row named "${name}"`);
  }
  return row;
}

/** Type into a field the way a person would: set the value, then fire `input`. */
function typeInto(field: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  field.value = value;
  field.dispatchEvent(new Event('input', { bubbles: true }));
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
});

/* ---- AC1 ---------------------------------------------------------------- */

const SOUND_REQUIRED =
  'a sound button requires a sound_id — choose a sound from the library, or make this button speak';

describe('1. a sound button without a sound is refused, client side and server side', () => {
  it('refuses a create with no sound and sends no request at all', async () => {
    const backend = fakeBackend({ sounds: [soundRow()] });
    await settings.load();
    renderEditor(null);
    await vi.waitFor(() => expect(pageText()).toContain('Sound library'));

    typeInto(inputByLabel('Name'), 'Front Door');
    buttonByText('Create button').click();
    flushSync();

    expect(alertTexts()).toContain(SOUND_REQUIRED);
    // The client rule is not a request that failed later — nothing was sent.
    expect(callsTo(backend.calls, '/api/buttons', 'POST')).toHaveLength(0);
  });

  it('sends exactly one POST once a sound is chosen, then renders the server 422 verbatim', async () => {
    const backend = fakeBackend({ sounds: [soundRow()] });
    // The server uses the same sentence as the client rule, verbatim.
    backend.createError = SOUND_REQUIRED;
    await settings.load();
    renderEditor(null);
    await vi.waitFor(() => expect(pageText()).toContain('Sound library'));

    typeInto(inputByLabel('Name'), 'Front Door');
    openPicker('Sound library');
    expect(document.querySelector('dialog[open]')).not.toBeNull();
    chooseOption('Ding dong');
    // The trigger now names the chosen sound, so the client rule can pass.
    expect(pageText()).toContain('Ding dong');

    buttonByText('Create button').click();
    await vi.waitFor(() =>
      expect(callsTo(backend.calls, '/api/buttons', 'POST')).toHaveLength(1)
    );

    const posts = callsTo(backend.calls, '/api/buttons', 'POST');
    expect(posts).toHaveLength(1);
    expect(bodyOf(posts[0]).sound_id).toBe(1);

    await vi.waitFor(() => expect(alertTexts()).toContain(SOUND_REQUIRED));
  });
});

/* ---- AC2 ---------------------------------------------------------------- */

describe('2. switching the action type swaps the editor', () => {
  it('swaps the sound picker for the phrase and engine fields in both directions', async () => {
    fakeBackend({ buttons: [buttonRow({ id: 1, action_type: 'sound', sound_id: 1 })] });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Sound library'));

    expect(pageText()).toContain('Sound library');
    expect(pageText()).not.toContain('Speech phrase');

    radio('tts').click();
    flushSync();
    expect(pageText()).toContain('Speech phrase');
    expect(pageText()).toContain('Engine override');
    expect(pageText()).not.toContain('Sound library');

    radio('sound').click();
    flushSync();
    expect(pageText()).toContain('Sound library');
    expect(pageText()).not.toContain('Speech phrase');
    expect(pageText()).not.toContain('Engine override');
  });
});

/* ---- AC3 ---------------------------------------------------------------- */

describe('3. Test reports the outcome, the engine or file used, and the latency', () => {
  it('shows the outcome, the engine the press named, and a round-trip latency element', async () => {
    fakeBackend({
      buttons: [
        buttonRow({
          id: 1,
          action_type: 'tts',
          sound_id: null,
          tts_text: 'Someone is at the door',
          tts_engine: null
        })
      ]
    });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Speech phrase'));

    buttonByText('Test').click();
    await vi.waitFor(() => expect(pageText()).toContain('Queued for playback'));

    expect(pageText()).toContain('Engine used');
    expect(pageText()).toContain('ha_tts');
    expect(pageText()).toMatch(/Round-trip latency: \d+ ms/);

    const latency = document.querySelector<HTMLElement>('[data-round-trip]');
    expect(latency).not.toBeNull();
    expect(latency?.getAttribute('data-round-trip')).toMatch(/^\d+(\.\d+)?$/);
  });

  it('measures the latency with performance.now rather than inventing it', async () => {
    fakeBackend({
      buttons: [
        buttonRow({
          id: 1,
          action_type: 'tts',
          sound_id: null,
          tts_text: 'Someone is at the door',
          tts_engine: null
        })
      ]
    });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Speech phrase'));

    // Two calls, in order: `started` then `elapsed`. The click event is built
    // BEFORE the stub because happy-dom stamps every `Event` it constructs with
    // `performance.now()`, which would otherwise swallow the first stubbed
    // value. Dispatching that pre-built event gives testPress exactly the two
    // numbers below.
    const testClick = new MouseEvent('click', { bubbles: true });
    const now = vi.spyOn(performance, 'now');
    now.mockReturnValueOnce(1000);
    now.mockReturnValueOnce(1042);

    buttonByText('Test').dispatchEvent(testClick);
    // No flushSync here: it would have to run between `started` and `elapsed`,
    // and `vi.waitFor` renders the result anyway.
    await vi.waitFor(() => expect(pageText()).toContain('Queued for playback'));

    expect(pageText()).toContain('Round-trip latency: 42 ms');
    expect(document.querySelector('[data-round-trip]')?.getAttribute('data-round-trip')).toBe(
      '42'
    );
  });

  it('names a sound from the library and hedges it as the saved configuration', async () => {
    const detail = 'sound_missing: sound 1 no longer exists';
    fakeBackend({
      buttons: [buttonRow({ id: 1, action_type: 'sound', sound_id: 1 })],
      sounds: [soundRow({ id: 1, name: 'Ding dong' })],
      press: {
        button_id: 1,
        button_name: 'Front Door',
        source: 'ui_test',
        outcome: 'failed',
        pressed_at: '2026-11-12T00:00:01Z',
        code: 'sound_missing',
        detail,
        synthesis_ms: null
      }
    });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Sound library'));

    buttonByText('Test').click();
    await vi.waitFor(() => expect(pageText()).toContain('Failed'));

    expect(pageText()).toContain('Sound used');
    expect(pageText()).toContain('Ding dong');
    // The press response carries no sound name — this value came from the library,
    // so the block must say so.
    expect(pageText()).toContain('(from the saved configuration)');
    expect(pageText()).toContain('Server detail');
    expect(pageText()).toContain(detail);
  });

  it('shows the press result inside the list row it was measured on and nowhere else', async () => {
    fakeBackend({
      buttons: [
        buttonRow({
          id: 1,
          name: 'Front Door',
          action_type: 'tts',
          sound_id: null,
          tts_text: 'Someone is at the door',
          tts_engine: null
        }),
        buttonRow({ id: 2, name: 'Back Door', slug: 'back-door', sort_order: 2 })
      ]
    });
    await settings.load();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Back Door'));

    buttonByLabel('Test Front Door').click();
    await vi.waitFor(() =>
      expect(rowFor('Front Door').textContent).toContain('Queued for playback')
    );

    const front = rowFor('Front Door');
    expect(front.textContent).toContain('Engine used');
    expect(front.textContent).toContain('ha_tts');
    expect(front.textContent).toMatch(/Round-trip latency: \d+ ms/);
    expect(front.querySelector('[data-round-trip]')).not.toBeNull();

    const back = rowFor('Back Door');
    expect(back.textContent).not.toContain('Queued for playback');
    expect(back.textContent).not.toContain('Round-trip latency');
    expect(document.querySelectorAll('[data-round-trip]')).toHaveLength(1);
  });
});

/* ---- AC4 ---------------------------------------------------------------- */

const SLUG_WARNING =
  'Changing the slug creates a NEW Home Assistant entity. The old entity and its topics are removed on the next republish; update any automation that references the old entity.';

describe('4. the slug warning appears before a changed slug is saved', () => {
  it('warns only while the slug differs, then sends exactly the slug and shows the server warning', async () => {
    const backend = fakeBackend({ buttons: [buttonRow({ id: 1, slug: 'front-door' })] });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Slug'));

    // The direction that stops a permanently-shown warning from passing.
    expect(pageText()).not.toContain(SLUG_WARNING);

    typeInto(inputByLabel('Slug'), 'side-door');
    expect(pageText()).toContain(SLUG_WARNING);
    // Nothing is saved just because the slug changed.
    expect(callsTo(backend.calls, '/api/buttons/1', 'PATCH')).toHaveLength(0);

    typeInto(inputByLabel('Slug'), 'front-door');
    expect(pageText()).not.toContain(SLUG_WARNING);

    typeInto(inputByLabel('Slug'), 'side-door');
    buttonByText('Save').click();
    await vi.waitFor(() =>
      expect(callsTo(backend.calls, '/api/buttons/1', 'PATCH')).toHaveLength(1)
    );

    const patches = callsTo(backend.calls, '/api/buttons/1', 'PATCH');
    expect(patches).toHaveLength(1);
    // The diff PATCH carries the rename and nothing else.
    expect(bodyOf(patches[0])).toEqual({ slug: 'side-door' });

    // The server's own sentence about the rename that just happened, verbatim.
    await vi.waitFor(() =>
      expect(pageText()).toContain(renameWarning('front-door', 'side-door'))
    );
  });
});

/* ---- AC5 ---------------------------------------------------------------- */

describe('5. a disabled button is visibly excluded from MQTT and keeps its configuration', () => {
  it('derives the MQTT line from enabled and keeps a disabled row’s slug and action', async () => {
    // `GET /api/buttons` populates `mqtt` for a disabled row too — the exclusion
    // happens at publish time in `mqtt/discovery.py`, not in that route. That is
    // why the disabled fixture below still carries its topics.
    fakeBackend({
      buttons: [
        buttonRow({ id: 1, name: 'Front Door', enabled: true }),
        buttonRow({
          id: 2,
          name: 'Back Door',
          slug: 'back-door',
          sort_order: 2,
          enabled: false,
          mqtt: {
            command: 'chime_deck/default/button/back-door/set',
            discovery: 'homeassistant/button/chime_deck_default_back-door/config'
          }
        })
      ]
    });
    await settings.load();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Back Door'));

    const front = rowFor('Front Door');
    expect(front.textContent).toContain('Announced to Home Assistant');
    expect(front.textContent).toContain('chime_deck/default/button/front-door/set');

    const back = rowFor('Back Door');
    expect(back.textContent).toContain(
      'Not announced to Home Assistant — this button is off; its saved configuration is kept.'
    );
    expect(back.textContent).not.toContain('chime_deck/default/button/back-door/set');
    expect(back.textContent).toContain('back-door');
    expect(back.textContent).toContain('Sound');
  });

  it('switches a row off with one enabled-only PATCH and keeps its configuration', async () => {
    const backend = fakeBackend({ buttons: [buttonRow({ id: 1, name: 'Front Door' })] });
    await settings.load();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));

    const toggle = rowFor('Front Door').querySelector<HTMLButtonElement>('[role="switch"]');
    expect(toggle).not.toBeNull();
    toggle?.click();
    flushSync();

    await vi.waitFor(() =>
      expect(callsTo(backend.calls, '/api/buttons/1', 'PATCH')).toHaveLength(1)
    );
    const patches = callsTo(backend.calls, '/api/buttons/1', 'PATCH');
    expect(patches).toHaveLength(1);
    expect(bodyOf(patches[0])).toEqual({ enabled: false });

    await vi.waitFor(() =>
      expect(rowFor('Front Door').textContent).toContain(
        'Not announced to Home Assistant — this button is off; its saved configuration is kept.'
      )
    );
    const off = rowFor('Front Door');
    expect(off.textContent).toContain('front-door');
    expect(off.textContent).toContain('Sound');
  });

  it('says the broker settings are unusable — the only thing a null mqtt means', async () => {
    fakeBackend({ buttons: [buttonRow({ id: 1, enabled: true, mqtt: null })] });
    await settings.load();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));

    const row = rowFor('Front Door');
    expect(row.textContent).toContain(
      'Not announced to Home Assistant — the MQTT broker settings are unusable.'
    );
    expect(row.textContent).not.toContain('this button is off');
  });
});

/* ---- AC6 ---------------------------------------------------------------- */

describe('6. speech switched off (D27)', () => {
  // The engines route is authoritative for "is speech on" (D27); with the
  // default engine `none` the real backend answers `disabled: true`, so the fake
  // mirrors that pair.
  const OFF_ENGINES: TtsEnginesResponse = { disabled: true, warning: null, engines: [] };
  const OFF_SETTINGS = { 'tts.default_engine': 'none' };
  const REFUSAL =
    "speech is disabled (tts.default_engine is 'none') — set tts.default_engine to a speech engine in Settings, or make this button play a sound";

  it('keeps an existing TTS row visible with the badge and the explanation', async () => {
    fakeBackend({ buttons: [ttsRow()], engines: OFF_ENGINES, settings: OFF_SETTINGS });
    await settings.load();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));

    const row = rowFor('Front Door');
    expect(row.textContent).toContain('Speech is off');
    expect(row.textContent).toContain(
      'Disabled by speech = none. Its speech configuration is kept, never deleted.'
    );
    // Never hidden or deleted.
    expect(document.querySelectorAll('li.row')).toHaveLength(1);
  });

  it('offers no Speech segment in a create and says speech is unavailable', async () => {
    fakeBackend({ engines: OFF_ENGINES, settings: OFF_SETTINGS });
    await settings.load();
    renderEditor(null);
    await vi.waitFor(() =>
      expect(pageText()).toContain('Speech unavailable: speech is switched off.')
    );

    expect(document.querySelector('input[type="radio"][value="tts"]')).toBeNull();
    expect(document.querySelector('input[type="radio"][value="sound"]')).not.toBeNull();
  });

  it('keeps an existing TTS button inert and renders the server refusal verbatim', async () => {
    const backend = fakeBackend({
      buttons: [ttsRow()],
      engines: OFF_ENGINES,
      settings: OFF_SETTINGS
    });
    backend.patchError = REFUSAL;
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Speech — disabled'));

    expect(pageText()).toContain(
      "Speech is switched off (tts.default_engine is none). This button's speech configuration is kept as saved, not deleted."
    );
    expect(buttonByText('Make this button play a sound')).not.toBeNull();

    const phrase = document.querySelector<HTMLTextAreaElement>('textarea');
    expect(phrase).not.toBeNull();
    expect(phrase?.value).toBe('Someone is at the door');
    expect(phrase?.disabled).toBe(true);

    // The save is never blocked client-side for D27; the server's own sentence is
    // what answers it. A name edit is enough to dirty the diff.
    typeInto(inputByLabel('Name'), 'Front Door bell');
    buttonByText('Save').click();
    await vi.waitFor(() =>
      expect(callsTo(backend.calls, '/api/buttons/1', 'PATCH')).toHaveLength(1)
    );
    await vi.waitFor(() => expect(alertTexts()).toContain(REFUSAL));
  });
});

/* ---- AC7 ---------------------------------------------------------------- */

describe('7. an unavailable engine is listed with its reason and cannot be chosen (D22)', () => {
  it('lists piper with its reason and pointer, without a radio, and keeps none selectable', async () => {
    fakeBackend({
      buttons: [
        buttonRow({
          id: 1,
          action_type: 'tts',
          sound_id: null,
          tts_text: 'Someone is at the door',
          tts_engine: null
        })
      ],
      engines: engineFixture()
    });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Speech phrase'));
    await vi.waitFor(() => expect(pageText()).toContain('Engine override'));

    openPicker('Engine override');
    expect(document.querySelector('dialog[open]')).not.toBeNull();

    expect(pageText()).toContain('Piper on this device (piper)');
    expect(pageText()).toContain('Piper needs 420 MiB of RAM; this device has 180 MiB.');
    // The pointer at a working alternative is computed by `engineChoices()` from
    // the same answer and rendered inside the unavailable row (the shared
    // `Select` row prints an unavailable option's description as well as its
    // reason), so a reader is never told "no" without being told what to do.
    expect(pageText()).toContain('available instead: ha_tts, espeak_ng');
    const piper = engineChoices(engineFixture(), 'ha_tts').find(
      (choice) => choice.value === 'piper'
    );
    expect(piper?.disabled).toBe(true);
    expect(piper?.description).toContain('available instead: ha_tts, espeak_ng');
    // An unavailable row is not a control, so it cannot be chosen at all.
    expect(document.querySelector('input[type="radio"][value="piper"]')).toBeNull();
    // D27's off state is first-class, never disabled.
    expect(document.querySelector('input[type="radio"][value="none"]')).not.toBeNull();
  });
});

/* ---- AC8 ---------------------------------------------------------------- */

describe('8. list states and the delete flow', () => {
  it('shows the loading spinner until the list request answers', async () => {
    const backend = fakeBackend({ buttons: [buttonRow()] });
    const gate = deferred();
    backend.listGate = gate.promise;
    await settings.load();

    renderPanel();
    flushSync();
    expect(pageText()).toContain('Loading chime buttons');
    expect(document.querySelectorAll('li.row')).toHaveLength(0);

    gate.resolve();
    backend.listGate = null;
    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));
  });

  it('shows a failing list’s sentence and refetches on Retry', async () => {
    const failure = 'the button list could not be read: the database is locked';
    const backend = fakeBackend({ buttons: [buttonRow()] });
    backend.listError = failure;
    await settings.load();

    renderPanel();
    await vi.waitFor(() => expect(alertTexts()).toContain(failure));
    expect(callsTo(backend.calls, '/api/buttons')).toHaveLength(1);

    backend.listError = null;
    buttonByText('Retry').click();
    flushSync();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));
    expect(callsTo(backend.calls, '/api/buttons')).toHaveLength(2);
    expect(document.querySelector('[role="alert"]')).toBeNull();
  });

  it('offers the empty state and its New button navigates to the add route', async () => {
    fakeBackend({ buttons: [] });
    await settings.load();
    const navigated: string[] = [];

    renderPanel((path) => navigated.push(path));
    await vi.waitFor(() => expect(pageText()).toContain('No chime buttons yet'));

    const empty = document.querySelector('.empty-state');
    const add = [...(empty?.querySelectorAll('button') ?? [])].find(
      (candidate) => (candidate.textContent ?? '').replace(/\s+/g, ' ').trim() === 'New button'
    );
    expect(add).toBeDefined();
    add?.click();
    flushSync();
    expect(navigated).toEqual(['/settings/buttons/add']);
  });

  it('names the button in the delete sheet, removes it, toasts and refetches', async () => {
    const backend = fakeBackend({
      buttons: [
        buttonRow({ id: 1, name: 'Front Door' }),
        buttonRow({ id: 2, name: 'Back Door', slug: 'back-door', sort_order: 2 })
      ]
    });
    await settings.load();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Back Door'));

    buttonByLabel('Delete Front Door').click();
    flushSync();
    expect(pageText()).toContain('Delete “Front Door”?');
    expect(pageText()).toContain("Home Assistant will remove this button's entity.");

    buttonByText('Delete button').click();
    await vi.waitFor(() =>
      expect(callsTo(backend.calls, '/api/buttons/1', 'DELETE')).toHaveLength(1)
    );
    await vi.waitFor(() =>
      expect(toastStore.items.map((toast) => toast.message)).toContain('Button deleted.')
    );

    // The list is refetched and no longer holds the deleted row.
    await vi.waitFor(() => expect(callsTo(backend.calls, '/api/buttons')).toHaveLength(2));
    await vi.waitFor(() => expect(pageText()).not.toContain('Front Door'));
    expect(pageText()).toContain('Back Door');
  });
});

/* ---- AC9 ---------------------------------------------------------------- */

describe('9. D26 field conventions', () => {
  it('keeps autocapitalize/autocorrect/spellcheck/inputmode off on the text fields', async () => {
    fakeBackend({
      buttons: [
        buttonRow({
          id: 1,
          action_type: 'tts',
          sound_id: null,
          tts_text: 'Someone is at the door',
          tts_engine: null
        })
      ]
    });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Speech phrase'));

    for (const label of ['Name', 'Slug']) {
      const input = inputByLabel(label);
      expect(input.getAttribute('autocapitalize'), label).toBe('none');
      expect(input.getAttribute('autocorrect'), label).toBe('off');
      expect(input.getAttribute('spellcheck'), label).toBe('false');
      expect((input.getAttribute('inputmode') ?? '').length, label).toBeGreaterThan(0);
    }

    const textarea = document.querySelector<HTMLTextAreaElement>('textarea');
    expect(textarea).not.toBeNull();
    expect(textarea?.getAttribute('autocapitalize')).toBe('none');
    expect(textarea?.getAttribute('autocorrect')).toBe('off');
    expect(textarea?.getAttribute('spellcheck')).toBe('false');
    // D26 asks for a sensible keyboard on every text field, not only on single
    // line ones: the phrase is prose, so it is a text keyboard.
    expect(textarea?.getAttribute('inputmode')).toBe('text');
  });
});

/* ---- AC10 --------------------------------------------------------------- */

describe('10. the template warning and the overrides', () => {
  const TEMPLATE = "The bell was pressed by {{ states('sensor.presser') }}";
  const TEMPLATE_WARNING =
    'This phrase is rendered on every press — remove the template to pre-render it once.';

  it('warns about a templated phrase, stops on a plain one, and renders no pre-render control without bake keys', async () => {
    fakeBackend({ buttons: [ttsRow()] });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Speech phrase'));

    expect(pageText()).not.toContain('Rendered on every press');

    const phrase = document.querySelector<HTMLTextAreaElement>('textarea');
    expect(phrase).not.toBeNull();
    if (phrase === null) {
      throw new Error('expected the phrase textarea');
    }
    typeInto(phrase, TEMPLATE);
    expect(pageText()).toContain('Rendered on every press');
    expect(pageText()).toContain(TEMPLATE_WARNING);
    // The stored row carries no bake keys, so this is the feature-detection case:
    // no pre-render control may render at all.
    expect(pageText()).not.toContain('Pre-render now');

    typeInto(phrase, 'Someone is at the door');
    expect(pageText()).not.toContain('Rendered on every press');
    expect(pageText()).not.toContain(TEMPLATE_WARNING);
    expect(pageText()).not.toContain('Pre-render now');
  });

  it('sets a volume override, saves the number, clears it and saves an explicit null', async () => {
    const backend = fakeBackend({ buttons: [buttonRow({ id: 1 })] });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Volume'));

    expect(pageText()).toContain('Using the app default (70%).');

    buttonByLabel('Increase Volume').click();
    flushSync();
    expect(pageText()).toContain('Overridden for this button.');
    expect(buttonByText('Use the default')).not.toBeNull();

    buttonByText('Save').click();
    await vi.waitFor(() =>
      expect(callsTo(backend.calls, '/api/buttons/1', 'PATCH')).toHaveLength(1)
    );
    expect(patchBodies(backend.calls)).toEqual([{ volume_override: 71 }]);
    // Wait for the echo to land before touching the draft again.
    await vi.waitFor(() =>
      expect(toastStore.items.map((toast) => toast.message)).toContain('Button saved.')
    );

    buttonByText('Use the default').click();
    flushSync();
    expect(pageText()).toContain('Using the app default (70%).');

    buttonByText('Save').click();
    await vi.waitFor(() =>
      expect(callsTo(backend.calls, '/api/buttons/1', 'PATCH')).toHaveLength(2)
    );
    // Clearing an override is an explicit `null` in the diff, never an omission.
    expect(patchBodies(backend.calls)).toEqual([
      { volume_override: 71 },
      { volume_override: null }
    ]);
  });
});

/* ---- AC11 --------------------------------------------------------------- */

describe('11. a missing sound cannot be assigned from the picker', () => {
  it('lists the missing sound and its reason without a radio, while a healthy sound stays selectable', async () => {
    fakeBackend({
      sounds: [
        soundRow({ id: 1, name: 'Healthy chime', missing: false }),
        soundRow({ id: 2, name: 'Missing chime', missing: true, duration_ms: null })
      ]
    });
    await settings.load();
    renderEditor(null);
    await vi.waitFor(() => expect(pageText()).toContain('Sound library'));

    openPicker('Sound library');
    const missing = [...document.querySelectorAll<HTMLElement>('[aria-disabled="true"]')].find(
      (candidate) => candidate.textContent?.includes('Missing chime')
    );
    expect(missing).toBeDefined();
    expect(missing?.textContent).toContain(
      'This sound is still indexed, but its file is missing.'
    );
    expect(missing?.textContent).toContain(
      'This file is missing from the sound library folder — put it back or rescan before choosing it.'
    );
    expect(missing?.querySelector('input[type="radio"]')).toBeNull();
    expect(document.querySelector('input[type="radio"][value="2"]')).toBeNull();
    expect(document.querySelector('input[type="radio"][value="1"]')).not.toBeNull();
  });
});

/* ---- AC12 --------------------------------------------------------------- */

/** The server's 503 sentence, verbatim (`api/buttons.py::bake_button`). */
const NO_SPEECH_SOURCE = 'no speech source is prepared yet; check the doorbell log';

/** The 422 `dynamic_phrase` sentence the bake route relays from the engine. */
const DYNAMIC_REFUSAL =
  "button 'front-door' speaks a Home Assistant template, which is rendered on every press and cannot be pre-rendered";

/** The editor's own reason for offering no action on a saved template. */
const TEMPLATE_NOT_BAKEABLE =
  "This button's saved phrase is a Home Assistant template, so it is rendered on every press and cannot be pre-rendered. Save a static phrase to pre-render it once.";

describe('12. the pre-render status and action come from the stored row', () => {
  it('reports a baked row with its badge, its format line and the Pre-render now action', async () => {
    const backend = fakeBackend({ buttons: [bakedRow()] });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Speech phrase'));

    expect(pageText()).toContain('Pre-rendered');
    expect(pageText()).toContain('Pre-rendered as mp3 · reused on press');
    expect(hasButton('Pre-render now')).toBe(true);
    // Rendering the stored state must not spend a render: nothing is baked on mount.
    expect(callsTo(backend.calls, '/api/buttons/1/bake', 'POST')).toHaveLength(0);
  });

  it('reports an unbaked row with bake keys and still offers the action', async () => {
    fakeBackend({ buttons: [plainBakeRow()] });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Speech phrase'));

    expect(pageText()).toContain('Not pre-rendered');
    expect(pageText()).not.toContain('Pre-rendered · reused on press');
    expect(hasButton('Pre-render now')).toBe(true);
  });

  it('reports a baked row but offers no bake while speech is off (D27)', async () => {
    // The real backend answers `disabled: true` when the default engine is `none`, so
    // the fake mirrors that pair: `speechDisabled()` reads the route's verdict first.
    fakeBackend({
      buttons: [bakedRow()],
      engines: { disabled: true, warning: null, engines: [] },
      settings: { 'tts.default_engine': 'none' }
    });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Speech — disabled'));

    // What is on disk is information, not a control: the stored state is still reported.
    expect(pageText()).toContain('Pre-rendered as mp3 · reused on press');
    // The server is guaranteed to refuse the bake, so the action is not offered...
    expect(hasButton('Pre-render now')).toBe(false);
    // ...and the reason is already on screen, so nothing claims a template instead.
    expect(pageText()).toContain('Speech is switched off (tts.default_engine is none).');
    expect(pageText()).not.toContain(TEMPLATE_NOT_BAKEABLE);
  });

  it('bakes once and flips the same mounted editor in place without a reload', async () => {
    const backend = fakeBackend({ buttons: [plainBakeRow()] });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Pre-render now'));

    const phrase = document.querySelector<HTMLTextAreaElement>('textarea');
    if (phrase === null) {
      throw new Error('expected the phrase textarea');
    }
    // An unsaved edit belongs to the user, so a bake must not touch it.
    typeInto(phrase, 'Someone is at the side door');

    buttonByText('Pre-render now').click();
    await vi.waitFor(() => expect(pageText()).toContain('Pre-rendered as mp3 · reused on press'));

    expect(callsTo(backend.calls, '/api/buttons/1/bake', 'POST')).toHaveLength(1);
    // The same DOM node, still holding the typed draft: the editor was not remounted.
    const after = document.querySelector<HTMLTextAreaElement>('textarea');
    expect(after).toBe(phrase);
    expect(after?.value).toBe('Someone is at the side door');
    // The answer was the stored row itself, so no second `GET /api/buttons` was needed.
    expect(callsTo(backend.calls, '/api/buttons', 'GET')).toHaveLength(1);
  });

  it('shows a failed bake’s reason verbatim and keeps the action for an explicit retry', async () => {
    fakeBackend({ buttons: [failedRow('espeak-ng exited 1')] });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Speech phrase'));

    expect(pageText()).toContain('failed: espeak-ng exited 1');
    expect(hasButton('Pre-render now')).toBe(true);
  });

  it('replaces the action with the reason when the saved phrase is a template', async () => {
    fakeBackend({
      buttons: [plainBakeRow({ tts_text: 'The {{ states("sensor.temperature") }} degrees' })]
    });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Speech phrase'));

    expect(pageText()).toContain('Rendered on every press');
    expect(hasButton('Pre-render now')).toBe(false);
    expect(pageText()).toContain(TEMPLATE_NOT_BAKEABLE);
    expect(pageText()).toContain('Not pre-rendered');
  });

  it('renders a 503 refusal verbatim, invents no badge and leaves the retry in place', async () => {
    fakeBackend({
      buttons: [plainBakeRow()],
      bakeError: { status: 503, message: NO_SPEECH_SOURCE }
    });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Pre-render now'));

    buttonByText('Pre-render now').click();
    await vi.waitFor(() => expect(alertTexts()).toContain(NO_SPEECH_SOURCE));

    // A refusal is not a bake: the stored state is still reported, nothing is faked.
    expect(pageText()).toContain('Not pre-rendered');
    expect(pageText()).not.toContain('Pre-rendered · reused on press');
    expect(hasButton('Pre-render now')).toBe(true);
  });

  it('renders a 422 dynamic_phrase refusal verbatim', async () => {
    fakeBackend({
      buttons: [plainBakeRow()],
      bakeError: {
        status: 422,
        message: DYNAMIC_REFUSAL,
        detail: { field: 'tts_text', reason: 'dynamic_phrase' }
      }
    });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Pre-render now'));

    buttonByText('Pre-render now').click();
    await vi.waitFor(() => expect(alertTexts()).toContain(DYNAMIC_REFUSAL));
  });
});

/* ---- AC13 --------------------------------------------------------------- */

describe('13. the list draws the stored pre-render badge and nothing more', () => {
  function listBackend(): ReturnType<typeof fakeBackend> {
    return fakeBackend({
      buttons: [
        bakedRow({ id: 1, name: 'Baked bell', sort_order: 1 }),
        plainBakeRow({ id: 2, name: 'Unbaked bell', slug: 'unbaked-bell', sort_order: 2 }),
        failedRow('espeak-ng exited 1', {
          id: 3,
          name: 'Failed bell',
          slug: 'failed-bell',
          sort_order: 3
        }),
        // The same keys on a sound row: no phrase, so nothing to report.
        bakedRow({
          id: 4,
          name: 'Sound bell',
          slug: 'sound-bell',
          sort_order: 4,
          action_type: 'sound',
          sound_id: 1,
          tts_text: null
        }),
        // A build without the bake keys must not look like an unbaked button.
        ttsRow({ id: 5, name: 'Old server bell', slug: 'old-server-bell', sort_order: 5 })
      ]
    });
  }

  it('shows Pre-rendered, Not pre-rendered and the failure reason, and hides the badge otherwise', async () => {
    listBackend();
    await settings.load();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Old server bell'));

    const baked = rowFor('Baked bell');
    expect(baked.querySelector('.badge')?.textContent).toContain('Pre-rendered');
    expect(baked.textContent).not.toContain('Not pre-rendered');

    expect(rowFor('Unbaked bell').querySelector('.badge')?.textContent).toContain(
      'Not pre-rendered'
    );
    expect(rowFor('Failed bell').querySelector('.badge')?.textContent).toContain(
      'failed: espeak-ng exited 1'
    );

    // A sound row has no phrase and the server reports no bake state for it.
    expect(rowFor('Sound bell').querySelector('.badge')).toBeNull();
    // No bake keys at all: the feature is absent, not merely unbaked.
    expect(rowFor('Old server bell').querySelector('.badge')).toBeNull();
    expect(rowFor('Old server bell').textContent).not.toContain('pre-rendered');
    // The badge is the whole story here; the action lives in the S11 editor.
    expect(pageText()).not.toContain('Pre-render now');
  });

  it('shows the new badge when the list is mounted again after a bake', async () => {
    const backend = fakeBackend({ buttons: [plainBakeRow()] });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Pre-render now'));

    buttonByText('Pre-render now').click();
    await vi.waitFor(() => expect(pageText()).toContain('Pre-rendered as mp3 · reused on press'));

    // Returning to the list re-reads `GET /api/buttons`; mounting it fresh is this
    // test's way of asking the same question of the same server truth.
    unmountAll();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));

    expect(backend.buttons[0].baked_sha).toBe('bakedc0ffee');
    expect(rowFor('Front Door').querySelector('.badge')?.textContent).toContain('Pre-rendered');
  });
});

/* ---- HSD-po1g ------------------------------------------------------------ */

const UNSAFE_SLUG_MESSAGE =
  "the slug 'Front.Door' cannot be used in an MQTT topic — use lowercase a-z, digits, hyphens and underscores, 1 to 128 characters";

describe('14. HSD-po1g — a refused slug is the server’s sentence, shown verbatim', () => {
  it('renders the 422 for an unsafe slug verbatim and sends exactly one request', async () => {
    const backend = fakeBackend({ sounds: [soundRow()] });
    // The server owns this rule; the client must not second-guess it.
    backend.createError = UNSAFE_SLUG_MESSAGE;
    await settings.load();
    renderEditor(null);
    await vi.waitFor(() => expect(pageText()).toContain('Sound library'));

    typeInto(inputByLabel('Name'), 'Front Door');
    typeInto(inputByLabel('Slug'), 'Front.Door');
    openPicker('Sound library');
    chooseOption('Ding dong');

    buttonByText('Create button').click();
    await vi.waitFor(() =>
      expect(callsTo(backend.calls, '/api/buttons', 'POST')).toHaveLength(1)
    );
    await vi.waitFor(() => expect(alertTexts()).toContain(UNSAFE_SLUG_MESSAGE));

    // No client-side charset check: the unsafe value really did reach the server.
    expect(bodyOf(callsTo(backend.calls, '/api/buttons', 'POST')[0]).slug).toBe('Front.Door');
    expect(callsTo(backend.calls, '/api/buttons', 'POST')).toHaveLength(1);
  });
});

describe('15. HSD-po1g — the button icon picker offers no custom icons', () => {
  it('hides “Your icons” and never fetches the custom listing for a button', async () => {
    const backend = fakeBackend({ buttons: [buttonRow({ id: 1 })] });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Icon'));

    document.querySelector<HTMLButtonElement>('.icon-trigger')?.click();
    flushSync();

    await vi.waitFor(() =>
      expect(callsTo(backend.calls, '/api/icons/search?q=&limit=120')).toHaveLength(1)
    );
    // The MDI grid is there; the custom block a button could never save is not.
    expect(pageText()).not.toContain('Your icons');
    expect(callsTo(backend.calls, '/api/icons/custom')).toHaveLength(0);
  });
});

/* ---- HSD-s856 ------------------------------------------------------------ */

/** What the dialog adds after the server's own sentence, verbatim from `ButtonEditor`. */
const PAID_TEMPLATE_TAIL =
  'A template is rendered on every press and can bill. Remove the template syntax to bake a static phrase once, or accept the repeated estimated charge for this phrase.';

describe('16. HSD-s856 — a paid templated phrase is confirmed once, for one request', () => {
  const TEMPLATE = "The bell was pressed by {{ states('sensor.presser') }}";

  /** A stored speech row with a template typed into it, and a backend that refuses once. */
  async function renderTemplatedSave(): Promise<ReturnType<typeof fakeBackend>> {
    const backend = fakeBackend({ buttons: [ttsRow()], paidRefusalOnce: true });
    await settings.load();
    renderEditor(1);
    await vi.waitFor(() => expect(pageText()).toContain('Speech phrase'));

    const phrase = document.querySelector<HTMLTextAreaElement>('textarea');
    if (phrase === null) {
      throw new Error('expected the phrase textarea');
    }
    typeInto(phrase, TEMPLATE);
    return backend;
  }

  it('opens only on the matching 422, and Cancel keeps the draft and the footer sentence', async () => {
    const backend = await renderTemplatedSave();
    // A dialog is a reaction to the server, never something the editor opens by itself.
    expect(document.querySelector('dialog[open]')).toBeNull();

    buttonByText('Save').click();
    await vi.waitFor(() =>
      expect(callsTo(backend.calls, '/api/buttons/1', 'PATCH')).toHaveLength(1)
    );
    await vi.waitFor(() => expect(document.querySelector('dialog[open]')).not.toBeNull());

    expect(pageText()).toContain('Paid template cost warning');
    // The server's sentence verbatim, then the editor's own explanation of the cost.
    expect(pageText()).toContain(PAID_TEMPLATE_REFUSAL);
    expect(pageText()).toContain(PAID_TEMPLATE_TAIL);

    buttonByText('Cancel').click();
    flushSync();
    expect(document.querySelector('dialog[open]')).toBeNull();
    // Cancel is not a retry: exactly one request was sent and none followed.
    expect(callsTo(backend.calls, '/api/buttons/1', 'PATCH')).toHaveLength(1);
    // The draft survives, and the refusal stays in the footer as the reason it did not save.
    expect(document.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe(TEMPLATE);
    expect(pageText()).toContain(PAID_TEMPLATE_REFUSAL);
  });

  it('accepts with one same-body retry, and no later save carries the flag', async () => {
    const backend = await renderTemplatedSave();
    buttonByText('Save').click();
    await vi.waitFor(() =>
      expect(callsTo(backend.calls, '/api/buttons/1', 'PATCH')).toHaveLength(1)
    );
    await vi.waitFor(() => expect(document.querySelector('dialog[open]')).not.toBeNull());

    buttonByText('Accept paid-template cost').click();
    // Exactly one more request: accepting never resends more than the refused save.
    await vi.waitFor(() =>
      expect(callsTo(backend.calls, '/api/buttons/1', 'PATCH')).toHaveLength(2)
    );

    const [first, retry] = patchBodies(backend.calls);
    expect(retry).toEqual({ ...first, acknowledge_paid_template: true });
    // The same request otherwise: the flag is the only difference, not a rebuilt body.
    const retryFields = Object.fromEntries(
      Object.entries(retry).filter(([key]) => key !== 'acknowledge_paid_template')
    );
    expect(retryFields).toEqual(first);

    await vi.waitFor(() =>
      expect(toastStore.items.map((toast) => toast.message)).toContain('Button saved.')
    );
    expect(document.querySelector('dialog[open]')).toBeNull();

    typeInto(inputByLabel('Name'), 'Front Door Bell');
    buttonByText('Save').click();
    await vi.waitFor(() =>
      expect(callsTo(backend.calls, '/api/buttons/1', 'PATCH')).toHaveLength(3)
    );

    // D25's acknowledgement is request-scoped: the next ordinary save states only its diff.
    const later = patchBodies(backend.calls)[2];
    expect(later).toEqual({ name: 'Front Door Bell' });
    expect(later).not.toHaveProperty('acknowledge_paid_template');
  });
});
