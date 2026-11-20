<!--
  The touch-first accent-colour control (the kit had none).

  A tile's accent is a mood, not a spec, so the common case is one tap on a
  swatch — those eight are the presets, drawn from the palette the dark surface
  already validates. Typing a hex is the exception, which is why the free-form
  field only appears when the user asks for it or when the stored value is not a
  preset (editing an existing custom colour must not silently snap to a preset).

  `isValidAccentColor` is imported rather than re-written: the backend stores
  `accent_color` verbatim, so one regex must decide everywhere, and a second copy
  is how a field starts accepting what the tile cannot render. An invalid entry
  leaves `value` on the last valid colour and shows the error, so the selected
  swatch and the saved value never disagree. Because `value` is deliberately
  left alone, the field also reports the bad text through the bindable
  `invalid` prop: the caller's Save must see the error the user sees, or it
  would save everything else and silently drop the colour.

  `allowEmpty` exists because a field that can submit a value the registry
  rejects is a trap: `ui.accent_color` must match a hex pattern, so the
  Appearance panel hides the "no accent" button and no caller can save `null`
  where the backend refuses it.
-->
<script lang="ts">
  import { isValidAccentColor, PRESET_ACCENT_COLORS } from '../entity-preview.js';
  import Icon from './Icon.svelte';
  import Input from './Input.svelte';
  import { nextComponentId } from './ids.js';

  interface Props {
    label: string;
    /** `null` means "no accent": the tile uses the theme's colour. */
    value?: string | null;
    hint?: string;
    error?: string;
    presets?: readonly { value: string; label: string }[];
    /** Set by the field: `true` while the custom text is present and invalid. */
    invalid?: boolean;
    /** Whether the field offers "no accent" (`null`). Off for a value the backend rejects when empty. */
    allowEmpty?: boolean;
  }

  let {
    label,
    value = $bindable(null),
    hint,
    error,
    presets = PRESET_ACCENT_COLORS,
    invalid = $bindable(false),
    allowEmpty = true
  }: Props = $props();

  const CUSTOM_ERROR = 'Use #rgb or #rrggbb, for example #4f9cf9';

  const labelId = nextComponentId('color-label');

  let customOpen = $state(false);
  let customText = $state(value ?? '');
  let customError = $state<string | null>(null);

  const isPreset = $derived(presets.some((preset) => preset.value === value));
  const showCustom = $derived(customOpen || (value !== null && !isPreset));
  const shownValue = $derived(value ?? 'Not set');

  /*
   * `Input` has no change callback, so the typed text is the signal: this effect
   * is the only writer of `value` on the custom path. It commits a valid colour
   * and otherwise only reports the error, which is what keeps `value` on the
   * last valid choice while the user is mid-typing.
   */
  $effect(() => {
    const text = customText.trim();
    if (!showCustom) {
      customError = null;
      invalid = false;
      return;
    }
    // An empty field is "still choosing", not a mistake: complain only about
    // text that is present and wrong.
    if (text === '') {
      customError = null;
      invalid = false;
      return;
    }
    if (isValidAccentColor(text)) {
      customError = null;
      invalid = false;
      value = text;
    } else {
      customError = CUSTOM_ERROR;
      invalid = true;
    }
  });

  function choosePreset(preset: string): void {
    value = preset;
    customText = preset;
    customError = null;
    customOpen = false;
  }

  function openCustom(): void {
    customText = value ?? '';
    customOpen = true;
  }

  function clear(): void {
    value = null;
    customText = '';
    customError = null;
    customOpen = false;
  }
</script>

<div class="field" role="group" aria-labelledby={labelId}>
  <div class="heading">
    <span class="field-label" id={labelId}>{label}</span>
    <span class="current" class:muted={value === null}>{shownValue}</span>
  </div>

  <div class="swatches">
    {#each presets as preset (preset.value)}
      <button
        type="button"
        class="swatch"
        class:selected={value === preset.value}
        style:background={preset.value}
        aria-label={`${label} (${preset.label}, ${preset.value})`}
        aria-pressed={value === preset.value}
        onclick={() => choosePreset(preset.value)}
      >
        {#if value === preset.value}
          <span class="check" aria-hidden="true">
            <Icon name="mdi:check" size={20} />
          </span>
        {/if}
      </button>
    {/each}
  </div>

  {#if !showCustom}
    <button type="button" class="text-button" onclick={openCustom}>Custom colour…</button>
  {:else}
    <Input
      label="Custom colour"
      bind:value={customText}
      inputmode="text"
      placeholder="#4f9cf9"
      error={customError ?? undefined}
    />
  {/if}

  {#if allowEmpty}
    <button type="button" class="text-button" onclick={clear}>No accent (use the theme)</button>
  {/if}

  {#if hint}
    <p class="hint">{hint}</p>
  {/if}
  {#if error}
    <p class="error">
      <Icon name="mdi:alert-circle-outline" size={20} />
      <span>{error}</span>
    </p>
  {/if}
</div>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--gap-sm);
  }

  .field-label {
    font-size: var(--text-label);
    line-height: 1.25;
    font-weight: 600;
  }

  .current {
    font-size: var(--text-label);
    color: var(--text);
  }

  .current.muted {
    color: var(--text-muted);
  }

  .swatches {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }

  /*
   * The swatch is its own colour, so the border carries the target's edge and
   * the check glyph (on an opaque disc) is what marks the selection — neither
   * relies on the colour being distinguishable from its neighbours.
   */
  .swatch {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: var(--tap);
    min-height: var(--tap);
    padding: 0;
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .swatch.selected {
    border-color: var(--text);
    box-shadow: inset 0 0 0 2px var(--text);
  }

  .check {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: var(--surface);
    color: var(--text);
  }

  .text-button {
    display: inline-flex;
    align-items: center;
    align-self: flex-start;
    min-height: var(--tap);
    padding: 0 var(--gap-sm);
    border: none;
    background: transparent;
    color: var(--accent);
    font-size: var(--text-label);
    font-weight: 600;
    text-decoration: underline;
    cursor: pointer;
  }

  .hint,
  .error {
    margin: 0;
    font-size: var(--text-body);
  }

  .hint {
    color: var(--text-muted);
  }

  .error {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    color: var(--state-alert);
  }
</style>
