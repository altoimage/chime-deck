// @vitest-environment happy-dom
/**
 * DOM tests for the Audio settings panel (HSD-wpm0).
 *
 * Fully offline: every request goes through `setFetchImpl`, so discovery,
 * settings, the clock header and the audible test tone are all deterministic
 * fakes. The panel never opens a real audio device or a network connection.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { setFetchImpl, type FetchLike, type FetchResponse } from './api.js';
import { describeDrift, formatDeviceClock } from './device-clock.js';
import AudioPanel from './settings/AudioPanel.svelte';
import { settings } from './settings.svelte.js';
import { toastStore } from './ui/toast.svelte.js';

/* ---- fetch fake ---------------------------------------------------------- */

interface FakeCall {
  path: string;
  init?: RequestInit;
}

interface DeviceRow {
  name: string;
  hostapi: string;
  is_default: boolean;
}

const DEFAULT_DEVICES: DeviceRow[] = [
  { name: 'auto', hostapi: 'auto', is_default: true },
  { name: 'alsa/usb-audio', hostapi: 'alsa', is_default: false },
  { name: 'pipewire/analog-output', hostapi: 'pipewire', is_default: false }
];

function jsonResponse(body: unknown, status = 200, date: string | null = null): FetchResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    headers: { get: (name) => (name.toLowerCase() === 'date' ? date : null) }
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
  devices?: DeviceRow[];
  audioTest?: unknown;
  dateHeader?: string | null;
  patchError?: { status: number; body: unknown };
  discoveryError?: string;
}

interface Backend {
  calls: FakeCall[];
  settings: Record<string, unknown>;
}

/**
 * The in-memory backend echoes GET /api/settings, diffs and applies PATCH,
 * resolves a hint by a case-insensitive substring of device NAME only, and
 * fakes the test-tone and device-clock endpoints.
 */
function fakeBackend(options: BackendOptions = {}): Backend {
  const storedSettings: Record<string, unknown> = { ...(options.settings ?? {}) };
  const devices = options.devices ?? DEFAULT_DEVICES;
  const calls = fakeFetch((path, init) => {
    if (path === '/api/settings' && init?.method === 'PATCH') {
      if (options.patchError) {
        return jsonResponse(options.patchError.body, options.patchError.status);
      }
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      Object.assign(storedSettings, body);
      return jsonResponse({ settings: { ...storedSettings }, secrets: {} });
    }

    if (path === '/api/settings') {
      return jsonResponse({ settings: { ...storedSettings }, secrets: {} });
    }

    if (path === '/api/audio/devices') {
      if (options.discoveryError) {
        return jsonResponse({
          ok: false,
          hint: String(storedSettings['audio.device_hint'] ?? ''),
          resolved_device: null,
          system_default: 'auto',
          devices: [],
          error: options.discoveryError
        });
      }
      const hint = String(storedSettings['audio.device_hint'] ?? '').trim().toLowerCase();
      const resolved =
        hint === '' ? null : (devices.find((device) => device.name.toLowerCase().includes(hint))?.name ?? null);
      return jsonResponse({
        ok: true,
        hint: String(storedSettings['audio.device_hint'] ?? ''),
        resolved_device: resolved,
        system_default: 'auto',
        devices,
        error: null
      });
    }

    if (path === '/api/settings/test/audio' && init?.method === 'POST') {
      return jsonResponse(
        options.audioTest ?? {
          ok: true,
          device: 'alsa/usb-audio',
          latency_ms: 42.5,
          error: null
        }
      );
    }

    if (path === '/api/health') {
      return jsonResponse(
        { status: 'ok', version: 'test', uptime_s: 1 },
        200,
        options.dateHeader === undefined ? new Date(Date.now()).toUTCString() : options.dateHeader
      );
    }

    throw new Error(`unexpected request ${path}`);
  });

  return { calls, settings: storedSettings };
}

/* ---- DOM helpers --------------------------------------------------------- */

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function renderPanel(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(AudioPanel, { target }));
  flushSync();
  return target;
}

function unmountAll(): void {
  for (const component of mounted.splice(0)) {
    unmount(component);
  }
  document.body.innerHTML = '';
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

function selectTrigger(label: string): HTMLButtonElement {
  const button = [...document.querySelectorAll<HTMLButtonElement>('button.trigger')].find((candidate) => {
    const ids = candidate.getAttribute('aria-labelledby')?.split(/\s+/) ?? [];
    return document.getElementById(ids[0] ?? '')?.textContent?.trim() === label;
  });
  if (button === undefined) {
    throw new Error(`expected a Select trigger labelled "${label}"`);
  }
  return button;
}

function chooseRadio(value: string): void {
  const radio = [...document.querySelectorAll<HTMLInputElement>('input[type="radio"]')].find(
    (candidate) => candidate.value === value
  );
  if (radio === undefined) {
    throw new Error(`expected a Select option with value "${value}"`);
  }
  radio.click();
  flushSync();
}

function pageText(): string {
  return (document.body.textContent ?? '').replace(/\s+/g, ' ').trim();
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

async function loadAndRender(options: BackendOptions = {}): Promise<Backend> {
  settings.reset();
  const backend = fakeBackend(options);
  await settings.load();
  renderPanel();
  await vi.waitFor(() => expect(pathCalls(backend.calls, '/api/audio/devices')).toHaveLength(1));
  await vi.waitFor(() => expect(document.querySelectorAll('button.trigger')).toHaveLength(2));
  await vi.waitFor(() => expect(document.querySelector('.clock-readings')).not.toBeNull());
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
  vi.useRealTimers();
});

/* ---- AC1: device selection and persistence ------------------------------- */

describe('AC1 audio device selection', () => {
  it('stores the selected full device name in exactly one diff PATCH', async () => {
    const backend = await loadAndRender();

    selectTrigger('Audio output').click();
    flushSync();
    chooseRadio('alsa/usb-audio');
    expect(selectTrigger('Audio output').textContent).toContain('alsa/usb-audio');

    buttonByText('Save').click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));
    expect(patchBody(patches(backend.calls)[0])).toEqual({ 'audio.device_hint': 'alsa/usb-audio' });
    expect(pathCalls(backend.calls, '/api/settings')).toHaveLength(2);
    expect(pathCalls(backend.calls, '/api/settings/test/audio')).toHaveLength(0);
  });

  it('keeps the saved device on remount and resolves a lowercase stored substring', async () => {
    const backend = await loadAndRender();
    selectTrigger('Audio output').click();
    flushSync();
    chooseRadio('alsa/usb-audio');
    buttonByText('Save').click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));
    await vi.waitFor(() => expect(pathCalls(backend.calls, '/api/audio/devices')).toHaveLength(2));

    unmountAll();
    settings.reset();
    await settings.load();
    renderPanel();
    await vi.waitFor(() => expect(pathCalls(backend.calls, '/api/audio/devices')).toHaveLength(3));
    await vi.waitFor(() => expect(document.querySelectorAll('button.trigger')).toHaveLength(2));
    expect(selectTrigger('Audio output').textContent).toContain('alsa/usb-audio');
    expect(pageText()).toContain('resolved');
    expect(patches(backend.calls)).toHaveLength(1);

    unmountAll();
    backend.settings['audio.device_hint'] = 'usb-audio';
    settings.reset();
    await settings.load();
    renderPanel();
    await vi.waitFor(() => expect(pathCalls(backend.calls, '/api/audio/devices')).toHaveLength(4));
    await vi.waitFor(() => expect(document.querySelectorAll('button.trigger')).toHaveLength(2));
    expect(backend.settings['audio.device_hint']).toBe('usb-audio');
    expect(selectTrigger('Audio output').textContent).toContain('alsa/usb-audio');
    const resolvedLines = [
      ...document.querySelectorAll<HTMLElement>(
        '[aria-label="The audio output this device resolves to"]'
      )
    ];
    expect(resolvedLines).toHaveLength(1);
    expect(resolvedLines[0].querySelector('.badge')?.textContent?.trim()).toBe('resolved');
    expect(resolvedLines[0].textContent).toContain('alsa/usb-audio');
    expect(resolvedLines[0].textContent).toContain('alsa');

    selectTrigger('Audio output').click();
    flushSync();
    const resolvedOption = [
      ...document.querySelectorAll<HTMLLabelElement>(
        'dialog .options[aria-label="Audio output"] label.row'
      )
    ].find(
      (row) => row.querySelector('.row-label')?.textContent?.trim() === 'alsa/usb-audio'
    );
    expect(resolvedOption).toBeDefined();
    expect(resolvedOption?.querySelector('.row-description')?.textContent).toContain(
      'alsa · resolved now'
    );
    expect(pageText()).not.toContain('matches no discovered output');
    expect(patches(backend.calls)).toHaveLength(1);
  });
});

/* ---- AC2: actual test-tone result --------------------------------------- */

describe('AC2 audio test tone', () => {
  it('renders the returned device and measured latency after one POST', async () => {
    const backend = await loadAndRender();

    const playButton = buttonByText('Play test tone');
    playButton.click();
    flushSync();
    expect(playButton.disabled).toBe(true);
    expect(playButton.getAttribute('aria-busy')).toBe('true');
    await vi.waitFor(() => expect(pageText()).toContain('Played on alsa/usb-audio in 42.5 ms'));

    expect(pageText()).toContain('Test tone played');
    expect(pathCalls(backend.calls, '/api/settings/test/audio')).toHaveLength(1);
    expect(patches(backend.calls)).toHaveLength(0);
  });

  it('explains an unmeasurable start delay and shows the hint in the test card', async () => {
    const backend = await loadAndRender({
      audioTest: { ok: true, device: 'alsa/usb-audio', latency_ms: null, error: null }
    });

    buttonByText('Play test tone').click();
    await vi.waitFor(() => expect(pageText()).toContain('Played on alsa/usb-audio'));

    expect(pageText()).toContain('alsa/usb-audio');
    expect(pageText()).toContain('could not be timed');
    expect(pageText()).toContain('shorter than the tone');
    expect(pageText()).not.toContain('failed');
    expect(pageText()).not.toContain('in 0 ms');
    expect(pageText()).not.toContain('in 0.0 ms');
    // The test result line carries the explanation, not a bare dash or a blank.
    const testLine = document.querySelector<HTMLElement>('.test-result p');
    expect(testLine).not.toBeNull();
    const testLineText = (testLine?.textContent ?? '').replace(/\s+/g, ' ').trim();
    expect(testLineText).toContain('could not be timed');
    expect(testLineText).not.toBe('—');
    expect(testLineText).not.toBe('');
    const testCard = [...document.querySelectorAll<HTMLElement>('.card')].find(
      (card) => card.querySelector('.card-title')?.textContent?.trim() === 'Test'
    );
    expect(testCard?.textContent).toContain(
      'If the tone finishes before it can be heard, the start delay is below what this test can resolve and no number is shown.'
    );
    expect(pathCalls(backend.calls, '/api/settings/test/audio')).toHaveLength(1);
    expect(patches(backend.calls)).toHaveLength(0);
  });

  it('shows failed prose verbatim without claiming a device or latency', async () => {
    const prose = 'the output stream was refused by the audio engine — no tone was played';
    const backend = await loadAndRender({
      audioTest: { ok: false, device: null, latency_ms: null, error: prose }
    });

    buttonByText('Play test tone').click();
    await vi.waitFor(() => expect(pageText()).toContain(prose));

    expect(pageText()).toContain('Test failed');
    expect(pageText()).toContain('Test again');
    expect(pageText()).not.toContain('Played on');
    expect(pageText()).not.toContain('in 42.5 ms');
    expect(pathCalls(backend.calls, '/api/settings/test/audio')).toHaveLength(1);
    expect(patches(backend.calls)).toHaveLength(0);
  });
});

/* ---- AC3: quiet-hours controls and server validation -------------------- */

describe('AC3 quiet hours', () => {
  it('steps to 23:00 and 06:30 and saves only the two changed times', async () => {
    const backend = await loadAndRender();

    buttonByAriaLabel('Increase Quiet hours start hour').click();
    flushSync();
    buttonByAriaLabel('Increase Quiet hours start minute').click();
    flushSync();
    buttonByAriaLabel('Decrease Quiet hours start minute').click();
    flushSync();
    buttonByAriaLabel('Decrease Quiet hours end hour').click();
    flushSync();
    for (let minute = 0; minute < 30; minute += 1) {
      buttonByAriaLabel('Increase Quiet hours end minute').click();
      flushSync();
    }
    flushSync();

    expect(pageText()).toContain('23:00 → 06:30, next day');
    buttonByText('Save').click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));
    expect(patchBody(patches(backend.calls)[0])).toEqual({
      'audio.quiet_hours_start': '23:00',
      'audio.quiet_hours_end': '06:30'
    });
    expect(pathCalls(backend.calls, '/api/settings')).toHaveLength(2);
    expect(patches(backend.calls)).toHaveLength(1);
  });

  it('shows the 422 message verbatim and keeps the uncoerced draft visible', async () => {
    const message = 'setting audio.quiet_hours_start must be a 24-hour time such as 22:00';
    const backend = await loadAndRender({
      patchError: {
        status: 422,
        body: {
          error: {
            code: 'invalid_setting',
            message,
            detail: { fields: [{ key: 'audio.quiet_hours_start' }] }
          }
        }
      }
    });

    buttonByAriaLabel('Increase Quiet hours start hour').click();
    flushSync();
    buttonByText('Save').click();
    await vi.waitFor(() => expect(document.querySelector('[role="alert"]')?.textContent).toContain(message));

    expect(document.querySelector('[aria-label="Save this section"] [role="alert"]')?.textContent).toContain(message);
    expect(pageText()).toContain('23:00 → 07:00, next day');
    expect(backend.settings['audio.quiet_hours_start']).toBeUndefined();
    expect(patches(backend.calls)).toHaveLength(1);
    expect(patchBody(patches(backend.calls)[0])).toEqual({ 'audio.quiet_hours_start': '23:00' });
  });
});

/* ---- AC4: the device's HTTP Date header ------------------------------- */

describe('AC4 device clock strip', () => {
  it('compares the device Date header with the browser clock and describes 47 seconds of drift', async () => {
    const browserMs = Date.UTC(2026, 10, 12, 21, 14, 54);
    vi.spyOn(Date, 'now').mockReturnValue(browserMs);
    const deviceMs = browserMs - 47_000;
    const backend = await loadAndRender({ dateHeader: new Date(deviceMs).toUTCString() });

    await vi.waitFor(() => expect(pageText()).toContain('the device clock is 47 s behind this browser'));
    expect(pageText()).toContain(`Device time ${formatDeviceClock(deviceMs, 0)}`);
    expect(pageText()).toContain(`this browser ${formatDeviceClock(browserMs, 0)}`);
    expect(pageText()).toContain('HTTP Date response header');
    expect(pageText()).toContain('good to ±1 s');
    expect(pathCalls(backend.calls, '/api/health')).toHaveLength(1);
  });

  it('ticks both clocks from one reading and clears the interval on unmount', async () => {
    let browserMs = Date.UTC(2026, 10, 12, 21, 14, 54);
    vi.spyOn(Date, 'now').mockImplementation(() => browserMs);
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const deviceMs = browserMs - 47_000;
    const backend = await loadAndRender({ dateHeader: new Date(deviceMs).toUTCString() });

    expect(pageText()).toContain(`Device time ${formatDeviceClock(deviceMs, 0)}`);
    expect(pageText()).toContain(`this browser ${formatDeviceClock(browserMs, 0)}`);
    browserMs += 1000;
    vi.advanceTimersByTime(1000);
    flushSync();
    expect(pageText()).toContain(`Device time ${formatDeviceClock(deviceMs, 1000)}`);
    expect(pageText()).toContain(`this browser ${formatDeviceClock(browserMs, 0)}`);
    expect(pathCalls(backend.calls, '/api/health')).toHaveLength(1);

    unmountAll();
    browserMs += 1000;
    vi.advanceTimersByTime(1000);
    expect(pathCalls(backend.calls, '/api/health')).toHaveLength(1);
  });

  it('shows the browser clock and says the device did not report its clock when Date is absent', async () => {
    const browserMs = Date.UTC(2026, 10, 12, 21, 14, 54);
    vi.spyOn(Date, 'now').mockReturnValue(browserMs);
    const backend = await loadAndRender({ dateHeader: null });

    await vi.waitFor(() => expect(pageText()).toContain('did not report its clock'));
    expect(pageText()).toContain(`this browser ${formatDeviceClock(browserMs, 0)}`);
    expect(pageText()).not.toContain('Device time');
    expect(pageText()).not.toContain(describeDrift(-47));
    expect(pathCalls(backend.calls, '/api/health')).toHaveLength(1);
  });
});

/* ---- Behaviour guards --------------------------------------------------- */

describe('audio behaviour guards', () => {
  it('offers only the selectable mpv backend and does not save or test on selection', async () => {
    const backend = await loadAndRender({ settings: { 'audio.backend': 'mpv' } });
    expect(selectTrigger('Audio backend').textContent).toContain('mpv (libmpv)');

    selectTrigger('Audio backend').click();
    flushSync();
    const rows = [
      ...document.querySelectorAll<HTMLElement>('dialog .options[aria-label="Audio backend"] .row')
    ];
    expect(rows).toHaveLength(1);
    expect(rows.map((row) => row.textContent?.trim())).toEqual(['mpv (libmpv)']);
    expect(rows.some((row) => row.textContent?.includes('sounddevice'))).toBe(false);

    const mpvRow = rows[0];
    const radio = mpvRow.querySelector<HTMLInputElement>('input[type="radio"]');
    expect(radio).not.toBeNull();
    expect(radio?.disabled).toBe(false);
    expect(mpvRow.getAttribute('aria-disabled')).toBeNull();
    mpvRow.click();
    flushSync();

    expect(selectTrigger('Audio backend').textContent).toContain('mpv (libmpv)');
    expect(backend.settings['audio.backend']).toBe('mpv');
    expect(patches(backend.calls)).toHaveLength(0);
    expect(pathCalls(backend.calls, '/api/settings/test/audio')).toHaveLength(0);
  });

  it('renders each discovered device exactly once as a selectable picker row', async () => {
    const devices: DeviceRow[] = [
      { name: 'auto', hostapi: 'auto', is_default: true },
      { name: 'alsa/usb-audio', hostapi: 'alsa', is_default: false },
      { name: 'pulse/desk-speaker', hostapi: 'pulse', is_default: false },
      { name: 'pipewire/analog-output', hostapi: 'pipewire', is_default: false }
    ];
    await loadAndRender({ devices });

    selectTrigger('Audio output').click();
    flushSync();
    const rows = [
      ...document.querySelectorAll<HTMLLabelElement>(
        'dialog .options[aria-label="Audio output"] label.row'
      )
    ];
    expect(rows).toHaveLength(4);
    expect(rows.map((row) => row.querySelector('.row-label')?.textContent?.trim())).toEqual([
      'System default',
      'alsa/usb-audio',
      'pulse/desk-speaker',
      'pipewire/analog-output'
    ]);

    const renderedText = pageText();
    for (const device of devices.filter((candidate) => candidate.name !== 'auto')) {
      expect(
        rows.filter((row) => row.querySelector('.row-label')?.textContent?.trim() === device.name)
      ).toHaveLength(1);
      expect(renderedText.split(device.name)).toHaveLength(2);
    }
  });

  it('mutes to zero and restores the last non-zero draft after the saved mute', async () => {
    const backend = await loadAndRender({ settings: { 'audio.volume': 55 } });

    buttonByText('Mute').click();
    flushSync();
    expect(document.querySelector('.volume-field')?.textContent).toContain('0%');
    buttonByText('Save').click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(1));
    await vi.waitFor(() =>
      expect(document.querySelector('[aria-label="Save this section"] button')?.getAttribute('aria-busy')).toBe('false')
    );
    expect(patchBody(patches(backend.calls)[0])).toEqual({ 'audio.volume': 0 });

    buttonByText('Unmute').click();
    flushSync();
    expect(document.querySelector('.volume-field')?.textContent).toContain('55%');
    buttonByText('Save').click();
    await vi.waitFor(() => expect(patches(backend.calls)).toHaveLength(2));
    await vi.waitFor(() =>
      expect(document.querySelector('[aria-label="Save this section"] button')?.getAttribute('aria-busy')).toBe('false')
    );
    expect(patchBody(patches(backend.calls)[1])).toEqual({ 'audio.volume': 55 });
    expect(pathCalls(backend.calls, '/api/settings/test/audio')).toHaveLength(0);
  });

  it('clamps master volume at 0 and 100 and has no dead self-test control', async () => {
    const backend = await loadAndRender({ settings: { 'audio.volume': 100 } });
    expect(buttonByAriaLabel('Increase Volume').disabled).toBe(true);
    buttonByText('Mute').click();
    flushSync();
    expect(buttonByAriaLabel('Decrease Volume').disabled).toBe(true);
    buttonByAriaLabel('Decrease Volume').click();
    flushSync();

    expect(pageText()).toContain('0%');
    expect(pageText()).not.toContain('Self-test');
    expect(patches(backend.calls)).toHaveLength(0);
    expect(pathCalls(backend.calls, '/api/settings/test/audio')).toHaveLength(0);
  });

  it('renders a failed discovery sentence verbatim with a working Retry action', async () => {
    const prose = 'the audio engine has not started yet — output discovery is unavailable';
    const backend = await loadAndRender({ discoveryError: prose });
    expect(pageText()).toContain(prose);
    expect(buttonByText('Retry')).toBeTruthy();

    buttonByText('Retry').click();
    await vi.waitFor(() => expect(pathCalls(backend.calls, '/api/audio/devices')).toHaveLength(2));
    await vi.waitFor(() => expect(pageText()).toContain(prose));
    expect(patches(backend.calls)).toHaveLength(0);
  });

  it('shows no fabricated device row while discovering, then explains an empty host', async () => {
    settings.reset();
    const backend = fakeBackend({ devices: [] });
    await settings.load();
    renderPanel();
    expect(pageText()).toContain('Discovering audio outputs');
    expect(document.querySelectorAll('button.trigger')).toHaveLength(1);

    await vi.waitFor(() => expect(pageText()).toContain('No audio outputs were discovered on this host'));
    expect(pageText()).toContain('System default (auto)');
    expect(pathCalls(backend.calls, '/api/audio/devices')).toHaveLength(1);
    expect(patches(backend.calls)).toHaveLength(0);
  });
});
