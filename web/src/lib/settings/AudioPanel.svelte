<script lang="ts">
  import {
    ApiError,
    listAudioDevices,
    readHardwareVolume,
    readServerClock,
    testAudio,
    type AudioDevicesResponse,
    type HardwareVolumeResponse,
    type AudioTestResponse,
    type ServerClockReading
  } from '../api.js';
  import { deviceChoices, matchNotice } from '../audio-devices.js';
  import { describeDrift, driftSeconds, formatDeviceClock } from '../device-clock.js';
  import { settings } from '../settings.svelte.js';
  import {
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_START,
    describeQuietWindow,
    formatClockTime,
    parseClockTime,
    type ClockTime
  } from '../quiet-hours.js';
  import Badge from '../ui/Badge.svelte';
  import Button from '../ui/Button.svelte';
  import Card from '../ui/Card.svelte';
  import Input from '../ui/Input.svelte';
  import SegmentedControl from '../ui/SegmentedControl.svelte';
  import Select from '../ui/Select.svelte';
  import Slider from '../ui/Slider.svelte';
  import Spinner from '../ui/Spinner.svelte';
  import Stepper from '../ui/Stepper.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import SectionFooter from './SectionFooter.svelte';
  import {
    DEFAULT_HARDWARE_VOLUME,
    DEFAULT_RAIL_TARGET,
    DEFAULT_VOLUME,
    clampVolume,
    isRailTarget,
    type RailTarget
  } from '../volume.js';

  const DEFAULT_BACKEND = 'mpv';
  const DEFAULT_SYSTEM_DEVICE = 'auto';

  const backendOptions = [{ value: 'mpv', label: 'mpv (libmpv)' }];
  const railTargetOptions = [
    { value: 'software', label: 'Software' },
    { value: 'hardware', label: 'Hardware' }
  ];

  let backend = $state(DEFAULT_BACKEND);
  let deviceChoice = $state(DEFAULT_SYSTEM_DEVICE);
  let volume = $state(DEFAULT_VOLUME);
  let hardwareVolume = $state(DEFAULT_HARDWARE_VOLUME);
  let railTarget = $state<RailTarget>(DEFAULT_RAIL_TARGET);
  let hardwareControl = $state('');
  let muted = $state(false);
  let quietHoursEnabled = $state(false);
  let quietStart = $state<ClockTime>({ ...DEFAULT_QUIET_START });
  let quietEnd = $state<ClockTime>({ ...DEFAULT_QUIET_END });
  let quietVolume = $state(0);
  let initialised = $state(false);
  let saving = $state(false);
  let saveError = $state<string | null>(null);

  let hardwareStatus = $state<HardwareVolumeResponse | null>(null);
  let hardwareLoading = $state(true);
  let hardwareError = $state<string | null>(null);
  let hardwareSequence = 0;

  let discovery = $state<AudioDevicesResponse | null>(null);
  let discoveryLoading = $state(true);
  let discoveryError = $state<string | null>(null);
  let discoverySequence = 0;

  let clockReading = $state<ServerClockReading | null>(null);
  let clockError = $state<string | null>(null);
  let browserNow = $state(Date.now());

  let testing = $state(false);
  let testResult = $state<AudioTestResponse | null>(null);
  let alive = false;

  const deviceOptions = $derived(
    discovery === null
      ? []
      : deviceChoices(discovery).map((choice) => ({
          value: choice.value,
          label: choice.label,
          description: choice.resolved ? `${choice.description} · resolved now` : choice.description
        }))
  );
  const notice = $derived(discovery === null ? null : matchNotice(discovery));
  const quietWindow = $derived(describeQuietWindow(quietStart, quietEnd));
  const dirty = $derived(
    backend !== settings.text('audio.backend', DEFAULT_BACKEND) ||
      deviceChoice !== baselineDeviceChoice() ||
      volume !== settings.integer('audio.volume', 0, 100, DEFAULT_VOLUME) ||
      hardwareVolume !== settings.hardwareVolume ||
      railTarget !== settings.railTarget ||
      hardwareControl !== settings.text('audio.hardware_control') ||
      muted !== settings.muted ||
      quietHoursEnabled !== settings.flag('audio.quiet_hours_enabled', false) ||
      formatClockTime(quietStart) !==
        formatClockTime(
          parseClockTime(settings.text('audio.quiet_hours_start')) ?? DEFAULT_QUIET_START
        ) ||
      formatClockTime(quietEnd) !==
        formatClockTime(parseClockTime(settings.text('audio.quiet_hours_end')) ?? DEFAULT_QUIET_END) ||
      quietVolume !== settings.integer('audio.quiet_hours_volume', 0, 100, 0)
  );

  function baselineDeviceChoice(): string {
    const hint = settings.text('audio.device_hint');
    if (hint === '') {
      return discovery?.system_default || DEFAULT_SYSTEM_DEVICE;
    }
    if (
      discovery?.resolved_device !== null &&
      discovery?.resolved_device !== undefined &&
      !deviceChoices(discovery).some((choice) => choice.value === hint)
    ) {
      return discovery.resolved_device;
    }
    return hint;
  }

  function rebase(): void {
    backend = settings.text('audio.backend', DEFAULT_BACKEND);
    deviceChoice = baselineDeviceChoice();
    volume = settings.integer('audio.volume', 0, 100, DEFAULT_VOLUME);
    hardwareVolume = settings.hardwareVolume;
    railTarget = settings.railTarget;
    hardwareControl = settings.text('audio.hardware_control');
    muted = settings.muted;
    quietHoursEnabled = settings.flag('audio.quiet_hours_enabled', false);
    quietStart = {
      ...(parseClockTime(settings.text('audio.quiet_hours_start')) ?? DEFAULT_QUIET_START)
    };
    quietEnd = { ...(parseClockTime(settings.text('audio.quiet_hours_end')) ?? DEFAULT_QUIET_END) };
    quietVolume = settings.integer('audio.quiet_hours_volume', 0, 100, 0);
    saveError = null;
  }

  $effect(() => {
    if (settings.loaded && !initialised) {
      rebase();
      initialised = true;
    }
  });

  async function refreshDiscovery(): Promise<void> {
    const sequence = ++discoverySequence;
    discoveryLoading = true;
    discoveryError = null;
    try {
      const response = await listAudioDevices();
      if (!alive || sequence !== discoverySequence) {
        return;
      }
      discovery = response;
      const storedHint = settings.text('audio.device_hint');
      if (
        deviceChoice === storedHint &&
        storedHint !== '' &&
        response.resolved_device !== null &&
        !deviceChoices(response).some((choice) => choice.value === storedHint)
      ) {
        deviceChoice = response.resolved_device;
      } else if (storedHint === '' && deviceChoice === DEFAULT_SYSTEM_DEVICE) {
        deviceChoice = response.system_default;
      }
    } catch (cause) {
      if (!alive || sequence !== discoverySequence) {
        return;
      }
      discoveryError =
        cause instanceof ApiError ? cause.message : 'audio outputs could not be discovered';
      discovery = null;
    } finally {
      if (alive && sequence === discoverySequence) {
        discoveryLoading = false;
      }
    }
  }

  async function refreshHardware(): Promise<void> {
    const sequence = ++hardwareSequence;
    hardwareLoading = true;
    hardwareError = null;
    try {
      const response = await readHardwareVolume();
      if (!alive || sequence !== hardwareSequence) {
        return;
      }
      hardwareStatus = response;
    } catch (cause) {
      if (!alive || sequence !== hardwareSequence) {
        return;
      }
      hardwareError =
        cause instanceof ApiError ? cause.message : 'the hardware volume could not be read';
    } finally {
      if (alive && sequence === hardwareSequence) {
        hardwareLoading = false;
      }
    }
  }

  async function refreshClock(): Promise<void> {
    clockError = null;
    clockReading = null;
    try {
      const reading = await readServerClock();
      if (!alive) {
        return;
      }
      clockReading = reading;
      browserNow = Date.now();
    } catch (cause) {
      if (!alive) {
        return;
      }
      clockError = cause instanceof ApiError ? cause.message : 'the device clock could not be read';
    }
  }

  $effect(() => {
    alive = true;
    void refreshDiscovery();
    void refreshHardware();
    void refreshClock();
    const interval = setInterval(() => {
      browserNow = Date.now();
    }, 1000);
    return () => {
      alive = false;
      clearInterval(interval);
      discoverySequence += 1;
      hardwareSequence += 1;
    };
  });

  function buildPatch(): Record<string, unknown> {
    const patch: Record<string, unknown> = {};
    if (backend !== settings.text('audio.backend', DEFAULT_BACKEND)) {
      patch['audio.backend'] = backend;
    }
    const baselineDevice = baselineDeviceChoice();
    if (deviceChoice !== baselineDevice) {
      patch['audio.device_hint'] = deviceChoice;
    }
    if (volume !== settings.integer('audio.volume', 0, 100, DEFAULT_VOLUME)) {
      patch['audio.volume'] = volume;
    }
    if (hardwareVolume !== settings.hardwareVolume) {
      patch['audio.hardware_volume'] = hardwareVolume;
    }
    if (railTarget !== settings.railTarget) {
      patch['audio.rail_target'] = railTarget;
    }
    if (hardwareControl !== settings.text('audio.hardware_control')) {
      patch['audio.hardware_control'] = hardwareControl;
    }
    if (muted !== settings.muted) {
      patch['audio.muted'] = muted;
    }
    if (quietHoursEnabled !== settings.flag('audio.quiet_hours_enabled', false)) {
      patch['audio.quiet_hours_enabled'] = quietHoursEnabled;
    }
    const savedStart =
      parseClockTime(settings.text('audio.quiet_hours_start')) ?? DEFAULT_QUIET_START;
    if (formatClockTime(quietStart) !== formatClockTime(savedStart)) {
      patch['audio.quiet_hours_start'] = formatClockTime(quietStart);
    }
    const savedEnd = parseClockTime(settings.text('audio.quiet_hours_end')) ?? DEFAULT_QUIET_END;
    if (formatClockTime(quietEnd) !== formatClockTime(savedEnd)) {
      patch['audio.quiet_hours_end'] = formatClockTime(quietEnd);
    }
    if (quietVolume !== settings.integer('audio.quiet_hours_volume', 0, 100, 0)) {
      patch['audio.quiet_hours_volume'] = quietVolume;
    }
    return patch;
  }

  async function save(): Promise<void> {
    const patch = buildPatch();
    if (saving || !dirty || Object.keys(patch).length === 0) {
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
      void refreshDiscovery();
      void refreshHardware();
    } else {
      saveError = settings.error ?? 'the settings could not be saved';
    }
  }

  function updateVolume(value: number): void {
    volume = clampVolume(value);
  }

  function updateHardwareVolume(value: number): void {
    hardwareVolume = clampVolume(value);
  }

  function updateRailTarget(value: string): void {
    if (isRailTarget(value)) {
      railTarget = value;
    }
  }

  function toggleMute(): void {
    muted = !muted;
  }

  function updateQuietStartHour(hour: number): void {
    quietStart = { ...quietStart, hour };
  }

  function updateQuietStartMinute(minute: number): void {
    quietStart = { ...quietStart, minute };
  }

  function updateQuietEndHour(hour: number): void {
    quietEnd = { ...quietEnd, hour };
  }

  function updateQuietEndMinute(minute: number): void {
    quietEnd = { ...quietEnd, minute };
  }

  async function playTest(): Promise<void> {
    if (testing) {
      return;
    }
    testing = true;
    testResult = null;
    try {
      const response = await testAudio();
      if (alive) {
        testResult = response;
      }
    } catch (cause) {
      if (alive) {
        testResult = {
          ok: false,
          device: null,
          latency_ms: null,
          error: cause instanceof ApiError ? cause.message : 'the test tone could not be requested'
        };
      }
    } finally {
      if (alive) {
        testing = false;
      }
    }
  }

  function browserClock(): string {
    return formatDeviceClock(browserNow, 0);
  }

  function deviceClock(): string | null {
    if (clockReading?.server_time_ms === null || clockReading === null) {
      return null;
    }
    return formatDeviceClock(
      clockReading.server_time_ms,
      Math.max(0, browserNow - clockReading.client_time_ms)
    );
  }

  function clockDrift(): string | null {
    if (clockReading?.server_time_ms === null || clockReading === null) {
      return null;
    }
    const elapsed = Math.max(0, browserNow - clockReading.client_time_ms);
    return describeDrift(driftSeconds(clockReading.server_time_ms + elapsed, browserNow));
  }

  function describeTest(result: AudioTestResponse): string {
    if (!result.ok) {
      return result.error ?? 'the test tone did not play';
    }
    if (result.device !== null && result.device !== '' && result.latency_ms !== null) {
      return `Played on ${result.device} in ${result.latency_ms} ms`;
    }
    if (result.device !== null && result.device !== '' && result.latency_ms === null) {
      return `Played on ${result.device} — the start delay was shorter than the tone, so it could not be timed`;
    }
    if (result.device !== null && result.device !== '') {
      return `Played on ${result.device}`;
    }
    if (result.latency_ms !== null) {
      return `Played in ${result.latency_ms} ms`;
    }
    return 'Test tone played.';
  }
</script>

<div class="panel">
  <section class="clock-strip" aria-label="Device clock">
    <h2>Device clock</h2>
    {#if clockError !== null}
      <p class="clock-error" role="alert">{clockError}</p>
      <Button onclick={() => void refreshClock()}>Retry</Button>
    {:else if clockReading === null}
      <Spinner label="Reading the device clock" />
    {:else}
      <div class="clock-readings" aria-live="off">
        {#if clockReading.server_time_ms !== null}
          <p><span>Device time</span>{' '}<strong>{deviceClock()}</strong></p>
          <p><span>this browser</span>{' '}<strong>{browserClock()}</strong></p>
          <p class="drift">{clockDrift()}</p>
          <p class="provenance">
            This is the device's own clock, read from its HTTP Date response header, good to ±1 s;
            comparing it with this browser's clock is what makes drift obvious.
          </p>
        {:else}
          <p><span>this browser</span>{' '}<strong>{browserClock()}</strong></p>
          <p class="provenance">The device did not report its clock in the HTTP Date response header.</p>
        {/if}
      </div>
    {/if}
  </section>

  <Card title="Output">
    <div class="fields">
      <Select label="Audio backend" options={backendOptions} bind:value={backend} />

      <div class="field">
        {#if discoveryLoading}
          <span class="field-label">Audio output</span>
          <Spinner label="Discovering audio outputs" />
        {:else if discoveryError !== null}
          <span class="field-label">Audio output</span>
          <p class="error" role="alert">{discoveryError}</p>
          <Button onclick={() => void refreshDiscovery()}>Retry</Button>
        {:else if discovery !== null}
          <Select label="Audio output" options={deviceOptions} bind:value={deviceChoice} />
          {#if discovery.ok}
            {@const resolvedChoice = deviceChoices(discovery).find((choice) => choice.resolved)!}
            <p class="resolved-device" aria-label="The audio output this device resolves to">
              <Badge kind="ok" label="resolved" />
              <span>{resolvedChoice.label}</span>
              <span>{resolvedChoice.description}</span>
            </p>
          {/if}
          {#if !discovery.ok}
            <p class="error" role="alert">{discovery.error ?? 'Audio output discovery failed.'}</p>
            <Button onclick={() => void refreshDiscovery()}>Retry</Button>
          {/if}
          {#if discovery.ok && discovery.devices.length === 0}
            <p class="hint">No audio outputs were discovered on this host. You can still choose System default (auto).</p>
          {/if}
          {#if notice !== null}
            <p class="hint">{notice}</p>
          {/if}
          <p class="hint">
            The stored value is a device NAME substring, matched case-insensitively against the name only — never the label.
          </p>
        {/if}
      </div>

      <div class="field volume-field">
        <div class="volume-controls">
          <div class="slider-commit" onchange={() => updateVolume(volume)}>
            <Slider label="Software volume" bind:value={volume} min={0} max={100} step={1} unit="%" />
          </div>
          <Stepper
            label="Software volume"
            bind:value={volume}
            min={0}
            max={100}
            step={1}
            unit="%"
            change={updateVolume}
          />
        </div>
        <Button variant="secondary" onclick={toggleMute}>{muted ? 'Unmute' : 'Mute'}</Button>
        <div class="helper">
          <p>
            What you hear is this software gain multiplied by the device's own hardware control.
            They are independent on purpose: 50% software + 50% hardware does not mean you selected
            one 25% volume setting.
          </p>
          <p>Mute silences both stages and leaves their saved levels unchanged.</p>
        </div>
      </div>

      <div class="field volume-field">
        <div class="volume-controls">
          <div class="slider-commit" onchange={() => updateHardwareVolume(hardwareVolume)}>
            <Slider
              label="Hardware volume"
              bind:value={hardwareVolume}
              min={0}
              max={100}
              step={1}
              unit="%"
              disabled={hardwareStatus === null || !hardwareStatus.ok || !hardwareStatus.available}
            />
          </div>
          <Stepper
            label="Hardware volume"
            bind:value={hardwareVolume}
            min={0}
            max={100}
            step={1}
            unit="%"
            disabled={hardwareStatus === null || !hardwareStatus.ok || !hardwareStatus.available}
            change={updateHardwareVolume}
          />
        </div>
        {#if hardwareLoading && hardwareStatus === null}
          <p class="hint" role="status">Reading hardware volume…</p>
        {:else if hardwareError !== null}
          <p class="error" role="status">{hardwareError}</p>
          <Button onclick={() => void refreshHardware()}>Retry hardware read</Button>
        {:else if hardwareStatus !== null && !hardwareStatus.ok}
          <p class="error" role="status">
            {hardwareStatus.reason || 'The hardware volume status could not be read.'}
          </p>
        {:else if hardwareStatus !== null && !hardwareStatus.available}
          <p class="hint" role="status">{hardwareStatus.reason}</p>
        {:else if hardwareStatus !== null}
          <p class="readback" role="status">
            {hardwareStatus.control ?? 'Hardware control'} on {hardwareStatus.device ?? 'the selected output'}
            — the device reports {hardwareStatus.muted ? 'muted' : `${hardwareStatus.percent ?? 'an unknown level'}${hardwareStatus.percent === null ? '' : '%'}`} right now.
          </p>
          {#if hardwareStatus.percent !== null &&
            hardwareStatus.configured !== null &&
            hardwareStatus.configured !== hardwareStatus.percent}
            <p class="mismatch" role="status">
              You asked for {hardwareStatus.configured}%, but the device reports {hardwareStatus.percent}%.
            </p>
          {/if}
        {/if}
      </div>

      <SegmentedControl
        label="Rail volume buttons"
        options={railTargetOptions}
        value={railTarget}
        change={updateRailTarget}
      />
      <p class="hint rail-hint">The front-panel ⬆/⬇ buttons change this one.</p>

      <Input
        label="Hardware control (advanced)"
        bind:value={hardwareControl}
        placeholder="auto"
        maxlength={64}
        hint="Leave empty to auto-detect; set the ALSA control name when the card exposes several (Speaker, Mic, Auto Gain Control)."
      />
    </div>
  </Card>

  <Card title="Quiet hours">
    <div class="fields">
      <Toggle
        label="Quiet hours"
        description="During this window, 0% suppresses presses and a higher level caps their volume."
        bind:checked={quietHoursEnabled}
      />

      <div class="field">
        <span class="field-label">Quiet hours start</span>
        <div class="time-controls">
          <Stepper
            label="Quiet hours start hour"
            value={quietStart.hour}
            min={0}
            max={23}
            step={1}
            change={updateQuietStartHour}
          />
          <Stepper
            label="Quiet hours start minute"
            value={quietStart.minute}
            min={0}
            max={59}
            step={1}
            change={updateQuietStartMinute}
          />
        </div>
      </div>

      <div class="field">
        <span class="field-label">Quiet hours end</span>
        <div class="time-controls">
          <Stepper
            label="Quiet hours end hour"
            value={quietEnd.hour}
            min={0}
            max={23}
            step={1}
            change={updateQuietEndHour}
          />
          <Stepper
            label="Quiet hours end minute"
            value={quietEnd.minute}
            min={0}
            max={59}
            step={1}
            change={updateQuietEndMinute}
          />
        </div>
      </div>

      <div class="field">
        <div class="volume-controls">
          <div class="slider-commit" onchange={() => (quietVolume = Math.min(100, Math.max(0, quietVolume)))}>
            <Slider label="Quiet hours volume" bind:value={quietVolume} min={0} max={100} step={1} unit="%" />
          </div>
          <Stepper
            label="Quiet hours volume"
            bind:value={quietVolume}
            min={0}
            max={100}
            step={1}
            unit="%"
          />
        </div>
        <p class="hint">
          0% suppresses presses inside the window entirely; above 0% caps the volume at that level.
        </p>
      </div>

      <p class="window-description">{quietWindow}</p>
    </div>
  </Card>

  <Card title="Test">
    <div class="fields">
      <Button variant="primary" size="lg" loading={testing} onclick={() => void playTest()}>
        Play test tone
      </Button>
      {#if testResult !== null}
        <div class="test-result" aria-live="polite">
          {#if testResult.ok}
            <Badge kind="ok" label="Test tone played" />
            <p>{describeTest(testResult)}</p>
          {:else}
            <Badge kind="alert" label="Test failed" />
            <p>{describeTest(testResult)}</p>
            <Button onclick={() => void playTest()} disabled={testing}>Test again</Button>
          {/if}
        </div>
      {/if}
      <p class="hint">
        Latency is the measured start delay (the playback time minus the tone's own length), not an estimate. If the tone finishes before it can be heard, the start delay is below what this test can resolve and no number is shown.
      </p>
    </div>
  </Card>

  <SectionFooter {dirty} {saving} error={saveError} onsave={() => void save()} />
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
    min-width: 0;
  }

  .clock-strip {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--gap-sm);
    padding: var(--gap);
    background: var(--surface);
    border: var(--border-width) solid var(--border);
    border-radius: var(--radius);
  }

  .clock-strip h2 {
    margin: 0;
    font-size: var(--text-title);
    line-height: 1.25;
  }

  .clock-readings {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    width: 100%;
  }

  .clock-readings p {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--gap-sm) var(--gap);
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.4;
  }

  .clock-readings strong {
    font-family: var(--font-num);
    font-size: var(--text-title);
    font-variant-numeric: tabular-nums;
  }

  .clock-readings .drift {
    font-weight: 600;
  }

  .clock-readings .provenance {
    color: var(--text-muted);
  }

  .clock-error,
  .error {
    margin: 0;
    color: var(--state-alert);
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
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

  .field-label {
    font-size: var(--text-label);
    line-height: 1.25;
    font-weight: 600;
  }

  .resolved-device {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--gap-sm);
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .hint,
  .helper,
  .window-description {
    margin: 0;
    color: var(--text-muted);
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .helper {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
  }

  .helper p {
    margin: 0;
  }

  .readback,
  .mismatch {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  .readback {
    color: var(--text-muted);
  }

  .mismatch {
    padding-inline-start: var(--gap-sm);
    border-inline-start: var(--border-width) solid var(--state-alert);
    color: var(--state-alert);
    font-weight: 600;
  }

  .volume-controls {
    display: flex;
    align-items: flex-end;
    gap: var(--gap);
    min-width: 0;
  }

  .slider-commit {
    flex: 1;
    min-width: 0;
  }

  .time-controls {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--gap-sm);
    min-width: 0;
  }

  .time-controls :global(.row) {
    gap: var(--gap-sm);
  }

  .time-controls :global(.value) {
    min-width: 2ch;
    font-size: var(--text-title);
  }

  .window-description {
    color: var(--text);
    font-weight: 600;
  }

  .test-result {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: var(--gap-sm);
  }

  .test-result p {
    margin: 0;
    font-size: var(--text-body);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

</style>
