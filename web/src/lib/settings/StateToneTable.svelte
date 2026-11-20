<script lang="ts">
  import { ApiError, getStateToneDefaults, type StateToneDefaults } from '../api.js';
  import Button from '../ui/Button.svelte';
  import Input from '../ui/Input.svelte';
  import SegmentedControl from '../ui/SegmentedControl.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { nextComponentId } from '../ui/ids.js';
  import {
    isNeverMappedState,
    mergeStateTones,
    stateToneRows,
    validateNewState
  } from './state-tones.js';

  interface Props {
    stateTones?: Record<string, string>;
    colourByState?: boolean;
    currentState: string | null;
    currentWord: string | null;
    /** The backend's tone for the live state; this component does not resolve it. */
    currentTone: string | null;
  }

  let {
    stateTones = $bindable({}),
    colourByState = $bindable(true),
    currentState,
    currentWord,
    currentTone
  }: Props = $props();

  const instanceId = nextComponentId('state-tones');

  const TONE_PRESENTATION: Record<string, { label: string; swatch: string }> = {
    ok: { label: 'OK', swatch: 'var(--state-ok)' },
    warn: { label: 'Warn', swatch: 'var(--state-warn)' },
    alert: { label: 'Alert', swatch: 'var(--state-alert)' },
    off: { label: 'Off', swatch: 'var(--state-off)' }
  };

  let toneData = $state<StateToneDefaults | null>(null);
  let defaultsLoading = $state(true);
  let defaultsError = $state<string | null>(null);
  let requestSequence = 0;
  let newState = $state('');
  let newStateTone = $state('');
  let addError = $state<string | null>(null);
  let pendingState = $state<string | null>(null);
  let focusState = $state<string | null>(null);

  async function loadDefaults(): Promise<void> {
    const sequence = ++requestSequence;
    defaultsLoading = true;
    defaultsError = null;
    toneData = null;
    try {
      const result = await getStateToneDefaults();
      if (sequence !== requestSequence) {
        return;
      }
      toneData = result;
    } catch (cause) {
      if (sequence !== requestSequence) {
        return;
      }
      defaultsError =
        cause instanceof ApiError
          ? cause.message
          : 'The built-in state tones could not be loaded. Please try again.';
    } finally {
      if (sequence === requestSequence) {
        defaultsLoading = false;
      }
    }
  }

  // Defaults are the backend's live contract, so the editor must not invent a
  // blank table if they cannot be fetched. Retry is explicit and remains local.
  $effect(() => {
    void loadDefaults();
    return () => {
      requestSequence += 1;
    };
  });

  const defaults = $derived(toneData?.defaults ?? {});
  const effectiveTones = $derived(mergeStateTones(defaults, stateTones));
  const rows = $derived(
    toneData === null ? [] : stateToneRows(defaults, stateTones, currentState, pendingState)
  );
  const toneOptions = $derived(
    (toneData?.tones ?? []).map((tone) => ({
      value: tone,
      label: TONE_PRESENTATION[tone]?.label ?? tone,
      swatch: TONE_PRESENTATION[tone]?.swatch ?? 'var(--state-off)'
    }))
  );

  function hasOwnTone(map: Record<string, string>, state: string | null): boolean {
    return state !== null && Object.prototype.hasOwnProperty.call(map, state);
  }

  const currentIsUsable = $derived(
    currentState !== null && currentState.trim() !== '' && !isNeverMappedState(currentState)
  );
  const currentIsMapped = $derived(
    toneData !== null &&
      currentIsUsable &&
      hasOwnTone(effectiveTones, currentState)
  );
  const currentIsUnmapped = $derived(
    toneData !== null && currentIsUsable && !currentIsMapped
  );
  // The editor's current-state summary follows the exact mapping being edited;
  // the preview remains separate and continues to carry the backend's live tone.
  const currentMappingTone = $derived(
    currentState !== null && hasOwnTone(effectiveTones, currentState)
      ? (effectiveTones[currentState] ?? null)
      : currentTone
  );
  const currentDisplayWord = $derived(currentWord ?? currentState ?? '');

  function toneLabel(tone: string | null): string {
    if (tone === null) {
      return 'tone not reported';
    }
    return TONE_PRESENTATION[tone]?.label ?? tone;
  }

  function stateRowId(state: string): string {
    return `${instanceId}-${encodeURIComponent(state)}`;
  }

  // $effect runs after the pending row has entered the DOM. Scrolling that row
  // into view and focusing its first radio makes the callout's action concrete.
  $effect(() => {
    const state = focusState;
    if (state === null) {
      return;
    }
    const target = document.getElementById(stateRowId(state));
    if (target === null) {
      return;
    }
    if (typeof target.scrollIntoView === 'function') {
      // No animated scroll is needed to make the destination discoverable.
      target.scrollIntoView({ behavior: 'auto', block: 'nearest' });
    }
    target.querySelector<HTMLInputElement>('input[type="radio"]')?.focus();
  });

  function assignCurrentState(): void {
    if (!currentIsUsable || currentState === null) {
      return;
    }
    pendingState = currentState;
    focusState = currentState;
  }

  function chooseTone(state: string, tone: string): void {
    stateTones = { ...stateTones, [state]: tone };
    if (pendingState === state) {
      pendingState = null;
      focusState = null;
    }
  }

  function removeOverride(state: string): void {
    const next = { ...stateTones };
    delete next[state];
    stateTones = next;
    if (pendingState === state) {
      pendingState = null;
      focusState = null;
    }
  }

  function addState(): void {
    const validation = validateNewState(newState, defaults, stateTones, pendingState);
    if (validation !== null) {
      addError = validation;
      return;
    }
    if (newStateTone === '' || !toneOptions.some((option) => option.value === newStateTone)) {
      addError = 'Choose a tone before adding this state.';
      return;
    }
    stateTones = { ...stateTones, [newState]: newStateTone };
    newState = '';
    newStateTone = '';
    addError = null;
  }
</script>

<div class="state-tone-table">
  <Toggle
    label="Colour by state"
    description="Turn this off to keep the tile neutral in every state and let the word carry the state."
    bind:checked={colourByState}
  />

  {#if toneData !== null && currentIsUsable}
    {#if !colourByState}
      <p class="current-line" role="status">
        Colour by state is off. The tile stays neutral right now;
        {currentDisplayWord} would be
        {currentIsMapped ? toneLabel(currentMappingTone) : 'unmapped'}.
      </p>
    {:else if currentIsMapped}
      <p class="current-line" role="status">
        Current state: {currentDisplayWord} — {toneLabel(currentMappingTone)}
      </p>
    {/if}

    {#if currentIsUnmapped}
      <section class="unmapped" role="status" aria-live="polite">
        <div>
          <strong>This state has no tone — assign one</strong>
          <p>{currentDisplayWord} is shown grey until you choose a tone for it.</p>
        </div>
        <Button onclick={assignCurrentState}>Assign a tone</Button>
      </section>
    {/if}
  {/if}

  {#if defaultsLoading}
    <Spinner label="Loading state tones" />
  {:else if defaultsError !== null}
    <div class="load-error" role="alert">
      <p>{defaultsError}</p>
      <Button onclick={() => void loadDefaults()}>Retry state tones</Button>
    </div>
  {:else if toneData !== null}
    <div class="mapping-content" class:deemphasized={!colourByState}>
      {#if !colourByState}
        <p class="neutral-note">
          These mappings stay editable and will apply again if Colour by state is turned on.
        </p>
      {/if}

      <div class="tone-rows">
        {#each rows as row (row.state)}
          <article class="tone-row" id={stateRowId(row.state)}>
            <div class="row-meta">
              <code class="state-token">{row.state}</code>
              <span class="provenance">{row.provenance}</span>
            </div>

            <SegmentedControl
              label={`Tone for ${row.state}`}
              options={toneOptions}
              value={row.tone ?? ''}
              change={(tone) => chooseTone(row.state, tone)}
            />

            {#if row.pending}
              <p class="pending-note">Choose a tone to finish assigning this current state.</p>
            {:else if row.provenance === 'custom'}
              {#if row.hasDefault}
                <Button onclick={() => removeOverride(row.state)}>Reset to default</Button>
              {:else}
                <Button onclick={() => removeOverride(row.state)}>Remove state</Button>
              {/if}
            {/if}
          </article>
        {/each}
      </div>
    </div>

    <div class="add-state">
      <h4>Add state</h4>
      <Input label="Raw state" bind:value={newState} />
      <SegmentedControl
        label="Tone for new state"
        options={toneOptions}
        bind:value={newStateTone}
      />
      {#if addError !== null}
        <p class="validation-error" role="alert">{addError}</p>
      {/if}
      <Button onclick={addState}>Add state</Button>
    </div>
  {/if}
</div>

<style>
  .state-tone-table {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

  .current-line {
    margin: 0;
    padding: var(--gap-sm) var(--gap);
    border-inline-start: 4px solid var(--accent);
    background: var(--surface);
    color: var(--text);
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .unmapped {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap);
    padding: var(--gap);
    border: var(--border-width) solid var(--state-warn);
    border-radius: var(--radius-sm);
    background: var(--surface);
  }

  .unmapped strong {
    display: block;
    font-size: var(--text-label);
    line-height: 1.35;
  }

  .unmapped p,
  .load-error p,
  .neutral-note,
  .pending-note,
  .validation-error {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .unmapped p,
  .neutral-note,
  .pending-note {
    color: var(--text-muted);
  }

  .load-error {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap);
    padding: var(--gap);
    border: var(--border-width) solid var(--state-alert);
    border-radius: var(--radius-sm);
    background: var(--surface);
  }

  .load-error p,
  .validation-error {
    color: var(--state-alert);
  }

  .mapping-content {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
    padding: var(--gap);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    background: var(--surface);
  }

  /* Neutral surfaces and borders de-emphasise inactive colouring without dimming
     words, focus rings, or controls into an inaccessible contrast level. */
  .mapping-content.deemphasized {
    background: var(--bg);
    border-color: var(--border);
  }

  .tone-rows {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

  .tone-row {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    min-width: 0;
    padding: var(--gap);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--surface);
    scroll-margin-block: var(--gap);
  }

  .row-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .state-token {
    min-width: 0;
    color: var(--text);
    font-family: var(--font-num);
    font-size: var(--text-body);
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  .provenance {
    flex: 0 0 auto;
    padding: 2px var(--gap-sm);
    border: var(--border-width) solid var(--control-border);
    border-radius: 999px;
    color: var(--text-muted);
    font-size: var(--text-body);
    line-height: 1.35;
  }

  .add-state {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
    padding-block-start: var(--gap);
    border-block-start: var(--border-width) solid var(--border);
  }

  .add-state h4 {
    margin: 0;
    font-size: var(--text-label);
    line-height: 1.25;
  }
</style>
