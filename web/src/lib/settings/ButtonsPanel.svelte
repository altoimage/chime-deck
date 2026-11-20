<!--
  The Buttons section list (storyboards S10 F1–F9): one row per chime button,
  with its enable switch, its MQTT honesty line and the three row actions.

  **Sub-routing.** `rest` is the tail after `/settings/buttons` and is the only
  thing this panel routes on: `''` is the list, `'add'` the create editor and a
  plain number one button's editor. Anything else is a "No such button" notice
  rather than a silent redirect back to the list.

  **The MQTT line is derived, never read off `mqtt`.** `GET /api/buttons`
  populates `mqtt` for *every* button, including a disabled one: the MQTT
  exclusion of a switched-off button happens at publish time in
  `mqtt/discovery.py`, not in that route. So `mqtt: null` means only one thing —
  the broker settings themselves are unusable — and a disabled button's topics
  are what the bridge *would* use. That is why "not announced" is derived from
  `enabled` first and why a topic list is never presented as live.

  **The Toggle is the only writer of `enabled`.** `button-action.changeBody`
  deliberately never sends it, so a stale editor draft can never switch a button
  off behind this list's back; the switch below is optimistic, and a refusal
  restores the old value and prints the server's sentence verbatim.

  **The Test result belongs to one row.** `pressFor` records which button the
  current report describes, so a result can never appear under a row it was not
  measured on.

  **The pre-render badge reports the stored state, never a guess.** It renders only
  for a speech row whose stored row carries bake keys: a sound button has no phrase
  and, as the server says, "no bake state to report on", and a build without those
  keys must not render "(Not pre-rendered)" as if something were missing. The badge
  always writes the state in words — colour alone never carries it. There is no
  _Pre-render now_ action here: S10 draws only the badge, and the action lives in
  the S11 editor.
-->
<script lang="ts">
  import {
    ApiError,
    deleteButton,
    listButtonRows,
    listSounds,
    pressButton,
    updateButton,
    type ButtonDetail,
    type SoundRow
  } from '../api.js';
  import { bakeStateOf, describeBakeStatus, type BakeBadgeKind } from '../bake.js';
  import { describePress, type PressReport } from '../press-report.js';
  import { settings } from '../settings.svelte.js';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import Dialog from '../ui/Dialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import Icon from '../ui/Icon.svelte';
  import IconButton from '../ui/IconButton.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { toastStore } from '../ui/toast.svelte.js';
  import ButtonEditor from './ButtonEditor.svelte';
  import ButtonPressResult from './ButtonPressResult.svelte';

  interface Props {
    /** The sub-route tail after `/settings/buttons`: `''`, `'add'` or a row id. */
    rest?: string;
    /** Move to another settings path without a page reload. */
    navigate?: (path: string) => void;
  }

  const LIST_PATH = '/settings/buttons';
  const ADD_PATH = '/settings/buttons/add';

  /** Stand-in navigator, so a preview or a test can mount the panel on its own. */
  function stay(): void {}

  let { rest = '', navigate = stay }: Props = $props();

  type PanelMode = 'list' | 'add' | 'editor' | 'missing';

  /** Which panel a sub-route tail names. */
  function modeFor(tail: string): PanelMode {
    if (tail === '') {
      return 'list';
    }
    if (tail === 'add') {
      return 'add';
    }
    if (/^\d+$/.test(tail)) {
      return 'editor';
    }
    return 'missing';
  }

  const mode = $derived(modeFor(rest));
  const editingId = $derived(mode === 'editor' ? Number(rest) : 0);

  /** `null` means "no answer yet" — the spinner, never an empty list. */
  let rows = $state<ButtonDetail[] | null>(null);
  let loadError = $state<string | null>(null);

  /** A non-null view of `rows`, so a row snippet can index it without widening. */
  const list = $derived(rows ?? []);

  /** The library, used ONLY to name the sound a Test result was configured with. */
  let sounds = $state<SoundRow[]>([]);

  /** The button whose press request is in flight, or null. */
  let pressing = $state<number | null>(null);
  let pressReport = $state<PressReport | null>(null);
  let pressError = $state<string | null>(null);
  /** Which row the current Test result belongs to; nothing renders under any other. */
  let pressFor = $state<number | null>(null);

  /** The button whose enable switch is in flight, or null. */
  let toggling = $state<number | null>(null);
  let toggleError = $state<{ id: number; message: string } | null>(null);

  /** The row the delete confirmation names; null closes it. */
  let deleteTarget = $state<ButtonDetail | null>(null);
  /** Bound to `Dialog`'s `open`; `Dialog` clears it on confirm or cancel. */
  let deleteOpen = $state(false);
  let deleting = $state(false);
  let deleteError = $state<string | null>(null);

  /** Guards against an older list answer overwriting a newer one. */
  let loadSeq = 0;

  /** The sound library, or an empty one — a sound name only decorates a Test result. */
  async function soundNames(): Promise<SoundRow[]> {
    try {
      return await listSounds();
    } catch {
      return [];
    }
  }

  async function load(): Promise<void> {
    loadSeq += 1;
    const seq = loadSeq;
    rows = null;
    loadError = null;
    pressReport = null;
    pressError = null;
    pressFor = null;
    toggleError = null;
    deleteError = null;
    try {
      const [answer, library] = await Promise.all([listButtonRows(), soundNames()]);
      if (seq !== loadSeq) {
        return;
      }
      rows = answer;
      sounds = library;
    } catch (cause) {
      if (seq !== loadSeq) {
        return;
      }
      loadError = cause instanceof ApiError ? cause.message : 'the button list could not be loaded';
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
    // that is what reloads the list when the editor navigates back to it.
    if (listRequested && tail !== '') {
      return;
    }
    listRequested = true;
    void load();
  });

  /**
   * `tts.default_engine === 'none'` is D27's documented hand-off for "speech is
   * off": a TTS button keeps its configuration, is never deleted, and is shown
   * as disabled rather than silently rewritten.
   */
  const defaultEngine = $derived(settings.text('tts.default_engine', 'ha_tts'));
  const speechOff = $derived(defaultEngine === 'none');

  /** The action word for a stored row; an unknown value reads as `Sound`, like `parseAction`. */
  function actionWord(row: ButtonDetail): string {
    return row.action_type === 'tts' ? 'Speech' : 'Sound';
  }

  /**
   * What a row says about its Home Assistant announcement.
   *
   * `enabled` is checked before `mqtt` on purpose: `mqtt` is populated even for
   * a switched-off button (the exclusion happens at publish time in
   * `mqtt/discovery.py`), so a disabled row's topics are only what the bridge
   * *would* use and must never read as live.
   */
  type Announcement = { kind: 'announced'; topic: string } | { kind: 'words'; words: string };

  function announcementFor(row: ButtonDetail): Announcement {
    if (!row.enabled) {
      return {
        kind: 'words',
        words: 'Not announced to Home Assistant — this button is off; its saved configuration is kept.'
      };
    }
    if (row.mqtt === null) {
      return {
        kind: 'words',
        words: 'Not announced to Home Assistant — the MQTT broker settings are unusable.'
      };
    }
    return { kind: 'announced', topic: row.mqtt.command };
  }

  /**
   * The pre-render badge for one row, or null when there is nothing honest to say.
   *
   * Only a speech row can be baked, and only a row that carries bake keys belongs to a
   * server that reports the state at all — an older build omits them, which is not the
   * same fact as an unbaked button.
   */
  function bakeBadgeOf(row: ButtonDetail): { kind: BakeBadgeKind; label: string } | null {
    if (row.action_type !== 'tts') {
      return null;
    }
    const state = bakeStateOf(row);
    return state === null ? null : describeBakeStatus(state);
  }

  /**
   * The enable switch: optimistic, then the server's answer replaces the row so
   * `mqtt`, `updated_at` and anything else it touched are the stored values.
   */
  async function setEnabled(row: ButtonDetail, next: boolean): Promise<void> {
    if (toggling !== null) {
      return;
    }
    const previous = row.enabled;
    row.enabled = next;
    toggling = row.id;
    toggleError = null;
    try {
      const saved = await updateButton(row.id, { enabled: next });
      if (rows !== null) {
        rows = rows.map((candidate) => (candidate.id === saved.id ? saved : candidate));
      }
    } catch (cause) {
      row.enabled = previous;
      const message = cause instanceof ApiError ? cause.message : 'the button could not be switched';
      toggleError = { id: row.id, message };
      toastStore.push('error', message);
    } finally {
      toggling = null;
    }
  }

  /**
   * Test one button through the same path MQTT uses (`source="ui_test"`), then
   * describe the answer with `press-report.ts` — this file never decides what a
   * press fact means.
   */
  async function testPress(row: ButtonDetail): Promise<void> {
    pressing = row.id;
    pressFor = row.id;
    pressReport = null;
    pressError = null;
    const started = performance.now();
    try {
      const response = await pressButton(row.id);
      const elapsed = performance.now() - started;
      const soundName =
        row.sound_id === null
          ? null
          : (sounds.find((sound) => sound.id === row.sound_id)?.name ?? null);
      pressReport = describePress(response, row, soundName, elapsed, defaultEngine);
    } catch (cause) {
      pressError = cause instanceof ApiError ? cause.message : 'the button could not be tested';
    } finally {
      pressing = null;
    }
  }

  function askDelete(row: ButtonDetail): void {
    deleteTarget = row;
    deleteOpen = true;
  }

  /**
   * Delete the row the confirmation named.
   *
   * The dialog closes on confirm (the kit's `Dialog` clears `open` before it
   * calls back), so a refusal is reported as an alert line above the list
   * instead of being re-opened inside the sheet: the durable record of a failed
   * delete is the inline sentence, exactly like every other panel's error.
   */
  async function doDelete(): Promise<void> {
    const target = deleteTarget;
    if (target === null || deleting) {
      return;
    }
    deleting = true;
    deleteError = null;
    try {
      await deleteButton(target.id);
      toastStore.push('success', 'Button deleted.');
      deleteTarget = null;
      void load();
    } catch (cause) {
      deleteError = cause instanceof ApiError ? cause.message : 'the button could not be deleted';
      deleteTarget = null;
    } finally {
      deleting = false;
    }
  }

  function cancelDelete(): void {
    deleteTarget = null;
  }
</script>

{#if mode === 'add'}
  <!--
    A create hands back the stored row, so the panel follows the server's own id
    into that button's editor: the form that only named it is not where a sound
    or a phrase gets chosen.
  -->
  <ButtonEditor
    id={null}
    onback={() => navigate(LIST_PATH)}
    oncreated={(button) => navigate(`${LIST_PATH}/${button.id}`)}
  />
{:else if mode === 'editor'}
  <ButtonEditor id={editingId} onback={() => navigate(LIST_PATH)} />
{:else if mode === 'missing'}
  <section class="notice">
    <h2>No such button</h2>
    <p>There is no button at “{rest}”.</p>
    <div class="actions">
      <Button onclick={() => navigate(LIST_PATH)}>Back to buttons</Button>
    </div>
  </section>
{:else}
  <div class="panel">
    <header class="head">
      <h2>Buttons</h2>
      <Button variant="primary" size="lg" onclick={() => navigate(ADD_PATH)}>New button</Button>
    </header>

    {#if loadError !== null}
      <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{loadError}</span></p>
      <div class="actions">
        <Button onclick={() => void load()}>Retry</Button>
      </div>
    {:else if rows === null}
      <Spinner label="Loading chime buttons" />
    {:else if list.length === 0}
      <EmptyState
        title="No chime buttons yet"
        description="A chime button plays a sound or speaks a phrase when it is pressed."
      >
        <Button variant="primary" size="lg" onclick={() => navigate(ADD_PATH)}>New button</Button>
      </EmptyState>
    {:else}
      {#if deleteError !== null}
        <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{deleteError}</span></p>
      {/if}
      {#if deleting}
        <Spinner label="Deleting the button" />
      {/if}

      <ul class="rows">
        {#each list as row (row.id)}
          {@const announcement = announcementFor(row)}
          {@const bakeBadge = bakeBadgeOf(row)}
          <li class="row">
            <div class="row-head">
              <h3>{row.name}</h3>
              {#if row.action_type === 'tts' && speechOff}
                <Badge kind="warn" icon="mdi:alert-circle-outline" label="Speech is off" />
              {/if}
              {#if bakeBadge !== null}
                <Badge
                  kind={bakeBadge.kind}
                  icon={bakeBadge.kind === 'ok' ? 'mdi:check-circle-outline' : 'mdi:alert-circle-outline'}
                  label={bakeBadge.label}
                />
              {/if}
            </div>

            <!--
              One sentence per row: slug (numbers line up), action word, then the
              MQTT words. Never a blank where the announcement would be — an off
              button and an unusable broker are different facts and each says so.
            -->
            <p class="meta">
              <span class="slug">{row.slug}</span>{' · '}<span>{actionWord(row)}</span>{' · '}
              {#if announcement.kind === 'announced'}
                Announced to Home Assistant <span class="topic">{announcement.topic}</span>
              {:else}
                <span class="announcement-unavailable">
                  <Icon name="mdi:alert-circle-outline" size={20} />
                  <span>{announcement.words}</span>
                </span>
              {/if}
            </p>

            {#if row.action_type === 'tts' && speechOff}
              <p class="note">Disabled by speech = none. Its speech configuration is kept, never deleted.</p>
            {/if}

            <!--
              `change` rather than `bind:checked`: the server's answer is the
              stored value, so the row is written by `setEnabled` only.
            -->
            <Toggle
              label="Enabled"
              checked={row.enabled}
              disabled={toggling === row.id}
              change={(next) => void setEnabled(row, next)}
            />

            <div class="actions">
              <!--
                `mdi:cog-outline` instead of a pencil: the kit bundles no pencil
                (the storyboard records the question-mark fallback for
                `mdi:pencil-outline`), and settings is where a row is configured.
              -->
              <IconButton
                label={`Edit ${row.name}`}
                icon="mdi:cog-outline"
                onclick={() => navigate(`${LIST_PATH}/${row.id}`)}
              />
              <IconButton
                label={`Test ${row.name}`}
                icon="mdi:play"
                disabled={pressing !== null}
                onclick={() => void testPress(row)}
              />
              <IconButton
                label={`Delete ${row.name}`}
                icon="mdi:close"
                disabled={deleting}
                onclick={() => askDelete(row)}
              />
            </div>

            {#if toggleError !== null && toggleError.id === row.id}
              <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{toggleError.message}</span></p>
            {/if}

            {#if pressFor === row.id}
              <ButtonPressResult
                pressing={pressing === row.id}
                error={pressError}
                report={pressReport}
              />
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  </div>

  <!--
    One dialog for the whole panel, never one per row: it is driven by
    `deleteTarget` and mounts outside the `{#each}`.
  -->
  {#if deleteTarget !== null}
    <div class="delete-dialog">
      <Dialog
        bind:open={deleteOpen}
        title={`Delete “${deleteTarget.name}”?`}
        message="Home Assistant will remove this button's entity."
        confirmLabel="Delete button"
        destructive
        onconfirm={() => void doDelete()}
        oncancel={cancelDelete}
      />
    </div>
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

  h3 {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.3;
    font-weight: 600;
    overflow-wrap: anywhere;
  }

  .rows {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  /* A row is a stack of labeled controls, so each one keeps its own target. */
  .row {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    padding: var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
  }

  .row-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
  }

  /* A verbatim engine reason is long: it wraps instead of widening the row. */
  .row-head :global(.badge) {
    max-width: 100%;
    overflow-wrap: anywhere;
  }

  .meta {
    margin: 0;
    font-size: var(--text-label);
    line-height: 1.4;
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }

  .announcement-unavailable {
    display: inline-flex;
    align-items: baseline;
    gap: var(--gap-sm);
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .announcement-unavailable > :global(svg) {
    flex-shrink: 0;
  }

  .announcement-unavailable > span {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .slug,
  .topic {
    font-family: var(--font-num);
    overflow-wrap: anywhere;
  }

  .note {
    margin: 0;
    font-size: var(--text-label);
    line-height: 1.4;
    color: var(--text-muted);
  }

  /* Always-visible row actions, 48×48 each with the 8px gap D26 asks for. */
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }

  .error {
    display: flex;
    align-items: flex-start;
    gap: var(--gap-sm);
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    color: var(--state-alert);
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

  .delete-dialog :global(.sheet-title) {
    min-width: 0;
    overflow-wrap: anywhere;
  }
</style>
