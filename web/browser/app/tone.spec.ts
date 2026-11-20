import {
  expect,
  test,
  type APIRequestContext,
  type Locator,
  type Page
} from '@playwright/test';
import {
  SEED,
  pushHaState,
  readEntities,
  resetBackend,
  setTileCount,
  watchOffOriginRequests,
  type EntityRowShape,
  type OffOriginWatch
} from './fixtures.js';

/**
 * The tile's colour, decided by the **backend** and painted by the **browser**.
 *
 * This is the acceptance tier for HSD-d5x2, and it exists because Vitest cannot
 * prove what this ticket claims. A Vitest test types `tone: 'warn'` into a
 * fixture, so it can only say "given this tone, that class" — it would stay green
 * if the default table moved `running` to `alert`, if the store dropped
 * `state_tones` on a round trip, or if the hub stopped spending the override on
 * the `entity_state` frame. Here the chain is the real one end to end: the fake
 * Home Assistant peer → the real hub → the real store → the real `GET
 * /api/entities` and `/ws` snapshot → the real browser's paint. The tone is
 * resolved by `chime_deck/icons/tones.DEFAULT_STATE_TONES` and the per-tile
 * override, over the real wire, in whatever build `web/dist` is.
 *
 * Every claim is asserted twice on purpose: once on the DOM (`data-kind`, the
 * word) and once on the REST row (`tone`, `state_tones`, `colour_by_state`). The
 * DOM alone cannot tell "the backend said warn" from "the frontend guessed
 * warn", and after this ticket the frontend no longer has a table to guess with.
 */

/** Every tile on the page, whatever its staleness. */
const TILES = 'ul.tiles li.tile';

/** A tile with the first real snapshot behind it (staleness cleared). */
const LIVE_TILES = 'ul.tiles li.tile[data-stale="false"]';

/** The two synthetic tiles these tests create; neither is part of the seed. */
const WASHER = 'sensor.washer';
const DRYER = 'sensor.dryer';

/** The off-origin watch the `beforeEach` attached; asserted at the end of each test. */
let watch: OffOriginWatch;

test.use({ viewport: { width: 1280, height: 720 } });

test.beforeEach(async ({ page, request }) => {
  await resetBackend(request);
  watch = watchOffOriginRequests(page);
});

test.afterEach(async ({ request }) => {
  // The bench is one process for a whole run and `/__test/reset` re-upserts the
  // canonical rows but never deletes a row a spec added, so a `sensor.washer`
  // left behind would give the next spec file eight tiles instead of six. The
  // reset comes first (it restores the six) and the prune second (it deletes
  // everything that is not one of them), exactly as `layout.spec.ts` does.
  await resetBackend(request);
  await setTileCount(request, SEED.length);
});

/** One tile this spec created, kept by numeric id because PATCH/DELETE use it. */
interface CreatedTile {
  readonly id: number;
  readonly entityId: string;
}

/**
 * Create one tile through the real REST route.
 *
 * `POST /api/entities`, not a seeded row: the seed's six entities are a
 * documented contract this spec must not disturb, and a tile the spec owns is
 * the only one it may recolour.
 *
 * @param request the test's API request context.
 * @param entityId the Home Assistant entity id the tile watches.
 * @param displayName the name the dashboard will show.
 */
async function createTile(
  request: APIRequestContext,
  entityId: string,
  displayName: string
): Promise<CreatedTile> {
  const response = await request.post('/api/entities', {
    data: { entity_id: entityId, display_name: displayName }
  });
  expect(response.ok(), await response.text()).toBe(true);
  const row = (await response.json()) as EntityRowShape;
  expect(row.entity_id).toBe(entityId);
  return { id: row.id, entityId: row.entity_id };
}

/**
 * `PATCH /api/entities/{id}` — the one write a tile's colour config has.
 *
 * @param request the test's API request context.
 * @param id the stored row's `id`, not its `entity_id`.
 * @param body the fields to change; omitted fields keep their stored value.
 */
async function patchTile(
  request: APIRequestContext,
  id: number,
  body: Record<string, unknown>
): Promise<void> {
  const response = await request.patch(`/api/entities/${id}`, { data: body });
  expect(response.ok(), await response.text()).toBe(true);
}

/**
 * One row of `GET /api/entities` by entity id, or a throw when it is missing.
 *
 * Throws rather than returning `undefined` so a spec's assertion reads
 * `row.tone` instead of `row?.tone`, which would pass on an absent row.
 *
 * @param request the test's API request context.
 * @param entityId the Home Assistant entity id to find.
 */
async function readRow(request: APIRequestContext, entityId: string): Promise<EntityRowShape> {
  const rows = await readEntities(request);
  const row = rows.find((candidate) => candidate.entity_id === entityId);
  if (row === undefined) {
    throw new Error(`${entityId} is not a stored row`);
  }
  return row;
}

/**
 * The dashboard tile for one entity.
 *
 * @param page the page showing the dashboard.
 * @param entityId the Home Assistant entity id the tile carries.
 */
function tileOf(page: Page, entityId: string): Locator {
  return page.locator(`li.tile[data-entity="${entityId}"]`);
}

/**
 * Open the dashboard and wait until every tile is painted from the live
 * snapshot, so no assertion races the first frame.
 *
 * @param page the page to open.
 * @param expected how many tiles the dashboard must show.
 */
async function openDashboard(page: Page, expected: number): Promise<void> {
  await page.goto('/');
  await expect(page.locator(TILES)).toHaveCount(expected);
  await expect(page.locator(LIVE_TILES)).toHaveCount(expected);
}

/**
 * The `data-kind` and `.state .word` the browser actually painted for one tile.
 *
 * Read, not merely asserted: a test that only says "this one is warn" cannot see
 * that its neighbour drifted to the same value, which is the failure a
 * tone table regression produces. Call it only once the caller has awaited both
 * tiles' `data-kind` and word: those retries are what make this read race-free.
 *
 * @param tile the tile locator.
 */
async function painted(tile: Locator): Promise<{ kind: string | null; word: string }> {
  const kind = await tile.getAttribute('data-kind');
  const word = await tile.locator('.state .word').textContent();
  if (word === null) {
    throw new Error('the tile must render a state word');
  }
  return { kind, word: word.trim() };
}

test('a running tile and an idle tile differ in tone AND word', async ({ page, request }) => {
  // Two tiles the default table treats differently: `running` is building work
  // in progress (amber) and `idle` is a machine at rest (grey). Both colours come
  // from `DEFAULT_STATE_TONES`; neither tile stores a mapping, so a backend that
  // forgot to resolve the default would paint two identical grey tiles.
  await createTile(request, WASHER, 'Washer');
  await createTile(request, DRYER, 'Dryer');
  await pushHaState(request, WASHER, 'running', { friendly_name: 'Washer' });
  await pushHaState(request, DRYER, 'idle', { friendly_name: 'Dryer' });

  await openDashboard(page, SEED.length + 2);
  const washer = tileOf(page, WASHER);
  const dryer = tileOf(page, DRYER);

  await expect(washer).toHaveAttribute('data-kind', 'warn');
  await expect(dryer).toHaveAttribute('data-kind', 'off');
  await expect(washer.locator('.state .word')).toHaveText('Running');
  await expect(dryer.locator('.state .word')).toHaveText('Idle');

  // Both facts compared, not just each asserted: if a regression painted every
  // tile the same colour and the same word, two independent `toBe`s could still
  // pass only by both being right, while these two comparisons name the defect.
  const washerPaint = await painted(washer);
  const dryerPaint = await painted(dryer);
  expect(washerPaint.kind, 'the two tones must differ').not.toBe(dryerPaint.kind);
  expect(washerPaint.word, 'the two words must differ').not.toBe(dryerPaint.word);

  // And the backend's own answer: the tone on each row, with no stored mapping
  // anywhere. `state_tones: null` is the proof the colour was resolved from the
  // default table at request time rather than read out of the row.
  const washerRow = await readRow(request, WASHER);
  const dryerRow = await readRow(request, DRYER);
  expect(washerRow.state).toBe('running');
  expect(washerRow.tone).toBe('warn');
  expect(washerRow.state_tones).toBeNull();
  expect(dryerRow.state).toBe('idle');
  expect(dryerRow.tone).toBe('off');
  expect(dryerRow.state_tones).toBeNull();

  watch.assertAllLocal();
});

test('a per-tile override beats the default for that tile only', async ({ page, request }) => {
  // The point of "overrides only": one tile may disagree with the built-in table
  // without freezing the table for every other tile. Both tiles are in the SAME
  // state here, so the only thing that can explain a different colour is the
  // stored override — a state-driven frontend table could not produce it.
  await createTile(request, WASHER, 'Washer');
  const dryer = await createTile(request, DRYER, 'Dryer');
  await pushHaState(request, WASHER, 'running', { friendly_name: 'Washer' });
  await pushHaState(request, DRYER, 'running', { friendly_name: 'Dryer' });
  await patchTile(request, dryer.id, { state_tones: { running: 'alert' } });

  await openDashboard(page, SEED.length + 2);
  const washerTile = tileOf(page, WASHER);
  const dryerTile = tileOf(page, DRYER);

  await expect(dryerTile).toHaveAttribute('data-kind', 'alert');
  await expect(washerTile).toHaveAttribute('data-kind', 'warn');
  // The colour changed, the word did not: an override recolours a state, it does
  // not rename it. Two tiles in `running` still both say `Running`.
  await expect(dryerTile.locator('.state .word')).toHaveText('Running');
  await expect(washerTile.locator('.state .word')).toHaveText('Running');

  const washerPaint = await painted(washerTile);
  const dryerPaint = await painted(dryerTile);
  expect(washerPaint.word).toBe(dryerPaint.word);
  expect(washerPaint.kind, 'the override must not leak to the sibling').not.toBe(dryerPaint.kind);

  // The REST rows say which one carries the mapping, so the DOM difference is
  // explained by stored configuration rather than by anything the browser did.
  const washerRow = await readRow(request, WASHER);
  const dryerRow = await readRow(request, DRYER);
  expect(washerRow.state_tones).toBeNull();
  expect(washerRow.tone).toBe('warn');
  expect(dryerRow.state_tones).toEqual({ running: 'alert' });
  expect(dryerRow.tone).toBe('alert');

  watch.assertAllLocal();
});

test('colour_by_state false is neutral in every state while the word still updates', async ({
  page,
  request
}) => {
  // The opt-out, for a tile whose colour would be noise: it stays grey in every
  // state, and the word is what carries the news. `running` and `finished` are
  // both mapped by the default table (warn and ok), so a tile that ignored
  // `colour_by_state` would wear two different colours here.
  const created = await createTile(request, WASHER, 'Washer');
  await patchTile(request, created.id, { colour_by_state: false });
  await pushHaState(request, WASHER, 'running', { friendly_name: 'Washer' });

  await openDashboard(page, SEED.length + 1);
  const tile = tileOf(page, WASHER);
  await expect(tile).toHaveAttribute('data-kind', 'off');
  await expect(tile.locator('.state .word')).toHaveText('Running');

  // A live delta, through the fake Home Assistant peer: the word moves to the
  // next state while the colour stays neutral. Bounded, like the live spec's
  // delta assertions, because a repaint should travel with the frame.
  await pushHaState(request, WASHER, 'finished', { friendly_name: 'Washer' });
  await expect(tile.locator('.state .word')).toHaveText('Finished', { timeout: 3_000 });
  await expect(tile).toHaveAttribute('data-kind', 'off');

  const row = await readRow(request, WASHER);
  expect(row.state).toBe('finished');
  expect(row.colour_by_state).toBe(false);
  // `ok` is what the default table says for `finished`; the row's tone is grey
  // because the opt-out is applied by the backend, not by the browser.
  expect(row.tone).toBe('off');

  watch.assertAllLocal();
});

test('an unmapped state is grey with its raw word', async ({ page, request }) => {
  // The grey-and-raw rule: the table is a convenience, not a vocabulary, so a
  // state nobody mapped still renders its own word instead of being hidden or
  // dressed as "good". `suds-lock` also proves the title-case rule only touches
  // the first letter — the hyphen survives, as it does for `pre-wash`.
  await createTile(request, WASHER, 'Washer');
  await pushHaState(request, WASHER, 'suds-lock', { friendly_name: 'Washer' });

  await openDashboard(page, SEED.length + 1);
  const tile = tileOf(page, WASHER);
  await expect(tile).toHaveAttribute('data-kind', 'off');
  await expect(tile.locator('.state .word')).toHaveText('Suds-lock');

  const row = await readRow(request, WASHER);
  expect(row.state).toBe('suds-lock');
  expect(row.tone).toBe('off');

  watch.assertAllLocal();
});

test('unavailable is never mapped', async ({ page, request }) => {
  // A tone is a claim about a state; "unavailable" is the absence of one. The
  // backend checks that before any lookup, so even an explicit `alert` override
  // cannot colour it — and the tile keeps its own `unavailable` kind, which is
  // the honesty rule the dashboard has always had. Without this the owner could
  // make a dead sensor look alarming-but-alive.
  const created = await createTile(request, WASHER, 'Washer');
  await patchTile(request, created.id, { state_tones: { unavailable: 'alert' } });
  await pushHaState(request, WASHER, 'unavailable', { friendly_name: 'Washer' });

  await openDashboard(page, SEED.length + 1);
  const tile = tileOf(page, WASHER);
  await expect(tile).toHaveAttribute('data-kind', 'unavailable');
  await expect(tile.locator('.state .word')).toHaveText('Unavailable');

  // The override really is stored — this is not a mapping that failed to save —
  // and the backend still refuses to spend it on an absent state.
  const row = await readRow(request, WASHER);
  expect(row.state).toBe('unavailable');
  expect(row.state_tones).toEqual({ unavailable: 'alert' });
  expect(row.tone).toBe('off');

  watch.assertAllLocal();
});
