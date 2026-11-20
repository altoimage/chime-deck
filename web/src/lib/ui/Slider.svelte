<script lang="ts">
  import { nextComponentId } from './ids.js';

  interface Props {
    label: string;
    value?: number;
    min?: number;
    max?: number;
    step?: number;
    /** Display suffix, e.g. '%' — also exposed via aria-valuetext. */
    unit?: string;
    disabled?: boolean;
  }

  let {
    label,
    value = $bindable(0),
    min = 0,
    max = 100,
    step = 1,
    unit = '',
    disabled = false
  }: Props = $props();

  const id = nextComponentId('slider');
</script>

<div class="field">
  <div class="head">
    <label class="field-label" for={id}>{label}</label>
    <!-- The range input announces the value (and aria-valuetext) itself. -->
    <span class="value" aria-hidden="true">{value}{unit}</span>
  </div>
  <input
    class="range"
    {id}
    type="range"
    bind:value
    {min}
    {max}
    {step}
    {disabled}
    aria-valuetext={unit ? `${value}${unit}` : undefined}
  />
</div>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--gap);
  }

  .field-label {
    font-size: var(--text-label);
    line-height: 1.25;
    font-weight: 600;
  }

  .field:has(.range:disabled) .field-label {
    color: var(--text-muted);
  }

  .value {
    font-family: var(--font-num);
    font-size: var(--text-body);
    color: var(--text-muted);
  }

  /* The control is 48px tall so the thin track stays finger-tappable. */
  .range {
    -webkit-appearance: none;
    appearance: none;
    width: 100%;
    height: var(--tap);
    margin: 0;
    background: transparent;
    cursor: pointer;
  }

  .range::-webkit-slider-runnable-track {
    height: 8px;
    border-radius: 999px;
    background: var(--surface-hover);
    border: var(--border-width) solid var(--control-border);
  }

  .range::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: var(--thumb);
    height: var(--thumb);
    margin-top: calc((8px + 2 * var(--border-width) - var(--thumb)) / 2);
    border: none;
    border-radius: 50%;
    background: var(--accent);
  }

  .range::-moz-range-track {
    height: 8px;
    border-radius: 999px;
    background: var(--surface-hover);
    border: var(--border-width) solid var(--control-border);
  }

  .range::-moz-range-thumb {
    width: var(--thumb);
    height: var(--thumb);
    border: none;
    border-radius: 50%;
    background: var(--accent);
  }

  .range:disabled {
    cursor: default;
  }

  .range:disabled::-webkit-slider-runnable-track {
    background: var(--surface);
    border-color: var(--border);
  }

  .range:disabled::-webkit-slider-thumb {
    background: var(--text-muted);
  }

  .range:disabled::-moz-range-track {
    background: var(--surface);
    border-color: var(--border);
  }

  .range:disabled::-moz-range-thumb {
    background: var(--text-muted);
  }

  .field:has(.range:disabled) .value {
    color: var(--text-muted);
  }
</style>
