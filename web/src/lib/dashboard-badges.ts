/**
 * The dashboard header's two state badges, decided outside the component (D28).
 *
 * D28 splits the shell into three zones and makes the header **state only, no
 * controls**: besides the health pills and the clock it carries a moon badge
 * when quiet hours are *currently* suppressing, and a crossed-speaker badge when
 * the device is muted or at zero gain. Badges are invisible when inactive, so
 * each one is a promise: "the next chime will be silent, and here is why".
 *
 * A badge is therefore not a copy of a setting — it is the answer to "is sound
 * suppressed right now", and it must agree with `press.py`:
 *
 *   - the moon appears only when quiet hours are enabled, *now* is inside the
 *     half-open window (`in_quiet_hours`), and `audio.quiet_hours_volume` is
 *     zero. A positive quiet volume plays the chime quieter, it does not
 *     suppress it, so there is nothing to explain with a moon.
 *   - a start or end time that does not parse answers "no moon": the backend
 *     would never enter that window, so promising a silent night would be a lie.
 *   - the crossed speaker appears when `audio.muted` is on — the deliberate
 *     "silence this device" state, which outranks quiet hours and the exempt
 *     rule (D28, `PressOutcome.SUPPRESSED_MUTED`) — or when the master gain is
 *     already zero (`isMuted(volume)`), because both mean the same thing to
 *     someone standing at the panel: it will not make a sound.
 *
 * Pure module: no runes, no DOM, no fetching. The component reads the settings
 * store and the clock; this decides what the badges say.
 */

import { type ClockTime, inQuietWindow, parseClockTime } from './quiet-hours.js';
import { isMuted } from './volume.js';

/** Everything the header badges depend on, one value per registry key. */
export interface BadgeInput {
  /** The clock reading to test the window against (the header's own clock). */
  now: Date;
  /** `audio.quiet_hours_enabled`. */
  quietEnabled: boolean;
  /** `audio.quiet_hours_start`, `HH:MM` as stored. */
  quietStart: string;
  /** `audio.quiet_hours_end`, `HH:MM` as stored. */
  quietEnd: string;
  /** `audio.quiet_hours_volume`, 0..100; zero is the suppressing case. */
  quietVolume: number;
  /** `audio.muted` — the device-wide silence state (HSD-1lud). */
  muted: boolean;
  /** `audio.volume`, 0..100; zero is zero gain. */
  volume: number;
}

/** Which header badges are active. Both are hidden when false. */
export interface HeaderBadges {
  /** Moon: quiet hours are suppressing sound at `now`. */
  quietHours: boolean;
  /** Crossed speaker: muted, or at zero gain. */
  muted: boolean;
}

/**
 * Whether quiet hours are suppressing audio at this instant.
 *
 * Every condition is load-bearing, and an unreadable time fails closed to
 * "nothing is being suppressed": `parseClockTime` refuses the near misses
 * (`7:00`, `24:00`) that the registry would reject, so a window we cannot read is
 * a window the backend is not honouring either.
 */
function isQuietHoursSuppressing(input: BadgeInput, start: ClockTime, end: ClockTime): boolean {
  return input.quietEnabled && input.quietVolume <= 0 && inQuietWindow(input.now, start, end);
}

/** Decide both badges from the settings snapshot and the current time. */
export function headerBadges(input: BadgeInput): HeaderBadges {
  const start = parseClockTime(input.quietStart);
  const end = parseClockTime(input.quietEnd);
  const quietHours =
    start !== null && end !== null && isQuietHoursSuppressing(input, start, end);

  return {
    quietHours,
    muted: input.muted || isMuted(input.volume)
  };
}
