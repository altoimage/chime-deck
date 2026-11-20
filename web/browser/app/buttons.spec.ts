import { expect, test } from '@playwright/test';
import {
  CHIME_BUTTON_NAME,
  readLatestPress,
  resetBackend,
  watchOffOriginRequests,
  type OffOriginWatch
} from './fixtures.js';

/**
 * A button Test against the **real** press path.
 *
 * `POST /api/buttons/{id}/press` is the route MQTT presses take too, so this is
 * the shipped press path end to end (press service → dispatch queue → playback
 * worker → `press_log` → the panel's report). The bench injects
 * `RecordingBackend`, so the queue records the playback instead of opening an
 * audio device: **no speaker is touched and nothing is audible** in this test —
 * which is exactly the honesty boundary, because the figure the panel prints is
 * *round-trip latency*, not playback time, and the panel says so in words.
 */

/** `PressResponse`, as this tier reads it. */
interface PressBody {
  readonly button_id: number | null;
  readonly button_name: string;
  readonly source: string;
  readonly outcome: string;
  readonly pressed_at: string;
}

/** The one outcome word a successful dispatch is labelled with (`press-report.ts`). */
const QUEUED_LABEL = 'Queued for playback';

/** The off-origin watch the `beforeEach` attached; asserted at the end of each test. */
let watch: OffOriginWatch;

test.beforeEach(async ({ page, request }) => {
  await resetBackend(request);
  watch = watchOffOriginRequests(page);
});

test('a Test press reports a real round trip and lands in the press log', async ({
  page,
  request
}) => {
  const before = await readLatestPress(request);

  await page.goto('/settings/buttons');
  const testButton = page.getByRole('button', { name: `Test ${CHIME_BUTTON_NAME}` });
  await expect(testButton).toBeVisible();

  const pressed = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' && response.url().endsWith('/press')
  );
  await testButton.click();
  const answer = await pressed;
  expect(answer.ok(), await answer.text()).toBe(true);
  const press = (await answer.json()) as PressBody;

  // The panel measures the request itself: a finite, non-negative number of
  // milliseconds, printed whole and carried unrounded in `data-round-trip`.
  const latency = page.locator('[data-round-trip]');
  await expect(latency).toBeVisible();
  const measured = Number(await latency.getAttribute('data-round-trip'));
  expect(Number.isFinite(measured)).toBe(true);
  expect(measured).toBeGreaterThanOrEqual(0);
  await expect(latency).toHaveText(/Round-trip latency: \d+ ms/);

  // The sentence that stops "42 ms" being read as the chime's playback time.
  await expect(
    page.getByText(
      'Round-trip latency is how long the press request took; it is not playback time.'
    )
  ).toBeVisible();

  // The badge is the outcome the real press path answered with.
  expect(press.outcome).toBe('queued');
  await expect(page.locator('.headline .badge')).toHaveText(QUEUED_LABEL);

  // The log row is this very press: same button, same canonical timestamp (a
  // new row, not the one that was there before).
  const after = await readLatestPress(request);
  if (after === null) {
    throw new Error('the press must be logged');
  }
  expect(after.id).not.toBe(before?.id);
  expect(after.source).toBe('ui_test');
  expect(after.button_name).toBe(CHIME_BUTTON_NAME);
  expect(after.button_id).toBe(press.button_id);
  expect(after.pressed_at).toBe(press.pressed_at);
  // A `queued` row is revised to `played` by the playback worker once the fake
  // backend returns, so either word describes this press; anything else would
  // mean the log disagrees with the press that was answered.
  expect(['queued', 'played']).toContain(after.outcome);

  watch.assertAllLocal();
});
