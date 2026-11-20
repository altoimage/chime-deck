<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';

  interface Props {
    icon?: string;
    title: string;
    description?: string;
    /** Optional actions, rendered one per row. */
    children?: Snippet;
  }

  const { icon, title, description, children }: Props = $props();
</script>

<div class="empty-state">
  {#if icon}
    <span class="icon">
      <Icon name={icon} size={48} />
    </span>
  {/if}
  <h2 class="title">{title}</h2>
  {#if description}
    <p class="description">{description}</p>
  {/if}
  {#if children}
    <div class="actions">
      {@render children()}
    </div>
  {/if}
</div>

<style>
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--gap);
    padding: calc(var(--gap) * 2) var(--gap);
    text-align: center;
  }

  .icon {
    display: inline-flex;
    color: var(--text-muted);
  }

  .title {
    margin: 0;
    font-size: var(--text-title);
    line-height: 1.2;
    font-weight: 700;
  }

  .description {
    margin: 0;
    max-width: 48ch;
    font-size: var(--text-body);
    line-height: 1.45;
    font-weight: 500;
    color: var(--text-muted);
  }

  .actions {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: var(--gap);
    margin-top: var(--gap);
    min-width: min(320px, 100%);
  }
</style>
