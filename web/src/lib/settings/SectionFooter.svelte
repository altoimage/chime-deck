<script lang="ts">
  import Button from '../ui/Button.svelte';
  import Icon from '../ui/Icon.svelte';

  interface Props {
    /** The primary action's label. */
    label?: string;
    /**
     * The section landmark's accessible name; distinguish multiple save footers
     * on one page while keeping the shared default stable for existing panels.
     */
    describe?: string;
    /** Whether anything changed since the last save. */
    dirty: boolean;
    /** Whether a client-side rule currently blocks the save. */
    invalid?: boolean;
    /** Whether a save is in flight (the button shows a Spinner). */
    saving: boolean;
    /** The server's sentence, verbatim, when the last save failed. */
    error?: string | null;
    onsave: () => void;
  }

  const {
    label = 'Save',
    describe = 'Save this section',
    dirty,
    invalid = false,
    saving,
    error = null,
    onsave
  }: Props = $props();
</script>

<!--
  The shared per-section save bar (design 05 §2.2: every section has its own
  save). Later panels reuse this instead of growing their own buttons, so the
  busy/disabled/error wording stays identical everywhere.

  The inline error is the durable record — a toast can be missed or dismissed.
-->
<section class="footer" aria-label={describe}>
  {#if error}
    <p class="error" role="alert">
      <Icon name="mdi:alert-circle-outline" size={24} />
      <span>{error}</span>
    </p>
  {/if}

  <Button variant="primary" loading={saving} disabled={!dirty || invalid || saving} onclick={onsave}>
    {label}
  </Button>
</section>

<style>
  .footer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap);
    padding-top: var(--gap);
    border-top: var(--border-width) solid var(--border);
  }

  .error {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    flex: 1 1 100%;
    margin: 0;
    color: var(--state-alert);
    font-size: var(--text-body);
    line-height: 1.4;
  }
</style>
