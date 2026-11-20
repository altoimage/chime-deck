/**
 * Websocket transport: one connection, one reconnect timer, no queues.
 *
 * The backend pushes every live fact over one `/ws` socket (design 05 §4) and
 * sends one small `heartbeat` per client after a period of silence. Once
 * observed, that heartbeat is a liveness contract: three missed beats
 * (15 s at the production 5 s cadence) mean the socket is dead. A browser
 * `online` transition forces a fresh socket because the held one may be a
 * zombie. Three rules keep this client from leaking:
 *
 *   - `stop()` removes every listener it added (socket, document, window),
 *     cancels the one pending timer, closes the socket and resets the store.
 *     A late event cannot touch state afterwards: the handlers are gone and
 *     they check `#started` as a second line of defence.
 *   - a failure marks the store stale synchronously, then schedules exactly one
 *     retry: `min(maxDelay, baseDelay * 2 ** attempt)`, i.e. 1s, 2s, 4s, 8s,
 *     15s, 15s… The delay restarts at `baseDelayMs` after a snapshot, so a
 *     connect-then-die loop cannot spin at a second forever.
 *   - frames are parsed and applied synchronously. The handler is not `async`
 *     and awaits nothing, so no promise chain can grow per frame; the store's
 *     own caps bound what the frames can add. The watchdog is one timer,
 *     cancelled on stop/close and armed only after a heartbeat is observed.
 *
 * Everything the environment provides (socket factory, clock, document,
 * `navigator.onLine` events) is injectable, so the node test suite drives the
 * whole lifecycle without a browser.
 */

import type { WireMessage } from './api.js';
import type { LiveState } from './stores.svelte.js';

/** The four members this client uses, so a fake socket is a dozen lines. */
export interface SocketLike {
  readonly readyState: number;
  addEventListener(type: string, listener: SocketListener): void;
  removeEventListener(type: string, listener: SocketListener): void;
  close(): void;
}

/** A socket or DOM event: only `data` (text frames) is ever read. */
export interface SocketEvent {
  data?: unknown;
}

export type SocketListener = (event: SocketEvent) => void;

/** `document`-like: only visibility matters here. */
export interface ViewLike {
  readonly visibilityState: string;
  addEventListener(type: string, listener: () => void): void;
  removeEventListener(type: string, listener: () => void): void;
}

/** `window`-like: only the network events matter here. */
export interface NetLike {
  addEventListener(type: string, listener: () => void): void;
  removeEventListener(type: string, listener: () => void): void;
}

/** `(fn, ms) => cancel`, so tests can run the clock by hand. */
export type Scheduler = (fn: () => void, ms: number) => () => void;

export interface UrlLocation {
  protocol: string;
  host: string;
}

export interface WsClientOptions {
  /** Socket factory; defaults to the platform `WebSocket`. */
  createSocket?: (url: string) => SocketLike;
  /** URL resolver; defaults to `wsUrl()`. */
  url?: () => string;
  /** Timer factory; defaults to `setTimeout`/`clearTimeout`. */
  schedule?: Scheduler;
  /** First retry delay, doubled per failure. */
  baseDelayMs?: number;
  /** Ceiling for the retry delay. */
  maxDelayMs?: number;
  /** `document`-like source of `visibilitychange`; `null` disables it. */
  view?: ViewLike | null;
  /** `window`-like source of `online`/`offline`; `null` disables it. */
  net?: NetLike | null;
}

/** `WebSocket.OPEN`, named so the intent is readable. */
const SOCKET_OPEN = 1;
const DEFAULT_BASE_DELAY_MS = 1000;
const DEFAULT_MAX_DELAY_MS = 15000;
/** Three missed beats mean death; one dropped beat is tolerated (15 s at 5 s). */
const HEARTBEAT_MISSES = 3;
/** Keep very fast/slow server declarations within useful client bounds. */
const MIN_SILENCE_TIMEOUT_MS = 1000;
const MAX_SILENCE_TIMEOUT_MS = 120_000;

function currentLocation(): UrlLocation | null {
  const location: unknown = (globalThis as Record<string, unknown>).location;
  if (typeof location !== 'object' || location === null) {
    return null;
  }
  const { protocol, host } = location as { protocol?: unknown; host?: unknown };
  if (typeof protocol !== 'string' || typeof host !== 'string') {
    return null;
  }
  return { protocol, host };
}

/**
 * The backend's websocket URL: root-absolute `/ws` on this origin, matching
 * the app's root mount. `wss:` follows `https:` so a proxied deployment works
 * without configuration.
 */
export function wsUrl(loc: UrlLocation | null = currentLocation()): string {
  if (loc === null) {
    throw new Error('wsUrl() needs a browser location to resolve the socket URL');
  }
  const scheme = loc.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${loc.host}/ws`;
}

function defaultCreateSocket(url: string): SocketLike {
  const Ctor = (globalThis as Record<string, unknown>).WebSocket;
  if (typeof Ctor !== 'function') {
    throw new Error('this environment has no WebSocket implementation');
  }
  return new (Ctor as new (target: string) => SocketLike)(url);
}

function defaultSchedule(fn: () => void, ms: number): () => void {
  const timer = setTimeout(fn, ms);
  return () => clearTimeout(timer);
}

/**
 * Narrow an unknown parsed frame to the wire union. The envelope is validated
 * here (`type` is known, `data` is an object); the payload is trusted, because
 * it comes from this app's own hub and every `apply*` method is total.
 */
function asWireMessage(value: unknown): WireMessage | null {
  if (typeof value !== 'object' || value === null) {
    return null;
  }
  const { type, data } = value as { type?: unknown; data?: unknown };
  if (typeof data !== 'object' || data === null) {
    return null;
  }
  switch (type) {
    case 'snapshot':
      return { type, data } as WireMessage;
    case 'entity_state':
      return { type, data } as WireMessage;
    case 'press':
      return { type, data } as WireMessage;
    case 'config_changed':
      return { type, data } as WireMessage;
    case 'health':
      return { type, data } as WireMessage;
    case 'heartbeat':
      return { type, data } as WireMessage;
    default:
      return null;
  }
}

/**
 * Drives one live connection: connect, apply frames, reconnect with a bounded
 * backoff, and tear everything down on `stop()`.
 */
export class WsClient {
  #state: LiveState;
  #createSocket: (url: string) => SocketLike;
  #resolveUrl: () => string;
  #schedule: Scheduler;
  #baseDelayMs: number;
  #maxDelayMs: number;
  #view: ViewLike | null;
  #net: NetLike | null;

  #socket: SocketLike | null = null;
  #cancelTimer: (() => void) | null = null;
  #silenceTimeoutMs: number | null = null;
  #cancelWatchdog: (() => void) | null = null;
  #started = false;
  #delayMs: number;

  readonly #onOpen: SocketListener = () => this.#handleOpen();
  readonly #onMessage: SocketListener = (event) => this.#handleMessage(event);
  readonly #onDrop: SocketListener = () => this.#handleDrop();
  readonly #onVisibilityChange = (): void => this.#handleVisibilityChange();
  readonly #onOnline = (): void => this.#handleOnline();
  readonly #onOffline = (): void => this.#handleOffline();

  constructor(state: LiveState, options: WsClientOptions = {}) {
    this.#state = state;
    this.#createSocket = options.createSocket ?? defaultCreateSocket;
    this.#resolveUrl = options.url ?? (() => wsUrl());
    this.#schedule = options.schedule ?? defaultSchedule;
    this.#baseDelayMs = options.baseDelayMs ?? DEFAULT_BASE_DELAY_MS;
    this.#maxDelayMs = options.maxDelayMs ?? DEFAULT_MAX_DELAY_MS;
    this.#view = options.view === undefined ? browserView() : options.view;
    this.#net = options.net === undefined ? browserNet() : options.net;
    this.#delayMs = this.#baseDelayMs;
  }

  /** The delay the next failed attempt would wait, for tests and telemetry. */
  get nextDelayMs(): number {
    return this.#delayMs;
  }

  /** Connect and install the wake/network listeners. Safe to call twice. */
  start(): void {
    if (this.#started) {
      return;
    }
    this.#started = true;
    this.#view?.addEventListener('visibilitychange', this.#onVisibilityChange);
    this.#net?.addEventListener('online', this.#onOnline);
    this.#net?.addEventListener('offline', this.#onOffline);
    this.#connect();
  }

  /**
   * Tear everything down: no pending timer, no listener, no open socket, and
   * the store back at its initial state. Any frame the dead socket still
   * delivers is ignored.
   */
  stop(): void {
    this.#started = false;
    this.#cancelPendingTimer();
    this.#view?.removeEventListener('visibilitychange', this.#onVisibilityChange);
    this.#net?.removeEventListener('online', this.#onOnline);
    this.#net?.removeEventListener('offline', this.#onOffline);
    this.#closeSocket();
    this.#silenceTimeoutMs = null;
    this.#delayMs = this.#baseDelayMs;
    this.#state.reset();
  }

  #connect(): void {
    if (!this.#started) {
      return;
    }
    this.#cancelPendingTimer();
    this.#closeSocket();
    this.#state.markConnecting();
    const socket = this.#createSocket(this.#resolveUrl());
    this.#socket = socket;
    socket.addEventListener('open', this.#onOpen);
    socket.addEventListener('message', this.#onMessage);
    socket.addEventListener('close', this.#onDrop);
    socket.addEventListener('error', this.#onDrop);
  }

  #handleOpen(): void {
    if (!this.#started) {
      return;
    }
    // Open is not fresh: the snapshot that clears staleness follows it.
    this.#state.markOpen();
    this.#armWatchdog();
  }

  #handleMessage(event: SocketEvent): void {
    if (!this.#started || typeof event.data !== 'string') {
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(event.data);
    } catch {
      return;
    }
    const frame = asWireMessage(parsed);
    if (frame === null) {
      return;
    }
    switch (frame.type) {
      case 'snapshot':
        this.#delayMs = this.#baseDelayMs;
        this.#state.applySnapshot(frame.data);
        break;
      case 'entity_state':
        this.#state.applyEntityState(frame.data);
        break;
      case 'press':
        this.#state.applyPress(frame.data);
        break;
      case 'config_changed':
        this.#state.applyConfigChanged(frame.data.what);
        break;
      case 'health':
        this.#state.applyHealth(frame.data);
        break;
      case 'heartbeat':
        this.#noteHeartbeat(frame.data.interval_s);
        break;
    }
    // Any accepted frame proves liveness; the heartbeat only defines the timeout.
    this.#armWatchdog();
  }

  /**
   * The socket failed or closed: mark stale synchronously, then arm exactly one
   * retry whose delay doubles up to `maxDelayMs`.
   */
  #handleDrop(): void {
    if (!this.#started || this.#socket === null) {
      return;
    }
    this.#closeSocket();
    const delay = this.#delayMs;
    this.#state.markDisconnected();
    this.#delayMs = Math.min(this.#maxDelayMs, delay * 2);
    this.#scheduleReconnect(delay);
  }

  /** Wake from sleep or regain the network: connect now, not in 15 seconds. */
  #handleVisibilityChange(): void {
    if (this.#view?.visibilityState !== 'visible') {
      return;
    }
    this.#reconnectNow();
  }

  #handleOnline(): void {
    if (!this.#started) {
      return;
    }
    // `online` is a rare browser-asserted network transition; trust it over a
    // readyState that may still describe the zombie socket we currently hold.
    this.#reconnectNow(true);
  }

  /** No network at all: stale at once; the socket's own close will arm the retry. */
  #handleOffline(): void {
    if (!this.#started) {
      return;
    }
    this.#state.markDisconnected();
  }

  #reconnectNow(force = false): void {
    if (
      !this.#started ||
      (!force && this.#socket !== null && this.#socket.readyState === SOCKET_OPEN)
    ) {
      return;
    }
    this.#delayMs = this.#baseDelayMs;
    this.#connect();
  }

  #scheduleReconnect(delay: number): void {
    this.#cancelPendingTimer();
    this.#cancelTimer = this.#schedule(() => {
      this.#cancelTimer = null;
      this.#connect();
    }, delay);
  }

  #cancelPendingTimer(): void {
    const cancel = this.#cancelTimer;
    this.#cancelTimer = null;
    cancel?.();
  }

  #noteHeartbeat(intervalS: number): void {
    if (!Number.isFinite(intervalS) || intervalS <= 0) {
      return;
    }
    this.#silenceTimeoutMs = Math.min(
      MAX_SILENCE_TIMEOUT_MS,
      Math.max(MIN_SILENCE_TIMEOUT_MS, intervalS * 1000 * HEARTBEAT_MISSES)
    );
  }

  #armWatchdog(): void {
    const cancel = this.#cancelWatchdog;
    this.#cancelWatchdog = null;
    cancel?.();
    if (!this.#started || this.#silenceTimeoutMs === null) {
      return;
    }
    this.#cancelWatchdog = this.#schedule(() => {
      this.#cancelWatchdog = null;
      this.#handleSilence();
    }, this.#silenceTimeoutMs);
  }

  #handleSilence(): void {
    if (!this.#started) {
      return;
    }
    // Silence is the same failure as a real drop so the backoff cannot drift.
    this.#handleDrop();
  }

  /** Detach first, then close: a socket we own must not report to a dead client. */
  #closeSocket(): void {
    const cancelWatchdog = this.#cancelWatchdog;
    this.#cancelWatchdog = null;
    cancelWatchdog?.();
    const socket = this.#socket;
    if (socket === null) {
      return;
    }
    this.#socket = null;
    socket.removeEventListener('open', this.#onOpen);
    socket.removeEventListener('message', this.#onMessage);
    socket.removeEventListener('close', this.#onDrop);
    socket.removeEventListener('error', this.#onDrop);
    socket.close();
  }
}

function browserView(): ViewLike | null {
  if (typeof document === 'undefined') {
    return null;
  }
  return document as unknown as ViewLike;
}

function browserNet(): NetLike | null {
  if (typeof window === 'undefined') {
    return null;
  }
  return window as unknown as NetLike;
}
