#!/usr/bin/env node
/**
 * A bounded, repeatable console-error soak for the kiosk dashboard
 * (HSD-0qtn phase 2).
 *
 * Runs the harness in-process, drives one real Chromium page against it and
 * records everything a wall panel must never do: console errors/warnings,
 * uncaught page errors, failed requests and websocket flapping. Heap growth is
 * sampled over CDP and reported, never asserted — a leak is a human reading of
 * the numbers, not a flaky threshold.
 *
 *   node browser/soak.mjs --minutes 6 --interval-s 30
 *
 * Bounded three ways: the loop stops at `now + minutes`; every wait is clamped
 * to the remaining time; and a watchdog at `minutes + 60 s` exits 2 if anything
 * hangs. The browser and the harness are closed in `finally`, independently, so
 * one failing close cannot skip the other.
 */

import { chromium } from '@playwright/test';
import { createHarnessServer } from './harness-server.mjs';

/** `--name <number>` from argv, or the fallback. */
function readNumberArg(name, fallback) {
  const index = process.argv.indexOf(name);
  if (index === -1) {
    return fallback;
  }
  const value = Number(process.argv[index + 1]);
  return Number.isFinite(value) ? value : fallback;
}

const minutesRequested = Math.max(0, readNumberArg('--minutes', 30));
const intervalS = Math.max(1, readNumberArg('--interval-s', 30));
const portArg = Math.max(0, readNumberArg('--port', 0));
const tiles = Math.max(1, Math.floor(readNumberArg('--tiles', 4)));

const durationMs = minutesRequested * 60_000;
const intervalMs = intervalS * 1000;
const startedAt = Date.now();
const deadline = startedAt + durationMs;

const consoleErrors = [];
const consoleWarnings = [];
const pageErrors = [];
const requestFailures = [];
let websocketOpens = 0;
let websocketCloses = 0;
let heapStartMiB = null;
let heapEndMiB = null;
let harnessFailure = null;

const watchdog = setTimeout(() => {
  process.stdout.write(
    `${JSON.stringify({ watchdog: true, minutesRequested, note: 'soak exceeded its bound' })}\n`
  );
  process.exit(2);
}, durationMs + 60_000);

const harness = createHarnessServer();
let browser = null;

try {
  harness.state.tiles = tiles;
  const port = await harness.listen(portArg);
  const origin = `http://127.0.0.1:${port}`;

  browser = await chromium.launch({
    channel: process.env.PW_CHANNEL || undefined,
    headless: true
  });
  const context = await browser.newContext({ viewport: { width: 1024, height: 600 } });
  const page = await context.newPage();

  page.on('console', (message) => {
    const entry = {
      type: message.type(),
      text: message.text(),
      location: message.location()
    };
    if (message.type() === 'error') {
      consoleErrors.push(entry);
    } else if (message.type() === 'warning') {
      consoleWarnings.push(entry);
    }
  });
  page.on('pageerror', (error) => {
    pageErrors.push({ name: error.name, message: error.message });
  });
  page.on('websocket', (socket) => {
    websocketOpens += 1;
    socket.on('close', () => {
      websocketCloses += 1;
    });
  });
  page.on('requestfailed', (request) => {
    requestFailures.push({
      url: request.url(),
      errorText: request.failure()?.errorText ?? ''
    });
  });

  await page.goto(`${origin}/?kiosk=1`, { waitUntil: 'domcontentloaded' });
  await page
    .locator('li.tile[data-stale="false"]')
    .first()
    .waitFor({ state: 'attached', timeout: 15_000 });

  const cdp = await context.newCDPSession(page);
  await cdp.send('Performance.enable');
  const sampleHeap = async () => {
    const { metrics } = await cdp.send('Performance.getMetrics');
    const metric = metrics.find((entry) => entry.name === 'JSHeapUsedSize');
    return metric === undefined ? null : metric.value / (1024 * 1024);
  };

  heapStartMiB = await sampleHeap();
  heapEndMiB = heapStartMiB;

  while (Date.now() < deadline) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      break;
    }
    await page.waitForTimeout(Math.min(intervalMs, remaining));
    heapEndMiB = await sampleHeap();
    const elapsed = Math.round((Date.now() - startedAt) / 1000);
    const heap = heapEndMiB === null ? 'n/a' : heapEndMiB.toFixed(1);
    process.stdout.write(
      `t+${elapsed}s heap=${heap}MiB reconnects=${Math.max(0, websocketOpens - 1)} ` +
        `errors=${consoleErrors.length} warnings=${consoleWarnings.length}\n`
    );
  }
} catch (error) {
  harnessFailure = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
} finally {
  clearTimeout(watchdog);

  try {
    if (browser !== null) {
      await browser.close();
    }
  } catch {
    // The report below is still worth printing.
  }
  try {
    await harness.close();
  } catch {
    // Same: the harness close is best-effort on the way out.
  }

  const minutesElapsed = (Date.now() - startedAt) / 60_000;
  const result = {
    minutesRequested,
    minutesElapsed: Number(minutesElapsed.toFixed(3)),
    consoleErrors,
    consoleWarnings,
    pageErrors,
    requestFailures,
    websocketOpens,
    websocketCloses,
    heapStartMiB: heapStartMiB === null ? null : Number(heapStartMiB.toFixed(2)),
    heapEndMiB: heapEndMiB === null ? null : Number(heapEndMiB.toFixed(2))
  };
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);

  const failed =
    harnessFailure !== null ||
    consoleErrors.length > 0 ||
    consoleWarnings.length > 0 ||
    pageErrors.length > 0 ||
    requestFailures.length > 0 ||
    websocketOpens > 1;
  if (harnessFailure !== null) {
    process.stdout.write(`soak failed to run: ${harnessFailure}\n`);
  }
  process.exit(failed ? 1 : 0);
}
