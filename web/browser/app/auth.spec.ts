import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import {
  BENCH_ORIGIN,
  SEED,
  readSettings,
  resetBackend,
  watchOffOriginRequests,
  type OffOriginWatch
} from './fixtures.js';

/**
 * The shared-token prompt against the **real** backend (HSD-0jke).
 *
 * The gate is switched on per test through the bench's `POST /__test/token`,
 * which writes the live `server.token`, so the `401` the UI reacts to is the
 * middleware's own and the cookie comes from the app's own login route — a spec
 * here cannot pass against a mock of the thing it verifies.
 *
 * Every test clears the token and the browser's cookies again in `afterEach`:
 * the ten-plus other `app/*.spec.ts` files share this one bench process, and a
 * token left behind would turn all of them into `401`s.
 */

/** The token each test configures; only its presence in `server.token` matters. */
const TOKEN = 'bench-shared-token-0123456789abcdef';

/** The one cookie the login route issues (`chime_deck.auth.COOKIE_NAME`). */
const SESSION_COOKIE = 'doorbell_session';

/** The prompt's mount point; absent entirely while the gate is off. */
const GATE = '#auth-gate';

/** Every live tile on the dashboard. */
const TILES = 'ul.tiles li.tile';

/** The off-origin watch the `beforeEach` attached; asserted at the end of each test. */
let watch: OffOriginWatch;

/** The prompt's token field. */
function tokenInput(page: Page) {
  return page.getByLabel('Shared token');
}

/** The prompt's one submit button. */
function unlockButton(page: Page) {
  return page.getByRole('button', { name: 'Unlock doorbell' });
}

/**
 * Set (or clear, with an empty string) `server.token` through the bench's live
 * store and assert the gate reports the state the test asked for.
 *
 * @param request the test's API request context.
 * @param token the token to configure; `''` turns the gate off.
 */
async function setToken(request: APIRequestContext, token: string): Promise<void> {
  const response = await request.post('/__test/token', { data: { token } });
  expect(response.ok(), await response.text()).toBe(true);
  const body = (await response.json()) as { authenticated: boolean };
  expect(body.authenticated).toBe(token.length > 0);
}

test.beforeEach(async ({ page, request }) => {
  await resetBackend(request);
  watch = watchOffOriginRequests(page);
});

test.afterEach(async ({ request, context }) => {
  await setToken(request, '');
  await context.clearCookies();
  // The clear really turned the gate off: this read is a 401 while the token is
  // still configured, so a failed cleanup fails here instead of in the next spec.
  await readSettings(request);
});

test('the prompt appears and the shared token unlocks the UI', async ({ page, request }) => {
  await setToken(request, TOKEN);
  await page.goto('/');

  const input = tokenInput(page);
  await expect(input).toBeVisible();
  // Scoped to the overlay: the dashboard behind it has an `h1` of its own.
  await expect(page.locator(GATE).getByRole('heading', { name: 'ChimeDeck' })).toBeVisible();

  // The kiosk target rule (D26) covers this prompt too: one assertion over both
  // controls, a 48 px minimum height each.
  const heights = await Promise.all(
    [input, unlockButton(page)].map(async (control) => (await control.boundingBox())?.height ?? 0)
  );
  expect(heights.every((height) => height >= 48)).toBe(true);

  // Armed before the click: the reload may land before the click resolves.
  const reloaded = page.waitForEvent('load');
  await input.fill(TOKEN);
  await unlockButton(page).click();
  await reloaded;

  // The cookie is now in the browser, so the reloaded app reads the real store.
  await expect(page.locator(TILES)).toHaveCount(SEED.length);
  await expect(page.locator(GATE)).toHaveCount(0);
  await expect(input).toHaveCount(0);

  watch.assertAllLocal();
});

test('a wrong token announces the refusal and sends exactly one login request', async ({
  page,
  request
}) => {
  await setToken(request, TOKEN);

  const loginPosts: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/auth/login') {
      loginPosts.push(request.url());
    }
  });

  await page.goto('/');
  const input = tokenInput(page);
  await expect(input).toBeVisible();

  await input.fill('not-the-shared-token');
  await unlockButton(page).click();

  await expect(page.getByRole('alert')).toHaveText('That token was not accepted.');
  // A short bounded settle: an automatic retry would land another POST here.
  await page.waitForTimeout(500);
  expect(loginPosts).toHaveLength(1);
  await expect(input).toBeVisible();

  watch.assertAllLocal();
});

/**
 * The kiosk clause: the login cookie survives a page reload **and a fresh
 * browser context** — exactly those two, which is what a wall panel and a new
 * tab need. It is not a claim about a literal browser restart: the cookie lives
 * in the browser profile, and nothing here kills a browser.
 *
 * The cookie is obtained honestly from the real `POST /api/auth/login` through
 * Playwright's API request context, never fabricated: a made-up value would only
 * prove that the gate accepts anything.
 */
test('the login cookie survives a reload and a fresh browser context', async ({
  page,
  request,
  browser,
  context
}) => {
  await setToken(request, TOKEN);

  const login = await request.post(`${BENCH_ORIGIN}/api/auth/login`, { data: { token: TOKEN } });
  expect(login.ok(), await login.text()).toBe(true);
  const [pair] = (login.headers()['set-cookie'] ?? '').split(';');
  const separator = pair.indexOf('=');
  expect(separator).toBeGreaterThan(0);
  const cookie = {
    name: pair.slice(0, separator),
    value: pair.slice(separator + 1),
    url: BENCH_ORIGIN
  };
  expect(cookie.name).toBe(SESSION_COOKIE);

  // (1) a reload of this page: same browser context, cookie already attached.
  await context.addCookies([cookie]);
  await page.goto('/');
  await expect(page.locator(TILES)).toHaveCount(SEED.length);
  await expect(page.locator(GATE)).toHaveCount(0);

  // (2) a brand-new context with the same cookie value: no other shared state.
  const fresh = await browser.newContext({ baseURL: BENCH_ORIGIN });
  try {
    await fresh.addCookies([cookie]);
    const freshPage = await fresh.newPage();
    await freshPage.goto('/');
    await expect(freshPage.locator(TILES)).toHaveCount(SEED.length);
    await expect(freshPage.locator(GATE)).toHaveCount(0);
  } finally {
    await fresh.close();
  }

  watch.assertAllLocal();
});
