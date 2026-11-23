/**
 * The UI scale state (HSD-nlbf). A rune-backed class in a `.svelte.ts` module,
 * like `theme.svelte.ts`, so it can be imported from plain TS modules as well
 * as components:
 *
 *   import { scale } from '../lib/scale.svelte.js';
 *
 * The scale is one multiplier for the whole interface, applied as a single root
 * `zoom` on <html> and nowhere else: every px in the app (tokens, the hardcoded
 * chrome, the D30 tile floors) moves by exactly one factor. The step is written
 * to <html data-ui-scale> and `tokens.css` turns it into `--ui-scale`; nothing
 * else may multiply by that factor — a second one is the double-scaling defect
 * this setting exists to avoid.
 *
 * Two sources name the scale and they are not equal: the registry's `ui.scale`
 * is the installation's setting and wins once settings land (`adopt`), while
 * localStorage is only the pre-paint cache that stops a flash of unscaled
 * content before the first `GET` resolves — the same rule as the theme. That
 * cache is read twice: here, and by the inline script in `index.html`.
 */

import { untrack } from 'svelte';

import { DEFAULT_SCALE, type ScaleStep, readScale } from './scale.js';

const STORAGE_KEY = 'doorbell.scale';

function readStoredStep(): ScaleStep {
  try {
    return readScale(Number(localStorage.getItem(STORAGE_KEY)));
  } catch {
    // A denied/throwing localStorage must never break the app.
  }
  return DEFAULT_SCALE;
}

function persistStep(step: ScaleStep): void {
  try {
    localStorage.setItem(STORAGE_KEY, String(step));
  } catch {
    // Non-fatal: the scale still applies for this session.
  }
}

class Scale {
  #step = $state<ScaleStep>(readStoredStep());

  /** The chosen step, as the registry's percent integer. */
  get step(): ScaleStep {
    return this.#step;
  }

  /** Change the step: persist it, then apply it to <html data-ui-scale>. */
  set(step: ScaleStep): void {
    this.#step = step;
    persistStep(step);
    this.apply();
  }

  /**
   * Adopt the installation's scale (`ui.scale`) as the local one.
   *
   * An off-step wire value degrades to the default rather than leaving an
   * unknown size on the panel. Writes only on a real change — and reads the
   * current step untracked, so an `$effect` that adopts cannot subscribe to the
   * value it writes and re-run forever.
   */
  adopt(value: unknown): void {
    const step = readScale(value);
    if (untrack(() => this.#step) === step) {
      return;
    }
    this.set(step);
  }

  /**
   * Write the step to <html data-ui-scale>.
   *
   * The attribute is removed at 100% rather than set to '100': the default
   * needs no override, so the base rendering path stays exactly as it was
   * before this setting existed. A no-op without a document: the
   * node-environment tests load the settings store.
   */
  apply(): void {
    if (typeof document === 'undefined') {
      return;
    }
    if (this.#step === DEFAULT_SCALE) {
      delete document.documentElement.dataset.uiScale;
      return;
    }
    document.documentElement.dataset.uiScale = String(this.#step);
  }
}

export const scale = new Scale();
