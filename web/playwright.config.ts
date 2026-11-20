import { defineConfig } from '@playwright/test';

/**
 * The real-browser verification tier.
 *
 * Deliberately separate from Vitest (`npm test`) and from `npm run verify`:
 * these specs launch a browser and need the built `web/dist` artifact, while
 * the offline suite must stay green without either. One worker, no parallelism:
 * the harness has process-global state (online flag, tile/chime counts).
 *
 * **Two backends, and each project names which spec uses which.**
 *
 * - `kiosk` → `browser/harness-server.mjs` on port 4173, a deliberately thin
 *   Node fake. Its reason to exist is the connection degradation a live process
 *   cannot produce *per request*: an offline `503`, a request that accepts and
 *   never answers, a reload painted from the cached snapshot, cursor idle
 *   timing, and clip sweeps over a page the fake can reshape at will.
 * - `ui` → `browser/backend/server.py` on port 4174, the shipped wire: the real
 *   FastAPI app with the real store, the real REST routes, the real `/ws`
 *   snapshot and `entity_state` frames, and the real press path. Only the device
 *   edges are faked (MQTT broker, audio backend, the Home Assistant peer), so a
 *   spec on this project cannot pass against a mock of the thing it verifies.
 *
 * `channel` is unset by default (the downloaded Chromium), and `PW_CHANNEL`
 * selects a system browser instead (`PW_CHANNEL=chrome`).
 */
export default defineConfig({
  testDir: './browser',
  testMatch: '**/*.spec.ts',
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  timeout: 60_000,
  /** A whole run is bounded too, so one wedged bench cannot hang the tier. */
  globalTimeout: 300_000,
  expect: { timeout: 10_000 },
  reporter: [['list']],
  use: {
    headless: true,
    viewport: { width: 1024, height: 600 },
    trace: 'off',
    video: 'off',
    screenshot: 'off',
    channel: process.env.PW_CHANNEL || undefined
  },
  projects: [
    {
      name: 'kiosk',
      testMatch: 'kiosk.spec.ts',
      use: { baseURL: 'http://127.0.0.1:4173' }
    },
    {
      name: 'ui',
      testMatch: 'app/**/*.spec.ts',
      use: { baseURL: 'http://127.0.0.1:4174' }
    }
  ],
  webServer: [
    {
      command: 'node browser/harness-server.mjs --port 4173',
      url: 'http://127.0.0.1:4173/__control/status',
      reuseExistingServer: false,
      timeout: 30_000,
      stdout: 'pipe',
      stderr: 'pipe'
    },
    {
      // The real backend bench: `cwd: '..'` is the repository root, where `uv`
      // finds the project's own `.venv` (run `uv sync` once), and
      // `PYTHONPATH=tests` is what makes the bench's plain `chime_fakes` /
      // `ha_ws_fakes` / `mqtt_fakes` imports work — `tests/` is not a package.
      command: 'uv run --no-sync python web/browser/backend/server.py --port 4174',
      cwd: '..',
      env: { PYTHONPATH: 'tests' },
      url: 'http://127.0.0.1:4174/__test/status',
      reuseExistingServer: false,
      timeout: 90_000,
      stdout: 'pipe',
      stderr: 'pipe'
    }
  ]
});
