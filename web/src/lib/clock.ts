/**
 * Locale-free time formatting for the header clock, the "changed" line and
 * its compact relative wording.
 *
 * `toLocaleTimeString()` is deliberately not used: its output depends on the
 * browser's locale and ICU data, so the same `Date` would print differently on
 * the wall panel than in a test run, and a test would have to pin a locale to
 * say anything at all. Reading the local components gives one stable `HH:MM`
 * and `YYYY-MM-DD HH:MM` everywhere.
 */

/** Two digits, zero padded. */
function pad(value: number): string {
  return value < 10 ? `0${value}` : String(value);
}

/** The header clock: 24-hour local time, e.g. `09:04`. */
export function formatClock(date: Date): string {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * A timestamp for humans: local `YYYY-MM-DD HH:MM`.
 *
 * Three honest answers, never `Invalid Date`: "never reported" when the
 * backend has no timestamp (a configured entity that has not answered yet), and
 * the string verbatim when it does not parse — printing it unedited is the only
 * way to keep the fact that the backend sent something odd.
 */
export function formatStamp(iso: string | null | undefined): string {
  if (iso === null || iso === undefined || iso === '') {
    return 'never reported';
  }
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return iso;
  }
  const day = `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`;
  return `${day} ${formatClock(parsed)}`;
}

/**
 * A concise, locale-free description of when an entity last changed.
 *
 * Missing and malformed values stay honest, and a clock skew over one minute
 * uses an absolute timestamp rather than claiming a negative relative age.
 */
export function formatChangedLine(iso: string | null | undefined, now: number = Date.now()): string {
  if (iso === null || iso === undefined || iso === '') {
    return 'Never reported';
  }

  const changedAt = new Date(iso).getTime();
  if (Number.isNaN(changedAt)) {
    return `Changed at ${iso}`;
  }

  const age = now - changedAt;
  if (age < -60_000) {
    return `Changed at ${formatStamp(iso)}`;
  }
  if (age < 60_000) {
    return 'Changed just now';
  }
  if (age < 60 * 60_000) {
    const minutes = Math.floor(age / 60_000);
    return `Changed ${minutes} ${minutes === 1 ? 'minute' : 'minutes'} ago`;
  }
  if (age < 24 * 60 * 60_000) {
    const hours = Math.floor(age / (60 * 60_000));
    return `Changed ${hours} ${hours === 1 ? 'hour' : 'hours'} ago`;
  }

  const days = Math.floor(age / (24 * 60 * 60_000));
  return `Changed ${days} ${days === 1 ? 'day' : 'days'} ago`;
}
