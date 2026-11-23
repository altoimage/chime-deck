/**
 * The two audio sentences (HSD-8544), asserted as exact strings.
 *
 * `describeAudioTest` and `describeHardwareLevel` were lifted out of
 * `AudioPanel.svelte` into `audio-check.ts` so Settings → Audio and the wizard's
 * inline speaker check cannot word the same verdict differently. That promise is
 * only worth anything if the wording is pinned: every assertion below is the
 * whole sentence, so a change that reads better on one screen and worse on the
 * other fails here instead of at the device.
 *
 * Pure functions over the wire types, so this file runs in the default `node`
 * environment and needs no DOM and no fetch seam.
 */

import { describe, expect, it } from 'vitest';

import type { AudioTestResponse, HardwareVolumeResponse } from '../api.js';
import { describeAudioTest, describeHardwareLevel } from './audio-check.js';

/** A `AudioTestResponse` with every field at its "the probe said nothing" value. */
function audioTest(overrides: Partial<AudioTestResponse> = {}): AudioTestResponse {
  return { ok: true, device: null, latency_ms: null, error: null, ...overrides };
}

/** A `HardwareVolumeResponse` for an available output at `percent`. */
function hardware(
  overrides: Partial<HardwareVolumeResponse> = {}
): HardwareVolumeResponse {
  return {
    ok: true,
    available: true,
    device: 'PCM',
    control: 'PCM',
    percent: 80,
    muted: false,
    reason: '',
    configured: 80,
    applied: null,
    ...overrides
  };
}

describe('describeAudioTest', () => {
  it("uses the server's own sentence when the tone failed", () => {
    const prose = 'the audio backend reported no default output device — check audio.device_hint';
    const sentence = describeAudioTest(
      audioTest({ ok: false, device: null, latency_ms: null, error: prose })
    );
    // The probe is the only thing that knows why silence happened; rewording it
    // would be this module guessing where the server measured.
    expect(sentence).toBe(prose);
  });

  it('falls back to its own sentence when a failure carries no error', () => {
    expect(describeAudioTest(audioTest({ ok: false, device: null, error: null }))).toBe(
      'the test tone did not play'
    );
  });

  it('names the device and the measured start delay', () => {
    expect(describeAudioTest(audioTest({ device: 'pipewire/offline-output', latency_ms: 42 }))).toBe(
      'Played on pipewire/offline-output in 42 ms'
    );
  });

  it('explains an unmeasurable delay rather than printing a missing one', () => {
    expect(describeAudioTest(audioTest({ device: 'pipewire/offline-output', latency_ms: null }))).toBe(
      'Played on pipewire/offline-output — the start delay was shorter than the tone, so it could not be timed'
    );
  });

  it('never drops the device, whatever the delay says', () => {
    // The plan for this module listed a "device, no delay" case. There is none:
    // `latency_ms` is typed `number | null`, so the two branches above already
    // cover every wire value and the device always comes with a delay claim.
    // What can be asserted is the property those two branches share — a known
    // device is never reduced to "the tone played".
    for (const latency of [12, null]) {
      expect(
        describeAudioTest(audioTest({ device: 'PCM', latency_ms: latency })),
        `a device is never dropped for latency_ms ${String(latency)}`
      ).toContain('Played on PCM');
    }
  });

  it('names the delay alone when no device was reported', () => {
    expect(describeAudioTest(audioTest({ device: null, latency_ms: 7 }))).toBe('Played in 7 ms');
  });

  it('treats an empty device string as "no device"', () => {
    expect(describeAudioTest(audioTest({ device: '', latency_ms: 7 }))).toBe('Played in 7 ms');
  });

  it('says only that the tone played when it knows nothing more', () => {
    expect(describeAudioTest(audioTest())).toBe('Test tone played.');
  });
});

describe('describeHardwareLevel', () => {
  it("uses the server's reason when the read failed", () => {
    const prose = 'the audio backend is not running';
    expect(
      describeHardwareLevel(hardware({ ok: false, available: false, percent: null, reason: prose }))
    ).toBe(prose);
  });

  it('says the volume could not be read when a failed read gives no reason', () => {
    expect(
      describeHardwareLevel(hardware({ ok: false, available: false, percent: null, reason: '' }))
    ).toBe('The system volume could not be read.');
  });

  it("uses the server's reason when the device has no hardware control", () => {
    const prose = 'This device has no hardware volume control.';
    expect(
      describeHardwareLevel(
        hardware({ available: false, device: null, control: null, percent: null, reason: prose })
      )
    ).toBe(prose);
  });

  it('falls back to its own sentence when an unavailable device gives no reason', () => {
    expect(
      describeHardwareLevel(
        hardware({ available: false, device: null, control: null, percent: null, reason: '  ' })
      )
    ).toBe('This device has no hardware volume control; the level is set by the device itself.');
  });

  it('names the muted state and the device', () => {
    expect(describeHardwareLevel(hardware({ muted: true, percent: 80 }))).toBe(
      'System volume: muted on PCM.'
    );
  });

  it('names the muted state without a device', () => {
    // A number beside "muted" would invite the reader to believe it: the muted
    // sentence deliberately carries no percentage at all.
    expect(describeHardwareLevel(hardware({ muted: true, device: null }))).toBe(
      'System volume: muted.'
    );
  });

  it('names the level and the device', () => {
    expect(describeHardwareLevel(hardware({ percent: 80, device: 'PCM' }))).toBe(
      'System volume: 80 % on PCM.'
    );
  });

  it('names the level without a device', () => {
    expect(describeHardwareLevel(hardware({ percent: 55, device: null }))).toBe(
      'System volume: 55 %.'
    );
  });

  it('treats an empty device string as "no device"', () => {
    expect(describeHardwareLevel(hardware({ percent: 55, device: '' }))).toBe(
      'System volume: 55 %.'
    );
  });
});
