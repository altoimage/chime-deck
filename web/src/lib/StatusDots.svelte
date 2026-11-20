<!--
  Header health cluster: explicit words plus marks that remain distinct without
  colour. The status button opens the touch-reachable health sheet; it replaces
  a hover-only tooltip under D26, so every explanation is available by tap.
-->
<script lang="ts">
  import { liveState } from './stores.svelte.js';
  import {
    componentViews,
    connectionPhase,
    connectionView,
    summarySentence,
    type ConnectionInput
  } from './health-view.js';
  import Sheet from './ui/Sheet.svelte';

  let open = $state(false);

  const componentShortNames = { ha: 'HA', mqtt: 'MQTT', audio: 'Audio' } as const;

  const components = $derived(componentViews(liveState.health));
  const connectionInput = $derived.by((): ConnectionInput => ({
    status: liveState.connection.status,
    stale: liveState.stale,
    attempts: liveState.connection.attempts,
    hasSnapshot: liveState.hasSnapshot
  }));
  const connection = $derived(connectionView(connectionInput));
  const connectionState = $derived(connectionPhase(connectionInput));
  const summary = $derived(summarySentence(components));
  const accessibleLabel = $derived(
    `Connection status: ${summary}. ${connection.word}. Open details.`
  );
  const detailRows = $derived([
    ...components,
    { key: 'connection', label: 'Connection', ...connection }
  ]);
</script>

<button
  type="button"
  class="status"
  aria-haspopup="dialog"
  aria-label={accessibleLabel}
  onclick={() => (open = true)}
>
  <span class="components" aria-hidden="true">
    {#each components as component (component.key)}
      <span class="component">
        <span
          class="dot {component.kind} mark-{component.mark}"
          data-health-key={component.key}
          data-health-kind={component.kind}
        ></span>
        <span class="name">{componentShortNames[component.key]}</span>
        <span class="word">{component.word}</span>
      </span>
    {/each}
  </span>

  <span
    class="connection {connection.kind}"
    data-connection-state={connectionState}
    role="status"
    aria-live="polite"
  >
    <span
      class="dot {connection.kind} mark-{connection.mark}"
      data-health-key="connection"
      data-health-kind={connection.kind}
      aria-hidden="true"
    ></span>
    <span class="connection-text">{connection.word}</span>
    <span class="sr-only">{summary}</span>
  </span>

  {#if liveState.health.detail}
    <span class="detail">{liveState.health.detail}</span>
  {/if}
</button>

<!--
  D26: this sheet is the touch-reachable replacement for a hover tooltip. It
  makes every component and connection explanation available by tap or keyboard.
-->
<Sheet bind:open title="Connection status">
  <div class="health-rows">
    {#each detailRows as row (row.key)}
      <section class="health-row" data-row-key={row.key}>
        <div class="row-heading">
          <span
            class="dot {row.kind} mark-{row.mark}"
            data-health-key={row.key}
            data-health-kind={row.kind}
            aria-hidden="true"
          ></span>
          <h3>{row.label}</h3>
          <span class="row-word">{row.word}</span>
        </div>
        <p class="explanation">{row.explanation}</p>
      </section>
    {/each}

    {#if liveState.health.detail}
      <section class="reported-reason">
        <h3>Reported reason</h3>
        <p>{liveState.health.detail}</p>
      </section>
    {/if}
  </div>
</Sheet>

<style>
  .status {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap);
    min-height: var(--tap);
    padding: 0;
    border: 0;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--text);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }

  .components {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap);
  }

  .component {
    display: inline-flex;
    align-items: center;
    gap: var(--gap-sm);
    min-height: var(--tap);
    padding: 0 var(--gap);
    border: var(--border-width) solid var(--control-border);
    border-radius: 999px;
    font-size: var(--text-label);
    line-height: 1.25;
  }

  .name {
    color: var(--text-muted);
  }

  .word,
  .row-word {
    font-weight: 600;
  }

  .connection {
    display: inline-flex;
    align-items: center;
    gap: var(--gap-sm);
    min-height: var(--tap);
    padding: 0 var(--gap);
    border: var(--border-width) solid var(--control-border);
    border-radius: 999px;
    font-size: var(--text-body);
    font-weight: 600;
    line-height: 1.25;
  }

  .detail {
    flex-basis: 100%;
    margin: 0;
    font-size: var(--text-label);
    color: var(--text-muted);
  }

  .dot {
    position: relative;
    display: inline-grid;
    place-items: center;
    box-sizing: border-box;
    width: 12px;
    height: 12px;
    flex: none;
    border-radius: 50%;
  }

  .ok {
    color: var(--state-ok);
  }

  .warn {
    color: var(--state-warn);
  }

  .alert {
    color: var(--state-alert);
  }

  .off {
    color: var(--state-off);
  }

  .dot.ok,
  .dot.warn,
  .dot.alert {
    background: currentColor;
  }

  .dot.mark-ring {
    border: 2px solid currentColor;
    background: transparent;
  }

  .mark-cross::after {
    content: '×';
    color: var(--surface);
    font-size: 12px;
    font-weight: 800;
    line-height: 1;
  }

  .mark-hollow {
    border: 2px solid currentColor;
    background: transparent;
  }

  .health-rows {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
  }

  .health-row,
  .reported-reason {
    padding: var(--gap);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius-sm);
  }

  .row-heading {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
  }

  .row-heading h3,
  .reported-reason h3 {
    margin: 0;
    font-size: var(--text-body);
  }

  .row-word {
    margin-inline-start: auto;
  }

  .explanation,
  .reported-reason p {
    margin: var(--gap-sm) 0 0;
    color: var(--text-muted);
    font-size: var(--text-body);
    line-height: 1.4;
  }
</style>
