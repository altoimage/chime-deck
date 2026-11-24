<script lang="ts">
  import Icon from './Icon.svelte';
  import { nextComponentId } from './ids.js';

  interface Option {
    value: string;
    label: string;
    icon?: string;
    /** A token-backed colour swatch; the label remains visible beside it. */
    swatch?: string;
  }

  interface Props {
    label: string;
    options: Option[];
    value?: string;
    /**
     * Called after the internal selection has updated `value` (the new value).
     * Optional: existing callers keep the current behaviour (mutate only), while a
     * caller whose value is a narrowed union can respond without `bind:`.
     */
    change?: (value: string) => void;
    /**
     * The control's accessible description, rendered under the options and wired
     * to the fieldset with `aria-describedby` (a loose `<p>` beside a fieldset is
     * not associated with it, so a screen reader would read the group name and
     * never the explanation).
     */
    hint?: string;
  }

  let { label, options, value = $bindable(''), change: onchange, hint }: Props = $props();

  // D26: segmented controls are for ≤4 options; more than that belongs in a
  // Select picker sheet. Logged once at init (deliberately the initial value
  // only, hence the closure); the control still renders.
  function warnIfTooManyOptions(): void {
    if (options.length > 4) {
      console.error(
        `SegmentedControl ("${label}") received ${options.length} options, above the D26 ` +
          'maximum of 4 — use Select instead.'
      );
    }
  }
  warnIfTooManyOptions();

  // One id per instance, like Input.svelte: the fieldset carries it, the hint
  // derives from it, and the radios take their group `name` from it, so the
  // describedby target cannot collide with another control's.
  const id = nextComponentId('segmented');
  const hintId = `${id}-hint`;
  const name = `${id}-options`;

  // The last value this control either carried or reported, so `change` fires
  // once per real selection and never for a re-tap of the already-chosen
  // option. Re-synced after every render, so a value the parent pushes back in
  // is never mistaken for one already reported.
  let reported = value;

  $effect(() => {
    reported = value;
  });

  /*
   * `change` only ever comes from the radio's own event, i.e. a real pointer or
   * keyboard selection (the same rule as Select.svelte), never from a
   * programmatic `value` assignment — so it cannot echo a parent's update back
   * at it.
   */
  function select(next: string): void {
    if (next === reported) {
      return;
    }
    reported = next;
    onchange?.(next);
  }
</script>

<fieldset class="segmented" id={id} aria-describedby={hint ? hintId : undefined}>
  <legend class="field-label">{label}</legend>
  <div class="options">
    {#each options as option (option.value)}
      <label class="option" class:selected={value === option.value}>
        <input
          class="sr-only"
          type="radio"
          {name}
          value={option.value}
          bind:group={value}
          onchange={(event) => select(event.currentTarget.value)}
        />
        {#if option.icon}
          <Icon name={option.icon} size={20} />
        {/if}
        {#if option.swatch}
          <span class="swatch" style:background-color={option.swatch} aria-hidden="true"></span>
        {/if}
        <span class="option-label">{option.label}</span>
        {#if value === option.value}
          <!-- Selected state is colour AND a check icon, never colour alone. -->
          <Icon name="mdi:check" size={20} />
        {/if}
      </label>
    {/each}
  </div>
  {#if hint}
    <p class="hint" id={hintId}>{hint}</p>
  {/if}
</fieldset>

<style>
  .segmented {
    margin: 0;
    padding: 0;
    border: none;
    min-inline-size: 0;
  }

  .field-label {
    margin: 0 0 var(--gap-sm);
    padding: 0;
    font-size: var(--text-label);
    line-height: 1.25;
    font-weight: 600;
  }

  .options {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }

  .option {
    flex: 1 1 0;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--gap-sm);
    min-height: var(--tap);
    min-width: var(--tap);
    padding: var(--gap-sm);
    background: var(--surface);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    font-size: var(--text-body);
    cursor: pointer;
    transition: background-color var(--motion-fast) var(--ease);
  }

  .option:not(.selected):hover {
    background: var(--surface-hover);
  }

  .option-label {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .swatch {
    width: 16px;
    height: 16px;
    flex: 0 0 16px;
    border: var(--border-width) solid var(--control-border);
    border-radius: 50%;
  }

  .option.selected {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--accent-contrast);
    font-weight: 600;
  }

  /* The radio input is visually hidden; the ring lands on the row. */
  .option:has(:focus-visible) {
    outline: var(--focus-ring-width) solid var(--focus-ring);
    outline-offset: var(--focus-ring-offset);
  }

  .hint {
    margin: var(--gap-sm) 0 0;
    color: var(--text-muted);
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }
</style>
