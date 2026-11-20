<!--
  The icon browser (storyboard S08 F11/F12): a `Sheet`, never a modal, with a
  sticky search, the shipped MDI set as 48×48 cells and the user's own files as
  64×64 thumbnails.

  Every hit already carries its SVG path, so a 120-cell grid draws with zero
  further requests — that is what `Icon`'s `path` prop is for. Selection is a
  check glyph as well as a tinted cell, because colour alone is never a state.

  "Use the resolved icon" is the first block on purpose: clearing the override is
  a real choice, and the user has to be able to see which icon the D16 resolver
  picked (and why) before deciding to keep it. That row keeps the source label
  from `iconSourceLabel()`, so a resolved guess is never mistaken for a choice.

  Files the server refuses are listed with their sentence verbatim and are never
  offered as a choice: the user should learn why an SVG was rejected *before*
  picking it, not after.

  A caller can turn the user's own files off with `allowCustom={false}`: a
  button's icon is published inside a Home Assistant discovery payload, which HA
  validates as a whole and only accepts `mdi:` names, so the server refuses a
  `custom:` reference on a button (HSD-po1g) and this sheet must not offer a
  choice that would be refused. Nothing is fetched in that case — a list nobody
  can choose from is a request wasted.
-->
<script lang="ts">
  import {
    ApiError,
    listCustomIcons,
    searchIcons,
    type CustomIconEntry,
    type CustomIconSkip,
    type IconSearchResult
  } from '../api.js';
  import { iconSourceLabel } from '../tile-view.js';
  import Button from '../ui/Button.svelte';
  import Icon from '../ui/Icon.svelte';
  import Input from '../ui/Input.svelte';
  import Sheet from '../ui/Sheet.svelte';
  import Spinner from '../ui/Spinner.svelte';

  interface Props {
    /** Whether the sheet is on screen; bindable so choosing an icon can close it. */
    open?: boolean;
    /** The stored override (`mdi:…`, `custom:…`) or `null` for "let D16 decide". */
    value: string | null;
    /** The D16-resolved icon, shown even when an override is stored. */
    resolvedIcon: string;
    /** The D16 source of that resolved icon (`user`, `ha-attribute`, …). */
    resolvedSource: string;
    /** A chosen reference verbatim, or `null` to clear the override. */
    onselect: (icon: string | null) => void;
    /**
     * Whether the user's own files are offered at all. `false` for a button,
     * whose icon is published to Home Assistant and has to be an `mdi:` name.
     */
    allowCustom?: boolean;
  }

  let {
    open = $bindable(false),
    value,
    resolvedIcon,
    resolvedSource,
    onselect,
    allowCustom = true
  }: Props = $props();

  /** One page of the icon set (the server caps the page size at 200). */
  const ICON_LIMIT = 120;

  let query = $state('');
  let icons = $state<IconSearchResult[]>([]);
  let matched = $state(0);
  let version = $state('');
  let iconsError = $state<string | null>(null);
  let searching = $state(false);

  let custom = $state<CustomIconEntry[]>([]);
  let skipped = $state<CustomIconSkip[]>([]);
  let customError = $state<string | null>(null);
  let loadingCustom = $state(false);

  /** Older answers (a slower keystroke) must not win over a newer one. */
  let iconSeq = 0;
  let customSeq = 0;

  /** Thumbnails that failed to load, keyed by file name — the 404 fallback. */
  let failedCustom = $state<Record<string, boolean>>({});

  async function search(text: string): Promise<void> {
    iconSeq += 1;
    const seq = iconSeq;
    searching = true;
    try {
      const answer = await searchIcons(text, ICON_LIMIT);
      if (seq !== iconSeq) {
        return;
      }
      icons = answer.icons;
      matched = answer.matched;
      version = answer.version;
      iconsError = null;
    } catch (cause) {
      if (seq !== iconSeq) {
        return;
      }
      icons = [];
      matched = 0;
      iconsError = cause instanceof ApiError ? cause.message : 'the icon search could not be completed';
    } finally {
      if (seq === iconSeq) {
        searching = false;
      }
    }
  }

  async function loadCustom(): Promise<void> {
    customSeq += 1;
    const seq = customSeq;
    loadingCustom = true;
    try {
      const answer = await listCustomIcons();
      if (seq !== customSeq) {
        return;
      }
      custom = answer.icons;
      skipped = answer.skipped;
      customError = null;
    } catch (cause) {
      if (seq !== customSeq) {
        return;
      }
      custom = [];
      skipped = [];
      customError = cause instanceof ApiError ? cause.message : 'the icon list could not be loaded';
    } finally {
      if (seq === customSeq) {
        loadingCustom = false;
      }
    }
  }

  // A refetch on every open is simpler than a cache and costs two requests.
  $effect(() => {
    if (!open || !allowCustom) {
      return;
    }
    void loadCustom();
  });

  // `query` is read here, so typing searches; `open` is only a gate.
  $effect(() => {
    if (!open) {
      return;
    }
    void search(query);
  });

  function choose(icon: string | null): void {
    onselect(icon);
    open = false;
  }

  function isCustomIcon(icon: string): boolean {
    return icon.startsWith('custom:');
  }

  function customIconFile(icon: string): string {
    return icon.slice('custom:'.length);
  }

  /** The visible name of an `mdi:` hit: the reference without its prefix. */
  function iconName(name: string): string {
    return name.startsWith('mdi:') ? name.slice('mdi:'.length) : name;
  }
</script>

<Sheet bind:open title="Choose icon">
  <div class="search">
    <Input label="Search icons" bind:value={query} />
  </div>

  <section class="block" data-current={value === null ? 'resolved' : 'override'}>
    <div class="resolved-row">
      <span class="resolved-icon">
        {#if !isCustomIcon(resolvedIcon)}
          <Icon name={resolvedIcon} size={24} />
        {:else if failedCustom[resolvedIcon] === true}
          <span data-custom-icon-failed="true">
            <Icon name="mdi:help-circle-outline" size={24} />
          </span>
        {:else}
          <img
            class="custom-icon"
            src={`/custom-icons/${encodeURIComponent(customIconFile(resolvedIcon))}`}
            alt=""
            data-icon-source="custom"
            onerror={() => {
              failedCustom[resolvedIcon] = true;
            }}
          />
        {/if}
      </span>
      <span class="texts">
        <span class="title">Use the resolved icon</span>
        <span class="muted">{iconSourceLabel(resolvedSource)}</span>
      </span>
      {#if value === null}
        <!-- A word as well as the glyph: the selected state is never colour alone. -->
        <span class="selected-mark">
          <Icon name="mdi:check" size={24} />
          <span>Selected</span>
        </span>
      {/if}
      <Button onclick={() => choose(null)}>Use this</Button>
    </div>
  </section>

  <section class="block">
    <div class="block-head">
      <h3>Icons</h3>
      {#if version !== ''}
        <p class="muted">Icon set {version}</p>
      {/if}
    </div>

    {#if iconsError !== null}
      <p class="error" role="alert">{iconsError}</p>
      <div class="actions">
        <Button onclick={() => void search(query)}>Retry</Button>
      </div>
    {:else if searching && icons.length === 0}
      <Spinner label="Searching icons" />
    {:else if icons.length === 0}
      <p class="muted">No icons match “{query}”.</p>
    {:else}
      {#if matched > icons.length}
        <p class="muted">
          Showing {icons.length} of {matched} matches — keep typing to narrow it.
        </p>
      {/if}
      <div class="grid">
        {#each icons as hit (hit.name)}
          <button
            type="button"
            class="icon-cell"
            class:selected={value === hit.name}
            aria-label={iconName(hit.name)}
            aria-pressed={value === hit.name}
            onclick={() => choose(hit.name)}
          >
            <Icon name={hit.name} path={hit.path} size={32} />
            {#if value === hit.name}
              <span class="check" aria-hidden="true">
                <Icon name="mdi:check" size={20} />
              </span>
            {/if}
          </button>
        {/each}
      </div>
    {/if}
  </section>

  {#if allowCustom}
    <section class="block">
      <h3>Your icons</h3>

      {#if customError !== null}
        <p class="error" role="alert">{customError}</p>
        <div class="actions">
          <Button onclick={() => void loadCustom()}>Retry</Button>
        </div>
      {:else if loadingCustom && custom.length === 0}
        <Spinner label="Loading your icons" />
      {:else if custom.length === 0}
        <p class="muted">
          No files in data/icons yet — drop an .svg or .png in there to use it as an icon.
        </p>
      {:else}
        <div class="custom-grid">
          {#each custom as entry (entry.name)}
            <button
              type="button"
              class="custom-cell"
              class:selected={value === entry.reference}
              aria-label={entry.name}
              aria-pressed={value === entry.reference}
              onclick={() => choose(entry.reference)}
            >
              {#if failedCustom[entry.name] === true}
                <span data-custom-icon-failed="true">
                  <Icon name="mdi:help-circle-outline" size={32} />
                </span>
              {:else}
                <img
                  class="thumbnail"
                  src={`/custom-icons/${encodeURIComponent(entry.name)}`}
                  alt=""
                  data-icon-source="custom"
                  onerror={() => {
                    failedCustom[entry.name] = true;
                  }}
                />
              {/if}
              {#if value === entry.reference}
                <span class="check" aria-hidden="true">
                  <Icon name="mdi:check" size={20} />
                </span>
              {/if}
            </button>
          {/each}
        </div>
      {/if}

      {#if skipped.length > 0}
        <div class="skipped">
          <p class="muted">
            {skipped.length} {skipped.length === 1 ? 'file' : 'files'} could not be used
          </p>
          <ul>
            {#each skipped as entry (entry.name)}
              <li>{entry.name}: {entry.reason}</li>
            {/each}
          </ul>
        </div>
      {/if}
    </section>
  {/if}
</Sheet>

<style>
  /* The search stays put while the grids scroll inside the sheet body. */
  .search {
    position: sticky;
    top: 0;
    z-index: 1;
    padding-block: var(--gap-sm);
    background: var(--surface);
    border-bottom: var(--border-width) solid var(--border);
  }

  .block {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .block-head {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  h3 {
    margin: 0;
    font-size: var(--text-label);
    line-height: 1.25;
    font-weight: 700;
  }

  .resolved-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    padding: var(--gap-sm);
    background: var(--surface);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
  }

  .block[data-current='resolved'] .resolved-row {
    border-color: var(--accent);
  }

  .resolved-icon {
    display: inline-flex;
    flex-shrink: 0;
  }

  .texts {
    display: flex;
    flex: 1 1 160px;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .title {
    font-size: var(--text-body);
    font-weight: 600;
  }

  .muted {
    margin: 0;
    font-size: var(--text-label);
    line-height: 1.4;
    color: var(--text-muted);
  }

  .error {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    color: var(--state-alert);
  }

  .selected-mark {
    display: inline-flex;
    align-items: center;
    gap: var(--gap-sm);
    font-size: var(--text-label);
    font-weight: 600;
    color: var(--text);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }

  /* 48×48 cells with an 8px gutter: every icon is its own finger target. */
  .grid {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }

  .icon-cell,
  .custom-cell {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    background: var(--surface);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    color: var(--text);
    cursor: pointer;
  }

  .icon-cell {
    width: var(--tap);
    height: var(--tap);
  }

  .custom-cell {
    width: 64px;
    height: 64px;
  }

  .icon-cell.selected,
  .custom-cell.selected {
    border-color: var(--accent);
    box-shadow: inset 0 0 0 2px var(--accent);
  }

  /* The glyph sits on an opaque disc so it reads on any icon underneath. */
  .check {
    position: absolute;
    right: 2px;
    bottom: 2px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: var(--accent);
    color: var(--accent-contrast);
  }

  .thumbnail {
    width: 40px;
    height: 40px;
    object-fit: contain;
  }

  .skipped ul {
    display: flex;
    flex-direction: column;
    gap: 2px;
    margin: 0;
    padding-inline-start: var(--gap);
    list-style: disc;
  }

  .skipped li {
    font-size: var(--text-label);
    line-height: 1.4;
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }
</style>
