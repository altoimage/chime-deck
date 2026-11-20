/**
 * Live application state: everything the UI knows because of the websocket.
 *
 * A rune-backed class in a `.svelte.ts` module, like `theme.svelte.ts` and
 * `toast.svelte.ts`, so plain TS modules and components can share one
 * singleton without a store library:
 *
 *   import { liveState } from '../lib/stores.svelte.js';
 *
 * The store is deliberately dumb about the transport: the websocket client
 * calls the `apply*` methods, the app wires `setConfigRefreshHandler()`. That
 * keeps the rules that matter testable without a socket:
 *
 *   - `stale` is the tile-staleness flag. It starts true, `markDisconnected()`
 *     sets it synchronously (no timer between a dead socket and a stale tile),
 *     and only `applySnapshot()` clears it — a socket that is open but has not
 *     sent its snapshot yet still shows yesterday's data, so `open` is not
 *     `fresh`.
 *   - `entity_state` deltas are applied by id and unknown ids are ignored: each
 *     carries the presentation block too, while an absent presentation key is
 *     "unchanged", never "cleared". The configuration is authoritative, and
 *     that is also the bound on how much the client can accumulate (only
 *     configured entities ever exist).
 *   - the tile's `tone` rides in that same presentation block: it is the
 *     backend's state→colour answer (HSD-d5x2), so it is stored with the tile and
 *     repainted next to the word, never re-derived here.
 *   - `presses` is capped at `MAX_PRESSES` and upserted by
 *     `button_id|pressed_at`, because a press arrives twice (queued, then
 *     played/failed) under the same key. Never two rows for one press, never
 *     unbounded growth.
 *   - a `config_changed` refetch coalesces: at most one request in flight, and
 *     a burst collapses into exactly one follow-up run — not a queue.
 *   - `hideWhenUnavailable` lives only on the REST row (`hub.py` omits it from
 *     the snapshot), so a snapshot carries the previous value over by entity id
 *     instead of resetting it. Until `refreshConfig()` has hydrated it once, a
 *     tile that should be hidden is briefly visible: a documented transient,
 *     never a lie about live state.
 *   - `selected` is runtime intent (D29), not configuration: the snapshot and
 *     `entity_state` frames carry it, the REST row does not, so both the
 *     snapshot mapping and a config refetch keep the live value. The wire sends
 *     an ABSOLUTE value, never a toggle, so a broadcast arriving after
 *     `setSelected()` agrees with it instead of flipping it back.
 *   - `restoreCached()` paints a last-known snapshot from `localStorage` before
 *     the socket connects. It marks the store stale and sets `restoredFromCache`,
 *     and the ONLY way back to live is `applySnapshot()` (which clears both).
 */

import {
  type ButtonTile,
  type EntityStateData,
  type EntityTile,
  type HealthRow,
  type HealthState,
  type PressData,
  type SnapshotData,
  toButtonTile,
  toEntityTile,
  toHealthState
} from './api.js';
import type { CachePayload } from './snapshot-cache.js';

/** How many presses the store keeps; the log is a status line, not a history. */
export const MAX_PRESSES = 20;

export type ConnectionStatus = 'idle' | 'connecting' | 'open' | 'closed';

export interface ConnectionState {
  status: ConnectionStatus;
  /** True while the tiles must not be trusted as live. */
  stale: boolean;
  /** Failed connection attempts since the last applied snapshot. */
  attempts: number;
}

/** One press, newest first, keyed by `button_id|pressed_at`. */
export interface PressEvent {
  key: string;
  buttonId: number;
  buttonName: string;
  outcome: string;
  pressedAt: string;
  /** False only while the press is queued; a terminal outcome is settled. */
  settled: boolean;
}

/**
 * What a config refetch returned. Both fields are optional so a handler may
 * refresh only the half named by `what`, and `void` is a valid answer when the
 * handler has nothing to merge.
 */
export interface ConfigRefresh {
  entities?: EntityTile[];
  buttons?: ButtonTile[];
}

export type ConfigRefreshHandler = (what: string) => Promise<ConfigRefresh | void>;

/** Health of an environment that has never been configured, as the backend reports it. */
const INITIAL_HEALTH: HealthState = {
  ha: 'not_configured',
  mqtt: 'not_configured',
  audio: 'disabled',
  detail: null
};

/**
 * Replace a freshly fetched tile list while keeping the live runtime facts of
 * entities that already existed. The REST row carries no `attributes` or
 * `last_changed` and its `state` is null until the live wiring lands, so a
 * blind replace would wipe what `entity_state` frames already delivered.
 *
 * It carries no `selected` either (D29: the configuration surface is
 * runtime-free), so the live value is kept for the same reason — without it,
 * `refreshConfig()` would silently clear the border of every selected tile.
 */
function mergeEntities(fresh: readonly EntityTile[], current: readonly EntityTile[]): EntityTile[] {
  const previous = new Map(current.map((tile) => [tile.entityId, tile]));
  return fresh.map((tile) => {
    const live = previous.get(tile.entityId);
    if (live === undefined) {
      return tile;
    }
    return {
      ...tile,
      state: live.state,
      attributes: live.attributes,
      lastChanged: live.lastChanged,
      // The tone belongs to the state it was resolved from — both arrive in the
      // same payload. A refetch that saw the same state may still recolour the
      // tile (the owner just edited the mapping and the row is how that lands),
      // so its tone wins; a refetch that saw an OLDER state than a delta already
      // applied must not paint the newer word in the older colour.
      tone: tile.state === live.state ? tile.tone : live.tone,
      selected: live.selected
    };
  });
}

/**
 * Carry the REST-only `hideWhenUnavailable` flag over a fresh snapshot.
 *
 * A snapshot row does not send the flag, so mapping the frame alone would
 * reset every tile to "always visible" and a tile the user hid would reappear
 * on the next reconnect. `refreshConfig()` hydrates the flag from the
 * authoritative REST row; this keeps it in between.
 */
function preserveHideFlags(fresh: readonly EntityTile[], current: readonly EntityTile[]): EntityTile[] {
  const previous = new Map(current.map((tile) => [tile.entityId, tile]));
  return fresh.map((tile) => {
    const known = previous.get(tile.entityId);
    if (known === undefined) {
      return tile;
    }
    return { ...tile, hideWhenUnavailable: known.hideWhenUnavailable };
  });
}

/** The rune-backed store class; the app uses the `liveState` singleton. */
export class LiveState {
  #entities = $state<EntityTile[]>([]);
  #buttons = $state<ButtonTile[]>([]);
  #health = $state<HealthState>({ ...INITIAL_HEALTH });
  #status = $state<ConnectionStatus>('idle');
  #stale = $state(true);
  #attempts = $state(0);
  #hasSnapshot = $state(false);
  #configVersion = $state(0);
  #presses = $state<PressEvent[]>([]);
  #restoredFromCache = $state(false);
  #refreshing = $state(false);
  #refreshHandler: ConfigRefreshHandler | null = null;
  #refreshPending = false;
  #refreshRun = 0;

  /** The configured status tiles, in the server's `sort_order`. */
  get entities(): readonly EntityTile[] {
    return this.#entities;
  }

  /** The configured chime buttons, in the server's `sort_order`. */
  get buttons(): readonly ButtonTile[] {
    return this.#buttons;
  }

  /** Health of Home Assistant, the broker and audio. */
  get health(): HealthState {
    return this.#health;
  }

  /** Transport status, staleness and the failed-attempt count. */
  get connection(): ConnectionState {
    return { status: this.#status, stale: this.#stale, attempts: this.#attempts };
  }

  /** True while the shown tiles must not be trusted as live. */
  get stale(): boolean {
    return this.#stale;
  }

  /** True once a snapshot has been applied at least once. */
  get hasSnapshot(): boolean {
    return this.#hasSnapshot;
  }

  /** True while the shown tiles came from the last-known cache, not the socket. */
  get restoredFromCache(): boolean {
    return this.#restoredFromCache;
  }

  /** The last snapshot's `config_version`, plus one per `config_changed`. */
  get configVersion(): number {
    return this.#configVersion;
  }

  /** Recent presses, newest first, capped at `MAX_PRESSES`. */
  get presses(): readonly PressEvent[] {
    return this.#presses;
  }

  /** True while a config refetch is in flight. */
  get refreshing(): boolean {
    return this.#refreshing;
  }

  /** Replace everything a snapshot carries; the only place staleness clears. */
  applySnapshot(data: SnapshotData): void {
    this.#entities = preserveHideFlags(data.entities.map(toEntityTile), this.#entities);
    this.#buttons = data.buttons.map(toButtonTile);
    this.#health = toHealthState(data.health);
    this.#configVersion = data.config_version;
    this.#stale = false;
    this.#hasSnapshot = true;
    this.#restoredFromCache = false;
    this.#attempts = 0;
  }

  /**
   * Paint a last-known payload read from `localStorage` before the socket
   * connects. It is deliberately stale: only `applySnapshot()` clears both
   * staleness and `restoredFromCache`, so a restored screen can never claim to
   * be live. Status, attempts and presses are untouched — they belong to the
   * transport, which has not run yet.
   */
  restoreCached(payload: CachePayload): void {
    this.#entities = payload.entities.map((tile) => ({ ...tile }));
    this.#buttons = payload.buttons.map((button) => ({ ...button }));
    this.#health = { ...payload.health };
    this.#configVersion = payload.configVersion;
    this.#hasSnapshot = true;
    this.#stale = true;
    this.#restoredFromCache = true;
  }

  /**
   * The current view shapes for the cache writer, or `null` before the first
   * snapshot. Plain copies, so the cache can never alias the live store.
   */
  cachePayload(): CachePayload | null {
    if (!this.#hasSnapshot) {
      return null;
    }
    return {
      configVersion: this.#configVersion,
      entities: this.#entities.map((tile) => ({ ...tile })),
      buttons: this.#buttons.map((button) => ({ ...button })),
      health: { ...this.#health }
    };
  }

  /** Apply one live entity delta; an id the configuration does not know is ignored. */
  applyEntityState(data: EntityStateData): void {
    const index = this.#entities.findIndex((tile) => tile.entityId === data.entity_id);
    if (index === -1) {
      return;
    }
    const current = this.#entities[index];
    this.#entities[index] = {
      ...current,
      // `state` is optional because a selection toggle with no state change
      // arrives as a minimal frame carrying only `{entity_id, selected}`: an
      // absent key is "unchanged", so it must never blank the tile's state.
      state: data.state === undefined ? current.state : data.state,
      attributes: data.attributes === undefined ? current.attributes : data.attributes,
      lastChanged: data.last_changed === undefined ? current.lastChanged : data.last_changed,
      // The presentation block: the backend resolves the word, the unit and the
      // attribute hints from the same state hints a snapshot row uses, so a
      // delta repaints all three instead of keeping the snapshot's. `null` is a
      // value here (it clears the word), so only an absent key means "keep".
      displayedState:
        data.displayed_state === undefined ? current.displayedState : data.displayed_state,
      unitOfMeasurement:
        data.unit_of_measurement === undefined
          ? current.unitOfMeasurement
          : data.unit_of_measurement,
      attributeHints:
        data.attribute_hints === undefined ? current.attributeHints : data.attribute_hints,
      // The colour rides with the word: `tone` is resolved by the backend from
      // the same state, so a delta repaints it like `displayedState`. `null` is
      // a value here ("no state, no colour"), so only an absent key means "keep".
      tone: data.tone === undefined ? current.tone : data.tone,
      // The wire carries an ABSOLUTE value, never a toggle, so applying a
      // broadcast after an optimistic write is idempotent — it agrees rather
      // than flipping twice.
      selected: data.selected === undefined ? current.selected : data.selected
    };
  }

  /**
   * Light or clear one tile (D29). An unknown id and a value the tile already
   * has are both no-ops, so a repeated tap or an entity the configuration has
   * dropped never churns the array or resurrects a tile.
   *
   * This is the optimistic write after a tap; the hub's broadcast carries the
   * absolute value and `applyEntityState()` then agrees with it.
   */
  setSelected(entityId: string, selected: boolean): void {
    const index = this.#entities.findIndex((tile) => tile.entityId === entityId);
    if (index === -1) {
      return;
    }
    const current = this.#entities[index];
    if (current.selected === selected) {
      return;
    }
    this.#entities[index] = { ...current, selected };
  }

  /** Whether one tile is lit; an entity the configuration does not know is not. */
  isSelected(entityId: string): boolean {
    return this.#entities.find((tile) => tile.entityId === entityId)?.selected ?? false;
  }

  /**
   * Upsert one press by `button_id|pressed_at`. The queued report and its
   * terminal flip are the same press; a terminal report whose `queued` was
   * never seen still becomes exactly one settled row.
   */
  applyPress(data: PressData): void {
    const key = `${data.button_id}|${data.pressed_at}`;
    const settled = data.outcome !== 'queued';
    const index = this.#presses.findIndex((press) => press.key === key);
    if (index === -1) {
      const event: PressEvent = {
        key,
        buttonId: data.button_id,
        buttonName: data.button_name,
        outcome: data.outcome,
        pressedAt: data.pressed_at,
        settled
      };
      this.#presses = [event, ...this.#presses].slice(0, MAX_PRESSES);
      return;
    }
    this.#presses[index] = {
      ...this.#presses[index],
      buttonName: data.button_name,
      outcome: data.outcome,
      settled
    };
  }

  /** Apply a health frame; `detail` is null when the backend has nothing to add. */
  applyHealth(data: HealthRow): void {
    this.#health = toHealthState(data);
  }

  /**
   * Ask the installed handler to refetch `what`, exactly as a
   * `config_changed` frame would. The app calls this once on mount so the
   * REST-only fields a snapshot never carries (`hideWhenUnavailable`) are
   * hydrated; the version bump still means "the configuration may have
   * changed", which is what the counter already means.
   */
  refreshConfig(what: string): void {
    this.#configVersion += 1;
    void this.#drainRefresh(what);
  }

  /**
   * Record a configuration change and refetch through the handler. The version
   * counter moves immediately — nothing else is optimistic.
   */
  applyConfigChanged(what: string): void {
    this.refreshConfig(what);
  }

  /** Install the refetch callback; `null` removes it. */
  setConfigRefreshHandler(fn: ConfigRefreshHandler | null): void {
    this.#refreshHandler = fn;
  }

  /** A socket attempt has started; the tiles stay stale until its snapshot. */
  markConnecting(): void {
    this.#status = 'connecting';
  }

  /** The socket is open. Still stale: the snapshot has not arrived yet. */
  markOpen(): void {
    this.#status = 'open';
  }

  /** The socket is down: stale in the same tick, and one more attempt counted. */
  markDisconnected(): void {
    this.#status = 'closed';
    this.#stale = true;
    this.#attempts += 1;
  }

  /**
   * Return to the initial state (unmount, tests). The handler is kept.
   *
   * There is no separate flag store to clear: the preserved
   * `hideWhenUnavailable` values live on `#entities`, which this empties, so a
   * later snapshot starts from the mapping default rather than a stale flag.
   */
  reset(): void {
    this.#entities = [];
    this.#buttons = [];
    this.#health = { ...INITIAL_HEALTH };
    this.#status = 'idle';
    this.#stale = true;
    this.#attempts = 0;
    this.#hasSnapshot = false;
    this.#configVersion = 0;
    this.#presses = [];
    this.#restoredFromCache = false;
    this.#refreshing = false;
    this.#refreshPending = false;
    // Abandon any in-flight run so its result cannot land in the fresh store.
    this.#refreshRun += 1;
  }

  /**
   * One refetch loop: at most one run in flight, and any change that arrives
   * meanwhile collapses into exactly one follow-up run (`#refreshPending` is a
   * flag, not a queue). Handler failures are swallowed on purpose — a refetch
   * that fails leaves the last good configuration on screen.
   */
  async #drainRefresh(what: string): Promise<void> {
    if (this.#refreshing) {
      this.#refreshPending = true;
      return;
    }
    const handler = this.#refreshHandler;
    if (handler === null) {
      return;
    }
    const run = this.#refreshRun;
    this.#refreshing = true;
    let again = true;
    while (again && run === this.#refreshRun) {
      again = false;
      try {
        const result = await handler(what);
        if (run === this.#refreshRun && result !== undefined) {
          this.#mergeRefresh(result);
        }
      } catch {
        // A rejected refetch keeps the state it has; only `refreshing` clears.
      }
      if (this.#refreshPending) {
        this.#refreshPending = false;
        again = true;
      }
    }
    if (run === this.#refreshRun) {
      this.#refreshPending = false;
      this.#refreshing = false;
    }
  }

  #mergeRefresh(result: ConfigRefresh): void {
    if (result.entities !== undefined) {
      this.#entities = mergeEntities(result.entities, this.#entities);
    }
    if (result.buttons !== undefined) {
      this.#buttons = result.buttons;
    }
  }
}

/** The one live-state instance the app runs. */
export const liveState = new LiveState();
