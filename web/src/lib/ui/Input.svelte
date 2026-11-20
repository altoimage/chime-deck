<script lang="ts">
  import type { HTMLInputAttributes } from 'svelte/elements';
  import Icon from './Icon.svelte';
  import IconButton from './IconButton.svelte';
  import { nextComponentId } from './ids.js';

  interface Props {
    label: string;
    value?: string;
    type?: 'text' | 'password' | 'url' | 'email';
    inputmode?: HTMLInputAttributes['inputmode'];
    placeholder?: string;
    hint?: string;
    error?: string;
    disabled?: boolean;
    required?: boolean;
    /** Renders as password with a show/hide toggle; paste-friendly. */
    secret?: boolean;
    autocomplete?: HTMLInputAttributes['autocomplete'];
    name?: string;
  }

  let {
    label,
    value = $bindable(''),
    type = 'text',
    inputmode,
    placeholder,
    hint,
    error,
    disabled = false,
    required = false,
    secret = false,
    autocomplete,
    name
  }: Props = $props();

  // One id per instance from the shared module-level counter.
  const id = nextComponentId('input');
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;

  let revealed = $state(false);

  const actualType = $derived(secret ? (revealed ? 'text' : 'password') : type);
  const describedby = $derived(
    [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(' ') || undefined
  );

  function toggleRevealed(): void {
    revealed = !revealed;
  }

  /** Keep the press from moving focus out of the field. */
  function keepFocus(event: MouseEvent): void {
    event.preventDefault();
  }
</script>

<div class="field">
  <label class="field-label" for={id}>
    {label}
    {#if required}<span class="required" aria-hidden="true">*</span>{/if}
  </label>

  <div class="control">
    <input
      {id}
      type={actualType}
      {name}
      bind:value
      {inputmode}
      {placeholder}
      {disabled}
      {required}
      {autocomplete}
      aria-invalid={error ? 'true' : undefined}
      aria-describedby={describedby}
      autocapitalize="none"
      autocorrect="off"
      spellcheck="false"
    />
    {#if secret}
      <IconButton
        icon={revealed ? 'mdi:eye-off' : 'mdi:eye'}
        label={revealed ? 'Hide value' : 'Show value'}
        {disabled}
        onclick={toggleRevealed}
        onmousedown={keepFocus}
      />
    {/if}
  </div>

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

  .field:has(input:disabled) .field-label {
    color: var(--text-muted);
  }

  .required {
    color: var(--state-alert);
  }

  .control {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    min-height: var(--tap);
    padding-inline-end: var(--gap-sm);
    background: var(--surface);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
  }

  /* The ring moves to the wrapper for keyboard focus on the text field;
     the reveal button keeps its own focus-visible ring. */
  .control:has(input:focus-visible) {
    outline: var(--focus-ring-width) solid var(--focus-ring);
    outline-offset: var(--focus-ring-offset);
  }

  .control:has(input:disabled) {
    background: var(--bg);
    border-color: var(--border);
  }

  .control input {
    flex: 1;
    min-width: 0;
    min-height: var(--tap);
    padding: var(--gap-sm) var(--gap);
    border: none;
    background: transparent;
    color: var(--text);
    font-size: var(--text-body);
    line-height: 1.5;
  }

  .control input:focus-visible {
    outline: none;
  }

  .control input:disabled {
    color: var(--text-muted);
    cursor: default;
  }

  .control input::placeholder {
    color: var(--text-muted);
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
