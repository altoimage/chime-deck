<script lang="ts">
  import IconButton from './IconButton.svelte';
  import { nextComponentId } from './ids.js';

  interface Props {
    label: string;
    value?: number;
    min?: number;
    max?: number;
    step?: number;
    unit?: string;
    disabled?: boolean;
    /**
     * Paint the label visually clipped (`.sr-only`) while keeping it in the DOM as
     * the group's `aria-labelledby` target.
     *
     * For rows where another control already prints the same label: Settings →
     * Audio's two volume rows pass one label to both a Slider and this Stepper, so
     * the row read it twice. The clip is the shared `global.css` rule — a 1px box,
     * never `display: none` — so the text still names the `role="group"` row and
     * is still announced. Opt-in: left unset, the label stays visible, which is
     * the only label a Stepper has in VolumeSheet, ButtonEditor, SpeechPanel and
     * the Gallery.
     */
    labelHidden?: boolean;
    /**
     * Called after an internal −/+ change has updated `value` (the new value).
     * Optional: existing callers keep the old behaviour (mutate only), while a
     * sheet can commit one save per tap instead of PATCHing per movement.
     */
    change?: (value: number) => void;
  }

  let {
    label,
    value = $bindable(0),
    min = 0,
    max = 100,
    step = 1,
    unit = '',
    disabled = false,
    labelHidden = false,
    change: onchange
  }: Props = $props();

  const labelId = nextComponentId('stepper-label');

  const canDecrease = $derived(!disabled && value - step >= min);
  const canIncrease = $derived(!disabled && value + step <= max);

  function change(delta: number): void {
    value = Math.min(max, Math.max(min, value + delta));
    onchange?.(value);
  }
</script>

<div class="field">
  <span class="field-label" class:sr-only={labelHidden} id={labelId}>{label}</span>
  <!-- D26: no <input type="number"> — large −/+ buttons instead. -->
  <div class="row" role="group" aria-labelledby={labelId}>
    <IconButton
      icon="mdi:minus"
      label={`Decrease ${label}`}
      disabled={!canDecrease}
      onclick={() => change(-step)}
    />
    <span class="value" aria-live="polite">{value}{unit}</span>
    <IconButton
      icon="mdi:plus"
      label={`Increase ${label}`}
      disabled={!canIncrease}
      onclick={() => change(step)}
    />
  </div>
</div>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .field-label {
    font-size: var(--text-label);
    line-height: 1.25;
    font-weight: 600;
  }

  .row {
    display: flex;
    align-items: center;
    gap: var(--gap);
    min-height: var(--tap);
  }

  .value {
    min-width: 3ch;
    text-align: center;
    font-family: var(--font-num);
    font-size: var(--text-state);
  }
</style>
