<!--
  Root of the settings route (`/settings…`), mounted by `main.ts` instead of
  the dashboard. It owns the route's process-level wiring and nothing else:

  - the theme effect (identical to `App.svelte`, teardown included);
  - one `settings.load()` on mount — a failed load keeps the documented
    defaults, and the panels render them rather than crash;
  - the `popstate` listener, added in an effect and removed in its teardown, so
    back/forward moves between sections without a reload;
  - the single `ToastHost` for the route.

  The shell itself is `SettingsLayout.svelte`; a later ticket adds panels
  there, not here.
-->
<script lang="ts">
  import { auth } from '../auth.svelte.js';
  import { settings } from '../settings.svelte.js';
  import { theme } from '../theme.svelte.js';
  import ToastHost from '../ui/ToastHost.svelte';
  import SettingsLayout from './SettingsLayout.svelte';
  import UnprotectedBanner from './UnprotectedBanner.svelte';

  // main.ts only mounts this on a settings path, so the fallback never shows.
  let pathname = $state(
    typeof window === 'undefined' ? '/settings' : window.location.pathname
  );

  $effect(() => {
    theme.apply();
    return theme.subscribeSystem();
  });

  $effect(() => {
    void settings.load();
    void auth.loadStatus();
  });

  $effect(() => {
    const onPopState = (): void => {
      pathname = window.location.pathname;
    };
    window.addEventListener('popstate', onPopState);
    return () => {
      window.removeEventListener('popstate', onPopState);
    };
  });
</script>

<div class="settings-shell">
  <UnprotectedBanner status={auth.status} />
  <SettingsLayout bind:pathname />
  <ToastHost />
</div>

<style>
  .settings-shell {
    min-height: 100dvh;
    background: var(--bg);
    color: var(--text);
  }
</style>
