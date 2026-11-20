<!--
  The Speech section (storyboard S13 F4/F5/F7/F10/F11): which engine this installation
  synthesises with, that engine's own fields, what the resolver reports, and the Piper
  voice catalogue with its downloads.

  It reads `GET /api/settings` through the store (the defaults and the two secret flags),
  `GET /api/tts/engines` for availability, and — only while Piper is the selected engine —
  `GET /api/tts/voices` for the catalogue. Every `reason`, `warning`, `error` and
  `unavailable_reason` below is the server's own whole sentence, rendered verbatim; this
  panel invents none of them, and an empty answer is never given a reason of its own.

  `none` is a first-class choice (D27), so the off state is styled plainly here: no alert
  or warning colour, no error icon, no second action. A deliberately silent doorbell is a
  supported configuration, not a fault.

  External provider configuration lives in `SpeechProviders.svelte`: providers carry an
  explicit paid marker, their Test response reports measured latency and cost per 100
  characters, and a paid templated phrase has a request-scoped confirmation in
  `ButtonEditor.svelte` (HSD-s856 lifts HSD-31dk's deferral). `tts.renderer.token` remains
  absent because it configures *this device as a renderer*, not where a phrase is
  synthesised. The remote renderer token's stored value is never returned: its field starts
  empty on every mount.

  The default-engine choice takes effect after a restart, while each engine's own fields
  apply on the next press — `src/chime_deck/tts/speech.py` reads them per press ("a
  changed ``tts.ha.engine_id`` or ``tts.default_engine`` applies on the next press"), and
  the resolver itself is built at startup. The download poll is capped at two minutes and
  cancelled on unmount, so leaving the section can never leave a timer running.
-->
<script lang="ts">
  import {
    ApiError,
    getTtsEngines,
    getTtsVoices,
    getTtsProviders,
    getVoiceDownload,
    startVoiceDownload,
    type TtsEnginesResponse,
    type TtsProvidersResponse,
    type TtsVoicesResponse,
    type VoiceDownloadStatus
  } from '../api.js';
  import { settings } from '../settings.svelte.js';
  import {
    DEFAULT_ENGINE,
    DEFAULT_ENGINE_OPTIONS,
    DOWNLOAD_POLL_INTERVAL_MS,
    DOWNLOAD_POLL_MAX_ATTEMPTS,
    SPEECH_OFF,
    downloadStateLabel,
    formatProgress,
    formatSynthesisMs,
    isDownloadFinished,
    voiceChoices
  } from '../speech.js';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import Input from '../ui/Input.svelte';
  import Select from '../ui/Select.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import Stepper from '../ui/Stepper.svelte';
  import { toastStore } from '../ui/toast.svelte.js';
  import SectionFooter from './SectionFooter.svelte';
  import SpeechProviders from './SpeechProviders.svelte';

  /** Registry defaults and ranges (`settings_registry.py`), so a default never reads as a change. */
  const DEFAULT_HA_ENGINE_ID = 'tts.piper';
  const DEFAULT_HA_LANGUAGE = 'en_GB';
  const DEFAULT_PIPER_VOICE = 'en_GB-alan-low';
  const DEFAULT_ESPEAK_VOICE = 'en-gb';
  const DEFAULT_ESPEAK_SPEED = 160;
  const MIN_ESPEAK_SPEED = 80;
  const MAX_ESPEAK_SPEED = 450;
  const DEFAULT_REMOTE_URL = '';
  const DEFAULT_REMOTE_TIMEOUT = 20;
  const MIN_REMOTE_TIMEOUT = 1;
  const MAX_REMOTE_TIMEOUT = 120;

  /** The registry's own hints, quoted so the field and the server agree. */
  const HA_ENGINE_HINT =
    'The Home Assistant TTS entity, such as tts.piper — not this device’s local piper engine.';
  const HA_LANGUAGE_HINT = 'must be a language tag such as en_GB or en-GB';
  const ESPEAK_VOICE_HINT = "espeak-ng's voice name, such as en-gb.";
  const REMOTE_URL_HINT = 'must be empty or an http(s) URL';

  let engine = $state(DEFAULT_ENGINE);
  let haEngineId = $state(DEFAULT_HA_ENGINE_ID);
  let haLanguage = $state(DEFAULT_HA_LANGUAGE);
  let piperVoice = $state(DEFAULT_PIPER_VOICE);
  let espeakVoice = $state(DEFAULT_ESPEAK_VOICE);
  let espeakSpeed = $state(DEFAULT_ESPEAK_SPEED);
  let remoteUrl = $state(DEFAULT_REMOTE_URL);
  let remoteToken = $state('');
  let clearToken = $state(false);
  let remoteTimeout = $state(DEFAULT_REMOTE_TIMEOUT);
  let httpTtsProvider = $state('');
  let initialised = $state(false);
  let saving = $state(false);
  let saveError = $state<string | null>(null);
  let alive = $state(false);

  let engines = $state<TtsEnginesResponse | null>(null);
  let enginesLoading = $state(false);
  let enginesError = $state<string | null>(null);
  let enginesSequence = 0;

  let voices = $state<TtsVoicesResponse | null>(null);
  let voicesLoading = $state(false);
  let voicesError = $state<string | null>(null);
  let voicesSequence = 0;

  // The http_tts selector owns only the provider id setting; SpeechProviders owns CRUD.
  let providerList = $state<TtsProvidersResponse | null>(null);
  let providerListLoading = $state(false);
  let providerListError = $state<string | null>(null);
  let providerListSequence = 0;

  let download = $state<VoiceDownloadStatus | null>(null);
  let downloadVoice = $state<string | null>(null);
  let downloadError = $state<string | null>(null);
  let downloadGaveUp = $state(false);
  let downloadSequence = 0;

  const speechOff = $derived(engine === SPEECH_OFF);
  // Only the STORED value is a health state: the resolver learns about `none` at startup,
  // so a draft that has not been saved yet is not something health can report.
  const storedOff = $derived(settings.text('tts.default_engine', DEFAULT_ENGINE) === SPEECH_OFF);

  const tokenHint = $derived(describeTokenHint());
  const voiceRows = $derived(voiceChoices(voices, piperVoice));

  // A curated voice is not installed, and a model installed by hand is not curated: the
  // two lists are shown side by side so neither hides the other.
  const installedExtras = $derived(
    (voices?.installed ?? []).filter(
      (voice) => !(voices?.suggested ?? []).some((row) => row.id === voice.id)
    )
  );

  // Comparison reads the store every time (never a captured copy), so adopting the echo
  // immediately makes the form clean again.
  const dirty = $derived(
    engine !== settings.text('tts.default_engine', DEFAULT_ENGINE) ||
      haEngineId !== settings.text('tts.ha.engine_id', DEFAULT_HA_ENGINE_ID) ||
      haLanguage !== settings.text('tts.ha.language', DEFAULT_HA_LANGUAGE) ||
      piperVoice !== settings.text('tts.piper.voice', DEFAULT_PIPER_VOICE) ||
      espeakVoice !== settings.text('tts.espeak_voice', DEFAULT_ESPEAK_VOICE) ||
      espeakSpeed !== savedEspeakSpeed() ||
      remoteUrl !== settings.text('tts.remote_tts.url', DEFAULT_REMOTE_URL) ||
      remoteTimeout !== savedRemoteTimeout() ||
      httpTtsProvider !== settings.text('tts.http_tts.provider', '') ||
      remoteToken !== '' ||
      clearToken
  );

  function savedEspeakSpeed(): number {
    return settings.integer(
      'tts.espeak_speed',
      MIN_ESPEAK_SPEED,
      MAX_ESPEAK_SPEED,
      DEFAULT_ESPEAK_SPEED
    );
  }

  function savedRemoteTimeout(): number {
    return settings.integer(
      'tts.remote_tts.timeout_s',
      MIN_REMOTE_TIMEOUT,
      MAX_REMOTE_TIMEOUT,
      DEFAULT_REMOTE_TIMEOUT
    );
  }

  /** The token hint: a pending clear wins over "stored" wins over "nothing stored". */
  function describeTokenHint(): string {
    if (clearToken) {
      return 'The stored token will be cleared when you save.';
    }
    if (settings.hasSecret('tts.remote_tts.token')) {
      return 'A token is stored. Type a new one to replace it.';
    }
    return 'No token stored.';
  }

  /** Re-read every key from the store, so the draft is the server's answer. */
  function rebase(): void {
    engine = settings.text('tts.default_engine', DEFAULT_ENGINE);
    haEngineId = settings.text('tts.ha.engine_id', DEFAULT_HA_ENGINE_ID);
    haLanguage = settings.text('tts.ha.language', DEFAULT_HA_LANGUAGE);
    piperVoice = settings.text('tts.piper.voice', DEFAULT_PIPER_VOICE);
    espeakVoice = settings.text('tts.espeak_voice', DEFAULT_ESPEAK_VOICE);
    espeakSpeed = savedEspeakSpeed();
    remoteUrl = settings.text('tts.remote_tts.url', DEFAULT_REMOTE_URL);
    remoteTimeout = savedRemoteTimeout();
    httpTtsProvider = settings.text('tts.http_tts.provider', '');
    // Secrets are write-only: the draft is always empty, never an echo of a stored value.
    remoteToken = '';
    clearToken = false;
    saveError = null;
  }

  $effect(() => {
    if (settings.loaded && !initialised) {
      rebase();
      initialised = true;
    }
  });

  async function refreshEngines(): Promise<void> {
    const sequence = ++enginesSequence;
    enginesLoading = true;
    enginesError = null;
    try {
      const response = await getTtsEngines();
      if (!alive || sequence !== enginesSequence) {
        return;
      }
      engines = response;
    } catch (cause) {
      if (!alive || sequence !== enginesSequence) {
        return;
      }
      enginesError =
        cause instanceof ApiError ? cause.message : 'the speech engines could not be listed';
      engines = null;
    } finally {
      if (alive && sequence === enginesSequence) {
        enginesLoading = false;
      }
    }
  }

  async function refreshProviders(): Promise<void> {
    const sequence = ++providerListSequence;
    providerListLoading = true;
    providerListError = null;
    try {
      const response = await getTtsProviders();
      if (!alive || sequence !== providerListSequence) {
        return;
      }
      providerList = response;
    } catch (cause) {
      if (!alive || sequence !== providerListSequence) {
        return;
      }
      providerList = null;
      providerListError =
        cause instanceof ApiError ? cause.message : 'the external providers could not be listed';
    } finally {
      if (alive && sequence === providerListSequence) {
        providerListLoading = false;
      }
    }
  }

  async function refreshVoices(): Promise<void> {
    const sequence = ++voicesSequence;
    voicesLoading = true;
    voicesError = null;
    try {
      const response = await getTtsVoices();
      if (!alive || sequence !== voicesSequence) {
        return;
      }
      voices = response;
    } catch (cause) {
      if (!alive || sequence !== voicesSequence) {
        return;
      }
      voicesError =
        cause instanceof ApiError ? cause.message : 'the Piper voices could not be listed';
      voices = null;
    } finally {
      if (alive && sequence === voicesSequence) {
        voicesLoading = false;
      }
    }
  }

  // The engine list is the availability answer for whichever engine is selected, so it is
  // fetched whenever speech is on — re-reading only when that changes, not on every switch.
  $effect(() => {
    if (!settings.loaded || speechOff) {
      return;
    }
    void refreshEngines();
  });

  // The voices route is the Piper catalogue only: no other engine downloads models.
  $effect(() => {
    if (!settings.loaded || engine !== 'piper') {
      return;
    }
    void refreshVoices();
  });

  // The picker needs provider names and ids; CRUD and its status remain owned by the
  // adjacent SpeechProviders section, which intentionally loads its own envelope.
  $effect(() => {
    if (!settings.loaded || speechOff || engine !== 'http_tts') {
      return;
    }
    void refreshProviders();
  });

  $effect(() => {
    alive = true;
    return () => {
      alive = false;
      // Bumping both counters cancels any in-flight fetch and the download poll.
      enginesSequence += 1;
      voicesSequence += 1;
      providerListSequence += 1;
      downloadSequence += 1;
    };
  });

  /** Only the changed keys travel; counts stay numbers and the token is never `''`. */
  function buildPatch(): Record<string, unknown> {
    const patch: Record<string, unknown> = {};
    if (engine !== settings.text('tts.default_engine', DEFAULT_ENGINE)) {
      patch['tts.default_engine'] = engine;
    }
    if (haEngineId !== settings.text('tts.ha.engine_id', DEFAULT_HA_ENGINE_ID)) {
      patch['tts.ha.engine_id'] = haEngineId;
    }
    if (haLanguage !== settings.text('tts.ha.language', DEFAULT_HA_LANGUAGE)) {
      patch['tts.ha.language'] = haLanguage;
    }
    if (piperVoice !== settings.text('tts.piper.voice', DEFAULT_PIPER_VOICE)) {
      patch['tts.piper.voice'] = piperVoice;
    }
    if (espeakVoice !== settings.text('tts.espeak_voice', DEFAULT_ESPEAK_VOICE)) {
      patch['tts.espeak_voice'] = espeakVoice;
    }
    if (espeakSpeed !== savedEspeakSpeed()) {
      patch['tts.espeak_speed'] = espeakSpeed;
    }
    if (remoteUrl !== settings.text('tts.remote_tts.url', DEFAULT_REMOTE_URL)) {
      patch['tts.remote_tts.url'] = remoteUrl;
    }
    if (remoteTimeout !== savedRemoteTimeout()) {
      patch['tts.remote_tts.timeout_s'] = remoteTimeout;
    }
    if (httpTtsProvider !== settings.text('tts.http_tts.provider', '')) {
      patch['tts.http_tts.provider'] = httpTtsProvider;
    }
    // A pending clear wins over a draft, and a clear is `null` — never `''`, which the
    // backend rejects.
    if (clearToken) {
      patch['tts.remote_tts.token'] = null;
    } else if (remoteToken !== '') {
      patch['tts.remote_tts.token'] = remoteToken;
    }
    return patch;
  }

  async function save(explicitPatch?: Record<string, unknown>): Promise<void> {
    if (saving) {
      return;
    }
    const patch = explicitPatch ?? buildPatch();
    if (Object.keys(patch).length === 0) {
      return;
    }
    saving = true;
    saveError = null;
    const ok = await settings.save(patch);
    if (!alive) {
      return;
    }
    saving = false;
    if (ok) {
      rebase();
      toastStore.push('success', 'Speech settings saved.');
    } else {
      // The draft stays on screen, and the server's sentence is the whole error.
      saveError = settings.error ?? 'the speech settings could not be saved';
    }
  }

  /** One tap, one write path: enabling speech is the same PATCH a save is. */
  function enableSpeech(): void {
    void save({ 'tts.default_engine': DEFAULT_ENGINE });
  }

  function markTokenCleared(): void {
    if (!settings.hasSecret('tts.remote_tts.token') || clearToken) {
      return;
    }
    clearToken = true;
    remoteToken = '';
  }

  function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  /** The words under a curated voice: only the facts the response carries. */
  function voiceFacts(voice: TtsVoicesResponse['suggested'][number]): string {
    const facts: string[] = [];
    if (voice.quality !== null) {
      facts.push(voice.quality);
    }
    if (voice.size_mb > 0) {
      facts.push(`${voice.size_mb} MiB`);
    }
    if (voice.licence !== null) {
      facts.push(voice.licence);
    }
    return facts.join(' · ');
  }

  /** Whether a started download is this voice's, so its button shows the busy state. */
  function downloadBusy(voiceId: string): boolean {
    return download !== null && downloadVoice === voiceId;
  }

  /**
   * Record a terminal job. Success refetches the catalogue, because the install changed
   * what is on disk and the picker must show the model as installed rather than curated.
   */
  function finishDownload(snapshot: VoiceDownloadStatus): void {
    download = null;
    downloadVoice = null;
    if (snapshot.state === 'done') {
      toastStore.push('success', 'Voice installed.');
      void refreshVoices();
      return;
    }
    downloadError = snapshot.error ?? 'the voice download did not finish';
  }

  /**
   * Start one voice download and poll its job, bounded by the attempt ceiling.
   *
   * A refused start is a real `ApiError` whose message is the engine's own sentence, so it
   * is shown verbatim; the poll stops the moment the panel is gone or a newer download
   * takes over, which is why every await re-checks the sequence.
   */
  async function startDownload(voiceId: string): Promise<void> {
    if (voices === null || !voices.can_download || download !== null || downloadVoice === voiceId) {
      return;
    }
    downloadError = null;
    downloadGaveUp = false;
    const sequence = ++downloadSequence;
    downloadVoice = voiceId;
    try {
      const started = await startVoiceDownload(voiceId);
      if (!alive || sequence !== downloadSequence) {
        return;
      }
      for (let attempt = 0; attempt < DOWNLOAD_POLL_MAX_ATTEMPTS; attempt += 1) {
        if (!alive || sequence !== downloadSequence) {
          return;
        }
        const snapshot = await getVoiceDownload(started.job_id);
        if (!alive || sequence !== downloadSequence) {
          return;
        }
        download = snapshot;
        if (isDownloadFinished(snapshot.state)) {
          finishDownload(snapshot);
          return;
        }
        await sleep(DOWNLOAD_POLL_INTERVAL_MS);
      }
      downloadGaveUp = true;
      downloadVoice = null;
    } catch (cause) {
      if (!alive || sequence !== downloadSequence) {
        return;
      }
      downloadError =
        cause instanceof ApiError ? cause.message : 'the voice download could not be started';
      downloadVoice = null;
    }
  }
</script>

<div class="panel">
  <h2>Speech</h2>

  {#if !settings.loaded}
    {#if settings.error !== null}
      <p class="error" role="alert">{settings.error}</p>
      <Button onclick={() => void settings.load()}>Retry</Button>
    {:else}
      <Spinner label="Loading speech settings" />
    {/if}
  {:else}
    <p class="lead">
      These settings apply to this installation — every device that shows this dashboard —
      not to this browser tab.
    </p>

    <div class="fields">
      <div class="field">
        <Select
          label="Default engine"
          options={DEFAULT_ENGINE_OPTIONS.map((option) => ({
            value: option.value,
            label: option.label,
            description: option.description
          }))}
          bind:value={engine}
        />
        <p class="hint">
          Changing the default engine takes effect after the doorbell restarts. Each
          engine's own settings apply on the next press.
        </p>
      </div>

      {#if speechOff}
        <!-- Storyboard F7: speech off is a state, not a fault — plain styling, one action. -->
        <section class="off" data-testid="speech-off" aria-labelledby="speech-off-title">
          <h3 id="speech-off-title">Speech is off</h3>
          <p>No speech — sound files only. Sound-file chimes remain available.</p>
          {#if storedOff}
            <p>Health: disabled — this is not a degraded status.</p>
          {/if}
          <div class="actions">
            <Button variant="primary" size="lg" disabled={saving} onclick={enableSpeech}>
              Enable speech
            </Button>
          </div>
        </section>
      {:else}
        <div class="fields">
          {#if engine === 'ha_tts'}
            <Input
              label="Home Assistant engine id"
              bind:value={haEngineId}
              hint={HA_ENGINE_HINT}
            />
            <Input label="Language" bind:value={haLanguage} hint={HA_LANGUAGE_HINT} />
          {:else if engine === 'piper'}
            <!--
              The picker waits for the catalogue: a `Choose…` trigger would read as "nothing
              chosen" while a voice is in fact stored, and the section below is already
              saying that the voices are being checked.
            -->
            {#if voices !== null}
              <Select label="Piper voice" options={voiceRows} bind:value={piperVoice} />
            {/if}
          {:else if engine === 'espeak_ng'}
            <Input label="Voice" bind:value={espeakVoice} hint={ESPEAK_VOICE_HINT} />
            <Stepper
              label="Speed"
              bind:value={espeakSpeed}
              min={MIN_ESPEAK_SPEED}
              max={MAX_ESPEAK_SPEED}
              unit=" wpm"
            />
          {:else if engine === 'remote_tts'}
            <Input label="Server URL" bind:value={remoteUrl} hint={REMOTE_URL_HINT} />
            <!--
              The stored token is never rendered: the backend returns only the flag, so the
              field starts empty and a hint states what saving will do.
            -->
            <Input
              label="Token"
              secret
              bind:value={remoteToken}
              hint={tokenHint}
              autocomplete="new-password"
            />
            {#if settings.hasSecret('tts.remote_tts.token')}
              <div class="actions">
                <Button variant="danger" disabled={clearToken} onclick={markTokenCleared}>
                  Clear stored token
                </Button>
              </div>
            {/if}
            <Stepper
              label="Timeout"
              bind:value={remoteTimeout}
              min={MIN_REMOTE_TIMEOUT}
              max={MAX_REMOTE_TIMEOUT}
              unit=" s"
            />
          {:else if engine === 'http_tts'}
            {#if providerListLoading && providerList === null}
              <div class="field">
                <span class="field-label">Provider</span>
                <Spinner label="Loading external providers" />
              </div>
            {:else if providerListError !== null && providerList === null}
              <div class="field">
                <span class="field-label">Provider</span>
                <p class="error" role="alert">{providerListError}</p>
                <Button onclick={() => void refreshProviders()}>Retry</Button>
              </div>
            {:else if providerList !== null && providerList.providers.length === 0}
              <EmptyState
                title="No external provider is configured yet."
                description="Add one in the External TTS providers section below."
              />
            {:else if providerList !== null}
              {@const selectedProvider = providerList.providers.find((provider) => provider.id === httpTtsProvider)}
              <div class="provider-picker">
                <Select
                  label="Provider"
                  options={[
                    { value: '', label: 'No provider selected' },
                    ...providerList.providers.map((provider) => ({ value: provider.id, label: provider.name }))
                  ]}
                  bind:value={httpTtsProvider}
                />
                {#if selectedProvider?.paid}
                  <Badge kind="accent" label="paid" />
                {/if}
              </div>
            {/if}
          {/if}
        </div>

        <section class="group" data-testid="speech-engines" aria-labelledby="speech-engines-title">
          <h3 id="speech-engines-title">Speech engine availability</h3>
          {#if enginesLoading}
            <!-- F4: loading is never drawn as an empty result. -->
            <Spinner label="Checking engines" />
          {:else if enginesError !== null}
            <p class="error" role="alert">{enginesError}</p>
            <Button onclick={() => void refreshEngines()}>Retry</Button>
          {:else if engines !== null}
            {#if engines.warning !== null}
              <p class="warning-row">
                <Badge kind="warn" label="warn" />
                <span>{engines.warning}</span>
              </p>
            {/if}
            {#if engines.engines.length === 0}
              {#if engines.warning === null}
                <!-- F5: an empty list with no sentence gets no invented reason either. -->
                <EmptyState
                  title="No engines reported"
                  description="The response contains an empty engines list and no explanation."
                />
              {/if}
            {:else}
              <ul class="engine-list">
                {#each engines.engines as row (row.name)}
                  <li class="engine-row">
                    <div class="engine-head">
                      <span class="engine-name">{row.name}</span>
                      <Badge
                        kind={row.available ? 'ok' : 'warn'}
                        label={row.available ? 'available' : 'unavailable'}
                      />
                      <span class="engine-last">last: {formatSynthesisMs(row.last_synthesis_ms)}</span>
                    </div>
                    {#if !row.available && row.reason !== null}
                      <p class="engine-reason">{row.reason}</p>
                    {/if}
                  </li>
                {/each}
              </ul>
            {/if}
          {:else}
            <Spinner label="Checking engines" />
          {/if}
        </section>

        {#if engine === 'piper'}
          <section class="group" data-testid="speech-voices" aria-labelledby="speech-voices-title">
            <h3 id="speech-voices-title">Piper voices</h3>
            {#if voicesLoading}
              <Spinner label="Checking voices" />
            {:else if voicesError !== null}
              <p class="error" role="alert">{voicesError}</p>
              <Button onclick={() => void refreshVoices()}>Retry</Button>
            {:else if voices !== null}
              {#if voices.warning !== null}
                <p class="warning-text">{voices.warning}</p>
              {/if}
              {#if voices.memory_warning !== null}
                <!-- The D22 sentence: a small board gets its RAM reason even when the
                     refusal below is for something else. -->
                <p class="warning-text">{voices.memory_warning}</p>
              {/if}
              {#if !voices.can_download && voices.unavailable_reason !== null}
                <!-- F11: the offer stays visible with its reason instead of vanishing. -->
                <p class="warning-text">{voices.unavailable_reason}</p>
              {/if}

              <ul class="voice-list">
                {#each voices.suggested as voice (voice.id)}
                  <li class="voice-row">
                    <div class="voice-head">
                      <span class="engine-name">{voice.id}</span>
                      {#if voice.installed}
                        <Badge kind="ok" label="installed" />
                      {/if}
                    </div>
                    <p class="voice-facts">{voiceFacts(voice)}</p>
                    {#if voice.problem !== null}
                      <p class="error">{voice.problem}</p>
                    {/if}
                    <div class="actions">
                      <Button
                        disabled={!voices.can_download || download !== null}
                        loading={downloadBusy(voice.id)}
                        onclick={() => void startDownload(voice.id)}
                      >
                        Download {voice.id}
                      </Button>
                    </div>
                  </li>
                {/each}
              </ul>

              {#if installedExtras.length > 0}
                <!-- F10: curated is not installed, and a hand-installed model stays listed. -->
                <h4>Installed on this device</h4>
                <ul class="voice-list">
                  {#each installedExtras as voice (voice.id)}
                    <li class="voice-row">
                      <span class="engine-name">{voice.id}</span>
                      <p class="voice-facts">{voice.size_mb} MiB</p>
                    </li>
                  {/each}
                </ul>
              {/if}

              {#if download !== null}
                {@const progress = formatProgress(download.bytes_done, download.bytes_total)}
                <div class="progress" aria-live="polite">
                  <p class="progress-words">
                    {download.voice} — {downloadStateLabel(download.state)}: {progress.label}
                  </p>
                  <div class="bar">
                    <div class="bar-fill" style:width={`${progress.percent}%`}></div>
                  </div>
                </div>
              {/if}

              {#if downloadError !== null}
                <p class="error" role="alert">{downloadError}</p>
              {/if}

              {#if downloadGaveUp}
                <p class="hint">
                  This panel stopped checking after 2 minutes. The download continues on the
                  device — reload to see the result.
                </p>
              {/if}
            {:else}
              <Spinner label="Checking voices" />
            {/if}
          </section>
        {/if}

        <SpeechProviders />
      {/if}
    </div>

    <SectionFooter
      label="Save speech"
      {dirty}
      {saving}
      error={saveError}
      onsave={() => void save()}
    />
  {/if}
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

  .panel h2 {
    margin: 0;
    font-size: var(--text-title);
    line-height: 1.25;
  }

  .lead {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--text-body);
    line-height: 1.45;
  }

  .fields {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .hint {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .error {
    margin: 0;
    color: var(--state-alert);
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }

  .provider-picker {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .provider-picker :global(.field) {
    flex: 1;
    min-width: min(100%, 16rem);
  }

  /* The off state keeps the ordinary surface and ink: D27 is a choice, not a fault. */
  .off,
  .group {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    padding: var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
    min-width: 0;
  }

  .off h3,
  .group h3 {
    margin: 0;
    font-size: var(--text-label);
    line-height: 1.25;
    font-weight: 600;
  }

  .off p,
  .group h4 {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .group h4 {
    font-size: var(--text-label);
    font-weight: 600;
  }

  .engine-list,
  .voice-list {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    margin: 0;
    padding: 0;
    list-style: none;
    min-width: 0;
  }

  .engine-row,
  .voice-row {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .engine-head,
  .voice-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .engine-name {
    font-family: var(--font-num);
    font-size: var(--text-body);
    font-weight: 600;
    overflow-wrap: anywhere;
  }

  .engine-last,
  .engine-reason,
  .voice-facts,
  .warning-text {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  /* The server's sentence, never restyled into a verdict of this panel's own. */
  .engine-reason {
    color: var(--text);
  }

  .warning-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .progress {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .progress-words {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }

  /* The words carry the number; the bar only makes the proportion visible. */
  .bar {
    width: 100%;
    height: 8px;
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius-sm);
    overflow: hidden;
  }

  .bar-fill {
    height: 100%;
    background: var(--text);
  }
</style>
