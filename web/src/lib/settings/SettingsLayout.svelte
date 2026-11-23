<!--
  The settings shell (design 05 §2.2): a nav of implemented sections plus the
  current section's panel. A left-hand column on wide screens, a horizontal tab
  strip on narrow ones.

  The nav items are real links — keyboard-native, focus ring from global CSS,
  middle-click/new-tab friendly — enhanced with `history.pushState` so moving
  between sections does not reload the app. If `pushState` throws (a hostile
  embedding or an old engine), the click falls back to a normal navigation
  instead of silently doing nothing. `goTo` is that same move exposed as a
  plain function, because a panel that owns sub-routes (Entities) navigates
  from a button, not only from an `<a>`.

  **Extension point:** a later ticket adds one `SECTIONS` entry in `sections.ts`
  and one panel branch below. Nothing else in the shell changes. The nav lists
  only implemented sections, so a hand-typed future deep link parses to
  `unknown` and gets the honest notice — never a blank screen or a silent
  redirect to Connection.
-->
<script lang="ts">
  import Icon from '../ui/Icon.svelte';
  import AboutPanel from './AboutPanel.svelte';
  import AppearancePanel from './AppearancePanel.svelte';
  import AudioPanel from './AudioPanel.svelte';
  import BackToDashboard from './BackToDashboard.svelte';
  import ButtonsPanel from './ButtonsPanel.svelte';
  import ConnectionPanel from './ConnectionPanel.svelte';
  import EntitiesPanel from './EntitiesPanel.svelte';
  import HistoryPanel from './HistoryPanel.svelte';
  import SoundsPanel from './SoundsPanel.svelte';
  import SpeechPanel from './SpeechPanel.svelte';
  import SecurityPanel from './SecurityPanel.svelte';
  import { DEFAULT_SECTION, SECTIONS, parseSettingsPath, settingsPath } from './sections.js';

  interface Props {
    /** The current pathname; the route owner updates it on `popstate`. */
    pathname?: string;
  }

  let { pathname = $bindable('/settings') }: Props = $props();

  const target = $derived(parseSettingsPath(pathname));
  const sectionId = $derived(target.kind === 'section' ? target.id : 'unknown');
  const currentPath = $derived(target.kind === 'section' ? settingsPath(target.id) : '');
  const connectionPath = settingsPath(DEFAULT_SECTION);

  /**
   * Move to a settings path in place. Falls back to a real navigation when the
   * History API is unavailable, so the URL never disagrees with the panel.
   */
  function goTo(path: string): void {
    try {
      history.pushState(null, '', path);
    } catch {
      // No History API: let the browser load the path for real.
      window.location.assign(path);
      return;
    }
    pathname = path;
  }

  /**
   * Enhance a real link with `pushState`. Modified clicks keep their native
   * behaviour (new tab, new window), so nothing is stolen from the user.
   */
  function navigate(event: MouseEvent, path: string): void {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }

    event.preventDefault();
    goTo(path);
  }
</script>

<div class="settings">
  <header class="masthead" data-testid="shell-bar">
    <div class="bar">
      <!-- The kiosk escape (HSD-h44d): a wall panel in kiosk mode has no browser
           back button, so settings must carry its own way home. -->
      <BackToDashboard />
      <h1>Settings</h1>
      <!--
        A real link to the wizard's own route, on every section: a full
        navigation starts the wizard root cleanly, and a user who dismissed
        setup once must always be able to come back. Text only — the icon bundle
        has nothing that means "wizard", and a nav control must never draw a
        question mark.
      -->
      <a class="setup-link" href="/setup">Setup wizard</a>
    </div>
  </header>

  <div class="layout">
    <nav class="nav" aria-label="Settings sections">
      <ul class="sections">
        {#each SECTIONS as section (section.id)}
          <li>
            <a
              class="section-link"
              href={section.path}
              aria-current={section.path === currentPath ? 'page' : undefined}
              onclick={(event) => navigate(event, section.path)}
            >
              <Icon name={section.icon} size={24} />
              <span>{section.label}</span>
            </a>
          </li>
        {/each}
      </ul>
    </nav>

    <main class="panel" data-section={sectionId}>
      {#if target.kind === 'unknown'}
        <section class="notice" aria-labelledby="settings-notice-title">
          <h2 id="settings-notice-title">This settings section is not available in this build yet</h2>
          <p>
            The “{target.requested}” section is planned and will arrive in a later update, so
            there is nothing to configure here today.
          </p>
          <a class="notice-link" href={connectionPath} onclick={(event) => navigate(event, connectionPath)}>
            Go to Connection
          </a>
        </section>
      {:else if sectionId === 'connection'}
        <ConnectionPanel />
      {:else if sectionId === 'security'}
        <SecurityPanel />
      {:else if target.kind === 'section' && target.id === 'entities'}
        <EntitiesPanel rest={target.rest} navigate={goTo} />
      {:else if target.kind === 'section' && target.id === 'buttons'}
        <ButtonsPanel rest={target.rest} navigate={goTo} />
      {:else if sectionId === 'sounds'}
        <SoundsPanel />
      {:else if sectionId === 'speech'}
        <SpeechPanel />
      {:else if sectionId === 'audio'}
        <AudioPanel />
      {:else if sectionId === 'appearance'}
        <AppearancePanel />
      {:else if sectionId === 'history'}
        <HistoryPanel />
      {:else if sectionId === 'about'}
        <AboutPanel />
      {/if}
      <!-- Extension point: one more `{:else if sectionId === '…'}` branch per section.
           A panel takes props only when it owns a sub-route; History and About own
           none, so their branch is the bare component tag. -->
    </main>
  </div>
</div>

<style>
  .settings {
    /* The horizontal padding is a property too: the sticky bar has to cancel it
       with a negative inline margin to reach the viewport edges. */
    --shell-pad: clamp(var(--gap), 3vw, calc(var(--gap) * 2));
    /* The bar's own height, so sticky content inside the panel can sit under it. */
    --shell-bar: calc(var(--tap) + var(--gap-sm) * 2);

    min-height: var(--viewport-height);
    background: var(--bg);
    color: var(--text);
    /* Scrolling is allowed in settings (design 05 §7), unlike the dashboard. */
    padding: var(--shell-pad);
    padding-bottom: calc(var(--tap-primary) + env(safe-area-inset-bottom));
  }

  .masthead {
    position: sticky;
    top: 0;
    z-index: var(--z-sticky);
    /* Full-bleed: the bar's own background must cover the viewport edge to
       edge so panel content never slides past its sides on a wide screen. */
    margin-inline: calc(var(--shell-pad) * -1);
    padding: var(--gap-sm) var(--shell-pad);
    margin-bottom: var(--gap);
    background: var(--bg);
  }

  .bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    /* Aligned with `.layout`'s column so the title and the actions line up
       with the nav and panel below, not with the viewport edges. */
    width: min(100%, 1120px);
    margin-inline: auto;
  }

  .setup-link {
    display: inline-flex;
    align-items: center;
    min-height: var(--tap);
    margin-left: auto;
    padding: var(--gap-sm) var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: var(--text-label);
    font-weight: 600;
    text-decoration: none;
  }

  .setup-link:hover {
    background: var(--surface-hover);
  }

  h1 {
    margin: 0;
    font-size: var(--text-title);
    line-height: 1.2;
    font-weight: 700;
  }

  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--gap);
    width: min(100%, 1120px);
    margin-inline: auto;
  }

  .sections {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .section-link {
    display: flex;
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
    transition: background-color var(--motion-fast) var(--ease);
  }

  .section-link:hover {
    background: var(--surface-hover);
  }

  /* Current section: a 4px accent bar plus bold weight, never colour alone. */
  .section-link[aria-current='page'] {
    background: var(--surface-hover);
    border-color: var(--accent);
    box-shadow: inset 4px 0 0 var(--accent);
    font-weight: 700;
  }

  .panel {
    min-width: 0;
  }

  .notice {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    padding: calc(var(--gap) * 1.5);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
  }

  .notice h2 {
    margin: 0;
    font-size: var(--text-title);
    line-height: 1.25;
    font-weight: 700;
  }

  .notice p {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
  }

  .notice-link {
    display: inline-flex;
    align-items: center;
    min-height: var(--tap);
    color: var(--accent);
    font-size: var(--text-label);
    font-weight: 600;
  }

  /* Wide screens: the nav becomes the left-hand column. */
  @media (min-width: 768px) {
    .layout {
      grid-template-columns: minmax(180px, 220px) minmax(0, 1fr);
      align-items: start;
    }

    .sections {
      flex-direction: column;
      flex-wrap: nowrap;
    }

    .section-link {
      width: 100%;
    }
  }
</style>
