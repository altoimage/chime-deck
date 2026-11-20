/**
 * The quiet-hours window: strict parsing, locale-free formatting, and the one
 * sentence that describes it.
 *
 * The backend owns the semantics (`src/chime_deck/press.py::in_quiet_hours`)
 * and this module mirrors them exactly, because a panel that guessed differently
 * would promise a silent night the doorbell does not respect:
 *
 *   - the window is HALF-OPEN: the start minute is inside, the end minute is
 *     outside (`22:00` starts the night, `07:00` is already daytime);
 *   - `start === end` is a ZERO-LENGTH window, so quiet hours never apply;
 *   - `start > end` is a window that crosses midnight — the usual `22:00 → 07:00`.
 *
 * Stored values are validated by the registry's own pattern
 * (`audio.quiet_hours_start`/`_end`, hint "must be a 24-hour time such as 22:00"),
 * so a parse that coerced `7:00` into `07:00` would store a value the backend
 * refuses. `parseClockTime` therefore only ever answers with a real `HH:MM`.
 *
 * Pure TS, no DOM and no runes: the panel renders, this module decides.
 */

/** A time of day, stored as the `HH:MM` setting the backend validates. */
export interface ClockTime {
  hour: number;
  minute: number;
}

/** `audio.quiet_hours_start` registry default (settings_registry.py). */
export const DEFAULT_QUIET_START: ClockTime = { hour: 22, minute: 0 };

/** `audio.quiet_hours_end` registry default (settings_registry.py). */
export const DEFAULT_QUIET_END: ClockTime = { hour: 7, minute: 0 };

/** Exactly the registry's pattern: two digits, 00–23 hours, 00–59 minutes. */
const CLOCK_TIME_PATTERN = /^([01]\d|2[0-3]):([0-5]\d)$/;

/** Two digits, zero padded. */
function pad(value: number): string {
  return value < 10 ? `0${value}` : String(value);
}

/** Minutes since midnight, so the window's ordering is one integer comparison. */
function minutesOf(time: ClockTime): number {
  return time.hour * 60 + time.minute;
}

/**
 * Parse a stored `HH:MM` setting strictly; `null` when it is not a real
 * 24-hour time.
 *
 * `"7:00"`, `"24:00"`, `"22:60"`, `" 22:00"`, `""` and `"2200"` are all `null`:
 * the value is going back to a backend that would reject it, so coercing it here
 * would only move the rejection to the save, after the user believed it worked.
 */
export function parseClockTime(value: string): ClockTime | null {
  const match = CLOCK_TIME_PATTERN.exec(value);
  if (match === null) {
    return null;
  }
  return { hour: Number(match[1]), minute: Number(match[2]) };
}

/** Format a clock time as `HH:MM`, zero padded, locale-free. */
export function formatClockTime(time: ClockTime): string {
  return `${pad(time.hour)}:${pad(time.minute)}`;
}

/**
 * Whether *now* falls inside the daily quiet-hours window.
 *
 * A deliberate mirror of `src/chime_deck/press.py::in_quiet_hours`: the same
 * three rules, in the same order, on the local wall-clock time of *now* — the
 * clock the header shows, so the moon badge and the chime agree about what time
 * it is. Nothing here re-derives the window; if the backend rule changes, this
 * function changes with it.
 *
 * @param now the clock reading to test (local wall-clock hour/minute).
 * @param start the first local minute inside the window.
 * @param end the first local minute outside the window.
 */
export function inQuietWindow(now: Date, start: ClockTime, end: ClockTime): boolean {
  const startMinutes = minutesOf(start);
  const endMinutes = minutesOf(end);
  if (startMinutes === endMinutes) {
    return false;
  }
  const current = now.getHours() * 60 + now.getMinutes();
  if (startMinutes < endMinutes) {
    return startMinutes <= current && current < endMinutes;
  }
  return current >= startMinutes || current < endMinutes;
}

/**
 * One readable sentence for the window, including the crossing-midnight case.
 *
 * It states only what the backend will do: the empty window is spelled out
 * because a user who set both times to `22:00` needs to know quiet hours are off,
 * not that they are "22:00 to 22:00".
 */
export function describeQuietWindow(start: ClockTime, end: ClockTime): string {
  const from = formatClockTime(start);
  const to = formatClockTime(end);
  if (minutesOf(start) === minutesOf(end)) {
    return `${from} → ${to} — the start and end are the same, so the window is empty and quiet hours never apply`;
  }
  const day = minutesOf(start) < minutesOf(end) ? 'same day' : 'next day';
  return `${from} → ${to}, ${day}`;
}
