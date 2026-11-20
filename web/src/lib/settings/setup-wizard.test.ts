// @vitest-environment happy-dom
/**
 * DOM tests for the first-run setup wizard (HSD-vl7t).
 *
 * Fully offline: every request goes through `setFetchImpl`, so no test opens a
 * socket. `happy-dom` provides the DOM per file; the default `node`
 * environment (and the rest of the suite) is untouched. The pure routing rules
 * behind these screens are proved separately in `setup.test.ts`.
 *
 * The tests are numbered to map 1:1 onto the ticket's acceptance criteria.
 * They prove semantics — the word rendered, the key in the patch, the request
 * made, the target navigated to — not layout: a DOM has no CSS engine, so the
 * D26 sizing is asserted on the styles and the real finger work is the
 * Playwright ticket's.
 *
 * The wizard is mounted with an `onnavigate` spy, so nothing here can leave the
 * document; that spy is also the only way to prove where a skip or a finish
 * actually sends the user.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import Dashboard from '../Dashboard.svelte';
import {
  setFetchImpl,
  type FetchLike,
  type FetchResponse,
  type SnapshotData
} from '../api.js';
import { settings } from '../settings.svelte.js';
import { setupRedirectAfterLoad, setupStateFrom } from '../setup.js';
import { liveState } from '../stores.svelte.js';
import { toastStore } from '../ui/toast.svelte.js';
import SettingsLayout from './SettingsLayout.svelte';
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
  haTest?: unknown;
  mqttTest?: unknown;
  entities?: unknown[];
  buttons?: unknown[];
  /** Answer every settings GET with a 500 envelope carrying this sentence. */
  settingsLoadFails?: string;
  /** Answer every settings PATCH with a 500 envelope carrying this sentence. */
  patchFails?: string;
}

interface Backend {
  calls: FakeCall[];
  settings: Record<string, unknown>;
  secrets: Record<string, boolean>;
}

const HA_OK = { ok: true, ha_version: '2024.10.1', entity_count: 42, error: null };
const MQTT_OK = { ok: true, broker_version: '2.0.18', error: null, auth_failed: false };

/**
 * A settings key whose value is a secret: the backend stores it but never
 * returns it, so `GET /api/settings` carries only the `secrets` flag.
 */
function isSecretKey(key: string): boolean {
  return key === 'ha.token' || key === 'mqtt.password';
}

/**
 * A tiny in-memory settings backend: GET echoes the state, PATCH applies the
 * diff and echoes the whole snapshot (a secret `null` clears it, a string
 * stores it — but its value is never echoed back, matching the real backend),
 * and the two probes, the entity list and the button list answer whatever the
 * test set.
 */
function fakeBackend(options: BackendOptions = {}): Backend {
  const storedSettings: Record<string, unknown> = { ...(options.settings ?? {}) };
  const storedSecrets: Record<string, boolean> = { ...(options.secrets ?? {}) };

  const calls = fakeFetch((path, init) => {
    if (path === '/api/settings' && init?.method === 'PATCH') {
      if (options.patchFails !== undefined) {
        return jsonResponse(
          {
            error: {
              code: 'invalid_setting',
              message: options.patchFails,
              detail: null
            }
          },
          500
        );
      }
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      for (const [key, value] of Object.entries(body)) {
        if (value === null) {
          delete storedSettings[key];
          storedSecrets[key] = false;
        } else if (isSecretKey(key)) {
          // A stored secret never comes back on the wire: only its flag does.
          delete storedSettings[key];
          storedSecrets[key] = true;
        } else {
          storedSettings[key] = value;
        }
      }
      return jsonResponse({
        settings: { ...storedSettings },
        secrets: { ...storedSecrets }
      });
    }

    if (path === '/api/settings') {
      if (options.settingsLoadFails !== undefined) {
        return jsonResponse(
          {
            error: {
              code: 'internal_error',
              message: options.settingsLoadFails,
              detail: null
            }
          },
          500
        );
      }
      return jsonResponse({
        settings: { ...storedSettings },
        secrets: { ...storedSecrets }
      });
    }

    if (path === '/api/settings/test/ha' && init?.method === 'POST') {
      return jsonResponse(
        options.haTest ?? { ok: false, ha_version: null, entity_count: null, error: 'not stubbed' }
      );
    }

    if (path === '/api/settings/test/mqtt' && init?.method === 'POST') {
      return jsonResponse(
        options.mqttTest ?? {
          ok: false,
          broker_version: null,
          error: 'not stubbed',
          auth_failed: false
        }
      );
    }

    if (path === '/api/entities') {
      return jsonResponse(options.entities ?? []);
    }

    if (path === '/api/buttons') {
      return jsonResponse(options.buttons ?? []);
    }

    // The picker's search route: never reached by these tests, served anyway so
    // a future test can open the picker without extending this fake.
    if (path.startsWith('/api/ha/entities')) {
      return jsonResponse({ entities: [], warning: null });
    }

    throw new Error(`unexpected request ${path}`);
  });

  return { calls, settings: storedSettings, secrets: storedSecrets };
}

/* ---- DOM helpers --------------------------------------------------------- */

const mounted: Array<Parameters<typeof unmount>[0]> = [];

/** Every path the wizard asked to navigate to (nothing navigates for real). */
const navigations: string[] = [];

function renderWizard(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(
    mount(SetupWizard, {
      target,
      props: {
        onnavigate: (path: string) => {
          navigations.push(path);
        }
      }
    })
  );
  flushSync();
  return target;
}

function renderDashboard(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(Dashboard, { target }));
  flushSync();
  return target;
}

function renderLayout(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(SettingsLayout, { target }));
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

function buttonByText(label: string): HTMLButtonElement {
  const button = [...document.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === label
  );
  if (button === undefined) {
    throw new Error(`expected a button labelled "${label}"`);
  }
  return button;
}

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

/** Type into a field the way a person would: set the value, fire input. */
function type(input: HTMLInputElement, value: string): void {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

/** Normalised page text, so a wrapped sentence still matches as one string. */
function pageText(): string {
  return (document.body.textContent ?? '').replace(/\s+/g, ' ').trim();
}

/** The same, for one element: a sentence wrapped across lines still matches. */
function textOf(element: Element): string {
  return (element.textContent ?? '').replace(/\s+/g, ' ').trim();
}

/** The step line's words: the word carries the state, never the dots. */
function stepText(): string {
  return mustFind('[data-setup-progress]').textContent?.trim() ?? '';
}

function patches(calls: FakeCall[]): FakeCall[] {
  return calls.filter((call) => call.path === '/api/settings' && call.init?.method === 'PATCH');
}

function patchBody(call: FakeCall): Record<string, unknown> {
  return JSON.parse(String(call.init?.body)) as Record<string, unknown>;
}

/**
 * Record the escape's real navigation in the same log as the requests, so a
 * test can prove the dismissal was saved first. happy-dom does not navigate, so
 * the call is captured rather than followed.
 */
function spyNavigation(calls: FakeCall[]): void {
  vi.spyOn(window.location, 'assign').mockImplementation((url: string | URL) => {
    calls.push({ path: `location.assign:${String(url)}` });
  });
}

/** A dashboard snapshot with nothing configured: a legal, unremarkable state. */
function emptySnapshot(): SnapshotData {
  return {
    entities: [],
    buttons: [],
    health: { ha: 'connected', mqtt: 'connected', audio: 'ready' },
    config_version: 1
  };
}

/** Type a URL and a token, pass the step-1 test and advance to step 2. */
async function passStepOne(): Promise<void> {
  type(inputByLabel('Home Assistant URL'), 'http://ha.local:8123');
  type(inputByLabel('Long-lived token'), 'token-123');
  buttonByText('Test connection').click();
  await vi.waitFor(() => expect(buttonByText('Continue').disabled).toBe(false));
  buttonByText('Continue').click();
  flushSync();
}

beforeEach(() => {
  settings.reset();
  liveState.reset();
  setFetchImpl(null);
  navigations.length = 0;
  unmountAll();
});

afterEach(() => {
  unmountAll();
  settings.reset();
  liveState.reset();
  setFetchImpl(null);
  toastStore.clear();
  vi.restoreAllMocks();
});

/* ---- 1. fresh database redirects ----------------------------------------- */

describe('1. a fresh database redirects to the wizard', () => {
  it('sends the dashboard root to /setup and opens on step 1', async () => {
    fakeBackend({ settings: { 'ha.base_url': '' }, secrets: { 'ha.token': false } });

    await settings.load();

    expect(setupRedirectAfterLoad(settings.loaded, '/', setupStateFrom(settings))).toBe('/setup');

    renderWizard();

    expect(pageText()).toContain('Connect to Home Assistant');
    expect(stepText()).toBe('Step 1 of 3');
    expect(mustFind('[data-setup="wizard"]')).toBeTruthy();
    expect(mustFind('[data-setup-step="1"]')).toBeTruthy();
  });
});

/* ---- 2. a valid URL and token advance ------------------------------------ */

describe('2. a valid URL and token advance and show the detected version', () => {
  it('saves only its own keys, probes once, then advances', async () => {
    const backend = fakeBackend({ haTest: HA_OK });
    await settings.load();
    renderWizard();

    type(inputByLabel('Home Assistant URL'), 'http://ha.local:8123');
    type(inputByLabel('Long-lived token'), 'token-123');
    buttonByText('Test connection').click();

    await vi.waitFor(() => expect(pageText()).toContain('2024.10.1'));

    // Exactly one PATCH, exactly the two keys step 1 owns, and no empty string
    // standing in for a secret.
    const saved = patches(backend.calls);
    expect(saved).toHaveLength(1);
    expect(patchBody(saved[0])).toEqual({
      'ha.base_url': 'http://ha.local:8123',
      'ha.token': 'token-123'
    });
    expect(backend.calls.filter((call) => call.path === '/api/settings/test/ha')).toHaveLength(1);

    expect(pageText()).toContain('42');
    expect(pageText()).toContain('entities');
    expect(pageText()).toContain('Connected');

    expect(buttonByText('Continue').disabled).toBe(false);
    buttonByText('Continue').click();
    flushSync();

    expect(pageText()).toContain('Connect to MQTT');
    expect(stepText()).toBe('Step 2 of 3');
  });
});

/* ---- 3. an unreported version degrades honestly -------------------------- */

describe('3. an unreported version degrades honestly', () => {
  it('shows the count, says the version was not reported, and still advances', async () => {
    fakeBackend({ haTest: { ok: true, ha_version: null, entity_count: 7, error: null } });
    await settings.load();
    renderWizard();

    type(inputByLabel('Home Assistant URL'), 'http://ha.local:8123');
    type(inputByLabel('Long-lived token'), 'token-123');
    buttonByText('Test connection').click();

    await vi.waitFor(() => expect(pageText()).toContain('7 entities'));

    expect(pageText()).toContain('the version was not reported');
    expect(buttonByText('Continue').disabled).toBe(false);
  });
});

/* ---- 4. an invalid token blocks ------------------------------------------ */

describe('4. an invalid token blocks with a clear error', () => {
  it('renders the server sentence verbatim and keeps Continue disabled', async () => {
    const prose =
      'Home Assistant rejected the token (401 Unauthorized) — create a new long-lived token and paste it here';
    fakeBackend({ haTest: { ok: false, ha_version: null, entity_count: null, error: prose } });
    await settings.load();
    renderWizard();

    type(inputByLabel('Home Assistant URL'), 'http://ha.local:8123');
    type(inputByLabel('Long-lived token'), 'bad-token');
    buttonByText('Test connection').click();

    await vi.waitFor(() => expect(pageText()).toContain(prose));

    expect(pageText()).toContain('Not connected');
    expect(buttonByText('Continue').disabled).toBe(true);
    expect(stepText()).toBe('Step 1 of 3');

    // A different token is a different question: the gate stays shut until it
    // has been answered by a new test.
    type(inputByLabel('Long-lived token'), 'another-token');
    expect(buttonByText('Continue').disabled).toBe(true);
    expect(stepText()).toBe('Step 1 of 3');
  });
});

/* ---- 5. skipping and re-entry -------------------------------------------- */

describe('5. skipping works and settings re-entry exists', () => {
  it('dismisses once and lands on the dashboard', async () => {
    const backend = fakeBackend({ settings: { 'ha.base_url': '' } });
    await settings.load();
    renderWizard();

    buttonByText("I'll do it later").click();

    await vi.waitFor(() => expect(navigations).toEqual(['/']));

    const saved = patches(backend.calls);
    expect(saved).toHaveLength(1);
    expect(patchBody(saved[0])).toEqual({ 'ui.setup_dismissed': true });
  });

  it('offers the wizard again from the settings shell', async () => {
    fakeBackend({});
    await settings.load();
    renderLayout();

    const link = mustFind<HTMLAnchorElement>('a[href="/setup"]');
    expect(link.textContent?.trim()).toBe('Setup wizard');
  });

  it('stays put and shows why when the dismissal could not be saved', async () => {
    const prose = 'the settings could not be saved because the database is read-only';
    fakeBackend({ settings: { 'ha.base_url': '' }, patchFails: prose });
    await settings.load();
    renderWizard();

    buttonByText("I'll do it later").click();

    await vi.waitFor(() => expect(pageText()).toContain(prose));
    expect(navigations).toEqual([]);
    expect(pageText()).toContain('Connect to Home Assistant');
  });
});

/* ---- 6. half-configured states never crash ------------------------------- */

describe('6. half-configured states never crash', () => {
  it('(a) renders with nothing configured at all', async () => {
    fakeBackend({ settings: { 'ha.base_url': '' } });
    await settings.load();
    liveState.applySnapshot(emptySnapshot());

    renderDashboard();

    expect(pageText()).toContain('No status tiles configured');
  });

  it('(b) renders with Home Assistant configured and nothing else', async () => {
    fakeBackend({
      settings: { 'ha.base_url': 'http://ha.local:8123' },
      secrets: { 'ha.token': true }
    });
    await settings.load();
    liveState.applySnapshot(emptySnapshot());

    renderDashboard();

    expect(pageText()).toContain('No status tiles configured');
  });

  it('(c) renders with MQTT configured and nothing else', async () => {
    fakeBackend({ settings: { 'mqtt.host': 'broker.local' } });
    await settings.load();
    liveState.applySnapshot(emptySnapshot());

    renderDashboard();

    expect(pageText()).toContain('No status tiles configured');
  });

  it('(d) renders when the configured Home Assistant cannot be reached', async () => {
    const prose =
      'the server could not be reached: connect ECONNREFUSED 192.168.0.10:8123';
    const backend = fakeBackend({
      settings: { 'ha.base_url': 'http://192.168.0.10:8123' },
      secrets: { 'ha.token': true },
      haTest: { ok: false, ha_version: null, entity_count: null, error: prose }
    });
    await settings.load();
    liveState.applySnapshot(emptySnapshot());

    renderDashboard();
    expect(pageText()).toContain('No status tiles configured');

    renderWizard();
    buttonByText('Test connection').click();

    await vi.waitFor(() =>
      expect(mustFind('[data-setup-step="1"]').textContent).toContain(prose)
    );
    // Nothing was rewritten: the stored configuration was already what is on
    // screen, so a probe needs no PATCH.
    expect(patches(backend.calls)).toHaveLength(0);
    expect(mustFind('[data-setup-step="1"]').textContent).toContain('Not connected');
  });

  it('(e) renders the fields and the sentence when the load itself failed', async () => {
    const prose = 'the settings could not be loaded because the database is locked';
    fakeBackend({ settingsLoadFails: prose });

    await settings.load();
    expect(settings.loaded).toBe(false);

    liveState.applySnapshot(emptySnapshot());
    renderDashboard();
    expect(pageText()).toContain('No status tiles configured');

    renderWizard();

    expect(inputByLabel('Home Assistant URL')).toBeTruthy();
    expect(inputByLabel('Long-lived token')).toBeTruthy();
    const alert = mustFind('[data-setup="wizard"] [role="alert"]');
    expect(alert.textContent?.trim()).toBe(prose);
    expect(stepText()).toBe('Step 1 of 3');
  });
});

/* ---- 7. step 3 and finish ------------------------------------------------ */

describe('7. step 3 and finish', () => {
  it('counts what is configured, then finishes with the dismissal saved', async () => {
    const backend = fakeBackend({ haTest: HA_OK, mqttTest: MQTT_OK });
    await settings.load();
    renderWizard();

    await passStepOne();
    buttonByText('Test MQTT').click();
    await vi.waitFor(() => expect(buttonByText('Continue').disabled).toBe(false));
    buttonByText('Continue').click();
    flushSync();

    expect(stepText()).toBe('Step 3 of 3');
    expect(mustFind('[data-setup-step="3"]')).toBeTruthy();
    expect(pageText()).toContain('Add your first tile and a first chime');

    // Zero tiles and zero chimes is a legal install, not a failure.
    await vi.waitFor(() =>
      expect(mustFind('[data-setup-count="tiles"]').textContent).toBe('0')
    );
    expect(mustFind('[data-setup-count="chimes"]').textContent).toBe('0');
    expect(pageText()).toContain('0 tiles and 0 chimes added.');
    expect(buttonByText('Add a first tile')).toBeTruthy();
    expect(buttonByText('Add a first chime')).toBeTruthy();

    const finish = buttonByText('Finish setup');
    expect(finish.disabled).toBe(false);
    finish.click();

    await vi.waitFor(() => expect(navigations).toEqual(['/?setup=done']));

    const dismissals = patches(backend.calls).filter(
      (call) => patchBody(call)['ui.setup_dismissed'] === true
    );
    expect(dismissals).toHaveLength(1);
  });

  it('lets the optional security step be skipped and still finishes setup', async () => {
    const backend = fakeBackend({ haTest: HA_OK, mqttTest: MQTT_OK });
    await settings.load();
    renderWizard();

    await passStepOne();
    buttonByText('Test MQTT').click();
    await vi.waitFor(() => expect(buttonByText('Continue').disabled).toBe(false));
    buttonByText('Continue').click();
    flushSync();
    await vi.waitFor(() => expect(mustFind('[data-setup-count="tiles"]').textContent).toBe('0'));

    buttonByText('Protect the configuration interface').click();
    flushSync();
    expect(mustFind('[data-setup-step="4"]')).toBeTruthy();
    expect(pageText()).toContain('Protect the configuration interface');
    expect(buttonByText('Skip for now')).toBeTruthy();

    buttonByText('Skip for now').click();
    await vi.waitFor(() => expect(navigations).toEqual(['/?setup=done']));
    expect(backend.calls.some((call) => call.path === '/api/auth/password')).toBe(false);
    expect(patches(backend.calls).some((call) => patchBody(call)['ui.setup_dismissed'] === true)).toBe(true);
  });
});

/* ---- 8. the step-2 gate -------------------------------------------------- */

describe('8. the step-2 gate advances on "tested", not on "tested OK"', () => {
  it('enables Continue after the probe ran, and disables it on an edit', async () => {
    const prose =
      'the MQTT broker at core-mosquitto.local:1883 refused the connection — check that the broker is running and that mqtt.port is the port it listens on';
    const backend = fakeBackend({
      haTest: HA_OK,
      mqttTest: { ok: false, broker_version: null, error: prose, auth_failed: false }
    });
    await settings.load();
    renderWizard();

    await passStepOne();

    expect(buttonByText('Continue').disabled).toBe(true);

    buttonByText('Test MQTT').click();
    await vi.waitFor(() => expect(pageText()).toContain(prose));

    expect(buttonByText('Continue').disabled).toBe(false);
    expect(backend.calls.filter((call) => call.path === '/api/settings/test/mqtt')).toHaveLength(1);

    // The gate is the edit, not the outcome: a climb down from a dead end must
    // still re-arm itself once the broker changes.
    type(inputByLabel('Broker host'), 'other-broker.local');
    expect(buttonByText('Continue').disabled).toBe(true);
  });
});

/* ---- 9. the one-tap escape (HSD-h44d) ------------------------------------ */

describe('9. the wizard has the same one-tap escape', () => {
  it('renders the escape in the sticky bar, outside the step content', async () => {
    fakeBackend({ settings: { 'ha.base_url': '' } });
    await settings.load();
    renderWizard();

    const bar = mustFind('[data-testid="shell-bar"]');
    const escape = mustFind<HTMLAnchorElement>('[data-testid="back-to-dashboard"]');
    expect(escape.closest('[data-testid="shell-bar"]')).toBe(bar);
    expect(escape.getAttribute('href')).toBe('/');
    expect(escape.getAttribute('aria-label')).toBe('Back to dashboard');
    expect(escape.textContent?.trim()).toBe('Dashboard');
    // The progress row is content, not chrome: it must stay out of the bar.
    expect(pageText()).toContain('Step 1 of 3');
    expect(bar.textContent).not.toContain('Step 1 of 3');
  });

  it('saves the dismissal before it navigates', async () => {
    const backend = fakeBackend({ settings: { 'ha.base_url': '' } });
    await settings.load();
    spyNavigation(backend.calls);
    renderWizard();

    const escape = mustFind<HTMLAnchorElement>('[data-testid="back-to-dashboard"]');
    escape.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }));

    await vi.waitFor(() => expect(navigations).toEqual(['/']));

    const saved = patches(backend.calls);
    expect(saved).toHaveLength(1);
    expect(patchBody(saved[0])).toEqual({ 'ui.setup_dismissed': true });
    // The order is the point: a navigation first would bounce `/` straight back
    // to `/setup`, which is the loop the saved dismissal exists to prevent.
    expect(backend.calls.map((call) => call.path)).toEqual([
      '/api/settings',
      '/api/settings',
      'location.assign:/'
    ]);
  });

  it('a failed save neither navigates nor hides the reason', async () => {
    const prose = 'the settings could not be saved because the database is read-only';
    const backend = fakeBackend({ settings: { 'ha.base_url': '' }, patchFails: prose });
    await settings.load();
    spyNavigation(backend.calls);
    renderWizard();

    const escape = mustFind<HTMLAnchorElement>('[data-testid="back-to-dashboard"]');
    escape.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }));

    await vi.waitFor(() => expect(pageText()).toContain(prose));
    expect(navigations).toEqual([]);
    expect(backend.calls.filter((call) => call.path.startsWith('location.assign:'))).toHaveLength(0);
    expect(pageText()).toContain('Connect to Home Assistant');
  });

  it('leaves a modified click to the browser and saves nothing', async () => {
    const backend = fakeBackend({ settings: { 'ha.base_url': '' } });
    await settings.load();
    spyNavigation(backend.calls);
    renderWizard();

    const escape = mustFind<HTMLAnchorElement>('[data-testid="back-to-dashboard"]');
    const modified = new MouseEvent('click', {
      bubbles: true,
      cancelable: true,
      button: 0,
      ctrlKey: true
    });
    escape.dispatchEvent(modified);
    flushSync();

    // A new tab (or window) is the browser's own move, so the guard must not run.
    expect(modified.defaultPrevented).toBe(false);
    expect(patches(backend.calls)).toHaveLength(0);
    expect(navigations).toEqual([]);
    expect(backend.calls.filter((call) => call.path.startsWith('location.assign:'))).toHaveLength(0);
  });
});

/* ---- 10. the broker credentials (HSD-9xpm) ------------------------------- */

describe('10. the wizard asks for the broker credentials, and they are optional', () => {
  it('offers them empty, passes anonymously, and keeps D26 on the secret', async () => {
    const backend = fakeBackend({ haTest: HA_OK, mqttTest: MQTT_OK });
    await settings.load();
    renderWizard();
    await passStepOne();

    const username = inputByLabel('Username');
    const password = inputByLabel('Password');
    expect(username.value).toBe('');
    expect(password.value).toBe('');
    expect(pageText()).toContain('Leave empty if the broker allows anonymous connections.');
    expect(pageText()).toContain('No password stored.');

    // An anonymous broker is a supported broker: a host alone is enough for
    // step 2 to probe, and the blank credentials still advance.
    const before = patches(backend.calls).length; // step 1's own save came first
    type(inputByLabel('Broker host'), 'broker.local');
    buttonByText('Test MQTT').click();
    await vi.waitFor(() => expect(buttonByText('Continue').disabled).toBe(false));

    const saved = patches(backend.calls).slice(before);
    expect(saved).toHaveLength(1);
    const body = patchBody(saved[0]);
    expect(body).toEqual({ 'mqtt.host': 'broker.local' });
    expect(Object.prototype.hasOwnProperty.call(body, 'mqtt.username')).toBe(false);
    expect(Object.prototype.hasOwnProperty.call(body, 'mqtt.password')).toBe(false);

    // D26, on the field that is a secret: masked, a visible reveal button, a
    // hint announced through aria-describedby, and the label above the field.
    expect(password.type).toBe('password');

    const field = password.closest('.field');
    expect(field).not.toBeNull();

    const reveal = mustFind<HTMLButtonElement>('button[aria-label="Show value"]');
    expect(reveal.tagName).toBe('BUTTON');
    expect(reveal.closest('.field')).toBe(field);
    expect(reveal.hidden).toBe(false);
    expect(reveal.getAttribute('aria-hidden')).toBeNull();

    const label = mustFind<HTMLLabelElement>(`label[for="${password.id}"]`);
    expect(label.closest('.field')).toBe(field);
    expect(label.compareDocumentPosition(password) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const describedby = password.getAttribute('aria-describedby');
    expect(describedby).not.toBeNull();
    expect(document.getElementById(describedby ?? '')?.textContent).toContain('No password stored.');
  });
});

/* ---- 11. the credentials are saved, and a rejection explains itself ------ */

describe('11. step 2 writes the credentials and an auth failure explains itself', () => {
  const credentialProse =
    'the MQTT broker at broker.local:1883 rejected the credentials (Not authorized) — check mqtt.username and mqtt.password in Settings';

  it('saves only its own keys once and never replays the password', async () => {
    const backend = fakeBackend({ haTest: HA_OK, mqttTest: MQTT_OK });
    await settings.load();
    renderWizard();
    await passStepOne();

    const before = patches(backend.calls).length; // step 1's own save came first
    type(inputByLabel('Broker host'), 'broker.local');
    type(inputByLabel('Username'), 'doorbell');
    type(inputByLabel('Password'), 'broker-secret');
    buttonByText('Test MQTT').click();
    await vi.waitFor(() => expect(buttonByText('Continue').disabled).toBe(false));

    const saved = patches(backend.calls).slice(before);
    expect(saved).toHaveLength(1);
    const body = patchBody(saved[0]);
    expect(body['mqtt.host']).toBe('broker.local');
    expect(body['mqtt.username']).toBe('doorbell');
    expect(body['mqtt.password']).toBe('broker-secret');
    expect(Object.keys(body).filter((key) => key.startsWith('ha.'))).toEqual([]);

    // The backend stored the secret and returned only the flag, so the echo
    // could never re-fill the field: it holds exactly what the user typed.
    expect(backend.secrets['mqtt.password']).toBe(true);
    expect(backend.settings['mqtt.password']).toBeUndefined();
    expect(inputByLabel('Password').value).toBe('broker-secret');

    // A fresh mount with a stored password starts EMPTY — there is no stored
    // value to render, only a hint saying one exists.
    unmountAll();
    settings.reset();
    fakeBackend({ haTest: HA_OK, secrets: { 'mqtt.password': true } });
    await settings.load();
    renderWizard();
    await passStepOne();

    expect(inputByLabel('Password').value).toBe('');
    expect(pageText()).toContain('A password is stored. Type a new one to replace it.');
  });

  it('locks Continue again when a credential is edited after the pass', async () => {
    fakeBackend({ haTest: HA_OK, mqttTest: MQTT_OK });
    await settings.load();
    renderWizard();
    await passStepOne();

    type(inputByLabel('Broker host'), 'broker.local');
    type(inputByLabel('Username'), 'doorbell');
    type(inputByLabel('Password'), 'broker-secret');
    buttonByText('Test MQTT').click();
    await vi.waitFor(() => expect(buttonByText('Continue').disabled).toBe(false));

    // The signature covers the credentials, so a corrected username is a new
    // question and the gate re-arms.
    type(inputByLabel('Username'), 'other-user');
    expect(buttonByText('Continue').disabled).toBe(true);
  });

  it('still advances on a rejected credential, and step 3 explains why', async () => {
    fakeBackend({
      haTest: HA_OK,
      mqttTest: { ok: false, broker_version: null, error: credentialProse, auth_failed: true }
    });
    await settings.load();
    renderWizard();
    await passStepOne();

    type(inputByLabel('Broker host'), 'broker.local');
    type(inputByLabel('Username'), 'doorbell');
    type(inputByLabel('Password'), 'wrong-secret');
    buttonByText('Test MQTT').click();

    // Tested, not tested OK: the wizard must never strand a user on step 2.
    await vi.waitFor(() => expect(pageText()).toContain(credentialProse));
    expect(buttonByText('Continue').disabled).toBe(false);

    buttonByText('Continue').click();
    flushSync();

    expect(stepText()).toBe('Step 3 of 3');
    const notice = mustFind('[data-setup-broker-problem]');
    expect(notice.getAttribute('role')).toBe('alert');
    const noticeText = textOf(notice);
    expect(noticeText).toContain('Credentials rejected');
    expect(noticeText).toContain(credentialProse);
    expect(noticeText).toContain(
      'A chime reaches Home Assistant through the broker, so it could not be announced until this connection works.'
    );

    // The escape stays open: adding a chime is still allowed.
    const chime = buttonByText('Add a first chime');
    expect(chime.disabled).toBe(false);
  });

  it('names a failure that is not about credentials with its own word', async () => {
    fakeBackend({
      haTest: HA_OK,
      mqttTest: {
        ok: false,
        broker_version: null,
        error: 'the MQTT broker at broker.local:1883 refused the connection',
        auth_failed: false
      }
    });
    await settings.load();
    renderWizard();
    await passStepOne();

    type(inputByLabel('Broker host'), 'broker.local');
    buttonByText('Test MQTT').click();
    await vi.waitFor(() => expect(buttonByText('Continue').disabled).toBe(false));

    buttonByText('Continue').click();
    flushSync();

    const noticeText = textOf(mustFind('[data-setup-broker-problem]'));
    expect(noticeText).toContain('Broker not connected');
    expect(noticeText).not.toContain('Credentials rejected');
  });
});
