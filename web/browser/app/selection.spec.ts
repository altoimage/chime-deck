import { expect, test, type Page } from '@playwright/test';
import {
  FRONT_DOOR,
  SEED,
  resetBackend,
  watchOffOriginRequests,
  type OffOriginWatch
} from './fixtures.js';

/**
 * Tile selection (D29), driven through the **real** backend bench: the tap is
 * the client's optimistic write, `POST /api/tiles/{id}/selection` is the
 * server's toggle, and the `entity_state` frame is the hub's broadcast. The
 * in-memory selection survives `/__test/reset`, so every test normalises it
 * first rather than trusting the bench to start clear.
 */

/** A tile with the first real snapshot behind it (staleness cleared). */
const LIVE_TILES = 'ul.tiles li.tile[data-stale="false"]';

/** The three panel sizes this contract is verified at. */
const VIEWPORTS = [
  { width: 1024, height: 600 },
  { width: 1280, height: 720 },
  { width: 1920, height: 1080 }
] as const;

/** The off-origin watch the `beforeEach` attached; asserted at the end of each test. */
let watch: OffOriginWatch;

test.beforeEach(async ({ page, request }) => {
  await resetBackend(request);
  watch = watchOffOriginRequests(page);
});

function doorTile(page: Page) {
  return page.locator(`li.tile[data-entity="${FRONT_DOOR}"]`);
}

function doorCard(page: Page) {
  return page.locator(`li.tile[data-entity="${FRONT_DOOR}"] button.card`);
}

/**
 * Wait for the live snapshot, then leave the front door unselected.
 *
 * The selection is process-global and volatile, and `/__test/reset` does not
 * clear it, so a stale selection from an earlier test must be undone before a
 * test can assert the "off" state of a tap.
 *
 * That undo is a real toggle, so it also queues a hub broadcast that can arrive
 * up to one flush interval later. A frame landing after a later test's
 * optimistic write would overwrite it, so once the server has answered the
 * clear the page is reloaded: the reloaded page takes its state from the
 * snapshot of a **new** connection, and the hub gives every connection its own
 * queue, so a frame queued before that connection existed is never delivered to
 * it. Anything still in flight carries the value the snapshot already gave the
 * store, so it cannot change what a test sees. Both waits are conditions — the
 * POST's answer and the live snapshot — never a sleep.
 */
async function normaliseSelection(page: Page): Promise<void> {
  await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length);
  const card = doorCard(page);
  if ((await card.getAttribute('aria-pressed')) === 'true') {
    // Armed before the click: the response may arrive before the click resolves.
    const answered = page.waitForResponse(
      (response) =>
        response.url().includes('/api/tiles/') &&
        response.url().endsWith('/selection') &&
        response.request().method() === 'POST'
    );
    await card.click();
    await answered;
    await page.reload();
    await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length);
  }
  // The store now holds the server's answer, not an optimistic guess.
  await expect(doorCard(page)).toHaveAttribute('aria-pressed', 'false');
}

/** The document's scroll metrics — the reflow part of the selection contract. */
async function scrollMetrics(page: Page): Promise<{
  scrollWidth: number;
  clientWidth: number;
  scrollHeight: number;
  clientHeight: number;
}> {
  return page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    scrollHeight: document.documentElement.scrollHeight,
    clientHeight: document.documentElement.clientHeight
  }));
}

for (const viewport of VIEWPORTS) {
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    test('a tap toggles the tile and does so with no layout change', async ({ page }) => {
      await page.goto('/');
      await normaliseSelection(page);

      const tile = doorTile(page);
      const card = doorCard(page);

      const boxBefore = await tile.boundingBox();
      const scrollBefore = await scrollMetrics(page);
      if (boxBefore === null) {
        throw new Error('the front door tile must have a visible box');
      }

      await card.click();
      await expect(card).toHaveAttribute('aria-pressed', 'true');

      const cue = tile.locator('[data-testid="tile-selection-cue"]');
      await expect(cue).toBeVisible();
      await expect(cue.locator('svg')).toHaveCount(1);

      // The border is drawn with an inset shadow: computed style is where a
      // CSS engine (not a DOM shim) proves it, and polling lets the 120 ms
      // transition finish.
      await expect
        .poll(() => card.evaluate((element) => getComputedStyle(element).boxShadow), {
          timeout: 5_000
        })
        .toContain('inset');
      expect(await card.evaluate((element) => getComputedStyle(element).boxShadow)).not.toBe(
        'none'
      );

      // An inset shadow paints inside the card's border box, so selecting must
      // not move anything or open a document scrollbar.
      expect(await tile.boundingBox()).toEqual(boxBefore);
      expect(await scrollMetrics(page)).toEqual(scrollBefore);

      await card.click();
      await expect(card).toHaveAttribute('aria-pressed', 'false');
      await expect(cue).toHaveCount(0);

      watch.assertAllLocal();
    });

    test('the state word survives a selection change', async ({ page }) => {
      await page.goto('/');
      await normaliseSelection(page);

      const tile = doorTile(page);
      const card = doorCard(page);
      const word = tile.locator('.state .word');
      const stateWord = await word.textContent();
      if (stateWord === null || stateWord === '') {
        throw new Error('the front door tile must render its state word');
      }

      await card.click();
      await expect(card).toHaveAttribute('aria-pressed', 'true');

      // The hub also broadcasts a selection-only `{entity_id, selected}` frame.
      // Give that frame a bounded moment to land before asserting the word was
      // not blanked by the absent `state` key.
      await page.waitForTimeout(500);
      await expect(word).toHaveText(stateWord);
      await expect(tile).toHaveAttribute('data-kind', 'ok');

      watch.assertAllLocal();
    });

    test('a failed toggle reverts and surfaces an error', async ({ page }) => {
      await page.goto('/');
      await normaliseSelection(page);

      await page.route('**/api/tiles/*/selection', async (route) => {
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({
            error: {
              code: 'store_unavailable',
              message: 'the selection could not be saved',
              detail: null
            }
          })
        });
      });

      const card = doorCard(page);
      await card.click();

      // The optimistic paint is reverted because the server refused.
      await expect(card).toHaveAttribute('aria-pressed', 'false');
      const toast = page.locator('.toast.error');
      await expect(toast).toBeVisible();
      await expect(toast.locator('.message')).toHaveText('the selection could not be saved');

      watch.assertAllLocal();
    });

    test('a long-press opens the popover without toggling, and Shift+Enter opens it by keyboard', async ({
      page
    }) => {
      await page.goto('/');
      await normaliseSelection(page);

      const card = doorCard(page);
      const pressedBefore = await card.getAttribute('aria-pressed');
      const box = await card.boundingBox();
      if (box === null) {
        throw new Error('the front door control must have a visible box');
      }

      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      // The long-press threshold is 400 ms; 500 is bounded and clears it.
      await page.waitForTimeout(500);
      await page.mouse.up();

      const popover = page.locator('[role="dialog"]');
      await expect(popover).toBeVisible();
      await expect(popover).toContainText(FRONT_DOOR);
      await expect(card).toHaveAttribute('aria-pressed', pressedBefore ?? 'false');

      await page.keyboard.press('Escape');
      await expect(popover).toHaveCount(0);

      // The keyboard path: Shift+Enter keeps the popover reachable while plain
      // Enter/Space activate the native toggle.
      await card.focus();
      await page.keyboard.press('Shift+Enter');
      await expect(page.locator('[role="dialog"]')).toBeVisible();
      await expect(card).toHaveAttribute('aria-pressed', pressedBefore ?? 'false');

      await page.keyboard.press('Escape');
      await expect(page.locator('[role="dialog"]')).toHaveCount(0);

      watch.assertAllLocal();
    });

    test('the selection survives a reload', async ({ page }) => {
      await page.goto('/');
      await normaliseSelection(page);

      const card = doorCard(page);
      const answered = page.waitForResponse(
        (response) =>
          response.url().includes('/api/tiles/') &&
          response.url().endsWith('/selection') &&
          response.request().method() === 'POST'
      );
      await card.click();
      await answered;
      await expect(card).toHaveAttribute('aria-pressed', 'true');

      await page.reload();
      await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length);

      // Not local state: the value came back in the server's snapshot.
      await expect(doorCard(page)).toHaveAttribute('aria-pressed', 'true');

      watch.assertAllLocal();
    });
  });
}
