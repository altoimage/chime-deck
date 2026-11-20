<script lang="ts">
  import Icon from './Icon.svelte';

  interface Props {
    kind?: 'neutral' | 'ok' | 'warn' | 'alert' | 'accent';
    /** The word(s) carrying the state — never colour alone. */
    label: string;
    icon?: string;
  }

  const { kind = 'neutral', label, icon }: Props = $props();
</script>

<span
  class="badge"
  class:ok={kind === 'ok'}
  class:warn={kind === 'warn'}
  class:alert={kind === 'alert'}
  class:accent={kind === 'accent'}
>
  {#if icon}
    <Icon name={icon} size={24} />
  {/if}
  <span>{label}</span>
</span>

<style>
  .badge {
    display: inline-flex;
    align-items: center;
    gap: var(--gap-sm);
    padding: var(--gap-sm) var(--gap);
    border-radius: 999px;
    background: var(--surface-hover);
    border: var(--border-width) solid var(--control-border);
    color: var(--text);
    font-size: var(--text-body);
    font-weight: 600;
    line-height: 1.25;
  }

  .ok {
    color: var(--state-ok);
    border-color: var(--state-ok);
  }

  .warn {
    color: var(--state-warn);
    border-color: var(--state-warn);
  }

  .alert {
    color: var(--state-alert);
    border-color: var(--state-alert);
  }

  .accent {
    color: var(--accent);
    border-color: var(--accent);
  }
</style>
