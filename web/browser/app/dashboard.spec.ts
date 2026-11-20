import { expect, test, type Page } from '@playwright/test';
import {
  BACK_DOOR,
  FRONT_DOOR,
  GARAGE_DOOR,
  HALLWAY_TEMPERATURE,
  SEED,
  readSettings,
  resetBackend,
  seedTile,
  watchOffOriginRequests,
  type OffOriginWatch,
  type SettingsBodyShape
} from './fixtures.js';

/**
 * The dashboard, painted by the **real** backend: a real seeded SQLite store,
 * the real `/api/entities` REST read and the real `/ws` snapshot frame. Nothing
 * here is served by a mock, so this is the tier that can say "the shipped
 * dashboard renders the shipped data".
 *
 * The seed (see `browser/backend/server.py`) is a contract, not a fixture this
 * spec may tune: six tiles in a fixed order, one of them `unavailable` and one
 * of them numeric with a unit.
 */

/** Every tile on the page, whatever its staleness. */
const TILES = 'ul.tiles li.tile';

/** A tile with the first real snapshot behind it (staleness cleared). */
const LIVE_TILES = 'ul.tiles li.tile[data-stale="false"]';

/** The off-origin watch the `beforeEach` attached; asserted at the end of each test. */
let watch: OffOriginWatch;

/** One `getBoundingClientRect()` reading, narrowed to what a spec compares. */
interface BoxRect {
  readonly left: number;
  readonly right: number;
  readonly top: number;
  readonly bottom: number;
  readonly width: number;
  readonly height: number;
}

/** The rail and tile geometry collected in one page evaluation. */
interface RailGeometry {
  readonly innerWidth: number;
  readonly innerHeight: number;
  readonly rail: BoxRect;
  readonly controls: readonly BoxRect[];
  readonly tiles: readonly BoxRect[];
}

test.beforeEach(async ({ page, request }) => {
  await resetBackend(request);
  watch = watchOffOriginRequests(page);
});

test('paints the six seeded tiles in the seed order', async ({ page }) => {
  await page.goto('/');

  await expect(page.locator(TILES)).toHaveCount(SEED.length);
  // One assertion over the whole list: the names in the DOM order the dashboard
  // renders, which is the store's `sort_order`, not the seed's insertion order.
  await expect(page.locator(`${TILES} .name`)).toHaveText(SEED.map((tile) => tile.name));

  for (const tile of SEED) {
    await expect(page.locator(`li.tile[data-entity="${tile.entityId}"]`)).toBeVisible();
  }

  watch.assertAllLocal();
});

test('presents a reading as a value with its unit, and a lock by its word', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length);

  const temperature = page.locator(`li.tile[data-entity="${HALLWAY_TEMPERATURE}"]`);
  const reading = seedTile(HALLWAY_TEMPERATURE);
  const unit = reading.unit;
  if (unit === null) {
    throw new Error(`${HALLWAY_TEMPERATURE} is the seed's unit-bearing tile`);
  }
  await expect(temperature).toHaveAttribute('data-kind', 'value');
  await expect(temperature.locator('.state .word')).toHaveText(reading.word);
  await expect(temperature.locator('.state .unit')).toHaveText(unit);

  // The tile shows the backend's *word*, not the raw state: HSD-d5x2 made the
  // backend title-case it, so `locked` renders `Locked`.
  const frontDoor = page.locator(`li.tile[data-entity="${FRONT_DOOR}"]`);
  await expect(frontDoor).toHaveAttribute('data-kind', 'ok');
  await expect(frontDoor.locator('.state .word')).toHaveText(seedTile(FRONT_DOOR).word);

  // `open` is active, not good: amber, with the word carried beside the colour.
  const garageDoor = page.locator(`li.tile[data-entity="${GARAGE_DOOR}"]`);
  await expect(garageDoor).toHaveAttribute('data-kind', 'warn');
  await expect(garageDoor.locator('.state .word')).toHaveText(seedTile(GARAGE_DOOR).word);

  watch.assertAllLocal();
});

test('an unavailable tile never shows a unit', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length);

  const backDoor = page.locator(`li.tile[data-entity="${BACK_DOOR}"]`);
  await expect(backDoor).toHaveAttribute('data-kind', 'unavailable');
  await expect(backDoor.locator('.state .word')).toHaveText('Unavailable');
  // The honesty rule: a tile with no reading must not print a unit beside the
  // word, because "Unavailable %" would read as a measurement.
  await expect(backDoor.locator('.state .unit')).toHaveCount(0);

  watch.assertAllLocal();
});

test('every tile goes stale when the socket dies', async ({ page, context }) => {
  await page.goto('/');
  await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length);

  // Chromium's `setOffline(true)` does **not** necessarily close the existing
  // websocket, so this is not the socket's `close` handler at work: the store
  // goes stale because `WsClient.#handleOffline` (`web/src/lib/ws.ts`) listens
  // for the browser's `offline` event and calls `markDisconnected()`
  // synchronously. That is one tick, so no sleep is needed between the two
  // assertions below.
  await context.setOffline(true);

  await expect(page.locator(`${TILES}[data-stale="true"]`)).toHaveCount(SEED.length);
  const stale = page.locator(`li.tile[data-entity="${FRONT_DOOR}"] .stale`);
  await expect(stale).toBeVisible();
  await expect(stale).toHaveText('Stale');

  watch.assertAllLocal();
});

test('the header reports the health of the bench it is talking to', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length);

  const header = page.locator('header .status');

  // The three component entries read the app's own health registry, which the
  // bench fills with what its fake MQTT broker and fake Home Assistant peer
  // really are: connected, connected, and a started speaker. `health-view.ts`
  // turns each status into a `data-health-kind` on the dot and a word beside
  // it, so both are asserted: the attribute is what a screenshot-free check can
  // read, the word is what an operator reads.
  const components = [
    { key: 'ha', word: 'connected' },
    { key: 'mqtt', word: 'connected' },
    { key: 'audio', word: 'ready' }
  ] as const;
  for (const { key, word } of components) {
    const component = header.locator(`.component:has([data-health-key="${key}"])`);
    await expect(component, `the header must carry a ${key} entry`).toHaveCount(1);
    await expect(component.locator('.dot')).toHaveAttribute('data-health-kind', 'ok');
    await expect(component.locator('.word')).toHaveText(word);
  }

  // The regression this test exists for: a header that says "not configured"
  // over six live tiles.
  await expect(header).not.toContainText('not configured');

  // And the connection pill reports the live socket, not a stale or absent one.
  await expect(header.locator('[data-connection-state]')).toHaveAttribute(
    'data-connection-state',
    'live'
  );

  watch.assertAllLocal();
});

test('the control rail never overlaps the tile grid', async ({ page }) => {
  const viewports = [
    { width: 1024, height: 600 },
    { width: 1280, height: 800 },
    { width: 1920, height: 1080 }
  ];

  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    await page.goto('/');
    await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length);
    await expect(page.locator('[data-testid="control-rail"]')).toHaveCount(1);

    const geometry = await page.evaluate((): RailGeometry => {
      function boxOf(element: Element): BoxRect {
        const rect = element.getBoundingClientRect();
        return {
          left: rect.left,
          right: rect.right,
          top: rect.top,
          bottom: rect.bottom,
          width: rect.width,
          height: rect.height
        };
      }

      const rail = document.querySelector('[data-testid="control-rail"]');
      if (rail === null) {
        throw new Error('the control rail is not on the dashboard');
      }
      return {
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
        rail: boxOf(rail),
        controls: Array.from(rail.querySelectorAll('.control')).map(boxOf),
        tiles: Array.from(document.querySelectorAll('ul.tiles li.tile')).map(boxOf)
      };
    });

    const detail = `viewport ${viewport.width}x${viewport.height}: ${JSON.stringify(geometry)}`;

    // The rail is fully on screen: a control that hangs off the panel cannot be
    // tapped at all.
    expect(geometry.rail.left, detail).toBeGreaterThanOrEqual(0);
    expect(geometry.rail.top, detail).toBeGreaterThanOrEqual(0);
    expect(geometry.rail.right, detail).toBeLessThanOrEqual(geometry.innerWidth);
    expect(geometry.rail.bottom, detail).toBeLessThanOrEqual(geometry.innerHeight);

    // Exactly the four D28 controls, each on the 56 px tap target.
    expect(geometry.controls, detail).toHaveLength(4);
    for (const control of geometry.controls) {
      expect(control.width, detail).toBe(56);
      expect(control.height, detail).toBe(56);
    }

    // The grid ends where the rail begins, and no tile box touches it. The
    // arithmetic is the assertion; a screenshot would only make a human check.
    expect(geometry.tiles, detail).toHaveLength(SEED.length);
    for (const tile of geometry.tiles) {
      expect(tile.right, detail).toBeLessThanOrEqual(geometry.rail.left);
      const overlaps =
        tile.right > geometry.rail.left &&
        tile.left < geometry.rail.right &&
        tile.bottom > geometry.rail.top &&
        tile.top < geometry.rail.bottom;
      expect(overlaps, detail).toBe(false);
    }
  }

  watch.assertAllLocal();
});

/**
 * Tap the rail's volume-up control and wait for that tap's own PATCH to return.
 *
 * `ControlRail` steps from the volume the **client** has adopted
 * (`stepVolume(settings.volume, 'up')`) and adopts a new value only from the
 * PATCH's echo, so a tap that lands before the previous echo has been parsed
 * re-sends the previous value — a no-op write. Polling `GET /api/settings` is
 * not that signal: the write is visible to another connection a few
 * milliseconds before the response reaches the browser, which is a window the
 * full suite lands in often enough to lose the second tap and leave the volume
 * at 75. Waiting for the response (and for the value it echoes) is waiting for
 * the client's own input to the next step, and every assertion below keeps its
 * meaning: a tap the app really dropped still fails here.
 *
 * @param page the page showing the dashboard.
 * @param expected the volume this tap must ask for and the backend echo back.
 */
async function tapVolumeUp(page: Page, expected: number): Promise<void> {
  const echo = page.waitForResponse(
    (response) =>
      response.request().method() === 'PATCH' && response.url().endsWith('/api/settings')
  );
  await page.locator('[data-testid="rail-volume-up"]').click();
  const response = await echo;
  expect(response.ok(), await response.text()).toBe(true);
  const body = (await response.json()) as SettingsBodyShape;
  expect(body.settings['audio.volume'], 'the echo must be the value this tap asked for').toBe(
    expected
  );
}

test('volume taps step exactly five points and clamp at the top', async ({ page, request }) => {
  await page.goto('/');
  await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length);

  /** `audio.volume` as the real store holds it — not what the DOM claims. */
  async function storedVolume(): Promise<number> {
    const body = await readSettings(request);
    return Number(body.settings['audio.volume']);
  }

  expect(await storedVolume()).toBe(70);

  await tapVolumeUp(page, 75);
  await expect.poll(storedVolume, { timeout: 5_000 }).toBe(75);
  await tapVolumeUp(page, 80);
  await expect.poll(storedVolume, { timeout: 5_000 }).toBe(80);

  // From 80 at most four taps reach 100; six is the bound this loop may not
  // exceed, and each step must be exactly +5 and never past the range.
  let clicks = 0;
  while (clicks < 6) {
    const before = await storedVolume();
    if (before >= 100) {
      break;
    }
    const expected = Math.min(100, before + 5);
    await tapVolumeUp(page, expected);
    clicks += 1;
    await expect.poll(storedVolume, { timeout: 5_000 }).toBe(expected);
    expect(await storedVolume()).toBeLessThanOrEqual(100);
  }
  expect(await storedVolume()).toBe(100);

  // The end sticks: one more tap is another 100, not 105.
  await tapVolumeUp(page, 100);
  await expect.poll(storedVolume, { timeout: 5_000 }).toBe(100);

  // `/__test/reset` does not restore `audio.volume`; this spec does, so the
  // next test (and the next run) starts from the registry default.
  const restore = await request.patch('/api/settings', { data: { 'audio.volume': 70 } });
  expect(restore.ok(), await restore.text()).toBe(true);

  watch.assertAllLocal();
});

test('mute from the rail flips audio.muted and the header badge without a reload', async ({
  page,
  request
}) => {
  await page.goto('/');
  await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length);

  const badge = page.locator('[data-testid="badge-muted"]');
  const mute = page.locator('[data-testid="rail-mute"]');
  await expect(badge).toHaveCount(0);
  await expect(mute).toHaveAttribute('aria-label', 'Mute');

  /** `audio.muted` as the real store holds it. */
  async function storedMuted(): Promise<boolean> {
    const body = await readSettings(request);
    return body.settings['audio.muted'] === true;
  }

  expect(await storedMuted()).toBe(false);

  await mute.click();
  // No reload: the badge is the same mounted header reacting to the echoed
  // snapshot.
  await expect(badge).toBeVisible();
  await expect(mute).toHaveAttribute('aria-label', 'Unmute');
  await expect.poll(storedMuted, { timeout: 5_000 }).toBe(true);

  await mute.click();
  await expect(badge).toHaveCount(0);
  await expect(mute).toHaveAttribute('aria-label', 'Mute');
  await expect.poll(storedMuted, { timeout: 5_000 }).toBe(false);

  const restore = await request.patch('/api/settings', { data: { 'audio.muted': false } });
  expect(restore.ok(), await restore.text()).toBe(true);

  watch.assertAllLocal();
});

test('the rail is absent on the settings shell and the setup wizard', async ({ page }) => {
  await page.goto('/settings');
  await expect(page.locator('html')).toHaveAttribute('data-layout', 'page');
  await expect(page.locator('[data-testid="control-rail"]')).toHaveCount(0);

  // `SetupApp` mounts on this path whatever the stored configuration says.
  await page.goto('/setup');
  await expect(page.locator('.setup-shell')).toBeVisible();
  await expect(page.locator('[data-testid="control-rail"]')).toHaveCount(0);

  watch.assertAllLocal();
});

// The hub heartbeats after 5 s of client silence; three missed beats (15 s)
// detect a zombie. Here `online` forces an immediate fresh socket, so the 3 s
// bound asserts real immediacy, not the heartbeat interval.
test('a returning network makes every tile live again', async ({ page, context }) => {
  await page.goto('/');
  await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length);

  await context.setOffline(true);
  await expect(page.locator(`${TILES}[data-stale="true"]`)).toHaveCount(SEED.length);

  await context.setOffline(false);

  // A short bound: this is a known failure, not a slow one.
  await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length, { timeout: 3_000 });

  watch.assertAllLocal();
});
