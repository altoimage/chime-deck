import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { resetBackend, watchOffOriginRequests, type OffOriginWatch } from './fixtures.js';

/**
 * The first-run wizard's geometry, measured on the **real** backend bench
 * (HSD-8544): the real store, the real probes against the fake device edges, and
 * the shipped CSS from `web/dist`.
 *
 * The ticket's complaint was a first-run page that hid its own answers: at
 * 1280×720 the verdict to "Test connection" landed 80px below the fold and step
 * 2 needed a screen and a half of scrolling, while the primary action sat 5px
 * out of reach. A screenshot would show that; it could not say "the verdict's
 * bottom is above the pinned bar's top" or "scrollY went back to 0". So **every
 * assertion in this file is a bounding box, a scroll metric or a computed
 * style** — there is not one image comparison, and no fixed sleep stands in for
 * waiting.
 *
 * Steps 2–4 are reached by driving the real buttons, never by setting component
 * state: the wizard's steps live inside the component, and a spec that jumped
 * straight to step 3 would not prove the route a person actually walks.
 *
 * What is deliberately *not* here: the wizard's semantics (the keys a save
 * sends, the requests it makes, the words it renders). Those belong to the
 * offline suite, `src/lib/settings/setup-wizard.test.ts` and
 * `setup-wizard-roundtrip.test.ts`.
 */

/**
 * Every panel shape the ticket names (D30's spread): the 1024×600 wall panel,
 * the 1280×720 reference panel, portrait, and 1440p.
 */
const VIEWPORTS = [
  { width: 1024, height: 600 },
  { width: 1280, height: 720 },
  { width: 720, height: 1280 },
  { width: 2560, height: 1440 }
] as const;

/** The wizard's steps: 1–3 are the numbered ones, 4 is the optional security step. */
const STEPS = [1, 2, 3, 4] as const;
type Step = (typeof STEPS)[number];

/** `--tap` from `tokens.css`: no interactive element may render shorter (D26). */
const MIN_TARGET = 48;

/** `--tap-primary` from `tokens.css`: `size="lg"` and primary buttons. */
const MIN_PRIMARY_TARGET = 56;

/** The two viewports the ticket's own defect was reported at. */
const FOLD_VIEWPORTS = [
  { width: 1024, height: 600 },
  { width: 1280, height: 720 }
] as const;

/** The panels the D26 matrix measures; a 4K panel adds nothing a tap size can fail. */
const SCALE_VIEWPORTS = [
  { width: 1280, height: 720 },
  { width: 1024, height: 600 },
  { width: 720, height: 1280 }
] as const;

/** The default step (which renders no attribute) and the floor step. */
const SCALE_STEPS = [100, 85] as const;
type ScaleStep = (typeof SCALE_STEPS)[number];

/** The action row whose `position: sticky` is the ticket's fix. */
const ACTION_BAR = '.actions--sticky';

/** The off-origin watch the `beforeEach` attached; asserted at the end of each test. */
let watch: OffOriginWatch;

test.beforeEach(async ({ page, request }) => {
  // `/__test/reset` restores the bench's canonical settings, which is what makes
  // "Home Assistant is configured, the broker is not" true for every test here.
  await resetBackend(request);
  watch = watchOffOriginRequests(page);
});

test.afterEach(async ({ page, request }) => {
  // `ui.scale` is the one setting this spec writes. The reset restores it, but a
  // test that failed before its own `finally` would leave the panel zoomed for
  // the next file, so it is put back here as well.
  await applyScale(request, 100);
  await page.setViewportSize({ width: 1280, height: 720 });
  watch.assertAllLocal();
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

/** One interactive element, as painted and named. */
interface ControlReading {
  readonly tag: string;
  /** The accessible name: `aria-label`, else the `label[for]`, else the text. */
  readonly label: string;
  readonly rect: BoxRect;
}

/** Everything the horizontal-overflow check compares, read in one pass. */
interface DocumentMetrics {
  readonly controls: readonly ControlReading[];
  readonly innerWidth: number;
  readonly innerHeight: number;
  readonly scrollWidth: number;
  readonly clientWidth: number;
  readonly scrollHeight: number;
  readonly clientHeight: number;
  readonly scrollY: number;
  /** `ui.scale`, or null at the default step (the base rendering path). */
  readonly scale: string | null;
}

/** One control that an ancestor with a non-visible overflow cuts off. */
interface ClippedControl {
  readonly label: string;
  readonly ancestor: string;
  /** How far the control's edges fall outside that ancestor's padding box. */
  readonly over: {
    readonly left: number;
    readonly right: number;
    readonly top: number;
    readonly bottom: number;
  };
}

/** The pinned action row, with a hit test for each of its buttons. */
interface ActionBarReading {
  readonly rect: BoxRect;
  readonly innerWidth: number;
  readonly innerHeight: number;
  readonly scrollY: number;
  readonly buttons: readonly {
    readonly label: string;
    readonly bottom: number;
    /** `self` when the centre hit-tests to the button, else what is there instead. */
    readonly hit: string;
  }[];
}

/** One element the scale matrix measures, rendered size included. */
interface TargetReading {
  readonly tag: string;
  readonly label: string;
  readonly height: number;
  readonly primary: boolean;
}

/** One Tab stop, as the document reports it. */
interface FocusStop {
  readonly tag: string;
  readonly label: string;
  /** A one-line description of the visible focus indicator, or `none`. */
  readonly ring: string;
}
/**
 * Every painted interactive element, with its rect, plus the document's scroll
 * metrics.
 *
 * The three helper functions are declared inside the evaluation rather than
 * shared, because a `page.evaluate` body is serialised and can close over
 * nothing: each evaluation below re-declares the two or three lines it needs.
 *
 * `getBoundingClientRect()` and never `getComputedStyle().height` throughout this
 * file: a computed height is reported *before* the root `zoom`, so an 85% panel
 * would report the same 48px as a 100% one and the whole D26 matrix would
 * measure nothing.
 *
 * @param page the page showing the wizard.
 */
async function measureControls(page: Page): Promise<DocumentMetrics> {
  return page.evaluate((): DocumentMetrics => {
    function isVisible(element: Element): boolean {
      const style = getComputedStyle(element);
      if (style.display === 'none' || style.visibility === 'hidden') {
        return false;
      }
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    }

    function nameOf(element: Element): string {
      const aria = (element.getAttribute('aria-label') ?? '').trim();
      if (aria !== '') {
        return aria;
      }
      if (element.id !== '') {
        const label = document.querySelector(`label[for="${element.id}"]`);
        if (label !== null) {
          return (label.textContent ?? '').replace(/\s+/g, ' ').trim();
        }
      }
      return (element.textContent ?? '').replace(/\s+/g, ' ').trim();
    }

    const controls = Array.from(document.querySelectorAll('button, a[href], input'))
      .filter(isVisible)
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          tag: element.tagName.toLowerCase(),
          label: nameOf(element),
          rect: {
            left: rect.left,
            right: rect.right,
            top: rect.top,
            bottom: rect.bottom,
            width: rect.width,
            height: rect.height
          }
        };
      });

    const scrolling = document.scrollingElement ?? document.documentElement;
    return {
      controls,
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      scrollWidth: scrolling.scrollWidth,
      clientWidth: scrolling.clientWidth,
      scrollHeight: scrolling.scrollHeight,
      clientHeight: scrolling.clientHeight,
      scrollY: window.scrollY,
      scale: document.documentElement.dataset.uiScale ?? null
    };
  });
}

/**
 * Every control an ancestor cuts off.
 *
 * The walk is what separates "nothing overflows the viewport" from "nothing is
 * clipped": the wizard has no overflow of its own today, so anything found here
 * is a scroller or a clipper a later ticket added, and a control that happens to
 * sit inside the viewport horizontally would still be unreachable. 1.5px of
 * tolerance: a fractional layout width is not a clip.
 *
 * @param page the page showing the wizard.
 */
async function clippedControls(page: Page): Promise<readonly ClippedControl[]> {
  return page.evaluate((): ClippedControl[] => {
    function isVisible(element: Element): boolean {
      const style = getComputedStyle(element);
      if (style.display === 'none' || style.visibility === 'hidden') {
        return false;
      }
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    }

    function nameOf(element: Element): string {
      const aria = (element.getAttribute('aria-label') ?? '').trim();
      if (aria !== '') {
        return aria;
      }
      if (element.id !== '') {
        const label = document.querySelector(`label[for="${element.id}"]`);
        if (label !== null) {
          return (label.textContent ?? '').replace(/\s+/g, ' ').trim();
        }
      }
      return (element.textContent ?? '').replace(/\s+/g, ' ').trim();
    }

    const found: ClippedControl[] = [];
    for (const control of Array.from(document.querySelectorAll('button, a[href], input'))) {
      if (!isVisible(control)) {
        continue;
      }
      const rect = control.getBoundingClientRect();
      for (let node = control.parentElement; node !== null; node = node.parentElement) {
        const style = getComputedStyle(node);
        const clipsX = style.overflowX !== 'visible';
        const clipsY = style.overflowY !== 'visible';
        if (!clipsX && !clipsY) {
          continue;
        }
        // An overflow clips against the padding box, so the borders come off.
        const box = node.getBoundingClientRect();
        const left = box.left + parseFloat(style.borderLeftWidth);
        const right = box.right - parseFloat(style.borderRightWidth);
        const top = box.top + parseFloat(style.borderTopWidth);
        const bottom = box.bottom - parseFloat(style.borderBottomWidth);
        const over = {
          left: clipsX ? Math.max(0, left - rect.left) : 0,
          right: clipsX ? Math.max(0, rect.right - right) : 0,
          top: clipsY ? Math.max(0, top - rect.top) : 0,
          bottom: clipsY ? Math.max(0, rect.bottom - bottom) : 0
        };
        if (over.left > 1.5 || over.right > 1.5 || over.top > 1.5 || over.bottom > 1.5) {
          const classes = String(node.className ?? '').split(' ').filter(Boolean).join('.');
          found.push({
            label: nameOf(control),
            ancestor: node.tagName.toLowerCase() + (classes === '' ? '' : `.${classes}`),
            over
          });
        }
      }
    }
    return found;
  });
}

/**
 * The pinned action row: its rect, and where each of its buttons' centres land.
 *
 * The hit test is the point. A bar inside the viewport but painted under
 * something — the sticky masthead, a toast, another step's row — passes a rect
 * check and fails a finger; `elementFromPoint` answers the question the finger
 * asks.
 *
 * @param page the page showing the wizard.
 */
async function actionBar(page: Page): Promise<ActionBarReading> {
  return page.evaluate((barSelector): ActionBarReading => {
    function nameOf(element: Element): string {
      const aria = (element.getAttribute('aria-label') ?? '').trim();
      if (aria !== '') {
        return aria;
      }
      return (element.textContent ?? '').replace(/\s+/g, ' ').trim();
    }

    const bar = document.querySelector(barSelector);
    if (bar === null) {
      throw new Error(`the wizard has no ${barSelector} row`);
    }
    const rect = bar.getBoundingClientRect();
    const buttons = Array.from(bar.querySelectorAll('button')).map((element) => {
      const buttonRect = element.getBoundingClientRect();
      const hit = document.elementFromPoint(
        buttonRect.left + buttonRect.width / 2,
        buttonRect.top + buttonRect.height / 2
      );
      let result = 'self';
      if (hit === null) {
        result = 'nothing';
      } else if (hit !== element && !element.contains(hit)) {
        const classes = String(hit.className ?? '').split(' ').filter(Boolean).join('.');
        result = `other: ${hit.tagName.toLowerCase()}${
          classes === '' ? '' : `.${classes}`
        } "${nameOf(hit)}"`;
      }
      return { label: nameOf(element), bottom: buttonRect.bottom, hit: result };
    });

    return {
      rect: {
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        width: rect.width,
        height: rect.height
      },
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      scrollY: window.scrollY,
      buttons
    };
  }, ACTION_BAR);
}

/**
 * Every painted interactive element's rendered height, at the current scale.
 *
 * @param page the page showing the wizard.
 */
async function measureTargets(page: Page): Promise<readonly TargetReading[]> {
  return page.evaluate((): TargetReading[] => {
    function isVisible(element: Element): boolean {
      const style = getComputedStyle(element);
      if (style.display === 'none' || style.visibility === 'hidden') {
        return false;
      }
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    }

    function nameOf(element: Element): string {
      const aria = (element.getAttribute('aria-label') ?? '').trim();
      if (aria !== '') {
        return aria;
      }
      if (element.id !== '') {
        const label = document.querySelector(`label[for="${element.id}"]`);
        if (label !== null) {
          return (label.textContent ?? '').replace(/\s+/g, ' ').trim();
        }
      }
      return (element.textContent ?? '').replace(/\s+/g, ' ').trim();
    }

    return Array.from(document.querySelectorAll('button, a[href], input'))
      .filter(isVisible)
      .map((element) => ({
        tag: element.tagName.toLowerCase(),
        label: nameOf(element),
        height: element.getBoundingClientRect().height,
        primary: element.tagName === 'BUTTON' && element.classList.contains('lg')
      }));
  });
}

/**
 * Read the focused element's tag, its name and its visible focus indicator.
 *
 * The ring is computed, not inferred, and it is read from the focused element
 * **or its field wrapper**: `ui/Input` deliberately moves the ring from the
 * `<input>` to its `.control` box (`input:focus-visible { outline: none }` plus
 * `.control:has(input:focus-visible)`), so reading only the input would report
 * "no ring" for a control that is ringed. Walking up three levels finds that
 * wrapper without turning "is anything focused" into "is anything nearby".
 *
 * @param page the page whose focus is read.
 */
async function readFocus(page: Page): Promise<FocusStop> {
  return page.evaluate((): FocusStop => {
    const element = document.activeElement;
    if (element === null) {
      return { tag: 'none', label: 'none', ring: 'none' };
    }
    const aria = (element.getAttribute('aria-label') ?? '').trim();
    let label = aria;
    if (label === '') {
      if (element.id !== '') {
        const field = document.querySelector(`label[for="${element.id}"]`);
        label = field === null ? '' : (field.textContent ?? '').replace(/\s+/g, ' ').trim();
      }
      if (label === '') {
        label = (element.textContent ?? '').replace(/\s+/g, ' ').trim();
      }
    }

    const style = getComputedStyle(element);
    const isRinged = (candidate: Element): string | null => {
      const computed = getComputedStyle(candidate);
      if (computed.outlineStyle !== 'none' && parseFloat(computed.outlineWidth) > 0) {
        return `outline ${computed.outlineWidth} ${computed.outlineStyle} ${computed.outlineColor}`;
      }
      return computed.boxShadow !== 'none' ? `box-shadow ${computed.boxShadow}` : null;
    };

    let ring = isRinged(element);
    let node = element.parentElement;
    for (let depth = 0; ring === null && node !== null && depth < 3; depth += 1) {
      const classes = String(node.className ?? '').split(' ').filter(Boolean).join('.');
      const found = isRinged(node);
      if (found !== null) {
        ring = `${found} on ${node.tagName.toLowerCase()}${classes === '' ? '' : `.${classes}`}`;
      }
      node = node.parentElement;
    }

    return { tag: element.tagName.toLowerCase(), label, ring: ring ?? 'none' };
  });
}

/** The button whose text contains *label*, the way a person reads it. */
function button(page: Page, label: string) {
  // `hasText` is a case-insensitive substring match, so "Test connection" cannot
  // also match "Test MQTT", and the probe's own spinner label ("Working") does
  // not knock it out of the locator while the request is in flight.
  return page.locator('button', { hasText: label }).first();
}

/**
 * Run one step's probe and take it when its gate opens.
 *
 * The gate is polled (`toBeEnabled`), never slept through: step 1 enables
 * Continue on a successful pass and step 2 on a completed test, and neither is a
 * fixed duration.
 *
 * @param page the page to drive.
 * @param probe the probe button's visible text.
 */
async function probeAndContinue(page: Page, probe: string): Promise<void> {
  await button(page, probe).click();
  const next = button(page, 'Continue');
  await expect(next, `${probe} must open the Continue gate`).toBeEnabled();
  await next.click();
}

/**
 * Drive the real buttons from `/setup` to *step*, starting from a fresh load.
 *
 * @param page the page to drive.
 * @param step the step to stop on (1–4).
 */
async function openStep(page: Page, step: Step): Promise<void> {
  await page.goto('/setup');
  await expect(page.locator('[data-setup-step="1"]'), 'the wizard opens on step 1').toBeVisible();

  if (step >= 2) {
    await probeAndContinue(page, 'Test connection');
    await expect(page.locator('[data-setup-step="2"]'), 'step 1 advances to step 2').toBeVisible();
  }
  if (step >= 3) {
    await probeAndContinue(page, 'Test MQTT');
    await expect(page.locator('[data-setup-step="3"]'), 'step 2 advances to step 3').toBeVisible();
  }
  if (step === 4) {
    await button(page, 'Protect the configuration interface').click();
    await expect(
      page.locator('[data-setup-step="4"]'),
      'the optional security step opens'
    ).toBeVisible();
  }
}

/**
 * Store `ui.scale` through the real settings PATCH.
 *
 * @param request the test's API request context.
 * @param step the step to store, as the registry's percent integer.
 */
async function applyScale(request: APIRequestContext, step: ScaleStep): Promise<void> {
  const patch = await request.patch('/api/settings', { data: { 'ui.scale': step } });
  expect(patch.ok(), await patch.text()).toBe(true);
}

/**
 * Wait until the document renders at *step*, and assert both halves of it.
 *
 * The attribute is the input (`tokens.css` turns it into `--ui-scale`) and the
 * computed root `zoom` is what the renderer applies, so both are asserted: an
 * attribute with no zoom rule is a stale `web/dist`, and a zoom with a second
 * factor elsewhere is the double-scaling defect. 100 is the default step and
 * leaves no attribute behind.
 *
 * @param page the page whose document element is measured.
 * @param step the step the page must be rendering at.
 */
async function expectScaleApplied(page: Page, step: ScaleStep): Promise<void> {
  await expect
    .poll(
      () =>
        page.evaluate(() => ({
          scaleAttr: document.documentElement.dataset.uiScale ?? null,
          zoom:
            Math.round(parseFloat(getComputedStyle(document.documentElement).zoom) * 1000) / 1000
        })),
      { message: `ui.scale ${step} must be one root zoom on <html> (${step / 100}x)` }
    )
    .toEqual({ scaleAttr: step === 100 ? null : String(step), zoom: step / 100 });
}

/** The whole measurement, so a failure names the panel and the numbers. */
function detailOf(label: string, metrics: DocumentMetrics): string {
  return `${label}: ${JSON.stringify(metrics)}`;
}

/* ---- 1. no clipping, no horizontal overflow ------------------------------ */

test('the wizard neither clips nor overflows horizontally at any supported viewport', async ({
  page
}) => {
  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport);

    for (const step of STEPS) {
      const label = `${viewport.width}x${viewport.height} step ${step}`;
      await openStep(page, step);

      await expect(page.locator('.setup-shell'), `${label}: the shell must paint`).toBeVisible();

      const escape = page.locator('[data-testid="back-to-dashboard"]');
      await expect(escape, `${label}: the escape must be reachable`).toBeVisible();
      await expect(escape, `${label}: the escape names its destination`).toHaveText('Dashboard');
      const escapeBox = await escape.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        return { width: rect.width, height: rect.height };
      });
      expect(
        escapeBox.height,
        `${label}: the escape is a D26 target, ${JSON.stringify(escapeBox)}`
      ).toBeGreaterThanOrEqual(MIN_TARGET);

      // The rail belongs to the dashboard; the wizard must not inherit it.
      await expect(
        page.locator('[data-testid="control-rail"]'),
        `${label}: the wizard has no dashboard control rail`
      ).toHaveCount(0);

      const metrics = await measureControls(page);
      const detail = detailOf(label, metrics);

      expect(
        metrics.scrollWidth,
        `the document scrolls horizontally (scrollWidth ${metrics.scrollWidth} vs clientWidth ` +
          `${metrics.clientWidth}) — ${detail}`
      ).toBeLessThanOrEqual(metrics.clientWidth + 1);

      // Every painted control inside the viewport's inline axis: one past the
      // right edge is a control a finger cannot reach.
      const outside = metrics.controls.filter(
        (control) => control.rect.left < -1 || control.rect.right > metrics.innerWidth + 1
      );
      expect(
        outside,
        `a control is outside the viewport's width (innerWidth ${metrics.innerWidth}) — ${detail}`
      ).toEqual([]);

      const clipped = await clippedControls(page);
      expect(
        clipped,
        `a control is clipped by an ancestor that scrolls or hides its overflow — ${detail}`
      ).toEqual([]);
    }
  }
});

/* ---- 2. the primary action is on screen --------------------------------- */

test('the primary action is on screen before any scrolling', async ({ page }) => {
  for (const viewport of FOLD_VIEWPORTS) {
    await page.setViewportSize(viewport);

    for (const step of STEPS) {
      const label = `${viewport.width}x${viewport.height} step ${step}`;
      await openStep(page, step);
      await page.evaluate(() => window.scrollTo(0, 0));

      const bar = await actionBar(page);
      const detail = `${label}: ${JSON.stringify(bar)}`;

      // The bar is pinned to the viewport bottom, so the answer follows the
      // question instead of landing 80–310px below the fold.
      expect(bar.scrollY, `the panel is at the top of the document — ${detail}`).toBe(0);
      expect(
        bar.rect.top,
        `the action bar starts above the viewport — ${detail}`
      ).toBeGreaterThanOrEqual(-1);
      expect(
        bar.rect.bottom,
        `the action bar runs past the viewport bottom (innerHeight ${bar.innerHeight}) — ${detail}`
      ).toBeLessThanOrEqual(bar.innerHeight + 1);
      expect(
        bar.rect.left,
        `the action bar starts left of the viewport — ${detail}`
      ).toBeGreaterThanOrEqual(-1);
      expect(
        bar.rect.right,
        `the action bar runs past the viewport's right edge (innerWidth ` +
          `${bar.innerWidth}) — ${detail}`
      ).toBeLessThanOrEqual(bar.innerWidth + 1);

      const lastBottom = Math.max(...bar.buttons.map((entry) => entry.bottom));
      expect(
        lastBottom,
        `the bar's last row is below the fold (innerHeight ${bar.innerHeight}) — ${detail}`
      ).toBeLessThanOrEqual(bar.innerHeight + 1);

      // A bar that is on screen but painted under something is not on screen.
      for (const entry of bar.buttons) {
        expect(
          entry.hit,
          `a tap at "${entry.label}"'s centre does not land on the button — ${detail}`
        ).toBe('self');
      }
    }
  }
});

/* ---- 3. the page scrolls, it does not trap ------------------------------ */

test('the page scrolls instead of trapping tall content', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 600 });
  await openStep(page, 2);

  const tall = await measureControls(page);
  const detail = detailOf('1024x600 step 2', tall);

  // Step 2 at 1024x600 is taller than the panel — that is the case the ticket is
  // about, so an unscrollable one would have nothing to prove.
  expect(
    tall.scrollHeight,
    `step 2 must be taller than the panel for this test to mean anything — ${detail}`
  ).toBeGreaterThan(tall.clientHeight + 1);

  await page.evaluate(() => window.scrollTo(0, 300));
  const scrolled = await measureControls(page);
  expect(
    scrolled.scrollY,
    `the document did not scroll to 300 — ${detailOf('1024x600 step 2', scrolled)}`
  ).toBe(300);

  // The document must be the only scroller: a nested `overflow-y: auto` region
  // on a 600px touch panel is the trap this ticket removes.
  const scrollers = await page.evaluate(() => {
    const step = document.querySelector('[data-setup-step="2"]');
    if (step === null) {
      throw new Error('step 2 is not on screen');
    }
    const found: string[] = [];
    for (let node = step.parentElement; node !== null; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.overflowY === 'auto' || style.overflowY === 'scroll') {
        const classes = String(node.className ?? '').split(' ').filter(Boolean).join('.');
        found.push(
          `${node.tagName.toLowerCase()}${classes === '' ? '' : `.${classes}`} (overflow-y: ${
            style.overflowY
          })`
        );
      }
    }
    return found;
  });
  expect(
    scrollers,
    `a content region between step 2 and <html> is its own vertical scroller — ${detail}`
  ).toEqual([]);

  // ...and the bottom really is reachable, which is the other half of "scrolls,
  // does not trap": the last control is not stranded below the scroll range.
  await page.evaluate(() => {
    const scrolling = document.scrollingElement ?? document.documentElement;
    window.scrollTo(0, scrolling.scrollHeight);
  });
  const bottom = await measureControls(page);
  const last = bottom.controls[bottom.controls.length - 1];
  expect(last, `the wizard must have at least one control — ${detail}`).toBeTruthy();
  expect(
    last.rect.bottom,
    `the last control ("${last.label}") cannot be scrolled into view (bottom ` +
      `${last.rect.bottom.toFixed(1)} vs innerHeight ${bottom.innerHeight}) — ${detailOf(
        '1024x600 step 2',
        bottom
      )}`
  ).toBeLessThanOrEqual(bottom.innerHeight + 1);
});

/* ---- 4. the verdict is visible the moment it arrives -------------------- */

test('a connection verdict is visible the moment it arrives', async ({ page }) => {
  for (const viewport of FOLD_VIEWPORTS) {
    await page.setViewportSize(viewport);

    for (const step of [1, 2] as const) {
      const label = `${viewport.width}x${viewport.height} step ${step}`;
      await openStep(page, step);

      const probe = step === 1 ? 'Test connection' : 'Test MQTT';
      const selector = `[data-setup-step="${step}"] .result`;
      await button(page, probe).click();

      // Polled on the rendered text, never slept for: the probe is a real HTTP
      // round trip against the bench's fake device edge.
      await expect
        .poll(
          () =>
            page
              .locator(selector)
              .evaluate((element) => (element.textContent ?? '').trim()),
          { message: `${label}: ${probe} must render a verdict` }
        )
        .not.toBe('');

      const measured = await page.evaluate((resultSelector) => {
        const result = document.querySelector(resultSelector);
        const bar = document.querySelector('.actions--sticky');
        if (result === null || bar === null) {
          throw new Error('the step has no verdict or the wizard has no action bar');
        }
        const resultRect = result.getBoundingClientRect();
        const barRect = bar.getBoundingClientRect();
        return {
          verdict: {
            left: resultRect.left,
            right: resultRect.right,
            top: resultRect.top,
            bottom: resultRect.bottom,
            width: resultRect.width,
            height: resultRect.height
          },
          barTop: barRect.top,
          innerWidth: window.innerWidth,
          innerHeight: window.innerHeight,
          scrollY: window.scrollY,
          text: (result.textContent ?? '').replace(/\s+/g, ' ').trim()
        };
      }, selector);

      const detail = `${label}: ${JSON.stringify(measured)}`;

      // The whole point: the answer to the user's own tap is above the pinned
      // bar, not underneath it and not below the fold.
      expect(
        measured.verdict.bottom,
        `the verdict ("${measured.text}") is hidden under the action bar (its top is ` +
          `${measured.barTop.toFixed(1)}) — ${detail}`
      ).toBeLessThanOrEqual(measured.barTop + 1);
      expect(
        measured.verdict.top,
        `the verdict starts above the viewport — ${detail}`
      ).toBeGreaterThanOrEqual(-1);
      expect(
        measured.verdict.bottom,
        `the verdict runs past the viewport bottom (innerHeight ${measured.innerHeight}) — ${detail}`
      ).toBeLessThanOrEqual(measured.innerHeight + 1);
      expect(
        measured.verdict.left,
        `the verdict starts left of the viewport — ${detail}`
      ).toBeGreaterThanOrEqual(-1);
      expect(
        measured.verdict.right,
        `the verdict runs past the viewport's right edge (innerWidth ` +
          `${measured.innerWidth}) — ${detail}`
      ).toBeLessThanOrEqual(measured.innerWidth + 1);
    }
  }
});

/* ---- 5. D26 at both offered scales ------------------------------------- */

test('every target meets D26 at 100 % and 85 % scale', async ({ page, request }) => {
  try {
    for (const viewport of SCALE_VIEWPORTS) {
      await page.setViewportSize(viewport);

      for (const scale of SCALE_STEPS) {
        // Step 1 carries the connection form; step 3 carries the speaker card,
        // its slider and its buttons. Together they are every control shape the
        // wizard can render.
        for (const step of [1, 3] as const) {
          const label = `${viewport.width}x${viewport.height} step ${step} at ${scale}%`;
          await applyScale(request, scale);
          await openStep(page, step);
          await expectScaleApplied(page, scale);

          const targets = await measureTargets(page);
          const detail = `${label}: ${JSON.stringify(targets)}`;

          const small = targets.filter((target) => target.height < MIN_TARGET);
          expect(
            small,
            `an interactive element renders shorter than ${MIN_TARGET}px — ${detail}`
          ).toEqual([]);

          const primaries = targets.filter((target) => target.primary);
          expect(
            primaries.length,
            `the step must offer a primary action to measure — ${detail}`
          ).toBeGreaterThan(0);
          const shortPrimaries = primaries.filter(
            (target) => target.height < MIN_PRIMARY_TARGET
          );
          expect(
            shortPrimaries,
            `a large/primary button renders shorter than ${MIN_PRIMARY_TARGET}px — ${detail}`
          ).toEqual([]);
        }
      }
    }
  } finally {
    await applyScale(request, 100);
  }
});

/* ---- 6. keyboard ------------------------------------------------------- */

test('keyboard: a full tab order, a visible focus ring, and honest busy/disabled states', async ({
  page
}) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await openStep(page, 1);

  // Focus the page itself first, so the walk starts where a keyboard user does.
  await page.locator('body').click({ position: { x: 5, y: 5 } });
  await page.keyboard.press('Escape');

  const before: FocusStop[] = [];
  for (let press = 0; press < 12; press += 1) {
    await page.keyboard.press('Tab');
    const stop = await readFocus(page);
    before.push(stop);
    if (stop.label === 'Test connection') {
      break;
    }
  }

  const beforeDetail = `${JSON.stringify(before, null, 0)} (viewport 1280x720, step 1)`;
  // The escape, then the form in reading order, then its two field buttons. The
  // token field's reveal and clear controls come before the actions because they
  // belong to the field (HSD-9xpm).
  expect(
    before.map((stop) => stop.label),
    `the tab order through step 1 is wrong — ${beforeDetail}`
  ).toEqual([
    'Back to dashboard',
    'Home Assistant URL',
    'Long-lived token',
    'Show value',
    'Clear stored token',
    'Test connection'
  ]);

  // Every stop must show where it is; an invisible ring is the same failure as
  // no ring at all.
  for (const stop of before) {
    expect(
      stop.ring,
      `"${stop.label}" has no visible focus indicator (${stop.ring}) — ${beforeDetail}`
    ).not.toBe('none');
  }

  const continueButton = button(page, 'Continue');
  await expect(
    continueButton,
    'Continue must be shut until the connection has been tested'
  ).toBeDisabled();

  // A route delay is the only way to observe the in-flight state; the 700ms is
  // the thing being measured — the length of the request — not a stand-in for
  // waiting for something else.
  const probeRoute = '**/api/settings/test/ha';
  await page.route(probeRoute, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 700));
    await route.continue();
  });

  const probe = button(page, 'Test connection');
  await probe.click();

  await expect(
    probe,
    'a probe in flight must announce itself and refuse a second tap'
  ).toHaveAttribute('aria-busy', 'true');
  await expect(probe, 'a probe in flight must be disabled').toBeDisabled();

  const verdict = page.locator('[data-setup-step="1"] .result');
  await expect
    .poll(() => verdict.evaluate((element) => (element.textContent ?? '').trim()), {
      message: 'the verdict must arrive once the probe answers'
    })
    .not.toBe('');

  await expect(
    verdict,
    'the verdict must be announced when it lands, not only when it is looked at'
  ).toHaveAttribute('aria-live', 'polite');
  await expect(
    probe,
    'the button must report that it is no longer working'
  ).toHaveAttribute('aria-busy', 'false');
  await page.unroute(probeRoute);

  // The gate is open now, and its own controls join the tab order from here: a
  // disabled button is not a tab stop, which is why Continue is missing above.
  await expect(continueButton, 'Continue opens once the probe has passed').toBeEnabled();

  // Restart the walk from the top of the page (the probe's own click left the
  // sequence mid-bar) so the *whole* order can be read in one list.
  await page.locator('body').click({ position: { x: 5, y: 5 } });
  await page.keyboard.press('Escape');

  const after: FocusStop[] = [];
  for (let press = 0; press < 12; press += 1) {
    await page.keyboard.press('Tab');
    const stop = await readFocus(page);
    after.push(stop);
    if (stop.label === "I'll do it later") {
      break;
    }
  }
  const afterDetail = JSON.stringify(after);
  expect(
    after.map((stop) => stop.label),
    `the tab order with the gate open is wrong — ${afterDetail}`
  ).toEqual([
    'Back to dashboard',
    'Home Assistant URL',
    'Long-lived token',
    'Show value',
    'Clear stored token',
    'Test connection',
    'Continue',
    "I'll do it later"
  ]);
  for (const stop of after) {
    expect(
      stop.ring,
      `"${stop.label}" has no visible focus indicator (${stop.ring}) — ${afterDetail}`
    ).not.toBe('none');
  }
});

/* ---- 7. a step change starts at the top -------------------------------- */

test('a step change starts at the top', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await openStep(page, 1);

  // Continue is shut until the probe has passed, so the gate is opened first and
  // only then is the page parked at the bottom — the scroll offset has to be a
  // real one for "did it reset" to mean anything.
  await button(page, 'Test connection').click();
  await expect(button(page, 'Continue'), 'the gate must open before the move').toBeEnabled();

  const parked = await page.evaluate(() => {
    window.scrollTo(0, document.body.scrollHeight);
    const scrolling = document.scrollingElement ?? document.documentElement;
    return { scrollY: window.scrollY, overflow: scrolling.scrollHeight - window.innerHeight };
  });
  const atTop = `1280x720 step 1: ${JSON.stringify(parked)}`;
  expect(
    parked.overflow,
    `step 1 must be scrollable for this test to mean anything — ${atTop}`
  ).toBeGreaterThan(1);
  expect(parked.scrollY, `the page did not scroll — ${atTop}`).toBeGreaterThan(1);

  // Continue lives in the pinned bar, so it is reachable from the bottom of a
  // long page — which is the whole reason the bar is pinned.
  await button(page, 'Continue').click();
  await expect(page.locator('[data-setup-step="2"]'), `step 2 must open — ${atTop}`).toBeVisible();

  const landed = await page.evaluate(() => {
    const title = document.querySelector('[data-setup-step="2"] .step-title');
    const bar = document.querySelector('[data-testid="shell-bar"]');
    if (title === null || bar === null) {
      throw new Error('step 2 has no title, or the shell has no bar');
    }
    const titleRect = title.getBoundingClientRect();
    const barRect = bar.getBoundingClientRect();
    return {
      scrollY: window.scrollY,
      titleTop: titleRect.top,
      mastheadBottom: barRect.bottom,
      heading: (title.textContent ?? '').replace(/\s+/g, ' ').trim()
    };
  });
  const landedDetail = `1280x720 step 1 -> 2: ${JSON.stringify(landed)}`;

  expect(
    landed.scrollY,
    `the scroll position carried over from step 1, so step 2 opens mid-page — ${landedDetail}`
  ).toBe(0);
  expect(
    landed.titleTop,
    `step 2's title is above the viewport — ${landedDetail}`
  ).toBeGreaterThanOrEqual(-1);
  expect(
    landed.titleTop,
    `step 2's title ("${landed.heading}") is hidden under the masthead (its bottom is ` +
      `${landed.mastheadBottom.toFixed(1)}) — ${landedDetail}`
  ).toBeGreaterThanOrEqual(landed.mastheadBottom - 0.5);
});
