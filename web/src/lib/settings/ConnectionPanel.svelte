<!--
  Connection settings (design 05 §2.2): Home Assistant and MQTT in two cards,
  each with one save action for its own field group. There is no competing
  section-wide save, and each group's save button is also that group's dirty
  indicator. The device name is committed with MQTT because discovery needs to
  hear the renamed unit alongside the broker configuration.

  Every rule this panel applies comes from `connection-form.ts`, which the
  first-run wizard shares (HSD-vl7t): the registry defaults, the client-side
  validation, the dirty test, the diff PATCH and the two never-throwing test
  runners. Keeping them there is what stops the panel and the wizard drifting
  apart on which keys a save sends, which endpoint a "Test" calls and how a
  result is worded.

  Secrets are write-only end to end. The backend never returns a secret value,
  only the `secrets` flags, so the token/password fields start EMPTY on every
  mount and the panel has no value it could accidentally render. Save builds a
  diff against the last loaded snapshot: an untouched secret sends no key, a
  typed one sends its new value, and the clear button sends `null` — never `""`,
  which the backend rejects. To undo an unsent clear, reload the page; the
  pending state is announced in the hint so it is never a surprise.

  The re-announce action stays here: it republishes discovery payloads, not a
  connection probe, so it has no business in the shared form rules.

  The Instance ID sits under a read-only sentence naming the id the unit
  actually publishes as, because the server pins that id from the first device
  name it is given (HSD-vwdy): a field that can disagree with the entities Home
  Assistant shows is a mystery, and a mystery here is a broken automation. The
  sentence reads the echoed setting, never the draft, so a half-typed value can
  never describe an identity that does not exist.
-->
<script lang="ts">
  import {
    republishAll,
    type HaTestResponse,
    type MqttTestResponse,
    type RepublishResponse
  } from '../api.js';
  import { settings } from '../settings.svelte.js';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import Card from '../ui/Card.svelte';
  import Input from '../ui/Input.svelte';
  import { toastStore } from '../ui/toast.svelte.js';
  import {
    DEFAULT_INSTANCE_ID,
    baseUrlProblem,
    draftFrom,
    groupDirty,
    groupInvalid,
    groupPatch,
    haTestView,
    mqttTestView,
    passwordHint,
    portProblem,
    rebaseDraft,
    rebaseHaFields,
    rebaseMqttFields,
    requestFailure,
    runHaTest,
    runMqttTest,
    tokenHint,
    type ConnectionDraft
  } from './connection-form.js';
  import ConnectionTestResult from './ConnectionTestResult.svelte';
  import HaConnectionFields from './HaConnectionFields.svelte';
  import MqttConnectionFields from './MqttConnectionFields.svelte';
  import SectionFooter from './SectionFooter.svelte';

  let draft = $state<ConnectionDraft>(draftFrom(settings));

  let initialised = $state(false);
  let savingHa = $state(false);
  let haSaveError = $state<string | null>(null);
  let savingMqtt = $state(false);
  let mqttSaveError = $state<string | null>(null);

  let haTesting = $state(false);
  let haResult = $state<HaTestResponse | null>(null);
  let haTestUsedTypedToken = $state(false);
  let mqttTesting = $state(false);
  let mqttResult = $state<MqttTestResponse | null>(null);
  let republishing = $state(false);
  let republishResult = $state<RepublishResponse | null>(null);

  const baseUrlError = $derived(baseUrlProblem(draft));
  const portError = $derived(portProblem(draft));
  const haInvalid = $derived(groupInvalid(draft, 'ha'));
  const haDirty = $derived(groupDirty(draft, settings, 'ha'));
  const mqttInvalid = $derived(groupInvalid(draft, 'mqtt'));
  const mqttDirty = $derived(groupDirty(draft, settings, 'mqtt'));

  const tokenHintText = $derived(tokenHint(draft, settings));
  const passwordHintText = $derived(passwordHint(draft, settings));

  // The echoed id, never the drafted one: the server pins `mqtt.instance_id` on
  // the first device-name write, so the sentence below must describe what this
  // unit actually publishes as — a half-typed field cannot.
  const effectiveId = $derived(settings.text('mqtt.instance_id', DEFAULT_INSTANCE_ID));

  const haView = $derived(haTestView(haResult));
  const mqttView = $derived(mqttTestView(mqttResult));

  // The shell loads the settings, not the panel, so the draft opens on the
  // registry defaults and takes the stored values when that load lands — field
  // by field, so anything already typed survives it (HSD-8rwk).
  $effect(() => {
    if (settings.loaded && !initialised) {
      draft = rebaseDraft(draft, settings);
      initialised = true;
    }
  });

  async function saveHa(): Promise<void> {
    if (savingHa || haInvalid || !haDirty) {
      return;
    }
    savingHa = true;
    haSaveError = null;
    const ok = await settings.save(groupPatch(draft, settings, 'ha'));
    savingHa = false;
    if (ok) {
      draft = rebaseHaFields(draft, settings);
      toastStore.push('success', 'Home Assistant settings saved.');
    } else {
      haSaveError = settings.error ?? 'the settings could not be saved';
      toastStore.push('error', `Home Assistant settings: ${haSaveError}`);
    }
  }

  async function saveMqtt(): Promise<void> {
    if (savingMqtt || mqttInvalid || !mqttDirty) {
      return;
    }
    savingMqtt = true;
    mqttSaveError = null;
    const ok = await settings.save(groupPatch(draft, settings, 'mqtt'));
    savingMqtt = false;
    if (ok) {
      draft = rebaseMqttFields(draft, settings);
      toastStore.push('success', 'MQTT settings and the device name saved.');
    } else {
      mqttSaveError = settings.error ?? 'the settings could not be saved';
      toastStore.push('error', `MQTT settings and the device name: ${mqttSaveError}`);
    }
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
    haTestUsedTypedToken = draft.token !== '';
    if (baseUrlProblem(draft) !== undefined) {
      haTesting = false;
      return;
    }
    haResult = await runHaTest({ baseUrl: draft.baseUrl, token: draft.token });
    haTesting = false;
  }

  async function runMqttConnectionTest(): Promise<void> {
    if (mqttTesting) {
      return;
    }
    mqttTesting = true;
    mqttResult = null;
    mqttResult = await runMqttTest();
    mqttTesting = false;
  }

  /** The re-announce badge's state word; colour never carries it alone.
   *
   * Ordered so the word never contradicts the count: a queued or rejected run
   * still announced buttons, and a failed run that announced some is "partly"
   * rather than "not".
   */
  function republishLabel(result: RepublishResponse): string {
    if (result.queued) {
      return 'Queued';
    }
    if (result.failed > 0) {
      return 'Partly rejected';
    }
    if (!result.ok) {
      if (result.announced > 0) {
        return 'Partly announced';
      }
      return 'Not announced';
    }
    return 'Announced';
  }

  function republishKind(result: RepublishResponse): 'ok' | 'warn' | 'alert' {
    if (result.queued || result.failed > 0) {
      return 'warn';
    }
    if (!result.ok) {
      return 'alert';
    }
    return 'ok';
  }

  /** The badge icon: an alert whenever the state is not plain success. */
  function republishIcon(result: RepublishResponse): string {
    if (republishKind(result) === 'ok') {
      return 'mdi:check-circle-outline';
    }
    return 'mdi:alert-circle-outline';
  }

  /** Always the server's count — never a guessed one. */
  function republishSummary(result: RepublishResponse): string {
    const noun = result.announced === 1 ? 'button' : 'buttons';
    return `Announced ${result.announced} ${noun} to Home Assistant`;
  }

  /**
   * How many payloads were not published. A count only: the server's `error`
   * prose, rendered verbatim beside it, is what names the cause.
   */
  function unpublishedSummary(count: number): string {
    return count === 1 ? '1 payload was not published.' : `${count} payloads were not published.`;
  }

  async function runRepublish(): Promise<void> {
    if (republishing) {
      return;
    }
    republishing = true;
    republishResult = null;
    try {
      republishResult = await republishAll();
    } catch (cause) {
      republishResult = {
        ok: false,
        announced: 0,
        queued: false,
        failed: 0,
        error: requestFailure(cause, 'the buttons could not be re-announced')
      };
    } finally {
      republishing = false;
    }
  }
</script>

<div class="panel">
  <Card title="Home Assistant">
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
    </div>

    <ConnectionTestResult view={haView} />
    {#if haResult !== null}
      <p class="facts" aria-live="polite">
        {#if haTestUsedTypedToken}
          This tested the token you typed; it was not saved.
        {:else}
          No token was typed for this test, so it used the stored token if one is saved. Type one
          above to test a different token.
        {/if}
      </p>
    {/if}

    <SectionFooter
      label="Save Home Assistant settings"
      describe="Home Assistant settings"
      dirty={haDirty}
      invalid={haInvalid}
      saving={savingHa}
      error={haSaveError}
      onsave={saveHa}
    />
  </Card>

  <Card title="MQTT">
    <Input
      label="Device name"
      bind:value={draft.deviceName}
      hint="The name this unit appears as in Home Assistant, and the browser title."
    />
    <p class="facts">Saving this group also renames the unit in Home Assistant discovery.</p>

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

    <Input
      label="Discovery prefix"
      bind:value={draft.discoveryPrefix}
      hint="The discovery topic prefix Home Assistant listens on."
    />
    <Input
      label="Instance ID"
      bind:value={draft.instanceId}
      hint="Identifies this unit on the broker. It was derived from the device name when you first named it, so renaming the device does not change it — change it here only if you want a different identity."
    />
    <p class="facts">
      This unit publishes as <strong>{effectiveId}</strong>, so its Home Assistant entities are
      <code>chime_deck_{effectiveId}_…</code>.
    </p>

    <div class="actions">
      <Button loading={mqttTesting} onclick={runMqttConnectionTest}>Test MQTT</Button>
      <Button loading={republishing} onclick={runRepublish}>Re-announce all buttons</Button>
    </div>

    <ConnectionTestResult view={mqttView} />

    <div class="result" aria-live="polite">
      {#if republishResult}
        <Badge
          kind={republishKind(republishResult)}
          label={republishLabel(republishResult)}
          icon={republishIcon(republishResult)}
        />
        <p class="facts">{republishSummary(republishResult)}</p>
        {#if republishResult.queued}
          <p class="facts">
            The broker is offline, so the announcement is queued and will be sent when it
            reconnects.
          </p>
        {/if}
        {#if republishResult.failed > 0}
          <p class="facts">{unpublishedSummary(republishResult.failed)}</p>
        {/if}
        {#if republishResult.error}
          <p class="facts">{republishResult.error}</p>
        {/if}
      {/if}
    </div>

    <SectionFooter
      label="Save MQTT and device name"
      describe="MQTT and device name settings"
      dirty={mqttDirty}
      invalid={mqttInvalid}
      saving={savingMqtt}
      error={mqttSaveError}
      onsave={saveMqtt}
    />
  </Card>
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }

  /* The live region keeps its height only when it has something to say. */
  .result {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .result:empty {
    display: none;
  }

  .facts {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
  }

  /* The identity is code-like and can be long; it must never overflow the card. */
  .facts code {
    font-family: var(--font-num);
    word-break: break-word;
  }
</style>
