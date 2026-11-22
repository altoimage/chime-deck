import { expect, test, type Page, type Response } from '@playwright/test';
import {
  BENCH_ORIGIN,
  FRONT_DOOR,
  readEntities,
  readSettings,
  resetBackend,
  watchOffOriginRequests,
  type OffOriginWatch,
  type SettingsBodyShape
} from './fixtures.js';

/**
 * The settings screens against the **real** backend: every save is a real
 * `PATCH /api/settings` (or `PATCH /api/entities/{id}`) into the real SQLite
 * store, and the assertions read the same store back through `GET`.
 *
 * Three facts are checked for each save, and they are deliberately different
 * facts: the response echoes the new value, the store holds it, and the form is
 * clean again (an input still holding the saved value with Save disabled is the
 * only honest sign that the echo became the new baseline rather than staying
 * "unsaved").
 */

/** The URL the Connection panel is asked to save. */
const NEW_HA_URL = 'http://ha-bench.example:8123';

/** The quiet-hours start the Audio panel is asked to save. */
const NEW_QUIET_START = '23:00';

/** The icon the picker is asked to choose. */
const NEW_ICON = 'mdi:door-open';

/** The off-origin watch the `beforeEach` attached; asserted at the end of each test. */
let watch: OffOriginWatch;

test.beforeEach(async ({ page, request }) => {
  await resetBackend(request);
  watch = watchOffOriginRequests(page);
});

/** Whether a response is the settings save this tier drives. */
function isSettingsPatch(response: Response): boolean {
  return response.url().endsWith('/api/settings') && response.request().method() === 'PATCH';
}

/**
 * The one section footer's Save button, for the panels that still have a single
 * anonymous save (Audio, the entity editor) — `exact` keeps the footer's
 * aria-label out.
 *
 * Not the Connection panel: cd-ev3m replaced that section's one footer with a
 * save per field group, each labelled with what it commits, so a bare `Save` no
 * longer exists there. Its Home Assistant group uses `haSaveButton`.
 */
function saveButton(page: Page) {
  return page.getByRole('button', { name: 'Save', exact: true });
}

/** The Connection panel's Home Assistant group save, named by cd-ev3m. */
function haSaveButton(page: Page) {
  return page.getByRole('button', { name: 'Save Home Assistant settings', exact: true });
}

/** The Speech panel's own footer button, same rule as `saveButton`. */
function speechSaveButton(page: Page) {
  return page.getByRole('button', { name: 'Save speech', exact: true });
}

/** The body the settings PATCH answered with. */
async function patchBody(response: Response): Promise<SettingsBodyShape> {
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as SettingsBodyShape;
}

test('the Connection panel saves the Home Assistant URL through the real PATCH', async ({
  page,
  request
}) => {
  await page.goto('/settings/connection');

  const url = page.getByLabel('Home Assistant URL');
  await expect(url).toHaveValue('http://homeassistant.local:8123');

  // Armed before the click: the response may arrive before the click resolves.
  const patched = page.waitForResponse(isSettingsPatch);
  await url.fill(NEW_HA_URL);
  await haSaveButton(page).click();
  const body = await patchBody(await patched);

  // (a) the echo of this save…
  expect(body.settings['ha.base_url']).toBe(NEW_HA_URL);
  // (b) …and the store that a later reader sees.
  await expect.poll(async () => (await readSettings(request)).settings['ha.base_url']).toBe(NEW_HA_URL);
  // (c) the UI took the echo as its baseline: the value stayed, the form is clean.
  await expect(url).toHaveValue(NEW_HA_URL);
  await expect(haSaveButton(page)).toBeDisabled();

  watch.assertAllLocal();
});

test('the Audio panel saves the quiet-hours start hour through the real PATCH', async ({
  page,
  request
}) => {
  await page.goto('/settings/audio');

  // The quiet-hours times are Stepper −/+ groups, not inputs: the group is
  // labelled by its visible heading and the value is its own live region.
  const startHour = page.getByRole('group', { name: 'Quiet hours start hour' });
  await expect(startHour).toContainText('22');
  await expect(saveButton(page)).toBeDisabled();

  const patched = page.waitForResponse(isSettingsPatch);
  await startHour.getByRole('button', { name: 'Increase Quiet hours start hour' }).click();
  await saveButton(page).click();
  const body = await patchBody(await patched);

  expect(body.settings['audio.quiet_hours_start']).toBe(NEW_QUIET_START);
  await expect
    .poll(async () => (await readSettings(request)).settings['audio.quiet_hours_start'])
    .toBe(NEW_QUIET_START);
  await expect(startHour).toContainText('23');
  await expect(saveButton(page)).toBeDisabled();

  watch.assertAllLocal();
});

test('the icon picker stores an override through the real entity PATCH', async ({
  page,
  request
}) => {
  // The editor's URL needs the stored row's primary key, so it comes from the
  // real store rather than from a number hard-coded in this spec.
  const seeded = await readEntities(request);
  const frontDoor = seeded.find((row) => row.entity_id === FRONT_DOOR);
  if (frontDoor === undefined) {
    throw new Error(`${FRONT_DOOR} must be a seeded row`);
  }

  await page.goto(`/settings/entities/${frontDoor.id}`);

  const trigger = page.locator('.icon-trigger');
  await expect(trigger).toBeVisible();
  // The seed stores `mdi:lock` as the row's own override, so the trigger reads
  // that raw reference (the resolved icon is shown beside it).
  await expect(trigger.locator('.icon-text')).toHaveText('mdi:lock');

  await trigger.click();
  // A `Sheet` is a native <dialog>, so `[open]` is the component's own state.
  const sheet = page.locator('dialog.sheet[open]');
  await expect(sheet).toBeVisible();
  await expect(sheet.locator('.sheet-title')).toHaveText('Choose icon');

  await sheet.getByLabel('Search icons').fill('door-open');
  const cell = sheet.locator(`button.icon-cell[aria-label="${NEW_ICON.slice('mdi:'.length)}"]`);
  await expect(cell).toBeVisible();
  await cell.click();
  // Choosing closes the sheet (`choose()` clears `open`); no explicit close.
  await expect(sheet).toBeHidden();

  const patched = page.waitForResponse(
    (response) =>
      response.request().method() === 'PATCH' && response.url().includes('/api/entities/')
  );
  await expect(saveButton(page)).toBeEnabled();
  await saveButton(page).click();
  expect((await patched).ok()).toBe(true);

  await expect
    .poll(async () => (await readEntities(request)).find((row) => row.id === frontDoor.id)?.icon_override)
    .toBe(NEW_ICON);
  await expect(trigger.locator('.icon-text')).toHaveText(NEW_ICON);
  watch.assertAllLocal();
});

test('the Speech panel turns speech off and back on through the real PATCH', async ({
  page,
  request
}) => {
  await page.goto('/settings/speech');

  // The bench seeds no `tts.*` key, so the registry default is ha_tts and the piper
  // entity id is what a fresh install shows. The engine-availability rows are NOT
  // asserted: this dev host genuinely lacks espeak-ng and the piper extra.
  const engineTrigger = page
    .locator('button.trigger')
    .filter({ hasText: "Home Assistant's voices" });
  await expect(engineTrigger).toBeVisible();
  const haEngine = page.getByLabel('Home Assistant engine id');
  await expect(haEngine).toHaveValue('tts.piper');

  // D27 off: the picker's row, one save, and the off block instead of the engine fields.
  const offPatch = page.waitForResponse(isSettingsPatch);
  await engineTrigger.click();
  const sheet = page.locator('dialog.sheet[open]');
  await expect(sheet).toBeVisible();
  const offRow = sheet.locator('.options label.row').filter({ hasText: 'No speech' });
  await expect(offRow).toBeVisible();
  await offRow.click();
  // Choosing a row closes the sheet, so the Save below is the page's own footer.
  await expect(sheet).toHaveCount(0);
  await speechSaveButton(page).click();
  const offBody = await patchBody(await offPatch);

  // (a) the echo of this save, (b) the store a later reader sees.
  expect(offBody.settings['tts.default_engine']).toBe('none');
  await expect
    .poll(async () => (await readSettings(request)).settings['tts.default_engine'])
    .toBe('none');

  const off = page.getByTestId('speech-off');
  await expect(off).toBeVisible();
  await expect(off).toContainText(
    'No speech — sound files only. Sound-file chimes remain available.'
  );
  // The stored value is now `none`, so health reports the deliberate state, not a fault.
  await expect(off).toContainText('Health: disabled — this is not a degraded status.');
  // (c) the echo became the baseline: nothing left to save.
  await expect(speechSaveButton(page)).toBeDisabled();

  // Back on in one tap, and the same three facts in reverse.
  const onPatch = page.waitForResponse(isSettingsPatch);
  await page.getByRole('button', { name: 'Enable speech', exact: true }).click();
  const onBody = await patchBody(await onPatch);

  expect(onBody.settings['tts.default_engine']).toBe('ha_tts');
  await expect
    .poll(async () => (await readSettings(request)).settings['tts.default_engine'])
    .toBe('ha_tts');
  await expect(haEngine).toBeVisible();
  await expect(haEngine).toHaveValue('tts.piper');
  await expect(off).toHaveCount(0);
  await expect(speechSaveButton(page)).toBeDisabled();

  // HSD-31dk deliberately shipped this panel with no paid-provider surface, recording
  // that the badge, the cost check and the confirmation must wait until a provider
  // could actually be marked paid. HSD-s856 creates that condition, so the section is
  // present now — and the renderer's secret key is still never named by this panel.
  await expect(page.getByTestId('speech-providers')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Add provider', exact: true })).toBeEnabled();
  const words = (await page.locator('main.panel').innerText()).toLowerCase();
  expect(words).not.toContain('tts.renderer.token');

  watch.assertAllLocal();
});

/**
 * The settings sections the dashboard escape is measured on, in the nav's own
 * order: a section added to `sections.ts` must be listed here too.
 */
const SECTIONS = [
  '/settings/connection',
  '/settings/entities',
  '/settings/buttons',
  '/settings/sounds',
  '/settings/speech',
  '/settings/audio',
  '/settings/appearance',
  '/settings/history',
  '/settings/about'
] as const;

/** The wall panel's own size and the largest desktop the shell is built for. */
const ESCAPE_VIEWPORTS = [
  { width: 1024, height: 600 },
  { width: 1920, height: 1080 }
] as const;

/** The escape control's box, or a failure if it never painted. */
async function escapeBox(page: Page): Promise<{ width: number; height: number }> {
  const box = await page.locator('[data-testid="back-to-dashboard"]').boundingBox();
  if (box === null) {
    throw new Error('the dashboard escape has no box');
  }
  return box;
}

/**
 * The sticky bar's edges relative to the viewport. It must reach both edges: a
 * bar narrower than the screen lets panel content slide past its sides once the
 * page is scrolled.
 */
async function shellBarBox(
  page: Page
): Promise<{ left: number; right: number; viewportWidth: number }> {
  return await page.evaluate(() => {
    const bar = document.querySelector('[data-testid="shell-bar"]');
    if (bar === null) {
      throw new Error('the settings shell has no sticky bar');
    }
    const rect = bar.getBoundingClientRect();
    return { left: rect.left, right: rect.right, viewportWidth: window.innerWidth };
  });
}

for (const viewport of ESCAPE_VIEWPORTS) {
  test(`the dashboard is one tap away from every settings section at ${viewport.width}x${viewport.height}`, async ({
    page
  }) => {
    await page.setViewportSize(viewport);

    for (const path of SECTIONS) {
      await page.goto(path);

      const control = page.locator('[data-testid="back-to-dashboard"]');
      await expect(control, path).toBeVisible();

      // D26: a finger target, never a text link.
      const box = await escapeBox(page);
      expect(box.height, path).toBeGreaterThanOrEqual(48);
      expect(box.width, path).toBeGreaterThanOrEqual(48);

      // The trap this ticket fixes: with 3000px of panel the escape must stay
      // on screen. A bar at rest sits at the shell padding, so the honest test
      // is "did we actually scroll, and is the bar pinned to the top now".
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      const pinned = await page.evaluate(() => {
        const bar = document.querySelector('[data-testid="shell-bar"]');
        if (bar === null) {
          throw new Error('the settings shell has no sticky bar');
        }
        const rect = bar.getBoundingClientRect();
        return {
          scrollY: window.scrollY,
          top: rect.top,
          height: rect.height,
          overflow: document.body.scrollHeight - window.innerHeight
        };
      });
      if (pinned.scrollY > 0) {
        expect(Math.abs(pinned.top), `${path} bar pinned to the viewport top`).toBeLessThanOrEqual(0.5);
      } else {
        expect(pinned.overflow, `${path} page did not need to scroll`).toBeLessThanOrEqual(1);
      }
      // The bar's own height is what `--shell-bar` promises the picker's sticky
      // search, so a wrapper that grew it would silently break that offset.
      expect(pinned.height, path).toBe(64);
      await expect(control, `${path} after scrolling to the bottom`).toBeInViewport();

      const bar = await shellBarBox(page);
      expect(bar.left, path).toBeLessThanOrEqual(0.5);
      expect(bar.right, path).toBeGreaterThanOrEqual(bar.viewportWidth - 0.5);

      // One click is the whole way home, and it is a real navigation: the
      // settings root is gone, not just the URL.
      await control.click();
      await expect(page).toHaveURL(`${BENCH_ORIGIN}/`);
      expect(new URL(page.url()).pathname, path).toBe('/');
      await expect(page.locator('[data-testid="control-rail"]')).toBeVisible();
      await expect(page.locator('ul.tiles')).toHaveCount(1);
    }

    watch.assertAllLocal();
  });
}

test('the setup wizard has the same one-tap escape', async ({ page, request }) => {
  await page.goto('/setup');

  const control = page.locator('[data-testid="back-to-dashboard"]');
  await expect(control).toBeVisible();
  const box = await escapeBox(page);
  expect(box.height).toBeGreaterThanOrEqual(48);
  // The rail belongs to the dashboard; the wizard must not have it.
  await expect(page.locator('[data-testid="control-rail"]')).toHaveCount(0);

  await control.click();

  await expect(page).toHaveURL(`${BENCH_ORIGIN}/`);
  expect(new URL(page.url()).pathname).toBe('/');
  await expect(page.locator('[data-testid="control-rail"]')).toBeVisible();
  // The escape went through `dismiss`: without the saved key, `/` would bounce
  // straight back to `/setup` and the panel would be trapped again.
  expect((await readSettings(request)).settings['ui.setup_dismissed']).toBe(true);

  watch.assertAllLocal();
});
