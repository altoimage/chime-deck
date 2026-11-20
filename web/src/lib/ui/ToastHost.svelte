<script lang="ts">
  import Toast from './Toast.svelte';
  import { toastStore } from './toast.svelte.js';

  /*
   * Reads the toast store directly. Timers live in the store keyed by toast
   * id and are cleared on dismiss; on host unmount everything left is
   * cleared so no timer can leak past the host.
   */
  $effect(() => {
    return () => {
      toastStore.clear();
    };
  });
</script>

<div class="toast-host" aria-live="polite" aria-atomic="false">
  {#each toastStore.items as toast (toast.id)}
    <Toast kind={toast.kind} message={toast.message} ondismiss={() => toastStore.dismiss(toast.id)} />
  {/each}
</div>

<style>
  .toast-host {
    position: fixed;
    inset-inline: 0;
    bottom: calc(var(--gap) + env(safe-area-inset-bottom));
    z-index: var(--z-toast);
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--gap-sm);
    padding-inline-start: calc(var(--gap) + env(safe-area-inset-left));
    padding-inline-end: calc(var(--gap) + env(safe-area-inset-right));
    /* The strip never blocks taps on the UI beneath it; the toasts
       themselves re-enable pointer events. */
    pointer-events: none;
  }
</style>
