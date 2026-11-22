/**
 * The app-gain volume and its mute semantics, in one place (HSD-wpm0).
 *
 * Audible loudness passes through two independent stages: the app's software
 * gain and the output device's own hardware control. The values are not
 * percentages of a shared scale — 50% software multiplied by 50% hardware is
 * 25% of full-scale output. Keeping the rules here prevents the rail, settings
 * panel and dashboard badge from inventing different answers.
 *
 * `isMuted()` stays the zero-gain predicate for ONE stage. `isSilent()` is the
 * device-wide answer: silence is intentional mute or either stage being fully
 * down. The crossed-speaker badge uses that predicate rather than reimplementing
 * it.
 *
 * Both screens import these functions; nothing here renders and nothing here
 * keeps state, so a test pins the semantics without a DOM.
 */

/** `audio.volume` registry default (settings_registry.py), changed in cd-qul9. */
export const DEFAULT_VOLUME = 100;

/** `audio.hardware_volume` registry default (settings_registry.py). */
export const DEFAULT_HARDWARE_VOLUME = 100;

/** The volume stage controlled by the dashboard rail. */
export type RailTarget = 'software' | 'hardware';

/** Accepted rail targets, in the order shown by the settings control. */
export const RAIL_TARGETS: readonly RailTarget[] = ['software', 'hardware'];

/** `audio.rail_target` registry default. */
export const DEFAULT_RAIL_TARGET: RailTarget = 'software';

/** Whether an unreadable setting value is one of the supported rail targets. */
export function isRailTarget(value: string): value is RailTarget {
  return value === 'software' || value === 'hardware';
}

/** The one registry key a rail tap is allowed to change. */
export function railVolumeKey(target: RailTarget): 'audio.volume' | 'audio.hardware_volume' {
  return target === 'software' ? 'audio.volume' : 'audio.hardware_volume';
}

/** The lowest volume the backend accepts (`minimum=0` in settings_registry.py). */
export const MIN_VOLUME = 0;

/** The highest volume the backend accepts (`maximum=100` in settings_registry.py). */
export const MAX_VOLUME = 100;

/**
 * One tap of the rail's volume buttons moves its selected stage by this much (D28).
 *
 * D28 fixes the step at 5 points: no long-press acceleration and no sheet, so a
 * 56 px button is precise and comfortable to tap repeatedly on a wall panel.
 */
export const VOLUME_STEP = 5;

/**
 * Clamp an arbitrary number onto the volume range the backend accepts.
 *
 * A non-finite value (an empty control, a `NaN` from parsing) becomes the
 * documented default rather than `0`: a value we could not read must not
 * silently mute the doorbell. Everything else is rounded, because the backend's
 * setting is an integer and a fractional PATCH would be rejected.
 */
export function clampVolume(value: number): number {
  if (!Number.isFinite(value)) {
    return DEFAULT_VOLUME;
  }
  return Math.min(MAX_VOLUME, Math.max(MIN_VOLUME, Math.round(value)));
}

/**
 * The volume one tap of the rail's up/down button produces (D28).
 *
 * Exactly one step of 5 points, clamped onto the backend's 0..100 range, so the
 * ends stick (`98` + up is `100`, `100` + up is still `100`) instead of running
 * past a value the PATCH would reject. A non-finite input first clamps to the
 * documented default, then steps — an unreadable number must not land on
 * silence because a tap happened to be "down".
 *
 * @param volume the current software or hardware volume reading, 0..100.
 * @param direction which button was tapped.
 */
export function stepVolume(volume: number, direction: 'up' | 'down'): number {
  const delta = direction === 'up' ? VOLUME_STEP : -VOLUME_STEP;
  return clampVolume(clampVolume(volume) + delta);
}

/** Zero-gain predicate for ONE stage: that stage is fully down. */
export function isMuted(volume: number): boolean {
  return volume === 0;
}

/**
 * Device-wide silence predicate (D28): intentional mute or either stage fully down.
 *
 * A non-finite stage value is not treated as zero; we do not know that the
 * device is silent merely because a value could not be read.
 */
export function isSilent(input: {
  muted: boolean;
  volume: number;
  hardwareVolume: number;
}): boolean {
  return input.muted || isMuted(input.volume) || isMuted(input.hardwareVolume);
}

/**
 * The value Unmute restores: the last non-zero value used in this session, else
 * the default. Retained for the retired Sound sheet's storyboard fixture.
 *
 * A remembered `0` cannot be restored — unmuting to silence is not unmuting — so
 * it falls back to the registry default instead of leaving the button stuck at
 * the volume it was trying to leave.
 */
export function unmuteTarget(lastNonZero: number): number {
  const volume = clampVolume(lastNonZero);
  return volume === 0 ? DEFAULT_VOLUME : volume;
}
