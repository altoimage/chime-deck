<!--
  The About section (storyboards S16 F1–F9): what this build is, where its data
  lives, the log excerpt, and configuration export/import.

  The cards stay independent on purpose. The facts card owns `GET /api/about`; the
  export and import cards are local actions that must stay usable when that one
  request fails.

  Two honesty rules drive the layout:
    - a control is rendered only when it can work. The log anchor exists only when
      `log_available` is true, and Apply is disabled until a dry run of *this*
      document has succeeded — never a button that silently does nothing.
    - the import is a merge, so the per-section table says `removed: 0 · merge
      keeps absent rows` beside the number instead of leaving a zero to be
      misread as a deletion count.

  The pre-render card exists only after `GET /api/tts/bake_usage` answers with both
  measured directories: an older build therefore gets no card, no disabled button and
  no invented zero. Inside it, all four batch outcomes stay separate — an empty
  category says `None.` rather than vanishing — and every byte figure is the device's
  own measurement, because the browser cannot see those directories. Refusals and
  per-button failures are printed in the server's own words, since only the server
  knows why it declined.
-->
<script lang="ts">
  import {
    ApiError,
    bakeAllPhrases,
    exportConfigUrl,
    getAbout,
    getBakeUsage,
    importConfig,
    listButtonRows,
    logExcerptUrl,
    request,
    type AboutResponse,
    type BakeAllResponse,
    type BakeUsageResponse,
    type ButtonDetail,
    type ImportReport
  } from '../api.js';
  import {
    bakeReportIsEmpty,
    describeBakeReport,
    isBakeAllResponse,
    isBakeUsage
  } from '../bake.js';
  import { compareDocuments, formatUptime, type TransferDiff } from '../config-transfer.js';
  import Button from '../ui/Button.svelte';
  import Card from '../ui/Card.svelte';
  import Icon from '../ui/Icon.svelte';
  import Sheet from '../ui/Sheet.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { toastStore } from '../ui/toast.svelte.js';

  /** `null` means "no answer yet" — the spinner, never a blank facts list. */
  let about = $state<AboutResponse | null>(null);
  let aboutError = $state<string | null>(null);
  let factsSeq = 0;
  let alive = false;

  /** Off by default: a redacted export is the safe one. */
  let includeSecrets = $state(false);

  /** The parsed document, or `null` until a file has been read successfully. */
  let document = $state<unknown>(null);
  let documentName = $state('');
  let documentSize = $state(0);
  let documentError = $state<string | null>(null);
  let reading = $state(false);

  /** The native file input, read through its own element: the browser owns it. */
  let fileInput: HTMLInputElement | undefined = $state();

  let dryRunning = $state(false);
  let dryReport = $state<ImportReport | null>(null);
  let dryDiff = $state<TransferDiff | null>(null);
  let importError = $state<{ message: string; problems: string[] } | null>(null);
  let applying = $state(false);
  let applied = $state(false);
  let confirmOpen = $state(false);

  /**
   * The device-measured speech usage, or `null` until the endpoint answers. The probe is
   * also the feature signal: a build without the route keeps this `null` and the whole
   * pre-render card stays out of the page.
   */
  let bakeUsage = $state<BakeUsageResponse | null>(null);
  let bakeSeq = 0;
  let bakeAlive = false;
  let baking = $state(false);
  let bakeReport = $state<BakeAllResponse | null>(null);
  let bakeNames = $state<ReadonlyMap<number, string>>(new Map());
  let bakeError = $state<string | null>(null);

  const sectionRows = $derived(
    dryDiff === null
      ? []
      : [
          { label: 'Settings', diff: dryDiff.settings },
          { label: 'Entities', diff: dryDiff.entities },
          { label: 'Sounds', diff: dryDiff.sounds },
          { label: 'Buttons', diff: dryDiff.buttons }
        ]
  );

  const bakeSections = $derived(
    bakeReport === null ? [] : describeBakeReport(bakeReport, bakeNames)
  );
  const bakeReportEmpty = $derived(bakeReport !== null && bakeReportIsEmpty(bakeReport));

  function messageFor(cause: unknown, fallback: string): string {
    return cause instanceof ApiError ? cause.message : fallback;
  }

  async function refreshFacts(): Promise<void> {
    const sequence = ++factsSeq;
    about = null;
    aboutError = null;
    try {
      const answer = await getAbout();
      if (!alive || sequence !== factsSeq) {
        return;
      }
      about = answer;
    } catch (cause) {
      if (!alive || sequence !== factsSeq) {
        return;
      }
      aboutError = messageFor(cause, 'the device facts could not be read');
    }
  }

  $effect(() => {
    alive = true;
    void refreshFacts();
    return () => {
      alive = false;
      factsSeq += 1;
    };
  });

  /**
   * Probe the usage route once to find out whether this build has it.
   *
   * A rejection, or a body that is not a usage report, is the feature signal rather than
   * an error: no card is rendered, so nothing is printed for it either.
   */
  async function probeBakeUsage(): Promise<void> {
    const sequence = ++bakeSeq;
    try {
      const answer = await getBakeUsage();
      if (!bakeAlive || sequence !== bakeSeq) {
        return;
      }
      bakeUsage = isBakeUsage(answer) ? answer : null;
    } catch {
      if (!bakeAlive || sequence !== bakeSeq) {
        return;
      }
      bakeUsage = null;
    }
  }

  /** Re-read the figures a batch changed; a failure keeps the last good measurement. */
  async function refreshBakeUsage(): Promise<void> {
    try {
      const answer = await getBakeUsage();
      if (bakeAlive && isBakeUsage(answer)) {
        bakeUsage = answer;
      }
    } catch {
      // The route already proved itself on mount, so stale figures beat none at all.
    }
  }

  $effect(() => {
    bakeAlive = true;
    void probeBakeUsage();
    return () => {
      bakeAlive = false;
      bakeSeq += 1;
    };
  });

  /**
   * Re-render every static phrase, then refresh the figures it changed.
   *
   * The route is synchronous and can take many seconds, so the button keeps its loading
   * state for the whole call. The name lookup runs beside the batch instead of after it,
   * and a failed lookup only costs the names: ids fall back to `Button #<id>`.
   */
  async function reRenderAll(): Promise<void> {
    if (baking) {
      return;
    }
    baking = true;
    bakeError = null;
    bakeReport = null;
    try {
      const [answer, rows] = await Promise.all([
        bakeAllPhrases(),
        listButtonRows().catch((): ButtonDetail[] => [])
      ]);
      if (!bakeAlive) {
        return;
      }
      if (!isBakeAllResponse(answer)) {
        bakeError = 'the server answered, but its answer did not describe the batch';
        return;
      }
      bakeReport = answer;
      bakeNames = new Map(rows.map((row): [number, string] => [row.id, row.name]));
      await refreshBakeUsage();
    } catch (cause) {
      if (bakeAlive) {
        bakeError = messageFor(cause, 'the batch could not be run');
      }
    } finally {
      if (bakeAlive) {
        baking = false;
      }
    }
  }

  /** A failure's server sentence, plus `detail.problems` when it listed any. */
  function describeImportFailure(cause: unknown): { message: string; problems: string[] } {
    if (cause instanceof ApiError) {
      return { message: cause.message, problems: problemsFrom(cause.detail) };
    }
    return { message: 'the configuration file could not be validated', problems: [] };
  }

  function problemsFrom(detail: unknown): string[] {
    if (typeof detail !== 'object' || detail === null) {
      return [];
    }
    const problems = (detail as Record<string, unknown>)['problems'];
    if (!Array.isArray(problems)) {
      return [];
    }
    return problems.filter((problem): problem is string => typeof problem === 'string');
  }

  /** A byte count a person can read, without a locale-dependent formatter. */
  function formatFileSize(bytes: number): string {
    if (bytes < 1024) {
      return `${bytes} bytes`;
    }
    if (bytes < 1024 * 1024) {
      return `${(bytes / 1024).toFixed(1)} kB`;
    }
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  /** `1 entity` / `2 entities`: a count with its noun in the matching number. */
  function counted(count: number, one: string, many: string): string {
    return `${count} ${count === 1 ? one : many}`;
  }

  /**
   * The server's own counts, as one sentence that says exactly what they are:
   * every row the document names, including the ones the diff calls unchanged.
   */
  function describeReport(report: ImportReport): string {
    const written = [
      counted(report.settings_written, 'setting', 'settings'),
      counted(report.entities, 'entity', 'entities'),
      counted(report.sounds, 'sound', 'sounds'),
      counted(report.buttons, 'button', 'buttons')
    ].join(', ');
    if (report.dry_run) {
      return `Dry run accepted: the server would write ${written} — every row the document names, unchanged ones included. Nothing was written.`;
    }
    return `Applied: the server wrote ${written}.`;
  }

  /** Forget every transfer result: a different file has not been compared yet. */
  function resetTransfer(): void {
    dryReport = null;
    dryDiff = null;
    importError = null;
    applied = false;
    confirmOpen = false;
    documentError = null;
  }

  async function chooseFile(): Promise<void> {
    const file = fileInput?.files?.[0] ?? null;
    resetTransfer();
    document = null;
    documentName = '';
    documentSize = 0;
    if (file === null) {
      return;
    }
    reading = true;
    documentName = file.name;
    documentSize = file.size;
    try {
      const parsed: unknown = JSON.parse(await file.text());
      if (alive) {
        document = parsed;
      }
    } catch (cause) {
      if (alive) {
        documentError = `This file is not valid JSON: ${messageFor(cause, 'it could not be parsed')}`;
      }
    } finally {
      if (alive) {
        reading = false;
      }
    }
  }

  /**
   * Compare the selected file with the current configuration, then ask the server
   * to validate it without writing. Nothing is applied by a dry run.
   */
  async function runDryRun(): Promise<void> {
    if (document === null || dryRunning) {
      return;
    }
    dryRunning = true;
    importError = null;
    dryReport = null;
    dryDiff = null;
    applied = false;
    try {
      const current = await request<unknown>(exportConfigUrl(false));
      const report = await importConfig(document, true);
      if (!alive) {
        return;
      }
      dryReport = report;
      dryDiff = compareDocuments(current, document);
    } catch (cause) {
      if (alive) {
        importError = describeImportFailure(cause);
      }
    } finally {
      if (alive) {
        dryRunning = false;
      }
    }
  }

  /** Apply the reviewed document, then re-read the facts an import can change. */
  async function apply(): Promise<void> {
    if (document === null || dryReport === null || applying || applied) {
      return;
    }
    applying = true;
    importError = null;
    confirmOpen = false;
    try {
      const report = await importConfig(document, false);
      if (!alive) {
        return;
      }
      dryReport = report;
      applied = true;
      toastStore.push('success', 'Configuration applied.');
      await refreshFacts();
    } catch (cause) {
      if (alive) {
        importError = describeImportFailure(cause);
      }
    } finally {
      if (alive) {
        applying = false;
      }
    }
  }
</script>

<div class="panel">
  <header class="head">
    <h2>About</h2>
  </header>

  <Card title="This build">
    {#if aboutError !== null}
      <p class="error" role="alert">
        <Icon name="mdi:alert-circle-outline" size={20} /><span>{aboutError}</span>
      </p>
      <div class="controls">
        <Button onclick={() => void refreshFacts()}>Retry</Button>
      </div>
    {:else if about === null}
      <Spinner label="Reading the device facts" />
    {:else}
      <dl class="facts">
        <dt>Version</dt>
        <dd>{about.version}</dd>
        <dt>Schema version</dt>
        <dd>{about.schema_version}</dd>
        <dt>Uptime</dt>
        <dd>{formatUptime(about.uptime_s)}</dd>
        <dt>Data directory</dt>
        <dd class="path">{about.data_dir}</dd>
      </dl>

      {#if about.log_available}
        <a class="download" href={logExcerptUrl()} download>Download log excerpt</a>
      {:else}
        <p class="hint">
          File logging is off, so there is no log file to download. Start the service with
          <code>--log-file</code> (or <code>CHIMEDECK_LOG_FILE=1</code>) to write one under
          the data directory.
        </p>
      {/if}
    {/if}
  </Card>

  <!--
    Rendered only after the usage probe has answered: no endpoint means no bake feature
    on this build, so there is no control here that could not work.
  -->
  {#if bakeUsage !== null}
    <Card title="Pre-rendered speech">
      <p class="hint">
        A static phrase is rendered to a file once, so a press plays that file. A phrase with
        template syntax is rendered on every press instead, because its values change.
      </p>

      <div class="controls">
        <Button size="lg" loading={baking} onclick={() => void reRenderAll()}
          >Re-render all static phrases</Button
        >
      </div>

      {#if baking}
        <Spinner label="Re-rendering every static phrase" />
        <p class="hint">
          The device is working through every static phrase one by one; this can take a while,
          and the button stays busy until the last one is done.
        </p>
      {/if}

      {#if bakeError !== null}
        <p class="error" role="alert">
          <Icon name="mdi:alert-circle-outline" size={20} /><span>{bakeError}</span>
        </p>
      {/if}

      {#if bakeReport !== null}
        <div class="report" aria-live="polite">
          {#if bakeReportEmpty}
            <p class="counts">
              The server reported no button in any of the four categories, so there was nothing
              to bake, nothing already current, nothing rendered on every press and no failure.
            </p>
          {:else}
            {#each bakeSections as section (section.kind)}
              <section class="bake-section">
                <h4>{section.label}</h4>
                <p class="bake-count">{counted(section.entries.length, 'button', 'buttons')}</p>
                {#if section.entries.length === 0}
                  <p class="bake-none">None.</p>
                {:else}
                  <ul class="bake-entries">
                    {#each section.entries as entry}
                      <li>{entry}</li>
                    {/each}
                  </ul>
                {/if}
              </section>
            {/each}
          {/if}
        </div>
      {/if}

      <dl class="facts">
        <dt><code>data/tts/baked/</code></dt>
        <dd>
          {counted(bakeUsage.baked.files, 'file', 'files')} · {formatFileSize(bakeUsage.baked.bytes)}
        </dd>
        <dt><code>data/tts-cache/</code></dt>
        <dd>
          {counted(bakeUsage.cache.files, 'file', 'files')} · {formatFileSize(bakeUsage.cache.bytes)}
        </dd>
      </dl>
      <p class="footnote">
        These figures are measured on the device in those two directories, not counted in the
        browser.
      </p>
    </Card>
  {/if}

  <Card title="Configuration export">
    <Toggle
      label="Include secrets"
      description="Off by default: a redacted export leaves out your Home Assistant token and MQTT password entirely, rather than masking them."
      bind:checked={includeSecrets}
    />
    {#if includeSecrets}
      <p class="warning" role="note">
        <Icon name="mdi:alert-circle-outline" size={24} />
        <span>This export contains your Home Assistant token and MQTT password. Store the file somewhere safe.</span>
      </p>
    {/if}
    <a
      class="download primary"
      href={exportConfigUrl(includeSecrets)}
      download
      data-export-secrets={includeSecrets ? 'true' : 'false'}>Export configuration</a
    >
  </Card>

  <Card title="Configuration import">
    <div class="file-field">
      <label class="file-label" for="import-file">Configuration file</label>
      <input
        id="import-file"
        class="file-input"
        type="file"
        accept="application/json,.json"
        bind:this={fileInput}
        onchange={() => void chooseFile()}
      />
      <p class="hint">
        The browser's own file chooser is used: the component kit has no file control,
        and a drawn one could not open a file.
      </p>
    </div>

    {#if reading}
      <Spinner label="Reading the configuration file" />
    {:else if documentName !== ''}
      <p class="file-facts">{documentName} · {formatFileSize(documentSize)}</p>
    {/if}

    {#if documentError !== null}
      <p class="error" role="alert">
        <Icon name="mdi:alert-circle-outline" size={20} /><span>{documentError}</span>
      </p>
    {/if}

    <p class="hint">
      An import merges: every row the file names is written, and everything it does not name
      is left untouched. Nothing on this device is deleted by importing a file.
    </p>

    <div class="controls">
      <Button
        size="lg"
        loading={dryRunning}
        disabled={document === null || reading}
        onclick={() => void runDryRun()}>Dry run</Button
      >
    </div>

    {#if importError !== null}
      <div class="failure" role="alert">
        <p class="error">
          <Icon name="mdi:alert-circle-outline" size={20} /><span>{importError.message}</span>
        </p>
        {#if importError.problems.length > 0}
          <ul class="problems">
            {#each importError.problems as problem}
              <li>{problem}</li>
            {/each}
          </ul>
        {/if}
      </div>
    {/if}

    {#if dryReport !== null}
      <div class="report" aria-live="polite">
        <p class="counts">{describeReport(dryReport)}</p>
        {#if sectionRows.length > 0}
          <table class="diff">
            <caption>Dry-run report · the selected file compared with the current configuration</caption>
            <thead>
              <tr>
                <th scope="col">Section</th>
                <th scope="col">Added</th>
                <th scope="col">Changed</th>
                <th scope="col">Removed</th>
              </tr>
            </thead>
            <tbody>
              {#each sectionRows as row (row.label)}
                <tr>
                  <th scope="row">{row.label}</th>
                  <td>{row.diff.added}</td>
                  <td>{row.diff.changed}</td>
                  <td>{row.diff.removed} · {row.diff.note}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
        <p class="footnote">
          Added and changed are how this file differs from the stored configuration. A row the
          file names but does not change is rewritten too, so the written counts above can be
          larger. Removed is always 0 because an import merges and never deletes.
        </p>
      </div>

      <div class="controls">
        <Button
          variant="primary"
          size="lg"
          loading={applying}
          disabled={applied}
          onclick={() => (confirmOpen = true)}>Apply</Button
        >
      </div>
    {/if}

    {#if applied}
      <p class="applied" role="status">
        Configuration applied. The sound library was rescanned, so the imported sounds are
        indexed and can be played.
      </p>
    {/if}
  </Card>
</div>

<!--
  The confirmation exists only once there is a dry-run report to confirm: before
  that there is nothing to apply, so no inert dialog sits in the page.
-->
{#if dryReport !== null}
  <Sheet
    bind:open={confirmOpen}
    title="Apply reviewed configuration"
    description="Apply the changes in the dry-run report?"
  >
    <p>
      The file is merged into this device's configuration: rows it names are written and
      everything it does not name is left untouched.
    </p>
    <div class="controls">
      <Button size="lg" onclick={() => (confirmOpen = false)}>Cancel</Button>
      <Button variant="primary" size="lg" loading={applying} onclick={() => void apply()}>Apply</Button>
    </div>
  </Sheet>
{/if}

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

  h2 {
    margin: 0;
    font-size: var(--text-title);
    line-height: 1.2;
    font-weight: 700;
  }

  .head {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .facts {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: var(--gap-sm) var(--gap);
    margin: 0;
    min-width: 0;
    font-size: var(--text-body);
    line-height: 1.4;
  }

  .facts dt {
    color: var(--text-muted);
    font-weight: 600;
  }

  .facts dd {
    margin: 0;
    min-width: 0;
    overflow-wrap: anywhere;
    font-weight: 600;
  }

  .facts .path,
  .facts code {
    font-family: var(--font-num);
    font-weight: 500;
    word-break: break-word;
  }

  .bake-section {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .bake-section h4,
  .bake-count,
  .bake-none {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  .bake-section h4 {
    font-weight: 700;
  }

  .bake-count {
    color: var(--text-muted);
    font-weight: 600;
  }

  .bake-none {
    color: var(--text-muted);
  }

  .bake-entries {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    margin: 0;
    padding-inline-start: var(--gap);
    font-size: var(--text-body);
    line-height: 1.45;
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
    padding: var(--gap-sm) var(--gap);
    background-color: var(--surface-hover);
    border-inline-start: 4px solid var(--state-warn);
    border-radius: var(--radius-sm);
    color: var(--text);
  }

  .warning :global(svg),
  .error :global(svg) {
    flex-shrink: 0;
    margin-top: 0.15em;
  }

  .error {
    color: var(--state-alert);
  }

  .error > span {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .hint,
  .file-facts,
  .counts,
  .applied {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .hint {
    color: var(--text-muted);
  }

  .hint code {
    font-family: var(--font-num);
  }

  .counts {
    font-weight: 600;
  }

  .applied {
    color: var(--state-ok);
    font-weight: 600;
  }

  .file-field,
  .report,
  .failure {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .file-label {
    font-size: var(--text-label);
    line-height: 1.25;
    font-weight: 600;
  }

  /* The native control keeps its own look; only the target grows to 48px. */
  .file-input {
    min-height: var(--tap);
    padding: var(--gap-sm);
    background-color: var(--surface);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: var(--text-body);
  }

  /* A real download anchor styled as a button: the browser saves the file. */
  .download {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: var(--tap);
    padding: var(--gap-sm) var(--gap);
    background-color: var(--surface);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: var(--text-label);
    font-weight: 600;
    text-decoration: none;
    align-self: flex-start;
  }

  .download.primary {
    min-height: var(--tap-primary);
    background-color: var(--accent);
    border-color: var(--accent);
    color: var(--accent-contrast);
  }

  .download:hover {
    background-color: var(--surface-hover);
  }

  .download.primary:hover {
    background-color: var(--accent);
    filter: brightness(1.08);
  }

  .diff {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--text-body);
    font-variant-numeric: tabular-nums;
  }

  .diff caption {
    padding-bottom: var(--gap-sm);
    color: var(--text-muted);
    font-size: var(--text-body);
    text-align: start;
    overflow-wrap: anywhere;
  }

  .diff th,
  .diff td {
    padding: var(--gap-sm);
    border-bottom: var(--border-width) solid var(--border);
    text-align: start;
    vertical-align: top;
    overflow-wrap: anywhere;
  }

  .diff thead th {
    color: var(--text-muted);
    font-weight: 600;
  }

  .diff tbody th {
    font-weight: 600;
  }

  /* Not a tooltip: the reconciliation between the two counts is always on screen. */
  .footnote {
    margin: 0;
    font-size: 16px;
    line-height: 1.45;
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }

  .problems {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    margin: 0;
    padding-inline-start: var(--gap);
    color: var(--state-alert);
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
  }

  @media (min-width: 768px) {
    .facts {
      grid-template-columns: minmax(9rem, max-content) minmax(0, 1fr);
    }
  }
</style>
