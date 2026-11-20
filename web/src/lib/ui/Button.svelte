<script lang="ts">
  import type { Snippet } from 'svelte';
  import Spinner from './Spinner.svelte';

  interface Props {
    /**
     * primary = --accent fill, danger = --state-alert fill,
     * secondary = --surface + --control-border outline, ghost = transparent.
     */
    variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
    /** lg (and primary) use the 56px primary-action tap target. */
    size?: 'md' | 'lg';
    disabled?: boolean;
    /** Shows an inline Spinner, sets aria-busy and disables the button. */
    loading?: boolean;
    type?: 'button' | 'submit';
    fullWidth?: boolean;
    onclick?: (event: MouseEvent) => void;
    children: Snippet;
  }

  const {
    variant = 'secondary',
    size = 'md',
    disabled = false,
    loading = false,
    type = 'button',
    fullWidth = false,
    onclick,
    children
  }: Props = $props();
</script>

<button
  {type}
  class="btn"
  class:primary={variant === 'primary'}
  class:secondary={variant === 'secondary'}
  class:ghost={variant === 'ghost'}
  class:danger={variant === 'danger'}
  class:lg={size === 'lg'}
  class:full-width={fullWidth}
  disabled={disabled || loading}
  aria-busy={loading}
  {onclick}
>
  {#if loading}
    <Spinner size={20} label="Working" />
  {/if}
  {@render children()}
</button>

<style>
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--gap-sm);
    min-height: var(--tap);
    min-width: var(--tap);
    padding: var(--gap-sm) var(--gap);
    border: var(--border-width) solid transparent;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text);
    font-size: var(--text-label);
    font-weight: 600;
    cursor: pointer;
    transition: background-color var(--motion-fast) var(--ease);
  }

  /* D26: primary actions and large buttons get the 56px target. */
  .primary,
  .lg {
    min-height: var(--tap-primary);
  }

  .primary {
    background: var(--accent);
    color: var(--accent-contrast);
  }

  .secondary {
    background: var(--surface);
    border-color: var(--control-border);
    color: var(--text);
  }

  .ghost {
    background: transparent;
    color: var(--text);
  }

  /*
   * --state-alert is a strong fill; --bg is the readable text colour on it
   * in both palettes (≈6:1 dark, ≈5:1 light).
   */
  .danger {
    background: var(--state-alert);
    color: var(--bg);
  }

  /* Hover may only add the --surface-hover tint, and only where it does not
     erase a meaningful fill. */
  .secondary:hover:not(:disabled),
  .ghost:hover:not(:disabled) {
    background: var(--surface-hover);
  }

  .btn:disabled {
    background: var(--surface);
    border-color: var(--border);
    color: var(--text-muted);
    cursor: default;
  }

  /* The button's visible action label names its busy state. Keep the
     Spinner's own announcement for assistive technology without duplicating
     that word visually. */
  :global(.btn .spinner .status-label) {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  .full-width {
    width: 100%;
  }
</style>
