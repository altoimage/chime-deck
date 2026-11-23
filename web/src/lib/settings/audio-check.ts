import type { AudioTestResponse, HardwareVolumeResponse } from '../api.js';

/**
 * The one wording of an audio-probe verdict, shared by Settings → Audio and the
 * wizard, so the two screens cannot describe the same result differently.
 *
 * The body is the text `AudioPanel` rendered before this module existed: the
 * device and the measured start delay when the probe reported them, and the
 * server's own sentence when it did not.
 */
export function describeAudioTest(result: AudioTestResponse): string {
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

/**
 * One sentence naming the device's own playback level, or why it cannot be
 * named.
 *
 * The order is the server's: a failed read speaks first, then an output with no
 * hardware control, then the muted state, and only then a number. The device is
 * omitted rather than printed as `null`, because a sentence is the whole point.
 */
export function describeHardwareLevel(status: HardwareVolumeResponse): string {
  if (!status.ok) {
    return status.reason.trim() !== '' ? status.reason : 'The system volume could not be read.';
  }
  if (!status.available) {
    return status.reason.trim() !== ''
      ? status.reason
      : 'This device has no hardware volume control; the level is set by the device itself.';
  }
  const named = status.device === null || status.device === '' ? '' : ` on ${status.device}`;
  if (status.muted) {
    return `System volume: muted${named}.`;
  }
  return `System volume: ${status.percent} %${named}.`;
}
