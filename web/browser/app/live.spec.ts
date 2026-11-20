import { expect, test, type Page } from '@playwright/test';
import {
  FRONT_DOOR,
  HALLWAY_TEMPERATURE,
  LIVING_ROOM_HUMIDITY,
  SEED,
  pushHaState,
  readEntities,
  resetBackend,
  seedTile,
  watchOffOriginRequests,
  type OffOriginWatch
} from './fixtures.js';

/**
 * Live data, through the **real** websocket.
 *
 * The bench pushes the change through the *fake Home Assistant peer* into the
 * real `HomeAssistantWebSocketClient`, so the delta travels the production path:
 * HA peer → client → hub → `/ws` → store → tile. Nothing here pokes the DOM.
 *
 * Two independent facts prove "no reload": the page-load counter (a reload fires
 * another `load`) and a value stamped on `window` (a reload starts a fresh JS
 * realm and loses it). A locator alone would not: Playwright re-resolves it.
 *
 * The hub sends `displayed_state`/`unit_of_measurement`/`tone`/`attribute_hints`
 * on every `entity_state` frame from the same `presentation_hints` call a
 * snapshot row uses (HSD-7oto, extended by HSD-d5x2 with the tone), so a delta
 * repaints the word, the unit and the colour together.
 */

/** Every tile on the page, whatever its staleness. */
const TILES = 'ul.tiles li.tile';

/** The off-origin watch the `beforeEach` attached; asserted at the end of each test. */
let watch: OffOriginWatch;

test.beforeEach(async ({ page, request }) => {
  await resetBackend(request);
  watch = watchOffOriginRequests(page);
});

/** A typed window for the "this document is the one that answered" stamp. */
type MarkedWindow = Window & { __tileMark?: string };

/** Paint the dashboard, wait for every tile to be live, and stamp the document. */
async function openLiveDashboard(page: Page): Promise<void> {
  await page.goto('/');
  await expect(page.locator(TILES)).toHaveCount(SEED.length);
  await expect(page.locator(`${TILES}[data-stale="false"]`)).toHaveCount(SEED.length);
  await page.evaluate(() => {
    (window as MarkedWindow).__tileMark = 'alive';
  });
}

test('an entity_state delta reaches the tile in place, without a reload', async ({
  page,
  request
}) => {
  let loads = 0;
  page.on('load', () => {
    loads += 1;
  });

  await openLiveDashboard(page);
  expect(loads).toBe(1);

  const tile = page.locator(`li.tile[data-entity="${FRONT_DOOR}"]`);
  await expect(tile).toHaveAttribute('data-kind', 'ok');

  await pushHaState(request, FRONT_DOOR, 'unlocked', { friendly_name: 'Front Door' });

  // `unlocked` is the safety case: the delta carries the tone the backend
  // resolved for it (`alert`, from the built-in table), so the tile repaints
  // green → red with no snapshot and no reload.
  await expect(tile).toHaveAttribute('data-kind', 'alert');

  // Same document: a reload would reset `loads` and drop the mark.
  expect(loads).toBe(1);
  expect(await page.evaluate(() => (window as MarkedWindow).__tileMark)).toBe('alive');

  watch.assertAllLocal();
});

test('a numeric delta is what the real store reports back', async ({ page, request }) => {
  await openLiveDashboard(page);

  await pushHaState(request, HALLWAY_TEMPERATURE, '18.5', {
    friendly_name: 'Hallway Temperature',
    unit_of_measurement: '°C'
  });

  // The honest seam: `GET /api/entities` resolves each row against the live Home
  // Assistant context, so this is the fake peer's state read through the real
  // route — not a value this spec wrote into a DOM node.
  const rows = await readEntities(request);
  const temperature = rows.find((row) => row.entity_id === HALLWAY_TEMPERATURE);
  expect(temperature, `${HALLWAY_TEMPERATURE} must be a stored row`).toBeDefined();
  expect(temperature?.state).toBe('18.5');

  watch.assertAllLocal();
});

test('the delta is the one the store holds, not a DOM-only change', async ({ page, request }) => {
  await openLiveDashboard(page);

  await pushHaState(request, FRONT_DOOR, 'unlocked', { friendly_name: 'Front Door' });
  await expect(page.locator(`li.tile[data-entity="${FRONT_DOOR}"]`)).toHaveAttribute(
    'data-kind',
    'alert'
  );

  const rows = await readEntities(request);
  const frontDoor = rows.find((row) => row.entity_id === FRONT_DOOR);
  expect(frontDoor, `${FRONT_DOOR} must be a stored row`).toBeDefined();
  expect(frontDoor?.state).toBe('unlocked');

  watch.assertAllLocal();
});

/**
 * Guarantees that the word, unit and colour all follow a live delta in place,
 * with no reload and no snapshot.
 */
test('the tile word follows the delta', async ({ page, request }) => {
  await openLiveDashboard(page);

  const frontDoor = page.locator(`li.tile[data-entity="${FRONT_DOOR}"]`);
  const temperature = page.locator(`li.tile[data-entity="${HALLWAY_TEMPERATURE}"]`);
  const reading = seedTile(HALLWAY_TEMPERATURE);
  const unit = reading.unit;
  if (unit === null) {
    throw new Error(`${HALLWAY_TEMPERATURE} is the seed's unit-bearing tile`);
  }
  await expect(frontDoor.locator('.state .word')).toHaveText(seedTile(FRONT_DOOR).word);
  await expect(temperature.locator('.state .word')).toHaveText(reading.word);
  await expect(temperature.locator('.state .unit')).toHaveText(unit);

  await pushHaState(request, FRONT_DOOR, 'unlocked', { friendly_name: 'Front Door' });
  await pushHaState(request, HALLWAY_TEMPERATURE, '18.5', {
    friendly_name: 'Hallway Temperature',
    unit_of_measurement: '°C'
  });

  // Keep these bounds short: the presentation block should travel with the delta.
  // Title-cased by the backend (HSD-d5x2): the delta carries `Unlocked`.
  await expect(frontDoor.locator('.state .word')).toHaveText('Unlocked', { timeout: 3_000 });
  await expect(temperature.locator('.state .word')).toHaveText('18.5', { timeout: 3_000 });
  await expect(temperature.locator('.state .unit')).toHaveText(unit);

  watch.assertAllLocal();
});

test('an unavailable humidity delta clears its unit and recovers', async ({ page, request }) => {
  let loads = 0;
  page.on('load', () => {
    loads += 1;
  });

  await openLiveDashboard(page);
  expect(loads).toBe(1);

  const humidity = page.locator(`li.tile[data-entity="${LIVING_ROOM_HUMIDITY}"]`);
  const reading = seedTile(LIVING_ROOM_HUMIDITY);
  expect(reading.state).toBe('48');
  expect(reading.unit).toBe('%');
  await expect(humidity).toHaveAttribute('data-kind', 'value');
  await expect(humidity.locator('.state .word')).toHaveText('48');
  await expect(humidity.locator('.state .unit')).toHaveText('%');

  await pushHaState(request, LIVING_ROOM_HUMIDITY, 'unavailable', {
    friendly_name: 'Living Room Humidity'
  });
  await expect(humidity).toHaveAttribute('data-kind', 'unavailable', { timeout: 3_000 });
  await expect(humidity.locator('.state .word')).toHaveText('Unavailable', { timeout: 3_000 });
  await expect(humidity.locator('.state .unit')).toHaveCount(0, { timeout: 3_000 });

  await pushHaState(request, LIVING_ROOM_HUMIDITY, '55', {
    friendly_name: 'Living Room Humidity',
    unit_of_measurement: '%'
  });
  await expect(humidity).toHaveAttribute('data-kind', 'value', { timeout: 3_000 });
  await expect(humidity.locator('.state .word')).toHaveText('55', { timeout: 3_000 });
  await expect(humidity.locator('.state .unit')).toHaveText('%', { timeout: 3_000 });

  expect(loads).toBe(1);
  expect(await page.evaluate(() => (window as MarkedWindow).__tileMark)).toBe('alive');
  watch.assertAllLocal();
});
