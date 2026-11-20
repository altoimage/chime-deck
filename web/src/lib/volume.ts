/**
 * The app-gain volume and its mute semantics, in one place (HSD-wpm0).
 *
 * "Mute" means exactly one thing here: `audio.volume === 0`. It is not a mixer
 * control, and the backend offers no PipeWire/Pulse mute read to mirror (design
 * 05 §8), so the dashboard sheet and the settings panel must not each decide for
 * themselves what mute means — two meanings would eventually disagree, and the
 * panel would report a muted doorbell that still chimes.
 *
 * `isMuted()` stays the zero-gain predicate (`audio.volume === 0`) and is the
 * only meaning this module gives the word. The device-wide "silence
 * everything" state is a separate boolean registry key, `audio.muted` (HSD-1lud,
 * read through `settings.muted`): it outranks quiet hours and the exempt rule
 * (D28), so folding it into the zero-gain predicate would report one state with
 * the other's name.
 *
 * Both screens import these functions; nothing here renders and nothing here
 * keeps state, so a test pins the semantics without a DOM.
 */

/** `audio.volume` registry default (settings_registry.py). */
export const DEFAULT_VOLUME = 70;

/** The lowest volume the backend accepts (`minimum=0` in settings_registry.py). */
export const MIN_VOLUME = 0;

/** The highest volume the backend accepts (`maximum=100` in settings_registry.py). */
export const MAX_VOLUME = 100;

/**
 * One tap of the rail's volume buttons moves the master gain by this much (D28).
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
 * @param volume the current `audio.volume` reading, 0..100.
 * @param direction which button was tapped.
 */
export function stepVolume(volume: number, direction: 'up' | 'down'): number {
  const delta = direction === 'up' ? VOLUME_STEP : -VOLUME_STEP;
  return clampVolume(clampVolume(volume) + delta);
}

/**
 * Mute is app gain only: `audio.volume === 0`. Not a mixer control.
 *
 * A non-finite value reads as "not muted", which is the honest answer for a
 * number we could not parse: we do not know that the doorbell is silent.
 */
export function isMuted(volume: number): boolean {
  return volume === 0;
}

/**
 * The value Unmute restores: the last non-zero value used in this session, else
 * the default.
 *
 * A remembered `0` cannot be restored — unmuting to silence is not unmuting — so
 * it falls back to the registry default instead of leaving the button stuck at
 * the volume it was trying to leave.
 */
export function unmuteTarget(lastNonZero: number): number {
  const volume = clampVolume(lastNonZero);
  return volume === 0 ? DEFAULT_VOLUME : volume;
}
