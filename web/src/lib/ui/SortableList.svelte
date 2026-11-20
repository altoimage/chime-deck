<!--
  The shared reorder list (HSD-umw; HSD-wnkm/HSD-w9eq/HSD-l22f reuse it).

  Three ways to reorder, one of them accessible and always visible: the ▲/▼
  buttons. A long-press on the grab handle is the D26 enhancement on top — a tap
  on it does nothing, so a mis-tap can never move a row — and the pointer drag
  adds no ARIA at all, because a screen-reader user already has the buttons.

  `row` receives the **original** index into `items`, never the display position:
  while a drag is in flight the rendered order differs from `items`, and a caller
  that got a display position would render the wrong row's body from its own
  array. So the display order is built as a list of original indices and the
  snippet is handed those.

  `onreorder` is called ONCE per finished gesture with the whole new key order —
  never per movement, never with a partial move — so a drag across five rows is
  one request and no half-applied reorder can be observed.
-->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { moveItem, reorderKeys, targetIndexFor, type SortableItem } from '../reorder.js';
  import Icon from './Icon.svelte';
  import IconButton from './IconButton.svelte';
  import { longPressDrag } from './drag.js';

  interface Props {
    items: readonly SortableItem[];
    disabled?: boolean;
    onreorder: (keys: (string | number)[]) => void | Promise<void>;
    /** The row body for the item at that ORIGINAL index in `items`. */
    row: Snippet<[number]>;
    /** Accessible name prefix for the grab handle. */
    handlelabel?: string;
  }

  let { items, disabled = false, onreorder, row, handlelabel = 'Reorder' }: Props = $props();

  let dragFrom = $state<number | null>(null);
  let dragTo = $state<number | null>(null);
  let listEl: HTMLOListElement | undefined = $state();

  /*
   * The boxes measured when the drag started. Kept across the whole gesture on
   * purpose: the rows move under the finger as the order changes, so re-measuring
   * mid-drag would make the target chase the pointer.
   */
  let boxes: { top: number; bottom: number }[] = [];

  const displayOrder = $derived(
    dragFrom !== null && dragTo !== null
      ? moveItem(
          items.map((_item, index) => index),
          dragFrom,
          dragTo
        )
      : items.map((_item, index) => index)
  );

  const movingDown = $derived(dragFrom !== null && dragTo !== null && dragTo > dragFrom);

  function measure(): void {
    if (listEl === undefined) {
      boxes = [];
      return;
    }
    boxes = Array.from(listEl.children).map((child) => {
      const rect = child.getBoundingClientRect();
      return { top: rect.top, bottom: rect.bottom };
    });
  }

  function startDrag(index: number): void {
    if (disabled) {
      return;
    }
    measure();
    dragFrom = index;
    dragTo = index;
  }

  function moveDrag(y: number): void {
    if (dragFrom === null) {
      return;
    }
    dragTo = targetIndexFor(y, boxes);
  }

  function endDrag(): void {
    const from = dragFrom;
    const to = dragTo;
    dragFrom = null;
    dragTo = null;
    boxes = [];
    if (from === null || to === null || from === to) {
      return;
    }
    void onreorder(reorderKeys(items, from, to));
  }

  function moveBy(index: number, target: number): void {
    if (disabled) {
      return;
    }
    void onreorder(reorderKeys(items, index, target));
  }
</script>

<ol class="rows" bind:this={listEl}>
  {#each displayOrder as originalIndex, position (items[originalIndex].key)}
    <li
      class="row"
      data-dragging={dragFrom === originalIndex ? 'true' : undefined}
      class:insert-above={dragFrom !== null && dragTo !== null && position === dragTo && dragFrom !== dragTo && !movingDown}
      class:insert-below={dragFrom !== null && dragTo !== null && position === dragTo && dragFrom !== dragTo && movingDown}
    >
      <!-- The handle is a real button: harmless on tap, keyboard-focusable, and
           named so a screen-reader user knows what it is (the drag itself is
           pointer-only, which is why it adds no ARIA). -->
      <button
        type="button"
        class="handle"
        aria-label={`${handlelabel} ${items[originalIndex].label}`}
        {disabled}
        use:longPressDrag={disabled
          ? undefined
          : {
              onstart: () => startDrag(originalIndex),
              onmove: ({ y }) => moveDrag(y),
              onend: () => endDrag()
            }}
      >
        <Icon name="mdi:drag-vertical" size={24} />
      </button>

      <div class="body">
        {@render row(originalIndex)}
      </div>

      <div class="actions">
        <IconButton
          icon="mdi:chevron-up"
          label={`Move ${items[originalIndex].label} up`}
          disabled={disabled || originalIndex === 0}
          onclick={() => moveBy(originalIndex, originalIndex - 1)}
        />
        <IconButton
          icon="mdi:chevron-down"
          label={`Move ${items[originalIndex].label} down`}
          disabled={disabled || originalIndex === items.length - 1}
          onclick={() => moveBy(originalIndex, originalIndex + 1)}
        />
      </div>
    </li>
  {/each}
</ol>

<style>
  .rows {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .row {
    position: relative;
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    min-height: var(--tap);
    padding: var(--gap-sm);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius-sm);
  }

  .row[data-dragging='true'] {
    z-index: 1;
    background: var(--surface-hover);
    box-shadow: var(--shadow-sheet);
  }

  .handle {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    width: var(--tap);
    height: var(--tap);
    padding: 0;
    border: none;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text-muted);
    /* The browser must not claim the gesture for scrolling (see drag.ts). */
    touch-action: none;
    cursor: grab;
  }

  .handle:disabled {
    color: var(--border);
    cursor: default;
  }

  .body {
    flex: 1;
    min-width: 0;
  }

  .actions {
    display: flex;
    flex-shrink: 0;
  }

  /* Decoration only: 1px where the row will land, never a drop target itself. */
  .insert-above::before,
  .insert-below::after {
    content: '';
    position: absolute;
    inset-inline: 0;
    height: 1px;
    background: var(--accent);
    pointer-events: none;
  }

  .insert-above::before {
    top: -1px;
  }

  .insert-below::after {
    bottom: -1px;
  }
</style>
