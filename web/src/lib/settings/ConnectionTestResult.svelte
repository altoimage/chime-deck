<!--
  The one way a connection-test result is rendered (HSD-vl7t): the badge word,
  then the facts sentence, so the Connection panel's Home Assistant and MQTT
  probes and the first-run wizard say the same thing in the same shape.

  The container is always rendered as a polite live region, even with nothing to
  say: a screen reader announces the verdict when it arrives. It styles itself
  out of the layout while empty, so an untested connection leaves no gap.

  Body text is --text-body and never hover-only — a result must be readable from
  across a wall panel and in greyscale, and the word on the badge is what carries
  the state, not colour.
-->
<script lang="ts">
  import Badge from '../ui/Badge.svelte';
  import type { ConnectionTestView } from './connection-form.js';

  interface Props {
    /** The result to render, or null before anything has been tested. */
    view: ConnectionTestView | null;
  }

  const { view }: Props = $props();
</script>

<div class="result" aria-live="polite">
  {#if view !== null}
    {#if view.ok}
      <Badge kind="ok" label="Connected" icon="mdi:check-circle-outline" />
      <p class="facts">{view.summary}</p>
    {:else}
      <Badge kind="alert" label="Not connected" icon="mdi:alert-circle-outline" />
      {#if view.error !== null}
        <p class="facts">{view.error}</p>
      {/if}
    {/if}
  {/if}
</div>

<style>
  .result {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  /* The live region keeps its height only when it has something to say. */
  .result:empty {
    display: none;
  }

  .facts {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
  }
</style>
