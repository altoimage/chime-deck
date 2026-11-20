<script lang="ts">
  import StateToneTable from './StateToneTable.svelte';

  interface Props {
    currentState?: string | null;
    currentWord?: string | null;
    currentTone?: string | null;
  }

  let {
    currentState = null,
    currentWord = null,
    currentTone = null
  }: Props = $props();

  let stateTones = $state<Record<string, string>>({});
  let colourByState = $state(true);
</script>

<StateToneTable
  bind:stateTones
  bind:colourByState
  {currentState}
  {currentWord}
  {currentTone}
/>
<output data-testid="bound-state-tone-draft">
  {JSON.stringify({ stateTones, colourByState })}
</output>
