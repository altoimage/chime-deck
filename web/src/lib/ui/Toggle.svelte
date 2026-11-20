<script lang="ts">
  interface Props {
    label: string;
    checked?: boolean;
    description?: string;
    disabled?: boolean;
    /**
     * Called after the internal toggle has flipped `checked` (the new value).
     * Optional: existing callers keep the current behaviour (mutate only), while a
     * control that has to round-trip to the server can drive it without `bind:`.
     */
    change?: (checked: boolean) => void;
  }

  let {
    label,
    checked = $bindable(false),
    description,
    disabled = false,
    change: onchange
  }: Props = $props();

  function toggle(): void {
    checked = !checked;
    onchange?.(checked);
  }
</script>

<button
  type="button"
  class="toggle"
  class:on={checked}
  role="switch"
  aria-checked={checked}
  {disabled}
  onclick={toggle}
>
  <span class="texts">
    <span class="label">{label}</span>
    {#if description}
      <span class="description">{description}</span>
    {/if}
  </span>
  <span class="track" aria-hidden="true">
    <span class="knob"></span>
  </span>
</button>

<style>
  .toggle {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap);
    width: 100%;
    min-height: var(--tap);
    padding: var(--gap-sm) var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    color: var(--text);
    text-align: start;
    cursor: pointer;
    transition: background-color var(--motion-fast) var(--ease);
  }

  .toggle:hover:not(:disabled) {
    background: var(--surface-hover);
  }

  .toggle:disabled {
    border-color: var(--border);
    color: var(--text-muted);
    cursor: default;
  }

  .toggle:disabled .track {
    background: var(--surface);
    border-color: var(--border);
  }

  .toggle:disabled .knob {
    background: var(--text-muted);
  }

  .texts {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .label {
    font-size: var(--text-label);
    line-height: 1.25;
    font-weight: 600;
  }

  .description {
    font-size: var(--text-body);
    line-height: 1.4;
    color: var(--text-muted);
  }

  .track {
    position: relative;
    flex-shrink: 0;
    width: 56px;
    height: var(--thumb);
    border-radius: 999px;
    background: var(--surface-hover);
    border: var(--border-width) solid var(--control-border);
    transition:
      background-color var(--motion-fast) var(--ease),
      border-color var(--motion-fast) var(--ease);
  }

  .knob {
    position: absolute;
    top: 50%;
    left: 2px;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    background: var(--control-border);
    transform: translate(0, -50%);
    /* Transform-only motion, correct at 0ms under reduced motion. */
    transition:
      transform var(--motion-fast) var(--ease),
      background-color var(--motion-fast) var(--ease);
  }

  .on .track {
    background: var(--accent);
    border-color: var(--accent);
  }

  .on .knob {
    transform: translate(26px, -50%);
    background: var(--surface);
  }
</style>
