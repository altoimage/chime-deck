import { expect, test } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import {
  ARTIFACTS_DIR,
  SEED,
  pngSize,
  resetBackend,
  watchOffOriginRequests,
  type OffOriginWatch
} from './fixtures.js';

/**
 * Screenshot evidence at the three panel sizes, taken from the **real** backend
 * so the images show the shipped data rather than a fake's stand-in.
 *
 * The size assertion is the point of the test: `pngSize()` reads the file's own
 * IHDR chunk, so a screenshot clipped to the wrong region, taken with the wrong
 * device-pixel ratio, or silently scaled would fail here even though the browser
 * reported the viewport it was asked for.
 *
 * The PNGs land in `web/browser/artifacts/` (git-ignored — run output, not an
 * input; the suite regenerates them).
 */

/** The wall-panel viewports this tier verifies (the 1024×600 panel plus two desks). */
const VIEWPORTS = [
  { width: 1024, height: 600 },
  { width: 1280, height: 800 },
  { width: 1920, height: 1080 }
] as const;

/** Every seeded tile, live. */
const LIVE_TILES = 'ul.tiles li.tile[data-stale="false"]';

/** The off-origin watch the `beforeEach` attached; asserted at the end of each test. */
let watch: OffOriginWatch;

test.beforeEach(async ({ page, request }) => {
  await resetBackend(request);
  watch = watchOffOriginRequests(page);
});

for (const viewport of VIEWPORTS) {
  const name = `dashboard-${viewport.width}x${viewport.height}.png`;

  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    test(`captures ${name} at the exact viewport size`, async ({ page }) => {
      mkdirSync(ARTIFACTS_DIR, { recursive: true });
      const file = join(ARTIFACTS_DIR, name);

      await page.goto('/');
      await expect(page.locator('ul.tiles li.tile')).toHaveCount(SEED.length);
      await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length);

      await page.screenshot({ path: file });

      expect(pngSize(file)).toEqual({ width: viewport.width, height: viewport.height });
      watch.assertAllLocal();
    });
  });
}
