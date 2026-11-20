/**
 * Last-known-snapshot persistence (HSD-0qtn).
 *
 * A wall panel that reloads with no network should show the tiles it saw a
 * moment ago, clearly marked stale, rather than an empty screen. This module is
 * the whole cache boundary: it reads, validates and writes one `localStorage`
 * key and never trusts what it finds.
 *
 * Why every read is total: `localStorage` holds whatever a previous version
 * wrote, whatever a user pasted, and whatever a quota-evicted half-write left
 * behind. A half-restored cache is a lie about live state, so the reader either
 * returns a fully valid payload or `null` and removes the key. Extra unknown
 * fields are tolerated (a newer writer may add them); a wrong-typed required
 * field is not.
 *
 * Why the writer coalesces: a busy panel can receive `entity_state` frames per
 * second, and serialising the whole snapshot on each one would be wasteful. The
 * first payload is written immediately so a reload straight after the first
 * snapshot has data; later ones collapse into one trailing timer.
 *
 * Everything the environment provides (storage, clock, timer) is injectable, so
 * the node suite drives the whole lifecycle without a browser.
 */

import type { ButtonTile, EntityTile, HealthState } from './api.js';
import type { StorageLike } from './kiosk.js';
import type { Scheduler } from './ws.js';

/** The one `localStorage` key the cache owns. */
export const SNAPSHOT_STORAGE_KEY = 'doorbell.snapshot';

/** Bumped when the stored shape changes; an older payload is discarded. */
export const SNAPSHOT_CACHE_VERSION = 1;

/** A payload above this many serialised characters is not written at all. */
export const SNAPSHOT_MAX_BYTES = 131072;

/** A snapshot older than a day is not worth showing as "last known". */
export const SNAPSHOT_MAX_AGE_MS = 86_400_000;

/** The view shapes the dashboard needs to repaint without a socket. */
export interface CachePayload {
  configVersion: number;
  entities: EntityTile[];
  buttons: ButtonTile[];
  health: HealthState;
}

/** The on-disk envelope: the payload plus the version and write time. */
export interface CachedSnapshot extends CachePayload {
  v: number;
  savedAt: number;
}

/** A clock-skew guard: a `savedAt` this far in the future is corrupt. */
const MAX_CLOCK_SKEW_MS = 60_000;

/** The default trailing-write delay for the coalescing writer. */
const DEFAULT_WRITE_DELAY_MS = 2000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isEntityTile(value: unknown): boolean {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.id === 'number' &&
    typeof value.entityId === 'string' &&
    typeof value.icon === 'string' &&
    typeof value.iconSource === 'string'
  );
}

function isButtonTile(value: unknown): boolean {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.id === 'number' &&
    typeof value.name === 'string' &&
    typeof value.slug === 'string'
  );
}

function isHealthState(value: unknown): boolean {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.ha === 'string' &&
    typeof value.mqtt === 'string' &&
    typeof value.audio === 'string'
  );
}

/** Every rule a stored payload must satisfy before it may be restored. */
function isCachedSnapshot(value: unknown, now: number): value is CachedSnapshot {
  if (!isRecord(value)) {
    return false;
  }
  if (value.v !== SNAPSHOT_CACHE_VERSION) {
    return false;
  }
  if (typeof value.savedAt !== 'number' || !Number.isFinite(value.savedAt)) {
    return false;
  }
  if (value.savedAt > now + MAX_CLOCK_SKEW_MS) {
    return false;
  }
  if (now - value.savedAt > SNAPSHOT_MAX_AGE_MS) {
    return false;
  }
  if (typeof value.configVersion !== 'number' || !Number.isFinite(value.configVersion)) {
    return false;
  }
  if (!Array.isArray(value.entities) || !value.entities.every(isEntityTile)) {
    return false;
  }
  if (!Array.isArray(value.buttons) || !value.buttons.every(isButtonTile)) {
    return false;
  }
  return isHealthState(value.health);
}

/** Best-effort delete; a store that cannot forget must not throw. */
function discard(storage: StorageLike): void {
  try {
    storage.removeItem(SNAPSHOT_STORAGE_KEY);
  } catch {
    // Nothing more can be done; the key is simply not ours to remove.
  }
}

/**
 * Read and fully validate the cached payload. Any failure — no store, no key,
 * bad JSON, wrong version, stale or future timestamp, wrong-typed rows — removes
 * the key where possible and returns `null`. Never partially restores.
 */
export function readSnapshotCache(storage: StorageLike | null, now: number): CachePayload | null {
  if (storage === null) {
    return null;
  }
  let raw: string | null;
  try {
    raw = storage.getItem(SNAPSHOT_STORAGE_KEY);
  } catch {
    return null;
  }
  if (raw === null) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    discard(storage);
    return null;
  }
  if (!isCachedSnapshot(parsed, now)) {
    discard(storage);
    return null;
  }
  return {
    configVersion: parsed.configVersion,
    entities: parsed.entities,
    buttons: parsed.buttons,
    health: parsed.health
  };
}

/**
 * Serialise and store one payload. An oversized payload removes the old key
 * rather than writing a truncated cache; a quota failure is swallowed because
 * the cache is best-effort.
 */
export function writeSnapshotCache(
  storage: StorageLike | null,
  payload: CachePayload,
  now: number
): void {
  if (storage === null) {
    return;
  }
  const cached: CachedSnapshot = {
    v: SNAPSHOT_CACHE_VERSION,
    savedAt: now,
    configVersion: payload.configVersion,
    entities: payload.entities,
    buttons: payload.buttons,
    health: payload.health
  };
  let serialized: string;
  try {
    serialized = JSON.stringify(cached);
  } catch {
    discard(storage);
    return;
  }
  if (serialized.length > SNAPSHOT_MAX_BYTES) {
    discard(storage);
    return;
  }
  try {
    storage.setItem(SNAPSHOT_STORAGE_KEY, serialized);
  } catch {
    // Quota exceeded or private mode: the cache is best-effort.
  }
}

/** Remove the cached snapshot, swallowing a store that cannot forget. */
export function clearSnapshotCache(storage: StorageLike | null): void {
  if (storage === null) {
    return;
  }
  discard(storage);
}

/** The coalescing writer the app holds for the lifetime of a mount. */
export interface CacheWriter {
  queue(payload: CachePayload): void;
  flush(): void;
  stop(): void;
}

export interface CacheWriterOptions {
  /** Timer factory; defaults to `setTimeout`/`clearTimeout`. */
  schedule?: Scheduler;
  /** Clock; defaults to `Date.now`. */
  now?: () => number;
  /** Trailing-write delay; defaults to 2000 ms. */
  delayMs?: number;
}

function defaultSchedule(fn: () => void, ms: number): () => void {
  const timer = setTimeout(fn, ms);
  return () => clearTimeout(timer);
}

/**
 * Build a writer whose first payload is written synchronously and whose later
 * payloads collapse into one trailing write. At most one timer is ever pending.
 */
export function createCacheWriter(
  storage: StorageLike | null,
  options: CacheWriterOptions = {}
): CacheWriter {
  const schedule = options.schedule ?? defaultSchedule;
  const now = options.now ?? (() => Date.now());
  const delayMs = options.delayMs ?? DEFAULT_WRITE_DELAY_MS;
  let wroteFirst = false;
  let pending: CachePayload | null = null;
  let cancelTimer: (() => void) | null = null;
  let stopped = false;

  function cancelPendingTimer(): void {
    const cancel = cancelTimer;
    cancelTimer = null;
    cancel?.();
  }

  function writePending(): void {
    cancelPendingTimer();
    if (pending === null) {
      return;
    }
    writeSnapshotCache(storage, pending, now());
    pending = null;
  }

  return {
    queue(payload: CachePayload): void {
      if (stopped) {
        return;
      }
      if (!wroteFirst) {
        wroteFirst = true;
        writeSnapshotCache(storage, payload, now());
        return;
      }
      pending = payload;
      cancelPendingTimer();
      cancelTimer = schedule(() => {
        cancelTimer = null;
        writePending();
      }, delayMs);
    },
    flush(): void {
      writePending();
    },
    stop(): void {
      stopped = true;
      cancelPendingTimer();
      pending = null;
    }
  };
}
