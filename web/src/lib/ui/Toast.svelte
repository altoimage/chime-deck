<script lang="ts">
  import Icon from './Icon.svelte';
  import IconButton from './IconButton.svelte';
  import type { ToastKind } from './toast.svelte.js';

  interface Props {
    kind: ToastKind;
    message: string;
    ondismiss?: () => void;
  }

  const { kind, message, ondismiss }: Props = $props();

  const KIND_ICONS: Record<ToastKind, string> = {
    info: 'mdi:information-outline',
    success: 'mdi:check-circle-outline',
    error: 'mdi:alert-circle-outline'
  };
</script>

<!--
  A single toast. Never hover-dependent: the dismiss button is always
  visible and auto-dismiss is the host/store's job, not CSS.
-->
<div
  class="toast"
  class:info={kind === 'info'}
  class:success={kind === 'success'}
  class:error={kind === 'error'}
>
  <span class="kind-icon">
    <Icon name={KIND_ICONS[kind]} size={28} />
  </span>
  <span class="kind-label">{kind === 'info' ? 'Info' : kind === 'success' ? 'Success' : 'Error'}</span>
  <span class="message">{message}</span>
  <IconButton icon="mdi:close" label="Dismiss notification" onclick={() => ondismiss?.()} />
</div>

<style>
  .toast {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    width: min(480px, 100%);
    min-height: var(--tap-primary);
    padding: var(--gap-sm) var(--gap-sm) var(--gap-sm) var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-inline-start: 4px solid var(--accent);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-sheet);
    font-size: var(--text-body);
    line-height: 1.35;
    font-weight: 600;
    pointer-events: auto;
  }

  .success {
    border-inline-start-color: var(--state-ok);
  }

  .error {
    border-inline-start-color: var(--state-alert);
  }

  /* Icon + bar + words: state is never carried by colour alone. */
  .kind-icon {
    display: inline-flex;
    flex-shrink: 0;
    color: var(--accent);
  }

  .success .kind-icon {
    color: var(--state-ok);
  }

  .error .kind-icon {
    color: var(--state-alert);
  }

  .kind-label {
    flex-shrink: 0;
    font-weight: 700;
  }

  .info .kind-label {
    color: var(--accent);
  }

  .success .kind-label {
    color: var(--state-ok);
  }

  .error .kind-label {
    color: var(--state-alert);
  }

  .message {
    flex: 1;
    min-width: 0;
  }
</style>
