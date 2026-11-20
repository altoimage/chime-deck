<script lang="ts">
  import { onMount } from 'svelte';
  import { auth } from './auth.svelte.js';

  onMount(() => {
    void auth.loadStatus();
  });

  const isAdminPassword = $derived(
    auth.status?.listener === 'admin' && auth.status.passwordSet
  );

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const form = event.currentTarget;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }

    const submittedValue = new FormData(form).get('credential');
    if (typeof submittedValue !== 'string') {
      return;
    }
    await auth.submit(submittedValue);
  }
</script>

{#if auth.required}
  <main class="auth-gate" aria-labelledby="auth-title">
    <section class="card">
      <div class="identity">
        <span class="brand-mark" aria-hidden="true"></span>
        <p class="eyebrow">HA SUPER DOORBELL <span aria-hidden="true">/</span> PRIVATE ACCESS</p>
      </div>

      <h1 id="auth-title">ChimeDeck</h1>
      <p class="explanation">
        {isAdminPassword
          ? 'This configuration interface is protected by a password.'
          : 'This doorbell is protected by a shared token.'}
      </p>

      <form class="login-form" onsubmit={submit}>
        <label for="auth-token">{isAdminPassword ? 'Configuration password' : 'Shared token'}</label>
        <!-- svelte-ignore a11y_autofocus -->
        <input
          id="auth-token"
          name="credential"
          type="password"
          placeholder={isAdminPassword ? 'Configuration password' : 'Shared token'}
          autocomplete="current-password"
          autofocus
          spellcheck="false"
          autocapitalize="off"
          inputmode="text"
          oninput={() => auth.dismissError()}
          aria-describedby={auth.error ? 'auth-description auth-error' : 'auth-description'}
        />
        <p id="auth-description" class="hint">
          {isAdminPassword
            ? 'Enter the configuration password to continue.'
            : 'Enter the token provided by your administrator.'}
        </p>

        {#if auth.error}
          <p id="auth-error" class="error" role="alert">{auth.error}</p>
        {/if}

        <button type="submit" disabled={auth.busy} aria-busy={auth.busy}>
          {auth.busy ? 'Checking…' : 'Unlock doorbell'}
        </button>
      </form>
    </section>
  </main>
{/if}

<style>
  .auth-gate {
    position: fixed;
    inset: 0;
    z-index: 9999;
    display: grid;
    place-items: center;
    overflow-y: auto;
    padding: calc(var(--gap) + env(safe-area-inset-top))
      calc(var(--gap) + env(safe-area-inset-right))
      calc(var(--gap) + env(safe-area-inset-bottom))
      calc(var(--gap) + env(safe-area-inset-left));
    background: radial-gradient(ellipse at 50% 42%, var(--surface) 0, var(--bg) 68%);
    color: var(--text);
  }

  .card {
    position: relative;
    width: min(100%, 460px);
    padding: clamp(24px, 6vw, 44px);
    overflow: hidden;
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow-sheet);
  }

  .card::before {
    position: absolute;
    inset: 0 0 auto;
    height: 3px;
    background: var(--accent);
    content: '';
  }

  .identity {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    margin-bottom: calc(var(--gap) * 1.5);
  }

  .brand-mark {
    position: relative;
    display: grid;
    flex: 0 0 48px;
    width: 48px;
    height: 48px;
    place-items: center;
    background: var(--bg);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
  }

  .brand-mark::before {
    width: 12px;
    height: 12px;
    background: var(--accent);
    border-radius: 50%;
    box-shadow: 0 0 0 6px var(--bg), 0 0 0 7px var(--accent);
    content: '';
  }

  .eyebrow {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--text-label);
    font-weight: 700;
    letter-spacing: 0.08em;
    line-height: 1.35;
  }

  .eyebrow span {
    padding-inline: 0.25em;
    color: var(--accent);
  }

  h1 {
    margin: 0;
    font-size: var(--text-state);
    font-weight: 700;
    line-height: 1.15;
    letter-spacing: -0.025em;
  }

  .explanation {
    margin: var(--gap-sm) 0 0;
    color: var(--text-muted);
    font-size: var(--text-body);
    line-height: 1.5;
  }

  .login-form {
    display: grid;
    gap: var(--gap-sm);
    margin-top: calc(var(--gap) * 1.5);
  }

  label {
    color: var(--text);
    font-size: var(--text-label);
    font-weight: 600;
  }

  input {
    width: 100%;
    min-height: var(--tap);
    padding: 10px var(--gap);
    background: var(--bg);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: var(--text-body);
  }

  input:focus-visible {
    outline: var(--focus-ring-width) solid var(--focus-ring);
    outline-offset: var(--focus-ring-offset);
  }

  .hint,
  .error {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
  }

  .hint {
    color: var(--text-muted);
  }

  .error {
    padding: var(--gap-sm) var(--gap);
    background: var(--bg);
    border: var(--border-width) solid var(--state-alert);
    border-radius: var(--radius-sm);
    color: var(--state-alert);
  }

  button {
    min-height: var(--tap-primary);
    margin-top: var(--gap-sm);
    padding: var(--gap-sm) var(--gap);
    background: var(--accent);
    border: var(--border-width) solid var(--accent);
    border-radius: var(--radius-sm);
    color: var(--accent-contrast);
    cursor: pointer;
    font-size: var(--text-label);
    font-weight: 700;
  }

  button:hover:not(:disabled) {
    background: var(--surface-hover);
    color: var(--text);
  }

  button:disabled {
    background: var(--surface-hover);
    border-color: var(--control-border);
    color: var(--text-muted);
    cursor: wait;
  }
</style>
