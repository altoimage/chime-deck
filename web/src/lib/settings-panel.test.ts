// @vitest-environment happy-dom
/**
 * DOM tests for the settings shell and the Connection panel (HSD-bq76).
 *
 * Fully offline: every request goes through `setFetchImpl`, so no test opens a
 * socket or a browser. `happy-dom` provides the DOM per file; the default
 * `node` environment (and the rest of the suite) is untouched.
 *
 * The tests are numbered to map 1:1 onto the ticket's acceptance criteria.
 * They prove semantics — the word rendered, the key in the patch, the request
 * made — not layout; the D26 sizing lives in the styles and the contrast gate
 * checks the tokens.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { setFetchImpl, type FetchLike, type FetchResponse } from './api.js';
import ConnectionPanel from './settings/ConnectionPanel.svelte';
import SettingsLayout from './settings/SettingsLayout.svelte';
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
  secrets?: Record<string, boolean>;
  haTest?: unknown;
  mqttTest?: unknown;
  republish?: unknown;
}

interface Backend {
  calls: FakeCall[];
  settings: Record<string, unknown>;
  secrets: Record<string, boolean>;
}

/**
 * A tiny in-memory settings backend: GET echoes the state, PATCH applies the
 * diff and echoes the whole snapshot (a secret `null` clears it, a string
 * stores it), and the three POST endpoints answer whatever the test set.
 */
function fakeBackend(options: BackendOptions = {}): Backend {
  const storedSettings: Record<string, unknown> = { ...(options.settings ?? {}) };
  const storedSecrets: Record<string, boolean> = { ...(options.secrets ?? {}) };

  const calls = fakeFetch((path, init) => {
    if (path === '/api/settings' && init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      for (const [key, value] of Object.entries(body)) {
        if (value === null) {
          delete storedSettings[key];
          storedSecrets[key] = false;
        } else {
          storedSettings[key] = value;
          if (key === 'ha.token' || key === 'mqtt.password') {
            storedSecrets[key] = true;
          }
        }
      }
      return jsonResponse({
        settings: { ...storedSettings },
        secrets: { ...storedSecrets }
      });
    }

    if (path === '/api/settings') {
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

    if (path === '/api/mqtt/republish' && init?.method === 'POST') {
      return jsonResponse(
        options.republish ?? { ok: false, announced: 0, queued: false, failed: 0, error: 'not stubbed' }
      );
    }

    throw new Error(`unexpected request ${path}`);
  });

  return { calls, settings: storedSettings, secrets: storedSecrets };
}

/* ---- DOM helpers --------------------------------------------------------- */

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function renderPanel(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(ConnectionPanel, { target }));
  flushSync();
  return target;
}

function renderLayout(pathname: string): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(SettingsLayout, { target, props: { pathname } }));
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

function patches(calls: FakeCall[]): FakeCall[] {
  return calls.filter((call) => call.path === '/api/settings' && call.init?.method === 'PATCH');
}

function patchBody(call: FakeCall): Record<string, unknown> {
  return JSON.parse(String(call.init?.body)) as Record<string, unknown>;
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

/* ---- 1. secrets are never read back -------------------------------------- */

describe('1. secrets are write-only', () => {
  it('shows a masked, empty token field and never renders the stored value', async () => {
    const leaked = 'SENTINEL-TOKEN-VALUE-NEVER-RENDERED';
    fakeBackend({
      settings: { 'ha.base_url': 'http://ha.local:8123', 'ha.token': leaked },
      secrets: { 'ha.token': true }
    });
    await settings.load();

    const root = renderPanel();
    const tokenInput = inputByLabel('Long-lived token');

    expect(tokenInput.type).toBe('password');
    expect(tokenInput.value).toBe('');
    expect(pageText()).toContain('A token is stored. Type a new one to replace it.');
    expect(root.innerHTML).not.toContain(leaked);
    expect(document.body.innerHTML).not.toContain(leaked);
  });
});

/* ---- 2. replace / untouched / clear ------------------------------------- */

describe('2. secret diffs', () => {
  async function freshPanel(options: BackendOptions): Promise<Backend> {
    unmountAll();
    settings.reset();
    const backend = fakeBackend(options);
    await settings.load();
    renderPanel();
    return backend;
  }

  it('sends only the typed replacement, no key when untouched, and null when clearing', async () => {
    // 2a — a replaced secret contributes its new value.
    let backend = await freshPanel({ secrets: { 'ha.token': true } });
    type(inputByLabel('Long-lived token'), 'new-token-value');
    buttonByText('Save').click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));
    expect(patchBody(patches(backend.calls)[0])).toEqual({ 'ha.token': 'new-token-value' });

    // 2b — an untouched secret contributes NO key.
    backend = await freshPanel({ secrets: { 'ha.token': true } });
    type(inputByLabel('Broker host'), 'broker.local');
    buttonByText('Save').click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));
    const untouched = patchBody(patches(backend.calls)[0]);
    expect(untouched).toEqual({ 'mqtt.host': 'broker.local' });
    expect(Object.prototype.hasOwnProperty.call(untouched, 'ha.token')).toBe(false);

    // 2c — clearing sends null, never an empty string.
    backend = await freshPanel({ secrets: { 'ha.token': true } });
    expect(buttonByText('Clear stored token')).toBeTruthy();
    buttonByText('Clear stored token').click();
    flushSync();
    buttonByText('Save').click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));
    const cleared = patchBody(patches(backend.calls)[0]);
    expect(cleared).toEqual({ 'ha.token': null });
    expect(cleared['ha.token']).not.toBe('');
  });
});

/* ---- 3. connection test success ----------------------------------------- */

describe('3. Home Assistant test success', () => {
  it('renders the returned version and entity count', async () => {
    const backend = fakeBackend({
      haTest: { ok: true, ha_version: '2024.10.1', entity_count: 42, error: null }
    });
    await settings.load();
    renderPanel();

    buttonByText('Test connection').click();

    await vi.waitFor(() => expect(pageText()).toContain('2024.10.1'));
    expect(pageText()).toContain('42');
    expect(pageText()).toContain('entities');
    expect(pageText()).toContain('Connected to Home Assistant');
    expect(backend.calls.filter((call) => call.path === '/api/settings/test/ha')).toHaveLength(1);
  });
});

/* ---- 4. connection test failure ----------------------------------------- */

describe('4. Home Assistant test failure', () => {
  it('renders the backend prose verbatim, with no generic wording', async () => {
    const prose =
      'Home Assistant rejected the token (401 Unauthorized) — create a new long-lived token and paste it here';
    fakeBackend({
      haTest: { ok: false, ha_version: null, entity_count: null, error: prose }
    });
    await settings.load();
    renderPanel();

    buttonByText('Test connection').click();

    await vi.waitFor(() => expect(pageText()).toContain(prose));
    expect(pageText()).toContain('Not connected');
    expect(pageText()).not.toMatch(/test failed/i);
    expect(pageText()).not.toContain('could not be run');
  });
});

/* ---- 5. re-announce ------------------------------------------------------ */

describe('5. re-announce', () => {
  it('always shows the count, and renders the degraded prose verbatim', async () => {
    // 5a — the happy path still reports the server's count.
    let backend = fakeBackend({
      republish: { ok: true, announced: 3, queued: false, failed: 0, error: null }
    });
    await settings.load();
    renderPanel();
    buttonByText('Re-announce all buttons').click();
    await vi.waitFor(() => expect(pageText()).toContain('Announced 3 buttons to Home Assistant'));

    // 5b — queued + rejected: the count is still shown, with the prose verbatim.
    unmountAll();
    settings.reset();
    const prose = 'the broker refused the announcement — check mqtt.host and mqtt.port';
    backend = fakeBackend({
      republish: { ok: false, announced: 0, queued: true, failed: 1, error: prose }
    });
    await settings.load();
    renderPanel();
    buttonByText('Re-announce all buttons').click();
    await vi.waitFor(() => expect(pageText()).toContain(prose));
    expect(pageText()).toContain('Announced 0 buttons to Home Assistant');
    expect(pageText()).toContain('queued');
    // The badge must agree with the count it sits next to: a queued run is not
    // "Not announced", and the cause sentence is the server's, rendered as-is.
    expect(pageText()).toContain('Queued');
    expect(pageText()).not.toContain('Not announced');
    expect(pageText()).toContain('1 payload was not published');
    expect(pageText()).not.toContain('rejected by the broker');
    expect(backend.calls.filter((call) => call.path === '/api/mqtt/republish')).toHaveLength(1);
  });
});

/* ---- 6. per-section save ------------------------------------------------- */

describe('6. per-section save', () => {
  it('sends exactly one PATCH whose body holds only the changed keys', async () => {
    const backend = fakeBackend({
      settings: { 'mqtt.host': 'old-broker.local', 'mqtt.tls': false, 'mqtt.port': 1883 }
    });
    await settings.load();
    renderPanel();

    type(inputByLabel('Broker host'), 'new-broker.local');
    mustFind<HTMLButtonElement>('button[role="switch"]').click();
    flushSync();
    buttonByText('Save').click();

    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));
    const body = patchBody(patches(backend.calls)[0]);
    expect(body).toEqual({ 'mqtt.host': 'new-broker.local', 'mqtt.tls': true });
    expect(Object.keys(body).sort()).toEqual(['mqtt.host', 'mqtt.tls']);
    expect(backend.calls.filter((call) => call.init?.method === 'PATCH')).toHaveLength(1);
  });
});

/* ---- 7. nav + unknown deep link ----------------------------------------- */

describe('7. the settings shell', () => {
  it('lists exactly the implemented sections and pushStates the section path', async () => {
    const pushState = vi.spyOn(window.history, 'pushState');
    fakeBackend({});
    await settings.load();

    renderLayout('/settings');

    const links = [...document.querySelectorAll<HTMLAnchorElement>('nav[aria-label="Settings sections"] a')];
    expect(links).toHaveLength(10);
    expect(links[0].getAttribute('href')).toBe('/settings/connection');
    expect(links[0].getAttribute('aria-current')).toBe('page');
    expect(links[1].getAttribute('href')).toBe('/settings/security');
    expect(links[1].getAttribute('aria-current')).toBeNull();
    expect(links[2].getAttribute('href')).toBe('/settings/entities');
    expect(links[2].getAttribute('aria-current')).toBeNull();
    expect(links[3].getAttribute('href')).toBe('/settings/buttons');
    expect(links[3].getAttribute('aria-current')).toBeNull();
    expect(links[4].getAttribute('href')).toBe('/settings/sounds');
    expect(links[4].getAttribute('aria-current')).toBeNull();
    expect(links[5].getAttribute('href')).toBe('/settings/speech');
    expect(links[5].getAttribute('aria-current')).toBeNull();
    expect(links[6].getAttribute('href')).toBe('/settings/audio');
    expect(links[6].getAttribute('aria-current')).toBeNull();
    expect(links[7].getAttribute('href')).toBe('/settings/appearance');
    expect(links[7].getAttribute('aria-current')).toBeNull();
    expect(links[8].getAttribute('href')).toBe('/settings/history');
    expect(links[8].getAttribute('aria-current')).toBeNull();
    expect(links[9].getAttribute('href')).toBe('/settings/about');
    expect(links[9].getAttribute('aria-current')).toBeNull();

    links[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }));
    flushSync();

    expect(pushState).toHaveBeenCalledWith(null, '', '/settings/connection');
    expect(document.querySelector('[data-section="connection"]')).not.toBeNull();
    expect(pageText()).toContain('Home Assistant');
  });

  it('moves to the Entities section without a reload', async () => {
    const pushState = vi.spyOn(window.history, 'pushState');
    fakeBackend({});
    await settings.load();

    renderLayout('/settings');

    const entitiesLink = mustFind<HTMLAnchorElement>('nav[aria-label="Settings sections"] a[href="/settings/entities"]');
    entitiesLink.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }));
    flushSync();

    expect(pushState).toHaveBeenCalledWith(null, '', '/settings/entities');
    expect(document.querySelector('[data-section="entities"]')).not.toBeNull();
    expect(document.querySelector('[data-section="connection"]')).toBeNull();
    expect(entitiesLink.getAttribute('aria-current')).toBe('page');
  });

  it('shows the honest notice for a deep link to an unimplemented section', async () => {
    fakeBackend({});
    await settings.load();

    renderLayout('/settings/diagnostics');

    expect(pageText()).toContain('This settings section is not available in this build yet');
    expect(document.querySelector('[data-section="unknown"]')).not.toBeNull();
    expect(document.querySelector('[data-section="connection"]')).toBeNull();
    // No panel was mounted: no form fields, no test buttons.
    expect(document.querySelectorAll('input')).toHaveLength(0);
    const back = mustFind<HTMLAnchorElement>('main a[href="/settings/connection"]');
    expect(back.textContent?.trim()).toBe('Go to Connection');
  });
});

/* ---- 8. invalid port blocks the save ------------------------------------- */

describe('8. invalid port', () => {
  it('shows the inline validation message and sends no request', async () => {
    const backend = fakeBackend({ settings: { 'mqtt.port': 1883 } });
    await settings.load();
    renderPanel();

    type(inputByLabel('Broker port'), '0');

    expect(pageText()).toContain('the port must be a whole number between 1 and 65535');
    const save = buttonByText('Save');
    expect(save.disabled).toBe(true);

    save.click();
    flushSync();
    await Promise.resolve();
    expect(patches(backend.calls)).toHaveLength(0);
  });
});

/* ---- 9. the escape back to the dashboard (HSD-h44d) ---------------------- */

describe('9. the escape back to the dashboard', () => {
  it('renders one real Dashboard link on a section and on the unknown notice', async () => {
    // A wall panel in kiosk mode has no browser back button, so the shell must
    // carry its own way home on every route — including the honest notice a
    // hand-typed future deep link lands on.
    for (const pathname of ['/settings/audio', '/settings/diagnostics']) {
      unmountAll();
      settings.reset();
      fakeBackend({});
      await settings.load();

      renderLayout(pathname);

      const escapes = [...document.querySelectorAll<HTMLAnchorElement>('a[href="/"]')];
      expect(escapes, pathname).toHaveLength(1);
      expect(escapes[0].getAttribute('aria-label'), pathname).toBe('Back to dashboard');
      expect(escapes[0].textContent?.trim(), pathname).toBe('Dashboard');
      // In the sticky bar, so it is reachable without scrolling back up.
      expect(escapes[0].closest('[data-testid="shell-bar"]'), pathname).not.toBeNull();
    }
  });
});

/* ---- 10. a settings load landing after the first keystroke (HSD-8rwk) ---- */

describe('10. a late settings load', () => {
  it('a load that lands after the first keystroke keeps what was typed and still saves it', async () => {
    // This is the shape the browser tier hit: the shell loads the settings and
    // the panel does not wait, so the first keystroke can beat the load. A load
    // that replaced the whole draft threw the keystroke away, left the form
    // clean, and the Save click early-returned with no PATCH at all.
    const stored: Record<string, unknown> = {
      'ha.base_url': 'http://homeassistant.local:8123',
      'mqtt.host': 'stored-broker.local'
    };
    const typedUrl = 'http://typed-before-load.example:8123';
    let releaseLoad: () => void = () => {};
    const loadInFlight = new Promise<void>((resolve) => {
      releaseLoad = resolve;
    });

    const calls = fakeFetch(async (path, init) => {
      if (path === '/api/settings' && init?.method === 'PATCH') {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        for (const [key, value] of Object.entries(body)) {
          stored[key] = value;
        }
        return jsonResponse({ settings: { ...stored }, secrets: {} });
      }
      if (path === '/api/settings') {
        await loadInFlight;
        return jsonResponse({ settings: { ...stored }, secrets: {} });
      }
      throw new Error(`unexpected request ${path}`);
    });

    const loading = settings.load();
    renderPanel();
    expect(settings.loaded).toBe(false);

    type(inputByLabel('Home Assistant URL'), typedUrl);
    expect(buttonByText('Save').disabled).toBe(false);

    releaseLoad();
    await loading;
    await vi.waitFor(() => expect(settings.loaded).toBe(true));

    expect(inputByLabel('Home Assistant URL').value).toBe(typedUrl);
    expect(inputByLabel('Broker host').value).toBe('stored-broker.local');
    expect(buttonByText('Save').disabled).toBe(false);

    buttonByText('Save').click();
    await vi.waitFor(() => expect(patches(calls)).toHaveLength(1));
    expect(patchBody(patches(calls)[0])).toEqual({ 'ha.base_url': typedUrl });
  });
});

/* ---- 11. the shared MQTT field group (HSD-9xpm) -------------------------- */

describe('11. the MQTT card is the shared field group', () => {
  it('saves both credentials in one diff PATCH and says when one is stored', async () => {
    const backend = fakeBackend({ secrets: { 'mqtt.password': true } });
    await settings.load();
    renderPanel();

    // The extracted component owns the hint and the empty field, exactly as the
    // inline block did: there is no stored value to render.
    expect(inputByLabel('Password').value).toBe('');
    expect(pageText()).toContain('A password is stored. Type a new one to replace it.');

    type(inputByLabel('Username'), 'doorbell');
    type(inputByLabel('Password'), 'broker-secret');
    buttonByText('Save').click();

    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));
    expect(patchBody(patches(backend.calls)[0])).toEqual({
      'mqtt.username': 'doorbell',
      'mqtt.password': 'broker-secret'
    });
  });
});

/* ---- 12. the device name and the pinned instance id (HSD-vwdy) ----------- */

describe('12. the device name and the pinned instance id', () => {
  /**
   * A backend that pins like the server does: the first `device.name` write
   * derives `mqtt.instance_id` from it, in the same patch, and the echo carries
   * the whole snapshot — including the id the draft never sent.
   */
  function pinningBackend(stored: Record<string, unknown>): FakeCall[] {
    const slug = (name: string): string =>
      name
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '');

    return fakeFetch((path, init) => {
      if (path === '/api/settings' && init?.method === 'PATCH') {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        for (const [key, value] of Object.entries(body)) {
          stored[key] = value;
        }
        const name = body['device.name'];
        const pinned = stored['mqtt.instance_id'];
        if (typeof name === 'string' && !('mqtt.instance_id' in body) && pinned === undefined) {
          stored['mqtt.instance_id'] = slug(name);
        }
        return jsonResponse({ settings: { ...stored }, secrets: {} });
      }
      if (path === '/api/settings') {
        return jsonResponse({ settings: { ...stored }, secrets: {} });
      }
      throw new Error(`unexpected request ${path}`);
    });
  }

  it('renders the stored name, and names the id the unit publishes as', async () => {
    pinningBackend({ 'device.name': 'Hall Doorbell', 'mqtt.instance_id': 'hall-doorbell' });
    await settings.load();
    renderPanel();

    expect(inputByLabel('Device name').value).toBe('Hall Doorbell');
    expect(inputByLabel('Instance ID').value).toBe('hall-doorbell');
    expect(pageText()).toContain('This unit publishes as hall-doorbell');
    expect(pageText()).toContain('chime_deck_hall-doorbell_');
    // The field explains where the id came from, so renaming is not a gamble.
    expect(pageText()).toContain('derived from the device name when you first named it');
  });

  it('saves the name alone and adopts the pinned id from the echo', async () => {
    const calls = pinningBackend({});
    await settings.load();
    renderPanel();

    // An unnamed unit publishes as `default`, before anyone touches the field.
    expect(pageText()).toContain('This unit publishes as default');

    type(inputByLabel('Device name'), 'Hall Doorbell');
    expect(buttonByText('Save').disabled).toBe(false);
    buttonByText('Save').click();

    await vi.waitFor(() => expect(patches(calls)).toHaveLength(1));
    expect(patchBody(patches(calls)[0])).toEqual({ 'device.name': 'Hall Doorbell' });

    // The echo also pinned the instance id, and the form adopted it: a second
    // Save must be impossible, so no spurious `mqtt.instance_id` PATCH follows.
    await vi.waitFor(() => expect(inputByLabel('Instance ID').value).toBe('hall-doorbell'));
    flushSync();
    expect(buttonByText('Save').disabled).toBe(true);
    expect(patches(calls)).toHaveLength(1);
    expect(pageText()).toContain('This unit publishes as hall-doorbell');
  });

  it('leaves a pinned id alone on a later rename, sending only the name', async () => {
    const calls = pinningBackend({
      'device.name': 'ChimeDeck',
      'mqtt.instance_id': 'hall-doorbell'
    });
    await settings.load();
    renderPanel();

    type(inputByLabel('Device name'), 'Kitchen');
    buttonByText('Save').click();

    await vi.waitFor(() => expect(patches(calls)).toHaveLength(1));
    expect(patchBody(patches(calls)[0])).toEqual({ 'device.name': 'Kitchen' });

    // Clean again: the rename is the only key sent and the pinned id survived it.
    await vi.waitFor(() => expect(buttonByText('Save').disabled).toBe(true));
    expect(inputByLabel('Device name').value).toBe('Kitchen');
    expect(inputByLabel('Instance ID').value).toBe('hall-doorbell');
  });
});
