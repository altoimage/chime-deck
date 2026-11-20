<script lang="ts">
  import { ApiError, clearAdminPassword, setAdminPassword } from '../api.js';
  import { auth } from '../auth.svelte.js';
  import Button from '../ui/Button.svelte';
  import Card from '../ui/Card.svelte';
  import Input from '../ui/Input.svelte';
  import SectionFooter from './SectionFooter.svelte';

  let current = $state('');
  let next = $state('');
  let confirm = $state('');
  let busyAction = $state<'set' | 'clear' | null>(null);
  let error = $state<string | null>(null);
  let success = $state<string | null>(null);

  const passwordSet = $derived(auth.status?.passwordSet ?? false);
  const statusKnown = $derived(auth.status !== null);
  const busy = $derived(busyAction !== null);
  const currentError = $derived(
    passwordSet && current.length === 0 ? 'Enter the current password.' : undefined
  );
  const newError = $derived(
    next.length === 0
      ? 'Enter a new password.'
      : next.length < 8
        ? 'Use at least 8 characters.'
        : undefined
  );
  const confirmError = $derived(
    confirm.length === 0
      ? 'Confirm the new password.'
      : confirm !== next
        ? 'The passwords do not match.'
        : undefined
  );
  const setInvalid = $derived(
    !statusKnown || busy || currentError !== undefined || newError !== undefined ||
      confirm.length === 0 || confirmError !== undefined
  );

  function clearFeedback(): void {
    error = null;
    success = null;
  }

  function failureMessage(cause: unknown): string {
    return cause instanceof ApiError ? cause.message : 'The password could not be changed. Please try again.';
  }

  async function setPassword(): Promise<void> {
    if (setInvalid) {
      return;
    }
    busyAction = 'set';
    clearFeedback();
    try {
      const result = await setAdminPassword({ current: passwordSet ? current : null, new: next });
      auth.passwordSetChanged(result.password_set);
      current = '';
      next = '';
      confirm = '';
      success = 'Configuration password set.';
    } catch (cause) {
      error = failureMessage(cause);
    } finally {
      busyAction = null;
    }
  }

  async function clearPassword(): Promise<void> {
    if (busy || !passwordSet || current.length === 0) {
      if (passwordSet && current.length === 0) {
        error = 'Enter the current password.';
      }
      return;
    }
    busyAction = 'clear';
    clearFeedback();
    try {
      const result = await clearAdminPassword(current);
      auth.passwordSetChanged(result.password_set);
      current = '';
      next = '';
      confirm = '';
      success = 'Configuration password cleared.';
    } catch (cause) {
      error = failureMessage(cause);
    } finally {
      busyAction = null;
    }
  }
</script>

<div class="panel">
  <Card title="Configuration interface password">
    <p class="explanation">
      A password protects configuration settings on the admin interface, including each tile's
      current state. If the password is lost, recovery requires physical access to the device.
    </p>

    <p class="state" aria-live="polite">
      {#if !statusKnown}
        Password status is not available.
      {:else if passwordSet}
        <span class="state-dot is-set" aria-hidden="true"></span>
        A password is set
      {:else}
        <span class="state-dot" aria-hidden="true"></span>
        No password is set
      {/if}
    </p>

    <div class="fields" oninput={clearFeedback}>
      {#if passwordSet}
        <Input
          label="Current password"
          bind:value={current}
          type="password"
          secret
          autocomplete="current-password"
          required
          error={currentError}
        />
      {/if}

      <Input
        label="New password"
        bind:value={next}
        type="password"
        secret
        autocomplete="new-password"
        required
        hint="Use at least 8 characters."
        error={newError}
      />
      <Input
        label="Confirm new password"
        bind:value={confirm}
        type="password"
        secret
        autocomplete="new-password"
        required
        error={confirmError}
      />
    </div>

    {#if success}
      <p class="message success" role="status">{success}</p>
    {/if}

    <div class="actions">
      <Button
        variant="secondary"
        loading={busyAction === 'clear'}
        disabled={busy || !statusKnown || !passwordSet}
        onclick={() => void clearPassword()}
      >
        Clear password
      </Button>
    </div>
  </Card>

  <SectionFooter
    label="Set password"
    dirty={true}
    invalid={setInvalid}
    saving={busyAction === 'set'}
    error={error}
    onsave={() => void setPassword()}
  />
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
  }

  .fields {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
  }

  .explanation {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--text-body);
    line-height: 1.5;
  }

  .state {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    margin: 0;
    font-size: var(--text-label);
    font-weight: 700;
  }

  .state-dot {
    width: 0.7rem;
    height: 0.7rem;
    border-radius: 50%;
    background: var(--text-muted);
  }

  .state-dot.is-set {
    background: var(--state-ok);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--state-ok) 18%, transparent);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
    padding-top: var(--gap-sm);
  }

  .message {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
  }

  .success {
    color: var(--state-ok);
  }
</style>
