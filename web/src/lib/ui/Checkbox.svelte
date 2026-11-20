<!--
  A checkbox row for a set of independent choices.

  Toggle is a switch for one setting that is on or off *right now*; a checkbox
  says "this option is included", which is the shape a list of attributes or
  capabilities has. The whole row is the target (48px, D26) because a 24px box is
  not a finger target, and the box draws a check glyph when checked so the state
  is never carried by colour alone (D26).

  The real `<input type="checkbox">` is kept — visually hidden, not replaced —
  so a screen reader, a keyboard Space and a tap on the label all still work;
  the visible box is only the paint.
-->
<script lang="ts">
  import Icon from './Icon.svelte';

  interface Props {
    label: string;
    checked?: boolean;
    /** A muted line under the label: what choosing this actually does. */
    description?: string;
    disabled?: boolean;
    /** The new value, for callers that do not bind `checked`. */
    onselect?: (checked: boolean) => void;
  }

  let { label, checked = $bindable(false), description, disabled = false, onselect }: Props = $props();

  /*
   * The native input is the control (so a label tap, a keyboard Space and a
   * screen reader all work unchanged); this handler is the only writer of
   * `checked`, which keeps the visible box and the real input from disagreeing.
   */
  function handleChange(event: Event): void {
    const input = event.currentTarget;
    if (!(input instanceof HTMLInputElement)) {
      return;
    }
    checked = input.checked;
    onselect?.(checked);
  }
</script>

<label class="checkbox" class:disabled>
  <input type="checkbox" class="sr-only" {checked} {disabled} onchange={handleChange} />
  <span class="box" class:checked aria-hidden="true">
    {#if checked}
      <Icon name="mdi:check" size={20} />
    {/if}
  </span>
  <span class="texts">
    <span class="label">{label}</span>
    {#if description}
      <span class="description">{description}</span>
    {/if}
  </span>
</label>

<style>
  .checkbox {
    display: flex;
    align-items: center;
    gap: var(--gap);
    width: 100%;
    min-height: var(--tap);
    padding: var(--gap-sm) var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    color: var(--text);
    cursor: pointer;
    transition: background-color var(--motion-fast) var(--ease);
  }

  .checkbox:hover:not(.disabled) {
    background: var(--surface-hover);
  }

  /* The ring goes on the row: the real input is .sr-only, so its own outline
     would be drawn inside a 1px clipped box and never be seen. */
  .checkbox:has(input:focus-visible) {
    outline: var(--focus-ring-width) solid var(--focus-ring);
    outline-offset: var(--focus-ring-offset);
  }

  .checkbox.disabled {
    border-color: var(--border);
    color: var(--text-muted);
    cursor: default;
  }

  .box {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    width: 24px;
    height: 24px;
    border: var(--border-width) solid var(--control-border);
    border-radius: 6px;
    background: var(--surface-hover);
    color: var(--accent-contrast);
  }

  /* Checked is the accent fill plus the glyph, never the fill alone. */
  .checkbox:not(.disabled) .box.checked {
    background: var(--accent);
    border-color: var(--accent);
  }

  .checkbox.disabled .box {
    background: var(--bg);
    border-color: var(--border);
    color: var(--text-muted);
  }

  .texts {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .label {
    font-size: var(--text-body);
    line-height: 1.3;
  }

  .description {
    font-size: var(--text-label);
    line-height: 1.4;
    color: var(--text-muted);
  }
</style>
