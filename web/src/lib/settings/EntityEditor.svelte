<!--
  One entity's editor (storyboard S08): the real dashboard tile as a live
  preview, then display name, icon, accent, the attributes to show and the
  unavailable rule, with the shared per-section Save bar at the bottom.

  The preview is `Tile.svelte` itself, not a lookalike: a hand-drawn copy would
  drift from what the dashboard renders the first time `tile-view.ts` changed,
  and the tile is what the user is actually editing. Its state word comes from
  `previewTile()`, which merges the stored row's live facts with the draft, so
  nothing here invents a reading.

  Three warnings share one shape and none of them blocks Save, because each is
  information rather than a mistake the user must fix first: a stored tile whose
  `custom:` file vanished (`row.warning`), Home Assistant disagreeing about the
  entity (`detail.warning` when `found !== true`), and a describe call that could
  not be answered (`detailError`). The editor stays fully usable in all three.

  `icon_source` has five meanings, and the legend spells all five out permanently
  (no hover): a resolved guess must never be mistaken for the user's own choice,
  and `fallback` and `default` must not be read as the same thing.
-->
<script lang="ts">
  import {
    ApiError,
    describeHaEntity,
    listEntityRows,
    updateEntity,
    type EntityResponse,
    type HaEntityDetail
  } from '../api.js';
  import {
    changeBody,
    draftFrom,
    isDirty,
    isValidAccentColor,
    previewTile,
    type EntityDraft
  } from '../entity-preview.js';
  import { ICON_SOURCE_LABELS, iconSourceLabel } from '../tile-view.js';
  import Tile from '../Tile.svelte';
  import Button from '../ui/Button.svelte';
  import Checkbox from '../ui/Checkbox.svelte';
  import ColorField from '../ui/ColorField.svelte';
  import Icon from '../ui/Icon.svelte';
  import Input from '../ui/Input.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { toastStore } from '../ui/toast.svelte.js';
  import IconPickerSheet from './IconPickerSheet.svelte';
  import SectionFooter from './SectionFooter.svelte';
  import StateToneTable from './StateToneTable.svelte';

  interface Props {
    /** The id of the stored row to edit (from `/settings/entities/<id>`). */
    id: number;
    /** Leave the editor; the parent navigates back to the list. */
    onback: () => void;
  }

  const { id, onback }: Props = $props();

  /** The five `icon_source` wire values, paired with the words the UI explains them in. */
  const ICON_SOURCES: readonly (readonly [string, string])[] = Object.entries(ICON_SOURCE_LABELS);

  /**
   * The draft before a row has loaded. It is never saved — Save renders only once
   * `row` exists and is gated on a real diff — but it keeps `bind:` usable.
   */
  const EMPTY_DRAFT: EntityDraft = {
    displayName: '',
    iconOverride: null,
    accentColor: null,
    showAttributes: [],
    hideWhenUnavailable: false,
    stateTones: {},
    colourByState: true
  };

  /** `null` means "still loading" until the list has answered. */
  let row = $state<EntityResponse | null>(null);
  let loading = $state(true);
  let loadError = $state<string | null>(null);

  let detail = $state<HaEntityDetail | null>(null);
  let detailError = $state<string | null>(null);

  let draft = $state<EntityDraft>({ ...EMPTY_DRAFT });

  let saving = $state(false);
  let saveError = $state<string | null>(null);
  let iconSheetOpen = $state(false);

  /** Guards against an older load (a previous id) overwriting a newer one. */
  let loadSeq = 0;

  async function load(wanted: number): Promise<void> {
    loadSeq += 1;
    const seq = loadSeq;
    loading = true;
    loadError = null;
    detail = null;
    detailError = null;

    let found: EntityResponse | null = null;
    try {
      const rows = await listEntityRows();
      if (seq !== loadSeq) {
        return;
      }
      found = rows.find((candidate) => candidate.id === wanted) ?? null;
    } catch (cause) {
      if (seq !== loadSeq) {
        return;
      }
      loadError = cause instanceof ApiError ? cause.message : 'the entity could not be loaded';
      loading = false;
      return;
    }

    row = found;
    if (found === null) {
      loading = false;
      return;
    }
    draft = draftFrom(found);
    saveError = null;

    // The live description only enriches the preview and the warnings, so a
    // rejection is tolerated: the editor works with the stored facts alone.
    try {
      const described = await describeHaEntity(found.entity_id);
      if (seq !== loadSeq) {
        return;
      }
      detail = described;
    } catch (cause) {
      if (seq !== loadSeq) {
        return;
      }
      detailError =
        cause instanceof ApiError ? cause.message : 'Home Assistant could not describe this entity';
    }
    loading = false;
  }

  // `id` is the only reactive value read here; `load` assigns state this effect
  // never reads, so a finished load cannot re-trigger it.
  $effect(() => {
    void load(id);
  });

  /** The sorted union of what Home Assistant reports and what is already stored. */
  function attributeUnion(
    attributes: Record<string, unknown>,
    stored: readonly string[]
  ): string[] {
    const names = new Set<string>(Object.keys(attributes));
    for (const name of stored) {
      // A stored name Home Assistant no longer reports stays in the list: a saved
      // choice must never be dropped just because it went quiet.
      names.add(name);
    }
    return [...names].sort();
  }

  const attributeNames = $derived(attributeUnion(detail?.attributes ?? {}, row?.show_attributes ?? []));

  function toggleAttribute(name: string, next: boolean): void {
    const chosen = new Set(draft.showAttributes);
    if (next) {
      chosen.add(name);
    } else {
      chosen.delete(name);
    }
    // Rebuild in the union's order, so the stored list is predictable.
    draft.showAttributes = attributeNames.filter((candidate) => chosen.has(candidate));
  }

  /** Reported by `ColorField`: the typed custom text is present and invalid. */
  let accentInvalid = $state(false);
  /** A stored invalid accent must block Save too, even before it is retyped. */
  const storedAccentInvalid = $derived(
    draft.accentColor !== null && !isValidAccentColor(draft.accentColor)
  );
  const accentBlocked = $derived(accentInvalid || storedAccentInvalid);
  const accentError = $derived(
    accentBlocked ? 'Use #rgb or #rrggbb, for example #4f9cf9' : undefined
  );

  const preview = $derived(
    row === null ? null : previewTile(row, draft, { attributes: detail?.attributes ?? {} })
  );

  /** Home Assistant could not be asked at all (not "the entity is missing"). */
  const previewStale = $derived(detail !== null && detail.found === null);

  const currentIcon = $derived(draft.iconOverride ?? row?.icon ?? 'mdi:help-circle-outline');
  const currentSource = $derived(draft.iconOverride === null ? (row?.icon_source ?? '') : 'user');
  const title = $derived(row === null ? 'Entity' : (row.display_name ?? row.entity_id));

  let failedCustomIcons = $state<Record<string, boolean>>({});

  function isCustomIcon(icon: string): boolean {
    return icon.startsWith('custom:');
  }

  function customIconFile(icon: string): string {
    return icon.slice('custom:'.length);
  }

  function chooseIcon(icon: string | null): void {
    draft.iconOverride = icon;
  }

  async function save(): Promise<void> {
    if (row === null || saving || accentBlocked || !isDirty(draft, row)) {
      return;
    }
    saving = true;
    saveError = null;
    try {
      const saved = await updateEntity(row.id, changeBody(draft, row));
      row = saved;
      draft = draftFrom(saved);
      toastStore.push('success', 'Entity saved.');
    } catch (cause) {
      // The draft is kept: a rejected save must not throw away what was typed.
      saveError = cause instanceof ApiError ? cause.message : 'the entity could not be saved';
    } finally {
      saving = false;
    }
  }

  const dirty = $derived(row !== null && isDirty(draft, row));
</script>

<div class="editor">
  <div class="topline">
    <Button onclick={onback}>Back to entities</Button>
    <h2>{title}</h2>
  </div>

  {#if loading}
    <Spinner label="Loading entity" />
  {:else if loadError !== null}
    <p class="error" role="alert">{loadError}</p>
    <div class="actions">
      <Button onclick={() => void load(id)}>Retry</Button>
    </div>
  {:else if row === null || preview === null}
    <section class="notice">
      <h2>This entity is not configured</h2>
      <p>There is no stored tile with the id “{id}”.</p>
      <div class="actions">
        <Button onclick={onback}>Back to entities</Button>
      </div>
    </section>
  {:else}
    <!-- Warnings are information, not blockers: none of them disables Save. -->
    {#if row.warning !== null}
      <p class="warning" role="status">{row.warning}</p>
    {/if}
    {#if detail !== null && detail.found !== true && detail.warning !== null}
      <p class="warning" role="status">{detail.warning}</p>
    {/if}
    {#if detailError !== null}
      <p class="warning" role="status">{detailError}</p>
    {/if}

    <div class="layout">
      <section class="preview-block">
        <h3>Live preview</h3>
        <ul class="preview">
          <Tile tile={preview} interactive={false} stale={previewStale} />
        </ul>
        <p class="muted">
          This is the tile the dashboard shows; the state is what Home Assistant reported.
        </p>
      </section>

      <div class="fields">
        <Input
          label="Display name"
          bind:value={draft.displayName}
          hint="Leave blank to use the name Home Assistant reports."
        />

        <div class="field">
          <span class="field-label">Icon</span>
          <!--
            A native button, not the kit's `Button`: that component does not
            forward `aria-haspopup`, and this trigger opens a sheet. The 48px
            shape and the secondary colours are the kit's own.
          -->
          <button
            type="button"
            class="icon-trigger"
            aria-haspopup="dialog"
            onclick={() => (iconSheetOpen = true)}
          >
            <span class="icon-current">
              {#if !isCustomIcon(currentIcon)}
                <Icon name={currentIcon} size={24} />
              {:else if failedCustomIcons[currentIcon] === true}
                <span data-custom-icon-failed="true">
                  <Icon name="mdi:help-circle-outline" size={24} />
                </span>
              {:else}
                <img
                  class="custom-icon"
                  src={`/custom-icons/${encodeURIComponent(customIconFile(currentIcon))}`}
                  alt=""
                  data-icon-source="custom"
                  onerror={() => {
                    failedCustomIcons[currentIcon] = true;
                  }}
                />
              {/if}
            </span>
            <span class="icon-text">
              {draft.iconOverride === null ? 'Icon (resolved automatically)' : draft.iconOverride}
            </span>
            <Icon name="mdi:chevron-down" size={24} />
          </button>

          <ul class="legend">
            {#each ICON_SOURCES as [value, label] (value)}
              <li>
                <span class="legend-label">{label}</span>
                <span class="wire">{value}</span>
              </li>
            {/each}
          </ul>
          <p class="muted">now: {iconSourceLabel(currentSource)}</p>
        </div>

        <ColorField
          label="Accent colour"
          bind:value={draft.accentColor}
          bind:invalid={accentInvalid}
          hint="Leave unset to use the theme accent."
          error={accentError}
        />

        <div class="field">
          <span class="field-label">Attributes to show</span>
          {#if attributeNames.length === 0}
            <p class="muted">Home Assistant reports no attributes for this entity.</p>
          {:else}
            {#each attributeNames as name (name)}
              <Checkbox
                label={name}
                checked={draft.showAttributes.includes(name)}
                onselect={(checked) => toggleAttribute(name, checked)}
              />
            {/each}
          {/if}
        </div>

        <div class="field">
          <span class="field-label">State tones</span>
          <StateToneTable
            bind:stateTones={draft.stateTones}
            bind:colourByState={draft.colourByState}
            currentState={row.state}
            currentWord={row.displayed_state}
            currentTone={row.tone}
          />
        </div>

        <Toggle
          label="Hide when unavailable"
          description="A tile that is unavailable is removed from the dashboard instead of shown greyed out."
          bind:checked={draft.hideWhenUnavailable}
        />

        <SectionFooter
          label="Save"
          dirty={dirty}
          invalid={accentBlocked}
          saving={saving}
          error={saveError}
          onsave={save}
        />
      </div>
    </div>

    <IconPickerSheet
      bind:open={iconSheetOpen}
      value={draft.iconOverride}
      resolvedIcon={row.icon}
      resolvedSource={row.icon_source}
      onselect={chooseIcon}
    />
  {/if}
</div>

<style>
  .editor {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

  .topline {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap);
  }

  h2 {
    margin: 0;
    font-size: var(--text-title);
    line-height: 1.2;
    font-weight: 700;
    overflow-wrap: anywhere;
  }

  h3 {
    margin: 0;
    font-size: var(--text-label);
    line-height: 1.25;
    font-weight: 700;
  }

  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--gap);
    align-items: start;
  }

  /* The real tile sits beside the fields once there is room for both. */
  @media (min-width: 1280px) {
    .layout {
      grid-template-columns: minmax(220px, 320px) minmax(0, 1fr);
    }
  }

  .preview-block {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .preview {
    margin: 0;
    padding: 0;
  }

  .fields {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

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

  /* The kit's secondary Button shape, at the kit's 48px target. */
  .icon-trigger {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
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
  }

  .icon-current {
    display: inline-flex;
    flex-shrink: 0;
  }

  .icon-text {
    flex: 1;
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .custom-icon {
    width: 24px;
    height: 24px;
    object-fit: contain;
  }

  .legend {
    display: flex;
    flex-direction: column;
    gap: 2px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .legend li {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--gap-sm);
    font-size: var(--text-label);
    line-height: 1.35;
    color: var(--text-muted);
  }

  /* The wire value, so the words below and the stored column can be matched up. */
  .wire {
    font-family: monospace;
    font-size: var(--text-label);
    color: var(--text-muted);
  }

  .muted {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    color: var(--text-muted);
  }

  .warning {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    color: var(--state-warn);
    overflow-wrap: anywhere;
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
</style>
