import { describe, expect, it } from 'vitest';

import type { EntityTile } from './api.js';
import type { StorageLike } from './kiosk.js';
import {
  clearSnapshotCache,
  createCacheWriter,
  readSnapshotCache,
  SNAPSHOT_MAX_AGE_MS,
  SNAPSHOT_STORAGE_KEY,
  SNAPSHOT_CACHE_VERSION,
  writeSnapshotCache,
  type CachePayload,
  type CachedSnapshot
} from './snapshot-cache.js';
import type { Scheduler } from './ws.js';

/* ---- fixtures ---------------------------------------------------------- */

function tile(overrides: Partial<EntityTile> = {}): EntityTile {
  return {
    id: 1,
    entityId: 'lock.front_door',
    displayName: 'Front Door',
    state: 'locked',
    attributes: {},
    icon: 'mdi:door',
    iconSource: 'user',
    accentColor: null,
    lastChanged: null,
    displayedState: 'Locked',
    unitOfMeasurement: null,
    attributeHints: [],
    tone: 'ok',
    hideWhenUnavailable: false,
    selected: false,
    ...overrides
  };
}

function payload(overrides: Partial<CachePayload> = {}): CachePayload {
  return {
    configVersion: 7,
    entities: [tile()],
    buttons: [{ id: 1, name: 'Front Door Chime', slug: 'front-door', showOnDashboard: true }],
    health: { ha: 'connected', mqtt: 'connected', audio: 'ready', detail: null },
    ...overrides
  };
}

interface FakeStorage {
  storage: StorageLike;
  data: Record<string, string>;
  throwOn: Set<'getItem' | 'setItem' | 'removeItem'>;
}

function makeStorage(initial: Record<string, string> = {}): FakeStorage {
  const data: Record<string, string> = { ...initial };
  const throwOn = new Set<'getItem' | 'setItem' | 'removeItem'>();
  const storage: StorageLike = {
    getItem(key: string): string | null {
      if (throwOn.has('getItem')) {
        throw new Error('storage denied');
      }
      return key in data ? data[key] : null;
    },
    setItem(key: string, value: string): void {
      if (throwOn.has('setItem')) {
        throw new Error('storage denied');
      }
      data[key] = value;
    },
    removeItem(key: string): void {
      if (throwOn.has('removeItem')) {
        throw new Error('storage denied');
      }
      delete data[key];
    }
  };
  return { storage, data, throwOn };
}

interface FakeTimer {
  fn: () => void;
  active: boolean;
}

function makeScheduler(): {
  schedule: Scheduler;
  pending: () => FakeTimer[];
  fire: () => void;
} {
  const timers: FakeTimer[] = [];
  const schedule: Scheduler = (fn) => {
    const timer: FakeTimer = { fn, active: true };
    timers.push(timer);
    return () => {
      timer.active = false;
    };
  };
  return {
    schedule,
    pending: () => timers.filter((timer) => timer.active),
    fire: () => {
      const next = timers.find((timer) => timer.active);
      if (next === undefined) {
        throw new Error('no pending timer to fire');
      }
      next.active = false;
      next.fn();
    }
  };
}

function parsed(data: Record<string, string>): CachedSnapshot {
  return JSON.parse(data[SNAPSHOT_STORAGE_KEY]) as CachedSnapshot;
}

/* ---- reader / writer --------------------------------------------------- */

describe('snapshot cache round trip', () => {
  it('writes a versioned envelope and reads the payload back', () => {
    const { storage, data } = makeStorage();
    writeSnapshotCache(storage, payload(), 1_000);

    const envelope = parsed(data);
    expect(envelope.v).toBe(SNAPSHOT_CACHE_VERSION);
    expect(envelope.savedAt).toBe(1_000);

    expect(readSnapshotCache(storage, 1_500)).toEqual(payload());
  });

  it('rejects and removes a payload past its maximum age', () => {
    const { storage, data } = makeStorage();
    writeSnapshotCache(storage, payload(), 0);

    expect(readSnapshotCache(storage, SNAPSHOT_MAX_AGE_MS + 1)).toBeNull();
    expect(data[SNAPSHOT_STORAGE_KEY]).toBeUndefined();
  });

  it('rejects and removes a payload from a future clock', () => {
    const { storage, data } = makeStorage();
    writeSnapshotCache(storage, payload(), 10_000_000);

    expect(readSnapshotCache(storage, 0)).toBeNull();
    expect(data[SNAPSHOT_STORAGE_KEY]).toBeUndefined();
  });

  it('rejects and removes a version mismatch', () => {
    const { storage, data } = makeStorage({
      [SNAPSHOT_STORAGE_KEY]: JSON.stringify({ ...payload(), v: 99, savedAt: 0 })
    });

    expect(readSnapshotCache(storage, 0)).toBeNull();
    expect(data[SNAPSHOT_STORAGE_KEY]).toBeUndefined();
  });

  it('rejects and removes malformed JSON', () => {
    const { storage, data } = makeStorage({ [SNAPSHOT_STORAGE_KEY]: '{not json' });

    expect(readSnapshotCache(storage, 0)).toBeNull();
    expect(data[SNAPSHOT_STORAGE_KEY]).toBeUndefined();
  });

  it('tolerates extra unknown fields at the top level and on a row', () => {
    const { storage, data } = makeStorage({
      [SNAPSHOT_STORAGE_KEY]: JSON.stringify({
        ...payload(),
        v: SNAPSHOT_CACHE_VERSION,
        savedAt: 0,
        extraTop: true,
        entities: [{ ...tile(), extra: 'x' }]
      })
    });

    const restored = readSnapshotCache(storage, 0);
    expect(restored).not.toBeNull();
    expect(restored?.configVersion).toBe(7);
    expect((restored?.entities[0] as unknown as { extra?: string }).extra).toBe('x');
    expect(data[SNAPSHOT_STORAGE_KEY]).toBeDefined();
  });

  it('rejects wrong-typed rows and removes the key', () => {
    const cases: unknown[] = [
      { ...payload(), entities: [{ id: 1, entityId: 'x', iconSource: 'user' }] },
      { ...payload(), entities: [{ ...tile(), id: 'one' }] },
      { ...payload(), buttons: [{ id: 1, name: 'Chime' }] },
      { ...payload(), health: { ha: 'connected', mqtt: 'connected' } },
      { ...payload(), configVersion: 'seven' }
    ];

    for (const bad of cases) {
      const { storage, data } = makeStorage({
        [SNAPSHOT_STORAGE_KEY]: JSON.stringify({
          ...(bad as Record<string, unknown>),
          v: SNAPSHOT_CACHE_VERSION,
          savedAt: 0
        })
      });

      expect(readSnapshotCache(storage, 0)).toBeNull();
      expect(data[SNAPSHOT_STORAGE_KEY]).toBeUndefined();
    }
  });

  it('does not write an oversized payload and removes the stale key', () => {
    const { storage, data } = makeStorage();
    writeSnapshotCache(storage, payload(), 0);
    expect(data[SNAPSHOT_STORAGE_KEY]).toBeDefined();

    const huge = payload({
      entities: [tile({ displayName: 'x'.repeat(200_000) })]
    });
    writeSnapshotCache(storage, huge, 1);

    expect(data[SNAPSHOT_STORAGE_KEY]).toBeUndefined();
  });

  it('swallows every storage failure', () => {
    const { storage, throwOn } = makeStorage();
    throwOn.add('getItem');
    throwOn.add('setItem');
    throwOn.add('removeItem');

    expect(() => readSnapshotCache(storage, 0)).not.toThrow();
    expect(() => writeSnapshotCache(storage, payload(), 0)).not.toThrow();
    expect(() => clearSnapshotCache(storage)).not.toThrow();
    expect(readSnapshotCache(storage, 0)).toBeNull();
  });

  it('is inert with a null store', () => {
    expect(readSnapshotCache(null, 0)).toBeNull();
    expect(() => writeSnapshotCache(null, payload(), 0)).not.toThrow();
    expect(() => clearSnapshotCache(null)).not.toThrow();
  });
});

/* ---- coalescing writer ------------------------------------------------- */

describe('createCacheWriter', () => {
  it('writes the first payload synchronously', () => {
    const { storage, data } = makeStorage();
    const scheduler = makeScheduler();
    const writer = createCacheWriter(storage, { schedule: scheduler.schedule, now: () => 1_000 });

    writer.queue(payload());

    expect(scheduler.pending()).toHaveLength(0);
    expect(parsed(data).savedAt).toBe(1_000);
    expect(parsed(data).configVersion).toBe(7);
    writer.stop();
  });

  it('coalesces later payloads into one trailing write of the latest', () => {
    const { storage, data } = makeStorage();
    const scheduler = makeScheduler();
    let clock = 1_000;
    const writer = createCacheWriter(storage, { schedule: scheduler.schedule, now: () => clock });

    writer.queue(payload({ configVersion: 1 }));
    writer.queue(payload({ configVersion: 2 }));
    writer.queue(payload({ configVersion: 3 }));
    expect(scheduler.pending()).toHaveLength(1);

    clock = 3_000;
    scheduler.fire();

    expect(parsed(data).configVersion).toBe(3);
    expect(parsed(data).savedAt).toBe(3_000);
    expect(scheduler.pending()).toHaveLength(0);
    writer.stop();
  });

  it('flush writes the pending payload immediately and cancels the timer', () => {
    const { storage, data } = makeStorage();
    const scheduler = makeScheduler();
    let clock = 1_000;
    const writer = createCacheWriter(storage, { schedule: scheduler.schedule, now: () => clock });

    writer.queue(payload({ configVersion: 1 }));
    writer.queue(payload({ configVersion: 2 }));
    clock = 2_000;
    writer.flush();

    expect(scheduler.pending()).toHaveLength(0);
    expect(parsed(data).configVersion).toBe(2);
    expect(parsed(data).savedAt).toBe(2_000);
    writer.stop();
  });

  it('stop cancels the timer and writes nothing further', () => {
    const { storage, data } = makeStorage();
    const scheduler = makeScheduler();
    const writer = createCacheWriter(storage, { schedule: scheduler.schedule, now: () => 1_000 });

    writer.queue(payload({ configVersion: 1 }));
    const firstWrite = data[SNAPSHOT_STORAGE_KEY];
    writer.queue(payload({ configVersion: 2 }));
    writer.stop();

    expect(scheduler.pending()).toHaveLength(0);
    expect(data[SNAPSHOT_STORAGE_KEY]).toBe(firstWrite);

    // A queue after stop is ignored.
    writer.queue(payload({ configVersion: 3 }));
    expect(data[SNAPSHOT_STORAGE_KEY]).toBe(firstWrite);
  });
});
