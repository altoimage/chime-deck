<script lang="ts">
  import Button from './Button.svelte';
  import Sheet from './Sheet.svelte';

  interface Props {
    open?: boolean;
    title: string;
    message: string;
    confirmLabel?: string;
    cancelLabel?: string;
    destructive?: boolean;
    onconfirm?: () => void;
    oncancel?: () => void;
  }

  let {
    open = $bindable(false),
    title,
    message,
    confirmLabel = 'Confirm',
    cancelLabel = 'Cancel',
    destructive = false,
    onconfirm,
    oncancel
  }: Props = $props();

  /*
   * D26 forbids centred modal dialogs: Dialog keeps the confirm/cancel
   * semantics but is ALWAYS rendered as a Sheet (full-screen ≤1024px,
   * bottom sheet on larger screens).
   *
   * oncancel fires for every close that was not a confirm — the cancel
   * button, Escape, a backdrop tap, or the parent clearing `open`. The
   * confirmed flag stops Sheet's close event from double-reporting a
   * confirm as a cancel.
   */
  let confirmed = false;

  function confirm(): void {
    confirmed = true;
    open = false;
    onconfirm?.();
  }

  function cancel(): void {
    // The Sheet's close event calls oncancel via handleSheetClose.
    open = false;
  }

  function handleSheetClose(): void {
    if (confirmed) {
      confirmed = false;
      return;
    }
    oncancel?.();
  }
</script>

<Sheet bind:open {title} onclose={handleSheetClose}>
  <p class="message">{message}</p>
  {#snippet footer()}
    <div class="actions">
      <Button variant="secondary" size="lg" fullWidth onclick={cancel}>{cancelLabel}</Button>
      <Button variant={destructive ? 'danger' : 'primary'} size="lg" fullWidth onclick={confirm}>
        {confirmLabel}
      </Button>
    </div>
  {/snippet}
</Sheet>

<style>
  .message {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.5;
    font-weight: 500;
  }

  /* One action per row, ≥48px each; cancel first in DOM order. */
  .actions {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
  }
</style>
