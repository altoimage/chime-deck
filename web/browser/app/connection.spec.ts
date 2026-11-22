import { createHash } from 'node:crypto';
import { expect, test, type APIRequestContext, type Page, type Response } from '@playwright/test';
import {
  resetBackend,
  watchOffOriginRequests,
  type OffOriginWatch,
  type SettingsBodyShape
} from './fixtures.js';

/**
 * The Settings → Connection credential flow, end to end against the **real**
 * backend (cd-ev3m).
 *
 * Every assertion here is about one of the four defects the owner hit on the
 * production panel after revoking a leaked token: a pasted token did not replace
 * a stored one in one save, a pending "Clear stored token" destroyed the token
 * pasted after it, the Test button probed the *stored* token instead of the one
 * on screen, and the section's single Save sat below the MQTT block, far from
 * the field it commits.
 *
 * Three facts are checked for each save, the same three `settings.spec.ts`
 * checks: the PATCH body carried the right keys, the *stored row* really changed
 * (read back through the bench, never through the panel's own echo), and the
 * form came out clean — the only honest sign the echo became the new baseline.
 *
 * The stored row is proven by fingerprint: `POST /__test/ha_credentials` reports
 * a SHA-256 prefix of the stored token, so a spec can assert that the row holds
 * exactly the value it typed while no response anywhere ever carries a secret.
 *
 * Nothing here reaches the network. The bench fakes Home Assistant's websocket
 * peer *and*, since this ticket, its REST edge (`httpx.MockTransport`), because
 * the Test button is the control under test and it cannot be exercised without
 * one.
 */

/** The token the bench seeds and its scripted peer accepts. */
const ACCEPTED_TOKEN = 'long-lived-token-0123456789abcdef';

/** A token that is stored and rejected: what a revoked credential looks like. */
const REVOKED_TOKEN = 'revoked-long-lived-token-0123456789';

/** A token typed into the field that the bench was never seeded with. */
const TYPED_BAD_TOKEN = 'typed-but-revoked-token';

/** The seeded `ha.base_url`; every save echoes it back unchanged. */
const STORED_HA_URL = 'http://homeassistant.local:8123';

/** The `window` key the reload sentinel is planted under. */
const RELOAD_SENTINEL = '__connectionSpecReloadSentinel';

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

/** The body a settings PATCH answered with. */
async function patchBody(response: Response): Promise<SettingsBodyShape> {
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as SettingsBodyShape;
}

/** The Home Assistant group's own save — the label names what it commits. */
function saveHa(page: Page) {
  return page.getByRole('button', { name: 'Save Home Assistant settings', exact: true });
}

/** The MQTT group's own save — device name lives in that group (D32). */
function saveMqtt(page: Page) {
  return page.getByRole('button', { name: 'Save MQTT and device name', exact: true });
}

/** The hint paragraph under the token field: the panel's own announcement. */
function tokenHint(page: Page) {
  return page
    .locator('.field')
    .filter({ has: page.getByLabel('Long-lived token') })
    .locator('.hint');
}

/**
 * The same SHA-256 prefix `POST /__test/ha_credentials` reports, computed here.
 *
 * The bench and this file must agree about the arithmetic, not about a literal:
 * a spec that hard-coded the digest would keep passing after a token change.
 *
 * @param token the plaintext token whose fingerprint is wanted.
 */
function fingerprintOf(token: string): string {
  return createHash('sha256').update(token).digest('hex').slice(0, 12);
}

/** What `POST /__test/ha_credentials` answers with. */
interface HaCredentialsState {
  readonly base_url: string;
  readonly token_stored: boolean;
  /** The live `ha` health word — `error` after a rejected token, else `connected`. */
  readonly ha: string;
  /** The stored token's SHA-256 prefix, or `null` when nothing is stored. */
  readonly token_fingerprint: string | null;
}

/**
 * Write and/or read the stored Home Assistant credentials through the bench.
 *
 * An empty `body` is a pure read: the route writes only the keys it was handed,
 * so a read can never disturb the row it is asserting.
 *
 * @param request the test's API request context.
 * @param body `base_url` / `token` to write, or `{}` to read.
 */
async function haCredentials(
  request: APIRequestContext,
  body: { base_url?: string; token?: string | null }
): Promise<HaCredentialsState> {
  const response = await request.post('/__test/ha_credentials', { data: body });
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()) as HaCredentialsState;
}

/**
 * Every JSON body the page sent to `pathname`, in order, from
 * `page.on('request')`.
 *
 * Reading the outgoing request rather than the response echo is what makes the
 * assertions decisive: a response only echoes what the *server* stored, so it
 * cannot prove the browser sent the pasted token in the first place.
 *
 * @param page the page whose requests are recorded.
 * @param pathname the exact path to record, e.g. `/api/settings`.
 */
function captureRequests(
  page: Page,
  pathname: string
): Array<Record<string, unknown> | null> {
  const bodies: Array<Record<string, unknown> | null> = [];
  page.on('request', (request) => {
    if (new URL(request.url()).pathname !== pathname) {
      return;
    }
    const raw = request.postData();
    if (raw === null) {
      // No body at all is itself the finding in test 3: record it, do not skip it.
      bodies.push(null);
      return;
    }
    try {
      bodies.push(JSON.parse(raw) as Record<string, unknown>);
    } catch {
      bodies.push({ unparsedBody: raw });
    }
  });
  return bodies;
}

/** The `/api/` response bodies a page received, in arrival order. */
interface ApiBodyCapture {
  readonly bodies: string[];
  /** Resolve once every captured body has been read; call it before asserting. */
  settled(): Promise<void>;
}

/**
 * Record the text of every `/api/` response the page receives.
 *
 * Used for the leak check: a token that must never come back must be searched
 * for in *every* answer, not just the one whose shape this file knows about.
 *
 * @param page the page whose responses are recorded.
 */
function captureApiBodies(page: Page): ApiBodyCapture {
  const bodies: string[] = [];
  const pending: Array<Promise<void>> = [];
  page.on('response', (response) => {
    if (!new URL(response.url()).pathname.startsWith('/api/')) {
      return;
    }
    pending.push(
      response
        .text()
        .then((body) => {
          bodies.push(body);
        })
        .catch(() => {
          // A response the browser discarded has no body to leak.
        })
    );
  });
  return {
    bodies,
    settled: async () => {
      // Indexed rather than for-of: a response can still arrive while waiting.
      for (let index = 0; index < pending.length; index += 1) {
        await pending[index];
      }
    }
  };
}

/** What `POST /api/settings/test/ha` answers with. */
interface HaTestBody {
  readonly ok: boolean;
  readonly ha_version: string | null;
  readonly entity_count: number | null;
  readonly error: string | null;
}

/**
 * Press the Home Assistant Test button once and return the probe's own answer.
 *
 * A failed test is HTTP 200 with `ok: false` (the request succeeded; `ok` carries
 * the verdict), so this asserts the status and parses the body rather than
 * treating a failed probe as a failed request.
 *
 * @param page the page holding the Connection panel.
 */
async function pressTest(page: Page): Promise<HaTestBody> {
  const answered = page.waitForResponse(
    (response) =>
      response.url().endsWith('/api/settings/test/ha') &&
      response.request().method() === 'POST'
  );
  await page.getByRole('button', { name: 'Test connection', exact: true }).click();
  const response = await answered;
  expect(response.status()).toBe(200);
  return (await response.json()) as HaTestBody;
}

test('a pasted token replaces the stored one in one save and the panel reconnects without a reload', async ({
  page,
  request
}) => {
  // Seed the state the owner started from: a stored token Home Assistant has
  // revoked. `error` is the websocket client's own terminal auth verdict.
  const seeded = await haCredentials(request, { token: REVOKED_TOKEN });
  expect(seeded.token_stored).toBe(true);
  await expect.poll(async () => (await haCredentials(request, {})).ha).toBe('error');

  await page.goto('/settings/connection');

  // A reload sentinel: the client rebuild must happen inside the running page,
  // so a browser reload would silently "fix" the panel and hide the real work.
  await page.evaluate((key) => {
    (window as unknown as Record<string, unknown>)[key] = 'planted';
  }, RELOAD_SENTINEL);

  const token = page.getByLabel('Long-lived token');
  await token.fill(ACCEPTED_TOKEN);

  // The button's own state is the dirty indicator: pasting is what enables it.
  await expect(saveHa(page)).toBeEnabled();

  const sentBodies = captureRequests(page, '/api/settings');
  // Armed before the click: the response may arrive before the click resolves.
  const patched = page.waitForResponse(isSettingsPatch);
  await saveHa(page).click();
  const body = await patchBody(await patched);

  // (a) the echo of this save…
  expect(body.settings['ha.base_url']).toBe(STORED_HA_URL);
  expect(body.secrets['ha.token']).toBe(true);
  // …(b) what the browser actually sent, which is the decisive fact: one save,
  // carrying the pasted value rather than a clear.
  expect(sentBodies).toHaveLength(1);
  expect(sentBodies[0]).toEqual({ 'ha.token': ACCEPTED_TOKEN });
  // …(c) the stored row really changed — proven by fingerprint, so this
  // assertion never needs a response to carry the secret.
  await expect
    .poll(async () => (await haCredentials(request, {})).token_fingerprint)
    .toBe(fingerprintOf(ACCEPTED_TOKEN));

  // The echo became the baseline: the field is empty again and Save is clean.
  await expect(token).toHaveValue('');
  await expect(saveHa(page)).toBeDisabled();

  // No browser reload happened.
  expect(
    await page.evaluate((key) => (window as unknown as Record<string, unknown>)[key], RELOAD_SENTINEL)
  ).toBe('planted');

  // The running websocket client was rebuilt with the new credential, and the
  // scripted peer accepted it — no restart, no reload.
  await expect.poll(async () => (await haCredentials(request, {})).ha).toBe('connected');

  watch.assertAllLocal();
});

/**
 * The regression receipt for the root cause.
 *
 * Owner, 2026-11-20: "I had to click the clear token stored button, then I had
 * to hit save then I had to paste in the new one then hit save and then it
 * worked. ... pasting it in and hitting save didn't overrite the existing one
 * correctly."
 *
 * Old behaviour: `Clear stored token` set a `clearToken` draft flag that nothing
 * could unset, and the diff builder let a pending clear win over the draft for
 * ever. So the paste was ignored — the PATCH carried `{"ha.token":null}`, the
 * stored token was destroyed, and the hint kept announcing a clear, which is why
 * the only working sequence was clear → save → paste → save.
 */
test('a pending clear does not throw away the token pasted after it', async ({ page, request }) => {
  await haCredentials(request, { token: REVOKED_TOKEN });

  await page.goto('/settings/connection');
  const token = page.getByLabel('Long-lived token');
  const hint = tokenHint(page);
  await expect(hint).toContainText('replace');

  await page.getByRole('button', { name: 'Clear stored token', exact: true }).click();
  await expect(hint).toHaveText('The stored token will be cleared when you save.');

  await token.fill(ACCEPTED_TOKEN);

  // The point of the test: the hint must stop claiming the clear will win, because
  // the user has just given the panel something better to save.
  await expect(hint).not.toContainText('will be cleared');
  await expect(hint).toContainText('replace');

  const sentBodies = captureRequests(page, '/api/settings');
  const patched = page.waitForResponse(isSettingsPatch);
  await saveHa(page).click();
  await patchBody(await patched);

  expect(sentBodies).toHaveLength(1);
  // Not `null`: a pending clear must lose to a typed replacement.
  expect(sentBodies[0]).toEqual({ 'ha.token': ACCEPTED_TOKEN });

  await expect
    .poll(async () => (await haCredentials(request, {})).token_fingerprint)
    .toBe(fingerprintOf(ACCEPTED_TOKEN));
  expect((await haCredentials(request, {})).token_stored).toBe(true);
  await expect.poll(async () => (await haCredentials(request, {})).ha).toBe('connected');

  watch.assertAllLocal();
});

test('Test probes the typed token without saving it, and the token never comes back', async ({
  page,
  request
}) => {
  // The STORED credentials are good, so any failure below can only come from the
  // token the field holds — which is exactly the defect: the probe used to test
  // the stored one and report success whatever was typed.
  await haCredentials(request, { token: ACCEPTED_TOKEN });
  await expect.poll(async () => (await haCredentials(request, {})).ha).toBe('connected');
  const fingerprintBefore = (await haCredentials(request, {})).token_fingerprint;

  await page.goto('/settings/connection');
  const token = page.getByLabel('Long-lived token');
  await token.fill(TYPED_BAD_TOKEN);

  const probeBodies = captureRequests(page, '/api/settings/test/ha');
  const apiBodies = captureApiBodies(page);

  const rejected = await pressTest(page);

  // The typed token travelled with the probe…
  expect(probeBodies).toHaveLength(1);
  expect(probeBodies[0]).toMatchObject({ token: TYPED_BAD_TOKEN });
  // …and it is the one that was tested: a revoked token fails even though the
  // stored one is good.
  expect(rejected.ok).toBe(false);
  expect(rejected.error).toContain('Home Assistant rejected the token');
  await expect(page.getByText(/Home Assistant rejected the token/)).toBeVisible();
  await expect(page.getByText('Not connected', { exact: true })).toBeVisible();

  // A test is a probe, never a save.
  await apiBodies.settled();
  expect((await haCredentials(request, {})).token_fingerprint).toBe(fingerprintBefore);
  expect((await haCredentials(request, {})).token_stored).toBe(true);

  // The same button, a token the peer accepts: success with a real count.
  await token.fill('');
  await token.fill(ACCEPTED_TOKEN);
  const accepted = await pressTest(page);

  expect(accepted.ok).toBe(true);
  expect(accepted.entity_count).toBeGreaterThan(0);
  // This build deliberately does not read `/api/config` for the version, so the
  // honest assertion is the count and the summary — never a version string.
  expect(accepted.ha_version).toBeNull();
  const summary = page.getByText('Connected to Home Assistant', { exact: false });
  await expect(summary).toBeVisible();
  await expect(summary).toContainText(`${accepted.entity_count} entities`);
  await expect(summary).toContainText('the version was not reported');

  // The secret travels one way only: no response the page received — neither
  // probe answer, nor a settings echo — ever carried either token.
  await apiBodies.settled();
  expect(apiBodies.bodies.length).toBeGreaterThan(0);
  const leaked = apiBodies.bodies.filter(
    (body) => body.includes(TYPED_BAD_TOKEN) || body.includes(ACCEPTED_TOKEN)
  );
  expect(leaked, 'no response body may carry a token').toEqual([]);

  watch.assertAllLocal();
});

test('each group saves only its own keys and each save sits with its fields', async ({
  page,
  request
}) => {
  await haCredentials(request, { token: ACCEPTED_TOKEN });
  // The wall panel's own size: "reachable without scrolling" is measured here.
  await page.setViewportSize({ width: 1024, height: 600 });
  await page.goto('/settings/connection');

  const token = page.getByLabel('Long-lived token');
  await expect(token).toHaveCount(1);
  await expect(page.getByLabel('Broker host')).toHaveCount(1);
  await expect(saveHa(page)).toHaveCount(1);
  await expect(saveMqtt(page)).toHaveCount(1);
  // The old single footer Save is gone: one action per group, and every one of
  // them names what it commits.
  await expect(page.getByRole('button', { name: 'Save', exact: true })).toHaveCount(0);
  await expect(saveHa(page)).toBeInViewport();

  await page.getByLabel('Broker host').fill('group-test-broker.local');
  await token.fill('typed-token-for-the-mqtt-group-test');

  const sentBodies = captureRequests(page, '/api/settings');

  const mqttPatched = page.waitForResponse(isSettingsPatch);
  await saveMqtt(page).click();
  await patchBody(await mqttPatched);

  // Exactly one key: this button commits the MQTT group and nothing else.
  expect(sentBodies).toHaveLength(1);
  expect(sentBodies[0]).toEqual({ 'mqtt.host': 'group-test-broker.local' });
  // A cross-group save must not discard unsaved typing in the other group.
  await expect(token).toHaveValue('typed-token-for-the-mqtt-group-test');

  const haPatched = page.waitForResponse(isSettingsPatch);
  await saveHa(page).click();
  await patchBody(await haPatched);

  expect(sentBodies).toHaveLength(2);
  expect(sentBodies[1]).toEqual({ 'ha.token': 'typed-token-for-the-mqtt-group-test' });

  // Validation is per group: an impossible broker port disables the MQTT save
  // and leaves the Home Assistant save alone. A fresh token keeps that group
  // dirty, so the contrast is about the port and not about both buttons being
  // disabled for unrelated reasons.
  await token.fill('second-typed-token');
  await page.getByLabel('Broker port').fill('0');
  await expect(saveMqtt(page)).toBeDisabled();
  await expect(saveHa(page)).toBeEnabled();

  watch.assertAllLocal();
});
