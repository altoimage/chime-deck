<!--
  A selectable status tile. A tap toggles selection; the Dashboard owns any
  network call. Long-press (400 ms, or the platform gesture delivered as
  `contextmenu`) opens the read-only debug popover. Shift+Enter/Space keeps
  that popover reachable by keyboard without stealing native toggle keys.

  Why the word and the unit are separate elements: `tile-view.ts` decides both,
  and a screen reader must hear the state word separately from its unit. The
  `Stale` state remains a visible chip because opacity alone is not a signal.

  Why the popover is not a <dialog>: see TilePopover.svelte.
-->
<script lang="ts">
  import type { EntityTile } from './api.js';
  import { tileView } from './tile-view.js';
  import Icon from './ui/Icon.svelte';
  import TilePopover from './TilePopover.svelte';

  interface Props {
    tile: EntityTile;
    stale?: boolean;
    /**
     * Whether a press can open the debug popover. The dashboard wants it; a
     * preview inside the editor does not — a preview that swallowed a long-press
     * would look interactive while saving nothing.
     */
    interactive?: boolean;
    /** Called when the dashboard should toggle this tile's selection. */
    ontoggle?: (tile: EntityTile) => void;
  }

  let { tile, stale = false, interactive = true, ontoggle }: Props = $props();

  /** How long a press must last to count as a long-press (design 05 §8). */
  const LONG_PRESS_MS = 400;

  /** Movement beyond this many pixels is a scroll, not a long-press. */
  const MOVE_TOLERANCE_PX = 10;

  const view = $derived(tileView(tile));

  const name = $derived(tile.displayName ?? tile.entityId);
  const label = $derived(
    `${name}, ${view.word}${tile.selected ? ', selected' : ', not selected'}${stale ? ', not live' : ''}`
  );
  const secondary = $derived(
    view.kind === 'unavailable' || view.kind === 'unknown' ? null : (tile.attributeHints[0] ?? null)
  );

  const isCustomIcon = $derived(tile.icon.startsWith('custom:'));
  const customIconName = $derived(tile.icon.slice('custom:'.length));
  let customIconFailed = $state(false);

  let popoverOpen = $state(false);
  let pressTimer: ReturnType<typeof setTimeout> | null = null;
  let originX = 0;
  let originY = 0;
  let longPressFired = false;
  let moved = false;

  function clearPressTimer(): void {
    if (pressTimer !== null) {
      clearTimeout(pressTimer);
      pressTimer = null;
    }
  }

  /*
   * No setPointerCapture: capturing would steal the pointer from a scrolling
   * list. The move tolerance below is what distinguishes a scroll from a
   * deliberate press.
   */
  function onPointerDown(event: PointerEvent): void {
    longPressFired = false;
    moved = false;
    if (event.button !== 0) {
      return;
    }
    clearPressTimer();
    originX = event.clientX;
    originY = event.clientY;
    pressTimer = setTimeout(() => {
      pressTimer = null;
      longPressFired = true;
      popoverOpen = true;
    }, LONG_PRESS_MS);
  }

  function cancelPress(): void {
    clearPressTimer();
  }

  function onPointerCancel(): void {
    // Scrolling can cancel a touch pointer; suppress any click the browser still emits.
    moved = true;
    clearPressTimer();
  }

  function onPointerLeave(event: PointerEvent): void {
    if (pressTimer !== null) {
      const dx = Math.abs(event.clientX - originX);
      const dy = Math.abs(event.clientY - originY);
      if (dx > MOVE_TOLERANCE_PX || dy > MOVE_TOLERANCE_PX) {
        moved = true;
      }
    }
    clearPressTimer();
  }

  function onPointerMove(event: PointerEvent): void {
    if (pressTimer === null) {
      return;
    }
    const dx = Math.abs(event.clientX - originX);
    const dy = Math.abs(event.clientY - originY);
    if (dx > MOVE_TOLERANCE_PX || dy > MOVE_TOLERANCE_PX) {
      moved = true;
      clearPressTimer();
    }
  }

  /*
   * On touch the platform long-press arrives as `contextmenu`. Suppressing the
   * browser menu and opening our own popover is the same gesture with the same
   * meaning — it is never a shortcut to a browser action.
   */
  function onContextMenu(event: MouseEvent): void {
    event.preventDefault();
    clearPressTimer();
    longPressFired = true;
    popoverOpen = true;
  }

  function onClick(): void {
    if (longPressFired || moved) {
      // The browser still sends a click after a long-press or scroll; swallow it rather than toggling.
      longPressFired = false;
      moved = false;
      return;
    }
    ontoggle?.(tile);
  }

  function onKeydown(event: KeyboardEvent): void {
    if (!event.shiftKey || (event.key !== 'Enter' && event.key !== ' ')) {
      return;
    }
    // WCAG 2.1.1: keep the debug popover reachable by keyboard alone while plain Enter/Space toggle natively.
    event.preventDefault();
    popoverOpen = true;
  }

  // The timer must not outlive the tile (unmount during a press).
  $effect(() => {
    if (!interactive) {
      return;
    }
    return () => clearPressTimer();
  });
</script>

<li
  class="tile"
  data-kind={view.kind}
  data-stale={stale}
  data-entity={tile.entityId}
  data-selected={tile.selected ? 'true' : 'false'}
>
  {#snippet cardContents()}
    <span class="icon" data-kind={view.kind}>
      {#if isCustomIcon}
        {#if customIconFailed}
          <span data-custom-icon-failed="true">
            <Icon name="mdi:help-circle-outline" size={56} />
          </span>
        {:else}
          <!-- `/custom-icons` ships in HSD-i7pw; until then this 404s and the
               fallback above is the point, not a broken image box. -->
          <img
            class="custom-icon"
            src={`/custom-icons/${encodeURIComponent(customIconName)}`}
            alt=""
            data-icon-source="custom"
            onerror={() => {
              customIconFailed = true;
            }}
          />
        {/if}
      {:else}
        <Icon name={tile.icon} size={56} />
      {/if}
    </span>

    <span class="name">{name}</span>

    <span class="state" data-kind={view.kind}>
      <span class="word">{view.word}</span>
      {#if view.unit}
        <span class="unit">{view.unit}</span>
      {/if}
    </span>

    {#if secondary}
      <span class="secondary">{secondary.label}: {secondary.value}</span>
    {/if}

    {#if stale}
      <span class="stale">Stale</span>
    {/if}

    {#if tile.selected}
      <span class="selection-cue" data-testid="tile-selection-cue" aria-hidden="true">
        <Icon name="mdi:check" size={32} />
      </span>
    {/if}
  {/snippet}

  {#if interactive}
    <button
      type="button"
      class="card"
      aria-label={label}
      aria-pressed={tile.selected}
      onpointerdown={onPointerDown}
      onpointerup={cancelPress}
      onpointercancel={onPointerCancel}
      onpointerleave={onPointerLeave}
      onpointermove={onPointerMove}
      oncontextmenu={onContextMenu}
      onkeydown={onKeydown}
      onclick={onClick}
    >
      {@render cardContents()}
    </button>
    <TilePopover {tile} bind:open={popoverOpen} />
  {:else}
    <div class="card">
      {@render cardContents()}
    </div>
  {/if}
</li>

<style>
  .tile {
    /* The grid item has definite tracks, so it can safely contain its own size. */
    container-type: size;
    list-style: none;
    min-width: 0;
    min-height: 0;
    border-radius: var(--radius);
  }

  .card {
    /*
     * Every size below is a fraction of THIS tile's own box, floored at a legible
     * minimum and with no px upper bound: `max(floor, min(cqA, cqB))`. The `min()`
     * of two container units is the ceiling, and it is a fraction of the tile, so
     * a bigger tile renders bigger content all the way to 4K and a nine-up
     * arrangement stays compact. This layout is intentionally resolution- and
     * aspect-ratio-agnostic: a fixed breakpoint, an `aspect-ratio`, or a px
     * ceiling here is a regression, not a tidy-up.
     */
    --tile-icon: max(32px, min(80cqmin, 44cqb));
    --tile-name: max(16px, min(5.5cqi, 7cqb));
    --tile-state: max(20px, min(17.5cqi, 20cqb));
    --tile-unit: max(16px, min(5.5cqi, 7cqb));
    --tile-secondary: max(16px, min(4.5cqi, 5.5cqb));

    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: clamp(2px, 1.2cqb, 14px);
    width: 100%;
    block-size: 100%;
    height: 100%;
    box-sizing: border-box;
    min-height: var(--tap-primary);
    overflow: hidden;
    padding: clamp(3px, 1.5cqb, 20px) clamp(12px, 4cqi, 48px);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
    appearance: none;
    background: var(--surface);
    color: inherit;
    font: inherit;
    text-align: center;
    transition: box-shadow var(--motion-fast) var(--ease);
  }

  button.card {
    cursor: pointer;
  }

  .card:focus-visible {
    outline: var(--focus-ring-width) solid var(--focus-ring);
    outline-offset: var(--focus-ring-offset);
  }

  .tile[data-selected='true'] .card {
    box-shadow: inset 0 0 0 3px var(--accent);
  }

  .selection-cue {
    position: absolute;
    top: 8px;
    inset-inline-end: 8px;
    display: grid;
    place-items: center;
    inline-size: clamp(48px, 12cqmin, 72px);
    block-size: clamp(48px, 12cqmin, 72px);
    border-radius: 50%;
    background: var(--accent);
    color: var(--accent-contrast);
    pointer-events: none;
    z-index: 1;
  }

  .selection-cue :global(svg) {
    inline-size: clamp(32px, 8cqmin, 48px);
    block-size: clamp(32px, 8cqmin, 48px);
  }

  /*
   * Kind → colour. The word next to every colour is what actually carries the
   * state; these custom properties only reinforce it.
   */
  .tile[data-kind='ok'] {
    --tile-color: var(--state-ok);
  }

  .tile[data-kind='warn'] {
    --tile-color: var(--state-warn);
  }

  .tile[data-kind='alert'] {
    --tile-color: var(--state-alert);
  }

  .tile[data-kind='off'] {
    --tile-color: var(--state-off);
  }

  .tile[data-kind='unknown'] {
    --tile-color: var(--text-muted);
  }

  .tile[data-kind='unavailable'] {
    --tile-color: var(--text-muted);
  }

  .tile[data-kind='value'] {
    --tile-color: var(--text);
  }

  .icon {
    display: grid;
    inline-size: 100%;
    place-items: center;
    color: var(--tile-color, var(--text));
  }

  .icon :global(svg),
  .custom-icon {
    inline-size: min(var(--tile-icon), 100%);
    block-size: min(var(--tile-icon), 100%);
  }

  .custom-icon {
    object-fit: contain;
  }

  .name {
    font-size: var(--tile-name);
    font-weight: 600;
    line-height: 1.1;
    overflow-wrap: anywhere;
  }

  .state {
    display: flex;
    align-items: baseline;
    justify-content: center;
    flex-wrap: wrap;
    gap: var(--gap-sm);
    color: var(--tile-color, var(--text));
  }

  .word {
    font-size: var(--tile-state);
    font-weight: 700;
    line-height: 1.1;
  }

  /* The unit is its own readable span, never glued onto the word. */
  .unit {
    font-size: var(--tile-unit);
    font-weight: 600;
    line-height: 1.1;
    color: var(--text-muted);
  }

  .secondary {
    font-size: var(--tile-secondary);
    line-height: 1.1;
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }

  /* A visible word, not a colour/opacity-only hint. */
  .stale {
    padding: 1px var(--gap-sm);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    font-size: max(16px, min(3.5cqi, 4.5cqb));
    font-weight: 600;
    line-height: 1;
    color: var(--text-muted);
  }

  /* Stale also dims what is no longer live — the chip above is the real signal. */
  .tile[data-stale='true'] .icon,
  .tile[data-stale='true'] .state {
    opacity: 0.6;
  }

  /* Unavailable: a muted surface plus a diagonal hatch, never a fake reading. */
  .tile[data-kind='unavailable'] .card {
    background-image: repeating-linear-gradient(
      135deg,
      color-mix(in srgb, var(--border) 55%, transparent) 0 6px,
      color-mix(in srgb, var(--text-muted) 10%, transparent) 6px 12px
    );
  }

</style>
