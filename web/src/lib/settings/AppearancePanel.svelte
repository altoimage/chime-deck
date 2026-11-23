<!--
  The Appearance section (design 05 §2.2, storyboard S18 F1/F4/F7/F8): this
  installation's theme, accent, interface scale, dashboard columns and three
  dashboard switches.

  The panel owns the runtime half of the accent-contrast obligation `tokens.css`
  assigns to this ticket: it measures the candidate accent against the ink the
  page would paint on it, states the ratio in words, and disables Save while the
  pair fails 4.5:1, so a wall panel cannot be saved into an unreadable accent.

  The tile grid below the field is a preview, not a control: it renders the
  *candidate* accent and column count with the same ink the accent would be
  drawn with, and its caption names the count in words — nothing is ever
  signalled by colour alone, and the preview never edits anything.

  The scale specimen is a preview in exactly the same sense, and it carries the
  real `[data-ui-scale]` attribute itself so the tokens' own rules (zoom plus
  the tap floors) draw it: the preview cannot drift from the panel it describes.

  Nothing is applied until the echo: the theme is written by the store's
  `#apply` when the PATCH resolves, so a refused value can never leave the page
  painted with a setting the server did not store. The accent is stored and read
  here only — it is a tile accent, never a palette override. Every field is a
  local draft until Save.
-->
<script lang="ts">
  import { DEFAULT_ACCENT_COLOR, readAccent } from '../accent.js';
  import { TEXT_CONTRAST_MIN, formatContrastRatio } from '../contrast.js';
  import { DEFAULT_SCALE, SCALE_STEPS, scaleLabel } from '../scale.js';
  import { settings } from '../settings.svelte.js';
  import Button from '../ui/Button.svelte';
  import ColorField from '../ui/ColorField.svelte';
  import SegmentedControl from '../ui/SegmentedControl.svelte';
  import Select from '../ui/Select.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { toastStore } from '../ui/toast.svelte.js';
  import SectionFooter from './SectionFooter.svelte';

  /** `ui.theme` default (settings_registry.py). */
  const DEFAULT_THEME = 'dark';

  /** `ui.dashboard_columns` default and range (settings_registry.py): 0 is auto-fit. */
  const DEFAULT_COLUMNS = 0;
  const MIN_COLUMNS = 0;
  const MAX_COLUMNS = 6;

  /** The three switches default on (settings_registry.py). */
  const DEFAULT_FLAG = true;

  /** The accent registry sentence, quoted so the hint and the server agree. */
  const ACCENT_HINT = 'must be a hex colour such as #4f9cf9';

  const THEME_OPTIONS = [
    { value: 'dark', label: 'Dark' },
    { value: 'light', label: 'Light' },
    { value: 'auto', label: 'Auto' }
  ];

  /** Illustrative tiles for the preview; the words carry the state, not the colour. */
  const PREVIEW_TILES = [
    { room: 'Front door', state: 'Pressed' },
    { room: 'Hallway', state: 'Idle' },
    { room: 'Garden', state: 'Offline' },
    { room: 'Porch', state: 'Pressed' },
    { room: 'Kitchen', state: 'Idle' },
    { room: 'Garage', state: 'Idle' }
  ];

  /**
   * The column choices: `0` is auto-fit, `1`–`6` are exact counts.
   *
   * A Select holds strings, so the draft stays a string and only the PATCH sends
   * a number — the backend's registry validates an integer.
   */
  function columnChoices(): { value: string; label: string; description: string }[] {
    const choices = [
      { value: String(DEFAULT_COLUMNS), label: 'Auto', description: 'Fit as many columns as the screen allows' }
    ];
    for (let count = 1; count <= MAX_COLUMNS; count += 1) {
      choices.push({
        value: String(count),
        label: String(count),
        description: count === 1 ? 'Exactly 1 column' : `Exactly ${count} columns`
      });
    }
    return choices;
  }

  const columnOptions = columnChoices();

  /** The offered interface scales (`ui.scale`, HSD-nlbf), as Select options. */
  const SCALE_OPTIONS = SCALE_STEPS.map((step) => ({ value: String(step), label: scaleLabel(step) }));

  let theme = $state(DEFAULT_THEME);
  let accent = $state<string | null>(DEFAULT_ACCENT_COLOR);
  let columns = $state(String(DEFAULT_COLUMNS));
  let scale = $state(String(DEFAULT_SCALE));
  let showClock = $state(DEFAULT_FLAG);
  let hideCursor = $state(DEFAULT_FLAG);
  let showControlRail = $state(DEFAULT_FLAG);
  let accentFieldInvalid = $state(false);
  let initialised = $state(false);
  let saving = $state(false);
  let saveError = $state<string | null>(null);
  let alive = false;

  const accentReading = $derived(readAccent(accent ?? DEFAULT_ACCENT_COLOR));

  /*
   * The measured pair, and the sentence that names what is wrong with it. The
   * ratio comes from `readAccent`, i.e. the pair the page actually paints, so
   * the panel can never measure one ink while the dashboard renders another.
   */
  const accentProblem = $derived(
    accentReading.ratio >= TEXT_CONTRAST_MIN
      ? null
      : `This accent only reaches ${formatContrastRatio(accentReading.ratio)} against --accent-contrast (${accentReading.ink}); it must reach ${TEXT_CONTRAST_MIN}:1.`
  );

  const accentInvalid = $derived(accentFieldInvalid || accentProblem !== null);

  // Comparison reads the store every time (never a captured copy), so adopting
  // the echo immediately makes the form clean again.
  const dirty = $derived(
    theme !== settings.text('ui.theme', DEFAULT_THEME) ||
      (accent ?? DEFAULT_ACCENT_COLOR) !==
        settings.text('ui.accent_color', DEFAULT_ACCENT_COLOR) ||
      columns !== String(savedColumns()) ||
      scale !== String(savedScale()) ||
      showClock !== settings.flag('ui.show_clock', DEFAULT_FLAG) ||
      hideCursor !== settings.flag('ui.hide_cursor', DEFAULT_FLAG) ||
      showControlRail !== settings.flag('ui.show_control_rail', DEFAULT_FLAG)
  );

  function savedColumns(): number {
    return settings.integer('ui.dashboard_columns', MIN_COLUMNS, MAX_COLUMNS, DEFAULT_COLUMNS);
  }

  function savedScale(): number {
    return settings.scale;
  }

  /** Re-read every key from the store, so the draft is the server's answer. */
  function rebase(): void {
    theme = settings.text('ui.theme', DEFAULT_THEME);
    accent = settings.text('ui.accent_color', DEFAULT_ACCENT_COLOR);
    columns = String(savedColumns());
    scale = String(savedScale());
    showClock = settings.flag('ui.show_clock', DEFAULT_FLAG);
    hideCursor = settings.flag('ui.hide_cursor', DEFAULT_FLAG);
    showControlRail = settings.flag('ui.show_control_rail', DEFAULT_FLAG);
    saveError = null;
  }

  $effect(() => {
    if (settings.loaded && !initialised) {
      rebase();
      initialised = true;
    }
  });

  $effect(() => {
    alive = true;
    return () => {
      alive = false;
    };
  });

  /** Only the changed keys travel; the columns count goes as a number. */
  function buildPatch(): Record<string, unknown> {
    const patch: Record<string, unknown> = {};
    if (theme !== settings.text('ui.theme', DEFAULT_THEME)) {
      patch['ui.theme'] = theme;
    }
    if ((accent ?? DEFAULT_ACCENT_COLOR) !== settings.text('ui.accent_color', DEFAULT_ACCENT_COLOR)) {
      patch['ui.accent_color'] = accent ?? DEFAULT_ACCENT_COLOR;
    }
    if (columns !== String(savedColumns())) {
      patch['ui.dashboard_columns'] = Number(columns);
    }
    if (scale !== String(savedScale())) {
      patch['ui.scale'] = Number(scale);
    }
    if (showClock !== settings.flag('ui.show_clock', DEFAULT_FLAG)) {
      patch['ui.show_clock'] = showClock;
    }
    if (hideCursor !== settings.flag('ui.hide_cursor', DEFAULT_FLAG)) {
      patch['ui.hide_cursor'] = hideCursor;
    }
    if (showControlRail !== settings.flag('ui.show_control_rail', DEFAULT_FLAG)) {
      patch['ui.show_control_rail'] = showControlRail;
    }
    return patch;
  }

  async function save(): Promise<void> {
    if (saving || !dirty) {
      return;
    }
    const patch = buildPatch();
    if (Object.keys(patch).length === 0) {
      return;
    }
    saving = true;
    saveError = null;
    const ok = await settings.save(patch);
    if (!alive) {
      return;
    }
    saving = false;
    if (ok) {
      rebase();
      toastStore.push('success', 'Appearance saved.');
    } else {
      // F5: the draft stays on screen, the server's sentence is the whole error.
      saveError = settings.error ?? 'the appearance settings could not be saved';
    }
  }

  /** The candidate column count the preview draws; auto-fit is drawn as 3-up. */
  function previewColumns(): number {
    return columns === String(DEFAULT_COLUMNS) ? 3 : Number(columns);
  }

  /** The preview's caption, so the count is stated in words as well as drawn. */
  function previewCaption(): string {
    return columns === String(DEFAULT_COLUMNS)
      ? 'Preview: auto-fit tiles'
      : `Preview: ${columns} columns`;
  }

  /** The step the scale preview draws: the draft, never the saved value. */
  const previewStep = $derived(Number(scale));

  /** The scale preview's caption: the step and its factor in words, no measurements. */
  const scaleCaption = $derived(describeScale(previewStep));

  function describeScale(step: number): string {
    if (step === DEFAULT_SCALE) {
      return 'Showing 100% — tap targets, text and sheets are at their base size.';
    }
    const factor = step / 100;
    if (step < DEFAULT_SCALE) {
      return `Showing ${step}% — tap targets, text and sheets are all ${factor}× smaller.`;
    }
    return `Showing ${step}% — tap targets, text and sheets are all ${factor}× larger.`;
  }
</script>

<div class="panel">
  <h2>Appearance</h2>

  {#if !settings.loaded}
    {#if settings.error !== null}
      <p class="error" role="alert">{settings.error}</p>
      <Button onclick={() => void settings.load()}>Retry</Button>
    {:else}
      <Spinner label="Loading appearance settings" />
    {/if}
  {:else}
    <p class="lead">
      These are this installation's settings: they apply to the dashboard every device sees,
      not to this browser tab.
    </p>

    <div class="fields">
      <SegmentedControl label="Theme" options={THEME_OPTIONS} bind:value={theme} />

      <div class="field">
        <ColorField
          label="Accent"
          bind:value={accent}
          bind:invalid={accentFieldInvalid}
          allowEmpty={false}
          error={accentProblem ?? undefined}
          hint={ACCENT_HINT}
        />
        <!--
          Always visible: the measured pair is the reason Save is disabled, so
          it must be readable before and after the failure, never only in an
          error state. The role changes so a failing ratio is announced.
        -->
        <p class="contrast" role={accentProblem === null ? 'status' : 'alert'}>
          Contrast {formatContrastRatio(accentReading.ratio)} on {accentReading.ink} — the
          requirement is {TEXT_CONTRAST_MIN}:1 or better.
        </p>
      </div>

      <section
        class="preview"
        aria-labelledby="appearance-preview-title"
        data-testid="accent-preview"
        data-preview-columns={columns}
      >
        <h3 id="appearance-preview-title">Preview</h3>
        <p class="preview-caption">{previewCaption()}</p>
        <div
          class="preview-grid"
          style:grid-template-columns={`repeat(${previewColumns()}, minmax(0, 1fr))`}
        >
          {#each PREVIEW_TILES.slice(0, previewColumns()) as tile (tile.room)}
            <div
              class="preview-tile"
              style:background={accentReading.accent}
              style:color={accentReading.ink}
            >
              <span class="preview-room">{tile.room}</span>
              <span class="preview-state">{tile.state}</span>
            </div>
          {/each}
        </div>
      </section>

      <!--
        A Select, not a segmented control or a stepper: design 05 §8 allows a
        segmented control only for ≤4 options (SegmentedControl hard-errors past
        four) and the five steps are not evenly spaced (15/15/15/20); a stepper
        admitting 86–149 would offer sizes the backend rejects and the preview
        never draws.
      -->
      <div class="field">
        <Select label="Interface scale" options={SCALE_OPTIONS} bind:value={scale} />
        <p class="hint">
          Grows the whole interface — tap targets, text and sheets — for a high-DPI panel whose
          display mode cannot be changed or should not be. It does not touch the system cursor
          or the compositor's own chrome.
        </p>
      </div>

      <section
        class="preview"
        aria-labelledby="scale-preview-title"
        data-testid="scale-preview"
        data-preview-scale={scale}
      >
        <h3 id="scale-preview-title">Scale preview</h3>
        <p class="preview-caption">{scaleCaption}</p>
        <!--
          The specimen carries the attribute itself, so the real
          `[data-ui-scale]` rules drive it: the same root-equivalent zoom and
          the same tap floors the panel itself uses. It is the device, not a
          lookalike, and nothing here is applied to :root.
        -->
        <div class="preview-frame">
          <div
            class="preview-device"
            data-ui-scale={scale === String(DEFAULT_SCALE) ? undefined : scale}
          >
            <div class="sample-row">
              <div class="sample-card">
                <span class="sample-card-name">Front door</span>
                <span class="sample-card-state">Pressed</span>
              </div>
              <div class="sample-rail">Volume</div>
              <div class="sample-setting">
                <span class="sample-setting-label">Show clock</span>
                <span class="sample-switch"></span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <div class="field">
        <Select label="Dashboard columns" options={columnOptions} bind:value={columns} />
        <p class="hint">
          0 = auto-fit: the grid fits as many columns as the screen allows. 1–6 = exactly that
          many columns.
        </p>
      </div>

      <Toggle
        label="Show clock"
        bind:checked={showClock}
        description="The clock in the dashboard header."
      />
      <Toggle
        label="Hide cursor"
        bind:checked={hideCursor}
        description="Kiosk only: hide the pointer after five seconds without a touch."
      />
      <Toggle
        label="Control rail"
        bind:checked={showControlRail}
        description="The volume buttons in the dashboard's right-hand rail. Mute and Settings always stay."
      />
    </div>

    <SectionFooter
      label="Save appearance"
      {dirty}
      invalid={accentInvalid}
      {saving}
      error={saveError}
      onsave={() => void save()}
    />
  {/if}
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

  .panel h2 {
    margin: 0;
    font-size: var(--text-title);
    line-height: 1.25;
  }

  .lead {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--text-body);
    line-height: 1.45;
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
    min-width: 0;
  }

  .contrast,
  .hint {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .contrast {
    color: var(--text);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  .hint {
    color: var(--text-muted);
  }

  .error {
    margin: 0;
    color: var(--state-alert);
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .preview {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    padding: var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
    min-width: 0;
  }

  .preview h3 {
    margin: 0;
    font-size: var(--text-label);
    line-height: 1.25;
    font-weight: 600;
  }

  .preview-caption {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--text-body);
    line-height: 1.45;
  }

  .preview-grid {
    display: grid;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .preview-tile {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: var(--gap-sm);
    border-radius: var(--radius-sm);
    min-width: 0;
    font-size: var(--text-body);
    line-height: 1.3;
  }

  .preview-room,
  .preview-state {
    overflow-wrap: anywhere;
  }

  .preview-state {
    font-weight: 700;
  }

  /*
   * The specimen's viewport. This box does not zoom, so its size is its RENDERED
   * size; the content inside is laid out in the specimen's own coordinate space
   * (the frame divided by the factor, exactly as <html> does to the real panel)
   * and magnified by the zoom, so it fills this frame at every step whatever its
   * own width is. 180px is the frame at every step while the samples fit in one
   * line — the five steps can then be compared like the same window on the same
   * panel — and it grows only when wrapping is the price of not clipping.
   */
  .preview-frame {
    display: flex;
    align-items: center;
    inline-size: 100%;
    min-block-size: 180px;
    overflow: hidden;
    background: var(--bg);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
  }

  .preview-device {
    inline-size: 100%;
    min-inline-size: 0;
  }

  /*
   * The samples sit in one line whenever the frame is wide enough for their own
   * tap floors, and wrap into more lines when it is not: the frame then grows
   * with them (it has a minimum, not a fixed height) rather than cropping a
   * sample, so nothing is ever clipped at any step or panel width.
   */
  .sample-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    inline-size: 100%;
    min-inline-size: 0;
    padding: var(--gap-sm);
  }

  /* A tile in miniature: its text is container-query sized, floored at 16px. */
  .sample-card {
    container-type: size;
    display: flex;
    flex: 1 1 calc(var(--tap) * 3);
    flex-direction: column;
    justify-content: center;
    gap: calc(var(--gap-sm) / 2);
    min-inline-size: 0;
    block-size: calc(var(--tap) * 2);
    padding: var(--gap-sm);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius-sm);
    line-height: 1.25;
  }

  .sample-card-name {
    font-size: max(16px, min(5.5cqi, 7cqb));
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }

  .sample-card-state {
    font-size: max(16px, min(9cqi, 11cqb));
    font-weight: 700;
    color: var(--text);
    overflow-wrap: anywhere;
  }

  /* A rail button: the primary tap size, so it grows with the interface. */
  .sample-rail {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    justify-content: center;
    min-inline-size: var(--tap-primary);
    block-size: var(--tap-primary);
    padding-inline: var(--gap-sm);
    background: var(--surface-hover);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: var(--text-label);
    font-weight: 600;
  }

  /* A settings row: body text beside a control that is a full tap target. */
  .sample-setting {
    display: flex;
    flex: 1 1 calc(var(--tap) * 2.5);
    align-items: center;
    gap: var(--gap-sm);
    min-inline-size: 0;
    min-block-size: calc(var(--tap) + var(--gap-sm));
    padding-inline: var(--gap-sm);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: var(--text-body);
    line-height: 1.25;
  }

  .sample-setting-label {
    min-inline-size: 0;
    overflow-wrap: anywhere;
  }

  .sample-switch {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    justify-content: flex-end;
    inline-size: calc(var(--tap) * 1.25);
    block-size: var(--tap);
    margin-inline-start: auto;
    padding-inline: calc(var(--gap-sm) / 2);
    background: var(--accent);
    border-radius: 999px;
  }

  /* The thumb: the same shape the real Toggle draws, so the row reads as a switch. */
  .sample-switch::after {
    content: '';
    inline-size: calc(var(--tap) * 0.6);
    block-size: calc(var(--tap) * 0.6);
    background: var(--accent-contrast);
    border-radius: 50%;
  }
</style>
