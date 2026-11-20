/**
 * The settings store: the dashboard's slice plus the generic readers and the
 * write path every settings panel shares (HSD-bq76).
 *
 * The dashboard names only the keys it reads (the clock, the kiosk cursor
 * switch, the control rail, the column count, the volume, the mute state); the
 * settings screens read the rest through `text`/`integer`/`flag`, so this file
 * never grows into a second copy of the registry. A rune-backed class in a
 * `.svelte.ts` module, like `theme.svelte.ts`, so a component can `$derived`
 * off it.
 *
 * Four rules keep it honest:
 *   - every read type-checks the wire value. `settings` is
 *     `Record<string, unknown>`, so a string where a boolean belongs must fall
 *     back to the documented default rather than render `undefined` on a wall
 *     panel.
 *   - a failed load or patch sets `error` and keeps the last good values. The
 *     error is rendered, never swallowed: a volume that silently did not save
 *     is a bug report waiting to happen.
 *   - a secret's value never comes back from the server, so the store keeps
 *     only the `secrets` flags (`hasSecret`). A panel can say "stored" without
 *     ever holding the value, and can never echo one it did not receive.
 *   - the echo is the source of truth, so the two keys that own a DOM effect are
 *     adopted from it — never from a draft. `#apply` hands `ui.theme` to its
 *     owner and writes `device.name` into the browser title, which is why a
 *     failed PATCH cannot leave the page themed with a theme the server refused,
 *     or titled with a name it would not store. Both are adopted in `#apply`
 *     rather than at a call site because that is the ONE echo path: every root
 *     (the dashboard, `/settings` and `/setup`) reaches it through `load()`.
 */

import { type SettingsResponse, ApiError, getSettings, patchSettings } from './api.js';
import { theme } from './theme.svelte.js';
import { DEFAULT_VOLUME, MAX_VOLUME, MIN_VOLUME, clampVolume } from './volume.js';

/** `ui.show_clock` default (settings_registry.py). */
const DEFAULT_SHOW_CLOCK = true;

/**
 * `ui.show_control_rail` default (D28): the volume steps are gated, the mute
 * button and the Settings link are part of the shell and never are. Defaulting
 * on keeps the rail working on an install that has never saved this key.
 */
const DEFAULT_SHOW_CONTROL_RAIL = true;

/** `ui.hide_cursor` default (settings_registry.py): kiosk hides the pointer. */
const DEFAULT_HIDE_CURSOR = true;

/** `ui.dashboard_columns` default; 0 means auto-fit (settings_registry.py). */
const DEFAULT_COLUMNS = 0;

/** `ui.dashboard_columns` range the backend validates (settings_registry.py). */
const MIN_COLUMNS = 0;
const MAX_COLUMNS = 6;

/** `device.name` default (settings_registry.py): the browser title before a name is saved. */
const DEFAULT_DEVICE_NAME = 'ChimeDeck';

/** A boolean from the wire, or the fallback when the value is some other type. */
function readBoolean(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

/** An integer inside the range the backend validates, or the fallback. */
function readInteger(value: unknown, minimum: number, maximum: number, fallback: number): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < minimum || value > maximum) {
    return fallback;
  }
  return value;
}

/**
 * The text to render for a failed call: the server's message verbatim (design
 * 05 §3 says it is written to be read by a human), or `fallback` for anything
 * that is not an `ApiError`.
 */
function describeError(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? cause.message : fallback;
}

/** The settings state the dashboard reads, plus the generic readers the
 * settings screens use. */
class Settings {
  #settings = $state<Record<string, unknown>>({});
  #secrets = $state<Record<string, boolean>>({});
  #loaded = $state(false);
  #error = $state<string | null>(null);

  /** True once a load has succeeded at least once. */
  get loaded(): boolean {
    return this.#loaded;
  }

  /** The `ApiError` message when the last load/patch failed, else null. */
  get error(): string | null {
    return this.#error;
  }

  /**
   * Whether *key* holds a stored secret. True only for the flag exactly `true`:
   * a missing or malformed flag reads as "not stored", which is the safe
   * direction (the UI then offers the field instead of claiming a value).
   */
  hasSecret(key: string): boolean {
    return this.#secrets[key] === true;
  }

  /**
   * Read a string setting, or `fallback` when the wire value is not a string.
   * The generic readers keep this file from growing into a second registry:
   * a panel names its keys and its documented defaults locally.
   */
  text(key: string, fallback = ''): string {
    const value = this.#settings[key];
    return typeof value === 'string' ? value : fallback;
  }

  /** Read an integer setting inside the backend's range, or `fallback`. */
  integer(key: string, minimum: number, maximum: number, fallback: number): number {
    return readInteger(this.#settings[key], minimum, maximum, fallback);
  }

  /** Read a boolean setting, or `fallback` when the wire value is not a boolean. */
  flag(key: string, fallback: boolean): boolean {
    return readBoolean(this.#settings[key], fallback);
  }

  /** Whether the header clock is shown; defaults to on. */
  get showClock(): boolean {
    return this.flag('ui.show_clock', DEFAULT_SHOW_CLOCK);
  }

  /**
   * Whether kiosk hides the pointer after inactivity (`ui.hide_cursor`).
   *
   * A wall panel that always shows the pointer is a supported choice, so this is
   * a setting rather than a kiosk constant; the default is the registry's.
   */
  get hideCursor(): boolean {
    return this.flag('ui.hide_cursor', DEFAULT_HIDE_CURSOR);
  }

  /** Whether the rail's volume steps are shown; defaults to on (see the constant). */
  get showControlRail(): boolean {
    return this.flag('ui.show_control_rail', DEFAULT_SHOW_CONTROL_RAIL);
  }

  /** Dashboard columns, 1..6, or 0 for auto-fit. */
  get dashboardColumns(): number {
    return this.integer('ui.dashboard_columns', MIN_COLUMNS, MAX_COLUMNS, DEFAULT_COLUMNS);
  }

  /** Master volume, 0..100; the registry default (70) until a load says otherwise. */
  get volume(): number {
    return this.integer('audio.volume', MIN_VOLUME, MAX_VOLUME, DEFAULT_VOLUME);
  }

  /**
   * The device-wide "silence this device" state, `audio.muted` (HSD-1lud, D28).
   *
   * Not the zero-gain predicate: `isMuted(volume)` is a separate reading, and the
   * header badge combines the two. This one is the deliberate action that
   * outranks quiet hours and the exempt rule, so `true` here means every chime
   * is suppressed no matter what the volume says.
   */
  get muted(): boolean {
    return this.flag('audio.muted', false);
  }

  /**
   * Fetch the effective settings. A failure keeps the defaults, records the
   * reason and returns normally: the dashboard must still render, and a
   * settings panel with no data is not a reason to crash the panel.
   */
  async load(): Promise<void> {
    try {
      this.#apply(await getSettings());
    } catch (cause) {
      this.#error = describeError(cause, 'the settings could not be loaded');
    }
  }

  /**
   * Apply one partial patch and adopt the echoed snapshot.
   *
   * Exactly one PATCH per call; the echo is the source of truth, so the saved
   * state can never drift from the server's. Secrets are write-only: a caller
   * adds a key with its new value to replace a secret, or with `null` to clear
   * it — never `""`, which the backend rejects.
   *
   * @returns false when the patch failed — the caller MUST show `error` (the
   *   server's sentence, verbatim), since the last good values are still in
   *   place and nothing on screen was saved.
   */
  async save(patch: Record<string, unknown>): Promise<boolean> {
    return this.#patch(patch, 'the settings could not be saved');
  }

  /**
   * Save the master volume and adopt the echoed value. Delegates to `#patch` so
   * there is exactly one PATCH path; only the fallback sentence differs.
   */
  async setVolume(value: number): Promise<boolean> {
    return this.#patch({ 'audio.volume': clampVolume(value) }, 'the volume could not be saved');
  }

  /**
   * Turn the device-wide mute on or off and adopt the echoed snapshot (D28).
   * Same single PATCH path as `setVolume`: the echo is the source of truth, so a
   * badge can never claim a mute the server did not store.
   */
  async setMuted(muted: boolean): Promise<boolean> {
    return this.#patch({ 'audio.muted': muted }, 'the mute state could not be saved');
  }

  /** Return to the defaults (unmount, tests). */
  reset(): void {
    this.#settings = {};
    this.#secrets = {};
    this.#loaded = false;
    this.#error = null;
  }

  async #patch(patch: Record<string, unknown>, fallback: string): Promise<boolean> {
    try {
      this.#apply(await patchSettings(patch));
      return true;
    } catch (cause) {
      this.#error = describeError(cause, fallback);
      return false;
    }
  }

  /**
   * Adopt a response; success clears any previous error.
   *
   * This is the ONE echo path (`load()` and every PATCH), so the keys that own a
   * DOM effect are handed to their owners here and nowhere else: `ui.theme` goes
   * to the Theme object, and `device.name` becomes the browser title. The accent
   * is not one of them: it is stored and read by the Appearance panel only. A
   * theme selects a whole palette and so cannot break a colour pair, while an
   * accent override would.
   *
   * The title is the same bargain as the theme: the server refused a name it did
   * not store, so the page must not keep showing it. `index.html`'s static title
   * is the pre-JS one and stays.
   */
  #apply(response: SettingsResponse): void {
    this.#settings = response.settings;
    this.#secrets = response.secrets ?? {};
    this.#loaded = true;
    this.#error = null;
    theme.adopt(this.text('ui.theme'));
    if (typeof document !== 'undefined') {
      document.title = this.text('device.name', DEFAULT_DEVICE_NAME);
    }
  }
}

export const settings = new Settings();
