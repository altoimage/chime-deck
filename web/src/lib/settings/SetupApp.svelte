<!--
  Root of the wizard route (`/setup`), mounted by `main.ts` instead of the
  dashboard. The wizard is its own root for the same reason the settings shell
  is: the dashboard's socket, stores and refresh wiring must not start just to
  fill in a form. Filling in three fields must not open a websocket, hydrate
  live tiles or arm a config-refresh handler.

  It owns the route's process-level wiring and nothing else:

  - the theme effect (identical to `App.svelte`, teardown included);
  - one `settings.load()` on mount. A failed load is not fatal here: the wizard
    renders the registry defaults and the server's sentence verbatim, and the
    store keeps reporting the failure until a save succeeds;
  - the single `ToastHost`, so the route has the same toast plumbing as every
    other root even though the wizard confirms in place.

  There is no `popstate` listener because the wizard owns no sub-routes: its
  three steps are component state, and the full-screen picker and editor it
  borrows are views inside this one route.
-->
<script lang="ts">
  import { auth } from '../auth.svelte.js';
  import { settings } from '../settings.svelte.js';
  import { theme } from '../theme.svelte.js';
  import ToastHost from '../ui/ToastHost.svelte';
  import SetupWizard from './SetupWizard.svelte';
  import UnprotectedBanner from './UnprotectedBanner.svelte';

  $effect(() => {
    theme.apply();
    return theme.subscribeSystem();
  });

  $effect(() => {
    void settings.load();
    void auth.loadStatus();
  });
</script>

<div class="setup-shell">
  <div class="setup-banner">
    <UnprotectedBanner status={auth.status} link={false} />
  </div>
  <SetupWizard />
  <ToastHost />
</div>

<style>
  .setup-shell {
    min-height: var(--viewport-height);
    background: var(--bg);
    color: var(--text);
  }

  .setup-banner {
    width: min(100%, 720px);
    margin-inline: auto;
    padding: var(--gap) clamp(var(--gap), 3vw, calc(var(--gap) * 2)) 0;
  }

  /* `UnprotectedBanner` renders nothing on a protected install; without this,
     the wrapper's 16px top padding would make every wizard page scrollable by
     16px for no content. */
  .setup-banner:empty {
    display: none;
  }

  .setup-banner :global(.banner) {
    margin-bottom: 0;
  }
</style>
