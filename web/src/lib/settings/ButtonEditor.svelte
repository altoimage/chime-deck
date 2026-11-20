<!--
  One chime button's editor (storyboard S11), in the same shape as
  `EntityEditor`: load ladder, draft, diff-PATCH save, the shared Save bar, and a
  native icon trigger that opens `IconPickerSheet`.

  The button contract is a **diff PATCH** (`PATCH /api/buttons/{id}` merges the
  submitted keys and re-validates the merged row), so every decision about what
  to send lives in `button-action.ts` and nothing here duplicates it: `draftFrom`
  opens the form, `changeBody` builds the edit body, `createBody` the create one,
  `clientProblems` the client-side sentences and `isDirty` the Save gate.

  Three things this build cannot show honestly, and each is stated rather than
  faked:

  - **No sound preview.** `GET /api/sounds/{id}/audio` does not exist here
    (HSD-w9eq), so the sound picker only lists and chooses. A preview control
    that silently did nothing would be worse than no control.
  - **The pre-render status and the _Pre-render now_ action come from the stored
    row's bake keys.** They render only on a build whose button rows carry those
    keys; a build without them renders no bake control at all. A templated
    phrase (`{{ … }}`/`{% … %}`) is never bakeable — it is rendered on every
    press — so its action is replaced by the reason.
  - **Paid-template confirmation now exists (HSD-s856).** A matching server 422 opens a
    request-scoped confirmation and resends only that save with its acknowledgement. The
    cost estimate comes from provider pricing in the Speech panel; this editor does not
    invent an estimate.

  D27 (speech switched off) is the fourth honesty case: a create offers only the
  Sound action, while an existing TTS button keeps its `actionType` exactly as
  saved — converting it would delete the configuration — and its speech fields
  stay readable but inert until the user explicitly asks for a sound button. The
  save is never blocked client-side for that state: the server refuses it with
  its own `invalid_setting` sentence, which `saveError` shows verbatim.
-->
<script lang="ts">
  import {
    ApiError,
    bakeButton,
    createButton,
    getTtsEngines,
    listButtonRows,
    listSounds,
    pressButton,
    updateButton,
    type ButtonCreateBody,
    type ButtonDetail,
    type ButtonWriteBody,
    type SoundRow,
    type TtsEnginesResponse
  } from '../api.js';
  import {
    changeBody,
    clientProblems,
    createBody,
    draftFrom,
    hasTemplateSyntax,
    isDirty,
    newDraft,
    parseAction,
    type ButtonDraft,
    type ClientProblem
  } from '../button-action.js';
  import { bakeStateOf, canPreRender, describeBakeStatus, type BakeBadgeKind } from '../bake.js';
  import { describePress, type PressReport } from '../press-report.js';
  import { settings } from '../settings.svelte.js';
  import { ENGINE_LABELS, engineChoices, speechDisabled } from '../tts-engines.js';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import Dialog from '../ui/Dialog.svelte';
  import Icon from '../ui/Icon.svelte';
  import Input from '../ui/Input.svelte';
  import SegmentedControl from '../ui/SegmentedControl.svelte';
  import Select from '../ui/Select.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import Stepper from '../ui/Stepper.svelte';
  import Textarea from '../ui/Textarea.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { toastStore } from '../ui/toast.svelte.js';
  import ButtonPressResult from './ButtonPressResult.svelte';
  import IconPickerSheet from './IconPickerSheet.svelte';
  import SectionFooter from './SectionFooter.svelte';

  interface Props {
    /** The stored button to edit, or null to create a new one (`/settings/buttons/add`). */
    id: number | null;
    /** Leave the editor; the parent navigates back to the list. */
    onback: () => void;
    /**
     * Report a newly created button's id so the panel can navigate to its editor.
     * Optional and defaulted, so a test can mount this editor on its own.
     */
    oncreated?: (button: ButtonDetail) => void;
    /** The back button's text, so a wizard can name its own previous screen. */
    backLabel?: string;
  }

  let { id, onback, oncreated = () => {}, backLabel = 'Back to buttons' }: Props = $props();

  /** The two modes differ only in what a save means and what the title says. */
  const mode = $derived<'create' | 'edit'>(id === null ? 'create' : 'edit');

  /**
   * Documented registry defaults and ranges (`settings_registry.py`), so the
   * status lines below are correct even before a load has landed.
   */
  const DEDUPE_MIN = 0;
  const DEDUPE_MAX = 3600;
  const DEDUPE_FALLBACK = 3;
  const VOLUME_MIN = 0;
  const VOLUME_MAX = 100;
  const VOLUME_FALLBACK = 70;
  const ENGINE_FALLBACK = 'ha_tts';

  /** What a button with no stored icon is published as (`mqtt/discovery.py::DEFAULT_BUTTON_ICON`). */
  const DEFAULT_ICON = 'mdi:doorbell';

  /** `null` means "still loading" until the list has answered. */
  let row = $state<ButtonDetail | null>(null);
  /** Only ever rendered in edit mode; a create has no row request to wait for. */
  let loading = $state(true);
  let loadError = $state<string | null>(null);

  let draft = $state<ButtonDraft>(newDraft());

  let sounds = $state<SoundRow[]>([]);
  let loadingSounds = $state(false);
  let soundsError = $state<string | null>(null);

  let engines = $state<TtsEnginesResponse | null>(null);
  let enginesError = $state<string | null>(null);

  let saving = $state(false);
  let saveError = $state<string | null>(null);
  let paidConfirmationOpen = $state(false);
  // This is the exact failed request body, separate from the draft; its flag is added only
  // to the one accepted retry and can never leak into a later save.
  let pendingPaidBody = $state<ButtonCreateBody | ButtonWriteBody | null>(null);
  let paidConfirmationSentence = $state<string | null>(null);
  let slugChangeWarning = $state<string | null>(null);
  let attempted = $state(false);
  let iconSheetOpen = $state(false);

  let pressing = $state(false);
  let pressError = $state<string | null>(null);
  let report = $state<PressReport | null>(null);

  let baking = $state(false);
  let bakeError = $state<string | null>(null);

  /** Guards against an older load (a previous id) overwriting a newer one. */
  let loadSeq = 0;
  let soundsSeq = 0;
  let enginesSeq = 0;

  function describe(cause: unknown, fallback: string): string {
    return cause instanceof ApiError ? cause.message : fallback;
  }

  /**
   * The sound library. A failure is tolerated — the picker area explains itself
   * and offers Retry — because the rest of the button can still be edited.
   */
  async function loadSounds(): Promise<void> {
    soundsSeq += 1;
    const seq = soundsSeq;
    loadingSounds = true;
    try {
      const list = await listSounds();
      if (seq !== soundsSeq) {
        return;
      }
      sounds = list;
      soundsError = null;
    } catch (cause) {
      if (seq !== soundsSeq) {
        return;
      }
      sounds = [];
      soundsError = describe(cause, 'the sound library could not be loaded');
    } finally {
      if (seq === soundsSeq) {
        loadingSounds = false;
      }
    }
  }

  /**
   * The speech engines. A failure is tolerated too: `engineChoices(null, …)` and
   * `speechDisabled(null, …)` already fall back to the stored default setting
   * and to "availability unknown", so the picker stays usable.
   */
  async function loadEngines(): Promise<void> {
    enginesSeq += 1;
    const seq = enginesSeq;
    try {
      const status = await getTtsEngines();
      if (seq !== enginesSeq) {
        return;
      }
      engines = status;
      enginesError = null;
    } catch (cause) {
      if (seq !== enginesSeq) {
        return;
      }
      engines = null;
      enginesError = describe(cause, 'the speech engines could not be loaded');
    }
  }

  async function load(wanted: number | null): Promise<void> {
    loadSeq += 1;
    const seq = loadSeq;

    loading = wanted !== null;
    loadError = null;
    saveError = null;
    slugChangeWarning = null;
    pressError = null;
    report = null;
    baking = false;
    bakeError = null;
    row = null;
    draft = newDraft();

    // Both are independent of the row and tolerated on their own.
    void loadSounds();
    void loadEngines();

    if (wanted === null) {
      // Create mode: there is no stored row to find, and `newDraft()` stands in.
      return;
    }

    try {
      const rows = await listButtonRows();
      if (seq !== loadSeq) {
        return;
      }
      const found = rows.find((candidate) => candidate.id === wanted) ?? null;
      row = found;
      if (found !== null) {
        draft = draftFrom(found);
      }
    } catch (cause) {
      if (seq !== loadSeq) {
        return;
      }
      loadError = describe(cause, 'the button could not be loaded');
    } finally {
      if (seq === loadSeq) {
        loading = false;
      }
    }
  }

  // `id` is the only reactive value read here; `load` assigns state this effect
  // never reads, so a finished load cannot re-trigger it.
  $effect(() => {
    void load(id);
  });

  /** `tts.default_engine`, the documented fallback while the engines route is silent. */
  const defaultEngine = $derived(settings.text('tts.default_engine', ENGINE_FALLBACK));
  const defaultDedupe = $derived(
    settings.integer('buttons.default_dedupe_seconds', DEDUPE_MIN, DEDUPE_MAX, DEDUPE_FALLBACK)
  );
  const defaultVolume = $derived(
    settings.integer('audio.volume', VOLUME_MIN, VOLUME_MAX, VOLUME_FALLBACK)
  );

  /** `null` is "no override", so the effective value is the app default until it is set. */
  const dedupeEffective = $derived(draft.dedupeSeconds ?? defaultDedupe);
  const volumeEffective = $derived(draft.volumeOverride ?? defaultVolume);

  /** D27: the engines route is authoritative here, the stored setting the fallback. */
  const speechOff = $derived(speechDisabled(engines, defaultEngine));

  /** The editor's own reason for withholding a bake from a saved template phrase. */
  const TEMPLATE_NOT_BAKEABLE =
    "This button's saved phrase is a Home Assistant template, so it is rendered on every press and cannot be pre-rendered. Save a static phrase to pre-render it once.";

  /** What the speech branch says about the stored row's pre-render state. */
  interface BakeView {
    badge: { kind: BakeBadgeKind; label: string };
    /** The format sentence for a baked row, null when nothing is baked. */
    formatLine: string | null;
    /** Whether the *Pre-render now* action is offered. */
    eligible: boolean;
    /** Why no action is offered, or null when one is (see the markup). */
    blockedReason: string | null;
  }

  /**
   * The segment list, with Speech omitted while speech is switched off — a
   * choice that exists but cannot work is not an option (D27, storyboard S11 F8).
   */
  const actionOptions = $derived([
    { value: 'sound', label: 'Sound', icon: 'mdi:speaker' },
    ...(speechOff ? [] : [{ value: 'tts', label: 'Speech', icon: 'mdi:volume-high' }])
  ]);

  /**
   * An existing TTS button while speech is off: keep the action exactly as
   * saved and show it as static text. Switching it here would silently delete
   * the phrase and engine the button still stores.
   */
  const ttsFrozen = $derived(speechOff && draft.actionType === 'tts');

  /**
   * The stored row's pre-render state, or null when this server has no bake contract.
   *
   * Read from `row` and never from the draft: the server bakes the saved phrase, so an
   * unsaved edit cannot change what a bake would produce (storyboard S11 F6).
   *
   * The badge and the format line describe what is on disk, so they are reported in every
   * state — switching speech off does not un-bake a button. The *action* is withheld while
   * the button is frozen (D27): `ttsFrozen` means speech is off, which is exactly the 422 the
   * bake route answers, and a control that cannot work is not an option (storyboard S11 F8).
   */
  const bakeView = $derived.by((): BakeView | null => {
    if (row === null || row.action_type !== 'tts') {
      return null;
    }
    const state = bakeStateOf(row);
    if (state === null) {
      return null;
    }
    // A plain if/else, because the project bans nested ternaries.
    let formatLine: string | null = null;
    if (state.kind === 'baked') {
      if (state.format === null) {
        formatLine = 'Pre-rendered · reused on press';
      } else {
        formatLine = `Pre-rendered as ${state.format} · reused on press`;
      }
    }
    const eligible = canPreRender(row) && !ttsFrozen;
    // With speech off set aside, ineligibility has exactly one other cause: the saved
    // phrase is a template. That case needs a sentence; the frozen one needs none,
    // because the speech-off warning is already on screen above this block.
    let blockedReason: string | null = null;
    if (!eligible && !ttsFrozen) {
      blockedReason = TEMPLATE_NOT_BAKEABLE;
    }
    return { badge: describeBakeStatus(state), formatLine, eligible, blockedReason };
  });

  /** The engine row a frozen button shows instead of a picker (see the markup). */
  const frozenEngineLabel = $derived(
    draft.ttsEngine === ''
      ? `Use the default engine (${defaultEngine})`
      : (ENGINE_LABELS[draft.ttsEngine] ?? draft.ttsEngine)
  );

  /** One row of the kit's pickers; the shape `Select` accepts. */
  interface PickerOption {
    value: string;
    label: string;
    description?: string;
    disabled?: boolean;
    reason?: string;
  }

  /**
   * What a library row can honestly say about its file: the indexed format and
   * duration when they exist, and the one bad state the server reports.
   * Nothing here ever claims a file is present — `missing: false` is the absence
   * of a signal, not proof of a playable file.
   */
  function soundDescription(sound: SoundRow): string {
    if (sound.missing) {
      return 'The file is missing — a press will fail.';
    }
    const known: string[] = [];
    if (sound.format !== null) {
      known.push(sound.format);
    }
    if (sound.duration_ms !== null) {
      known.push(`${Math.round(sound.duration_ms / 1000)} s`);
    }
    return known.length === 0
      ? 'The library lists no format or duration for this file.'
      : known.join(' · ');
  }

  const soundOptions = $derived.by((): PickerOption[] => {
    const options: PickerOption[] = sounds.map((sound) => ({
      value: String(sound.id),
      label: sound.name,
      description: sound.missing
        ? 'This sound is still indexed, but its file is missing.'
        : soundDescription(sound),
      ...(sound.missing
        ? {
            disabled: true,
            reason:
              'This file is missing from the sound library folder — put it back or rescan before choosing it.'
          }
        : {})
    }));

    // A stored sound the library no longer lists stays visible and explained
    // instead of quietly reading as "nothing chosen".
    const chosen = draft.soundChoice.trim();
    if (chosen !== '' && !sounds.some((sound) => String(sound.id) === chosen)) {
      options.push({
        value: chosen,
        label: 'The saved sound is not in the library',
        disabled: true,
        reason: 'This button still stores a sound id the library does not list — choose a sound instead.'
      });
    }

    return options;
  });

  const engineOptions = $derived(engineChoices(engines, defaultEngine));

  /** A saved engine that has become unavailable must be explained, never swapped. */
  const engineReason = $derived.by((): string | null => {
    const chosen = engineOptions.find((choice) => choice.value === draft.ttsEngine);
    if (chosen === undefined || !chosen.disabled) {
      return null;
    }
    return chosen.reason ?? null;
  });

  const problems = $derived(clientProblems(draft, mode));
  const problemMessages = $derived(problems.map((problem) => problem.message).join(' '));

  /** The sentence under the field that caused it, once a save has been attempted. */
  function problemFor(field: ClientProblem['field']): string | undefined {
    if (!attempted) {
      return undefined;
    }
    return problems.find((problem) => problem.field === field)?.message;
  }

  const dirty = $derived(mode === 'create' ? true : row !== null && isDirty(draft, row));

  const title = $derived(mode === 'create' ? 'New button' : (row?.name ?? 'Button'));

  function needsPaidTemplateConfirmation(cause: unknown): cause is ApiError {
    if (
      !(cause instanceof ApiError) ||
      cause.detail === null ||
      typeof cause.detail !== 'object'
    ) {
      return false;
    }
    return 'requires' in cause.detail && cause.detail.requires === 'acknowledge_paid_template';
  }

  async function submitBody(
    body: ButtonCreateBody | ButtonWriteBody,
    acknowledged = false
  ): Promise<void> {
    if (saving) {
      return;
    }
    saving = true;
    saveError = null;
    // D25's flag is request-scoped: it is copied onto this request only and never added to
    // `draft`, `row`, or a later save body.
    const requestBody = acknowledged ? { ...body, acknowledge_paid_template: true } : body;
    try {
      if (row === null) {
        const created = await createButton(requestBody as ButtonCreateBody);
        toastStore.push('success', 'Button created.');
        oncreated(created);
      } else {
        const saved = await updateButton(row.id, requestBody as ButtonWriteBody);
        row = saved;
        draft = draftFrom(saved);
        slugChangeWarning = saved.slug_change?.warning ?? null;
        toastStore.push('success', 'Button saved.');
      }
    } catch (cause) {
      // The draft is kept: a rejected save must not throw away what was typed.
      saveError = describe(cause, 'the button could not be saved');
      if (!acknowledged && needsPaidTemplateConfirmation(cause)) {
        pendingPaidBody = body;
        paidConfirmationSentence = cause.message;
        paidConfirmationOpen = true;
      }
    } finally {
      saving = false;
    }
  }

  async function save(): Promise<void> {
    attempted = true;
    if (problems.length > 0 || saving) {
      // Refused client-side: NOTHING is sent.
      return;
    }
    const body = row === null ? createBody(draft) : changeBody(draft, row);
    await submitBody(body);
  }

  function cancelPaidConfirmation(): void {
    paidConfirmationOpen = false;
    pendingPaidBody = null;
    paidConfirmationSentence = null;
  }

  async function acceptPaidConfirmation(): Promise<void> {
    const body = pendingPaidBody;
    if (body === null || saving) {
      return;
    }
    pendingPaidBody = null;
    paidConfirmationOpen = false;
    paidConfirmationSentence = null;
    await submitBody(body, true);
  }

  /**
   * Pre-render the stored phrase now and show the server's refreshed row in place.
   *
   * The answer is the stored row, so the badge flips without a reload and without a
   * second request; the draft is never touched, because unsaved edits belong to the
   * user, not to a bake (storyboard S11 F6). A refusal keeps the stored artefact —
   * a bake is a cost optimisation, never a correctness requirement — so the badge
   * keeps reporting what is stored while this sentence carries the server's words.
   */
  async function bakeNow(): Promise<void> {
    if (row === null || baking) {
      return;
    }
    baking = true;
    bakeError = null;
    try {
      row = await bakeButton(row.id);
    } catch (cause) {
      bakeError = describe(cause, 'the phrase could not be pre-rendered');
    } finally {
      baking = false;
    }
  }

  async function testPress(): Promise<void> {
    if (row === null || pressing) {
      return;
    }
    pressing = true;
    pressError = null;
    report = null;
    // The response carries no timing, so the round trip is measured here.
    const started = performance.now();
    try {
      const response = await pressButton(row.id);
      const elapsed = performance.now() - started;
      const soundName = sounds.find((sound) => sound.id === row?.sound_id)?.name ?? null;
      report = describePress(response, row, soundName, elapsed, defaultEngine);
    } catch (cause) {
      pressError = describe(cause, 'the button could not be pressed');
    } finally {
      pressing = false;
    }
  }
</script>

<div class="editor">
  <div class="topline">
    <Button onclick={onback}>{backLabel}</Button>
    <h2>{title}</h2>
  </div>

  {#if mode === 'edit' && loading}
    <Spinner label="Loading button" />
  {:else if loadError !== null}
    <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{loadError}</span></p>
    <div class="actions">
      <Button onclick={() => void load(id)}>Retry</Button>
    </div>
  {:else if mode === 'edit' && row === null}
    <section class="notice">
      <h2>This button is not configured</h2>
      <p>There is no stored button with the id “{id}”.</p>
      <div class="actions">
        <Button onclick={onback}>{backLabel}</Button>
      </div>
    </section>
  {:else}
    <div class="fields">
      <Input label="Name" bind:value={draft.name} inputmode="text" error={problemFor('name')} />

      <Input
        label="Slug"
        bind:value={draft.slug}
        inputmode="text"
        hint={mode === 'create'
          ? 'Leave blank to derive it from the name. Lowercase letters, digits, hyphens and underscores only.'
          : "The slug names this button's Home Assistant entity. Lowercase letters, digits, hyphens and underscores only."}
        error={problemFor('slug')}
      />

      {#if slugChangeWarning !== null}
        <!-- The server's own sentence about the rename that just happened. -->
        <p class="warning" role="status"><Icon name="mdi:alert-circle-outline" size={20} /><span>{slugChangeWarning}</span></p>
      {/if}

      {#if draft.slug.trim() !== '' && row !== null && draft.slug.trim() !== row.slug}
        <!--
          Shown BEFORE any save, and only while the slug really differs: a
          warning that stayed on screen for an untouched field would be a lie.
        -->
        <p class="warning" role="status">
          <Icon name="mdi:alert-circle-outline" size={20} />
          <span>Changing the slug creates a NEW Home Assistant entity. The old entity and its topics are removed on the next republish; update any automation that references the old entity.</span>
        </p>
      {/if}

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
            <Icon name={draft.icon ?? DEFAULT_ICON} size={24} />
          </span>
          <span class="icon-text">
            {draft.icon ?? `No icon set — Home Assistant is told ${DEFAULT_ICON}.`}
          </span>
          <Icon name="mdi:chevron-down" size={24} />
        </button>
        <p class="muted">
          A button's icon must be an mdi: icon — it is published to Home Assistant.
        </p>
      </div>

      {#if ttsFrozen}
        <div class="field">
          <span class="field-label">Action type</span>
          <!-- Static text, not a disabled segment: D27 keeps the saved action. -->
          <p class="static-value frozen-value"><Icon name="mdi:alert-circle-outline" size={20} /><span>Speech — disabled</span></p>
          <p class="warning" role="status"><Icon name="mdi:alert-circle-outline" size={20} /><span>Speech is switched off (tts.default_engine is none). This button's speech configuration is kept as saved, not deleted.</span></p>
          <div class="actions">
            <Button onclick={() => (draft.actionType = 'sound')}>
              Make this button play a sound
            </Button>
          </div>
        </div>
      {:else}
        <!--
          `change` rather than `bind:value`: binding would widen the narrowed
          `ButtonAction` union and svelte-check would reject it.
        -->
        <SegmentedControl
          label="Action type"
          options={actionOptions}
          value={draft.actionType}
          change={(next) => (draft.actionType = parseAction(next))}
        />
        {#if speechOff}
          <p class="warning" role="status"><Icon name="mdi:alert-circle-outline" size={20} /><span>Speech unavailable: speech is switched off.</span></p>
        {/if}
      {/if}

      {#if draft.actionType === 'sound'}
        <!--
          The browser preview route `GET /api/sounds/{id}/audio` now exists and
          is offered by the Sounds panel, not this picker. The server enforces
          an unknown sound id with a 422 but does NOT enforce `missing`: a row
          that still exists is accepted. Disabling missing rows here is the
          client-side half of that rule; the Sounds panel's "Cannot be assigned
          to a button until the file returns." sentence is the honest UI promise.
        -->
        {#if soundsError !== null}
          <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{soundsError}</span></p>
          <div class="actions">
            <Button onclick={() => void loadSounds()}>Retry</Button>
          </div>
        {:else if loadingSounds && sounds.length === 0}
          <Spinner label="Loading sounds" />
        {:else if sounds.length === 0}
          <p class="muted">The sound library is empty — no sounds are indexed yet.</p>
        {:else}
          <Select label="Sound library" options={soundOptions} bind:value={draft.soundChoice} />
        {/if}
      {:else}
        <Textarea
          label="Speech phrase"
          bind:value={draft.ttsText}
          rows={3}
          inputmode="text"
          hint="The phrase the button speaks when it is pressed."
          disabled={ttsFrozen}
          error={problemFor('tts_text')}
        />

        {#if hasTemplateSyntax(draft.ttsText)}
          <div class="status-line">
            <Badge kind="warn" icon="mdi:alert-circle-outline" label="Rendered on every press" />
            <p class="warning"><Icon name="mdi:alert-circle-outline" size={20} /><span>This phrase is rendered on every press — remove the template to pre-render it once.</span></p>
          </div>
        {/if}

        {#if bakeView !== null}
          <div class="bake">
            <Badge kind={bakeView.badge.kind} label={bakeView.badge.label} />
            {#if bakeView.formatLine !== null}
              <p class="muted">{bakeView.formatLine}</p>
            {/if}
            {#if bakeView.eligible}
              <div class="actions">
                <Button loading={baking} onclick={() => void bakeNow()}>Pre-render now</Button>
              </div>
            {:else if bakeView.blockedReason !== null}
              <p class="muted">{bakeView.blockedReason}</p>
            {/if}
            {#if bakeError !== null}
              <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{bakeError}</span></p>
            {/if}
          </div>
        {/if}

        {#if ttsFrozen}
          <!--
            `ui/Select.svelte` has no `disabled` prop, so the frozen engine is
            rendered as the saved choice in plain text instead of a picker that
            could change it. Faking read-only with pointer-events would leave it
            reachable by keyboard, and marking every option disabled would claim
            the engines are unavailable, which is a different (untrue) story.
          -->
          <div class="field">
            <span class="field-label">Engine override</span>
            <p class="static-value">{frozenEngineLabel}</p>
          </div>
        {:else}
          <Select label="Engine override" options={engineOptions} bind:value={draft.ttsEngine} />
        {/if}

        {#if engines === null && enginesError === null}
          <!-- The picker above stays usable: availability is unknown, not bad. -->
          <Spinner label="Loading speech engines" />
        {/if}

        {#if enginesError !== null}
          <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{enginesError}</span></p>
          <div class="actions">
            <Button onclick={() => void loadEngines()}>Retry</Button>
          </div>
        {/if}

        {#if engines !== null && engines.warning !== null}
          <p class="warning" role="status"><Icon name="mdi:alert-circle-outline" size={20} /><span>{engines.warning}</span></p>
        {/if}

        {#if engineReason !== null}
          <!-- Verbatim, because an unavailable engine's reason is the server's sentence. -->
          <p class="warning" role="status"><Icon name="mdi:alert-circle-outline" size={20} /><span>{engineReason}</span></p>
        {/if}
      {/if}

      <!--
        `null` is the "no override" value and a Stepper cannot express it, so an
        override is only ever set by actually stepping and is cleared explicitly
        — never silently rewritten to the app default.
      -->
      <div class="field">
        <Stepper
          label="Dedupe window"
          value={dedupeEffective}
          min={DEDUPE_MIN}
          max={DEDUPE_MAX}
          unit=" s"
          change={(next) => (draft.dedupeSeconds = next)}
        />
        {#if draft.dedupeSeconds === null}
          <p class="muted">Using the app default ({defaultDedupe} s).</p>
        {:else}
          <p class="muted">Overridden for this button.</p>
          <div class="actions">
            <Button onclick={() => (draft.dedupeSeconds = null)}>Use the default</Button>
          </div>
        {/if}
      </div>

      <div class="field">
        <Stepper
          label="Volume"
          value={volumeEffective}
          min={VOLUME_MIN}
          max={VOLUME_MAX}
          unit="%"
          change={(next) => (draft.volumeOverride = next)}
        />
        {#if draft.volumeOverride === null}
          <p class="muted">Using the app default ({defaultVolume}%).</p>
        {:else}
          <p class="muted">Overridden for this button.</p>
          <div class="actions">
            <Button onclick={() => (draft.volumeOverride = null)}>Use the default</Button>
          </div>
        {/if}
        <p class="muted">This is the app's own gain; it does not change the operating-system mixer volume.</p>
      </div>

      <Toggle
        label="Quiet-hours exempt"
        description="Pressed during quiet hours, this button still plays at its own volume."
        bind:checked={draft.quietHoursExempt}
      />

      {#if attempted && problems.length > 0}
        <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{problemMessages}</span></p>
      {/if}

      {#if row !== null}
        <div class="actions">
          <Button loading={pressing} onclick={() => void testPress()}>Test</Button>
        </div>
        <ButtonPressResult pressing={pressing} error={pressError} report={report} />
      {:else}
        <!-- A create has no id to press yet, so no control is offered. -->
        <p class="muted">Save the button before testing it.</p>
      {/if}

      <SectionFooter
        label={mode === 'create' ? 'Create button' : 'Save'}
        dirty={dirty}
        saving={saving}
        error={saveError}
        onsave={save}
      />
    </div>

    <IconPickerSheet
      bind:open={iconSheetOpen}
      value={draft.icon}
      resolvedIcon={draft.icon ?? 'mdi:help-circle-outline'}
      resolvedSource={draft.icon !== null ? 'user' : 'default'}
      allowCustom={false}
      onselect={(icon) => (draft.icon = icon)}
    />
  {/if}
</div>

{#if paidConfirmationOpen && paidConfirmationSentence !== null}
  <Dialog
    bind:open={paidConfirmationOpen}
    title="Paid template cost warning"
    message={`${paidConfirmationSentence} A template is rendered on every press and can bill. Remove the template syntax to bake a static phrase once, or accept the repeated estimated charge for this phrase.`}
    cancelLabel="Cancel"
    confirmLabel="Accept paid-template cost"
    onconfirm={() => void acceptPaidConfirmation()}
    oncancel={cancelPaidConfirmation}
  />
{/if}

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

  /* The saved value of a field that cannot be edited in this state. */
  .static-value {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .frozen-value {
    display: flex;
    align-items: flex-start;
    gap: var(--gap-sm);
  }

  .frozen-value :global(svg) {
    flex-shrink: 0;
    margin-top: 0.2em;
  }

  .frozen-value > span {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .status-line {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--gap-sm);
  }

  /*
    The stored row's pre-render state: a badge, its words, then the action. The
    reason lines reuse `.muted`/`.error`, which already wrap long engine text.
  */
  .bake {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    min-width: 0;
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

  .muted {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }

  .warning,
  .error {
    display: flex;
    align-items: flex-start;
    gap: var(--gap-sm);
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .warning {
    color: var(--state-warn);
  }

  .error {
    color: var(--state-alert);
  }

  .warning :global(svg),
  .error :global(svg) {
    flex-shrink: 0;
    margin-top: 0.2em;
  }

  .warning > span,
  .error > span {
    min-width: 0;
    overflow-wrap: anywhere;
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
