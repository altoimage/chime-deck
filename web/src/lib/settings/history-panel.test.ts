// @vitest-environment happy-dom
/**
 * Acceptance tests for the History settings panel (HSD-l22f).
 *
 * Fully offline: every request is answered by the in-memory press-log server
 * through `setFetchImpl`, so no test reaches the network or a real press log.
 *
 * What is proven here: the seven stored fields render self-describing rows, the
 * outcome filter is a Select that re-fetches with `outcome` and the first page
 * size, a failed row's reason is printed verbatim (or the honest "no reason"
 * sentence), "Load more" raises `limit` while preserving `outcome`, the retention
 * cap replaces the button with an explanation, and loading / empty / error each
 * read as themselves. Not proven: CSS (a DOM has no layout engine) or the real
 * server's ordering.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  setFetchImpl,
  type FetchLike,
  type FetchResponse,
  type HistoryRow
} from '../api.js';
import { PAGE, retentionNote } from '../history-view.js';
import HistoryPanel from './HistoryPanel.svelte';

/* ---- in-memory press-log server ----------------------------------------- */

interface FakeCall {
  path: string;
  init?: RequestInit;
}

interface BackendOptions {
  rows?: HistoryRow[];
  retention?: number;
  listError?: string | null;
}

interface Backend {
  calls: FakeCall[];
  rows: HistoryRow[];
  retention: number;
  options: BackendOptions;
}

function historyRow(overrides: Partial<HistoryRow> = {}): HistoryRow {
  return {
    id: 2,
    button_id: 3,
    button_name: 'Front Door',
    pressed_at: '2026-11-12T09:04:00',
    source: 'mqtt',
    outcome: 'played',
    duration_ms: 1800,
    latency_ms: 18,
    synthesis_ms: 240,
    detail: 'engine piper',
    ...overrides
  };
}

function jsonResponse(body: unknown, status = 200): FetchResponse {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function errorResponse(status: number, message: string): FetchResponse {
  return jsonResponse({ error: { code: 'test_error', message, detail: null } }, status);
}

function fakeBackend(options: BackendOptions = {}): Backend {
  const backend: Backend = {
    calls: [],
    rows: (options.rows ?? [historyRow(), historyRow({ id: 1, outcome: 'failed' })]).map((row) => ({
      ...row
    })),
    retention: options.retention ?? 1000,
    options
  };

  const impl: FetchLike = async (path, init) => {
    backend.calls.push({ path, init });
    if (!path.startsWith('/api/history')) {
      throw new Error(`unexpected request ${init?.method ?? 'GET'} ${path}`);
    }
    if (backend.options.listError) {
      return errorResponse(500, backend.options.listError);
    }
    const query = new URL(path, 'http://localhost').searchParams;
    const limit = Number(query.get('limit') ?? PAGE);
    const outcome = query.get('outcome') ?? '';
    const kept = outcome === '' ? backend.rows : backend.rows.filter((row) => row.outcome === outcome);
    return jsonResponse({ rows: kept.slice(0, limit), retention: backend.retention });
  };

  setFetchImpl(impl);
  return backend;
}

/* ---- DOM harness -------------------------------------------------------- */

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function renderPanel(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(HistoryPanel, { target }));
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

function historyCalls(backend: Backend): FakeCall[] {
  return backend.calls.filter((call) => call.path.startsWith('/api/history'));
}

function queryOf(call: FakeCall): URLSearchParams {
  return new URL(call.path, 'http://localhost').searchParams;
}

/** Pick one outcome in the filter's Select sheet, as a finger would. */
function chooseOutcome(value: string): void {
  document.querySelector<HTMLButtonElement>('button[aria-haspopup="dialog"]')?.click();
  flushSync();
  const radio = document.querySelector<HTMLInputElement>(`input[type="radio"][value="${value}"]`);
  if (radio === null) {
    throw new Error(`expected an outcome option "${value}"`);
  }
  radio.checked = true;
  radio.dispatchEvent(new Event('change', { bubbles: true }));
  flushSync();
}

function buttonByText(label: string): HTMLButtonElement | undefined {
  return [...document.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => (candidate.textContent ?? '').replace(/\s+/g, ' ').trim() === label
  );
}

function rowById(id: number): HTMLLIElement {
  const row = document.querySelector<HTMLLIElement>(`li[data-row-id="${id}"]`);
  if (row === null) {
    throw new Error(`expected a history row with id ${id}`);
  }
  return row;
}

beforeEach(() => {
  setFetchImpl(null);
  unmountAll();
});

afterEach(() => {
  unmountAll();
  setFetchImpl(null);
  vi.restoreAllMocks();
});

/* ---- rows --------------------------------------------------------------- */

describe('1. the press log renders every stored field on the row', () => {
  it('shows time, button, source, outcome, duration and detail without a table', async () => {
    const backend = fakeBackend();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));

    expect(queryOf(historyCalls(backend)[0]).get('limit')).toBe(String(PAGE));
    expect(queryOf(historyCalls(backend)[0]).get('outcome')).toBeNull();

    expect(pageText()).toContain('2026-11-12 09:04');
    expect(pageText()).toContain('Played');
    expect(pageText()).toContain('source: mqtt');
    expect(pageText()).toContain('duration: 1.8 s');
    expect(pageText()).toContain('latency: 18 ms');
    expect(pageText()).toContain('synthesis: 240 ms');
    expect(pageText()).toContain('engine piper');
    expect(pageText()).toContain(retentionNote(1000));
    // Self-describing rows, not a dense desktop table.
    expect(document.querySelector('table')).toBeNull();
  });

  it('marks each row with its outcome and id for the end-to-end suite', async () => {
    fakeBackend();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));

    expect(rowById(2).getAttribute('data-outcome')).toBe('played');
    expect(rowById(1).getAttribute('data-outcome')).toBe('failed');
  });

  it('prints a zero latency as an immediate start, never a dash', async () => {
    fakeBackend({ rows: [historyRow({ latency_ms: 0 })] });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));

    expect(pageText()).toContain('latency: immediate');
    expect(pageText()).not.toContain('latency: —');
  });

  it('reads an unstarted job as a dash, never as a fabricated zero', async () => {
    fakeBackend({ rows: [historyRow({ latency_ms: null })] });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));

    expect(pageText()).toContain('latency: —');
    expect(pageText()).not.toContain('latency: 0 ms');
  });

  it('prints a press with no synthesis as a dash, never a fabricated zero or cause', async () => {
    fakeBackend({ rows: [historyRow({ synthesis_ms: null })] });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));

    // A cache or bake hit, a sound button, a failed press and an engine that keeps no
    // measurement all store `null`, so the only honest word is "nothing measured".
    expect(pageText()).toContain('synthesis: —');
    expect(pageText()).not.toContain('synthesis: 0 ms');
    expect(pageText()).not.toContain('synthesis: cached');
  });

  it('prints a synthesis rounded to zero as under a millisecond, never a dash', async () => {
    fakeBackend({ rows: [historyRow({ synthesis_ms: 0 })] });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));

    expect(pageText()).toContain('synthesis: <1 ms');
    expect(pageText()).not.toContain('synthesis: —');
  });
});

describe('2. the outcome filter re-fetches the first page', () => {
  it('asks for outcome=failed at the default page size', async () => {
    const backend = fakeBackend();
    renderPanel();
    await vi.waitFor(() => expect(historyCalls(backend)).toHaveLength(1));

    chooseOutcome('suppressed_quiet_hours');
    chooseOutcome('suppressed_muted');
    chooseOutcome('failed');

    await vi.waitFor(() => expect(historyCalls(backend)).toHaveLength(4));
    expect(queryOf(historyCalls(backend)[2]).get('outcome')).toBe('suppressed_muted');
    const last = queryOf(historyCalls(backend)[3]);
    expect(last.get('outcome')).toBe('failed');
    expect(last.get('limit')).toBe(String(PAGE));
  });
});

describe('3. a failed press always prints its reason inline', () => {
  it('renders the stored detail verbatim in a note block', async () => {
    fakeBackend({
      rows: [historyRow({ id: 1, outcome: 'failed', duration_ms: null, detail: 'mpv exited with status 1' })]
    });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('mpv exited with status 1'));

    const reason = rowById(1).querySelector<HTMLElement>('[data-failed-reason]');
    expect(reason).not.toBeNull();
    expect(reason?.getAttribute('role')).toBe('note');
    expect(reason?.textContent).toContain('Reason:');
    expect(reason?.textContent).toContain('mpv exited with status 1');
    expect(pageText()).toContain('duration: —');
  });

  it('says the server recorded no reason when the detail is null', async () => {
    fakeBackend({
      rows: [historyRow({ id: 1, outcome: 'failed', duration_ms: null, detail: null })]
    });
    renderPanel();
    await vi.waitFor(() =>
      expect(pageText()).toContain('the server recorded no reason for this failure.')
    );
    expect(rowById(1).querySelector('[data-failed-reason]')).not.toBeNull();
  });
});

describe('4. Load more raises the limit and keeps the filter', () => {
  it('asks for the next page size while preserving the selected outcome', async () => {
    const failed = Array.from({ length: 120 }, (_unused, index) =>
      historyRow({ id: index + 1, outcome: 'failed', detail: 'mpv exited with status 1' })
    );
    const backend = fakeBackend({ rows: failed });
    renderPanel();
    await vi.waitFor(() => expect(historyCalls(backend)).toHaveLength(1));

    chooseOutcome('failed');
    await vi.waitFor(() => expect(historyCalls(backend)).toHaveLength(2));
    await vi.waitFor(() => expect(buttonByText('Load more')).toBeDefined());

    buttonByText('Load more')?.click();
    await vi.waitFor(() => expect(historyCalls(backend)).toHaveLength(3));

    const last = queryOf(historyCalls(backend)[2]);
    expect(last.get('limit')).toBe('100');
    expect(last.get('outcome')).toBe('failed');
  });

  it('replaces the button with the retention explanation at the cap', async () => {
    fakeBackend({ retention: PAGE });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Front Door'));

    expect(buttonByText('Load more')).toBeUndefined();
    expect(pageText()).toContain('retention limit of 50 rows is reached');
  });
});

describe('5. loading, empty and error each read as themselves', () => {
  it('shows the spinner before the first answer and no list', () => {
    fakeBackend();
    renderPanel();
    expect(pageText()).toContain('Loading press history');
    expect(document.querySelector('ul.records')).toBeNull();
    expect(document.querySelector('table')).toBeNull();
  });

  it('shows the empty state for an answer with no rows', async () => {
    fakeBackend({ rows: [] });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('No presses recorded'));
    expect(pageText()).toContain(retentionNote(1000));
  });

  it('renders the server sentence verbatim with a Retry', async () => {
    const backend = fakeBackend({ listError: 'the press log table is locked' });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('the press log table is locked'));

    const alert = document.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain('the press log table is locked');

    buttonByText('Retry')?.click();
    await vi.waitFor(() => expect(historyCalls(backend)).toHaveLength(2));
  });
});
