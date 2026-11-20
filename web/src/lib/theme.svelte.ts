/**
 * Theme state. A rune-backed class in a `.svelte.ts` module so it can be
 * imported from plain TS modules as well as components:
 *
 *   import { theme } from '../lib/theme.svelte.js';
 *
 * The choice is persisted in localStorage; 'auto' follows the OS colour
 * scheme through a live media-query listener. The listener's teardown is
 * returned by subscribeSystem() — the consumer's `$effect` must return it
 * so the listener is always removed.
 *
 * Two sources name the theme and they are not equal: the registry's `ui.theme`
 * is the installation's setting and wins once settings land (`adopt`), while
 * localStorage is only the pre-load cache that stops a white flash before the
 * first `GET` resolves.
 */

import { untrack } from 'svelte';

export type ThemeChoice = 'dark' | 'light' | 'auto';

/** Whether *value* is one of the three choices, e.g. a value off the wire. */
export function isThemeChoice(value: unknown): value is ThemeChoice {
  return value === 'dark' || value === 'light' || value === 'auto';
}

type ResolvedTheme = 'dark' | 'light';

const STORAGE_KEY = 'doorbell.theme';
const LIGHT_QUERY = '(prefers-color-scheme: light)';
const DEFAULT_CHOICE: ThemeChoice = 'dark';

function readStoredChoice(): ThemeChoice {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (isThemeChoice(stored)) {
      return stored;
    }
  } catch {
    // A denied/throwing localStorage must never break the app.
  }
  return DEFAULT_CHOICE;
}

function persistChoice(choice: ThemeChoice): void {
  try {
    localStorage.setItem(STORAGE_KEY, choice);
  } catch {
    // Non-fatal: the theme still applies for this session.
  }
}

class Theme {
  #choice = $state<ThemeChoice>(readStoredChoice());
  #media: MediaQueryList | null = null;

  constructor() {
    if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
      this.#media = window.matchMedia(LIGHT_QUERY);
    }
  }

  /** The stored/user choice. */
  get choice(): ThemeChoice {
    return this.#choice;
  }

  /** The effective palette, with 'auto' resolved against the OS scheme. */
  get resolved(): ResolvedTheme {
    if (this.#choice === 'auto') {
      return this.#media?.matches ? 'light' : 'dark';
    }
    return this.#choice;
  }

  /** Change the choice: persist it, then apply it to <html data-theme>. */
  set(choice: ThemeChoice): void {
    this.#choice = choice;
    persistChoice(choice);
    this.apply();
  }

  /**
   * Adopt the installation's choice (`ui.theme`) as the local one.
   *
   * Anything that is not a choice is ignored: a malformed wire value must leave
   * the theme alone, not blank it. Writes only on a real change — and reads the
   * current choice untracked, so an `$effect` that adopts cannot subscribe to
   * the value it writes and re-run forever.
   */
  adopt(choice: unknown): void {
    if (!isThemeChoice(choice)) {
      return;
    }
    if (untrack(() => this.#choice) === choice) {
      return;
    }
    this.set(choice);
  }

  /**
   * Write the resolved theme to <html data-theme>.
   *
   * A no-op without a document: the node-environment tests load the settings
   * store, and the constructor already treats `window` as optional for the same
   * reason.
   */
  apply(): void {
    if (typeof document === 'undefined') {
      return;
    }
    document.documentElement.dataset.theme = this.resolved;
  }

  /**
   * Follow OS colour-scheme changes so 'auto' stays live. Returns the
   * teardown that removes the listener — the consumer's `$effect` must
   * return it; a listener that is never removed is a bug.
   */
  subscribeSystem(): () => void {
    const media = this.#media;
    if (!media) {
      return () => {};
    }
    const onChange = (): void => {
      if (this.#choice === 'auto') {
        this.apply();
      }
    };
    media.addEventListener('change', onChange);
    return () => {
      media.removeEventListener('change', onChange);
    };
  }
}

export const theme = new Theme();
