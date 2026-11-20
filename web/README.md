# chime-deck-ui

The ChimeDeck web UI — Svelte 5 (runes) + Vite + TypeScript, no CSS
framework and no runtime dependencies beyond the curated `@mdi/js` icon paths.
Design tokens live in `src/styles/tokens.css`, the global base in
`src/styles/global.css`, the touch-first primitives in `src/lib/ui/`, and the
live data layer in `src/lib/`:

- `api.ts` — typed HTTP layer and the websocket frame types (one `ApiError` for
  every failure, exactly one request per call, no retry).
- `stores.svelte.ts` — `liveState`, the rune-backed singleton the UI reads:
  entities, buttons, health, connection/staleness, the press log and the
  coalesced `config_changed` refetch.
- `ws.ts` — the `/ws` client: one socket, bounded reconnect backoff, stale in
  the same tick a socket dies, and every listener removed on `stop()`.
- `StatusDots.svelte` — the header health dots and the `aria-live` stale line.
- `Dashboard.svelte`, `Tile.svelte`, `TilePopover.svelte`,
  `HeaderClock.svelte` and `VolumeSheet.svelte` — the dashboard layer: the
  selectable status tiles (a tap toggles the entity's selection, D29), the
  long-press debug card, the clock and the sound sheet.
- `tile-view.ts` — the single state→word/kind/unit rule shared by every screen
  that shows an entity (the dashboard today, the settings preview later).
- `settings.svelte.ts` — the dashboard's slice of settings (`show_clock`,
  `show_volume_control`, `dashboard_columns`, `audio.volume`), the seed the
  settings shell extends.
- `sound-library.ts` — the Sounds panel's rules: duration/source wording, the
  rescan summary line, the affected-button names and the delete-confirm copy,
  so the strings the storyboard pins are testable without a DOM.

## Build

```bash
npm ci
npm run build
```

Output lands in `web/dist/`, which is **committed to the repository** and
served by the Python app at `/` (static files + SPA fallback). End users
never need Node.js.

## Test

```bash
npm test
```

Vitest in the **node** environment, fully offline: no network, no browser, no
download. The suite replays frames recorded from the real backend
(`src/lib/fixtures/wire.ts`) through the store, drives the websocket client
against a fake socket and a hand-run clock, and checks the transport against a
loopback RFC 6455 server on `127.0.0.1` (kill → stale < 1 s → reconnect). It
also covers the HTTP layer with an injected fake `fetch`.

DOM tests opt in per file with a `// @vitest-environment happy-dom` docblock
(`src/lib/dashboard.test.ts` does this for the dashboard components); the
default stays `node`, so the suite remains fully offline with no browser.

## Verify

```bash
npm run verify
```

The full local gate: `svelte-check` (warnings are fatal), the offline WCAG
contrast gate, the gallery coverage check, `npm test`, a production build, and
the post-build dist gate.

## Develop

```bash
npm run dev
```

A component gallery lives at `/gallery` **in `npm run dev` only**. It is
dynamically imported behind `import.meta.env.DEV`, so it is dead-code
eliminated (tree-shaken) from production builds; `check:dist` enforces that
it never leaks into `dist/`.

## Reproducibility

The committed `web/dist/` artifact was built with **Node 26.12.1 / npm
12.2.1**. Rebuilding with a different Node produces a whole-directory diff
(different content hashes), so rebuilds should use the same toolchain.
`npm ci` needs the network at build time, never at test time.

## Browser verification (not part of `npm run verify`)

A separate, opt-in tier drives a real Chromium against the built artifact. It
is **not** wired into `npm run verify` or `npm test`: both stay offline and
browser-free. Everything here is localhost-only and runs offline once the
browser binary exists.

Fresh clone, in order:

```bash
npm ci                            # once, needs the network
npx playwright install chromium   # one-time; or use the system Chrome below
npm run build                     # both backends serve web/dist, the shipped artifact
uv sync                           # once, needs the network: populates ./.venv
npm run test:browser              # both projects; starts and stops both backends
npm run soak -- --minutes 6       # bounded console-error soak over the dashboard
```

The Python bench needs `uv` on `PATH` and a populated `.venv`. Playwright's
`webServer` runs it from the repository root as
`uv run --no-sync python web/browser/backend/server.py --port 4174` with
`PYTHONPATH=tests`, because the offline fakes live in `tests/`, which is not a
package. `--no-sync` is deliberate: a test run installs nothing.

`PW_CHANNEL=chrome npm run test:browser` uses the system Google Chrome instead
of the downloaded Chromium (the same env var works for `npm run soak`).

### Two backends, and which spec uses which

Playwright starts both `webServer` entries and kills both at teardown, so
neither is ever started by hand and neither outlives the run.

| project | backend | port | specs |
| --- | --- | --- | --- |
| `kiosk` | `browser/harness-server.mjs` | 4173 | `browser/kiosk.spec.ts` |
| `ui` | `browser/backend/server.py` | 4174 | `browser/app/**/*.spec.ts` |

**The Node harness, for the kiosk contract.** Its reason to exist is the
connection degradation that a live process cannot produce *per request*: an
offline `503`, a request that accepts and never answers (`hang: true`), a reload
painted from the cached snapshot, cursor idle timing, and clip sweeps over a
page the fake can reshape between requests.

**The Python bench, for everything else.** `browser/backend/server.py` boots the
real application (`create_app`) on `127.0.0.1:4174` with a temporary data
directory and the canonical seed, so those specs drive the shipped wire: the
real SQLite store, the real REST routes, the real `/ws` snapshot and
`entity_state` frames, and the real press path. Only the device edges are faked
— `FakeBroker` for MQTT, `RecordingBackend` for the speaker, and a scripted Home
Assistant peer behind the real websocket client. A spec there therefore cannot
pass against a mock of the thing it verifies, and every spec asserts that every
request stayed on `http://127.0.0.1:4174` (the machine-checked form of "no test
reaches the network").

The bench exposes three control routes on its own wrapper (never through the
app): `GET /__test/status` (the readiness gate Playwright waits on),
`POST /__test/reset` (re-apply the seed in `beforeEach`) and
`POST /__test/ha_state` (push one state through the fake HA peer and answer only
once the hub's snapshot shows it).

### Screenshot evidence

`screenshots.spec.ts` writes three images to `web/browser/artifacts/`:

- `dashboard-1024x600.png`
- `dashboard-1280x800.png`
- `dashboard-1920x1080.png`

Each test asserts the PNG's own IHDR chunk matches the viewport exactly, so a
clipped or scaled screenshot fails. The directory is **git-ignored**: it is run
output, regenerated by the suite, never an input.

### The kiosk harness's control endpoints

The harness (`browser/harness-server.mjs`) is dependency-free Node: it serves
`web/dist` with the SPA fallback, fakes the handful of `/api/*` GETs and one
`/ws` snapshot, and exposes control endpoints for the specs:

- `GET  /__control/status` — `online`, `tiles`, `chimes`, `wsConnections`,
  `openSockets`.
- `POST /__control/reset` — back to the default scenario (`online: true`,
  `tiles: 4`, `chimes: 1`, connection counter zeroed).
- `POST /__control/state` — apply `{"online"?, "hang"?, "tiles"?, "chimes"?}`;
  going offline also ends every open websocket and refuses new upgrades, and
  `hang: true` accepts `/api/*` requests but never answers them (a backend that
  hangs rather than refuses).
