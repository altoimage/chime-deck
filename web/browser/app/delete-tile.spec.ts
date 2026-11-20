import { expect, test, type Page, type Response } from '@playwright/test';
import {
  FRONT_DOOR,
  SEED,
  readEntities,
  resetBackend,
  watchOffOriginRequests,
  type EntityRowShape,
  type OffOriginWatch
} from './fixtures.js';

/**
 * Deleting a tile from the Entities settings list, against the **real**
 * backend: the confirmation is the shipped `Dialog` (always a `Sheet`, so a
 * native `<dialog class="sheet" open>`), the confirm is a real
 * `DELETE /api/entities/{id}` into the real store, and the dashboard's
 * disappearance of the tile is the real `config_changed` refetch on its own
 * websocket.
 *
 * The four tests are deliberately different facts, in the order an owner meets
 * them: the delete button asks first and changes nothing when refused; the
 * confirm changes the list, the store and any dashboard mounted afterwards; an
 * open dashboard drops the tile without a reload; and deleting the last tile
 * leaves the documented empty panel rather than a broken one.
 *
 * Every test asserts the 1024x600 wall panel explicitly, because the
 * full-screen (not desktop-modal) shape of the confirmation is a size-dependent
 * claim (D26/D30) and it must not be inherited silently from the config.
 */

/** Every dashboard tile on the page, whatever its staleness. */
const TILES = 'ul.tiles li.tile';

/** A tile with the first real snapshot behind it (staleness cleared). */
const LIVE_TILES = 'ul.tiles li.tile[data-stale="false"]';

/** The wall panel: the project's default browser viewport. */
const VIEWPORT = { width: 1024, height: 600 } as const;

/** The confirmation, named by the component's own class, exactly as `settings.spec.ts` does. */
const SHEET = 'dialog.sheet[open]';

/** The off-origin watch the `beforeEach` attached; asserted at the end of each test. */
let watch: OffOriginWatch;

test.beforeEach(async ({ page, request }) => {
  await resetBackend(request);
  watch = watchOffOriginRequests(page);
});

/** The open confirmation sheet. */
function sheet(page: Page) {
  return page.locator(SHEET);
}

/**
 * One row's delete affordance, by its exact accessible name (`Delete <name>`).
 *
 * `exact` on purpose: without it the row button and the sheet's own
 * `Delete tile` would both match a looser query.
 */
function deleteButton(page: Page, name: string) {
  return page.getByRole('button', { name: `Delete ${name}`, exact: true });
}

/** One row's tap-to-edit body, which is how a spec says "the row is still listed". */
function editRow(page: Page, name: string) {
  return page.getByRole('button', { name: `Edit ${name}`, exact: true });
}

/** Every row's delete affordance, whatever the row's name. */
function deleteButtons(page: Page) {
  return page.getByRole('button', { name: /^Delete / });
}

/** Whether a response is this tier's tile deletion. */
function isEntityDelete(response: Response): boolean {
  return (
    response.request().method() === 'DELETE' &&
    new URL(response.url()).pathname.startsWith('/api/entities/')
  );
}

/** One seeded row, looked up in the store's own answer rather than a hard-coded id. */
function rowFor(rows: readonly EntityRowShape[], entityId: string): EntityRowShape {
  const row = rows.find((candidate) => candidate.entity_id === entityId);
  if (row === undefined) {
    throw new Error(`${entityId} must be a seeded row`);
  }
  return row;
}

/** Press the confirmation's destructive button and return its real answer. */
async function confirmDelete(page: Page): Promise<Response> {
  // Armed before the click: the response may arrive before the click resolves.
  const answered = page.waitForResponse(isEntityDelete);
  await sheet(page).getByRole('button', { name: 'Delete tile', exact: true }).click();
  const response = await answered;
  expect(response.ok(), await response.text()).toBe(true);
  return response;
}

test('nothing is deleted until the named confirmation is accepted', async ({ page, request }) => {
  await page.setViewportSize(VIEWPORT);

  // The id comes from the store, never from a number written into this spec.
  const frontDoor = rowFor(await readEntities(request), FRONT_DOOR);

  await page.goto('/settings/entities');
  await expect(deleteButtons(page)).toHaveCount(SEED.length);

  // Every DELETE the page issues is recorded, so "nothing was deleted" is a
  // fact about the wire and not only about the row still being painted.
  const deletes: string[] = [];
  page.on('request', (sent) => {
    if (sent.method() === 'DELETE') {
      deletes.push(sent.url());
    }
  });

  await deleteButton(page, 'Front Door').click();

  const confirmation = sheet(page);
  await expect(confirmation).toBeVisible();
  // D26/D30: at 1024x600 the confirmation is a full-screen sheet, not a desktop
  // modal — a 600px-wide dialog would report a width well under this floor.
  const box = await confirmation.boundingBox();
  if (box === null) {
    throw new Error('the confirmation sheet has no box');
  }
  expect(box.width).toBeGreaterThanOrEqual(1000);

  // The dialog names what will go: the tile's own name and the entity id.
  await expect(confirmation).toContainText('Delete “Front Door”?');
  await expect(confirmation).toContainText(frontDoor.entity_id);

  await confirmation.getByRole('button', { name: 'Cancel', exact: true }).click();

  // Cancelled: closed, no request, the row and the server untouched.
  await expect(page.locator(SHEET)).toHaveCount(0);
  await expect(editRow(page, 'Front Door')).toBeVisible();
  await expect(deleteButtons(page)).toHaveCount(SEED.length);
  expect(deletes, 'a cancelled confirmation must not delete anything').toEqual([]);
  expect(
    (await readEntities(request)).some((row) => row.id === frontDoor.id),
    'the store must still hold the row'
  ).toBe(true);

  watch.assertAllLocal();
});

test('accepting the confirmation deletes the tile from the list, the store and a fresh dashboard', async ({
  page,
  request
}) => {
  await page.setViewportSize(VIEWPORT);

  const frontDoor = rowFor(await readEntities(request), FRONT_DOOR);

  await page.goto('/settings/entities');
  await expect(deleteButtons(page)).toHaveCount(SEED.length);

  await deleteButton(page, 'Front Door').click();
  await expect(sheet(page)).toBeVisible();
  const response = await confirmDelete(page);

  // The real route, for the real row: not a POST that happens to look like one.
  expect(response.request().method()).toBe('DELETE');
  expect(new URL(response.url()).pathname).toBe(`/api/entities/${frontDoor.id}`);

  // The list refetches after the deletion, so the row and its button are gone.
  await expect(deleteButton(page, 'Front Door')).toHaveCount(0);
  await expect(editRow(page, 'Front Door')).toHaveCount(0);
  await expect(deleteButtons(page)).toHaveCount(SEED.length - 1);
  expect((await readEntities(request)).some((row) => row.id === frontDoor.id)).toBe(false);

  // A plain navigation, no reload trickery: a dashboard mounted *after* the
  // delete reads the store as it now is.
  await page.goto('/');
  await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length - 1);

  await expect(page.locator(`li.tile[data-entity="${FRONT_DOOR}"]`)).toHaveCount(0);
  for (const tile of SEED.filter((candidate) => candidate.entityId !== FRONT_DOOR)) {
    await expect(page.locator(`li.tile[data-entity="${tile.entityId}"]`)).toBeVisible();
  }

  watch.assertAllLocal();
});

test('a live delete drops the tile from the open dashboard without a reload', async ({
  page,
  request
}) => {
  await page.setViewportSize(VIEWPORT);

  await page.goto('/');
  await expect(page.locator(LIVE_TILES)).toHaveCount(SEED.length);

  // A marker on the page's own window: the only way to prove the tile left a
  // page that was never reloaded, rather than a fresh page that never had it.
  await page.evaluate(() => {
    (window as Window & { __deleteTileMarker?: string }).__deleteTileMarker = 'alive';
  });

  const frontDoor = rowFor(await readEntities(request), FRONT_DOOR);

  // The socket-driven refetch is a `GET /api/entities`; it is awaited (armed
  // first, because the frame can beat the HTTP answer) so a failure here is
  // about the refetch rather than about a timeout.
  const refetched = page.waitForResponse(
    (response) =>
      response.request().method() === 'GET' &&
      new URL(response.url()).pathname === '/api/entities'
  );
  const deleted = await request.delete(`/api/entities/${frontDoor.id}`);
  expect(deleted.ok(), await deleted.text()).toBe(true);
  await refetched;

  await expect(page.locator(`li.tile[data-entity="${FRONT_DOOR}"]`)).toHaveCount(0);
  await expect(page.locator(TILES)).toHaveCount(SEED.length - 1);
  for (const tile of SEED.filter((candidate) => candidate.entityId !== FRONT_DOOR)) {
    await expect(page.locator(`li.tile[data-entity="${tile.entityId}"]`)).toBeVisible();
  }

  const marker = await page.evaluate(
    () => (window as Window & { __deleteTileMarker?: string }).__deleteTileMarker
  );
  expect(marker, 'the dashboard must not have been reloaded').toBe('alive');

  watch.assertAllLocal();
});

test('deleting the last tile leaves a sensible empty dashboard', async ({ page, request }) => {
  await page.setViewportSize(VIEWPORT);

  const rows = await readEntities(request);
  expect(rows.length).toBe(SEED.length);

  await page.goto('/settings/entities');
  await expect(deleteButtons(page)).toHaveCount(rows.length);

  // Every row, deleted the way an owner deletes it: the list's own button and
  // the panel's own confirmation. `first()` is safe because the loop never runs
  // with the sheet open.
  let remaining = rows.length;
  for (const row of rows) {
    await deleteButtons(page).first().click();
    await expect(sheet(page), `${row.entity_id} must ask before it is deleted`).toBeVisible();
    await confirmDelete(page);
    remaining -= 1;
    await expect(deleteButtons(page)).toHaveCount(remaining);
  }

  // The list's own empty state, from the shipped component.
  await expect(page.getByRole('heading', { name: 'No entity tiles yet' })).toBeVisible();

  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'No status tiles configured' })).toBeVisible();
  await expect(
    page.getByText('Open the settings gear in the control rail to add your first tile.')
  ).toBeVisible();

  // An empty panel, not a broken one: no tiles, the way back still there, and
  // nothing shouting an error at the owner.
  await expect(page.locator(TILES)).toHaveCount(0);
  await expect(page.locator('[data-testid="rail-settings"]')).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);

  watch.assertAllLocal();
});
