<script lang="ts">
  import Icon from './Icon.svelte';

  interface Props {
    /** Required: becomes the aria-label (icon-only buttons have no visible text). */
    label: string;
    /** mdi icon name, e.g. `mdi:close`. */
    icon: string;
    variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
    disabled?: boolean;
    onclick?: (event: MouseEvent) => void;
    /**
     * Lets callers (Input's reveal toggle) prevent the press from moving
     * focus out of an adjacent field.
     */
    onmousedown?: (event: MouseEvent) => void;
    /** Icon size in px; the tap target stays 48px regardless. */
    size?: number;
  }

  const {
    label,
    icon,
    variant = 'ghost',
    disabled = false,
    onclick,
    onmousedown,
    size = 24
  }: Props = $props();
</script>

<button
  type="button"
  class="icon-btn"
  class:primary={variant === 'primary'}
  class:secondary={variant === 'secondary'}
  class:danger={variant === 'danger'}
  aria-label={label}
  {disabled}
  {onclick}
  {onmousedown}
>
  <Icon name={icon} {size} />
</button>

<style>
  .icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: var(--tap);
    min-height: var(--tap);
    padding: 0;
    border: var(--border-width) solid transparent;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text);
    cursor: pointer;
    transition: background-color var(--motion-fast) var(--ease);
  }

  .primary {
    background: var(--accent);
    color: var(--accent-contrast);
  }

  .secondary {
    background: var(--surface);
    border-color: var(--control-border);
  }

  .danger {
    color: var(--state-alert);
  }

  /* Hover may only add the --surface-hover tint. */
  .icon-btn:hover:not(:disabled):not(.primary) {
    background: var(--surface-hover);
  }

  .icon-btn:disabled {
    background: var(--surface);
    border-color: var(--border);
    color: var(--text-muted);
    cursor: default;
  }
</style>
