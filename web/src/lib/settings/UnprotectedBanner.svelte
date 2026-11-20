<script lang="ts">
  import Icon from '../ui/Icon.svelte';
  import type { AuthStatus } from '../auth.svelte.js';

  interface Props {
    status: AuthStatus | null;
    /** Settings can link to Security; the setup wizard only announces the risk. */
    link?: boolean;
  }

  let { status, link = true }: Props = $props();
  const visible = $derived(
    status?.listener === 'admin' && !status.protected
  );
</script>

{#if visible}
  <aside class="banner" role="status">
    <Icon name="mdi:alert-circle-outline" size={24} />
    <p>
      This configuration interface is unprotected — anyone on this network can read and change
      these settings, including each tile's current state.
      {#if link}
        <a href="/settings/security">Set a password.</a>
      {:else}
        Set a password.
      {/if}
    </p>
  </aside>
{/if}

<style>
  .banner {
    display: flex;
    align-items: flex-start;
    gap: var(--gap-sm);
    width: min(100%, 1120px);
    margin: 0 auto var(--gap);
    padding: var(--gap);
    background: color-mix(in srgb, var(--state-warn) 12%, var(--surface));
    border: var(--border-width) solid var(--state-warn);
    border-radius: var(--radius-sm);
    color: var(--text);
  }

  .banner :global(svg) {
    flex: 0 0 auto;
    color: var(--state-warn);
  }

  .banner p {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
  }

  .banner a {
    margin-inline-start: 0.3em;
    color: var(--accent);
    font-weight: 700;
    text-decoration-thickness: 1px;
    text-underline-offset: 0.15em;
  }

  .banner a:focus-visible {
    outline: var(--focus-ring-width) solid var(--focus-ring);
    outline-offset: var(--focus-ring-offset);
  }
</style>
