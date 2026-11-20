<!--
  The "Sound" sheet (D26): one app-gain volume control plus the audio self-test.
  Retired from the dashboard by D28; the control rail replaces it. "Play test
  chime" lives in Settings → Audio (`web/src/lib/settings/AudioPanel.svelte`).
  This component is no longer mounted anywhere and remains only because
  `scripts/check-storyboard.mjs` still requires every «Component» named in the
  read-only storyboard artifact to exist.

  Why the slider commits on the DOM `change` event: `input` fires on every
  pixel of a drag, and PATCHing per pixel would hammer the backend and leave
  the echoed value racing the thumb. The draft binds locally; `change` (end of
  drag, or a keyboard step) is the single save point. The stepper commits once
  per tap through Stepper's `change` prop.

  Mute is an explicit APP-GAIN mute, not a mixer control: it sets
  `audio.volume` to 0 and unmute returns to the last non-zero volume used here
  (70 after a reload). It does not touch the system mixer, and the helper text
  says so.

  There is deliberately NO "system volume is low/muted" row: the backend has
  no PipeWire/Pulse mixer read (design 05 §8 — the row is absent, not broken),
  so rendering a disabled or fake warning would invent a fact.
-->
<script lang="ts">
  import {
    ApiError,
    testAudio,
    type AudioTestResponse
  } from './api.js';
  import { settings } from './settings.svelte.js';
  import Button from './ui/Button.svelte';
  import Icon from './ui/Icon.svelte';
  import Sheet from './ui/Sheet.svelte';
  import Slider from './ui/Slider.svelte';
  import Stepper from './ui/Stepper.svelte';
  import { DEFAULT_VOLUME, isMuted, unmuteTarget } from './volume.js';

  interface Props {
    open?: boolean;
  }

  let { open = $bindable(false) }: Props = $props();

  let draft = $state(DEFAULT_VOLUME);
  /** The last non-zero value used in this session; unmute restores it. */
  let lastNonZero = $state(DEFAULT_VOLUME);
  let testing = $state(false);
  let testMessage = $state<string | null>(null);

  const muted = $derived(isMuted(settings.volume));

  /*
   * Seed the draft each time the sheet opens. `settings.volume` is also a
   * dependency: after a save (or mute) the echoed value is the truth, so the
   * controls follow it instead of holding a stale draft.
   */
  $effect(() => {
    if (!open) {
      return;
    }
    const volume = settings.volume;
    draft = volume;
    if (volume > 0) {
      lastNonZero = volume;
    }
  });

  async function commit(value: number): Promise<void> {
    draft = value;
    const saved = await settings.setVolume(value);
    if (saved && value > 0) {
      lastNonZero = value;
    }
  }

  function commitFromSlider(): void {
    void commit(draft);
  }

  function commitFromStepper(value: number): void {
    void commit(value);
  }

  function toggleMute(): void {
    void commit(muted ? unmuteTarget(lastNonZero) : 0);
  }

  /** Honest wording: a missing device or latency is omitted, never "null". */
  function describeTest(result: AudioTestResponse): string {
    if (!result.ok) {
      return result.error ?? 'the test chime did not play';
    }
    const parts: string[] = ['Played'];
    if (result.device !== null && result.device !== '') {
      parts.push(`on ${result.device}`);
    }
    if (result.latency_ms !== null) {
      parts.push(`in ${result.latency_ms} ms`);
    }
    return parts.join(' ');
  }

  async function playTest(): Promise<void> {
    testing = true;
    testMessage = null;
    try {
      testMessage = describeTest(await testAudio());
    } catch (cause) {
      testMessage =
        cause instanceof ApiError ? cause.message : 'the test chime could not be requested';
    } finally {
      testing = false;
    }
  }
</script>

<Sheet bind:open title="Sound">
  <div class="controls">
    <!-- The wrapper's change event is the end-of-drag save point. -->
    <div class="slider-field" onchange={commitFromSlider}>
      <Slider label="Volume" bind:value={draft} min={0} max={100} step={1} unit="%" />
    </div>

    <Stepper
      label="Volume"
      bind:value={draft}
      min={0}
      max={100}
      step={1}
      unit="%"
      change={commitFromStepper}
    />

    <Button variant="secondary" onclick={toggleMute}>
      <Icon name={muted ? 'mdi:volume-off' : 'mdi:volume-high'} size={24} />
      <span>{muted ? 'Unmute' : 'Mute'}</span>
    </Button>

    <div class="helper">
      <p>Mute sets the doorbell's own volume to 0%. It does not change the system mixer.</p>
      <p>Unmute returns to the last volume used here — 70% after a reload.</p>
    </div>

    <Button variant="primary" loading={testing} onclick={() => void playTest()}>
      Play test chime
    </Button>

    <p class="test-result" aria-live="polite">{testMessage ?? ''}</p>

    <!-- The volume on screen belongs to the last successful save. -->
    {#if settings.error}
      <p class="save-error" role="status">{settings.error}</p>
    {/if}
  </div>
</Sheet>

<style>
  .controls {
    display: flex;
    flex-direction: column;
    gap: var(--gap);
  }

  .slider-field {
    display: block;
  }

  .helper {
    display: flex;
    flex-direction: column;
    gap: var(--gap-sm);
    font-size: var(--text-body);
    line-height: 1.4;
    color: var(--text-muted);
  }

  .helper p {
    margin: 0;
  }

  .test-result,
  .save-error {
    min-height: 1.5em;
    margin: 0;
    font-size: var(--text-body);
  }

  .save-error {
    color: var(--state-alert);
  }
</style>
