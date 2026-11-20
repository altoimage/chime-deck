<!--
  The Add-entities picker (storyboard S07): one full-screen, multi-select list of
  what Home Assistant currently knows, with a sticky search above it and one
  sticky "Add n tiles" action below.

  The five states are deliberately kept apart, because collapsing them is how
  this screen starts lying. Home Assistant being unreachable is a WARNING from
  the search route (`warning !== null`, `entities` empty) and must never read as
  "no matches": that would blame the user's spelling for an outage. A rejected
  request is the error state, an empty opening list is "No entities available",
  and an empty list for a query is "Nothing matches". `loading` only takes over
  the screen while there is no earlier answer to keep, so a re-search never
  blanks the list the user is reading.

  Rows are labelled checkboxes inside a row that also toggles on a tap, and an
  entity that is already configured shows "Already added" instead of a control —
  the server must never be asked to duplicate one silently.

  The typed-id escape hatch is a deliberate, documented divergence from S07: a
  well-formed `entity_id` that Home Assistant did not return can still be saved,
  because that is how a bogus id reaches its warning. Its purpose is spelled out
  in words under the search field, and the warning comes back verbatim on the
  result screen — a success with a caveat, never a failure.
-->
<script lang="ts">
  import {
    ApiError,
    createEntity,
    listEntityRows,
    searchHaEntities,
    type EntityTile,
    type HaEntityRow
  } from '../api.js';
  import { tileView } from '../tile-view.js';
  import Button from '../ui/Button.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import Icon from '../ui/Icon.svelte';
  import Input from '../ui/Input.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import { toastStore } from '../ui/toast.svelte.js';

  interface Props {
    /** Leave the picker; the parent navigates back to the list and reloads it. */
    onclose: () => void;
    /** The header back button's text, so a host screen can name its own list. */
    backLabel?: string;
  }

  const { onclose, backLabel = 'Back to entities' }: Props = $props();

  /** How many hits one search page asks for (the server caps at 200). */
  const SEARCH_LIMIT = 60;

  /** What an entity id looks like: `domain.object_id`, lower-case. */
  const ENTITY_ID_PATTERN = /^[a-z0-9_]+\.[a-z0-9_]+$/;

  type AddOutcome = 'saved' | 'existed' | 'failed';

  /** One entity's fate in a finished add, so the result screen can say it. */
  interface AddResult {
    entity_id: string;
    outcome: AddOutcome;
    /** The server's warning for a saved row (a bogus id), verbatim. */
    warning: string | null;
    /** The server's sentence for an "existed" or "failed" outcome, verbatim. */
    message: string | null;
  }

  let query = $state('');
  let entities = $state<HaEntityRow[]>([]);
  let warning = $state<string | null>(null);
  let searchError = $state<string | null>(null);
  let loading = $state(true);

  let searchSeq = 0;

  /** Entity ids that already have a tile, so they can never be added twice. */
  let configured = $state<Set<string>>(new Set());

  let selected = $state<string[]>([]);
  let adding = $state(false);
  let results = $state<AddResult[] | null>(null);

  /** Custom-icon files that failed to load, keyed by entity id. */
  let failedCustomIcons = $state<Record<string, boolean>>({});

  async function loadConfigured(): Promise<void> {
    try {
      const rows = await listEntityRows();
      configured = new Set(rows.map((row) => row.entity_id));
    } catch {
      // An unknown set is survivable: creating a duplicate answers 409, and the
      // result screen reports that verbatim instead of duplicating silently.
      configured = new Set();
    }
  }

  async function search(text: string): Promise<void> {
    searchSeq += 1;
    const seq = searchSeq;
    loading = true;
    try {
      const answer = await searchHaEntities(text, SEARCH_LIMIT);
      if (seq !== searchSeq) {
        return;
      }
      entities = answer.entities;
      warning = answer.warning;
      searchError = null;
    } catch (cause) {
      if (seq !== searchSeq) {
        return;
      }
      entities = [];
      warning = null;
      searchError = cause instanceof ApiError ? cause.message : 'the search could not be completed';
    } finally {
      if (seq === searchSeq) {
        loading = false;
      }
    }
  }

  // Which entities are already configured, once, on mount.
  $effect(() => {
    void loadConfigured();
  });

  // Every keystroke is a search: the results ARE the list, there is no typeahead.
  // `query` is the only reactive value read here, and `search` assigns nothing
  // this effect reads, so a finished search cannot re-trigger it.
  $effect(() => {
    void search(query);
  });

  /** Re-run the current search as a fresh attempt (the last answer was unusable). */
  function retry(): void {
    warning = null;
    searchError = null;
    entities = [];
    void search(query);
  }

  function isSelected(entityId: string): boolean {
    return selected.includes(entityId);
  }

  function setSelected(entityId: string, next: boolean): void {
    if (next) {
      if (!selected.includes(entityId)) {
        selected = [...selected, entityId];
      }
      return;
    }
    selected = selected.filter((candidate) => candidate !== entityId);
  }

  function toggle(entityId: string): void {
    if (adding || configured.has(entityId)) {
      return;
    }
    setSelected(entityId, !isSelected(entityId));
  }

  /**
   * The row's tap handler. A tap that landed on the checkbox is the checkbox's
   * own business — the native label already toggled the input, so acting again
   * here would cancel it out.
   */
  function onRowClick(event: MouseEvent, entityId: string): void {
    const target = event.target;
    if (target instanceof Element && target.closest('.row-control') !== null) {
      return;
    }
    toggle(entityId);
  }

  /** The tile facts a search row carries: Home Assistant's state, nothing stored. */
  function rowTile(row: HaEntityRow): EntityTile {
    return {
      id: 0,
      entityId: row.entity_id,
      displayName: row.name,
      state: row.state,
      attributes: {},
      icon: row.icon,
      iconSource: row.icon_source,
      accentColor: null,
      lastChanged: null,
      // A search hit carries no presentation hints, so the word is the raw
      // state and there is never a unit to show (`tileView` decides both).
      displayedState: null,
      unitOfMeasurement: null,
      attributeHints: [],
      // No per-tile state→tone mapping exists yet, so there is no tone to render:
      // `null` paints the row neutral, which is what an unconfigured search hit
      // is. The picker must not invent a colour any more than it invents a word.
      tone: null,
      hideWhenUnavailable: false,
      selected: false
    };
  }

  const typedId = $derived(query.trim());
  const typedLooksValid = $derived(ENTITY_ID_PATTERN.test(typedId));
  const typedListed = $derived(entities.some((row) => row.entity_id === typedId));
  const canAddTyped = $derived(typedLooksValid && !typedListed && !configured.has(typedId));

  function addLabel(count: number): string {
    return count === 1 ? 'Add 1 tile' : `Add ${count} tiles`;
  }

  async function addOne(entityId: string): Promise<AddResult> {
    try {
      const saved = await createEntity({ entity_id: entityId });
      // A saved row's warning (bogus id) is kept verbatim and blocks nothing.
      return { entity_id: entityId, outcome: 'saved', warning: saved.warning, message: null };
    } catch (cause) {
      if (cause instanceof ApiError) {
        if (cause.status === 409) {
          return { entity_id: entityId, outcome: 'existed', warning: null, message: cause.message };
        }
        return { entity_id: entityId, outcome: 'failed', warning: null, message: cause.message };
      }
      return {
        entity_id: entityId,
        outcome: 'failed',
        warning: null,
        message: 'the entity could not be added'
      };
    }
  }

  function resultHeading(list: readonly AddResult[]): string {
    const caveats = list.some((result) => result.outcome !== 'saved' || result.warning !== null);
    if (caveats) {
      return 'Add finished with warnings';
    }
    const saved = list.filter((result) => result.outcome === 'saved').length;
    return saved === 1 ? 'Added 1 tile' : `Added ${saved} tiles`;
  }

  /** Create each selected id one after another — never in parallel — and report. */
  async function runAdd(ids: readonly string[]): Promise<void> {
    if (adding || ids.length === 0) {
      return;
    }
    adding = true;
    const collected: AddResult[] = [];
    for (const entityId of ids) {
      collected.push(await addOne(entityId));
    }
    adding = false;
    results = collected;

    const failed = collected.filter((result) => result.outcome === 'failed').length;
    if (failed === collected.length) {
      toastStore.push('error', 'No entities were added.');
      return;
    }
    const caveats = collected.some((result) => result.outcome !== 'saved' || result.warning !== null);
    toastStore.push('success', caveats ? 'Add finished with warnings' : 'Entities added');
  }

  function addSelected(): void {
    void runAdd(selected);
  }

  /**
   * Save the typed id even though Home Assistant did not return it. This is the
   * only path to a bogus-id tile, and the server's warning is what proves the
   * write happened.
   */
  function addTyped(): void {
    if (!canAddTyped) {
      return;
    }
    void runAdd([typedId]);
  }
</script>

{#if results !== null}
  <div class="panel">
    <header class="head">
      <h2>{resultHeading(results)}</h2>
    </header>

    <ul class="results">
      {#each results as result (result.entity_id)}
        <li class="result">
          <span class="result-name">{result.entity_id}</span>
          {#if result.outcome === 'saved'}
            <span class="result-word">Added</span>
            {#if result.warning !== null}
              <p class="result-warning" role="status">{result.warning}</p>
            {/if}
          {:else if result.outcome === 'existed'}
            <span class="result-word">Already added</span>
            <p class="result-message">{result.message}</p>
          {:else}
            <span class="result-word">Not added</span>
            <p class="result-error" role="alert">{result.message}</p>
          {/if}
        </li>
      {/each}
    </ul>

    <div class="actions">
      <Button variant="primary" size="lg" onclick={onclose}>Done</Button>
    </div>
  </div>
{:else}
  <div class="panel">
    <header class="head">
      <h2>Add entities</h2>
      <Button onclick={onclose}>{backLabel}</Button>
    </header>

    <div class="search">
      <Input label="Search entities" bind:value={query} />

      {#if canAddTyped}
        <div class="typed">
          <Button disabled={adding} onclick={addTyped}>
            Add “{typedId}” anyway
          </Button>
          <p class="hint">
            Saves this entity id even though Home Assistant did not return it. The tile warns
            until Home Assistant knows the entity.
          </p>
        </div>
      {:else if typedId !== '' && !typedLooksValid}
        <p class="hint">An entity id looks like domain.object_id, for example lock.front_door.</p>
      {/if}
    </div>

    {#if adding}
      <Spinner label="Adding selected entities" />
    {/if}

    {#if warning !== null}
      <p class="message" role="status">{warning}</p>
      <div class="actions">
        <Button onclick={retry}>Retry search</Button>
      </div>
    {:else if searchError !== null}
      <p class="message" role="alert">{searchError}</p>
      <div class="actions">
        <Button onclick={retry}>Retry search</Button>
      </div>
    {:else if loading && entities.length === 0}
      <Spinner label="Searching entities" />
    {:else if entities.length === 0 && query === ''}
      <EmptyState
        title="No entities available"
        description="No live entities were returned by Home Assistant."
      />
    {:else if entities.length === 0}
      <EmptyState
        title="Nothing matches"
        description="Try searching by entity_id, for example lock.front_door."
      />
    {:else}
      <!--
        Every row behaves like a <label>: a tap anywhere on it toggles the
        checkbox, and the real input inside `Checkbox` stays the accessible
        control (keyboard Space, screen-reader state). That is exactly why the
        row adds no role and no key handler of its own — a second interactive
        role here would announce the same entity twice.
      -->
      <ul class="results">
        {#each entities as row (row.entity_id)}
          {@const view = tileView(rowTile(row))}
          {@const added = configured.has(row.entity_id)}
          {@const customIcon = row.icon.startsWith('custom:')}
          <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
          <li
            class="pick-row"
            class:selected={isSelected(row.entity_id)}
            onclick={(event) => onRowClick(event, row.entity_id)}
          >
            <span class="row-icon">
              {#if !customIcon}
                <Icon name={row.icon} size={24} />
              {:else if failedCustomIcons[row.entity_id] === true}
                <span data-custom-icon-failed="true">
                  <Icon name="mdi:help-circle-outline" size={24} />
                </span>
              {:else}
                <img
                  class="custom-icon"
                  src={`/custom-icons/${encodeURIComponent(row.icon.slice('custom:'.length))}`}
                  alt=""
                  data-icon-source="custom"
                  onerror={() => {
                    failedCustomIcons[row.entity_id] = true;
                  }}
                />
              {/if}
            </span>

            <div class="row-control">
              {#if added}
                <span class="added">Already added</span>
              {:else}
                <Checkbox
                  label={row.name}
                  checked={isSelected(row.entity_id)}
                  onselect={(checked) => setSelected(row.entity_id, checked)}
                />
              {/if}
            </div>

            <div class="facts">
              {#if added}
                <span class="name">{row.name}</span>
              {/if}
              <span class="entity-id">{row.entity_id}</span>
              <!-- Word and unit stay separate spans, exactly as the tile shows them. -->
              <span class="state">
                <span class="word">{view.word}</span>
                {#if view.unit}
                  <span class="unit">{view.unit}</span>
                {/if}
              </span>
            </div>
          </li>
        {/each}
      </ul>
    {/if}

    <div class="footer">
      <span class="count">{selected.length} selected</span>
      <Button
        variant="primary"
        size="lg"
        disabled={selected.length === 0 || adding}
        onclick={addSelected}
      >
        {addLabel(selected.length)}
      </Button>
    </div>
  </div>
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

  /*
   * The search stays put while the results scroll, with an opaque surface and a
   * hairline so rows never slide under a transparent bar.
   *
   * The offset comes from the host: the settings shell draws a sticky bar above
   * this one, while the wizard's full-screen picker view renders outside the
   * frame that defines `--shell-bar`, so it falls back to the viewport top.
   */
  .search {
    position: sticky;
    top: var(--shell-bar, 0px);
    z-index: 1;
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    padding-block: var(--gap-sm);
    background: var(--surface);
    border-bottom: var(--border-width) solid var(--border);
  }

  .typed {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--gap-sm);
  }

  .hint,
  .message,
  .result-message,
  .result-warning,
  .result-error {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
  }

  .hint {
    color: var(--text-muted);
  }

  .message {
    color: var(--state-warn);
  }

  .results {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  /* One row is one 80px tap target; the checkbox inside it is its own 48px one. */
  .pick-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    min-height: 80px;
    padding: var(--gap-sm);
    background: var(--surface);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  /* Selected is an accent edge AND the checked box's glyph — never colour alone. */
  .pick-row.selected {
    border-color: var(--accent);
    box-shadow: inset 4px 0 0 var(--accent);
  }

  .row-icon {
    display: inline-flex;
    flex-shrink: 0;
    color: var(--text);
  }

  .custom-icon {
    width: 24px;
    height: 24px;
    object-fit: contain;
  }

  .row-control {
    flex: 1 1 200px;
    min-width: 0;
  }

  /* The row's name is the checkbox's own label, so it carries the row emphasis. */
  .pick-row :global(.label) {
    font-size: var(--text-body);
    font-weight: 600;
  }

  .facts {
    display: flex;
    flex: 1 1 160px;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .added {
    display: flex;
    align-items: center;
    min-height: var(--tap);
    font-size: var(--text-body);
    font-weight: 600;
    color: var(--text-muted);
  }

  .name {
    font-size: var(--text-body);
    font-weight: 600;
    line-height: 1.3;
    overflow-wrap: anywhere;
  }

  .entity-id,
  .state {
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
  }

  .word {
    font-weight: 600;
  }

  .unit {
    font-size: var(--text-label);
  }

  /* The footer stays visible while the results scroll. */
  .footer {
    position: sticky;
    bottom: 0;
    z-index: 1;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap);
    padding-block: var(--gap-sm);
    background: var(--surface);
    border-top: var(--border-width) solid var(--border);
  }

  .count {
    font-size: var(--text-body);
    font-weight: 600;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }

  .result {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: var(--gap-sm);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius-sm);
  }

  .result-name {
    font-size: var(--text-body);
    font-weight: 600;
    overflow-wrap: anywhere;
  }

  .result-word {
    font-size: var(--text-label);
    font-weight: 600;
  }

  .result-warning {
    color: var(--state-warn);
  }

  .result-error {
    color: var(--state-alert);
  }
</style>
