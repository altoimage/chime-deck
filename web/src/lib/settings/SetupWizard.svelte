<!--
  The first-run wizard (S01, design 05 §2.3): one route, three steps held in
  component state — Connect to Home Assistant → Connect to MQTT → Add your first
  tile and a first chime.

  Every rule about *what* to send lives in `connection-form.ts`, which the
  Connection panel shares: the registry defaults, the client-side validation,
  the diff PATCH and the two never-throwing probes. This component only decides
  the order the questions are asked in, and it asks them one step at a time.

  Four deliberate rules:

  - **A step writes only its own keys.** A save sends the changed keys of the
    step being tested — `ha.base_url`/`ha.token` on step 1,
    `mqtt.host`/`mqtt.port`/`mqtt.username`/`mqtt.password`/`mqtt.tls` on step
    2 — and nothing else. Typing a broker host on step 2 and walking back to
    step 1 must not save the broker, and the step-1 rebase after its own save
    leaves step 2's unsaved typing alone so a round trip never eats it. The
    credential fields are the Connection panel's own `MqttConnectionFields`
    (HSD-9xpm), so the two screens cannot show them differently.
  - **A failed save is the answer, not a precondition to ignore.** When the
    stored config is not what was typed, probing it would report on a
    configuration the user never entered, so the probe is skipped and the
    server's sentence is shown instead.
  - **Step 2 advances on "tested", not on "tested OK".** A broker that is
    temporarily unreachable must not strand the user in the wizard. The probe's
    own sentence is still rendered verbatim, and the setting can be corrected
    later from Settings.
  - **Skipping saves the dismissal first.** `ui.setup_dismissed` is install
    state, so a skip that failed to save would bounce the user from the
    dashboard straight back here — a loop. A failed skip therefore stays put
    and shows the reason.

  Step 3 renders the shipped `EntityPicker` and `ButtonEditor` full-screen
  rather than a third, invented picker: the wizard is a route through the real
  screens, never a parallel implementation of them.
-->
<script lang="ts">
  import {
    ApiError,
    listButtonRows,
    listEntityRows,
    setAdminPassword,
    type HaTestResponse,
    type MqttTestResponse
  } from '../api.js';
  import { auth } from '../auth.svelte.js';
  import { settings } from '../settings.svelte.js';
  import { lanSetupHint, setupDoneUrl } from '../setup.js';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import Input from '../ui/Input.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import {
    blankDraft,
    buildPatch,
    baseUrlProblem,
    draftFrom,
    haTestView,
    mqttTestView,
    passwordHint,
    portProblem,
    runHaTest,
    runMqttTest,
    tokenHint,
    type ConnectionDraft
  } from './connection-form.js';
  import BackToDashboard from './BackToDashboard.svelte';
  import ButtonEditor from './ButtonEditor.svelte';
  import ConnectionTestResult from './ConnectionTestResult.svelte';
  import EntityPicker from './EntityPicker.svelte';
  import HaConnectionFields from './HaConnectionFields.svelte';
  import MqttConnectionFields from './MqttConnectionFields.svelte';
  import UnprotectedBanner from './UnprotectedBanner.svelte';

  interface Props {
    /** Leave the wizard. A test passes a spy so nothing navigates. */
    onnavigate?: (path: string) => void;
  }

  const { onnavigate = (path: string) => window.location.assign(path) }: Props = $props();

  type Step = 1 | 2 | 3 | 4;
  type View = 'step' | 'tile' | 'chime';

  /** The settings keys each step owns; a step save may send only these. */
  const HA_KEYS = ['ha.base_url', 'ha.token'];
  const MQTT_KEYS = ['mqtt.host', 'mqtt.port', 'mqtt.username', 'mqtt.password', 'mqtt.tls'];

  let step = $state<Step>(1);
  let view = $state<View>('step');

  let draft = $state<ConnectionDraft>(blankDraft());
  let initialised = $state(false);
  let loadError = $state<string | null>(null);

  let haTesting = $state(false);
  let haResult = $state<HaTestResponse | null>(null);
  let haSaveError = $state<string | null>(null);
  let haTestedSignature = $state<string | null>(null);

  let mqttTesting = $state(false);
  let mqttResult = $state<MqttTestResponse | null>(null);
  let mqttSaveError = $state<string | null>(null);
  let mqttTestedSignature = $state<string | null>(null);

  let counting = $state(true);
  let countError = $state<string | null>(null);
  let tiles = $state(0);
  let chimes = $state(0);

  let dismissing = $state(false);
  let dismissError = $state<string | null>(null);

  let adminCurrent = $state('');
  let adminPassword = $state('');
  let adminConfirm = $state('');
  let adminBusy = $state(false);
  let adminError = $state<string | null>(null);
  const adminPasswordSet = $derived(auth.status?.passwordSet ?? false);
  const adminPasswordProblem = $derived(
    adminPassword.length === 0
      ? 'Enter a password.'
      : adminPassword.length < 8
        ? 'Use at least 8 characters.'
        : undefined
  );
  const adminConfirmProblem = $derived(
    adminConfirm.length === 0
      ? 'Confirm the password.'
      : adminConfirm !== adminPassword
        ? 'The passwords do not match.'
        : undefined
  );
  const adminCurrentProblem = $derived(
    adminPasswordSet && adminCurrent.length === 0 ? 'Enter the current password.' : undefined
  );
  const adminPasswordInvalid = $derived(
    adminBusy || adminPasswordProblem !== undefined || adminConfirmProblem !== undefined ||
      adminCurrentProblem !== undefined
  );

  // `settings.error` is the only evidence a load has finished when it failed,
  // so a failed load still counts as ready — the fields render their registry
  // defaults instead of leaving the user on a spinner that never ends.
  const ready = $derived(settings.loaded || settings.error !== null);

  const baseUrlError = $derived(baseUrlProblem(draft));
  const portError = $derived(portProblem(draft));
  const tokenHintText = $derived(tokenHint(draft, settings));
  const passwordHintText = $derived(passwordHint(draft, settings));

  const haView = $derived(haTestView(haResult));
  const mqttView = $derived(mqttTestView(mqttResult));

  const serverPort = $derived(settings.integer('server.port', 0, 65535, 8080));
  // A DOM-less mount sees no browser address; an empty one reads as loopback,
  // which is the honest answer — with no browser there is no network name to
  // claim.
  const lan = $derived(
    lanSetupHint({
      bindHost: settings.text('server.host', '127.0.0.1'),
      serverPort,
      browserHostname: typeof window === 'undefined' ? '' : window.location.hostname,
      browserPort: typeof window === 'undefined' ? '' : window.location.port
    })
  );

  /** Step 1 advances only on a pass that still describes the current draft. */
  const haReady = $derived(haResult?.ok === true && haTestedSignature === haSignature());

  /** Step 2 advances on a test that has been run since the last edit (see the comment above). */
  const mqttReady = $derived(mqttTestedSignature !== null && mqttTestedSignature === mqttSignature());

  // Initialise the draft when the load lands (or immediately when the wizard
  // mounts after it did). The load's failure sentence is captured ONCE here:
  // a later patch failure sets `settings.error` too, and it must never rewrite
  // the reason the form opened empty.
  $effect(() => {
    if (!ready || initialised) {
      return;
    }
    draft = draftFrom(settings);
    initialised = true;
    if (settings.error !== null) {
      loadError = settings.error;
    }
  });

  /** What a step-1 pass is a pass *about*: the address, the token, the clear. */
  function haSignature(): string {
    return `${draft.baseUrl}\u0000${draft.token}\u0000${draft.clearToken ? '1' : '0'}`;
  }

  /** The same, for the broker's fields — credentials included, so an edit re-locks Continue. */
  function mqttSignature(): string {
    const parts = [
      draft.host,
      draft.port,
      draft.username,
      draft.password !== '' ? '1' : '0', // a password pending, never the secret itself
      draft.clearPassword ? '1' : '0',
      draft.tls ? '1' : '0'
    ];
    return parts.join('\u0000');
  }

  /** The diff PATCH, narrowed to the keys the acting step owns. */
  function stepPatch(keys: string[]): Record<string, unknown> {
    const changed = buildPatch(draft, settings);
    const patch: Record<string, unknown> = {};
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(changed, key)) {
        patch[key] = changed[key];
      }
    }
    return patch;
  }

  /** Rebase step 1 on the echo, leaving step 2's unsaved typing untouched. */
  function rebaseHa(): void {
    const fresh = draftFrom(settings);
    draft.baseUrl = fresh.baseUrl;
    draft.token = fresh.token;
    draft.clearToken = fresh.clearToken;
  }

  /**
   * Rebase step 2 on the echo, for the same reason as `rebaseHa`.
   *
   * The username and a pending clear are adopted from the echo; the password
   * is never touched, because the server never returns it — the only value the
   * field can hold is the one the user typed.
   */
  function rebaseMqtt(): void {
    const fresh = draftFrom(settings);
    draft.host = fresh.host;
    draft.port = fresh.port;
    draft.username = fresh.username;
    draft.tls = fresh.tls;
    draft.clearPassword = fresh.clearPassword;
  }

  function markTokenCleared(): void {
    if (!settings.hasSecret('ha.token') || draft.clearToken) {
      return;
    }
    draft.clearToken = true;
    draft.token = '';
  }

  function markPasswordCleared(): void {
    if (!settings.hasSecret('mqtt.password') || draft.clearPassword) {
      return;
    }
    draft.clearPassword = true;
    draft.password = '';
  }

  async function runHaConnectionTest(): Promise<void> {
    if (haTesting) {
      return;
    }
    haTesting = true;
    haResult = null;
    haSaveError = null;
    haTestedSignature = null;

    // An invalid address is answered by the field's own sentence; a request
    // would only add noise the user cannot act on.
    if (baseUrlProblem(draft) !== undefined) {
      haTesting = false;
      return;
    }

    const patch = stepPatch(HA_KEYS);
    if (Object.keys(patch).length > 0 && !(await settings.save(patch))) {
      haSaveError = settings.error ?? 'the settings could not be saved';
      haTesting = false;
      return;
    }

    rebaseHa();
    haTestedSignature = haSignature();
    haResult = await runHaTest();
    haTesting = false;
  }

  async function runMqttConnectionTest(): Promise<void> {
    if (mqttTesting) {
      return;
    }
    mqttTesting = true;
    mqttResult = null;
    mqttSaveError = null;
    mqttTestedSignature = null;

    // The same rule as step 1: `buildPatch` cannot express a broken port, so
    // the inline sentence is the answer and nothing is sent.
    if (portProblem(draft) !== undefined) {
      mqttTesting = false;
      return;
    }

    const patch = stepPatch(MQTT_KEYS);
    if (Object.keys(patch).length > 0 && !(await settings.save(patch))) {
      mqttSaveError = settings.error ?? 'the settings could not be saved';
      mqttTesting = false;
      return;
    }

    rebaseMqtt();
    mqttTestedSignature = mqttSignature();
    mqttResult = await runMqttTest();
    mqttTesting = false;
  }

  /** How many tiles and chimes already exist — the server's counts, never a guess. */
  async function loadCounts(): Promise<void> {
    counting = true;
    countError = null;
    try {
      const [entities, buttons] = await Promise.all([listEntityRows(), listButtonRows()]);
      tiles = entities.length;
      chimes = buttons.length;
    } catch (cause) {
      countError = cause instanceof ApiError ? cause.message : 'the counts could not be loaded';
    } finally {
      counting = false;
    }
  }

  /** Leaving a full-screen picker or editor returns to step 3 with fresh counts. */
  function backToStep(): void {
    view = 'step';
    void loadCounts();
  }

  function advance(): void {
    if (step === 1 && haReady) {
      step = 2;
      return;
    }
    if (step === 2 && mqttReady) {
      step = 3;
      void loadCounts();
    }
  }

  function back(): void {
    if (step === 2) {
      step = 1;
      return;
    }
    if (step === 3) {
      step = 2;
      return;
    }
    if (step === 4) {
      step = 3;
    }
  }

  function openSecurityStep(): void {
    adminCurrent = '';
    adminPassword = '';
    adminConfirm = '';
    adminError = null;
    step = 4;
  }

  async function setWizardPassword(): Promise<void> {
    if (adminPasswordInvalid) {
      return;
    }
    adminBusy = true;
    adminError = null;
    try {
      const result = await setAdminPassword({
        current: adminPasswordSet ? adminCurrent : null,
        new: adminPassword
      });
      auth.passwordSetChanged(result.password_set);
      await finish();
    } catch (cause) {
      adminError = cause instanceof ApiError
        ? cause.message
        : 'The password could not be set. Please try again.';
    } finally {
      adminBusy = false;
    }
  }

  /**
   * Skip the wizard: save the dismissal, then leave. It answers whether the
   * move is allowed, so the shared dashboard escape can guard on it — `false`
   * means the dismissal could not be saved and the user must stay here.
   */
  async function dismiss(): Promise<boolean> {
    if (dismissing) {
      return false;
    }
    dismissing = true;
    dismissError = null;
    const ok = await settings.save({ 'ui.setup_dismissed': true });
    dismissing = false;
    if (ok) {
      onnavigate('/');
      return true;
    }
    // Stay on the wizard: navigating now would redirect straight back here.
    dismissError = settings.error ?? 'the settings could not be saved';
    return false;
  }

  async function finish(): Promise<void> {
    // Best effort: a failed dismissal must not trap a user who has just
    // configured everything. The dashboard is the right place either way.
    await settings.save({ 'ui.setup_dismissed': true });
    onnavigate(setupDoneUrl());
  }
</script>

<div class="wizard" data-setup="wizard">
  {#if !ready}
    <div class="loading">
      <Spinner label="Loading settings" />
    </div>
  {:else if view === 'tile'}
    <EntityPicker backLabel="Back to setup" onclose={backToStep} />
  {:else if view === 'chime'}
    <ButtonEditor id={null} backLabel="Back to setup" onback={backToStep} />
  {:else}
    <div class="frame">
      <header class="masthead" data-testid="shell-bar">
        <!-- The kiosk escape (HSD-h44d): guarding it on `dismiss` means the
             skip's saved dismissal is what allows the move, so the dashboard
             cannot bounce a fresh install back here. -->
        <BackToDashboard guard={dismiss} />
        <p class="product">ChimeDeck</p>
        <p class="label">Setup</p>
      </header>

      <div class="progress-row">
        <p class="progress" data-setup-progress>
          {step === 4 ? 'Optional step' : `Step ${step} of 3`}
        </p>
        <!-- Decorative: the words above carry the state, never the colour. -->
        <span class="dots" aria-hidden="true">
          {#each [1, 2, 3] as index (index)}
            <span class="dot" class:done={index <= step}></span>
          {/each}
        </span>
      </div>

      <p class="lan" data-setup-lan>
        {#if lan.kind === 'loopback'}
          This device only accepts connections from itself, so finish setup here. To set it up
          from a phone or PC, change the bind address in Settings → Connection first.
        {:else if lan.url !== null}
          You can also finish setup from another device on your network:
          <code data-setup-lan-url>{lan.url}</code>
        {:else}
          This device accepts connections from your network. Open this device's network address
          (port {serverPort}) in a browser on a phone or PC to finish setup there.
        {/if}
      </p>

      {#if loadError !== null}
        <p class="problem" role="alert">{loadError}</p>
      {/if}

      {#if dismissError !== null}
        <p class="problem" role="alert">{dismissError}</p>
      {/if}

      {#if step === 1}
        <section class="step" data-setup-step={1}>
          <h1 class="step-title">Connect to Home Assistant</h1>
          <p class="lead">
            The doorbell reads your entities from Home Assistant and announces its chime buttons
            there. Enter the address of your instance and a long-lived access token, then test the
            connection.
          </p>

          <HaConnectionFields
            bind:baseUrl={draft.baseUrl}
            bind:token={draft.token}
            tokenHint={tokenHintText}
            baseUrlError={baseUrlError}
            canClearToken={settings.hasSecret('ha.token')}
            cleared={draft.clearToken}
            onclear={markTokenCleared}
          />

          <div class="actions">
            <Button loading={haTesting} onclick={runHaConnectionTest}>Test connection</Button>
            <Button variant="primary" size="lg" disabled={!haReady} onclick={advance}>
              Continue
            </Button>
            <Button disabled={dismissing} onclick={dismiss}>I'll do it later</Button>
          </div>

          {#if haSaveError !== null}
            <p class="problem" role="alert">{haSaveError}</p>
          {/if}

          <ConnectionTestResult view={haView} />
        </section>
      {:else if step === 2}
        <section class="step" data-setup-step={2}>
          <h1 class="step-title">Connect to MQTT</h1>
          <p class="lead">
            The chime buttons are published to Home Assistant over MQTT, so the doorbell needs the
            broker it will announce them on.
          </p>

          <MqttConnectionFields
            bind:host={draft.host}
            bind:port={draft.port}
            bind:username={draft.username}
            bind:password={draft.password}
            bind:tls={draft.tls}
            portError={portError}
            passwordHint={passwordHintText}
            canClearPassword={settings.hasSecret('mqtt.password')}
            cleared={draft.clearPassword}
            onclear={markPasswordCleared}
          />
          <p class="lead">
            The discovery prefix and instance id are in Settings → Connection.
          </p>

          <div class="actions">
            <Button loading={mqttTesting} onclick={runMqttConnectionTest}>Test MQTT</Button>
            <Button variant="primary" size="lg" disabled={!mqttReady} onclick={advance}>
              Continue
            </Button>
            <Button onclick={back}>Back</Button>
            <Button disabled={dismissing} onclick={dismiss}>I'll do it later</Button>
          </div>

          {#if mqttSaveError !== null}
            <p class="problem" role="alert">{mqttSaveError}</p>
          {/if}

          <ConnectionTestResult view={mqttView} />
        </section>
      {:else if step === 3}
        <section class="step" data-setup-step={3}>
          <h1 class="step-title">Add your first tile and a first chime</h1>
          <p class="lead">
            A tile shows one entity on the panel; a chime is a button that plays a sound or speaks a
            phrase. Both are optional — the dashboard is valid with neither.
          </p>

          <!-- HSD-9xpm: step 3 has to explain itself while the broker is
               unauthenticated, because a chime that cannot be announced is not
               a failure the wizard should hide. The word comes from the
               structured `authFailed` flag, never from matching the English
               sentence the probe returned. -->
          {#if mqttView !== null && !mqttView.ok}
            <div class="notice" role="alert" data-setup-broker-problem>
              <Badge
                kind="alert"
                label={mqttView.authFailed ? 'Credentials rejected' : 'Broker not connected'}
                icon="mdi:alert-circle-outline"
              />
              {#if mqttView.error !== null}
                <p class="notice-text">{mqttView.error}</p>
              {/if}
              <p class="notice-text">
                A chime reaches Home Assistant through the broker, so it could not be announced
                until this connection works. Fix it on step 2, or add a chime later from the
                dashboard.
              </p>
            </div>
          {/if}

          <div class="actions">
            <Button variant="primary" size="lg" onclick={() => (view = 'tile')}>
              Add a first tile
            </Button>
            <Button variant="primary" size="lg" onclick={() => (view = 'chime')}>
              Add a first chime
            </Button>
          </div>

          <div class="counts" data-setup-counts>
            {#if counting}
              <p class="counts-line">Checking what is configured…</p>
            {:else if countError !== null}
              <p class="counts-line" role="alert">{countError}</p>
              <Button onclick={() => void loadCounts()}>Retry</Button>
            {:else}
              <p class="counts-line">
                <span data-setup-count="tiles">{tiles}</span>
                {tiles === 1 ? 'tile' : 'tiles'} and
                <span data-setup-count="chimes">{chimes}</span>
                {chimes === 1 ? 'chime' : 'chimes'} added.
              </p>
            {/if}
          </div>

          <div class="actions">
            <Button onclick={openSecurityStep}>Protect the configuration interface</Button>
            <Button variant="primary" size="lg" onclick={finish}>Finish setup</Button>
            <Button onclick={back}>Back</Button>
            <Button disabled={dismissing} onclick={dismiss}>I'll do it later</Button>
          </div>
        </section>
      {:else}
        <section class="step" data-setup-step={4}>
          <h1 class="step-title">Protect the configuration interface</h1>
          <p class="lead">
            A password protects configuration settings on the admin interface, including each
            tile's current state. If the password is lost, recovery requires physical access to
            the device.
          </p>
          <UnprotectedBanner status={auth.status} link={false} />

          {#if adminPasswordSet}
            <Input
              label="Current password"
              bind:value={adminCurrent}
              type="password"
              secret
              autocomplete="current-password"
              required
              error={adminCurrentProblem}
            />
          {/if}
          <Input
            label="New password"
            bind:value={adminPassword}
            type="password"
            secret
            autocomplete="new-password"
            required
            hint="Use at least 8 characters."
            error={adminPasswordProblem}
          />
          <Input
            label="Confirm new password"
            bind:value={adminConfirm}
            type="password"
            secret
            autocomplete="new-password"
            required
            error={adminConfirmProblem}
          />

          {#if adminError !== null}
            <p class="problem" role="alert">{adminError}</p>
          {/if}

          <div class="actions">
            <Button variant="primary" size="lg" loading={adminBusy} disabled={adminPasswordInvalid} onclick={setWizardPassword}>
              Set password
            </Button>
            <Button onclick={finish}>Skip for now</Button>
            <Button onclick={back}>Back</Button>
          </div>
        </section>
      {/if}
    </div>
  {/if}
</div>

<style>
  .wizard {
    min-height: 100dvh;
    background: var(--bg);
    color: var(--text);
  }

  .loading {
    display: flex;
    justify-content: center;
    padding-block: calc(var(--gap) * 2);
  }

  /*
    The padded, 720px-wide column holds the wizard's own chrome and step, so
    the borrowed full-screen picker and editor below get the whole viewport
    instead of a second, narrower frame around them.
  */
  .frame {
    /* The horizontal padding is a property too: the sticky bar has to cancel it
       with a negative inline margin to reach the frame's edges. */
    --shell-pad: clamp(var(--gap), 3vw, calc(var(--gap) * 2));
    /* The bar's own height, so sticky content below can sit under it. */
    --shell-bar: calc(var(--tap) + var(--gap-sm) * 2);

    display: flex;
    flex-direction: column;
    gap: var(--gap);
    width: min(100%, 720px);
    min-height: 100dvh;
    margin-inline: auto;
    padding: var(--shell-pad);
    padding-bottom: calc(var(--tap-primary) + env(safe-area-inset-bottom));
  }

  .masthead {
    position: sticky;
    top: 0;
    z-index: var(--z-sticky);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    margin-inline: calc(var(--shell-pad) * -1);
    padding: var(--gap-sm) var(--shell-pad);
    background: var(--bg);
  }

  /* The wordmark and the label sit on the right of the bar, beside the escape. */
  .product,
  .label {
    margin: 0;
    font-size: var(--text-label);
    font-weight: 700;
  }

  .product {
    margin-left: auto;
  }

  .label {
    color: var(--text-muted);
  }

  .progress-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
  }

  .progress {
    margin: 0;
    font-size: var(--text-body);
    font-weight: 600;
  }

  .dots {
    display: flex;
    gap: 8px;
  }

  .dot {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--surface);
    border: var(--border-width) solid var(--control-border);
  }

  .dot.done {
    background: var(--accent);
    border-color: var(--accent);
  }

  .lan {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    color: var(--text-muted);
  }

  .lan code {
    font-family: var(--font-num);
    font-size: var(--text-body);
    color: var(--text);
    word-break: break-all;
  }

  .step {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
  }

  .step-title {
    margin: 0;
    font-size: var(--text-title);
    line-height: 1.2;
    font-weight: 700;
  }

  .lead {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }

  /* A failure is a sentence the server wrote; it is never shrunk to helper text. */
  .problem {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    color: var(--state-alert);
  }

  .counts {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
  }

  /* The broker problem is a sentence plus a consequence, readable from across
     a wall panel: no hover-only anything and the word carries the state. */
  .notice {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    padding: var(--gap);
    border: var(--border-width) solid var(--state-alert);
    border-radius: var(--radius);
    font-size: var(--text-body);
  }

  .notice-text {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
  }

  .counts-line {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
  }
</style>
