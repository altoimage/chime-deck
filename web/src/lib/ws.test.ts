import { beforeEach, describe, expect, it } from 'vitest';

import { SNAPSHOT_FRAME, WIRE_FRAMES } from './fixtures/wire.js';
import { liveState } from './stores.svelte.js';
import {
  WsClient,
  wsUrl,
  type NetLike,
  type SocketEvent,
  type SocketLike,
  type SocketListener,
  type ViewLike
} from './ws.js';

class FakeSocket implements SocketLike {
  readyState = 0;
  closed = false;
  readonly added: string[] = [];
  readonly removed: string[] = [];
  #listeners = new Map<string, Set<SocketListener>>();

  addEventListener(type: string, listener: SocketListener): void {
    this.added.push(type);
    const set = this.#listeners.get(type) ?? new Set<SocketListener>();
    set.add(listener);
    this.#listeners.set(type, set);
  }

  removeEventListener(type: string, listener: SocketListener): void {
    this.removed.push(type);
    this.#listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.closed = true;
    this.readyState = 3;
  }

  emit(type: string, event: SocketEvent = {}): void {
    for (const listener of [...(this.#listeners.get(type) ?? [])]) {
      listener(event);
    }
  }

  open(): void {
    this.readyState = 1;
    this.emit('open');
  }

  send(frame: unknown): void {
    this.emit('message', { data: JSON.stringify(frame) });
  }
}

class FakeEventTarget {
  readonly added: string[] = [];
  readonly removed: string[] = [];
  #listeners = new Map<string, Set<() => void>>();

  addEventListener(type: string, listener: () => void): void {
    this.added.push(type);
    const set = this.#listeners.get(type) ?? new Set<() => void>();
    set.add(listener);
    this.#listeners.set(type, set);
  }

  removeEventListener(type: string, listener: () => void): void {
    this.removed.push(type);
    this.#listeners.get(type)?.delete(listener);
  }

  emit(type: string): void {
    for (const listener of [...(this.#listeners.get(type) ?? [])]) {
      listener();
    }
  }
}

class FakeView extends FakeEventTarget implements ViewLike {
  visibilityState = 'visible';
}

class FakeNet extends FakeEventTarget implements NetLike {}

/** A scheduler the test runs by hand, so the backoff is observed, not awaited. */
class ManualScheduler {
  readonly delays: number[] = [];
  #timers = new Map<number, { fn: () => void; ms: number; cancelled: boolean }>();
  #nextId = 0;

  readonly schedule = (fn: () => void, ms: number): (() => void) => {
    this.#nextId += 1;
    const id = this.#nextId;
    this.#timers.set(id, { fn, ms, cancelled: false });
    this.delays.push(ms);
    return () => {
      const timer = this.#timers.get(id);
      if (timer !== undefined) {
        timer.cancelled = true;
      }
    };
  };

  get pending(): Array<{ fn: () => void; ms: number }> {
    return [...this.#timers.values()].filter((timer) => !timer.cancelled);
  }

  runNext(): void {
    const entry = [...this.#timers.entries()].find(([, timer]) => !timer.cancelled);
    if (entry === undefined) {
      throw new Error('there is no pending timer to run');
    }
    this.#timers.delete(entry[0]);
    entry[1].fn();
  }
}

interface Harness {
  client: WsClient;
  sockets: FakeSocket[];
  scheduler: ManualScheduler;
  view: FakeView;
  net: FakeNet;
  requestedUrls: string[];
}

function harness(baseDelayMs = 1000): Harness {
  const sockets: FakeSocket[] = [];
  const requestedUrls: string[] = [];
  const scheduler = new ManualScheduler();
  const view = new FakeView();
  const net = new FakeNet();
  const client = new WsClient(liveState, {
    createSocket: (url) => {
      requestedUrls.push(url);
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    },
    url: () => 'ws://doorbell.test/ws',
    schedule: scheduler.schedule,
    baseDelayMs,
    maxDelayMs: 15000,
    view,
    net
  });
  return { client, sockets, scheduler, view, net, requestedUrls };
}

beforeEach(() => {
  liveState.reset();
  liveState.setConfigRefreshHandler(null);
});

describe('url', () => {
  it('is root-absolute /ws on the page origin and follows https', () => {
    expect(wsUrl({ protocol: 'http:', host: 'doorbell.local:8080' })).toBe(
      'ws://doorbell.local:8080/ws'
    );
    expect(wsUrl({ protocol: 'https:', host: 'doorbell.local' })).toBe('wss://doorbell.local/ws');
    expect(() => wsUrl(null)).toThrow();
  });

  it('asks the resolver each time it connects', () => {
    const { client, requestedUrls } = harness();
    client.start();
    expect(requestedUrls).toEqual(['ws://doorbell.test/ws']);
    client.stop();
  });
});

describe('frames', () => {
  it('clears staleness on the snapshot, not on open', () => {
    const { client, sockets } = harness();
    client.start();
    sockets[0].open();
    expect(liveState.connection.status).toBe('open');
    expect(liveState.stale).toBe(true);

    sockets[0].send(SNAPSHOT_FRAME);

    expect(liveState.stale).toBe(false);
    expect(liveState.hasSnapshot).toBe(true);
    expect(liveState.entities).toHaveLength(2);
    expect(client.nextDelayMs).toBe(1000);
    client.stop();
  });

  it('goes stale synchronously when the socket closes', () => {
    const { client, sockets } = harness();
    client.start();
    sockets[0].open();
    sockets[0].send(SNAPSHOT_FRAME);

    const startedAt = Date.now();
    sockets[0].emit('close');
    const delta = Date.now() - startedAt;

    expect(liveState.stale).toBe(true);
    expect(liveState.connection.status).toBe('closed');
    expect(delta).toBeLessThan(1000);
    client.stop();
  });
});

describe('backoff', () => {
  it('doubles to the ceiling and keeps exactly one pending timer', () => {
    const { client, sockets, scheduler } = harness();
    client.start();

    const delays: number[] = [];
    for (let attempt = 0; attempt < 6; attempt += 1) {
      sockets[sockets.length - 1].emit('close');
      expect(scheduler.pending).toHaveLength(1);
      delays.push(scheduler.pending[0].ms);
      scheduler.runNext();
    }

    expect(delays).toEqual([1000, 2000, 4000, 8000, 15000, 15000]);
    client.stop();
  });

  it('restarts at the base delay after a snapshot', () => {
    const { client, sockets, scheduler } = harness();
    client.start();

    for (let attempt = 0; attempt < 3; attempt += 1) {
      sockets[sockets.length - 1].emit('close');
      scheduler.runNext();
    }
    expect(client.nextDelayMs).toBe(8000);

    sockets[sockets.length - 1].open();
    sockets[sockets.length - 1].send(SNAPSHOT_FRAME);
    expect(client.nextDelayMs).toBe(1000);

    sockets[sockets.length - 1].emit('close');
    expect(scheduler.pending[0].ms).toBe(1000);
    client.stop();
  });

  it('reconnects when the scheduled callback runs and applies the new snapshot', () => {
    const { client, sockets, scheduler } = harness();
    client.start();
    sockets[0].open();
    sockets[0].send(SNAPSHOT_FRAME);

    sockets[0].emit('close');
    expect(liveState.stale).toBe(true);
    scheduler.runNext();

    expect(sockets).toHaveLength(2);
    expect(liveState.stale).toBe(true);

    sockets[1].open();
    sockets[1].send({ ...SNAPSHOT_FRAME, data: { ...SNAPSHOT_FRAME.data, config_version: 9 } });

    expect(liveState.stale).toBe(false);
    expect(liveState.configVersion).toBe(9);
    client.stop();
  });
});

describe('teardown', () => {
  it('removes every listener, closes the socket and leaves nothing scheduled', () => {
    const { client, sockets, scheduler, view, net } = harness();
    client.start();
    const socket = sockets[0];
    expect([...socket.added].sort()).toEqual(['close', 'error', 'message', 'open']);
    expect(view.added).toEqual(['visibilitychange']);
    expect([...net.added].sort()).toEqual(['offline', 'online']);

    client.stop();

    expect([...socket.removed].sort()).toEqual(['close', 'error', 'message', 'open']);
    expect(view.removed).toEqual(['visibilitychange']);
    expect([...net.removed].sort()).toEqual(['offline', 'online']);
    expect(socket.closed).toBe(true);
    expect(scheduler.pending).toHaveLength(0);
    expect(liveState.hasSnapshot).toBe(false);

    // A zombie socket cannot touch state or start another attempt.
    socket.open();
    socket.send(SNAPSHOT_FRAME);
    socket.emit('close');
    expect(liveState.hasSnapshot).toBe(false);
    expect(liveState.stale).toBe(true);
    expect(scheduler.pending).toHaveLength(0);
    expect(sockets).toHaveLength(1);
  });

  it('cancels a pending reconnect', () => {
    const { client, sockets, scheduler } = harness();
    client.start();
    sockets[0].emit('close');
    expect(scheduler.pending).toHaveLength(1);

    client.stop();

    expect(scheduler.pending).toHaveLength(0);
    expect(sockets).toHaveLength(1);
  });
});

describe('malformed frames', () => {
  it('ignores bad JSON, unknown types and non-text frames without throwing', () => {
    const { client, sockets } = harness();
    client.start();
    sockets[0].open();

    expect(() => sockets[0].emit('message', { data: '{not json' })).not.toThrow();
    expect(() => sockets[0].send({ type: 'invented', data: {} })).not.toThrow();
    expect(() => sockets[0].emit('message', { data: 42 })).not.toThrow();
    expect(() => sockets[0].emit('message', {})).not.toThrow();

    expect(liveState.hasSnapshot).toBe(false);

    sockets[0].send(SNAPSHOT_FRAME);
    expect(liveState.hasSnapshot).toBe(true);
    client.stop();
  });
});

describe('recorded session', () => {
  it('replays every recorded frame through the socket', () => {
    const { client, sockets } = harness(50);
    client.start();
    sockets[0].open();

    for (const frame of WIRE_FRAMES) {
      sockets[0].send(frame);
    }

    expect(liveState.stale).toBe(false);
    expect(liveState.entities).toHaveLength(2);
    expect(liveState.entities[0].state).toBe('locked');
    expect(liveState.presses).toHaveLength(1);
    expect(liveState.presses[0].outcome).toBe('played');
    expect(liveState.health.detail).toBe('Home Assistant unreachable (timeout)');
    expect(liveState.configVersion).toBe(10);
    client.stop();
  });
});

describe('wake and network changes', () => {
  it('reconnects at once when the page becomes visible or the network returns', () => {
    const { client, sockets, scheduler, view, net } = harness();
    client.start();
    sockets[0].open();
    sockets[0].send(SNAPSHOT_FRAME);

    sockets[0].emit('close');
    expect(scheduler.pending).toHaveLength(1);

    view.visibilityState = 'hidden';
    view.emit('visibilitychange');
    expect(sockets).toHaveLength(1);

    view.visibilityState = 'visible';
    view.emit('visibilitychange');
    expect(sockets).toHaveLength(2);
    expect(scheduler.pending).toHaveLength(0);

    sockets[1].emit('close');
    expect(scheduler.pending).toHaveLength(1);
    net.emit('online');
    expect(sockets).toHaveLength(3);
    expect(scheduler.pending).toHaveLength(0);

    net.emit('offline');
    expect(liveState.stale).toBe(true);
    client.stop();
  });
});

describe('liveness watchdog', () => {
  const heartbeat = { type: 'heartbeat', data: { interval_s: 1 } } as const;

  it('an idle socket that keeps heartbeating is never reconnected', () => {
    const { client, sockets, scheduler } = harness();
    client.start();
    sockets[0].open();
    sockets[0].send(SNAPSHOT_FRAME);

    for (let beat = 0; beat < 50; beat += 1) {
      sockets[0].send(heartbeat);
    }

    expect(sockets).toHaveLength(1);
    expect(liveState.stale).toBe(false);
    expect(scheduler.pending).toHaveLength(1);
    expect(scheduler.pending[0].ms).toBe(3000);
    client.stop();
    expect(scheduler.pending).toHaveLength(0);
  });

  it('a backend that never heartbeats is never reconnected for silence', () => {
    const { client, sockets, scheduler } = harness();
    client.start();
    sockets[0].open();
    sockets[0].send(SNAPSHOT_FRAME);

    expect(scheduler.pending).toHaveLength(0);
    expect(sockets).toHaveLength(1);
    client.stop();
    expect(scheduler.pending).toHaveLength(0);
  });

  it('a zombie socket is closed, marked stale and retried when the heartbeat stops', () => {
    const { client, sockets, scheduler } = harness();
    client.start();
    sockets[0].open();
    sockets[0].send(SNAPSHOT_FRAME);
    sockets[0].send(heartbeat);

    scheduler.runNext();

    expect(sockets[0].closed).toBe(true);
    expect(liveState.stale).toBe(true);
    expect(scheduler.pending).toHaveLength(1);
    expect(scheduler.pending[0].ms).toBe(1000);
    scheduler.runNext();
    expect(sockets).toHaveLength(2);
    client.stop();
    expect(scheduler.pending).toHaveLength(0);
  });

  it('zombie detection keeps the doubling backoff and a snapshot resets it', () => {
    const { client, sockets, scheduler } = harness();
    client.start();
    sockets[0].open();
    sockets[0].send(SNAPSHOT_FRAME);
    sockets[0].send(heartbeat);
    scheduler.runNext();
    expect(scheduler.pending[0].ms).toBe(1000);

    scheduler.runNext();
    sockets[1].open();
    sockets[1].send(heartbeat);
    scheduler.runNext();
    expect(scheduler.pending).toHaveLength(1);
    expect(scheduler.pending[0].ms).toBe(2000);

    scheduler.runNext();
    sockets[2].open();
    sockets[2].send(SNAPSHOT_FRAME);
    sockets[2].send(heartbeat);
    scheduler.runNext();
    expect(scheduler.pending).toHaveLength(1);
    expect(scheduler.pending[0].ms).toBe(1000);
    client.stop();
    expect(scheduler.pending).toHaveLength(0);
  });

  it('an online event replaces a zombie socket even though it still reports open', () => {
    const { client, sockets, scheduler, net } = harness();
    client.start();
    sockets[0].open();
    sockets[0].send(SNAPSHOT_FRAME);
    sockets[0].send(heartbeat);
    expect(sockets[0].readyState).toBe(1);

    net.emit('online');

    expect(sockets[0].closed).toBe(true);
    expect(sockets).toHaveLength(2);
    expect(scheduler.pending).toHaveLength(0);
    expect(client.nextDelayMs).toBe(1000);
    client.stop();
    expect(scheduler.pending).toHaveLength(0);
  });

  // Once a cadence is known, the timeout survives a reconnect. The replacement
  // socket must be watched from open, rather than becoming a zombie nobody watches.
  it('a fresh socket that opens and then says nothing is caught by the learned timeout', () => {
    const { client, sockets, scheduler } = harness();
    client.start();
    sockets[0].open();
    sockets[0].send(SNAPSHOT_FRAME);
    sockets[0].send(heartbeat);

    expect(scheduler.pending).toHaveLength(1);
    expect(scheduler.pending[0].ms).toBe(3000);
    scheduler.runNext();
    expect(sockets[0].closed).toBe(true);
    expect(scheduler.pending).toHaveLength(1);
    expect(scheduler.pending[0].ms).toBe(1000);

    scheduler.runNext();
    expect(sockets).toHaveLength(2);

    sockets[1].open();
    expect(scheduler.pending).toHaveLength(1);
    expect(scheduler.pending[0].ms).toBe(3000);

    scheduler.runNext();
    expect(sockets[1].closed).toBe(true);
    expect(liveState.stale).toBe(true);
    expect(scheduler.pending).toHaveLength(1);
    client.stop();
    expect(scheduler.pending).toHaveLength(0);
  });

  it('offline then online recovers without waiting for the backoff', () => {
    const { client, sockets, scheduler, net } = harness();
    client.start();
    sockets[0].open();
    sockets[0].send(SNAPSHOT_FRAME);

    net.emit('offline');
    expect(liveState.stale).toBe(true);
    net.emit('online');

    expect(sockets).toHaveLength(2);
    expect(sockets[0].closed).toBe(true);
    expect(scheduler.pending).toHaveLength(0);
    sockets[1].open();
    sockets[1].send(SNAPSHOT_FRAME);
    expect(liveState.stale).toBe(false);
    client.stop();
    expect(scheduler.pending).toHaveLength(0);
  });
});
