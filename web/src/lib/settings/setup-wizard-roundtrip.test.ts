// @vitest-environment happy-dom
/**
 * The two cross-step invariants of the first-run wizard (HSD-8544), proved
 * end-to-end through the real component and a stateful settings fake.
 *
 * `setup-wizard.test.ts` already proves each step's own rules one at a time. It
 * is also byte-identical by contract — a later ticket may not grow it — so the
 * facts that only exist *between* two steps live here instead of being appended
 * to it:
 *
 *   1. **A step save never discards the other step's typing.** Typing a broker
 *      host on step 2, walking Back to step 1 and saving there must leave the
 *      broker host exactly as typed, and each save must carry only its own
 *      key group. Neither half is observable from inside one step.
 *   2. **The inline speaker check spends nothing until it is asked.** Step 3
 *      gained a volume slider and a test tone; "no request at mount" and "the
 *      slider's release saves one key" are statements about the same screen at
 *      two moments, and the volume key belongs to neither connection group.
 *   3. **A step change starts at the top.** The wizard scrolls the document, so
 *      the assertion has to spy on the real `window`.
 *
 * Fully offline: every request goes through `setFetchImpl`, so no test opens a
 * socket. `happy-dom` is opted into for this file only; the default `node`
 * environment and the rest of the suite are untouched. Nothing here asserts
 * layout — a DOM has no CSS engine — so the geometric half of the ticket is
 * `web/browser/app/setup-layout.spec.ts`.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { setFetchImpl, type FetchLike, type FetchResponse } from '../api.js';
import { settings } from '../settings.svelte.js';
import SetupWizard from './SetupWizard.svelte';

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

/** A 500 the API seam turns into an `ApiError` carrying `message`. */
function errorResponse(code: string, message: string): FetchResponse {
  return jsonResponse({ error: { code, message, detail: null } }, 500);
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
  secrets?: Record<string, boolean>;
  /** Hold both audio answers until this resolves, so the busy state is observable. */
  audioGate?: Promise<void>;
  /** Answer `GET /api/audio/hardware` with a 500 carrying this sentence. */
  hardwareFails?: string;
  /** The `GET /api/audio/hardware` body, when it succeeds. */
  hardware?: Record<string, unknown>;
  entities?: unknown[];
  buttons?: unknown[];
}

interface Backend {
  calls: FakeCall[];
  settings: Record<string, unknown>;
  secrets: Record<string, boolean>;
}

/** What the real probe answers on a device with a working output and no timing. */
const AUDIO_OK = { ok: true, device: 'auto', latency_ms: null, error: null };

/** What the real bench answers for its offline output. */
const HARDWARE_OK = {
  ok: true,
  available: true,
  device: 'pipewire/offline-output',
  control: 'offline-output',
  percent: 100,
  muted: false,
  reason: '',
  configured: 100,
  applied: null
};

const HA_OK = { ok: true, ha_version: '2024.10.1', entity_count: 42, error: null };
const MQTT_OK = { ok: true, broker_version: '2.0.18', error: null, auth_failed: false };

/** The bench's own install: Home Assistant configured, no broker yet. */
const BENCH_SETTINGS: Record<string, unknown> = {
  'ha.base_url': 'http://homeassistant.local:8123',
  'audio.volume': 100
};

const BENCH_SECRETS: Record<string, boolean> = { 'ha.token': true };

/** A key the backend stores but never echoes, so only its flag comes back. */
function isSecretKey(key: string): boolean {
  return key === 'ha.token' || key === 'mqtt.password';
}

/**
 * A tiny in-memory backend shaped like `setup-wizard.test.ts`'s fake: `GET`
 * echoes the state, `PATCH` records, merges and echoes the whole snapshot, and
 * every other route it knows about answers a fixed body. An unrecognised path
 * throws, so a test can never pass on a request the wizard should not have made.
 */
function fakeBackend(options: BackendOptions = {}): Backend {
  const storedSettings: Record<string, unknown> = { ...(options.settings ?? {}) };
  const storedSecrets: Record<string, boolean> = { ...(options.secrets ?? {}) };

  const calls = fakeFetch(async (path, init) => {
    if (path === '/api/settings' && init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      for (const [key, value] of Object.entries(body)) {
        if (value === null) {
          delete storedSettings[key];
          storedSecrets[key] = false;
        } else if (isSecretKey(key)) {
          delete storedSettings[key];
          storedSecrets[key] = true;
        } else {
          storedSettings[key] = value;
        }
      }
      return jsonResponse({ settings: { ...storedSettings }, secrets: { ...storedSecrets } });
    }

    if (path === '/api/settings') {
      return jsonResponse({ settings: { ...storedSettings }, secrets: { ...storedSecrets } });
    }

    if (path === '/api/settings/test/ha' && init?.method === 'POST') {
      return jsonResponse(HA_OK);
    }

    if (path === '/api/settings/test/mqtt' && init?.method === 'POST') {
      return jsonResponse(MQTT_OK);
    }

    if (path === '/api/entities') {
      return jsonResponse(options.entities ?? []);
    }

    if (path === '/api/buttons') {
      return jsonResponse(options.buttons ?? []);
    }

    if (path === '/api/settings/test/audio' && init?.method === 'POST') {
      await options.audioGate;
      return jsonResponse(AUDIO_OK);
    }

    if (path === '/api/audio/hardware') {
      await options.audioGate;
      if (options.hardwareFails !== undefined) {
        return errorResponse('internal_error', options.hardwareFails);
      }
      return jsonResponse(options.hardware ?? HARDWARE_OK);
    }

    throw new Error(`unexpected request ${path}`);
  });

  return { calls, settings: storedSettings, secrets: storedSecrets };
}

/* ---- DOM helpers --------------------------------------------------------- */

const mounted: Array<Parameters<typeof unmount>[0]> = [];

/** Mount the wizard with an `onnavigate` spy, so nothing can leave the document. */
function mountWizard(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(SetupWizard, { target, props: { onnavigate: () => {} } }));
  flushSync();
  return target;
}

function unmountAll(): void {
  for (const component of mounted.splice(0)) {
    unmount(component);
  }
  document.body.innerHTML = '';
}

function mustFind<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (element === null) {
    throw new Error(`expected to find ${selector}`);
  }
  return element;
}

/** The document-wide button whose trimmed text is exactly *label*. */
function buttonByText(label: string): HTMLButtonElement {
  const button = [...document.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === label
  );
  if (button === undefined) {
    throw new Error(`expected a button labelled "${label}"`);
  }
  return button;
}

/** The input a `label[for]` points at, the way the wizard's fields are built. */
function inputByLabel(label: string): HTMLInputElement {
  const field = [...document.querySelectorAll('label')].find(
    (candidate) => candidate.textContent?.trim() === label
  );
  const id = field?.getAttribute('for') ?? null;
  const input = id === null ? null : document.getElementById(id);
  if (!(input instanceof HTMLInputElement)) {
    throw new Error(`expected an input labelled "${label}"`);
  }
  return input;
}

/** Type into a field the way a person would: set the value, fire `input`. */
function type(input: HTMLInputElement, value: string): void {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

/** One element's text with its whitespace normalised, so a wrapped sentence matches. */
function textOf(element: Element): string {
  return (element.textContent ?? '').replace(/\s+/g, ' ').trim();
}

function patches(calls: FakeCall[]): FakeCall[] {
  return calls.filter((call) => call.path === '/api/settings' && call.init?.method === 'PATCH');
}

function patchBody(call: FakeCall): Record<string, unknown> {
  return JSON.parse(String(call.init?.body)) as Record<string, unknown>;
}

/** The keys of every PATCH body that touched *key*, in call order. */
function bodiesTouching(calls: FakeCall[], key: string): Array<Record<string, unknown>> {
  return patches(calls)
    .map(patchBody)
    .filter((body) => Object.prototype.hasOwnProperty.call(body, key));
}

/**
 * Drive the real buttons to step 2: Test connection, wait for the gate to open,
 * Continue.
 */
async function passToStep2(): Promise<void> {
  buttonByText('Test connection').click();
  await vi.waitFor(() => expect(buttonByText('Continue').disabled).toBe(false));
  buttonByText('Continue').click();
  flushSync();
}

/** The same for step 3, including its counts load, so the screen is settled. */
async function passToStep3(): Promise<void> {
  await passToStep2();
  buttonByText('Test MQTT').click();
  await vi.waitFor(() => expect(buttonByText('Continue').disabled).toBe(false));
  buttonByText('Continue').click();
  flushSync();
  await vi.waitFor(() =>
    expect(mustFind('[data-setup-count="tiles"]').textContent).toBe('0')
  );
}

/**
 * Move the speaker slider and release it: `input` updates the bound value,
 * `change` is the commit the card listens for (a release, not a drag tick).
 */
function releaseSlider(value: number): void {
  const slider = inputByLabel('Speaker volume');
  slider.value = String(value);
  slider.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
  slider.dispatchEvent(new Event('change', { bubbles: true }));
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
  vi.restoreAllMocks();
});

/* ---- A. the two connection groups stay apart ----------------------------- */

describe('a step save never discards the other step’s typing', () => {
  it('keeps step 2’s unsaved broker host when step 1 saves, and sends only HA keys', async () => {
    const backend = fakeBackend({ settings: { ...BENCH_SETTINGS }, secrets: { ...BENCH_SECRETS } });
    await settings.load();
    mountWizard();
    await passToStep2();

    // Typed on step 2 and deliberately NOT tested: the point is that leaving the
    // step without saving must not lose it.
    type(inputByLabel('Broker host'), 'broker.typing.test');
    buttonByText('Back').click();
    flushSync();

    type(inputByLabel('Long-lived token'), 'token-from-step-one');
    const before = patches(backend.calls).length;
    buttonByText('Test connection').click();
    await vi.waitFor(() => expect(buttonByText('Continue').disabled).toBe(false));

    const stepOne = patches(backend.calls).slice(before);
    expect(stepOne.length, 'the step-1 save must be exactly one PATCH').toBe(1);
    const body = patchBody(stepOne[0]);
    expect(
      Object.keys(body).filter((key) => !key.startsWith('ha.')),
      `step 1 may only write ha.* keys, sent ${JSON.stringify(Object.keys(body))}`
    ).toEqual([]);
    expect(Object.keys(body), 'the step-1 save carries the typed token').toContain('ha.token');

    // The invariant: the broker host survived a save that was not about it.
    buttonByText('Continue').click();
    flushSync();
    expect(
      inputByLabel('Broker host').value,
      'step 1’s save must not discard step 2’s unsaved broker host'
    ).toBe('broker.typing.test');

    // ...and step 2 then saves it under its own keys only. The mirror case
    // (step 2's save clobbering step 1) is not asserted here because it is not
    // reachable through the UI: step 1 cannot be left without its own
    // successful test. `connection-form.test.ts` covers `rebaseMqttFields` in
    // isolation.
    const beforeMqtt = patches(backend.calls).length;
    buttonByText('Test MQTT').click();
    await vi.waitFor(() => expect(buttonByText('Continue').disabled).toBe(false));

    const stepTwo = patches(backend.calls).slice(beforeMqtt);
    expect(stepTwo.length, 'the step-2 save must be exactly one PATCH').toBe(1);
    const mqttBody = patchBody(stepTwo[0]);
    expect(mqttBody['mqtt.host'], 'the step-2 save carries the typed host').toBe(
      'broker.typing.test'
    );
    expect(
      Object.keys(mqttBody).filter((key) => key.startsWith('ha.')),
      `step 2 must not write ha.* keys, sent ${JSON.stringify(Object.keys(mqttBody))}`
    ).toEqual([]);
  });

  it('leaves the wizard’s own audio save out of both connection groups', async () => {
    const backend = fakeBackend({ settings: { ...BENCH_SETTINGS }, secrets: { ...BENCH_SECRETS } });
    await settings.load();
    mountWizard();
    await passToStep3();

    releaseSlider(64);
    await vi.waitFor(() => expect(bodiesTouching(backend.calls, 'audio.volume')).toHaveLength(1));

    // `audio.volume` belongs to neither connection group, so its save must be a
    // one-key patch: any `ha.*` or `mqtt.*` key here would be the wizard
    // re-saving a connection the user did not touch.
    const body = bodiesTouching(backend.calls, 'audio.volume')[0];
    expect(body, `the slider’s save must be one key, sent ${JSON.stringify(body)}`).toEqual({
      'audio.volume': 64
    });
  });
});

/* ---- B. the inline speaker check ---------------------------------------- */

describe('the inline speaker check', () => {
  it('spends no request until the tone is asked for', async () => {
    const backend = fakeBackend({ settings: { ...BENCH_SETTINGS }, secrets: { ...BENCH_SECRETS } });
    await settings.load();
    mountWizard();
    await passToStep3();

    const asked = backend.calls.filter(
      (call) => call.path === '/api/settings/test/audio' || call.path === '/api/audio/hardware'
    );
    // A first-run page that probes the speaker unasked would make noise and
    // spend a request the user did not ask for.
    expect(asked.map((call) => call.path), 'step 3 must mount without touching the audio API').toEqual(
      []
    );
  });

  it('plays the tone, names the system level and announces both', async () => {
    let releaseAudio = (): void => {};
    const audioGate = new Promise<void>((resolve) => {
      releaseAudio = resolve;
    });
    const backend = fakeBackend({
      settings: { ...BENCH_SETTINGS },
      secrets: { ...BENCH_SECRETS },
      audioGate
    });
    await settings.load();
    mountWizard();
    await passToStep3();

    const play = buttonByText('Play test sound');
    play.click();
    flushSync();

    // In flight: the button says so, and it cannot be pressed twice.
    expect(play.getAttribute('aria-busy'), 'the tone button announces its own work').toBe('true');
    expect(play.disabled, 'a tone already being played must not be re-triggered').toBe(true);

    releaseAudio();
    await vi.waitFor(() => expect(play.getAttribute('aria-busy')).toBe('false'));
    expect(play.disabled, 'the button comes back once the tone has played').toBe(false);

    const verdict = textOf(mustFind('[data-setup-step="3"] .result'));
    expect(verdict, 'the polite region announces the outcome in words').toContain(
      'Test sound played'
    );
    expect(verdict, 'and names the device it played on').toContain('Played on auto');

    const tone = backend.calls.filter((call) => call.path === '/api/settings/test/audio');
    const hardware = backend.calls.filter((call) => call.path === '/api/audio/hardware');
    expect(tone, 'one tap is one probe, never a retry loop').toHaveLength(1);
    expect(hardware, 'one tap is one system-level read').toHaveLength(1);
  });

  it('saves only audio.volume when the slider is released', async () => {
    const backend = fakeBackend({ settings: { ...BENCH_SETTINGS }, secrets: { ...BENCH_SECRETS } });
    await settings.load();
    mountWizard();
    await passToStep3();

    const before = patches(backend.calls).length;
    releaseSlider(64);
    await vi.waitFor(() => expect(patches(backend.calls).length).toBe(before + 1));

    // One save per gesture: the `change` event, not every drag tick.
    const saved = patches(backend.calls).slice(before);
    expect(patchBody(saved[0]), 'the release writes the volume and nothing else').toEqual({
      'audio.volume': 64
    });
  });

  it('stays soft when the system level cannot be read', async () => {
    const prose = 'the audio backend did not answer';
    const backend = fakeBackend({
      settings: { ...BENCH_SETTINGS },
      secrets: { ...BENCH_SECRETS },
      hardwareFails: prose
    });
    await settings.load();
    mountWizard();
    await passToStep3();

    buttonByText('Play test sound').click();

    await vi.waitFor(() =>
      expect(textOf(mustFind('[data-setup-step="3"] .result'))).toContain('Test sound played')
    );

    // The tone is the test that matters; the level is a courtesy read, and its
    // failure must be a quiet sentence rather than a rejection.
    const note = textOf(mustFind('[data-setup-step="3"] [role="status"]'));
    expect(note, 'a failed level read is reported in words, not thrown').toContain(
      'The system volume could not be read'
    );
    expect(
      backend.calls.filter((call) => call.path === '/api/settings/test/audio'),
      'the failed level read does not cost the tone'
    ).toHaveLength(1);
  });

  it('stays soft on a device with no hardware volume control', async () => {
    const prose = 'This device has no hardware volume control.';
    fakeBackend({
      settings: { ...BENCH_SETTINGS },
      secrets: { ...BENCH_SECRETS },
      hardware: {
        ok: true,
        available: false,
        device: null,
        control: null,
        percent: null,
        muted: false,
        reason: prose,
        configured: 100,
        applied: null
      }
    });
    await settings.load();
    mountWizard();
    await passToStep3();

    buttonByText('Play test sound').click();

    await vi.waitFor(() =>
      expect(textOf(mustFind('[data-setup-step="3"] [role="status"]'))).toBe(prose)
    );
    // The wizard offers no hardware slider, so an absent control is a fact to
    // state — not a problem whose number is missing.
    expect(textOf(mustFind('[data-setup-step="3"] .result'))).toContain('Test sound played');
  });
});

/* ---- C. a step change starts at the top --------------------------------- */

describe('a step change starts at the top', () => {
  it('scrolls to the top when a step advances and when it goes back', async () => {
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
    fakeBackend({ settings: { ...BENCH_SETTINGS }, secrets: { ...BENCH_SECRETS } });
    await settings.load();
    mountWizard();

    const atMount = scrollTo.mock.calls.length;
    await passToStep2();

    // Step 1 was taller than the panel; opening step 2 at the same offset would
    // put its title and lead above the fold.
    expect(
      scrollTo.mock.calls.length,
      'advancing from step 1 to step 2 must reset the scroll'
    ).toBe(atMount + 1);
    expect(scrollTo.mock.calls[atMount], 'the reset is the document origin').toEqual([0, 0]);

    buttonByText('Back').click();
    flushSync();

    // Going back is a step change too: the previous step's offset is not this
    // step's.
    expect(
      scrollTo.mock.calls.length,
      'going back from step 2 to step 1 must reset the scroll'
    ).toBe(atMount + 2);
    expect(scrollTo.mock.calls[atMount + 1], 'the reset is the document origin').toEqual([0, 0]);
  });
});
