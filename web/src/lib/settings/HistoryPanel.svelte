<!--
  The History section (storyboards S15 F1–F8): the stored press log, newest
  first, one page at a time.

  Rows are **not** a dense desktop table. The detail is arbitrary-length server
  prose, and seven 16 px columns inside the settings content area would force
  truncation or a horizontal scroll, so every row is a self-describing card that
  carries its own labels (`source:`, `duration:`, `latency:`, `synthesis:`) at
  every width. A failed row prints its reason inline as a `role="note"` block —
  never behind a tap, because "why did this fail" is the one thing a reader opens
  the log for.

  There is no `SectionFooter`: nothing here is a settings draft. Every action is a
  request — the outcome filter re-fetches, "Load more" raises `limit` — and the
  page size can never exceed the retention cap the server reports.
-->
<script lang="ts">
  import { untrack } from 'svelte';
  import { ApiError, getHistory, type HistoryRow } from '../api.js';
  import {
    OUTCOME_OPTIONS,
    PAGE,
    failedReason,
    formatDurationMs,
    formatHistoryTime,
    formatLatencyMs,
    formatPressSynthesisMs,
    historyOutcomeLabel,
    loadMoreLimit,
    outcomeKind,
    retentionNote
  } from '../history-view.js';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import Icon from '../ui/Icon.svelte';
  import Select from '../ui/Select.svelte';
  import Spinner from '../ui/Spinner.svelte';

  /** `null` means "no answer yet" — the spinner, never an empty log. */
  let rows = $state<HistoryRow[] | null>(null);
  let retention = $state<number | null>(null);
  /** How many rows this view asks for; raised by "Load more", capped by retention. */
  let limit = $state(PAGE);
  /** The selected outcome filter; `''` asks for every outcome. */
  let outcome = $state('');
  let loadError = $state<string | null>(null);
  let loadingMore = $state(false);

  /** Guards against an older answer overwriting the newest one. */
  let loadSeq = 0;
  let alive = false;
  /** The outcome the rows on screen were fetched with, so the filter fetches once. */
  let appliedOutcome = '';

  const list = $derived(rows ?? []);
  /** The next page size, or `null` when the retention cap is already reached. */
  const nextLimit = $derived(retention === null ? null : loadMoreLimit(limit, retention));

  function messageFor(cause: unknown, fallback: string): string {
    return cause instanceof ApiError ? cause.message : fallback;
  }

  /**
   * Fetch the current page. A fresh load empties the list first (the spinner is
   * the honest answer for "no rows yet"); "Load more" keeps the rows on screen
   * until the larger superset arrives, so a failed request never blanks the log.
   */
  async function load(keepRows = false): Promise<void> {
    const sequence = ++loadSeq;
    if (!keepRows) {
      rows = null;
    }
    loadError = null;
    try {
      const answer = await getHistory({ limit, outcome });
      if (!alive || sequence !== loadSeq) {
        return;
      }
      rows = answer.rows;
      retention = answer.retention;
    } catch (cause) {
      if (!alive || sequence !== loadSeq) {
        return;
      }
      loadError = messageFor(cause, 'the press history could not be loaded');
    }
  }

  $effect(() => {
    alive = true;
    void untrack(() => load());
    return () => {
      alive = false;
      loadSeq += 1;
    };
  });

  // A new filter is a new query: back to the first page, then fetch. The rows on
  // screen are always the answer to `appliedOutcome`, never a mix of two filters.
  $effect(() => {
    const selected = outcome;
    if (selected === appliedOutcome) {
      return;
    }
    appliedOutcome = selected;
    untrack(() => {
      limit = PAGE;
      void load();
    });
  });

  async function loadMore(): Promise<void> {
    if (loadingMore || retention === null) {
      return;
    }
    const next = loadMoreLimit(limit, retention);
    if (next === null) {
      return;
    }
    loadingMore = true;
    limit = next;
    await load(true);
    if (alive) {
      loadingMore = false;
    }
  }
</script>

<div class="panel">
  <header class="head">
    <h2>History</h2>
    {#if retention !== null}
      <p class="retention">Newest first · {retentionNote(retention)}</p>
    {/if}
  </header>

  <Select label="Outcome filter" options={OUTCOME_OPTIONS} bind:value={outcome} />

  {#if loadError !== null}
    <p class="error" role="alert">
      <Icon name="mdi:alert-circle-outline" size={20} /><span>{loadError}</span>
    </p>
    <div class="controls">
      <Button onclick={() => void load()}>Retry</Button>
    </div>
  {:else if rows === null}
    <Spinner label="Loading press history" />
  {:else if list.length === 0}
    <EmptyState
      title="No presses recorded"
      description={retention === null ? undefined : retentionNote(retention)}
    />
  {:else}
    <section class="log" aria-label="Press log">
      <ul class="records">
        {#each list as row (row.id)}
          <li class="record" data-outcome={row.outcome} data-row-id={row.id}>
            <p class="headline">
              <span class="time">{formatHistoryTime(row.pressed_at)}</span>
              <span class="sep" aria-hidden="true">·</span>
              <span class="button-name">{row.button_name}</span>
              <Badge kind={outcomeKind(row.outcome)} label={historyOutcomeLabel(row.outcome)} />
            </p>

            {#if row.outcome === 'failed'}
              <div class="reason" role="note" data-failed-reason>
                <span class="label">Reason:</span>
                <span class="reason-text">
                  {failedReason(row) ?? 'the server recorded no reason for this failure.'}
                </span>
              </div>
            {:else if row.detail !== null && row.detail.trim() !== ''}
              <p class="detail"><span class="label">detail:</span> <span>{row.detail}</span></p>
            {/if}

            <p class="facts">
              <span class="fact"><span class="label">source:</span> <span>{row.source}</span></span>
              <span class="fact">
                <span class="label">duration:</span>
                <span>{formatDurationMs(row.duration_ms)}</span>
              </span>
              <span class="fact">
                <span class="label">latency:</span>
                <span>{formatLatencyMs(row.latency_ms)}</span>
              </span>
              <span class="fact">
                <span class="label">synthesis:</span>
                <span>{formatPressSynthesisMs(row.synthesis_ms)}</span>
              </span>
            </p>
          </li>
        {/each}
      </ul>
    </section>

    {#if nextLimit !== null}
      <div class="controls">
        <Button size="lg" loading={loadingMore} onclick={() => void loadMore()}>Load more</Button>
      </div>
    {:else if retention !== null}
      <p class="cap">
        The retention limit of {retention} rows is reached, so there are no older presses to
        load; the newest {retention} rows are kept and older presses are discarded.
      </p>
    {/if}
  {/if}
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

  .head {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    min-width: 0;
  }

  h2 {
    margin: 0;
    font-size: var(--text-title);
    line-height: 1.2;
    font-weight: 700;
  }

  .retention,
  .cap {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .log,
  .records {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    margin: 0;
    padding: 0;
    min-width: 0;
    list-style: none;
  }

  .record {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    min-height: var(--tap);
    padding: var(--gap);
    background-color: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
    font-size: var(--text-body);
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  .headline {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    margin: 0;
    min-width: 0;
  }

  .time {
    font-family: var(--font-num);
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }

  .button-name {
    font-weight: 600;
  }

  .sep {
    color: var(--text-muted);
  }

  .facts {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm) var(--gap);
    margin: 0;
    min-width: 0;
  }

  .fact,
  .detail {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .detail {
    margin: 0;
  }

  .label {
    color: var(--text-muted);
    font-weight: 600;
  }

  /*
   * A failed row's reason is a distinct block, never colour alone: the left rule
   * plus the bold "Reason:" label survive a monochrome screen.
   */
  .reason {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    padding: var(--gap-sm) var(--gap);
    background-color: var(--surface-hover);
    border-inline-start: 4px solid var(--state-alert);
    border-radius: var(--radius-sm);
    min-width: 0;
  }

  .reason-text {
    overflow-wrap: anywhere;
  }

  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
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
</style>
