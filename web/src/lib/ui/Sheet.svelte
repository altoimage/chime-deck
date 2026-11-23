<script lang="ts">
  import type { Snippet } from 'svelte';
  import IconButton from './IconButton.svelte';
  import { nextComponentId } from './ids.js';

  interface Props {
    open?: boolean;
    title: string;
    description?: string;
    children: Snippet;
    footer?: Snippet;
    onclose?: () => void;
  }

  let { open = $bindable(false), title, description, children, footer, onclose }: Props = $props();

  const titleId = nextComponentId('sheet-title');
  const descriptionId = `${titleId}-description`;

  let dialogEl: HTMLDialogElement | undefined = $state();

  /*
   * Keep the native <dialog> in sync with the bindable `open` prop.
   * showModal() gives the top layer, a focus trap, Escape and focus restore
   * for free. Guard both directions so a redundant call can never throw.
   */
  $effect(() => {
    const el = dialogEl;
    if (!el) {
      return;
    }
    if (open && !el.open) {
      el.showModal();
    } else if (!open && el.open) {
      el.close();
    }
  });

  /**
   * Keep the keyboard inside a modal that has exactly ONE tab stop.
   *
   * `showModal()` already makes the rest of the page inert, but Chromium still
   * walks focus out of the dialog and parks it on `<body>` (and then on the
   * browser's own UI) when the modal's only tab stop is already focused. A
   * dialog whose whole body is read-only — the header's connection-status
   * sheet, say — has exactly one, so Tab would silently leave it. Wrapping that
   * single tab stop onto itself is the trap the dialog was opened for, and it
   * also pulls focus back in if it is already on the body.
   *
   * Guarded by `length === 1`: every sheet with two or more tab stops keeps the
   * browser's native cycling, so this cannot change those.
   */
  function handleKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Tab' || !dialogEl?.open) {
      return;
    }
    const focusable = dialogEl.querySelectorAll<HTMLElement>(
      'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled),' +
        ' textarea:not(:disabled), summary, [contenteditable="true"],' +
        ' [tabindex]:not([tabindex="-1"])'
    );
    if (focusable.length !== 1) {
      return;
    }
    event.preventDefault();
    focusable[0].focus();
  }

  function handleCancel(event: Event): void {
    // Escape fires `cancel`; route it through close() so every exit path
    // (Escape, backdrop, close button, parent prop) funnels into `close`.
    event.preventDefault();
    if (dialogEl?.open) {
      dialogEl.close();
    }
  }

  function handleClose(): void {
    open = false;
    onclose?.();
  }

  function handleBackdropClick(event: MouseEvent): void {
    // Clicks on the ::backdrop retarget to the <dialog> itself; clicks on
    // sheet content have an inner element as their target.
    if (event.target === dialogEl) {
      dialogEl?.close();
    }
  }

  function requestClose(): void {
    dialogEl?.close();
  }
</script>

<dialog
  bind:this={dialogEl}
  class="sheet"
  aria-labelledby={titleId}
  aria-describedby={description ? descriptionId : undefined}
  oncancel={handleCancel}
  onkeydown={handleKeydown}
  onclose={handleClose}
  onclick={handleBackdropClick}
>
  <div class="sheet-header">
    <div class="sheet-heading">
      <h2 class="sheet-title" id={titleId}>{title}</h2>
      {#if description}
        <p class="sheet-description" id={descriptionId}>{description}</p>
      {/if}
    </div>
    <IconButton icon="mdi:close" label="Close" onclick={requestClose} />
  </div>

  <div class="sheet-body">
    {@render children()}
  </div>

  {#if footer}
    <div class="sheet-footer">
      {@render footer()}
    </div>
  {/if}
</dialog>

<style>
  /*
   * D26: sheets, not modals. Full-screen at ≤1024px wide; a bottom sheet at
   * ≥1025px wide and ≥700px tall. The UA's dialog:modal rule caps
   * max-width/max-height, so both are overridden explicitly.
   */
  .sheet {
    width: 100%;
    max-width: none;
    height: var(--viewport-height);
    max-height: var(--viewport-height);
    margin: 0;
    padding: 0;
    border: none;
    background: var(--surface);
    color: var(--text);
  }

  .sheet[open] {
    display: flex;
    flex-direction: column;
  }

  .sheet:not([open]) {
    display: none;
  }

  .sheet::backdrop {
    background: var(--scrim);
  }

  @media (min-width: 1025px) and (min-height: 700px) {
    .sheet {
      width: min(600px, 100%);
      max-width: min(600px, 100%);
      height: auto;
      max-height: calc(var(--viewport-height) * 0.8);
      margin: auto auto 0;
      border-radius: var(--radius) var(--radius) 0 0;
      box-shadow: var(--shadow-sheet);
    }
  }

  .sheet-header {
    position: relative;
    z-index: 1;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap);
    flex-shrink: 0;
    min-height: var(--tap-primary);
    padding:
      calc(var(--gap) + env(safe-area-inset-top))
      calc(var(--gap) + env(safe-area-inset-right))
      var(--gap)
      calc(var(--gap) + env(safe-area-inset-left));
    background: var(--surface);
    border-bottom: var(--border-width) solid var(--border);
  }

  .sheet-heading {
    min-width: 0;
  }

  .sheet-title {
    margin: 0;
    font-size: var(--text-title);
    font-weight: 700;
  }

  .sheet-description {
    margin: var(--gap-sm) 0 0;
    font-size: var(--text-body);
    color: var(--text-muted);
  }

  .sheet-body {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
    scroll-padding-block: var(--gap);
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    padding: var(--gap);
    padding-inline-start: calc(var(--gap) + env(safe-area-inset-left));
    padding-inline-end: calc(var(--gap) + env(safe-area-inset-right));
  }

  .sheet-footer {
    position: relative;
    z-index: 1;
    flex-shrink: 0;
    padding: var(--gap);
    padding-bottom: calc(var(--gap) + env(safe-area-inset-bottom));
    background: var(--surface);
    border-top: var(--border-width) solid var(--border);
  }
</style>
