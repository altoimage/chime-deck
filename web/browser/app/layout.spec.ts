import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import {
  SEED,
  resetBackend,
  setTileCount,
  watchOffOriginRequests,
  type OffOriginWatch
} from './fixtures.js';

/**
 * The dashboard's fill contract (HSD-n3nr), measured on the **real** backend
 * bench: a real store, the real `/api/entities` REST reads and the real `/ws`
 * snapshot, with the shipped CSS from `web/dist`.
 *
 * The owner's complaint was that two or three tiles render short at the top of
 * a 1280×720 panel. A screenshot would show that; it would not measure it, and
 * it could not say "94% of the content box" or "scrollHeight equals
 * clientHeight". So every assertion here is a bounding box, a scroll metric or a
 * computed font size, and there is not one image comparison in the file.
 *
 * This file covers HSD-enxf as well: the tiles must use the **horizontal** space
 * too, and the icon, state text and label must scale with the *tile's* own box
 * rather than with the viewport. In those later tests the "content box" is
 * `.content`'s **border box** — the element itself, padding included — because
 * that is the space the owner sees as the panel; `geometry.content` is the
 * padding-inset box the tiles may actually use, and the two are measured and
 * asserted separately on purpose.
 *
 * Three things are deliberately *not* asserted, because they belong elsewhere:
 * the ≤639px single-column rule (`scripts/check-dist.mjs` greps the built CSS),
 * the control rail's non-overlap (`dashboard.spec.ts`) and the tap behaviour
 * (`selection.spec.ts`).
 */

/** Every tile on the page, whatever its staleness. */
const TILES = 'ul.tiles li.tile';

/** A tile with the first real snapshot behind it (staleness cleared). */
const LIVE_TILES = 'ul.tiles li.tile[data-stale="false"]';

/**
 * The panel sizes the ticket names: the 1024×600 wall panel up to 1440p.
 *
 * Deliberately kept apart from `MATRIX` below. These are the four **landscape**
 * sizes the earlier fill tests were written against, and those tests (nine tiles
 * fit, the rail is never reached, no safety scroller) are tuned to exactly these
 * four. Folding portrait or ultrawide into this list would change what they
 * assert, so the two lists stay separate on purpose.
 */
const VIEWPORTS = [
  { width: 1024, height: 600 },
  { width: 1280, height: 720 },
  { width: 1920, height: 1080 },
  { width: 2560, height: 1440 }
] as const;

/**
 * The amendment's spread: every panel shape the layout has to look deliberate
 * on, not just the 1280×720 the owner reported. Portrait and ultrawide are here
 * because "resolution- and aspect-ratio-agnostic" is the claim being made; 4K is
 * here because an uncapped container-relative internal has to keep growing
 * there, and a px ceiling used to make it stop.
 *
 * A separate constant from `VIEWPORTS` on purpose — see the comment above. Do
 * not "unify" the two: folding portrait, ultrawide and 4K into `VIEWPORTS`
 * would change what the earlier fill tests assert, and dropping them silently
 * would lose the coverage this amendment added.
 */
const MATRIX = [
  { width: 1024, height: 600 }, // the wall panel
  { width: 1280, height: 720 }, // the reference panel
  { width: 1920, height: 1080 },
  { width: 2560, height: 1440 },
  { width: 720, height: 1280 }, // portrait
  { width: 2560, height: 720 }, // ultrawide
  { width: 3840, height: 2160 } // 4K
] as const;

/** The 1024×600 panel: the smallest supported one, and the ticket's 9-tile case. */
const SMALLEST = VIEWPORTS[0];

/** The reference panel's kiosk viewport (1280×720): the size the owner reported. */
const PANEL = { width: 1280, height: 720 } as const;

/** `--tap` from `tokens.css`: no tile may be narrower than this (D26). */
const MIN_TILE_WIDTH = 48;

/** `--tap-primary` from `tokens.css`: no tile may be shorter than this (D26). */
const MIN_TILE_HEIGHT = 56;

/** Design 05 §2.1's track floor: a forced column count never goes below it. */
const TRACK_FLOOR = 240;

/** The off-origin watch the `beforeEach` attached; asserted at the end of each test. */
let watch: OffOriginWatch;

test.beforeEach(async ({ page, request }) => {
  await resetBackend(request);
  watch = watchOffOriginRequests(page);
});

test.afterEach(async ({ request }) => {
  // The bench is one process for a whole run, and `/__test/reset` never deletes
  // a row a spec added: a synthetic tile left here would hand the *next* spec
  // file seven or nine tiles instead of the seed's six (`selection.spec.ts`
  // asserts exactly six).
  //
  // `resetBackend` comes first because `setTileCount` alone cannot undo a trim:
  // it deletes rows and can only add `sensor.layout_extra_*` ones back, so the
  // canonical rows this spec dropped would stay dropped. The reset re-upserts
  // all six; the prune that follows then removes every synthetic row.
  await resetBackend(request);
  await setTileCount(request, SEED.length);
  // The last test forces a column count and restores it itself; this is the
  // safety net for a run where it failed before reaching that restore, because
  // `/__test/reset` does not restore settings at all.
  await restoreColumns(request);
});

/** One `getBoundingClientRect()` reading, narrowed to what a spec compares. */
interface BoxRect {
  readonly left: number;
  readonly right: number;
  readonly top: number;
  readonly bottom: number;
  readonly width: number;
  readonly height: number;
}

/** The fill geometry collected in one page evaluation. */
interface FillGeometry {
  /** `.content`'s **content box** — the padding is not part of the tiles' area. */
  readonly content: BoxRect;
  /** `.content`'s own **border box**: the element, padding included. */
  readonly contentBorder: BoxRect;
  /** The control rail (D28), or `null` on a page that has none. */
  readonly rail: BoxRect | null;
  readonly tiles: readonly BoxRect[];
  readonly scrollHeight: number;
  readonly clientHeight: number;
  readonly scrollTop: number;
}

/** One tile's typography and icon, as the browser resolves them. */
interface TileInternals {
  readonly word: number;
  readonly name: number;
  readonly iconWidth: number;
  readonly iconHeight: number;
}

/**
 * Read the fill geometry once.
 *
 * `content` is `.content`'s `getBoundingClientRect()` inset by its own computed
 * padding: `.content`'s padding is space the tiles may not use anyway, so
 * counting it would report a padding-only "empty region" and make the coverage
 * ratio depend on a `clamp()` rather than on the grid.
 *
 * `contentBorder` is that same rect **un-inset**. Both exist because they answer
 * two different questions, and neither can stand in for the other: HSD-enxf's
 * "content box" is the element itself (a tile may not cover the padding, and the
 * owner reads the padding as part of the panel), while the fill contract
 * (HSD-n3nr) is about the area the tiles are allowed to use. A max-width
 * centring column, for instance, shows up in `contentBorder` as a width share
 * far below 100% while the inset ratio still looks healthy.
 *
 * @param page the page showing the dashboard.
 */
async function measureFill(page: Page): Promise<FillGeometry> {
  return page.evaluate((): FillGeometry => {
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

    const content = document.querySelector('.content');
    if (content === null) {
      throw new Error('the dashboard has no .content area');
    }
    const rect = content.getBoundingClientRect();
    const style = getComputedStyle(content);
    const left = rect.left + parseFloat(style.paddingLeft);
    const right = rect.right - parseFloat(style.paddingRight);
    const top = rect.top + parseFloat(style.paddingTop);
    const bottom = rect.bottom - parseFloat(style.paddingBottom);

    const rail = document.querySelector('[data-testid="control-rail"]');

    return {
      content: { left, right, top, bottom, width: right - left, height: bottom - top },
      contentBorder: boxOf(content),
      rail: rail === null ? null : boxOf(rail),
      tiles: Array.from(document.querySelectorAll('ul.tiles li.tile')).map(boxOf),
      scrollHeight: content.scrollHeight,
      clientHeight: content.clientHeight,
      scrollTop: content.scrollTop
    };
  });
}

/**
 * Read the first tile's resolved word size, name size and icon box.
 *
 * `getComputedStyle` and a bounding box, not a class name: the tile's type is a
 * `cqi` container query, so only the browser can say what it resolves to.
 *
 * @param page the page showing the dashboard.
 */
async function measureInternals(page: Page): Promise<TileInternals> {
  return page.evaluate((): TileInternals => {
    const tile = document.querySelector('ul.tiles li.tile');
    if (tile === null) {
      throw new Error('the dashboard has no tile to measure');
    }
    const word = tile.querySelector('.state .word');
    const name = tile.querySelector('.name');
    const icon = tile.querySelector('.icon svg');
    if (word === null || name === null || icon === null) {
      throw new Error('the first tile must have a word, a name and an icon');
    }
    const iconRect = icon.getBoundingClientRect();
    return {
      word: parseFloat(getComputedStyle(word).fontSize),
      name: parseFloat(getComputedStyle(name).fontSize),
      iconWidth: iconRect.width,
      iconHeight: iconRect.height
    };
  });
}

/**
 * The rect enclosing every tile. The count assertion has already proved there
 * is at least one, so the `Math.min`/`Math.max` over an empty list cannot happen.
 */
function unionOf(tiles: readonly BoxRect[]): BoxRect {
  const left = Math.min(...tiles.map((tile) => tile.left));
  const right = Math.max(...tiles.map((tile) => tile.right));
  const top = Math.min(...tiles.map((tile) => tile.top));
  const bottom = Math.max(...tiles.map((tile) => tile.bottom));
  return { left, right, top, bottom, width: right - left, height: bottom - top };
}

/** How much of the content box the tile union covers, as a ratio. */
function coverageOf(geometry: FillGeometry): number {
  const union = unionOf(geometry.tiles);
  return (union.width * union.height) / (geometry.content.width * geometry.content.height);
}

/**
 * The tile union's share of `.content`'s **border box**, per axis and by area.
 *
 * The sibling of `coverageOf`, against the element instead of the padding-inset
 * box: `coverageOf` answers "did the grid use the space it was given", this one
 * answers "did the tiles use the panel". A horizontal deficit that the padding
 * hides — a capped, centred column — only shows up here.
 *
 * @param geometry the measured fill geometry.
 */
function borderCoverageOf(geometry: FillGeometry): {
  readonly width: number;
  readonly height: number;
  readonly area: number;
} {
  const union = unionOf(geometry.tiles);
  const border = geometry.contentBorder;
  return {
    width: union.width / border.width,
    height: union.height / border.height,
    area: (union.width * union.height) / (border.width * border.height)
  };
}

/**
 * HSD-enxf's fill contract: the tiles cover at least 90% of the content box, in
 * **both axes** — the ticket's own criterion, and the one that names the defect.
 *
 * Two axes and not the area, deliberately. The area ratio is smaller by
 * construction, because `.content`'s padding is inside its border box and
 * outside the tiles' reach: at 1280×720 the tiles cover 92.1% of the width and
 * 94.7% of the height, an area share of 87.2%. Asserting the area would
 * therefore not be a stricter version of the same test — it would be a test of
 * the padding `clamp()`, and it would push a later ticket to shrink the
 * deliberate panel inset just to move a number. The axis shares are what the
 * owner's complaint was about: an empty band down each side, and one at the top.
 *
 * @param geometry the measured fill geometry.
 * @param detail the failure message, carrying the whole measurement.
 */
function expectTilesCoverContentBox(geometry: FillGeometry, detail: string): void {
  const share = borderCoverageOf(geometry);
  /** A ratio as a percentage, for the failure message. */
  function pct(ratio: number): string {
    return `${(ratio * 100).toFixed(1)}%`;
  }
  const context = `width ${pct(share.width)}, height ${pct(share.height)}, area ${pct(share.area)} — ${detail}`;
  expect(share.width, `the tiles do not use the width: ${context}`).toBeGreaterThanOrEqual(0.9);
  expect(share.height, `the tiles do not use the height: ${context}`).toBeGreaterThanOrEqual(0.9);
}

/** The whole geometry, so a failure names the panel and the numbers. */
function detailOf(label: string, geometry: FillGeometry): string {
  return `${label}: coverage ${coverageOf(geometry).toFixed(4)}, ${JSON.stringify(geometry)}`;
}

/**
 * The tiles grouped into rows by their rounded `top`, left to right.
 *
 * @param tiles the measured tile boxes.
 */
function rowsOf(tiles: readonly BoxRect[]): BoxRect[][] {
  const rows = new Map<number, BoxRect[]>();
  for (const tile of tiles) {
    const key = Math.round(tile.top);
    const row = rows.get(key);
    if (row === undefined) {
      rows.set(key, [tile]);
    } else {
      row.push(tile);
    }
  }
  return [...rows.entries()].sort(([a], [b]) => a - b).map(([, row]) => row);
}

/**
 * The shared "no large empty region and no overflow" assertions.
 *
 * @param geometry the measured fill geometry.
 * @param detail the failure message, carrying the whole measurement.
 */
function expectTilesFillContent(geometry: FillGeometry, detail: string): void {
  expect(coverageOf(geometry), detail).toBeGreaterThan(0.9);

  // Every row spans the content width. The coverage ratio above is a union of
  // all tiles, so it cannot see a hole *inside* that union: 4 tiles in a 3×2
  // grid covered 89% of a 1280×720 content box while two-thirds of the last row
  // was empty, and a 3+1 arrangement is exactly what this catches. 90% leaves
  // room for a rounding half-pixel, not for a missing column.
  for (const row of rowsOf(geometry.tiles)) {
    const left = Math.min(...row.map((tile) => tile.left));
    const right = Math.max(...row.map((tile) => tile.right));
    expect(right - left, `a row of ${row.length} tiles is not full — ${detail}`).toBeGreaterThanOrEqual(
      geometry.content.width * 0.9
    );
  }

  // A tile that leaves the content box is clipped by `.content`'s
  // `overflow-x: hidden`, or starts a horizontal scroll: either way it is a
  // layout bug the 90% ratio alone would not separate from a padding effect.
  for (const tile of geometry.tiles) {
    expect(tile.left, detail).toBeGreaterThanOrEqual(geometry.content.left - 1);
    expect(tile.top, detail).toBeGreaterThanOrEqual(geometry.content.top - 1);
    expect(tile.right, detail).toBeLessThanOrEqual(geometry.content.right + 1);
    expect(tile.bottom, detail).toBeLessThanOrEqual(geometry.content.bottom + 1);
  }
}

/**
 * Put `ui.dashboard_columns` back to auto-fit (0), the way
 * `dashboard.spec.ts` puts `audio.volume` back: `/__test/reset` restores
 * neither — the bench's `CANONICAL_SETTINGS` carries no `ui.dashboard_columns`.
 *
 * @param request the test's API request context.
 */
async function restoreColumns(request: APIRequestContext): Promise<void> {
  const restore = await request.patch('/api/settings', { data: { 'ui.dashboard_columns': 0 } });
  expect(restore.ok(), await restore.text()).toBe(true);
}

/**
 * Point the bench at *count* tiles, mount the dashboard at *viewport* and wait
 * for the live snapshot, which is what gives the tiles their real content and
 * therefore their real height.
 *
 * @param page the page to drive.
 * @param request the test's API request context.
 * @param count how many tiles the dashboard must show.
 * @param viewport the panel size to measure at.
 */
async function openWithTiles(
  page: Page,
  request: APIRequestContext,
  count: number,
  viewport: { readonly width: number; readonly height: number }
): Promise<void> {
  await setTileCount(request, count);
  await page.setViewportSize({ width: viewport.width, height: viewport.height });
  await page.goto('/');
  const detail = `${count} tiles at ${viewport.width}x${viewport.height}`;
  await expect(page.locator(TILES), detail).toHaveCount(count);
  await expect(page.locator(LIVE_TILES)).toHaveCount(count);
}

test('the tiles fill the content box at 1, 2 and 4 tiles', async ({ page, request }) => {
  for (const viewport of VIEWPORTS) {
    for (const count of [1, 2, 4] as const) {
      await openWithTiles(page, request, count, viewport);

      const geometry = await measureFill(page);
      const detail = detailOf(`${count} tiles at ${viewport.width}x${viewport.height}`, geometry);

      expectTilesFillContent(geometry, detail);

      // The two touch floors (D26): a tile stretched to fill a panel is still a
      // target, so it may never be narrower than `--tap` or shorter than
      // `--tap-primary`.
      for (const tile of geometry.tiles) {
        expect(tile.width, detail).toBeGreaterThanOrEqual(MIN_TILE_WIDTH);
        expect(tile.height, detail).toBeGreaterThanOrEqual(MIN_TILE_HEIGHT);
      }

      // The safety valve exists for a pathological count, never for these: a
      // scrolled content area would mean some rows are off-screen.
      expect(geometry.scrollHeight, detail).toBeLessThanOrEqual(geometry.clientHeight + 1);
      expect(geometry.scrollTop, detail).toBe(0);
    }
  }

  watch.assertAllLocal();
});

test('nine tiles fit the smallest panel without the safety scroller', async ({ page, request }) => {
  await openWithTiles(page, request, 9, SMALLEST);

  const geometry = await measureFill(page);
  const detail = detailOf(`9 tiles at ${SMALLEST.width}x${SMALLEST.height}`, geometry);

  // The ticket's explicit case. `.content` has a hidden `overflow-y: auto`
  // scroller as a fallback; at 9 tiles on the smallest panel it must not scroll,
  // or the last row is off-screen on the reference panel.
  expect(geometry.scrollHeight, detail).toBeLessThanOrEqual(geometry.clientHeight + 1);
  expect(geometry.scrollTop, detail).toBe(0);
  expectTilesFillContent(geometry, detail);

  for (const tile of geometry.tiles) {
    expect(tile.height, detail).toBeGreaterThanOrEqual(MIN_TILE_HEIGHT);
  }

  watch.assertAllLocal();
});

test('nine tiles fill every supported resolution', async ({ page, request }) => {
  // The smallest panel is the previous test's case; these are the other three.
  for (const viewport of VIEWPORTS.slice(1)) {
    await openWithTiles(page, request, 9, viewport);

    const geometry = await measureFill(page);
    const detail = detailOf(`9 tiles at ${viewport.width}x${viewport.height}`, geometry);

    expectTilesFillContent(geometry, detail);
    for (const tile of geometry.tiles) {
      expect(tile.height, detail).toBeGreaterThanOrEqual(MIN_TILE_HEIGHT);
    }
    expect(geometry.scrollHeight, detail).toBeLessThanOrEqual(geometry.clientHeight + 1);
    expect(geometry.scrollTop, detail).toBe(0);
  }

  watch.assertAllLocal();
});

test('two tiles cover the content box in both axes at the panel resolution', async ({
  page,
  request
}) => {
  // HSD-enxf's headline defect: HSD-n3nr gave the tiles their vertical space and
  // left the horizontal one to a `min(100%, 1120px)` centring column, so two
  // tiles on the owner's own 1280×720 panel were tall and narrow with an empty
  // margin down each side. Measured against `.content`'s border box, because
  // that is the space the owner sees; the inset box would hide the margin inside
  // the padding. An axis-wise ratio catches it, which an area-only ratio can miss
  // on a wide-and-short union.
  await openWithTiles(page, request, 2, PANEL);

  const geometry = await measureFill(page);
  const detail = detailOf(`2 tiles at ${PANEL.width}x${PANEL.height}`, geometry);

  expectTilesCoverContentBox(geometry, detail);

  watch.assertAllLocal();
});

test('one tile covers the content box in both axes at the panel resolution', async ({
  page,
  request
}) => {
  // The dramatic end of the same contract: a lone tile must own the panel, not
  // sit in the middle of it at its two-tile size. A grid that still resolves to
  // two tracks with one tile in the first reports a width share near 50% here.
  await openWithTiles(page, request, 1, PANEL);

  const geometry = await measureFill(page);
  const detail = detailOf(`1 tile at ${PANEL.width}x${PANEL.height}`, geometry);

  expectTilesCoverContentBox(geometry, detail);

  watch.assertAllLocal();
});

test('nine tiles fit the smallest panel without overflow or a safety scroller', async ({
  page,
  request
}) => {
  // The horizontal fix must not cost the smallest panel a row. Nine tiles at
  // 1024×600 is the tightest supported case, and the two failures this catches
  // are a grid that grows past `.content` — the hidden scroller then puts the
  // last row off-screen — and a tile wide enough to run under the D28 rail, which
  // would put a control surface on top of a tap target.
  await openWithTiles(page, request, 9, SMALLEST);

  const geometry = await measureFill(page);
  const detail = detailOf(`9 tiles at ${SMALLEST.width}x${SMALLEST.height}`, geometry);
  const rail = geometry.rail;
  if (rail === null) {
    throw new Error(`the dashboard has no control rail — ${detail}`);
  }

  for (const tile of geometry.tiles) {
    // Inside the content box on all four edges, to a rounding pixel.
    expect(tile.left, detail).toBeGreaterThanOrEqual(geometry.contentBorder.left - 1);
    expect(tile.top, detail).toBeGreaterThanOrEqual(geometry.contentBorder.top - 1);
    expect(tile.right, detail).toBeLessThanOrEqual(geometry.contentBorder.right + 1);
    expect(tile.bottom, detail).toBeLessThanOrEqual(geometry.contentBorder.bottom + 1);

    // The two touch floors (D26) survive the shrink to nine tiles.
    expect(tile.width, detail).toBeGreaterThanOrEqual(MIN_TILE_WIDTH);
    expect(tile.height, detail).toBeGreaterThanOrEqual(MIN_TILE_HEIGHT);

    // D28: the rail is the dashboard's only control surface, so nothing is
    // painted under it. A right edge that merely reaches `rail.left` is already
    // the boundary case, so the comparison is not relaxed by a pixel.
    expect(tile.right, `a tile reaches the control rail — ${detail}`).toBeLessThanOrEqual(
      rail.left
    );
  }

  expect(geometry.scrollHeight, detail).toBeLessThanOrEqual(geometry.clientHeight + 1);
  expect(geometry.scrollTop, detail).toBe(0);

  watch.assertAllLocal();
});

test('tile internals scale with the tile, not the viewport', async ({ page, request }) => {
  const viewport = { width: 1280, height: 720 };

  // Two tiles share the width, nine split it three ways: the same viewport, a
  // different tile. Fixed pixel sizing would report identical font sizes for
  // both, which is exactly the defect this test exists for.
  await openWithTiles(page, request, 2, viewport);
  const wide = await measureInternals(page);

  await openWithTiles(page, request, 9, viewport);
  const narrow = await measureInternals(page);

  const detail = `1280x720, 2 tiles ${JSON.stringify(wide)}, 9 tiles ${JSON.stringify(narrow)}`;

  // Every internal shrinks, and by at least 15%: a token that merely follows
  // the viewport would shrink by nothing at all.
  for (const key of ['word', 'name', 'iconWidth', 'iconHeight'] as const) {
    expect(narrow[key], `${key} must shrink: ${detail}`).toBeLessThan(wide[key]);
    expect(
      wide[key],
      `${key} must be at least 15% larger at 2 tiles: ${detail}`
    ).toBeGreaterThanOrEqual(narrow[key] * 1.15);
  }

  // And the same tile count on a larger panel is still strictly larger. The
  // margin is a couple of px: since HSD-enxf the shell is uncapped, so the wider
  // panel really does widen each of the two tiles, and the tile's own `clamp()`
  // ceilings still leave room above what two tiles reach at either size — so the
  // tolerance is half a pixel, not a whole one.
  await openWithTiles(page, request, 2, SMALLEST);
  const small = await measureInternals(page);

  await openWithTiles(page, request, 2, { width: 1920, height: 1080 });
  const large = await measureInternals(page);

  const panelDetail =
    `2 tiles, 1024x600 ${JSON.stringify(small)}, ` +
    `1920x1080 ${JSON.stringify(large)}`;
  for (const key of ['word', 'name', 'iconWidth', 'iconHeight'] as const) {
    expect(large[key], `${key} must be larger on the larger panel: ${panelDetail}`).toBeGreaterThan(
      small[key] + 0.5
    );
  }

  watch.assertAllLocal();
});

test('the icon and the state text are larger in a one-tile layout than in a nine-tile layout', async ({
  page,
  request
}) => {
  // HSD-enxf's second half. The internals used to be sized from the viewport, so
  // a one-tile and a nine-tile layout on the *same* panel rendered the same icon
  // and the same state text: the tile grew and its contents did not. Only the
  // tile's own box differs between these two runs, so only a tile-local unit can
  // make these four numbers differ at all.
  await openWithTiles(page, request, 1, PANEL);
  const lone = await measureInternals(page);

  await openWithTiles(page, request, 9, PANEL);
  const crowded = await measureInternals(page);

  const detail = `1280x720, 1 tile ${JSON.stringify(lone)}, 9 tiles ${JSON.stringify(crowded)}`;

  // The icon is drawn into a square box. If one axis were clamped by the tile's
  // width and the other by its height, "larger" could be one axis growing while
  // the other shrank — that is not scaling with the tile, it is a distortion.
  expect(
    Math.abs(lone.iconWidth - lone.iconHeight),
    `the icon box must be square at 1 tile: ${detail}`
  ).toBeLessThanOrEqual(1);
  expect(
    Math.abs(crowded.iconWidth - crowded.iconHeight),
    `the icon box must be square at 9 tiles: ${detail}`
  ).toBeLessThanOrEqual(1);

  // Every internal grows with the tile, and by at least 15%: a token that merely
  // follows the viewport would report identical numbers for both.
  for (const key of ['word', 'name', 'iconWidth', 'iconHeight'] as const) {
    expect(crowded[key], `${key} must shrink with the tile: ${detail}`).toBeLessThan(lone[key]);
    expect(
      lone[key],
      `${key} must be at least 15% larger at 1 tile: ${detail}`
    ).toBeGreaterThanOrEqual(crowded[key] * 1.15);
  }

  watch.assertAllLocal();
});

test('a forced column count never produces a sliver', async ({ page, request }) => {
  /** How many distinct columns the tiles occupy. */
  function columnCount(geometry: FillGeometry): number {
    const lefts = new Set(geometry.tiles.map((tile) => Math.round(tile.left)));
    return lefts.size;
  }

  // Six columns asked for, two tiles to give: the 240px track floor wins, so
  // the tiles stay wide instead of becoming six slivers.
  const forced = await request.patch('/api/settings', { data: { 'ui.dashboard_columns': 6 } });
  expect(forced.ok(), await forced.text()).toBe(true);

  await openWithTiles(page, request, 2, SMALLEST);

  const sliverGeometry = await measureFill(page);
  const sliverDetail = detailOf(
    `2 tiles under 6 forced columns at ${SMALLEST.width}x${SMALLEST.height}`,
    sliverGeometry
  );
  expectTilesFillContent(sliverGeometry, sliverDetail);
  expect(sliverGeometry.tiles.length, sliverDetail).toBe(2);
  for (const tile of sliverGeometry.tiles) {
    expect(tile.width, sliverDetail).toBeGreaterThanOrEqual(TRACK_FLOOR);
  }
  // Same row: the two tiles differ only in their left edge.
  const rowOffset = Math.abs(sliverGeometry.tiles[0].top - sliverGeometry.tiles[1].top);
  expect(rowOffset, sliverDetail).toBeLessThanOrEqual(1);

  // The regression the gutter correction exists for: `auto-fit` counts the
  // gutters, so a track minimum of `100% / N` fits only N-1 tracks and a forced
  // 2 or 3 lost a column at every width. Each track now asks for
  // `(100% - (N-1)·gap) / N`, which is exactly N tracks.
  for (const columns of [2, 3] as const) {
    const patch = await request.patch('/api/settings', {
      data: { 'ui.dashboard_columns': columns }
    });
    expect(patch.ok(), await patch.text()).toBe(true);

    for (const viewport of [SMALLEST, VIEWPORTS[2], VIEWPORTS[3]]) {
      await openWithTiles(page, request, columns, viewport);

      const geometry = await measureFill(page);
      const detail = detailOf(
        `${columns} tiles under ${columns} forced columns at ${viewport.width}x${viewport.height}`,
        geometry
      );
      expect(columnCount(geometry), detail).toBe(columns);
      expect(geometry.tiles.length, detail).toBe(columns);
    }
  }

  // `/__test/reset` does not restore settings — the bench's `CANONICAL_SETTINGS`
  // has no `ui.dashboard_columns` — so the next test (and the next run) would
  // inherit the forced count unless this spec puts it back.
  await restoreColumns(request);

  watch.assertAllLocal();
});

test('two tiles cover the content box at every panel shape', async ({ page, request }) => {
  // The amendment's spread, not just the owner's 1280×720: the same contract is
  // measured on portrait, ultrawide and 4K, which is where a shape-dependent
  // layout (an aspect-ratio box, a fixed max-width column) gives itself away.
  for (const viewport of MATRIX) {
    await openWithTiles(page, request, 2, viewport);

    const geometry = await measureFill(page);
    const detail = detailOf(`2 tiles at ${viewport.width}x${viewport.height}`, geometry);

    expectTilesCoverContentBox(geometry, detail);

    // One line per panel shape, from the same measurement the assertion used, so
    // a run's own output is the per-viewport table: the ratios against the
    // content box's border box, plus the tile the grid actually produced.
    const share = borderCoverageOf(geometry);
    const tile = geometry.tiles[0];
    console.log(
      `2 tiles ${viewport.width}x${viewport.height}: ` +
        `width ${(share.width * 100).toFixed(1)}% ` +
        `height ${(share.height * 100).toFixed(1)}% of the content box ` +
        `(tile ${tile.width.toFixed(0)}x${tile.height.toFixed(0)})`
    );
  }

  watch.assertAllLocal();
});

test('one tile covers the content box at every panel shape', async ({ page, request }) => {
  // The dramatic end of the same contract at every shape: a lone tile owns the
  // panel. In portrait this is the case a landscape-only grid gets wrong, and on
  // the ultrawide it is the one a fixed two-track grid gets wrong.
  for (const viewport of MATRIX) {
    await openWithTiles(page, request, 1, viewport);

    const geometry = await measureFill(page);
    const detail = detailOf(`1 tile at ${viewport.width}x${viewport.height}`, geometry);

    expectTilesCoverContentBox(geometry, detail);

    // The same one-line-per-viewport report as the two-tile test, so the two
    // tables from a single run can be read side by side.
    const share = borderCoverageOf(geometry);
    const tile = geometry.tiles[0];
    console.log(
      `1 tile ${viewport.width}x${viewport.height}: ` +
        `width ${(share.width * 100).toFixed(1)}% ` +
        `height ${(share.height * 100).toFixed(1)}% of the content box ` +
        `(tile ${tile.width.toFixed(0)}x${tile.height.toFixed(0)})`
    );
  }

  watch.assertAllLocal();
});

test('the tile internals are strictly larger on a larger panel', async ({ page, request }) => {
  // Why this test exists: with the old px clamp ceilings the 1920×1080 and
  // 2560×1440 numbers were bit-identical — word 120.0, name 44.0, icon 320 —
  // because the ceiling, not the tile, decided the size. Two panels 1.8× apart in
  // area rendering the same tile is the fixed-resolution lock this amendment
  // forbids, and "larger" here means strictly larger, not "as large as allowed".
  const ladder = [
    { width: 1920, height: 1080 },
    { width: 2560, height: 1440 },
    { width: 3840, height: 2160 }
  ] as const;

  const series: TileInternals[] = [];
  for (const viewport of ladder) {
    await openWithTiles(page, request, 2, viewport);
    series.push(await measureInternals(page));
  }

  const detail = ladder
    .map(
      (viewport, index) =>
        `2 tiles at ${viewport.width}x${viewport.height} ${JSON.stringify(series[index])}`
    )
    .join(', ');

  // Half a pixel, not a whole one: the point is that the curve has not gone flat,
  // and a ceiling that has started to bind shows up as a sub-pixel step first.
  for (const key of ['word', 'name', 'iconWidth', 'iconHeight'] as const) {
    for (let index = 1; index < series.length; index += 1) {
      const previous = ladder[index - 1];
      const current = ladder[index];
      expect(
        series[index][key],
        `${key} must grow by more than 0.5px from ${previous.width}x${previous.height} ` +
          `to ${current.width}x${current.height}: ${detail}`
      ).toBeGreaterThan(series[index - 1][key] + 0.5);
    }
  }

  watch.assertAllLocal();
});

test('a tile never clips its own content at 4K', async ({ page, request }) => {
  // Why this test exists: the internals now have no px ceiling, so removing the
  // ceilings needs a guard proving they did not turn the tile into a clipping
  // box instead. The existing kiosk spec only sweeps 4K at nine tiles, where the
  // old ceiling never bound; one, two and four tiles are the shapes where the
  // tile is large enough that uncapped type could overflow it.
  const viewport = { width: 3840, height: 2160 };

  for (const count of [1, 2, 4] as const) {
    await openWithTiles(page, request, count, viewport);

    const clipping = await page.evaluate((): readonly string[] => {
      const offenders: string[] = [];
      const cards = Array.from(document.querySelectorAll('ul.tiles li.tile .card'));
      cards.forEach((card, index) => {
        const tall = card.scrollHeight > card.clientHeight + 1;
        const wide = card.scrollWidth > card.clientWidth + 1;
        if (tall || wide) {
          offenders.push(
            `card ${index}: scrollHeight ${card.scrollHeight} vs clientHeight ${card.clientHeight}, ` +
              `scrollWidth ${card.scrollWidth} vs clientWidth ${card.clientWidth}`
          );
        }
      });
      return offenders;
    });

    expect(
      clipping,
      `${count} tiles at ${viewport.width}x${viewport.height}: a card overflows its own box ` +
        `(the tile is clipping its content) — ${JSON.stringify(clipping)}`
    ).toEqual([]);
  }

  watch.assertAllLocal();
});
