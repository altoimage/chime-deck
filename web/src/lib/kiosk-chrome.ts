/**
 * The inactivity cursor controller for the wall panel (HSD-0qtn).
 *
 * A kiosk screen is a display, not a workstation: after a few seconds with no
 * input the pointer is hidden so it cannot sit on top of a status tile. Any
 * pointer, key, wheel or touch event brings it straight back and restarts the
 * countdown.
 *
 * Three rules keep it honest:
 *
 *   - exactly one timer is pending at any moment. Activity cancels the old one
 *     and arms a new one; there is never a `setInterval` and never a queue.
 *   - the cursor is never hidden while a sheet or the tile long-press popover
 *     is open (`[role="dialog"]` or a native `dialog[open]`). The timer re-arms
 *     instead, so a reading popover does not vanish mid-read.
 *   - `stop()` removes every listener, cancels the one timer, removes the
 *     `data-cursor` flag and resets `hidden`, so nothing can outlive a mount.
 *
 * Everything the environment provides (`document`, the clock) is injectable, so
 * the node suite drives the whole lifecycle without a browser.
 */

import type { Scheduler } from './ws.js';

/** How long the pointer may rest before it is hidden. */
export const KIOSK_IDLE_MS = 5000;

/** The activity that counts as "the user is here". */
const ACTIVITY_EVENTS: readonly string[] = [
  'pointermove',
  'pointerdown',
  'keydown',
  'wheel',
  'touchstart'
];

/** `<document.documentElement>`-ish: only the `data-cursor` flag is touched. */
export interface KioskDocumentElement {
  dataset: Record<string, string | undefined>;
}

/**
 * `document`-like: the listeners, the flags and the one selector the
 * open-surface check needs. A structural type keeps node tests DOM-free.
 */
export interface KioskDocument {
  documentElement: KioskDocumentElement;
  addEventListener(type: string, listener: () => void, options?: { passive: boolean }): void;
  removeEventListener(type: string, listener: () => void): void;
  querySelector(selector: string): unknown | null;
}

export interface KioskChromeOptions {
  doc: KioskDocument;
  /** Timer factory; defaults to `setTimeout`/`clearTimeout`. */
  schedule?: Scheduler;
  /** Idle timeout; defaults to `KIOSK_IDLE_MS`. */
  idleMs?: number;
}

function defaultSchedule(fn: () => void, ms: number): () => void {
  const timer = setTimeout(fn, ms);
  return () => clearTimeout(timer);
}

/** Hides the cursor after inactivity and shows it again on the next input. */
export class KioskChrome {
  #doc: KioskDocument;
  #schedule: Scheduler;
  #idleMs: number;
  #cancelTimer: (() => void) | null = null;
  #started = false;
  #hidden = false;

  readonly #onActivity = (): void => this.#handleActivity();

  constructor(options: KioskChromeOptions) {
    this.#doc = options.doc;
    this.#schedule = options.schedule ?? defaultSchedule;
    this.#idleMs = options.idleMs ?? KIOSK_IDLE_MS;
  }

  /** True while the cursor flag is set. */
  get hidden(): boolean {
    return this.#hidden;
  }

  /** The configured idle timeout, for tests and diagnostics. */
  get idleMs(): number {
    return this.#idleMs;
  }

  /** Install the activity listeners and arm the one timer. Safe to call twice. */
  start(): void {
    if (this.#started) {
      return;
    }
    this.#started = true;
    for (const type of ACTIVITY_EVENTS) {
      this.#doc.addEventListener(type, this.#onActivity, { passive: true });
    }
    this.#arm();
  }

  /** Remove every listener and the timer, show the cursor and reset the flag. */
  stop(): void {
    this.#started = false;
    this.#cancelPendingTimer();
    for (const type of ACTIVITY_EVENTS) {
      this.#doc.removeEventListener(type, this.#onActivity);
    }
    this.#showCursor();
  }

  #handleActivity(): void {
    this.#showCursor();
    this.#arm();
  }

  /** Replace any pending timer with a fresh one: never more than one at a time. */
  #arm(): void {
    this.#cancelPendingTimer();
    this.#cancelTimer = this.#schedule(() => {
      this.#cancelTimer = null;
      this.#fire();
    }, this.#idleMs);
  }

  #fire(): void {
    if (!this.#started) {
      return;
    }
    if (this.#surfaceOpen()) {
      // A sheet or popover is being read; wait another idle window, do not hide.
      this.#arm();
      return;
    }
    this.#doc.documentElement.dataset.cursor = 'hidden';
    this.#hidden = true;
  }

  #surfaceOpen(): boolean {
    return (
      this.#doc.querySelector('[role="dialog"]') !== null ||
      this.#doc.querySelector('dialog[open]') !== null
    );
  }

  #showCursor(): void {
    delete this.#doc.documentElement.dataset.cursor;
    this.#hidden = false;
  }

  #cancelPendingTimer(): void {
    const cancel = this.#cancelTimer;
    this.#cancelTimer = null;
    cancel?.();
  }
}
