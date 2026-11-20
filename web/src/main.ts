import { mount } from 'svelte';
import App from './App.svelte';
import AuthGate from './lib/AuthGate.svelte';
import { setUnauthorizedHandler } from './lib/api.js';
import { auth } from './lib/auth.svelte.js';
import SettingsApp from './lib/settings/SettingsApp.svelte';
import SetupApp from './lib/settings/SetupApp.svelte';
import { isSettingsPath } from './lib/settings/sections.js';
import { settings } from './lib/settings.svelte.js';
import { isSetupPath, setupRedirectAfterLoad, setupStateFrom } from './lib/setup.js';
import { applyKioskFlags, isKiosk } from './lib/kiosk.js';
import { readSnapshotCache } from './lib/snapshot-cache.js';
import { liveState } from './lib/stores.svelte.js';
import './styles/tokens.css';
import './styles/global.css';

/** How long a cached dashboard waits for settings before painting anyway. */
const SETTINGS_PAINT_GRACE_MS = 1500;

/** A one-shot bound for `Promise.race`; it never blocks the panel for long. */
function settleAfter(ms: number): Promise<void> {
  return new Promise<void>((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function main(): Promise<void> {
  const target = document.getElementById('app');
  if (!target) {
    throw new Error('#app mount target missing from index.html');
  }

  // Dev-only gallery branch. import.meta.env.DEV is statically replaced, so
  // Rolldown dead-code-eliminates this dynamic import (and its chunk) from
  // production builds; check:dist asserts the gallery sentinel and any
  // Gallery* chunk are absent from dist/.
  if (import.meta.env.DEV && window.location.pathname === '/gallery') {
    const { default: Gallery } = await import('./routes/Gallery.svelte');
    mount(Gallery, { target });
    return;
  }

  // Stamp the layout flags before any root mounts: the global stylesheet uses
  // `html[data-layout]` and `html[data-kiosk]` from the very first paint.
  // main.ts only ever runs in a browser, so `document` and `window` are safe.
  const kiosk = isKiosk();
  const dashboard = window.location.pathname === '/';
  applyKioskFlags(document.documentElement, { kiosk, dashboard });

  // One overlay, mounted the first time any call is refused, instead of a
  // login branch in each of the roots below: the dashboard, the wizard and the
  // settings shell each make their own first request, and this single hook
  // covers all of them. It is lazy on purpose — an install with no
  // `server.token` never creates the node, so it gains no DOM and no behaviour —
  // and it mounts into its own body-level `#auth-gate`, never into `#app`,
  // which the four roots own. The gate raises the prompt itself through
  // `auth`, so this only has to create the host once.
  let authGate: HTMLElement | null = null;
  setUnauthorizedHandler(() => {
    auth.promptRequired();
    if (authGate !== null) {
      return;
    }
    authGate = document.createElement('div');
    authGate.id = 'auth-gate';
    document.body.append(authGate);
    mount(AuthGate, { target: authGate });
  });

  // On the dashboard route, paint the last-known snapshot before the socket
  // connects so a reload with no network shows yesterday's tiles (marked
  // stale) instead of an empty screen. A blocked localStorage just means live
  // data only; `restoreCached` marks the store stale, and only `applySnapshot`
  // clears that. This runs before `mount(App)` below.
  let restored = false;
  if (dashboard) {
    try {
      const cached = readSnapshotCache(window.localStorage, Date.now());
      if (cached !== null) {
        liveState.restoreCached(cached);
        restored = true;
      }
    } catch {
      // A denied localStorage is not a reason to block the dashboard.
    }
  }

  // The wizard is its own root as well, for the same reason the settings shell
  // is (below): the dashboard's socket, stores and refresh wiring must not
  // start just to fill in a form — and this is the route a phone reaches first.
  if (isSetupPath(window.location.pathname)) {
    mount(SetupApp, { target });
    return;
  }

  // The settings shell is its own root: the dashboard's socket, stores and
  // refresh wiring must not start just to edit a form. The backend serves
  // index.html for every path (SPA fallback), so a reload lands right here.
  if (isSettingsPath(window.location.pathname)) {
    mount(SettingsApp, { target });
    return;
  }

  // First run: decide before the dashboard paints, so a fresh install never
  // flashes an empty screen. This is the same GET the dashboard needs anyway;
  // a failed load leaves `loaded` false (App retries) and never redirects.
  //
  // With a cached snapshot there is already something honest to paint, so the
  // wait is bounded: an unreachable server that hangs the request must not
  // leave a blank panel. A timed-out load leaves `loaded` false, which skips
  // the setup redirect (nothing has been proven about the configuration) and
  // makes App retry the load — the cached tiles stay up in the meantime.
  if (restored) {
    await Promise.race([settings.load(), settleAfter(SETTINGS_PAINT_GRACE_MS)]);
  } else {
    await settings.load();
  }
  const redirect = setupRedirectAfterLoad(
    settings.loaded,
    window.location.pathname,
    setupStateFrom(settings)
  );
  if (redirect !== null) {
    window.location.replace(redirect);
    return;
  }

  mount(App, { target });
}

void main();
