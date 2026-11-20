/**
 * Rune-backed toast store. A class in a `.svelte.ts` module so it is safe to
 * import and call from plain TS modules — no component context required:
 *
 *   import { toastStore } from '../lib/ui/toast.svelte.js';
 *   toastStore.push('success', 'Saved.');
 *
 * Auto-dismiss timers live in a module-level Map and are always cleared on
 * dismiss() / clear(), so no timer can outlive its toast.
 */

export type ToastKind = 'info' | 'success' | 'error';

export interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

const DEFAULT_TIMEOUT_MS = 5000;

let nextId = 0;
const timers = new Map<number, ReturnType<typeof setTimeout>>();

function clearTimer(id: number): void {
  const timer = timers.get(id);
  if (timer !== undefined) {
    clearTimeout(timer);
    timers.delete(id);
  }
}

class ToastStore {
  #toasts = $state<ToastItem[]>([]);

  get items(): ToastItem[] {
    return this.#toasts;
  }

  /** Push a toast; returns its id. `timeoutMs <= 0` disables auto-dismiss. */
  push(kind: ToastKind, message: string, timeoutMs = DEFAULT_TIMEOUT_MS): number {
    nextId += 1;
    const id = nextId;
    this.#toasts.push({ id, kind, message });
    if (timeoutMs > 0) {
      timers.set(
        id,
        setTimeout(() => this.dismiss(id), timeoutMs)
      );
    }
    return id;
  }

  dismiss(id: number): void {
    clearTimer(id);
    this.#toasts = this.#toasts.filter((toast) => toast.id !== id);
  }

  /** Clear every toast and every pending timer (host unmount, teardown). */
  clear(): void {
    for (const id of timers.keys()) {
      clearTimer(id);
    }
    this.#toasts = [];
  }
}

export const toastStore = new ToastStore();
