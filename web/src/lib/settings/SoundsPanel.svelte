<!--
  The Sounds section library (storyboards S12 F1–F12): indexed sound facts,
  browser preview, inline rename, delete confirmation, folder rescan and URL add.

  Every list change reloads from the server — `load()` is the single source of
  truth. A `missing` row stays listed because the index outlives its file. Delete
  removes the index row and, after an explicit second confirmation when needed,
  deletes the buttons that used it; the sound file on disk is never deleted.
  That two-step exists because a referenced sound takes its buttons with it.
  `from_url` performs real network I/O on the server. Browser preview streams
  `GET /api/sounds/{id}/audio` and is never offered for a missing file; one
  hidden `<audio preload="none">` is the only player. This panel has no settings
  draft and therefore no `SectionFooter`: every action is immediate.
-->
<script lang="ts">
  import {
    addSoundFromUrl,
    ApiError,
    deleteSound,
    listSounds,
    renameSound,
    rescanSounds,
    soundAudioUrl,
    type AffectedButton,
    type SoundRow
  } from '../api.js';
  import {
    affectedButtonNames,
    affectedButtonsFromError,
    deleteConfirmLabel,
    deleteConfirmMessage,
    deleteConfirmTitle,
    describeSourceType,
    formatSoundDuration,
    rescanSummary
  } from '../sound-library.js';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import Dialog from '../ui/Dialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import Icon from '../ui/Icon.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Input from '../ui/Input.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import { toastStore } from '../ui/toast.svelte.js';

  /** `null` means "no answer yet" — the spinner, never an empty list. */
  let rows = $state<SoundRow[] | null>(null);
  let loadError = $state<string | null>(null);
  /** A non-null view of rows, so the keyed record loop can stay narrow. */
  const list = $derived(rows ?? []);

  /** Guards against an older list answer overwriting the latest one. */
  let loadSeq = 0;
  let alive = false;

  let rescanning = $state(false);
  let rescanError = $state<string | null>(null);
  let rescanResult = $state<string | null>(null);

  let urlDraft = $state('');
  let adding = $state(false);
  let addError = $state<string | null>(null);

  let renameId = $state<number | null>(null);
  let renameDraft = $state('');
  let renamingId = $state<number | null>(null);
  let renameError = $state<{ id: number; message: string } | null>(null);

  let deletingId = $state<number | null>(null);
  let deleteError = $state<{ id: number; message: string } | null>(null);
  let deleteTarget = $state<SoundRow | null>(null);
  let deleteAffected = $state<AffectedButton[]>([]);
  let deleteOpen = $state(false);

  let previewId = $state<number | null>(null);
  let audioEl: HTMLAudioElement | undefined = $state();

  function messageFor(cause: unknown, fallback: string): string {
    return cause instanceof ApiError ? cause.message : fallback;
  }

  async function load(): Promise<void> {
    const sequence = ++loadSeq;
    rows = null;
    loadError = null;
    try {
      const answer = await listSounds();
      if (!alive || sequence !== loadSeq) {
        return;
      }
      rows = answer;
    } catch (cause) {
      if (!alive || sequence !== loadSeq) {
        return;
      }
      loadError = messageFor(cause, 'the sound library could not be loaded');
    }
  }

  function clearPreview(expectedId?: number): void {
    if (expectedId !== undefined && previewId !== expectedId) {
      return;
    }
    if (audioEl !== undefined) {
      audioEl.pause();
      audioEl.removeAttribute('src');
    }
    previewId = null;
  }

  function preview(id: number): void {
    const row = list.find((candidate) => candidate.id === id);
    if (row === undefined || row.missing || audioEl === undefined) {
      return;
    }
    if (previewId !== null) {
      clearPreview();
    }
    previewId = id;
    audioEl.src = soundAudioUrl(id);
    try {
      // Some browser/test implementations return no promise; a rejected play
      // is also an ordinary no-audio-device outcome, not a panel error.
      const started = audioEl.play() as Promise<void> | undefined;
      if (started) {
        void started.catch(() => clearPreview(id));
      }
    } catch {
      clearPreview(id);
    }
  }

  function handleEnded(): void {
    clearPreview();
  }

  $effect(() => {
    alive = true;
    void load();
    return () => {
      alive = false;
      loadSeq += 1;
      clearPreview();
    };
  });

  async function rescan(): Promise<void> {
    if (rescanning) {
      return;
    }
    rescanning = true;
    rescanError = null;
    rescanResult = null;
    try {
      const report = await rescanSounds();
      if (!alive) {
        return;
      }
      rescanResult = rescanSummary(report);
      toastStore.push('success', 'Sound folder rescanned.');
      await load();
    } catch (cause) {
      if (alive) {
        rescanError = messageFor(cause, 'the sound folder could not be rescanned');
      }
    } finally {
      if (alive) {
        rescanning = false;
      }
    }
  }

  async function addFromUrl(): Promise<void> {
    const url = urlDraft.trim();
    if (url === '' || adding) {
      return;
    }
    adding = true;
    addError = null;
    try {
      const answer = await addSoundFromUrl(url);
      if (!alive) {
        return;
      }
      urlDraft = '';
      toastStore.push(
        'success',
        answer.created ? 'Sound added.' : 'Sound already in the library.'
      );
      await load();
    } catch (cause) {
      if (alive) {
        addError = messageFor(cause, 'the sound could not be added');
      }
    } finally {
      if (alive) {
        adding = false;
      }
    }
  }

  function beginRename(row: SoundRow): void {
    if (renameId !== null || renamingId !== null) {
      return;
    }
    renameId = row.id;
    renameDraft = row.name;
    renameError = null;
  }

  function cancelRename(): void {
    if (renamingId !== null) {
      return;
    }
    renameId = null;
    renameDraft = '';
    renameError = null;
  }

  async function saveRename(row: SoundRow): Promise<void> {
    const name = renameDraft.trim();
    if (name === '' || name === row.name || renamingId !== null) {
      return;
    }
    renamingId = row.id;
    renameError = null;
    try {
      await renameSound(row.id, name);
      if (!alive) {
        return;
      }
      renameId = null;
      renameDraft = '';
      toastStore.push('success', 'Sound renamed.');
      await load();
    } catch (cause) {
      if (alive) {
        renameError = {
          id: row.id,
          message: messageFor(cause, 'the sound could not be renamed')
        };
      }
    } finally {
      if (alive) {
        renamingId = null;
      }
    }
  }

  function cancelDelete(): void {
    deleteOpen = false;
    deleteTarget = null;
    deleteAffected = [];
  }

  async function deleteRow(row: SoundRow): Promise<void> {
    if (deletingId !== null) {
      return;
    }
    deletingId = row.id;
    deleteError = null;
    try {
      await deleteSound(row.id);
      if (!alive) {
        return;
      }
      toastStore.push('success', 'Sound deleted.');
      await load();
    } catch (cause) {
      if (!alive) {
        return;
      }
      deleteError = { id: row.id, message: messageFor(cause, 'the sound could not be deleted') };
      if (cause instanceof ApiError && cause.status === 409) {
        deleteAffected = affectedButtonsFromError(cause.detail);
        deleteTarget = row;
        deleteOpen = true;
      }
    } finally {
      if (alive) {
        deletingId = null;
      }
    }
  }

  async function confirmDelete(): Promise<void> {
    const target = deleteTarget;
    if (target === null || deletingId !== null) {
      return;
    }
    deletingId = target.id;
    deleteError = null;
    try {
      await deleteSound(target.id, true);
      if (!alive) {
        return;
      }
      deleteTarget = null;
      deleteAffected = [];
      toastStore.push('success', 'Sound deleted.');
      await load();
    } catch (cause) {
      if (alive) {
        deleteError = {
          id: target.id,
          message: messageFor(cause, 'the sound could not be deleted')
        };
        deleteTarget = null;
        deleteAffected = [];
      }
    } finally {
      if (alive) {
        deletingId = null;
      }
    }
  }
</script>

<div class="panel">
  <header class="head">
    <h2>Sounds</h2>
  </header>

  {#if loadError !== null}
    <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{loadError}</span></p>
    <div class="controls">
      <Button onclick={() => void load()}>Retry</Button>
    </div>
  {:else if rows === null}
    <Spinner label="Loading the sound library" />
  {:else if list.length === 0}
    <EmptyState title="No sounds yet" description="Rescan the folder or add an audio URL." />
  {:else}
    <section class="library" aria-label="Sound library">
      <div class="column-head" aria-hidden="true">
        <span>Name</span>
        <span>Format</span>
        <span>Duration</span>
        <span>Source</span>
        <span>Missing</span>
        <span>Actions</span>
      </div>
      <ul class="records">
        {#each list as row (row.id)}
          <li class="record" class:broken={row.missing}>
            <div class="cell name-cell">
              <span class="mobile-label">Name</span>
              {#if renameId === row.id}
                <div class="rename-form">
                  <Input label="Sound name" bind:value={renameDraft} inputmode="text" disabled={renamingId === row.id} />
                  <div class="rename-actions">
                    <Button
                      variant="primary"
                      loading={renamingId === row.id}
                      disabled={renameDraft.trim() === '' || renameDraft.trim() === row.name || renamingId === row.id}
                      onclick={() => void saveRename(row)}
                    >Save rename</Button>
                    <Button disabled={renamingId === row.id} onclick={cancelRename}>Cancel</Button>
                  </div>
                </div>
              {:else}
                <div class="record-title">
                  <h3>{row.name}</h3>
                  {#if previewId === row.id}
                    <Badge kind="accent" label="PLAYING" />
                  {/if}
                </div>
              {/if}
              {#if renameError !== null && renameError.id === row.id}
                <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{renameError.message}</span></p>
              {/if}
            </div>

            <div class="cell fact-cell">
              <span class="mobile-label">Format</span>
              <span>{row.format ?? '—'}</span>
            </div>
            <div class="cell fact-cell">
              <span class="mobile-label">Duration</span>
              <span>{row.missing ? '—' : formatSoundDuration(row.duration_ms)}</span>
            </div>
            <div class="cell fact-cell">
              <span class="mobile-label">Source</span>
              <span>{describeSourceType(row.source_type)}</span>
            </div>
            <div class="cell fact-cell missing-cell">
              <span class="mobile-label">Missing</span>
              {#if row.missing}
                <Badge kind="alert" icon="mdi:alert-circle-outline" label="MISSING FILE" />
                <p class="missing-note">Cannot be assigned to a button until the file returns.</p>
              {:else}
                <span>No</span>
              {/if}
            </div>
            <div class="cell actions-cell">
              <span class="mobile-label">Actions</span>
              <div class="row-actions">
                {#if previewId === row.id}
                  <IconButton label="Stop preview" icon="mdi:stop" onclick={() => clearPreview(row.id)} />
                {:else}
                  <IconButton
                    label={`Preview ${row.name}`}
                    icon="mdi:play"
                    disabled={row.missing}
                    onclick={() => preview(row.id)}
                  />
                {/if}
                {#if renameId !== row.id}
                  <IconButton
                    label={`Rename ${row.name}`}
                    icon="mdi:pencil"
                    disabled={renameId !== null || renamingId !== null}
                    onclick={() => beginRename(row)}
                  />
                {/if}
                <IconButton
                  label={`Delete ${row.name}`}
                  icon="mdi:delete-outline"
                  variant="danger"
                  disabled={deletingId !== null}
                  onclick={() => void deleteRow(row)}
                />
              </div>
              {#if deletingId === row.id}
                <Spinner label="Deleting the sound" />
              {/if}
              {#if deleteError !== null && deleteError.id === row.id}
                <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{deleteError.message}</span></p>
              {/if}
            </div>
          </li>
        {/each}
      </ul>
    </section>
  {/if}

  <section class="populate" aria-label="Populate the sound library">
    <div class="rescan-block">
      <div class="controls">
        <Button loading={rescanning} onclick={() => void rescan()}>Rescan folder</Button>
        {#if rescanning}
          <Spinner label="Rescanning the sound folder" />
        {/if}
      </div>
      {#if rescanError !== null}
        <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{rescanError}</span></p>
      {/if}
      {#if rescanResult !== null}
        <div class="rescan-result" aria-live="polite">
          <p>{rescanResult}</p>
          <p class="hint">These counts are the server's: removed means newly flagged missing, and missing is the total still missing after the scan.</p>
        </div>
      {/if}
    </div>

    <div class="url-block">
      <h3>Add from URL</h3>
      <div class="url-form">
        <Input
          label="Audio URL"
          type="url"
          inputmode="url"
          placeholder="https://example.local/ding.mp3"
          bind:value={urlDraft}
          disabled={adding}
        />
        <Button
          variant="primary"
          size="lg"
          loading={adding}
          disabled={urlDraft.trim() === '' || adding}
          onclick={() => void addFromUrl()}
        >Add</Button>
      </div>
      {#if addError !== null}
        <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{addError}</span></p>
      {/if}
    </div>
  </section>

  <audio bind:this={audioEl} preload="none" hidden onended={handleEnded}></audio>
</div>

{#if deleteTarget !== null && deleteOpen}
  <div
    class="delete-dialog"
    role="group"
    aria-label={deleteAffected.length > 0 ? `Buttons also deleted: ${affectedButtonNames(deleteAffected)}` : 'Delete sound confirmation'}
  >
    <Dialog
      bind:open={deleteOpen}
      title={deleteConfirmTitle(deleteTarget.name)}
      message={deleteConfirmMessage(deleteTarget, deleteAffected)}
      confirmLabel={deleteConfirmLabel(deleteAffected)}
      destructive
      onconfirm={() => void confirmDelete()}
      oncancel={cancelDelete}
    />
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

  h3 {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.35;
    font-weight: 600;
    overflow-wrap: anywhere;
  }

  .library,
  .populate,
  .rescan-block,
  .url-block {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

  .populate {
    padding: var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
  }

  .column-head,
  .record {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--gap-sm);
    min-width: 0;
  }

  .column-head {
    display: none;
  }

  .records {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .record {
    padding: var(--gap);
    background-color: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
    overflow-wrap: anywhere;
  }

  .record.broken {
    /*
     * A hatch, not a tint: the broken state must survive a monochrome screen and
     * a colour-blind reader, so it never depends on the red alone. `color-mix`
     * keeps the token honest in both themes; a browser without it loses only the
     * hatch, never the MISSING FILE text or the badge.
     */
    background-image: repeating-linear-gradient(
      135deg,
      transparent 0,
      transparent 10px,
      color-mix(in srgb, var(--state-alert) 12%, transparent) 10px,
      color-mix(in srgb, var(--state-alert) 12%, transparent) 12px
    );
  }

  .cell,
  .name-cell,
  .fact-cell,
  .actions-cell {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--gap-sm);
    min-width: 0;
    font-size: var(--text-body);
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  .record-title {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .record-title h3 {
    min-width: 0;
  }

  .mobile-label {
    color: var(--text-muted);
    font-size: var(--text-body);
    font-weight: 600;
  }

  .missing-cell :global(.badge) {
    max-width: 100%;
  }

  .missing-note,
  .hint,
  .rescan-result p {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .missing-note,
  .hint {
    color: var(--text-muted);
  }

  .rescan-result {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .rescan-result > p:first-child {
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  .controls,
  .rename-actions,
  .row-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .url-form,
  .rename-form {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .url-form :global(.field) {
    flex: 1;
    min-width: 0;
  }

  .row-actions :global(button) {
    flex: 0 0 auto;
  }

  .error {
    display: flex;
    align-items: flex-start;
    gap: var(--gap-sm);
    margin: 0;
    color: var(--state-alert);
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .error :global(svg) {
    flex-shrink: 0;
    margin-top: 0.2em;
  }

  .error > span {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .delete-dialog :global(.sheet-title) {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  @media (min-width: 768px) {
    .column-head,
    .record {
      grid-template-columns:
        minmax(0, 1.6fr)
        minmax(0, 0.55fr)
        minmax(0, 0.7fr)
        minmax(0, 1fr)
        minmax(0, 1fr)
        minmax(10rem, 1.5fr);
      align-items: center;
    }

    .column-head {
      display: grid;
      gap: var(--gap-sm);
      padding-inline: var(--gap);
      color: var(--text-muted);
      font-size: var(--text-body);
      font-weight: 600;
      line-height: 1.3;
    }

    .record {
      gap: var(--gap-sm);
    }

    .mobile-label {
      display: none;
    }

    .name-cell,
    .fact-cell,
    .actions-cell {
      justify-content: center;
    }

    .url-form {
      flex-direction: row;
      align-items: flex-end;
      gap: var(--gap);
    }

    .url-form > :global(button) {
      flex-shrink: 0;
    }

    .row-actions {
      gap: var(--gap-sm);
    }
  }
</style>
