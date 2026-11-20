<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';

  interface Props {
    title?: string;
    children: Snippet;
    footer?: Snippet;
    /** Renders the card as a button with an always-visible affordance. */
    interactive?: boolean;
    onclick?: (event: MouseEvent) => void;
  }

  const { title, children, footer, interactive = false, onclick }: Props = $props();
</script>

{#snippet body()}
  {#if title}
    <h3 class="card-title">{title}</h3>
  {/if}
  <div class="card-body">
    {@render children()}
  </div>
  {#if footer}
    <div class="card-footer">
      {@render footer()}
    </div>
  {/if}
{/snippet}

{#if interactive}
  <button type="button" class="card interactive" {onclick}>
    {@render body()}
    <!-- Always-visible affordance: interactivity is never hover-only. -->
    <span class="affordance" aria-hidden="true">
      <Icon name="mdi:chevron-right" size={24} />
    </span>
  </button>
{:else}
  <div class="card">
    {@render body()}
  </div>
{/if}

<style>
  .card {
    display: block;
    width: 100%;
    padding: calc(var(--gap) * 1.5);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
    color: var(--text);
    font-size: var(--text-body);
    text-align: start;
  }

  .interactive {
    position: relative;
    min-height: var(--tap);
    padding-inline-end: calc(var(--tap) + var(--gap) * 1.5);
    border-color: var(--control-border);
    cursor: pointer;
    transition: background-color var(--motion-fast) var(--ease);
  }

  .interactive:hover {
    background: var(--surface-hover);
  }

  .affordance {
    position: absolute;
    top: 50%;
    inset-inline-end: var(--gap);
    transform: translateY(-50%);
    display: flex;
    align-items: center;
    justify-content: center;
    width: var(--tap);
    height: var(--tap);
    border: var(--border-width) solid var(--control-border);
    border-radius: 50%;
    color: var(--text-muted);
  }

  .card-title {
    margin: 0 0 var(--gap-sm);
    font-size: var(--text-title);
    font-weight: 700;
  }

  .card-body {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .card-footer {
    margin-top: var(--gap);
    padding-top: var(--gap);
    border-top: var(--border-width) solid var(--border);
  }
</style>
