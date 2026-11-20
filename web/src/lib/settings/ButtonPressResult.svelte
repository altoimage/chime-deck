<!--
  The result of one Test press (storyboards S11 F10 in flight, F11 finished): a
  shared block so the list row and the editor's Test button describe a press in
  exactly the same words.

  Three honesty rules live here, and each one exists because of what
  `POST /api/buttons/{id}/press` does *not* return:

  1. **Latency is measured, not reported.** `PressResponse` carries no round-trip
     timing field at all (its `synthesis_ms` is the engine's render time, not the
     request's), so `roundTripMs` is the caller's own `performance.now()` around
     the request. The label says "round-trip latency" and the closing sentence says
     what it is not — playback time — because a reader who saw only "42 ms"
     beside a chime would reasonably think the sound was that short.

  2. **`detail` is the only place the press path names an engine.** `press.py`
     appends `engine <name>` to its notes for a TTS press, so that token is the
     one fact about *this* press. Everything else (`report.engineOrFileProvenance
     === 'configured'`) came from the button's saved row, which is only what the
     button *would* use, so it is printed with the words "(from the saved
     configuration)" and can never be mistaken for a press fact.

  3. **An unknown value says so.** `press-report.ts` already decided every
     string below (an unnamed sound is "the sound this button plays", an unnamed
     engine is "an engine this press did not name"), so this file prints them
     unchanged instead of inventing a better-looking guess.

  The region is `aria-live="polite"` and `aria-busy` while the request is in
  flight: a Test press changes it without moving focus.
-->
<script lang="ts">
  import { formatLatency, type PressReport } from '../press-report.js';
  import Badge from '../ui/Badge.svelte';
  import Icon from '../ui/Icon.svelte';
  import Spinner from '../ui/Spinner.svelte';

  interface Props {
    /** True while the press request is in flight. */
    pressing: boolean;
    /** The ApiError message when the press could not be made at all. */
    error: string | null;
    /** The described result, or null before the first press. */
    report: PressReport | null;
  }

  const { pressing, error, report }: Props = $props();
</script>

<section class="result" aria-live="polite" aria-busy={pressing}>
  {#if pressing}
    <Spinner label="Testing button" />
  {:else if error}
    <p class="error" role="alert"><Icon name="mdi:alert-circle-outline" size={20} /><span>{error}</span></p>
  {/if}

  {#if report}
    <p class="headline">
      <Badge
        kind={report.kind}
        label={report.outcomeLabel}
        icon={report.kind === 'ok'
          ? 'mdi:check'
          : report.kind === 'warn'
            ? 'mdi:alert-circle-outline'
            : report.kind === 'alert'
              ? 'mdi:close'
              : 'mdi:help-circle-outline'}
      />
      <!--
        Whole milliseconds in the sentence (a raw `performance.now()` difference
        is a float with a meaningless fraction), and the unrounded measurement in
        an attribute so a test can still read the number the press actually took.
      -->
      <span class="latency" data-round-trip={report.roundTripMs}>
        Round-trip latency: {formatLatency(report.roundTripMs)}
      </span>
    </p>

    <dl class="facts">
      <div class="fact">
        <dt>{report.engineOrFileLabel}</dt>
        <dd>
          {report.engineOrFileValue}{#if report.engineOrFileProvenance === 'configured'} (from the saved configuration){/if}
        </dd>
      </div>
      {#if report.detail !== null}
        <div class="fact">
          <dt>Server detail</dt>
          <dd>{report.detail}</dd>
        </div>
      {/if}
    </dl>

    <p class="note">Round-trip latency is how long the press request took; it is not playback time.</p>
  {/if}
</section>

<style>
  .result {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    padding: var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius-sm);
  }

  .headline {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    margin: 0;
  }

  /* Measured numbers line up; the words stay body size (≥16px). */
  .latency {
    font-family: var(--font-num);
    font-size: var(--text-body);
    line-height: 1.35;
    color: var(--text-muted);
  }

  .facts {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    margin: 0;
  }

  .fact {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .fact dt {
    font-size: var(--text-label);
    line-height: 1.3;
    font-weight: 600;
    color: var(--text-muted);
  }

  .fact dd {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.4;
    overflow-wrap: anywhere;
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

  .note {
    margin: 0;
    font-size: var(--text-label);
    line-height: 1.4;
    color: var(--text-muted);
  }
</style>
