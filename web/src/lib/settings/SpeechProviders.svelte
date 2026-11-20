<!--
  External TTS provider records for storyboard S13 F8. The provider's paid word is a
  fact badge, not a warning colour; the credential stays write-only in `ui/Input`.
  Test lives on each record so its measured result stays next to the provider it names.
  The section owns its list lifecycle and immediate provider actions; it deliberately
  does not share the Speech panel's engine-settings save footer.
-->
<script lang="ts">
  import {
    ApiError,
    createTtsProvider,
    deleteTtsProvider,
    getTtsProviders,
    testTtsProvider,
    updateTtsProvider,
    type TtsPresetSummary,
    type TtsProvider,
    type TtsProviderTestResponse,
    type TtsProvidersResponse
  } from '../api.js';
  import {
    describeTestResult,
    emptyProviderDraft,
    formatCostPer100Chars,
    formatLatency,
    hasLanguagePlaceholder,
    hasVoicePlaceholder,
    paidSentence,
    presetOptions,
    providerBody,
    providerDraft,
    providerProblems,
    type ProviderDraft
  } from '../providers.js';
  import { settings } from '../settings.svelte.js';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import Dialog from '../ui/Dialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import Input from '../ui/Input.svelte';
  import Select from '../ui/Select.svelte';
  import Sheet from '../ui/Sheet.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { toastStore } from '../ui/toast.svelte.js';

  let data = $state<TtsProvidersResponse | null>(null);
  let loading = $state(true);
  let loadError = $state<string | null>(null);
  let loadSequence = 0;
  let alive = false;

  let sheetOpen = $state(false);
  let editing = $state<TtsProvider | null>(null);
  let draft = $state<ProviderDraft | null>(null);
  let attempted = $state(false);
  let saving = $state(false);
  let saveError = $state<string | null>(null);

  let deleteTarget = $state<TtsProvider | null>(null);
  let deleteOpen = $state(false);
  let deletingId = $state<string | null>(null);
  let deleteError = $state<{ id: string; message: string } | null>(null);

  let testingId = $state<string | null>(null);
  let testResults = $state<Record<string, TtsProviderTestResponse>>({});

  let dynamicPaid = $state(false);
  let dynamicPaidSaving = $state(false);
  let dynamicPaidError = $state<string | null>(null);

  const selectedPreset = $derived.by((): TtsPresetSummary | null => {
    if (draft === null || data === null) {
      return null;
    }
    return data.presets.find((preset) => preset.id === draft?.preset) ?? null;
  });
  const problems = $derived(
    draft === null ? [] : providerProblems(draft, selectedPreset)
  );

  $effect(() => {
    if (settings.loaded && !dynamicPaidSaving) {
      dynamicPaid = settings.flag('tts.allow_dynamic_paid', false);
    }
  });

  async function load(): Promise<void> {
    const sequence = ++loadSequence;
    loading = true;
    loadError = null;
    try {
      const response = await getTtsProviders();
      if (!alive || sequence !== loadSequence) {
        return;
      }
      data = response;
    } catch (cause) {
      if (!alive || sequence !== loadSequence) {
        return;
      }
      data = null;
      loadError = cause instanceof ApiError ? cause.message : 'the external providers could not be listed';
    } finally {
      if (alive && sequence === loadSequence) {
        loading = false;
      }
    }
  }

  $effect(() => {
    alive = true;
    void load();
    return () => {
      alive = false;
      loadSequence += 1;
    };
  });

  function openAdd(): void {
    const firstPreset = data?.presets[0];
    if (firstPreset === undefined || saving) {
      return;
    }
    editing = null;
    draft = emptyProviderDraft(firstPreset);
    attempted = false;
    saveError = null;
    sheetOpen = true;
  }

  function openEdit(provider: TtsProvider): void {
    if (saving) {
      return;
    }
    editing = provider;
    draft = providerDraft(provider);
    attempted = false;
    saveError = null;
    sheetOpen = true;
  }

  function clearEditor(): void {
    sheetOpen = false;
    editing = null;
    draft = null;
    attempted = false;
    saveError = null;
  }

  function handleSheetClose(): void {
    clearEditor();
  }

  function markKeyCleared(): void {
    if (draft === null || editing?.has_api_key !== true || draft.clearApiKey) {
      return;
    }
    draft.clearApiKey = true;
    draft.apiKey = '';
  }

  async function saveProvider(): Promise<void> {
    attempted = true;
    if (draft === null || problems.length > 0 || saving) {
      return;
    }
    saving = true;
    saveError = null;
    const body = providerBody(draft);
    try {
      if (editing === null) {
        await createTtsProvider(body);
        if (!alive) {
          return;
        }
        toastStore.push('success', 'Provider added.');
      } else {
        await updateTtsProvider(editing.id, body);
        if (!alive) {
          return;
        }
        toastStore.push('success', 'Provider saved.');
      }
      clearEditor();
      await load();
    } catch (cause) {
      if (alive) {
        saveError = cause instanceof ApiError ? cause.message : 'the provider could not be saved';
      }
    } finally {
      if (alive) {
        saving = false;
      }
    }
  }

  function requestDelete(provider: TtsProvider): void {
    deleteTarget = provider;
    deleteError = null;
    deleteOpen = true;
  }

  function cancelDelete(): void {
    deleteOpen = false;
    deleteTarget = null;
  }

  async function confirmDelete(): Promise<void> {
    const target = deleteTarget;
    if (target === null || deletingId !== null) {
      return;
    }
    deletingId = target.id;
    deleteError = null;
    try {
      await deleteTtsProvider(target.id);
      if (!alive) {
        return;
      }
      deleteTarget = null;
      toastStore.push('success', 'Provider deleted.');
      await load();
    } catch (cause) {
      if (alive) {
        deleteError = {
          id: target.id,
          message: cause instanceof ApiError ? cause.message : 'the provider could not be deleted'
        };
        deleteTarget = null;
      }
    } finally {
      if (alive) {
        deletingId = null;
      }
    }
  }

  async function testProvider(provider: TtsProvider): Promise<void> {
    if (testingId !== null) {
      return;
    }
    testingId = provider.id;
    const nextResults = { ...testResults };
    delete nextResults[provider.id];
    testResults = nextResults;
    try {
      const result = await testTtsProvider(provider.id);
      if (alive) {
        testResults = { ...testResults, [provider.id]: result };
      }
    } catch (cause) {
      if (alive) {
        // Match AudioPanel.playTest(): a rejected request is a failed result, not a
        // successful HTTP 200 with ok=false. ApiError.message remains the server's text.
        testResults = {
          ...testResults,
          [provider.id]: {
            ok: false,
            provider: provider.id,
            latency_ms: null,
            chars: null,
            format: null,
            cost_per_100_chars_usd: null,
            estimated_cost_usd: null,
            error:
              cause instanceof ApiError ? cause.message : 'the provider test could not be requested'
          }
        };
      }
    } finally {
      if (alive) {
        testingId = null;
      }
    }
  }

  async function saveDynamicPaid(value: boolean): Promise<void> {
    if (dynamicPaidSaving) {
      return;
    }
    dynamicPaidSaving = true;
    dynamicPaidError = null;
    const saved = await settings.save({ 'tts.allow_dynamic_paid': value });
    dynamicPaidSaving = false;
    dynamicPaid = settings.flag('tts.allow_dynamic_paid', false);
    if (saved) {
      toastStore.push('success', 'Paid-provider phrase setting saved.');
    } else {
      dynamicPaidError = settings.error ?? 'the paid-provider phrase setting could not be saved';
    }
  }

  function presetLabel(provider: TtsProvider): string {
    return data?.presets.find((preset) => preset.id === provider.preset)?.label ?? provider.preset;
  }

  function keyHint(): string {
    if (draft?.clearApiKey) {
      return 'The stored key will be cleared when you save.';
    }
    if (editing?.has_api_key === true) {
      return 'A key is stored. Type a new one to replace it.';
    }
    return 'No key stored.';
  }

  function baseUrlHint(preset: TtsPresetSummary | null): string {
    if (preset?.requires_base_url) {
      return "This preset needs the server's base URL.";
    }
    return "When this field is blank, the server uses this preset's default base URL.";
  }

  function priceHint(preset: TtsPresetSummary | null): string {
    const note = preset?.pricing_note ?? '';
    return note === ''
      ? "Leave this blank to use the preset's estimate."
      : `Leave this blank to use the preset's estimate. ${note}`;
  }
</script>

<section class="providers" data-testid="speech-providers" aria-labelledby="providers-title">
  <header class="section-head">
    <h3 id="providers-title">External TTS providers</h3>
    <Button
      disabled={data === null || data.presets.length === 0 || saving}
      onclick={openAdd}
    >Add provider</Button>
  </header>

  {#if loading && data === null}
    <Spinner label="Loading external providers" />
  {:else if loadError !== null && data === null}
    <p class="error" role="alert">{loadError}</p>
    <Button onclick={() => void load()}>Retry</Button>
  {:else if data !== null}
    {#if data.warning !== null}
      <p class="warning-row"><Badge kind="warn" label="warn" /><span>{data.warning}</span></p>
    {/if}

    {#if data.providers.length === 0}
      <EmptyState
        title={data.warning === null ? 'No external providers are configured.' : 'No provider records to show.'}
        description={data.warning === null
          ? 'Add a provider to use external speech synthesis.'
          : 'The response contains no readable providers.'}
      />
    {:else}
      <ul class="provider-list">
        {#each data.providers as provider (provider.id)}
          <li class="provider-record">
            <div class="record-heading">
              <h4>{provider.name}</h4>
              {#if provider.paid}
                <Badge kind="accent" label="paid" />
              {/if}
            </div>
            <p>Preset: {presetLabel(provider)}</p>
            <p>Voice: {provider.voice || 'No voice configured.'}</p>
            <p>{provider.has_api_key ? 'A key is stored.' : 'No key stored.'}</p>
            {#if paidSentence(provider, data.presets) !== null}
              <p class="paid-detail">{paidSentence(provider, data.presets)}</p>
            {/if}
            <div class="record-actions">
              <Button onclick={() => openEdit(provider)}>Edit</Button>
              <Button
                variant="danger"
                disabled={deletingId !== null}
                loading={deletingId === provider.id}
                onclick={() => requestDelete(provider)}
              >Delete</Button>
              <Button
                loading={testingId === provider.id}
                disabled={testingId !== null}
                onclick={() => void testProvider(provider)}
              >Test</Button>
            </div>
            {#if deleteError?.id === provider.id}
              <p class="error" role="alert">{deleteError.message}</p>
            {/if}
            {#if testResults[provider.id] !== undefined}
              {@const result = testResults[provider.id]}
              <div class="test-result" aria-live="polite">
                {#if result.ok}
                  <Badge kind="ok" label="Test passed" />
                  <p>{describeTestResult(result)}</p>
                  <p>Latency: {formatLatency(result.latency_ms)}</p>
                  <p>Estimated cost per 100 characters: {formatCostPer100Chars(result.cost_per_100_chars_usd)}</p>
                {:else}
                  <Badge kind="alert" label="Test failed" />
                  <p>{result.error ?? describeTestResult(result)}</p>
                {/if}
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}

  <div class="paid-setting">
    <Toggle
      label="Allow templated phrases on paid providers"
      description="The default is off, so each templated paid save asks for confirmation because every press can bill."
      bind:checked={dynamicPaid}
      disabled={dynamicPaidSaving || !settings.loaded}
      change={(value) => void saveDynamicPaid(value)}
    />
    {#if dynamicPaidError !== null}
      <p class="error" role="alert">{dynamicPaidError}</p>
    {/if}
  </div>
</section>

<Sheet
  bind:open={sheetOpen}
  title={editing === null ? 'Add provider' : `Edit ${editing.name}`}
  onclose={handleSheetClose}
>
  {#if draft !== null}
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <form
      class="provider-form"
      onsubmit={(event) => { event.preventDefault(); void saveProvider(); }}
      onkeydown={(event) => {
        if (
          event.key === 'Enter' &&
          event.target instanceof HTMLInputElement &&
          !event.isComposing
        ) {
          event.preventDefault();
          void saveProvider();
        }
      }}
    >
      {#if data !== null}
        <Select label="Preset" options={presetOptions(data.presets)} bind:value={draft.preset} />
      {/if}
      {#if selectedPreset !== null}
        <div class="preset-notes">
          <p>{selectedPreset.description}</p>
          <p>{selectedPreset.pricing_note}</p>
        </div>
      {/if}

      <Input label="Name" bind:value={draft.name} />
      <Input
        label="Base URL"
        bind:value={draft.baseUrl}
        placeholder={selectedPreset?.requires_base_url ? undefined : 'Preset default'}
        hint={baseUrlHint(selectedPreset)}
      />
      <Input
        label="Voice"
        bind:value={draft.voice}
        required={selectedPreset !== null && hasVoicePlaceholder(selectedPreset)}
        hint={selectedPreset !== null && hasVoicePlaceholder(selectedPreset)
          ? 'This preset sends the voice in its request, so a voice is required.'
          : undefined}
      />
      {#if selectedPreset !== null && hasLanguagePlaceholder(selectedPreset)}
        <Input label="Language" bind:value={draft.language} />
      {/if}
      <Input
        label="API key"
        secret
        bind:value={draft.apiKey}
        disabled={draft.clearApiKey}
        hint={keyHint()}
        autocomplete="new-password"
      />
      {#if editing?.has_api_key === true}
        <Button variant="danger" disabled={draft.clearApiKey} onclick={markKeyCleared}>
          Clear stored key
        </Button>
      {/if}
      <Input
        label="Price per 1,000 characters (USD)"
        bind:value={draft.pricePer1k}
        inputmode="decimal"
        hint={priceHint(selectedPreset)}
      />
      <Toggle
        label="Paid provider"
        description="A paid provider can refuse a templated phrase because every press bills."
        bind:checked={draft.paid}
      />
      {#if selectedPreset?.id === 'custom'}
        <p class="hint">The custom preset sends POST with a {"{\"text\": …}"} body and expects raw audio; a provider whose request shape differs is configured through the API's overrides field.</p>
      {/if}
      {#if attempted && problems.length > 0}
        <ul class="problems" role="alert">
          {#each problems as problem (problem)}
            <li>{problem}</li>
          {/each}
        </ul>
      {/if}
      {#if saveError !== null}
        <p class="error" role="alert">{saveError}</p>
      {/if}
    </form>
  {/if}
  {#snippet footer()}
    <div class="sheet-actions">
      <Button variant="primary" size="lg" disabled={draft === null || problems.length > 0 || saving} loading={saving} onclick={() => void saveProvider()}>
        Save provider
      </Button>
      <Button size="lg" disabled={saving} onclick={clearEditor}>Cancel</Button>
    </div>
  {/snippet}
</Sheet>

{#if deleteTarget !== null}
  <Dialog
    bind:open={deleteOpen}
    title={`Delete “${deleteTarget.name}”?`}
    message={`Delete ${deleteTarget.name} from the external TTS provider list?`}
    confirmLabel={`Delete ${deleteTarget.name}`}
    destructive
    onconfirm={() => void confirmDelete()}
    oncancel={cancelDelete}
  />
{/if}

<style>
  .providers,
  .provider-list,
  .provider-form,
  .paid-setting {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

  .providers {
    padding: var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
  }

  .section-head,
  .record-heading,
  .record-actions,
  .warning-row,
  .sheet-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .section-head {
    justify-content: space-between;
    gap: var(--gap);
  }

  h3,
  h4 {
    margin: 0;
    font-size: var(--text-label);
    line-height: 1.3;
    font-weight: 700;
    overflow-wrap: anywhere;
  }

  .provider-list {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .provider-record {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    min-width: 0;
    padding: var(--gap);
    background: var(--bg);
    border: var(--border-width) solid var(--control-border);
    border-radius: var(--radius-sm);
  }

  .provider-record p,
  .preset-notes p,
  .test-result p,
  .hint,
  .error,
  .paid-detail {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .preset-notes,
  .test-result {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .preset-notes,
  .hint,
  .paid-detail {
    color: var(--text-muted);
  }

  .warning-row {
    align-items: flex-start;
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .warning-row > span {
    flex: 1;
    min-width: 0;
  }

  .error,
  .problems {
    color: var(--state-alert);
  }

  .problems {
    margin: 0;
    padding-inline-start: 1.5rem;
    font-size: var(--text-body);
    line-height: 1.45;
  }

  .sheet-actions {
    flex-direction: column;
    align-items: stretch;
  }

  .sheet-actions :global(button) {
    width: 100%;
  }
</style>
