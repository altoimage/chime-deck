import { expect, type APIRequestContext, type Page, type Request } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/**
 * Shared helpers for the specs that run against the **real** backend bench
 * (`browser/backend/server.py`, port 4174).
 *
 * The bench seeds a fixed set of rows; `SEED` below is that contract in typed
 * form, so no spec has to restate an entity id, a tile name, a state or a unit
 * and a later change to the seed fails here instead of drifting silently.
 *
 * Nothing in this module is a Playwright fixture on purpose: a spec that wants a
 * clean bench in `beforeEach` calls `resetBackend()`, and a spec that wants the
 * off-origin watch calls `watchOffOriginRequests(page)` and asserts the returned
 * checker at the end. Explicit calls read in the spec, and no hidden setup can
 * apply to one test but not its neighbour.
 */

/** The bench's origin; every request a spec makes must land here. */
export const BENCH_ORIGIN = 'http://127.0.0.1:4174';

/** `lock.front_door` — the tile the live-delta and icon-editor specs drive. */
export const FRONT_DOOR = 'lock.front_door';

/** `sensor.hallway_temperature` — the tile with a unit. */
export const HALLWAY_TEMPERATURE = 'sensor.hallway_temperature';

/** `light.kitchen_ceiling` — seeded `on`. */
export const KITCHEN_CEILING = 'light.kitchen_ceiling';

/** `binary_sensor.garage_door` — seeded `open`, so `warn`. */
export const GARAGE_DOOR = 'binary_sensor.garage_door';

/** `lock.back_door` — seeded `unavailable`, the honesty case. */
export const BACK_DOOR = 'lock.back_door';

/** `sensor.living_room_humidity` — seeded `48` `%`. */
export const LIVING_ROOM_HUMIDITY = 'sensor.living_room_humidity';

/** One seeded tile, in the dashboard's own order (`sort_order` 1..6). */
export interface SeedTile {
  readonly entityId: string;
  /** The tile name the seed stores as `display_name`. */
  readonly name: string;
  /** The initial Home Assistant state, verbatim. */
  readonly state: string;
  /**
   * The word the backend renders beside the state (`displayed_state`).
   *
   * Not a re-spelling of `state`: HSD-d5x2 made the backend title-case every
   * word, so `locked` renders `Locked` and `pre-wash` renders `Pre-wash`. It is
   * a separate field because it is a separate fact — a spec that wants the tile
   * word must assert this one, and a change to the casing rule then fails here
   * instead of in six specs at once.
   */
  readonly word: string;
  /** The unit Home Assistant reports, or `null` when the tile has none. */
  readonly unit: string | null;
}

/** The canonical seed, in dashboard order — the fixed contract of the bench. */
export const SEED: readonly SeedTile[] = [
  { entityId: FRONT_DOOR, name: 'Front Door', state: 'locked', word: 'Locked', unit: null },
  {
    entityId: HALLWAY_TEMPERATURE,
    name: 'Hallway Temperature',
    state: '21.4',
    word: '21.4',
    unit: '°C'
  },
  { entityId: KITCHEN_CEILING, name: 'Kitchen Ceiling', state: 'on', word: 'On', unit: null },
  { entityId: GARAGE_DOOR, name: 'Garage Door', state: 'open', word: 'Open', unit: null },
  {
    entityId: BACK_DOOR,
    name: 'Back Door',
    state: 'unavailable',
    word: 'Unavailable',
    unit: null
  },
  {
    entityId: LIVING_ROOM_HUMIDITY,
    name: 'Living Room Humidity',
    state: '48',
    word: '48',
    unit: '%'
  }
];

/** The seeded button's name; its Test action is labelled `Test <name>`. */
export const CHIME_BUTTON_NAME = 'Front Door Chime';

/** The kind of an `EntityResponse` row from `GET /api/entities`, as this tier reads it. */
export interface EntityRowShape {
  readonly id: number;
  readonly entity_id: string;
  readonly icon_override: string | null;
  readonly state: string | null;
  /**
   * The tone the backend resolved for the row's **current** state, or `null`
   * when it has no state to colour.
   *
   * Read from the row rather than inferred from the DOM: the colour is the
   * backend's claim (HSD-d5x2), and a spec that only reads `data-kind` cannot
   * tell "the backend said `warn`" from "the frontend guessed `warn`".
   */
  readonly tone: string | null;
  /** The tile's explicit state→tone overrides, or `null` when it has none. */
  readonly state_tones: Record<string, string> | null;
  /** False when the tile opted out of state colouring (always neutral). */
  readonly colour_by_state: boolean;
}

/** One `HistoryRow` from `GET /api/history`, as this tier reads it. */
export interface HistoryRowShape {
  readonly id: number;
  readonly button_id: number | null;
  readonly button_name: string;
  readonly pressed_at: string;
  readonly source: string;
  readonly outcome: string;
}

/** `GET /api/history`'s body. */
export interface HistoryBodyShape {
  readonly rows: readonly HistoryRowShape[];
}

/** `GET /api/settings`'s body. */
export interface SettingsBodyShape {
  readonly settings: Record<string, unknown>;
  readonly secrets: Record<string, boolean>;
}

/**
 * Re-apply the canonical seed and the initial Home Assistant states.
 *
 * Runs in every `beforeEach`, so no test can inherit the previous one's writes
 * (a saved URL, an icon override, a pressed button). It is idempotent and cheap:
 * the bench upserts rows in place and re-pushes the initial states.
 *
 * @param request the test's API request context (already bound to the bench).
 */
export async function resetBackend(request: APIRequestContext): Promise<void> {
  const response = await request.post('/__test/reset');
  expect(response.ok(), await response.text()).toBe(true);
}

/**
 * Push one state through the fake Home Assistant peer and wait until the hub's
 * own snapshot has it, so a spec never races the websocket frame.
 *
 * @param request the test's API request context.
 * @param entityId the entity whose state changes.
 * @param state the new raw state, verbatim.
 * @param attributes the attributes Home Assistant reports alongside it.
 */
export async function pushHaState(
  request: APIRequestContext,
  entityId: string,
  state: string,
  attributes: Record<string, unknown> = {}
): Promise<void> {
  const response = await request.post('/__test/ha_state', {
    data: { entity_id: entityId, state, attributes }
  });
  expect(response.ok(), await response.text()).toBe(true);
}

/**
 * One seeded tile by its entity id, or a throw when the id is not in the seed.
 *
 * @param entityId the Home Assistant entity id to look up.
 */
export function seedTile(entityId: string): SeedTile {
  const tile = SEED.find((candidate) => candidate.entityId === entityId);
  if (tile === undefined) {
    throw new Error(`${entityId} is not in the canonical bench seed`);
  }
  return tile;
}

/** The off-origin watch `watchOffOriginRequests` hands back. */
export interface OffOriginWatch {
  /**
   * Assert the browser never left the bench origin.
   *
   * Call it at the end of a test. This is the machine-checkable form of the
   * tier's "fully offline, no test reaches the network" rule (AGENTS.md HARD
   * RULE 4): the bench is a loopback process serving both the app and its API,
   * so a request to any other origin is either a leaked external dependency or
   * a spec that quietly started talking to something real.
   */
  assertAllLocal(): void;
}

/**
 * Watch every request the page makes and fail when one leaves the bench.
 *
 * Only `http:`/`https:` requests are checked (a `data:` URI or a `blob:` URL is
 * not a network call); the websocket is `ws:` and lives on the same loopback
 * port, so it is not something this watch needs to police. Attach the watch
 * before `page.goto()` — a request made during the first paint must be seen.
 *
 * @param page the page whose requests are watched.
 */
export function watchOffOriginRequests(page: Page): OffOriginWatch {
  const seen: string[] = [];

  page.on('request', (request: Request) => {
    const url = request.url();
    if (!url.startsWith('http:') && !url.startsWith('https:')) {
      return;
    }
    if (new URL(url).origin !== BENCH_ORIGIN) {
      seen.push(`${request.method()} ${url}`);
    }
  });

  return {
    assertAllLocal: () => {
      expect(seen, 'every request must stay on the bench origin').toEqual([]);
    }
  };
}

/** A PNG's intrinsic size, read from its IHDR chunk. */
export interface PngSize {
  readonly width: number;
  readonly height: number;
}

/**
 * `GET /api/entities` — every stored row with its live state, as the real REST
 * store answers it.
 *
 * This is the API-side proof a spec needs when the question is "did the backend
 * actually change?", rather than "did the DOM redraw?".
 *
 * @param request the test's API request context.
 */
export async function readEntities(request: APIRequestContext): Promise<EntityRowShape[]> {
  const response = await request.get('/api/entities');
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as EntityRowShape[];
}

/**
 * Force the dashboard to show exactly `count` tiles.
 *
 * The bench's seed is six fixed rows, which is why every spec until now could
 * assume that number. A layout spec cannot: it has to ask for 1, 2, 4 or 9.
 *
 * Why the helper prunes before it adds: `POST /__test/reset` re-upserts the
 * canonical rows but does **not** delete a row a spec added, nor re-create one
 * a spec deleted — the store is only ever told what the seed says. So a
 * `sensor.layout_extra_*` from an earlier test would accumulate, and a trimmed
 * canonical row would stay trimmed. Both are repaired here instead of trusted:
 * anything that is not one of the six canonical ids is deleted, canonical rows
 * are dropped from the end of the dashboard order until the total is at most
 * `count`, synthetic rows are appended until it is exactly `count`, and a fresh
 * `GET` is what proves it. `beforeEach`'s `resetBackend()` re-upserts the six,
 * so these deletions heal themselves.
 *
 * @param request the test's API request context (already bound to the bench).
 * @param count how many tiles the dashboard must show.
 */
export async function setTileCount(request: APIRequestContext, count: number): Promise<void> {
  const canonical = new Set(SEED.map((tile) => tile.entityId));
  const rows = await readEntities(request);

  for (const row of rows.filter((candidate) => !canonical.has(candidate.entity_id))) {
    await deleteEntity(request, row.id);
  }

  // `GET /api/entities` answers in `sort_order`, so the tail of this list is the
  // tail of the dashboard: the rows a trimmed count drops are the ones a user
  // sees last, not an arbitrary subset.
  const canonicalRows = rows.filter((row) => canonical.has(row.entity_id));
  const survivors = canonicalRows.slice(0, Math.max(0, count));
  for (const row of canonicalRows.slice(survivors.length)) {
    await deleteEntity(request, row.id);
  }

  // The route requires only `entity_id`, and that id must match the API's
  // `[a-z0-9_]+\.[a-z0-9_]+` shape; the icon and the name keep the tile looking
  // like a tile rather than an empty row.
  for (let total = survivors.length; total < count; total += 1) {
    const response = await request.post('/api/entities', {
      data: {
        entity_id: `sensor.layout_extra_${total}`,
        display_name: `Layout Extra ${total}`,
        icon: 'mdi:door'
      }
    });
    expect(response.ok(), await response.text()).toBe(true);
  }

  expect((await readEntities(request)).length).toBe(count);
}

/**
 * `DELETE /api/entities/{id}` — one row by its numeric primary key.
 *
 * @param request the test's API request context.
 * @param id the stored row's `id`, not its `entity_id`.
 */
async function deleteEntity(request: APIRequestContext, id: number): Promise<void> {
  const response = await request.delete(`/api/entities/${id}`);
  expect(response.ok(), await response.text()).toBe(true);
}

/**
 * `GET /api/settings` — every non-secret setting plus the secret flags.
 *
 * @param request the test's API request context.
 */
export async function readSettings(request: APIRequestContext): Promise<SettingsBodyShape> {
  const response = await request.get('/api/settings');
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as SettingsBodyShape;
}

/**
 * `GET /api/history?limit=1` — the newest press-log row, or `null` when the log
 * is empty.
 *
 * @param request the test's API request context.
 */
export async function readLatestPress(
  request: APIRequestContext
): Promise<HistoryRowShape | null> {
  const response = await request.get('/api/history?limit=1');
  expect(response.ok(), await response.text()).toBe(true);
  const body = (await response.json()) as HistoryBodyShape;
  return body.rows[0] ?? null;
}

/** The eight bytes every PNG starts with. */
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/**
 * Read a PNG's width and height from the file's own header.
 *
 * Deliberately from the bytes rather than from anything the browser reports: a
 * screenshot taken at the wrong device-pixel ratio or with a wrong clip is a
 * different file, and only the file itself can prove otherwise.
 *
 * @param path the PNG file to read.
 * @returns its pixel width and height.
 */
export function pngSize(path: string): PngSize {
  const bytes = readFileSync(path);
  if (bytes.length < 24 || !bytes.subarray(0, 8).equals(PNG_SIGNATURE)) {
    throw new Error(`${path} is not a PNG file`);
  }
  if (bytes.subarray(12, 16).toString('latin1') !== 'IHDR') {
    throw new Error(`${path} does not start with an IHDR chunk`);
  }
  return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}

/**
 * Where the screenshot spec writes its images (`web/browser/artifacts/`).
 *
 * Git-ignored: a run output, regenerated by the suite, never an input.
 */
export const ARTIFACTS_DIR = fileURLToPath(new URL('../artifacts/', import.meta.url));
