<!--
  A large-target picker: a button that opens a sheet of 56px rows, never a
  native <select> dropdown (D26).

  An option can be listed as unavailable (`disabled` plus a `reason`). This
  exists because an unavailable engine — a speech voice with no data, say — must
  not be *offered*: a status line beside the picker still lets a user pick it
  and fail at press time, while a row that is not a control cannot be chosen at
  all. The row keeps its label and its reason, so the list still explains itself
  rather than silently shrinking.
-->
<script lang="ts">
  import { tick, untrack } from 'svelte';
  import Icon from './Icon.svelte';
  import Sheet from './Sheet.svelte';
  import { nextComponentId } from './ids.js';

  interface Option {
    value: string;
    label: string;
    description?: string;
    icon?: string;
    /** When true the option is listed with its reason but cannot be chosen. */
    disabled?: boolean;
    /** Why it cannot be chosen; rendered verbatim, never invented. */
    reason?: string;
  }

  interface Props {
    label: string;
    options: Option[];
    value?: string;
    placeholder?: string;
  }

  let { label, options, value = $bindable(''), placeholder = 'Choose…' }: Props = $props();

  // D26: a button + picker sheet, never a native <select> dropdown.
  const labelId = nextComponentId('select-label');
  const valueId = `${labelId}-value`;

  let open = $state(false);
  let listEl: HTMLDivElement | undefined = $state();

  const selected = $derived(options.find((option) => option.value === value));

  // An unavailable row is not a radio, so it is absent from the radio group:
  // focus and arrow movement index this list, never `options`.
  const selectable = $derived(options.filter((option) => !option.disabled));

  // Move focus to the selected row when the sheet opens (the sheet's own
  // effect runs first; tick() waits out any residual ordering doubt). A saved
  // value that has since become unavailable finds no row here and parks focus
  // on the first selectable one.
  $effect(() => {
    if (!open) {
      return;
    }
    const index = untrack(() => selectable.findIndex((option) => option.value === value));
    void tick().then(() => {
      listEl
        ?.querySelectorAll<HTMLInputElement>('input[type="radio"]')
        [Math.max(0, index)]?.focus();
    });
  });

  function focusOption(index: number): void {
    const clamped = Math.min(selectable.length - 1, Math.max(0, index));
    const input = listEl?.querySelectorAll<HTMLInputElement>('input[type="radio"]')[clamped];
    if (input) {
      value = input.value;
      input.focus();
    }
  }

  /*
   * All keyboard paths preventDefault, so the native radio quirk of firing
   * `change` on arrow keys can never close the sheet: `change` then only
   * ever comes from a real pointer tap (see onchange on the inputs).
   */
  function handleKeydown(event: KeyboardEvent): void {
    const currentIndex = selectable.findIndex((option) => option.value === value);
    switch (event.key) {
      case 'ArrowDown':
      case 'ArrowRight':
        event.preventDefault();
        focusOption(Math.min(selectable.length - 1, currentIndex + 1));
        break;
      case 'ArrowUp':
      case 'ArrowLeft':
        event.preventDefault();
        focusOption(Math.max(0, currentIndex <= 0 ? 0 : currentIndex - 1));
        break;
      case 'Home':
        event.preventDefault();
        focusOption(0);
        break;
      case 'End':
        event.preventDefault();
        focusOption(selectable.length - 1);
        break;
      case 'Enter':
      case ' ':
        // Selection already follows focus; Enter/Space confirms and closes.
        event.preventDefault();
        open = false;
        break;
    }
  }
</script>

<div class="field">
  <span class="field-label" id={labelId}>{label}</span>
  <button
    type="button"
    class="trigger"
    aria-haspopup="dialog"
    aria-expanded={open}
    aria-labelledby="{labelId} {valueId}"
    onclick={() => (open = true)}
  >
    <span class="trigger-texts">
      <span class="trigger-value" class:placeholder={!selected} id={valueId}>
        {selected ? selected.label : placeholder}
      </span>
      {#if selected?.disabled}
        <!-- A choice saved before it became unavailable stays visible here, so
             it can be seen and edited away — never silently blanked or
             rewritten to some other option. -->
        <span class="trigger-unavailable">not available</span>
      {/if}
    </span>
    <Icon name="mdi:chevron-down" size={24} />
  </button>
</div>

<Sheet bind:open title={label}>
  <!-- tabindex=-1: the radiogroup role must be focusable per ARIA; -1 keeps
       it out of the tab order (the radios themselves are the tab stops). -->
  <div
    class="options"
    role="radiogroup"
    aria-label={label}
    tabindex="-1"
    bind:this={listEl}
    onkeydown={handleKeydown}
  >
    {#each options as option (option.value)}
      {#if option.disabled}
        <!-- Deliberately not a control: no radio, no label, no tab stop, so it
             cannot be chosen. The reason is the visible justification and is
             always rendered — an unexplained absence is not acceptable. -->
        <div class="row unavailable" aria-disabled="true">
          <Icon name="mdi:alert-circle-outline" size={24} />
          {#if option.icon}
            <Icon name={option.icon} size={24} />
          {/if}
          <span class="row-texts">
            <span class="row-label">{option.label}</span>
            <!-- The description still applies to an unavailable option (it is
                 what the option *is*), and the reason is why it cannot be
                 chosen. Both are rendered: dropping the description would lose
                 the answer's own pointer at a working alternative. -->
            {#if option.description}
              <span class="row-description">{option.description}</span>
            {/if}
            {#if option.reason}
              <span class="row-reason">{option.reason}</span>
            {/if}
          </span>
        </div>
      {:else}
        <label class="row" class:selected={value === option.value}>
          <input
            class="sr-only"
            type="radio"
            name={labelId}
            value={option.value}
            bind:group={value}
            onchange={() => (open = false)}
          />
          {#if option.icon}
            <Icon name={option.icon} size={24} />
          {/if}
          <span class="row-texts">
            <span class="row-label">{option.label}</span>
            {#if option.description}
              <span class="row-description">{option.description}</span>
            {/if}
          </span>
          {#if value === option.value}
            <Icon name="mdi:check" size={24} />
          {/if}
        </label>
      {/if}
    {/each}
  </div>
</Sheet>

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

  .trigger {
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
    font-size: var(--text-body);
    text-align: start;
    cursor: pointer;
    transition: background-color var(--motion-fast) var(--ease);
  }

  .trigger:hover {
    background: var(--surface-hover);
  }

  .trigger-texts {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .trigger-value {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .trigger-value.placeholder {
    color: var(--text-muted);
  }

  .trigger-unavailable {
    color: var(--text-muted);
    font-size: var(--text-label);
    line-height: 1.25;
  }

  .options {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  /* Picker rows get the 56px primary-action target. */
  .row {
    display: flex;
    align-items: center;
    gap: var(--gap);
    min-height: var(--tap-primary);
    padding: var(--gap-sm) var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius-sm);
    font-size: var(--text-body);
    cursor: pointer;
    transition: background-color var(--motion-fast) var(--ease);
  }

  .row:not(.selected):not(.unavailable):hover {
    background: var(--surface-hover);
  }

  /* An unavailable row is not a control: no pointer cursor and no hover tint,
     so it never invites a tap that cannot work. */
  .row.unavailable {
    color: var(--text-muted);
    cursor: default;
  }

  .row.selected {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--accent-contrast);
    font-weight: 600;
  }

  .row:has(:focus-visible) {
    outline: var(--focus-ring-width) solid var(--focus-ring);
    outline-offset: var(--focus-ring-offset);
  }

  .row-texts {
    display: flex;
    flex-direction: column;
    gap: 2px;
    flex: 1;
    min-width: 0;
  }

  .row-label,
  .row-description,
  .row-reason {
    overflow-wrap: anywhere;
  }

  .row-description {
    color: var(--text-muted);
    font-weight: 400;
    line-height: 1.35;
  }

  .row.selected .row-description {
    color: inherit;
  }

  .row-reason {
    color: var(--text-muted);
    font-weight: 400;
    line-height: 1.35;
  }

  .row.unavailable > :global(svg) {
    flex-shrink: 0;
  }
</style>
