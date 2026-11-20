/**
 * `?kiosk=1` — the wall-panel mode flag (design 05 §7, D02).
 *
 * Boundary: this parses the flag so a screen can hide its non-essential chrome,
 * remembers the last explicit choice for a session, and stamps the layout
 * flags on `<html>`. The timers and DOM listeners that actually hide the cursor
 * after inactivity live in `kiosk-chrome.ts`, so nothing here owns a timer.
 *
 * A first visit may carry `?kiosk=1`; a later in-app navigation drops the query
 * string, so the session's remembered value is what keeps the panel in kiosk
 * mode without re-typing the URL.
 */

/** The values that mean "on"; everything else means off. */
const TRUTHY: ReadonlySet<string> = new Set(['1', 'true', 'yes', 'on']);

/** Session-storage key for the last explicit kiosk choice. */
export const KIOSK_STORAGE_KEY = 'doorbell.kiosk';

/** The storage members this module uses, so node tests need no DOM. */
export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** The `<html>` element-ish: only the data flags are touched. */
export interface DocumentLike {
  dataset: Record<string, string | undefined>;
}

/**
 * `true`/`false` for an explicit `kiosk` param, `null` when the URL does not
 * name it at all. Garbage is "absent": a hand-typed URL must never throw
 * before the first paint, and an unrecognised value (`?kiosk=maybe`) is off.
 */
function readKioskParam(search: string): boolean | null {
  let params: URLSearchParams;
  try {
    params = new URLSearchParams(search);
  } catch {
    return null;
  }
  const value = params.get('kiosk');
  if (value === null) {
    return null;
  }
  return TRUTHY.has(value.trim().toLowerCase());
}

/** Whether a query string asks for kiosk mode. */
export function parseKiosk(search: string): boolean {
  return readKioskParam(search) === true;
}

/**
 * Resolve kiosk mode and remember an explicit URL choice for the session.
 *
 * An explicit `kiosk` param always wins: `?kiosk=1` stores `'1'`, `?kiosk=0`
 * clears the key. When the param is absent the remembered value is the answer.
 * Every storage access is guarded: a denied or full store (private mode,
 * quota) simply means "URL only" and never throws.
 */
export function readKiosk(search: string, storage: StorageLike | null): boolean {
  const asked = readKioskParam(search);
  if (asked !== null) {
    if (asked) {
      try {
        storage?.setItem(KIOSK_STORAGE_KEY, '1');
      } catch {
        // Private mode or quota: kiosk still applies for this page load.
      }
    } else {
      try {
        storage?.removeItem(KIOSK_STORAGE_KEY);
      } catch {
        // A store that cannot forget is still allowed to fail silently.
      }
    }
    return asked;
  }
  try {
    return storage?.getItem(KIOSK_STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

/** The same question asked of the current page, `false` outside a browser. */
export function isKiosk(): boolean {
  if (typeof window === 'undefined') {
    return false;
  }
  let storage: StorageLike | null;
  try {
    storage = window.sessionStorage;
  } catch {
    storage = null;
  }
  return readKiosk(window.location.search, storage);
}

/**
 * Stamp the layout flags on `<html>` for the global stylesheet to select on.
 *
 * Only `data-layout` and `data-kiosk` are touched; every other attribute (the
 * theme, for one) belongs to someone else.
 */
export function applyKioskFlags(
  doc: DocumentLike,
  flags: { kiosk: boolean; dashboard: boolean }
): void {
  doc.dataset.layout = flags.dashboard ? 'dashboard' : 'page';
  if (flags.kiosk) {
    doc.dataset.kiosk = 'true';
  } else {
    delete doc.dataset.kiosk;
  }
}
