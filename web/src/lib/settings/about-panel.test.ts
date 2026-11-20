// @vitest-environment happy-dom
/**
 * Acceptance tests for the About settings panel (HSD-l22f): the process facts,
 * the log excerpt, the configuration export and the dry-run / apply import.
 *
 * Fully offline: every request is answered by the in-memory server below through
 * `setFetchImpl`. The file input is the browser's own control, driven here by
 * defining `files` on it and firing `change` — the same events a real chooser
 * emits, so the panel's own handler is what is under test.
 *
 * The last sentence is the feature contract: a build without `GET /api/tts/bake_usage`
 * renders no bake card at all, and a build with it keeps every one of the four batch
 * outcomes separate — including the zero ones, which say `None.` rather than vanish.
 */

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  setFetchImpl,
  type AboutResponse,
  type BakeAllResponse,
  type BakeUsageResponse,
  type ButtonDetail,
  type FetchLike,
  type FetchResponse
} from '../api.js';
import { toastStore } from '../ui/toast.svelte.js';
import AboutPanel from './AboutPanel.svelte';

/* ---- in-memory server ---------------------------------------------------- */

interface FakeCall {
  path: string;
  init?: RequestInit;
}

interface BackendOptions {
  about?: Partial<AboutResponse>;
  aboutError?: string | null;
  importError?: { message: string; problems?: string[] } | null;
  /** `null` (the default) means this build has no bake endpoint, so it answers 404. */
  bakeUsage?: BakeUsageResponse | null;
  /** A batch answer body, or a refusal with the server's own status and sentence. */
  bakeAll?: BakeAllResponse | BakeRefusal;
  /** A 200 body that is deliberately not a batch answer, to prove the client checks. */
  bakeAllRaw?: unknown;
  /** Holds `POST /api/tts/bake_all` open so the in-flight state can be inspected. */
  bakeGate?: Promise<void> | null;
  /** Rows for the name lookup; a failed lookup is exercised by `buttonsError`. */
  buttons?: ButtonDetail[];
  buttonsError?: string | null;
}

/** The refusal shape a bake batch can answer with instead of the four lists. */
interface BakeRefusal {
  status: number;
  message: string;
}

interface Backend {
  calls: FakeCall[];
  options: BackendOptions;
  /** How many non-dry imports the fake accepted. */
  applied: number;
  appliedDocuments: unknown[];
}

const ABOUT: AboutResponse = {
  version: '1.4.2',
  schema_version: 7,
  uptime_s: 8040,
  data_dir: '/var/lib/chime-deck/data',
  log_available: false
};

const CURRENT_DOCUMENT = {
  format: 'chime-deck/config',
  version: 1,
  settings: { 'ha.base_url': 'http://homeassistant.local:8123' },
  entities: [{ entity_id: 'lock.front_door', display_name: 'Front Door' }],
  sounds: [],
  buttons: []
};

const INCOMING_DOCUMENT = {
  format: 'chime-deck/config',
  version: 1,
  settings: { 'ha.base_url': 'http://homeassistant.local:8123', 'audio.volume': 55 },
  entities: [
    { entity_id: 'lock.front_door', display_name: 'Front Door' },
    { entity_id: 'light.hall', display_name: 'Hall' }
  ],
  sounds: [{ name: 'Ding', source_type: 'file', path: 'data/sounds/ding.mp3' }],
  buttons: [{ slug: 'front-door', name: 'Front Door' }]
};

const USAGE: BakeUsageResponse = {
  baked: { files: 2, bytes: 2048 },
  cache: { files: 0, bytes: 0 }
};

const EMPTY_REPORT: BakeAllResponse = {
  baked: [],
  skipped_current: [],
  skipped_dynamic: [],
  failed: []
};

/** One stored button row, with the bake columns an HSD-dzek build carries. */
function buttonRow(id: number, name: string): ButtonDetail {
  return {
    id,
    slug: name.toLowerCase().replace(/\s+/g, '-'),
    name,
    icon: null,
    action_type: 'tts',
    sound_id: null,
    tts_text: `The ${name} is here`,
    tts_engine: null,
    baked_sha: null,
    baked_format: null,
    baked_at: null,
    baked_error: null,
    volume_override: null,
    dedupe_seconds: null,
    show_on_dashboard: true,
    quiet_hours_exempt: false,
    enabled: true,
    sort_order: id,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    mqtt: null,
    slug_change: null
  };
}

const DEFAULT_ROWS: ButtonDetail[] = [
  buttonRow(1, 'Front Door'),
  buttonRow(2, 'Porch'),
  buttonRow(3, 'Hall'),
  buttonRow(4, 'Gate')
];

/** One deferred, so a test can hold a request open without a real timer. */
function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve: () => void = () => {};
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function jsonResponse(body: unknown, status = 200): FetchResponse {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function errorResponse(
  status: number,
  message: string,
  detail: unknown = null,
  code = 'validation_error'
): FetchResponse {
  return jsonResponse({ error: { code, message, detail } }, status);
}

/** The real 404 envelope this server answers for a route it does not have. */
function notFoundResponse(): FetchResponse {
  return errorResponse(404, 'not found', null, 'not_found');
}

function fakeBackend(options: BackendOptions = {}): Backend {
  const backend: Backend = { calls: [], options, applied: 0, appliedDocuments: [] };

  const impl: FetchLike = async (path, init) => {
    backend.calls.push({ path, init });
    const method = init?.method ?? 'GET';

    if (path === '/api/about' && method === 'GET') {
      if (backend.options.aboutError) {
        return errorResponse(500, backend.options.aboutError);
      }
      return jsonResponse({ ...ABOUT, ...backend.options.about });
    }

    if (path.startsWith('/api/config/export') && method === 'GET') {
      return jsonResponse(CURRENT_DOCUMENT);
    }

    if (path === '/api/config/import' && method === 'POST') {
      const body = JSON.parse(String(init?.body)) as { dry_run: boolean; document: unknown };
      if (backend.options.importError) {
        return errorResponse(422, backend.options.importError.message, {
          problems: backend.options.importError.problems ?? []
        });
      }
      if (!body.dry_run) {
        backend.applied += 1;
        backend.appliedDocuments.push(body.document);
      }
      // The real server counts every row the document names (import is a merge
      // that upserts each one), so the fake derives them from the document too.
      const incoming = body.document as {
        settings?: Record<string, unknown>;
        entities?: unknown[];
        sounds?: unknown[];
        buttons?: unknown[];
      };
      return jsonResponse({
        dry_run: body.dry_run,
        settings_written: Object.keys(incoming.settings ?? {}).length,
        entities: (incoming.entities ?? []).length,
        sounds: (incoming.sounds ?? []).length,
        buttons: (incoming.buttons ?? []).length
      });
    }

    if (path === '/api/buttons' && method === 'GET') {
      if (backend.options.buttonsError) {
        return errorResponse(500, backend.options.buttonsError);
      }
      return jsonResponse(backend.options.buttons ?? DEFAULT_ROWS);
    }

    if (path === '/api/tts/bake_usage' && method === 'GET') {
      // No usage body is the feature signal: this build does not have the endpoint.
      const usage = backend.options.bakeUsage;
      return usage === undefined || usage === null ? notFoundResponse() : jsonResponse(usage);
    }

    if (path === '/api/tts/bake_all' && method === 'POST') {
      if (backend.options.bakeGate) {
        await backend.options.bakeGate;
      }
      if (backend.options.bakeAllRaw !== undefined) {
        return jsonResponse(backend.options.bakeAllRaw);
      }
      const answer = backend.options.bakeAll ?? EMPTY_REPORT;
      return 'status' in answer
        ? errorResponse(answer.status, answer.message)
        : jsonResponse(answer);
    }

    throw new Error(`unexpected request ${method} ${path}`);
  };

  setFetchImpl(impl);
  return backend;
}

/* ---- DOM harness --------------------------------------------------------- */

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function renderPanel(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(AboutPanel, { target }));
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

function callsTo(backend: Backend, prefix: string, method = 'GET'): FakeCall[] {
  return backend.calls.filter(
    (call) => call.path.startsWith(prefix) && (call.init?.method ?? 'GET') === method
  );
}

function buttonByText(label: string, root: ParentNode = document): HTMLButtonElement | undefined {
  return [...root.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => (candidate.textContent ?? '').replace(/\s+/g, ' ').trim() === label
  );
}

/** Drive the native file chooser the way a browser does: set `files`, fire `change`. */
function chooseFile(name: string, body: unknown): void {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]');
  if (input === null) {
    throw new Error('expected a native file input');
  }
  const file = new File([JSON.stringify(body)], name, { type: 'application/json' });
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  input.dispatchEvent(new Event('change', { bubbles: true }));
  flushSync();
}

beforeEach(() => {
  setFetchImpl(null);
  unmountAll();
});

afterEach(() => {
  unmountAll();
  setFetchImpl(null);
  toastStore.clear();
  vi.restoreAllMocks();
});

/* ---- facts --------------------------------------------------------------- */

describe('1. the process facts are printed verbatim', () => {
  it('shows version, schema version, uptime and the data directory', async () => {
    const backend = fakeBackend();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('1.4.2'));

    expect(callsTo(backend, '/api/about')).toHaveLength(1);
    expect(pageText()).toContain('Version');
    expect(pageText()).toContain('Schema version');
    expect(pageText()).toContain('7');
    expect(pageText()).toContain('Uptime');
    expect(pageText()).toContain('2 h 14 min');
    expect(pageText()).toContain('/var/lib/chime-deck/data');
  });

  it('renders the server sentence with a Retry when the facts cannot be read', async () => {
    const backend = fakeBackend({ aboutError: 'the about route is unavailable' });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('the about route is unavailable'));
    expect(document.querySelector('[role="alert"]')?.textContent).toContain(
      'the about route is unavailable'
    );

    buttonByText('Retry')?.click();
    await vi.waitFor(() => expect(callsTo(backend, '/api/about')).toHaveLength(2));
  });
});

/* ---- export -------------------------------------------------------------- */

describe('2. the configuration export defaults to a redacted file', () => {
  it('leaves secrets out, and warns only when they are included', async () => {
    fakeBackend();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('1.4.2'));

    const anchor = document.querySelector<HTMLAnchorElement>('a[data-export-secrets]');
    expect(anchor).not.toBeNull();
    expect(anchor?.getAttribute('href')).toContain('include_secrets=false');
    expect(anchor?.getAttribute('href')).toBe('/api/config/export?include_secrets=false');
    expect(anchor?.hasAttribute('download')).toBe(true);

    const warning = 'This export contains your Home Assistant token and MQTT password.';
    expect(pageText()).not.toContain(warning);

    document.querySelector<HTMLElement>('[role="switch"]')?.click();
    flushSync();

    const toggled = document.querySelector<HTMLAnchorElement>('a[data-export-secrets]');
    expect(toggled?.getAttribute('href')).toContain('include_secrets=true');
    expect(toggled?.getAttribute('data-export-secrets')).toBe('true');
    expect(pageText()).toContain(warning);
    expect(pageText()).toContain('Store the file somewhere safe.');
  });
});

/* ---- log excerpt --------------------------------------------------------- */

describe('3. the log anchor exists only when there is a log file', () => {
  it('explains how to turn file logging on when it is off', async () => {
    fakeBackend({ about: { log_available: false } });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('File logging is off'));

    expect(document.querySelector('a[href="/api/about/log"]')).toBeNull();
    expect(pageText()).toContain('--log-file');
    expect(pageText()).toContain('CHIMEDECK_LOG_FILE=1');
  });

  it('offers the download when file logging is on', async () => {
    fakeBackend({ about: { log_available: true } });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('1.4.2'));

    const link = document.querySelector<HTMLAnchorElement>('a[href="/api/about/log"]');
    expect(link).not.toBeNull();
    expect(link?.hasAttribute('download')).toBe(true);
    expect(pageText()).toContain('Download log excerpt');
    expect(pageText()).not.toContain('File logging is off');
  });
});

/* ---- import -------------------------------------------------------------- */

describe('4. Apply is unavailable until a dry run of the selected file succeeds', () => {
  it('renders no Apply button before a dry run and shows the file facts once read', async () => {
    const backend = fakeBackend();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('1.4.2'));

    expect(buttonByText('Apply')).toBeUndefined();

    chooseFile('config.json', INCOMING_DOCUMENT);
    await vi.waitFor(() => expect(pageText()).toContain('config.json'));
    expect(pageText()).toContain('bytes');
    expect(buttonByText('Apply')).toBeUndefined();
    expect(callsTo(backend, '/api/config/import', 'POST')).toHaveLength(0);
  });

  it('reports the server counts and the client per-section diff', async () => {
    const backend = fakeBackend();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('1.4.2'));

    chooseFile('config.json', INCOMING_DOCUMENT);
    await vi.waitFor(() => expect(pageText()).toContain('config.json'));

    buttonByText('Dry run')?.click();
    await vi.waitFor(() => expect(pageText()).toContain('Dry run accepted: the server would write'));

    expect(pageText()).toContain('2 settings');
    expect(pageText()).toContain('2 entities');
    expect(pageText()).toContain('1 sound');
    expect(pageText()).toContain('1 button');
    expect(pageText()).toContain('every row the document names, unchanged ones included');
    expect(pageText()).toContain('Nothing was written.');

    const post = callsTo(backend, '/api/config/import', 'POST');
    expect(post).toHaveLength(1);
    expect(JSON.parse(String(post[0].init?.body))).toMatchObject({ dry_run: true });

    // The comparison is against the current (redacted) export, not a guess.
    expect(callsTo(backend, '/api/config/export')).toHaveLength(1);
    expect(callsTo(backend, '/api/config/export')[0].path).toContain('include_secrets=false');

    const table = document.querySelector('table.diff');
    expect(table).not.toBeNull();
    expect(table?.textContent).toContain('Settings');
    expect(table?.textContent).toContain('Entities');
    expect(table?.textContent).toContain('Sounds');
    expect(table?.textContent).toContain('Buttons');
    expect(table?.textContent).toContain('merge keeps absent keys');
    expect(table?.textContent).toContain('merge keeps absent rows');
    expect(pageText()).toContain('secrets are never exported');

    // settings added 1, entities added 1, sounds added 1, buttons added 1.
    const rows = [...(table?.querySelectorAll('tbody tr') ?? [])].map((row) =>
      (row.textContent ?? '').replace(/\s+/g, ' ').trim()
    );
    expect(rows).toHaveLength(4);
    for (const row of rows) {
      expect(row).toContain('0 · merge');
    }

    // The two numbers measure different things, and the panel says so in words:
    // the diff counts differing rows, the server counts every named row.
    expect(pageText()).toContain(
      'Added and changed are how this file differs from the stored configuration.'
    );
    expect(pageText()).toContain(
      'A row the file names but does not change is rewritten too, so the written counts above can be larger.'
    );
    expect(pageText()).toContain(
      'Removed is always 0 because an import merges and never deletes.'
    );

    buttonByText('Apply')?.click();
    flushSync();
    const sheet = document.querySelector('dialog[open]');
    expect(sheet).not.toBeNull();
    expect(sheet?.textContent).toContain('Apply the changes in the dry-run report?');

    buttonByText('Apply', sheet as HTMLElement)?.click();
    await vi.waitFor(() => expect(backend.applied).toBe(1));
    await vi.waitFor(() => expect(pageText()).toContain('The sound library was rescanned'));
    expect(toastStore.items.map((toast) => toast.message)).toContain('Configuration applied.');
    expect(JSON.parse(String(callsTo(backend, '/api/config/import', 'POST')[1].init?.body))).toMatchObject({
      dry_run: false
    });
  });

  it('prints the number of rows the document names, not the number that differ', async () => {
    // The property that was silently broken: the server counts every row the
    // document names (an unchanged row is still rewritten), so its numbers must
    // equal the fixture document's row counts — not the diff's added + changed.
    const backend = fakeBackend();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('1.4.2'));

    chooseFile('config.json', INCOMING_DOCUMENT);
    await vi.waitFor(() => expect(pageText()).toContain('config.json'));
    buttonByText('Dry run')?.click();
    await vi.waitFor(() => expect(pageText()).toContain('Dry run accepted: the server would write'));

    const named = [
      { count: Object.keys(INCOMING_DOCUMENT.settings).length, one: 'setting', many: 'settings' },
      { count: INCOMING_DOCUMENT.entities.length, one: 'entity', many: 'entities' },
      { count: INCOMING_DOCUMENT.sounds.length, one: 'sound', many: 'sounds' },
      { count: INCOMING_DOCUMENT.buttons.length, one: 'button', many: 'buttons' }
    ];
    const sentence = named
      .map(({ count, one, many }) => `${count} ${count === 1 ? one : many}`)
      .join(', ');

    expect(callsTo(backend, '/api/config/import', 'POST')).toHaveLength(1);
    expect(pageText()).toContain(`the server would write ${sentence} —`);

    // 2 settings and 2 entities are named, but only 1 of each differs.
    expect(named.map((entry) => entry.count)).toEqual([2, 2, 1, 1]);
    const rows = [...document.querySelectorAll('table.diff tbody tr')].map((row) =>
      [...row.querySelectorAll('td')].slice(0, 2).map((cell) => (cell.textContent ?? '').trim())
    );
    expect(rows).toEqual([
      ['1', '0'],
      ['1', '0'],
      ['1', '0'],
      ['1', '0']
    ]);
  });

  it('renders the server message verbatim with every problem, and applies nothing', async () => {
    const backend = fakeBackend({
      importError: {
        message:
          'the configuration document has 2 problems: settings.ha.token: value is too long; buttons[0].slug is not a slug',
        problems: ['settings.ha.token: value is too long', 'buttons[0].slug is not a slug']
      }
    });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('1.4.2'));

    chooseFile('broken.json', INCOMING_DOCUMENT);
    await vi.waitFor(() => expect(pageText()).toContain('broken.json'));

    buttonByText('Dry run')?.click();
    await vi.waitFor(() =>
      expect(pageText()).toContain('settings.ha.token: value is too long')
    );

    expect(pageText()).toContain('buttons[0].slug is not a slug');
    expect(document.querySelector('[role="alert"]')?.textContent).toContain(
      'the configuration document has 2 problems'
    );
    expect(buttonByText('Apply')).toBeUndefined();
    expect(backend.applied).toBe(0);
  });

  it('forgets the dry run when a different file is chosen', async () => {
    fakeBackend();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('1.4.2'));

    chooseFile('config.json', INCOMING_DOCUMENT);
    await vi.waitFor(() => expect(pageText()).toContain('config.json'));
    buttonByText('Dry run')?.click();
    await vi.waitFor(() => expect(buttonByText('Apply')).toBeDefined());

    chooseFile('other.json', CURRENT_DOCUMENT);
    await vi.waitFor(() => expect(pageText()).toContain('other.json'));

    expect(buttonByText('Apply')).toBeUndefined();
    expect(pageText()).not.toContain('Dry run accepted: the server would write');
  });
});

/* ---- the control is absent because the endpoint is absent -------------- */

describe('5. the bake control is absent because the endpoint is absent', () => {
  it('mentions neither a bulk re-render nor disk usage', async () => {
    // `bakeUsage: null` is a build whose `GET /api/tts/bake_usage` answers 404.
    fakeBackend({ about: { log_available: true }, bakeUsage: null });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('1.4.2'));

    chooseFile('config.json', INCOMING_DOCUMENT);
    await vi.waitFor(() => expect(pageText()).toContain('config.json'));
    buttonByText('Dry run')?.click();
    await vi.waitFor(() => expect(pageText()).toContain('Dry run accepted: the server would write'));

    const text = pageText().toLowerCase();
    expect(text).not.toContain('re-render');
    expect(text).not.toContain('rerender');
    expect(text).not.toContain('disk usage');
    expect([
      ...document.querySelectorAll<HTMLElement>('button, a')
    ].map((control) => (control.textContent ?? '').toLowerCase())).not.toContain('re-render all static phrases');
  });
});

/* ---- feature detection --------------------------------------------------- */

describe('6. the pre-render card appears only when this build has the endpoint', () => {
  it('renders no control and no figure when GET /api/tts/bake_usage is absent', async () => {
    const backend = fakeBackend();
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('1.4.2'));

    // The probe ran and failed silently: no card, no disabled button, no invented zero.
    expect(callsTo(backend, '/api/tts/bake_usage')).toHaveLength(1);
    expect(document.querySelector('[role="alert"]')).toBeNull();
    expect(buttonByText('Re-render all static phrases')).toBeUndefined();
    expect(pageText()).not.toContain('Pre-rendered speech');
    expect(pageText()).not.toContain('data/tts/baked/');
    expect(pageText()).not.toContain('data/tts-cache/');

    // The rest of the panel is unaffected by the missing endpoint.
    expect(document.querySelector('a[data-export-secrets]')).not.toBeNull();
    chooseFile('config.json', INCOMING_DOCUMENT);
    await vi.waitFor(() => expect(pageText()).toContain('config.json'));
    buttonByText('Dry run')?.click();
    await vi.waitFor(() => expect(pageText()).toContain('Dry run accepted: the server would write'));
  });

  it('shows both measured directories from the API body', async () => {
    const backend = fakeBackend({
      bakeUsage: { baked: { files: 3, bytes: 1234567 }, cache: { files: 1, bytes: 987 } }
    });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('Pre-rendered speech'));

    // 1234567 bytes is 1.2 MB: no visible number could be summed into that figure.
    expect(pageText()).toContain('data/tts/baked/');
    expect(pageText()).toContain('3 files · 1.2 MB');
    expect(pageText()).toContain('data/tts-cache/');
    expect(pageText()).toContain('1 file · 987 bytes');
    expect(pageText()).toContain('measured on the device in those two directories');
    expect(callsTo(backend, '/api/tts/bake_usage')).toHaveLength(1);
  });

  it('offers the batch button, enabled, when the endpoint answers', async () => {
    fakeBackend({ bakeUsage: USAGE });
    renderPanel();
    await vi.waitFor(() => expect(buttonByText('Re-render all static phrases')).toBeDefined());

    const button = buttonByText('Re-render all static phrases');
    expect(button?.disabled).toBe(false);
    expect(pageText()).toContain('data/tts/baked/');
  });
});

/* ---- the four honest outcome categories ---------------------------------- */

describe('7. a batch reports all four server categories separately', () => {
  it('renders each category with its count and the names from the buttons route', async () => {
    const backend = fakeBackend({
      bakeUsage: USAGE,
      bakeAll: {
        baked: [1],
        skipped_current: [2],
        skipped_dynamic: [3],
        failed: [{ button: 4, error: 'espeak-ng exited with status 1' }]
      }
    });
    renderPanel();
    await vi.waitFor(() => expect(buttonByText('Re-render all static phrases')).toBeDefined());

    buttonByText('Re-render all static phrases')?.click();
    await vi.waitFor(() => expect(pageText()).toContain('Baked now'));

    expect(callsTo(backend, '/api/tts/bake_all', 'POST')).toHaveLength(1);
    // The names are asked for beside the batch, so entries read as names, not ids.
    expect(callsTo(backend, '/api/buttons')).toHaveLength(1);

    const sections = [...document.querySelectorAll<HTMLElement>('section.bake-section')].map(
      (section) => ({
        label: (section.querySelector('h4')?.textContent ?? '').trim(),
        count: (section.querySelector('.bake-count')?.textContent ?? '').replace(/\s+/g, ' ').trim(),
        entries: [...section.querySelectorAll('li')].map((entry) => (entry.textContent ?? '').trim())
      })
    );
    expect(sections).toEqual([
      { label: 'Baked now', count: '1 button', entries: ['Front Door'] },
      { label: 'Already up to date', count: '1 button', entries: ['Porch'] },
      { label: 'Rendered on every press', count: '1 button', entries: ['Hall'] },
      {
        label: 'Failed',
        count: '1 button',
        entries: ['Gate — espeak-ng exited with status 1']
      }
    ]);
    expect(pageText()).toContain('Rendered on every press');
  });

  it('keeps a zero category visible with an explicit None.', async () => {
    fakeBackend({
      bakeUsage: USAGE,
      bakeAll: { baked: [1], skipped_current: [], skipped_dynamic: [3], failed: [] }
    });
    renderPanel();
    await vi.waitFor(() => expect(buttonByText('Re-render all static phrases')).toBeDefined());

    buttonByText('Re-render all static phrases')?.click();
    await vi.waitFor(() => expect(pageText()).toContain('Baked now'));

    expect(document.querySelectorAll('section.bake-section')).toHaveLength(4);
    expect(
      [...document.querySelectorAll('.bake-none')].map((element) => element.textContent?.trim())
    ).toEqual(['None.', 'None.']);
    // The zero is printed beside the heading, never dropped.
    expect(pageText()).toContain('0 buttons');
  });

  it('falls back to the button id when the name lookup fails', async () => {
    const backend = fakeBackend({
      bakeUsage: USAGE,
      buttonsError: 'the buttons route is unavailable',
      bakeAll: { baked: [7], skipped_current: [], skipped_dynamic: [], failed: [] }
    });
    renderPanel();
    await vi.waitFor(() => expect(buttonByText('Re-render all static phrases')).toBeDefined());

    buttonByText('Re-render all static phrases')?.click();
    await vi.waitFor(() => expect(pageText()).toContain('Baked now'));

    expect(callsTo(backend, '/api/buttons')).toHaveLength(1);
    expect(pageText()).toContain('Button #7');
  });
});

/* ---- the synchronous route is never pretended to be instant -------------- */

describe('8. the batch is announced while it runs', () => {
  it('shows a status line and a busy button until the server answers', async () => {
    const gate = deferred();
    fakeBackend({
      bakeUsage: USAGE,
      bakeGate: gate.promise,
      bakeAll: { baked: [1], skipped_current: [], skipped_dynamic: [], failed: [] }
    });
    renderPanel();
    await vi.waitFor(() => expect(buttonByText('Re-render all static phrases')).toBeDefined());

    const button = buttonByText('Re-render all static phrases');
    button?.click();
    await vi.waitFor(() => expect(pageText()).toContain('Re-rendering every static phrase'));

    const status = [...document.querySelectorAll('[role="status"]')].map((element) =>
      (element.textContent ?? '').replace(/\s+/g, ' ').trim()
    );
    expect(status).toContain('Re-rendering every static phrase');
    expect(button?.disabled).toBe(true);
    expect(button?.getAttribute('aria-busy')).toBe('true');
    expect(pageText()).toContain('can take a while');

    gate.resolve();
    await vi.waitFor(() => expect(pageText()).toContain('Baked now'));
    expect(pageText()).not.toContain('Re-rendering every static phrase');
    expect(button?.disabled).toBe(false);
  });
});

/* ---- refusals are the server's words, not ours --------------------------- */

describe('9. a refused batch is printed verbatim', () => {
  it('renders the 503 sentence and no report', async () => {
    const message = 'no speech source is prepared yet; check the doorbell log';
    const backend = fakeBackend({ bakeUsage: USAGE, bakeAll: { status: 503, message } });
    renderPanel();
    await vi.waitFor(() => expect(buttonByText('Re-render all static phrases')).toBeDefined());

    buttonByText('Re-render all static phrases')?.click();
    await vi.waitFor(() => expect(pageText()).toContain(message));

    expect(document.querySelector('[role="alert"]')?.textContent).toContain(message);
    expect(pageText()).not.toContain('Baked now');
    expect(pageText()).not.toContain('None.');
    // A refused batch wrote nothing, so the figures are not re-read.
    expect(callsTo(backend, '/api/tts/bake_usage')).toHaveLength(1);
  });

  it('says the answer did not describe the batch when the body is not a report', async () => {
    fakeBackend({ bakeUsage: USAGE, bakeAllRaw: { baked: 'nope' } });
    renderPanel();
    await vi.waitFor(() => expect(buttonByText('Re-render all static phrases')).toBeDefined());

    buttonByText('Re-render all static phrases')?.click();
    await vi.waitFor(() => expect(pageText()).toContain('did not describe the batch'));

    expect(pageText()).not.toContain('Baked now');
  });
});

/* ---- the figures follow the batch ---------------------------------------- */

describe('10. the figures are re-read after a batch changes them', () => {
  it('asks for the usage a second time and renders the new bytes', async () => {
    const backend = fakeBackend({
      bakeUsage: { baked: { files: 1, bytes: 1000 }, cache: { files: 0, bytes: 0 } },
      bakeAll: { baked: [1], skipped_current: [], skipped_dynamic: [], failed: [] }
    });
    renderPanel();
    await vi.waitFor(() => expect(pageText()).toContain('1 file · 1000 bytes'));

    // The batch wrote a second file; the next usage answer says so.
    backend.options.bakeUsage = {
      baked: { files: 2, bytes: 5000 },
      cache: { files: 0, bytes: 0 }
    };
    buttonByText('Re-render all static phrases')?.click();
    await vi.waitFor(() => expect(pageText()).toContain('2 files · 4.9 kB'));

    expect(callsTo(backend, '/api/tts/bake_usage')).toHaveLength(2);
  });
});

/* ---- an empty batch is one sentence, not four silent lists --------------- */

describe('11. an empty batch says so once', () => {
  it('renders one plain sentence instead of four empty categories', async () => {
    fakeBackend({ bakeUsage: USAGE, bakeAll: EMPTY_REPORT });
    renderPanel();
    await vi.waitFor(() => expect(buttonByText('Re-render all static phrases')).toBeDefined());

    buttonByText('Re-render all static phrases')?.click();
    await vi.waitFor(() =>
      expect(pageText()).toContain('reported no button in any of the four categories')
    );

    expect(document.querySelectorAll('section.bake-section')).toHaveLength(0);
    expect(pageText()).not.toContain('Baked now');
    expect(pageText()).not.toContain('None.');
  });
});
