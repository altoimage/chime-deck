<script lang="ts">
  import type { HTMLTextareaAttributes } from 'svelte/elements';
  import Icon from './Icon.svelte';
  import { nextComponentId } from './ids.js';

  interface Props {
    label: string;
    value?: string;
    rows?: number;
    placeholder?: string;
    hint?: string;
    error?: string;
    disabled?: boolean;
    required?: boolean;
    name?: string;
    /**
     * The on-screen keyboard D26 asks for. `Input` already takes one; a
     * multiline field needs it too, and without it a panel cannot state the
     * keyboard a phrase is typed on.
     */
    inputmode?: HTMLTextareaAttributes['inputmode'];
  }

  let {
    label,
    value = $bindable(''),
    rows = 4,
    placeholder,
    hint,
    error,
    disabled = false,
    required = false,
    name,
    inputmode
  }: Props = $props();

  const id = nextComponentId('textarea');
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;

  // autocorrect is valid HTML on textarea (Safari) but missing from Svelte's
  // attribute typings — spread it so the type-checker does not reject it.
  const autocorrectOff = { autocorrect: 'off' } as const;

  const describedby = $derived(
    [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(' ') || undefined
  );
</script>

<div class="field">
  <label class="field-label" for={id}>
    {label}
    {#if required}<span class="required" aria-hidden="true">*</span>{/if}
  </label>

  <textarea
    {id}
    {name}
    bind:value
    {rows}
    {placeholder}
    {disabled}
    {required}
    {inputmode}
    aria-invalid={error ? 'true' : undefined}
    aria-describedby={describedby}
    autocapitalize="none"
    {...autocorrectOff}
    spellcheck="false"
  ></textarea>

  {#if hint}
    <p class="hint" id={hintId}>{hint}</p>
  {/if}
  {#if error}
    <p class="error" id={errorId}>
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

  .field-label {
    font-size: var(--text-label);
    line-height: 1.25;
    font-weight: 600;
  }

  .field:has(textarea:disabled) .field-label {
    color: var(--text-muted);
  }

  .required {
    color: var(--state-alert);
  }

  textarea {
    width: 100%;
    min-height: calc(var(--tap) * 2); /* ~96px */
    padding: var(--gap-sm) var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: var(--text-body);
    line-height: 1.5;
    resize: vertical;
  }

  textarea::placeholder {
    color: var(--text-muted);
  }

  textarea:disabled {
    background: var(--bg);
    border-color: var(--border);
    color: var(--text-muted);
    cursor: default;
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
