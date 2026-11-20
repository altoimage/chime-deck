<!--
  Read-only debug card for one tile (design 05 §1/D26).

  NOT a <dialog> or Sheet on purpose: a tile's tap already means "toggle my
  selection" (D29), so this long-press card is a small peek at what the
  backend actually sent, not a modal task. A native dialog needs showModal(),
  which is awkward to drive in a DOM test, and D26 asks for a bottom-anchored
  card rather than a screen takeover. The card has no inputs, links or actions
  — only the facts and a way to close it — so it can never become a hidden
  control panel.

  Every state is printed as a word: the raw state, the icon source in the
  resolver's own words (so `fallback` and `default` cannot read the same) and
  the timestamp via the locale-free formatter.
-->
<script lang="ts">
  import type { EntityTile } from './api.js';
  import { formatStamp } from './clock.js';
  import { iconSourceLabel } from './tile-view.js';

  interface Props {
    tile: EntityTile;
    open?: boolean;
  }

  let { tile, open = $bindable(false) }: Props = $props();

  let cardEl: HTMLElement | undefined = $state();

  const stateText = $derived(
    tile.state === null || tile.state === '' ? 'not reported' : tile.state
  );
  const changedText = $derived(formatStamp(tile.lastChanged));
  const sourceText = $derived(iconSourceLabel(tile.iconSource));

  function close(): void {
    open = false;
  }

  /*
   * Escape is handled on the card itself; opening moves focus there so the
   * key arrives. A click outside also closes, but only while open — the
   * listener is added per open and removed in the teardown, so a closed
   * popover never leaves a window listener behind.
   *
   * `click` (not `pointerdown`) is the outside signal: by the time it fires
   * the long-press has finished, so nothing races the open. But the same
   * gesture that opened the card still delivers a `click` on the tile that
   * owns it (pointerup → click on the press target), so the owning tile's
   * subtree is excluded too: this popover is rendered inside the tile's
   * `<li>`, which makes that element `cardEl.parentElement`. A click anywhere
   * else — another tile, the page — still closes the card.
   */
  $effect(() => {
    if (open) {
      cardEl?.focus();
    }
  });

  $effect(() => {
    if (!open) {
      return;
    }
    function onWindowClick(event: MouseEvent): void {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }
      // Read here, not at effect setup: the owner only exists once the card is
      // bound, and the binding must not be captured as null.
      const owner = cardEl?.parentElement ?? null;
      const insideCard = cardEl?.contains(target) ?? false;
      if (insideCard || (owner !== null && owner.contains(target))) {
        return;
      }
      open = false;
    }
    window.addEventListener('click', onWindowClick);
    return () => window.removeEventListener('click', onWindowClick);
  });

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
    }
  }
</script>

{#if open}
  <div
    class="popover"
    role="dialog"
    aria-label="Entity details"
    aria-modal="false"
    tabindex="-1"
    bind:this={cardEl}
    onkeydown={onKeydown}
  >
    <dl class="facts">
      <div class="fact">
        <dt>Entity id</dt>
        <dd>{tile.entityId}</dd>
      </div>
      <div class="fact">
        <dt>State</dt>
        <dd>{stateText}</dd>
      </div>
      <div class="fact">
        <dt>Last changed</dt>
        <dd>{changedText}</dd>
      </div>
      <div class="fact">
        <dt>Icon</dt>
        <dd>{tile.icon}</dd>
      </div>
      <div class="fact">
        <dt>Icon source</dt>
        <dd>{sourceText}</dd>
      </div>
    </dl>

    <div class="actions">
      <button type="button" class="close" onclick={close}>Close</button>
    </div>
  </div>
{/if}

<style>
  /*
   * Fixed and bottom-anchored: on a wall panel the thumb lives at the bottom,
   * and the card must not move the grid underneath it.
   */
  .popover {
    position: fixed;
    inset-inline: 0;
    bottom: var(--gap);
    z-index: var(--z-sheet);
    width: min(420px, calc(100% - var(--gap) * 2));
    max-height: 70dvh;
    overflow-y: auto;
    margin-inline: auto;
    padding: var(--gap);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
    background: var(--surface);
    color: var(--text);
    box-shadow: var(--shadow-sheet);
  }

  .facts {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    margin: 0;
  }

  .fact {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  dt {
    font-size: var(--text-label);
    font-weight: 600;
    color: var(--text-muted);
  }

  dd {
    margin: 0;
    font-size: var(--text-body);
    overflow-wrap: anywhere;
  }

  .actions {
    display: flex;
    justify-content: flex-end;
    margin-top: var(--gap);
  }

  /* A word, not an icon-only affordance, and a full 48px target. */
  .close {
    min-width: var(--tap);
    min-height: var(--tap);
    padding: var(--gap-sm) var(--gap);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    background: var(--surface);
    color: var(--text);
    font-size: var(--text-label);
    font-weight: 600;
    cursor: pointer;
  }
</style>
