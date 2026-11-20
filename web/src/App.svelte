<script lang="ts">
  import { onDestroy, untrack } from 'svelte';
  import { listButtons, listEntities } from './lib/api.js';
  import { isKiosk } from './lib/kiosk.js';
  import { KioskChrome } from './lib/kiosk-chrome.js';
  import { createCacheWriter, type CacheWriter } from './lib/snapshot-cache.js';
  import type { StorageLike } from './lib/kiosk.js';
  import { settings } from './lib/settings.svelte.js';
  import { setupOutcome } from './lib/setup.js';
  import { liveState } from './lib/stores.svelte.js';
  import { theme } from './lib/theme.svelte.js';
  import { stripParam } from './lib/url.js';
  import ToastHost from './lib/ui/ToastHost.svelte';
  import { toastStore } from './lib/ui/toast.svelte.js';
  import { WsClient } from './lib/ws.js';
  import Dashboard from './lib/Dashboard.svelte';

  /** `localStorage`, or `null` when a locked-down browser denies the accessor. */
  function browserStorage(): StorageLike | null {
    try {
      return window.localStorage;
    } catch {
      return null;
    }
  }

  // Kiosk is resolved once at mount. An explicit `?kiosk=1` is remembered in
  // sessionStorage, so a later in-app navigation without the query string keeps
  // the panel in kiosk mode. `isKiosk()` guards a storage access that a locked-
  // down browser can make throw.
  const kiosk = isKiosk();

  // The wizard's exit marker, read once at mount: `/?setup=done` means setup
  // just finished, so confirm it and rewrite the flag out of the URL. A browser
  // that refuses the rewrite (a hostile embedding) keeps a harmless flag.
  if (typeof window !== 'undefined' && setupOutcome(window.location.search) === 'done') {
    toastStore.push('success', 'Setup complete.');
    try {
      // Strip only the one-shot flag; an active `?kiosk=1` must survive.
      const next = window.location.pathname + stripParam(window.location.search, 'setup');
      history.replaceState(null, '', next);
    } catch {
      // The flag is harmless when it cannot be cleared; the toast already fired.
    }
  }

  // One live connection for the app: the effect's teardown is the only place
  // that stops it, so no socket, timer or listener can outlive the mount.
  const ws = new WsClient(liveState);

  // Kiosk only: hide the cursor after inactivity and suppress the platform
  // context menu (a long-press elsewhere would otherwise open browser chrome).
  // Text fields exist only in the settings route, which is not a kiosk route,
  // so disabling selection never blocks an input.
  //
  // Two guards, not one: the cursor chrome is kiosk-only, and `ui.hide_cursor`
  // decides whether it runs at all — a wall panel that always shows the pointer
  // is a supported choice, and the setting existed in the registry with no
  // consumer until it reached this guard. The effect reads the setting, so
  // flipping it while the chrome runs stops it through this teardown.
  $effect(() => {
    if (!kiosk || !settings.hideCursor) {
      return;
    }
    const chrome = new KioskChrome({ doc: document });
    chrome.start();
    return () => chrome.stop();
  });

  // Persist the last-known snapshot so a reload with no network can paint it.
  // `cachePayload()` reads every reactive fact the cache holds, so this effect
  // re-runs per snapshot/delta and queues the fresh payload. The writer is
  // created once, not per run: its first write is synchronous and later writes
  // coalesce into one trailing timer (2 s), so a chatty entity stream cannot
  // rewrite localStorage per frame.
  const cacheWriter: CacheWriter = createCacheWriter(browserStorage());

  $effect(() => {
    const payload = liveState.cachePayload();
    if (payload !== null) {
      cacheWriter.queue(payload);
    }
  });

  // A reload can happen inside the coalescing window, so flush on the way out.
  // `pagehide` covers navigation/close; the teardown covers component destroy.
  const flushCache = (): void => cacheWriter.flush();
  window.addEventListener('pagehide', flushCache);
  onDestroy(() => {
    window.removeEventListener('pagehide', flushCache);
    cacheWriter.stop();
  });

  // Apply the persisted theme and keep 'auto' live against the OS scheme.
  // subscribeSystem() returns its teardown, which the $effect returns so the
  // media-query listener is always removed.
  $effect(() => {
    theme.apply();
    return theme.subscribeSystem();
  });

  // Read the dashboard's settings once on mount (`ui.show_clock`,
  // `ui.hide_cursor`, `ui.show_control_rail`, `ui.dashboard_columns`,
  // `ui.theme`, `ui.accent_color`, `audio.volume`). main.ts normally loaded them
  // already to make the first-run decision, so this only fires after a failed
  // load there. A failed load keeps the documented defaults, so the panel still
  // renders either way.
  $effect(() => {
    if (!settings.loaded) {
      void settings.load();
    }
  });

  // A `config_changed` frame means "refetch what you were told changed". The
  // store coalesces the calls; this wiring only knows the REST endpoints.
  //
  // The body runs inside `untrack`: `refreshConfig()` bumps `config_version`
  // and the refetch sets `refreshing`, so letting the effect subscribe to the
  // store it mutates re-runs it forever (`effect_update_depth_exceeded`, a
  // blank dashboard and a reconnect storm in a real browser). It is a mount
  // effect, not a derivation — nothing here may be a dependency.
  $effect(() => {
    untrack(() => {
      liveState.setConfigRefreshHandler(async (what) => {
        if (what === 'entities') {
          return { entities: await listEntities() };
        }
        if (what === 'buttons') {
          return { buttons: await listButtons() };
        }
        return {};
      });

      // `hideWhenUnavailable` lives only on the REST row (a snapshot omits it),
      // so hydrate it once through the handler just installed. Until this
      // resolves a hide-when-unavailable tile is briefly visible — a documented
      // transient, never a lie about live state.
      liveState.refreshConfig('entities');

      ws.start();
    });
    return () => {
      ws.stop();
      liveState.setConfigRefreshHandler(null);
    };
  });
</script>

<!--
  Kiosk only: suppress the platform context menu so a long-press cannot open
  browser chrome over the panel. The shell is not an interactive widget, so the
  a11y warning below does not apply — text fields live only in the settings
  route, which is never a kiosk route.
-->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="shell" class:kiosk oncontextmenu={(event) => { if (kiosk) event.preventDefault(); }}>
  <Dashboard {kiosk} />
  <ToastHost />
</div>

<style>
  .shell {
    /* Only ever mounted on the dashboard route: the panel owns the viewport,
       so the page itself never scrolls (the content area has its own scroller). */
    height: 100dvh;
    min-height: 0;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    background:
      radial-gradient(ellipse at 50% 38%, color-mix(in srgb, var(--accent) 9%, transparent), transparent 62%),
      var(--bg);
    color: var(--text);
  }

  /* Kiosk: the gradient is decoration; a wall panel gets the plain surface. */
  .shell.kiosk {
    background: var(--bg);
  }
</style>
