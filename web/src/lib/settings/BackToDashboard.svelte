<!--
  The one way back to the dashboard from a settings route (HSD-h44d).

  Both shells import this same component rather than each drawing their own
  home control: the D26 target size, the label and the home icon then cannot
  drift apart between the settings shell and the setup wizard.

  It is a real `<a href="/">`, not a `history.pushState` move. `pushState`
  changes the URL without unmounting the root, so SettingsApp's markup would
  stay on screen at `/`. A full navigation re-runs `main.ts`, which is also how
  kiosk mode is re-derived from `sessionStorage` when no `?kiosk` query is
  present — the reason the link needs no query string of its own.

  `guard` is optional and belongs to the host shell. The wizard saves
  `ui.setup_dismissed` before it may leave; a save that fails must keep the
  user here, because the dashboard would bounce a fresh install straight back
  to `/setup`.
-->
<script lang="ts">
  import Icon from '../ui/Icon.svelte';

  interface Props {
    /** Optional gate: resolve `true` to allow the navigation, `false` to stay. */
    guard?: () => Promise<boolean>;
  }

  const { guard }: Props = $props();

  /** One guarded move at a time, so a double tap cannot save twice. */
  let leaving = false;

  async function activate(event: MouseEvent): Promise<void> {
    // Ungated, the link is a plain link: the browser does the navigation.
    if (guard === undefined) {
      return;
    }
    // Modified clicks keep their native meaning (new tab, new window): only a
    // plain primary click is ours to intercept.
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }

    event.preventDefault();
    if (leaving) {
      return;
    }
    leaving = true;
    let allowed = false;
    try {
      allowed = await guard();
    } catch {
      // The host renders its own error sentence; a guard that threw has not
      // allowed the move, so the panel stays put.
      allowed = false;
    } finally {
      leaving = false;
    }
    if (allowed) {
      window.location.assign('/');
    }
  }
</script>

<a
  class="back"
  href="/"
  data-testid="back-to-dashboard"
  aria-label="Back to dashboard"
  onclick={activate}
>
  <Icon name="mdi:home" size={24} />
  <span>Dashboard</span>
</a>

<style>
  .back {
    display: inline-flex;
    flex: none;
    align-items: center;
    gap: var(--gap-sm);
    min-height: var(--tap);
    padding: var(--gap-sm) var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: var(--text-label);
    font-weight: 600;
    text-decoration: none;
  }

  /* A tint on hover, never the only affordance: the words and the border stay. */
  .back:hover {
    background: var(--surface-hover);
  }
</style>
