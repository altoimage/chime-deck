<!--
  The Entities section (design 05 §2.2; storyboards S07/S08/S09): a router over
  three sub-routes plus the reorder list itself.

  `rest` is the tail after `/settings/entities` and is the ONLY thing this panel
  routes on: `''` is the list, `'add'` the live picker and a plain number one
  row's editor. Anything else is an honest "No such entity" notice rather than a
  silent redirect back to the list — a hand-typed URL should say what happened.

  The list loads on mount and again every time the panel returns to it, which is
  how a tile the picker just created shows up without a page reload. The effect
  below reads `rest` and nothing else: reading `rows` (or anything `load()`
  assigns) would make each finished load re-trigger the effect and the spinner
  would never settle.

  Reordering is optimistic and posts exactly once per gesture — `SortableList`
  already guarantees one call per drop and one per ▲/▼ tap — so this panel never
  issues a second request of its own. A rejected reorder restores the previous
  order and prints the server's sentence verbatim, because a list that kept an
  order the backend refused would lie about what is stored. Each list row also
  carries its delete action; the panel owns one confirmation dialog.
-->
<script lang="ts">
  import {
    ApiError,
    deleteEntity,
    listEntityRows,
    reorderEntities,
    toEntityTileFromResponse,
    type EntityResponse
  } from '../api.js';
  import { iconSourceLabel, tileView, type TileView } from '../tile-view.js';
  import Button from '../ui/Button.svelte';
  import Dialog from '../ui/Dialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import Icon from '../ui/Icon.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import SortableList from '../ui/SortableList.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import { toastStore } from '../ui/toast.svelte.js';
  import EntityEditor from './EntityEditor.svelte';
  import EntityPicker from './EntityPicker.svelte';

  interface Props {
    /** The sub-route tail after `/settings/entities`: `''`, `'add'` or a row id. */
    rest?: string;
    /** Move to another settings path without a page reload. */
    navigate?: (path: string) => void;
  }

  const LIST_PATH = '/settings/entities';
  const ADD_PATH = '/settings/entities/add';

  /** Stand-in navigator, so a preview or a test can mount the panel on its own. */
  function stay(): void {}

  let { rest = '', navigate = stay }: Props = $props();

  type PanelMode = 'list' | 'picker' | 'editor' | 'missing';

  /** Which panel a sub-route tail names. */
  function modeFor(tail: string): PanelMode {
    if (tail === '') {
      return 'list';
    }
    if (tail === 'add') {
      return 'picker';
    }
    if (/^\d+$/.test(tail)) {
      return 'editor';
    }
    return 'missing';
  }

  const mode = $derived(modeFor(rest));
  const editingId = $derived(mode === 'editor' ? Number(rest) : 0);

  /** `null` means "no answer yet" — the spinner, never an empty list. */
  let rows = $state<EntityResponse[] | null>(null);
  let loadError = $state<string | null>(null);
  let saving = $state(false);
  let reorderError = $state<string | null>(null);
  let deleteTarget = $state<EntityResponse | null>(null);
  let deleteOpen = $state(false);
  let deleting = $state(false);
  let deleteError = $state<string | null>(null);

  /** A non-null view of `rows`, so a row snippet can index it without widening. */
  const list = $derived(rows ?? []);

  /**
   * Custom-file thumbnails that failed to load, keyed by row id. `$state` so the
   * question-mark fallback appears when the image errors; `Tile.svelte` keeps the
   * same per-tile flag.
   */
  let failedCustomIcons = $state<Record<number, boolean>>({});

  /** Guards against an older list answer overwriting a newer one. */
  let loadSeq = 0;

  async function load(): Promise<void> {
    loadSeq += 1;
    const seq = loadSeq;
    rows = null;
    loadError = null;
    reorderError = null;
    deleteError = null;
    try {
      const answer = await listEntityRows();
      if (seq !== loadSeq) {
        return;
      }
      rows = answer;
    } catch (cause) {
      if (seq !== loadSeq) {
        return;
      }
      loadError = cause instanceof ApiError ? cause.message : 'the entity list could not be loaded';
    }
  }

  /**
   * Whether an earlier effect run already loaded the list. A plain variable on
   * purpose: it is bookkeeping, not something the view renders.
   */
  let listRequested = false;

  $effect(() => {
    const tail = rest;
    // `tail` is read unconditionally, so this effect depends on `rest` alone:
    // that is what reloads the list when the picker navigates back to it.
    if (listRequested && tail !== '') {
      return;
    }
    listRequested = true;
    void load();
  });

  /** The name a row shows and a screen reader hears. */
  function rowName(row: EntityResponse): string {
    return row.display_name ?? row.entity_id;
  }

  /** How the row's tile reads: the dashboard's own rule, never a second copy. */
  function rowView(row: EntityResponse): TileView {
    return tileView(toEntityTileFromResponse(row));
  }

  function askDelete(row: EntityResponse): void {
    deleteTarget = row;
    deleteOpen = true;
  }

  /** Delete only after the named confirmation has been accepted. */
  async function doDelete(): Promise<void> {
    const target = deleteTarget;
    if (target === null || deleting) {
      return;
    }
    deleting = true;
    deleteError = null;
    try {
      await deleteEntity(target.id);
      toastStore.push('success', 'Tile deleted.');
      deleteTarget = null;
      void load();
    } catch (cause) {
      deleteError = cause instanceof ApiError ? cause.message : 'the tile could not be deleted';
      toastStore.push('error', deleteError);
      deleteTarget = null;
    } finally {
      deleting = false;
    }
  }

  function cancelDelete(): void {
    deleteTarget = null;
  }

  function isCustomIcon(icon: string): boolean {
    return icon.startsWith('custom:');
  }

  /** The file name inside a `custom:` reference (meaningful only when custom). */
  function customIconFile(icon: string): string {
    return icon.slice('custom:'.length);
  }

  /**
   * Apply the new key order optimistically, then post it once.
   *
   * Keys the callback did not mention are kept at the end rather than dropped —
   * a reorder must never make a tile disappear from the list.
   */
  async function reorder(keys: (string | number)[]): Promise<void> {
    if (saving || rows === null) {
      return;
    }
    const previous = rows;
    const byId = new Map(previous.map((row) => [row.id, row]));
    const next: EntityResponse[] = [];
    for (const key of keys) {
      const found = byId.get(Number(key));
      if (found !== undefined) {
        next.push(found);
        byId.delete(found.id);
      }
    }
    for (const leftover of byId.values()) {
      next.push(leftover);
    }

    rows = next;
    saving = true;
    reorderError = null;
    try {
      await reorderEntities(next.map((row) => row.id));
      toastStore.push('success', 'Order saved.');
    } catch (cause) {
      rows = previous;
      reorderError = cause instanceof ApiError ? cause.message : 'the order could not be saved';
      toastStore.push('error', reorderError);
    } finally {
      saving = false;
    }
  }
</script>

{#if mode === 'picker'}
  <EntityPicker onclose={() => navigate(LIST_PATH)} />
{:else if mode === 'editor'}
  <EntityEditor id={editingId} onback={() => navigate(LIST_PATH)} />
{:else if mode === 'missing'}
  <section class="notice">
    <h2>No such entity</h2>
    <p>There is no entity at “{rest}”.</p>
    <div class="actions">
      <Button onclick={() => navigate(LIST_PATH)}>Back to entities</Button>
    </div>
  </section>
{:else}
  <div class="panel">
    <header class="head">
      <h2>Entities</h2>
      <Button variant="primary" size="lg" disabled={saving} onclick={() => navigate(ADD_PATH)}>
        Add entities
      </Button>
    </header>

    {#if loadError !== null}
      <p class="error" role="alert">{loadError}</p>
      <div class="actions">
        <Button onclick={() => void load()}>Retry</Button>
      </div>
    {:else if rows === null}
      <Spinner label="Loading entities" />
    {:else if list.length === 0}
      <EmptyState
        title="No entity tiles yet"
        description="Add entities before changing their order."
      >
        <Button variant="primary" size="lg" onclick={() => navigate(ADD_PATH)}>Add entities</Button>
      </EmptyState>
    {:else}
      <p class="prose">Drag the handle for a moment, or use the arrows.</p>

      {#if reorderError !== null}
        <p class="error" role="alert">{reorderError}</p>
      {/if}
      {#if deleteError !== null}
        <p class="error" role="alert">{deleteError}</p>
      {/if}
      {#if saving}
        <Spinner label="Saving the order" />
      {/if}

      {#snippet entityRow(originalIndex: number)}
        {@const item = list[originalIndex]}
        {@const view = rowView(item)}
        {@const customIcon = isCustomIcon(item.icon)}

        <div class="row-main">
          <button
            type="button"
            class="open"
            aria-label={`Edit ${rowName(item)}`}
            onclick={() => navigate(`${LIST_PATH}/${item.id}`)}
          >
            <span class="row-icon">
              {#if !customIcon}
                <Icon name={item.icon} size={24} />
              {:else if failedCustomIcons[item.id] === true}
                <span data-custom-icon-failed="true">
                  <Icon name="mdi:help-circle-outline" size={24} />
                </span>
              {:else}
                <img
                  class="custom-icon"
                  src={`/custom-icons/${encodeURIComponent(customIconFile(item.icon))}`}
                  alt=""
                  data-icon-source="custom"
                  onerror={() => {
                    failedCustomIcons[item.id] = true;
                  }}
                />
              {/if}
            </span>

            <span class="texts">
              <span class="name">{rowName(item)}</span>
              <span class="entity-id">{item.entity_id}</span>
              <!-- Word and unit are separate elements, exactly as the tile draws them. -->
              <span class="state">
                <span class="word">{view.word}</span>
                {#if view.unit}
                  <span class="unit">{view.unit}</span>
                {/if}
              </span>
              <span class="source">{iconSourceLabel(item.icon_source)}</span>
            </span>
          </button>

          <div class="row-actions">
            <IconButton
              label={`Delete ${rowName(item)}`}
              icon="mdi:delete-outline"
              variant="danger"
              disabled={deleting}
              onclick={() => askDelete(item)}
            />
          </div>
        </div>

        {#if item.warning !== null}
          <p class="warning" role="status">{item.warning}</p>
        {/if}
      {/snippet}

      <SortableList
        items={list.map((row) => ({ key: row.id, label: rowName(row) }))}
        disabled={saving}
        onreorder={reorder}
        row={entityRow}
        handlelabel="Reorder"
      />
    {/if}
  </div>

  {#if deleteTarget !== null}
    <Dialog
      bind:open={deleteOpen}
      title={`Delete “${rowName(deleteTarget)}”?`}
      message={`${deleteTarget.entity_id} will be removed from the panel, and its Home Assistant selection sensor will be removed too. This cannot be undone — you can add the tile again from Add entities.`}
      confirmLabel="Delete tile"
      destructive
      onconfirm={() => void doDelete()}
      oncancel={cancelDelete}
    />
  {/if}
{/if}

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap);
  }

  h2 {
    margin: 0;
    font-size: var(--text-title);
    line-height: 1.2;
    font-weight: 700;
  }

  .prose {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    color: var(--text-muted);
  }

  .error {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    color: var(--state-alert);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }

  .notice {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    padding: calc(var(--gap) * 1.5);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
  }

  .notice p {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
  }

  /* The row body is the tap target for editing: 56px tall, full width. */
  .row-main {
    display: flex;
    align-items: center;
    gap: var(--gap);
    min-width: 0;
  }

  .open {
    display: flex;
    flex: 1;
    align-items: center;
    gap: var(--gap-sm);
    width: 100%;
    min-width: 0;
    min-height: var(--tap-primary);
    padding: 0;
    border: none;
    background: transparent;
    color: var(--text);
    text-align: start;
    cursor: pointer;
  }

  .row-icon {
    display: inline-flex;
    flex-shrink: 0;
    color: var(--text);
  }

  .row-actions {
    display: flex;
    align-items: center;
    gap: var(--gap);
    flex: none;
  }

  .custom-icon {
    width: 24px;
    height: 24px;
    object-fit: contain;
  }

  .texts {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .name {
    font-size: var(--text-body);
    font-weight: 600;
    line-height: 1.3;
    overflow-wrap: anywhere;
  }

  .entity-id,
  .source {
    font-size: var(--text-label);
    line-height: 1.35;
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }

  .state {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: var(--gap-sm);
    font-size: var(--text-label);
    line-height: 1.35;
    color: var(--text-muted);
  }

  .word {
    font-weight: 600;
  }

  /* The unit is its own span, never glued onto the word. */
  .unit {
    font-size: var(--text-label);
  }

  .warning {
    margin: 0;
    font-size: var(--text-label);
    line-height: 1.35;
    color: var(--state-warn);
    overflow-wrap: anywhere;
  }
</style>
