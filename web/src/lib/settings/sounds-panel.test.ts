// @vitest-environment happy-dom
/**
 * Acceptance tests for the Sounds settings panel (HSD-w9eq).
 *
 * Fully offline: every request is answered by the in-memory sounds server
 * through `setFetchImpl`. The browser preview stubs media playback; no test
 * fetches a URL, streams real audio, or touches an audio device.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  setFetchImpl,
  type AffectedButton,
  type FetchLike,
  type FetchResponse,
  type SoundFromUrlResponse,
  type SoundRescanResponse,
  type SoundRow
} from '../api.js';
import { formatSoundDuration } from '../sound-library.js';
import { settings } from '../settings.svelte.js';
import { toastStore } from '../ui/toast.svelte.js';
import SoundsPanel from './SoundsPanel.svelte';

/* ---- in-memory sounds server -------------------------------------------- */

interface FakeCall {
  path: string;
  init?: RequestInit;
}

interface BackendOptions {
  rows?: SoundRow[];
  rescan?: SoundRescanResponse;
  rescanAdd?: SoundRow | null;
  rescanError?: string | null;
  rescanGate?: Promise<void> | null;
  fromUrl?: SoundFromUrlResponse;
  fromUrlError?: { status: number; message: string } | null;
  renameError?: { status: number; message: string } | null;
  referenced?: Record<number, AffectedButton[]>;
  conflictMessage?: string;
  listError?: string | null;
  listGate?: Promise<void> | null;
}

interface Backend {
  calls: FakeCall[];
  rows: SoundRow[];
  options: BackendOptions;
}

function soundRow(overrides: Partial<SoundRow> = {}): SoundRow {
  return {
    id: 1,
    name: 'Front Door chime',
    source_type: 'file',
    path: 'data/sounds/front-door.mp3',
    url: null,
    sha256: 'abc123',
    format: 'mp3',
    duration_ms: 1800,
    bytes: 24000,
    missing: false,
    created_at: '2026-11-12T00:00:00Z',
    ...overrides
  };
}

function jsonResponse(body: unknown, status = 200): FetchResponse {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function errorResponse(status: number, message: string, detail: unknown = null): FetchResponse {
  return jsonResponse({ error: { code: status === 409 ? 'conflict' : 'test_error', message, detail } }, status);
}

function fakeBackend(options: BackendOptions = {}): Backend {
  const backend: Backend = {
    calls: [],
    rows: (options.rows ?? [soundRow()]).map((row) => ({ ...row })),
    options
  };

  const impl: FetchLike = async (path, init) => {
    backend.calls.push({ path, init });
    const method = init?.method ?? 'GET';

    if (path === '/api/sounds' && method === 'GET') {
      if (backend.options.listGate !== null && backend.options.listGate !== undefined) {
        await backend.options.listGate;
      }
      if (backend.options.listError) {
        return errorResponse(500, backend.options.listError);
      }
      return jsonResponse(backend.rows.map((row) => ({ ...row })));
    }

    if (path === '/api/sounds/rescan' && method === 'POST') {
      if (backend.options.rescanGate !== null && backend.options.rescanGate !== undefined) {
        await backend.options.rescanGate;
      }
      if (backend.options.rescanError) {
        return errorResponse(500, backend.options.rescanError);
      }
      const addedRow = backend.options.rescanAdd;
      if (addedRow && !backend.rows.some((row) => row.id === addedRow.id)) {
        backend.rows.push({ ...addedRow });
      }
      return jsonResponse(backend.options.rescan ?? { added: 0, removed: 0, missing: 0 });
    }

    if (path === '/api/sounds/from_url' && method === 'POST') {
      if (backend.options.fromUrlError) {
        return errorResponse(backend.options.fromUrlError.status, backend.options.fromUrlError.message);
      }
      const body = JSON.parse(String(init?.body)) as { url: string };
      const result = backend.options.fromUrl ?? {
        created: true,
        sound: soundRow({
          id: Math.max(0, ...backend.rows.map((row) => row.id)) + 1,
          name: 'Downloaded ding',
          source_type: 'url',
          path: null,
          url: body.url,
          duration_ms: 1800,
          format: 'mp3'
        })
      };
      if (!backend.rows.some((row) => row.id === result.sound.id)) {
        backend.rows.push({ ...result.sound });
      }
      return jsonResponse(result);
    }

    const patchMatch = /^\/api\/sounds\/(\d+)$/.exec(path);
    if (patchMatch !== null && method === 'PATCH') {
      if (backend.options.renameError) {
        return errorResponse(backend.options.renameError.status, backend.options.renameError.message);
      }
      const id = Number(patchMatch[1]);
      const index = backend.rows.findIndex((row) => row.id === id);
      if (index < 0) {
        return errorResponse(404, `there is no sound with the id ${id}`);
      }
      const body = JSON.parse(String(init?.body)) as { name: string };
      backend.rows[index] = { ...backend.rows[index], name: body.name };
      return jsonResponse({ ...backend.rows[index] });
    }

    const deleteMatch = /^\/api\/sounds\/(\d+)(\?confirm=true)?$/.exec(path);
    if (deleteMatch !== null && method === 'DELETE') {
      const id = Number(deleteMatch[1]);
      const confirmed = deleteMatch[2] === '?confirm=true';
      const affected = backend.options.referenced?.[id] ?? [];
      if (affected.length > 0 && !confirmed) {
        const message =
          backend.options.conflictMessage ??
          `sound ${id} is used by ${affected.length} buttons — resend with ?confirm=true to delete the sound and those buttons, or re-point the buttons at another sound first; they are deleted, not disabled`;
        return errorResponse(409, message, { affected_buttons: affected, requires: 'confirm', count: affected.length });
      }
      backend.rows = backend.rows.filter((row) => row.id !== id);
      return jsonResponse({ affected_buttons: affected });
    }

    throw new Error(`unexpected request ${method} ${path}`);
  };

  setFetchImpl(impl);
  return backend;
}

/* ---- DOM harness -------------------------------------------------------- */

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function renderPanel(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(SoundsPanel, { target }));
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
    (candidate) => (candidate.textContent ?? '').replace(/\s+/g, ' ').trim() === label
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

function typeInto(field: HTMLInputElement, value: string): void {
  field.value = value;
  field.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function pageText(): string {
  return (document.body.textContent ?? '').replace(/\s+/g, ' ').trim();
}

function callsTo(calls: FakeCall[], path: string, method = 'GET'): FakeCall[] {
  return calls.filter((call) => call.path === path && (call.init?.method ?? 'GET') === method);
}

function deleteCalls(calls: FakeCall[]): FakeCall[] {
  return calls.filter((call) => call.init?.method === 'DELETE');
}

function bodyOf(call: FakeCall): Record<string, unknown> {
  return JSON.parse(String(call.init?.body)) as Record<string, unknown>;
}

function rowFor(name: string): HTMLLIElement {
  const row = [...document.querySelectorAll<HTMLLIElement>('li.record')].find(
    (candidate) => candidate.querySelector('h3')?.textContent?.trim() === name
  );
  if (row === undefined) {
    throw new Error(`expected a sound record named "${name}"`);
  }
  return row;
}

function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve: () => void = () => {};
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
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

/* ---- AC1 ---------------------------------------------------------------- */

describe('1. rescan reports the added and removed counts', () => {
  it('posts once, reloads the newly found row, and explains the server counts', async () => {
    const rescanRow = soundRow({ id: 2, name: 'Hall chime', format: 'ogg' });
    const backend = fakeBackend({
      rescan: { added: 2, removed: 1, missing: 3 },
      rescanAdd: rescanRow
    });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door chime'));

    buttonByText('Rescan folder').click();
    await vi.waitFor(() => expect(callsTo(backend.calls, '/api/sounds/rescan', 'POST')).toHaveLength(1));
    await vi.waitFor(() => expect(pageText()).toContain('Hall chime'));

    expect(pageText()).toContain('added: 2 · removed: 1 · missing: 3');
    expect(pageText()).toContain('removed means newly flagged missing');
    expect(callsTo(backend.calls, '/api/sounds', 'GET')).toHaveLength(2);
    expect(toastStore.items.map((toast) => toast.message)).toContain('Sound folder rescanned.');
  });

  it('shows a failed rescan message verbatim and no result counts', async () => {
    const message = 'the folder scan could not read /data/sounds — check its permissions';
    const backend = fakeBackend({ rescanError: message });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door chime'));

    buttonByText('Rescan folder').click();
    await vi.waitFor(() => expect(document.querySelector('[role="alert"]')?.textContent).toContain(message));

    expect(document.querySelector('.rescan-result')).toBeNull();
    expect(callsTo(backend.calls, '/api/sounds/rescan', 'POST')).toHaveLength(1);
  });
});

/* ---- AC2 ---------------------------------------------------------------- */

describe('2. a URL sound appears in the table with its duration once downloaded', () => {
  it('posts only the URL, shows the downloaded row and duration, and clears the draft', async () => {
    const downloaded = soundRow({
      id: 8,
      name: 'Downloaded ding',
      source_type: 'url',
      path: null,
      url: 'https://example.local/ding.mp3',
      duration_ms: 1800
    });
    const backend = fakeBackend({ fromUrl: { created: true, sound: downloaded } });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door chime'));

    const input = inputByLabel('Audio URL');
    typeInto(input, 'https://example.local/ding.mp3');
    buttonByText('Add').click();
    await vi.waitFor(() => expect(callsTo(backend.calls, '/api/sounds/from_url', 'POST')).toHaveLength(1));
    await vi.waitFor(() => expect(pageText()).toContain('Downloaded ding'));

    const request = callsTo(backend.calls, '/api/sounds/from_url', 'POST')[0];
    expect(bodyOf(request)).toEqual({ url: 'https://example.local/ding.mp3' });
    expect(pageText()).toContain(formatSoundDuration(downloaded.duration_ms));
    expect(pageText()).toContain('1.8 s');
    expect(inputByLabel('Audio URL').value).toBe('');
    expect(toastStore.items.map((toast) => toast.message)).toContain('Sound added.');
  });

  it('uses the already-in-library toast when the server did not create a row', async () => {
    const existing = soundRow({ id: 4, name: 'Already saved' });
    const backend = fakeBackend({ rows: [existing], fromUrl: { created: false, sound: existing } });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Already saved'));

    typeInto(inputByLabel('Audio URL'), 'https://example.local/already.mp3');
    buttonByText('Add').click();
    await vi.waitFor(() => expect(toastStore.items.map((toast) => toast.message)).toContain('Sound already in the library.'));
    expect(callsTo(backend.calls, '/api/sounds/from_url', 'POST')).toHaveLength(1);
    expect(inputByLabel('Audio URL').value).toBe('');
  });

  it('renders a rejected download message verbatim and preserves the URL draft', async () => {
    const message = 'the download was rejected: this URL does not contain a supported audio file';
    const backend = fakeBackend({ fromUrlError: { status: 415, message } });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door chime'));

    const input = inputByLabel('Audio URL');
    typeInto(input, 'https://example.local/not-audio');
    buttonByText('Add').click();
    await vi.waitFor(() => expect(document.querySelector('[role="alert"]')?.textContent).toContain(message));

    expect(inputByLabel('Audio URL').value).toBe('https://example.local/not-audio');
    expect(callsTo(backend.calls, '/api/sounds/from_url', 'POST')).toHaveLength(1);
  });
});

/* ---- AC3 ---------------------------------------------------------------- */

describe('3. a missing file is visibly broken and cannot be assigned', () => {
  it('keeps the row, announces the missing file, and disables only its preview', async () => {
    const backend = fakeBackend({
      rows: [
        soundRow({ id: 1, name: 'Missing chime', missing: true, duration_ms: 1800 }),
        soundRow({ id: 2, name: 'Healthy chime', missing: false })
      ]
    });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Healthy chime'));

    const missing = rowFor('Missing chime');
    expect(missing.textContent).toContain('MISSING FILE');
    expect(missing.querySelector('.badge')?.getAttribute('class')).toContain('alert');
    expect(missing.querySelector('.badge svg')).not.toBeNull();
    expect(missing.textContent).toContain('Cannot be assigned to a button until the file returns.');
    expect(missing.textContent).toContain('—');
    expect(buttonByAriaLabel('Preview Missing chime').disabled).toBe(true);
    expect(buttonByAriaLabel('Preview Healthy chime').disabled).toBe(false);
    expect(missing.classList.contains('broken')).toBe(true);
    expect(backend.calls.some((call) => call.path.endsWith('/audio'))).toBe(false);
  });
});

/* ---- AC4 ---------------------------------------------------------------- */

describe('4. deleting a sound used by a button requires confirmation and names the buttons', () => {
  const first: AffectedButton = { id: 11, name: 'Front Door', slug: 'front-door' };
  const second: AffectedButton = { id: 12, name: 'Evening notice', slug: 'evening-notice' };
  const conflictMessage =
    'sound 1 is used by 2 buttons — resend with ?confirm=true to delete the sound and those buttons, or re-point the buttons at another sound first; they are deleted, not disabled';

  it('discovers affected buttons without confirming, then confirms, reloads and toasts', async () => {
    const backend = fakeBackend({
      referenced: { 1: [first, second] },
      conflictMessage
    });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door chime'));

    buttonByAriaLabel('Delete Front Door chime').click();
    await vi.waitFor(() => expect(deleteCalls(backend.calls)).toHaveLength(1));
    await vi.waitFor(() => expect(document.querySelector('dialog[open]')).not.toBeNull());
    expect(deleteCalls(backend.calls)[0].path).toBe('/api/sounds/1');
    expect(pageText()).toContain('Front Door');
    expect(pageText()).toContain('Evening notice');
    expect(pageText()).toContain(conflictMessage);
    expect(deleteCalls(backend.calls)).toHaveLength(1);

    buttonByText('Delete sound and buttons').click();
    await vi.waitFor(() => expect(deleteCalls(backend.calls)).toHaveLength(2));
    expect(deleteCalls(backend.calls)[1].path).toBe('/api/sounds/1?confirm=true');
    await vi.waitFor(() => expect(callsTo(backend.calls, '/api/sounds', 'GET')).toHaveLength(2));
    await vi.waitFor(() => expect(pageText()).not.toContain('Front Door chime'));
    expect(toastStore.items.map((toast) => toast.message)).toContain('Sound deleted.');
  });

  it('cancel leaves the sound in place and sends no confirmed DELETE', async () => {
    const backend = fakeBackend({ referenced: { 1: [first, second] }, conflictMessage });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door chime'));

    buttonByAriaLabel('Delete Front Door chime').click();
    await vi.waitFor(() => expect(document.querySelector('dialog[open]')).not.toBeNull());
    buttonByText('Cancel').click();
    await vi.waitFor(() => expect(document.querySelector('dialog[open]')).toBeNull());

    expect(deleteCalls(backend.calls)).toHaveLength(1);
    expect(deleteCalls(backend.calls)[0].path).toBe('/api/sounds/1');
    expect(pageText()).toContain('Front Door chime');
  });

  it('deletes an unreferenced sound in one request without opening a dialog', async () => {
    const backend = fakeBackend();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door chime'));

    buttonByAriaLabel('Delete Front Door chime').click();
    await vi.waitFor(() => expect(deleteCalls(backend.calls)).toHaveLength(1));
    await vi.waitFor(() => expect(pageText()).not.toContain('Front Door chime'));

    expect(deleteCalls(backend.calls)[0].path).toBe('/api/sounds/1');
    expect(document.querySelector('dialog[open]')).toBeNull();
    expect(toastStore.items.map((toast) => toast.message)).toContain('Sound deleted.');
  });
});

/* ---- AC5 ---------------------------------------------------------------- */

describe('5. rename sends only the new name', () => {
  it('opens inline, disables unchanged and empty drafts, and sends exactly the name', async () => {
    const backend = fakeBackend();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door chime'));

    buttonByAriaLabel('Rename Front Door chime').click();
    flushSync();
    const input = inputByLabel('Sound name');
    const save = buttonByText('Save rename');
    expect(save.disabled).toBe(true);

    typeInto(input, '');
    expect(save.disabled).toBe(true);
    typeInto(input, 'Entry chime');
    expect(save.disabled).toBe(false);
    buttonByText('Save rename').click();
    await vi.waitFor(() => expect(callsTo(backend.calls, '/api/sounds/1', 'PATCH')).toHaveLength(1));
    await vi.waitFor(() => expect(pageText()).toContain('Entry chime'));

    const patch = callsTo(backend.calls, '/api/sounds/1', 'PATCH')[0];
    expect(bodyOf(patch)).toEqual({ name: 'Entry chime' });
    expect(callsTo(backend.calls, '/api/sounds', 'GET')).toHaveLength(2);
    expect(toastStore.items.map((toast) => toast.message)).toContain('Sound renamed.');
  });

  it('renders a rejected rename verbatim and keeps the draft in place', async () => {
    const message = 'sound name must not contain a control character';
    const backend = fakeBackend({ renameError: { status: 422, message } });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door chime'));

    buttonByAriaLabel('Rename Front Door chime').click();
    flushSync();
    const input = inputByLabel('Sound name');
    typeInto(input, 'Bad name');
    buttonByText('Save rename').click();
    await vi.waitFor(() => expect(document.querySelector('[role="alert"]')?.textContent).toContain(message));

    expect(inputByLabel('Sound name').value).toBe('Bad name');
    expect(callsTo(backend.calls, '/api/sounds/1', 'PATCH')).toHaveLength(1);
    expect(bodyOf(callsTo(backend.calls, '/api/sounds/1', 'PATCH')[0])).toEqual({ name: 'Bad name' });
  });
});

/* ---- AC6 ---------------------------------------------------------------- */

describe('6. preview plays in the browser and only for a playable file', () => {
  it('streams one preview at a time, shows PLAYING, and stops cleanly', async () => {
    const backend = fakeBackend({
      rows: [
        soundRow({ id: 1, name: 'First chime' }),
        soundRow({ id: 2, name: 'Second chime' })
      ]
    });
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(() => Promise.resolve());
    const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Second chime'));

    buttonByAriaLabel('Preview First chime').click();
    flushSync();
    const audio = document.querySelector<HTMLAudioElement>('audio');
    expect(audio).not.toBeNull();
    expect(audio?.src).toMatch(/\/api\/sounds\/1\/audio$/);
    expect(play).toHaveBeenCalledTimes(1);
    expect(rowFor('First chime').textContent).toContain('PLAYING');
    expect(buttonByAriaLabel('Stop preview')).toBeTruthy();

    buttonByAriaLabel('Preview Second chime').click();
    flushSync();
    expect(pause).toHaveBeenCalledTimes(1);
    expect(audio?.src).toMatch(/\/api\/sounds\/2\/audio$/);
    expect(play).toHaveBeenCalledTimes(2);
    expect(rowFor('First chime').textContent).not.toContain('PLAYING');
    expect(rowFor('Second chime').textContent).toContain('PLAYING');

    buttonByAriaLabel('Stop preview').click();
    flushSync();
    expect(pause).toHaveBeenCalledTimes(2);
    expect(audio?.getAttribute('src')).toBeNull();
    expect(rowFor('Second chime').textContent).not.toContain('PLAYING');
    expect(backend.calls.filter((call) => call.path.endsWith('/audio'))).toHaveLength(0);
  });

  it('never sets a source or calls play for a missing row', async () => {
    const backend = fakeBackend({ rows: [soundRow({ id: 1, missing: true })] });
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(() => Promise.resolve());
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door chime'));

    const preview = buttonByAriaLabel('Preview Front Door chime');
    expect(preview.disabled).toBe(true);
    preview.click();
    flushSync();

    expect(document.querySelector('audio')?.getAttribute('src')).toBeNull();
    expect(play).not.toHaveBeenCalled();
    expect(backend.calls.some((call) => call.path.endsWith('/audio'))).toBe(false);
  });
});

/* ---- AC7 ---------------------------------------------------------------- */

describe('7. the table shows every row fact', () => {
  it('shows format, formatted duration, source type and missing state for file and URL rows', async () => {
    const backend = fakeBackend({
      rows: [
        soundRow({ id: 1, name: 'Folder bell', source_type: 'file', format: 'mp3', duration_ms: 1800 }),
        soundRow({
          id: 2,
          name: 'URL bell',
          source_type: 'url',
          format: 'ogg',
          duration_ms: 2100,
          path: null,
          url: 'https://example.local/bell.ogg'
        })
      ]
    });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('URL bell'));

    expect(pageText()).toContain('Folder bell');
    expect(pageText()).toContain('mp3');
    expect(pageText()).toContain('1.8 s');
    expect(pageText()).toContain('folder');
    expect(pageText()).toContain('No');
    expect(pageText()).toContain('URL bell');
    expect(pageText()).toContain('ogg');
    expect(pageText()).toContain('2.1 s');
    expect(pageText()).toContain('URL download');
    expect(pageText()).not.toContain('MISSING FILE');
    expect(callsTo(backend.calls, '/api/sounds', 'GET')).toHaveLength(1);
  });
});

/* ---- AC8 ---------------------------------------------------------------- */

describe('8. loading and error states', () => {
  it('shows a spinner before the answer, then retries a failed load', async () => {
    const gate = deferred();
    const backend = fakeBackend({ listGate: gate.promise });
    renderPanel();
    expect(pageText()).toContain('Loading the sound library');
    expect(document.querySelectorAll('li.record')).toHaveLength(0);

    backend.options.listGate = null;
    gate.resolve();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door chime'));
    unmountAll();

    backend.options.listError = 'the sound index is temporarily unavailable';
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('the sound index is temporarily unavailable'));
    expect(callsTo(backend.calls, '/api/sounds', 'GET')).toHaveLength(2);

    backend.options.listError = null;
    buttonByText('Retry').click();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door chime'));
    expect(callsTo(backend.calls, '/api/sounds', 'GET')).toHaveLength(3);
  });

  it('shows an empty library while keeping both population controls usable and follows D26 URL input rules', async () => {
    fakeBackend({ rows: [] });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('No sounds yet'));
    expect(pageText()).toContain('Rescan the folder or add an audio URL.');
    expect(document.querySelector('.empty-state')).not.toBeNull();
    expect(buttonByText('Rescan folder').disabled).toBe(false);
    expect(buttonByText('Add').disabled).toBe(true);

    const url = inputByLabel('Audio URL');
    expect(url.getAttribute('type')).toBe('url');
    expect(url.getAttribute('inputmode')).toBe('url');
    expect(url.getAttribute('autocapitalize')).toBe('none');
    expect(url.getAttribute('autocorrect')).toBe('off');
    expect(url.getAttribute('spellcheck')).toBe('false');
    expect(document.querySelector('select')).toBeNull();

    typeInto(url, 'https://example.local/new.mp3');
    expect(buttonByText('Add').disabled).toBe(false);
  });
});
