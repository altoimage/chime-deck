import { beforeEach, describe, expect, it } from 'vitest';

import { toEntityTileFromResponse, type EntityResponse } from './api.js';
import {
  CONFIG_CHANGED_ENTITIES_FRAME,
  ENTITY_STATE_LOCKED_FRAME,
  ENTITY_STATE_UNLOCKED_FRAME,
  HEALTH_FRAME,
  PRESS_PLAYED_FRAME,
  PRESS_QUEUED_FRAME,
  SNAPSHOT_FRAME
} from './fixtures/wire.js';
import { liveState, MAX_PRESSES } from './stores.svelte.js';
import type { CachePayload } from './snapshot-cache.js';

/** `SNAPSHOT_FRAME`'s `config_version`, so a bump assertion stays readable. */
const BEFORE_VERSION = SNAPSHOT_FRAME.data.config_version;

/** Let every already-resolved promise callback run. */
function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

/** A `GET /api/entities` row: the REST shape, which carries no selection (D29). */
function restRow(overrides: Partial<EntityResponse> = {}): EntityResponse {
  return {
    id: 1,
    entity_id: 'lock.front_door',
    display_name: 'Front Door',
    icon: 'mdi:door',
    icon_source: 'user',
    icon_override: 'mdi:door',
    accent_color: '#4f9cf9',
    show_attributes: [],
    hide_when_unavailable: false,
    sort_order: 0,
    created_at: '2026-01-01T00:00:00+00:00',
    state: null,
    displayed_state: null,
    unit_of_measurement: null,
    attribute_hints: [],
    warning: null,
    tone: null,
    state_tones: null,
    colour_by_state: true,
    ...overrides
  };
}

beforeEach(() => {
  liveState.reset();
  liveState.setConfigRefreshHandler(null);
});

describe('snapshot', () => {
  it('fills entities, buttons, health and the version and clears staleness', () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);

    expect(liveState.entities.map((tile) => tile.entityId)).toEqual([
      'lock.front_door',
      'light.hall'
    ]);
    const frontDoor = liveState.entities[0];
    expect(frontDoor).toEqual({
      id: 1,
      entityId: 'lock.front_door',
      displayName: 'Front Door',
      state: null,
      attributes: {},
      icon: 'mdi:door',
      iconSource: 'user',
      accentColor: '#4f9cf9',
      lastChanged: null,
      displayedState: null,
      unitOfMeasurement: null,
      attributeHints: [],
      tone: null,
      hideWhenUnavailable: false,
      selected: false
    });

    expect(liveState.buttons).toEqual([
      { id: 1, name: 'Front Door Chime', slug: 'front-door', showOnDashboard: true }
    ]);
    expect(liveState.health).toEqual({
      ha: 'not_configured',
      mqtt: 'reconnecting',
      audio: 'ready',
      detail: null
    });
    expect(liveState.configVersion).toBe(7);
    expect(liveState.stale).toBe(false);
    expect(liveState.hasSnapshot).toBe(true);
  });
});

describe('entity_state deltas', () => {
  it('updates only the matching tile and leaves the rest untouched', () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    const before = liveState.entities.map((tile) => ({ ...tile }));

    liveState.applyEntityState(ENTITY_STATE_UNLOCKED_FRAME.data);

    expect(liveState.entities[0].state).toBe('unlocked');
    expect(liveState.entities[0].attributes).toEqual({ battery_level: 87 });
    expect(liveState.entities[0].lastChanged).toBe('2026-11-15T02:11:04.512+00:00');
    expect(liveState.entities[1]).toEqual(before[1]);
    expect(liveState.entities.map((tile) => tile.id)).toEqual([1, 2]);
  });

  it('updates the word, unit and attribute hints from a presentation delta', () => {
    const frontDoor = {
      ...SNAPSHOT_FRAME.data.entities[0],
      displayed_state: 'Locked',
      unit_of_measurement: 'old unit',
      attribute_hints: [{ name: 'battery_level', label: 'Battery', value: '86%' }]
    };
    liveState.applySnapshot({
      ...SNAPSHOT_FRAME.data,
      entities: [frontDoor, SNAPSHOT_FRAME.data.entities[1]]
    });

    liveState.applyEntityState({
      entity_id: 'lock.front_door',
      state: 'unlocked',
      displayed_state: 'Unlocked',
      unit_of_measurement: 'new unit',
      attribute_hints: [{ name: 'battery_level', label: 'Battery', value: '87%' }],
      tone: 'alert'
    });

    expect(liveState.entities[0].displayedState).toBe('Unlocked');
    expect(liveState.entities[0].unitOfMeasurement).toBe('new unit');
    expect(liveState.entities[0].attributeHints).toEqual([
      { name: 'battery_level', label: 'Battery', value: '87%' }
    ]);
    // The colour rides with the word: it is the backend's resolution for the
    // state this frame carries, not something this side re-derives.
    expect(liveState.entities[0].tone).toBe('alert');
  });

  it('keeps each presentation value when its key is absent from a delta', () => {
    const frontDoor = {
      ...SNAPSHOT_FRAME.data.entities[0],
      displayed_state: 'Locked',
      unit_of_measurement: 'old unit',
      attribute_hints: [{ name: 'battery_level', label: 'Battery', value: '86%' }],
      tone: 'alert'
    };
    liveState.applySnapshot({
      ...SNAPSHOT_FRAME.data,
      entities: [frontDoor, SNAPSHOT_FRAME.data.entities[1]]
    });

    liveState.applyEntityState({ entity_id: 'lock.front_door', state: 'unlocked' });

    expect(liveState.entities[0].displayedState).toBe('Locked');
    expect(liveState.entities[0].unitOfMeasurement).toBe('old unit');
    expect(liveState.entities[0].attributeHints).toEqual([
      { name: 'battery_level', label: 'Battery', value: '86%' }
    ]);
    // A frame that names no tone must not be read as "the tone is now null" —
    // that would grey a tile whose colour is still the backend's last answer.
    expect(liveState.entities[0].tone).toBe('alert');
  });

  it('clears the displayed word when a delta explicitly sends null', () => {
    const frontDoor = { ...SNAPSHOT_FRAME.data.entities[0], displayed_state: 'Locked' };
    liveState.applySnapshot({
      ...SNAPSHOT_FRAME.data,
      entities: [frontDoor, SNAPSHOT_FRAME.data.entities[1]]
    });

    liveState.applyEntityState({
      entity_id: 'lock.front_door',
      state: 'unlocked',
      displayed_state: null
    });

    expect(liveState.entities[0].displayedState).toBeNull();
  });

  it('ignores a delta for an entity the configuration does not know', () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);

    liveState.applyEntityState({
      entity_id: 'sensor.never_configured',
      state: 'on',
      attributes: { anything: true },
      last_changed: '2026-11-15T02:20:00.000+00:00'
    });

    expect(liveState.entities).toHaveLength(2);
    expect(liveState.entities.map((tile) => tile.entityId)).toEqual([
      'lock.front_door',
      'light.hall'
    ]);
  });
});

describe('presses', () => {
  it('upserts the queued press and its terminal flip into one entry', () => {
    liveState.applyPress(PRESS_QUEUED_FRAME.data);
    expect(liveState.presses).toHaveLength(1);
    expect(liveState.presses[0].settled).toBe(false);

    liveState.applyPress(PRESS_PLAYED_FRAME.data);
    expect(liveState.presses).toHaveLength(1);
    expect(liveState.presses[0]).toEqual({
      key: '1|2026-11-15T02:13:00.100+00:00',
      buttonId: 1,
      buttonName: 'Front Door Chime',
      outcome: 'played',
      pressedAt: '2026-11-15T02:13:00.100+00:00',
      settled: true
    });
  });

  it('accepts a terminal press whose queued report was never seen', () => {
    liveState.applyPress({
      button_id: 2,
      button_name: 'Back Door',
      outcome: 'deduped',
      pressed_at: '2026-11-15T02:14:00.000+00:00'
    });

    expect(liveState.presses).toHaveLength(1);
    expect(liveState.presses[0].outcome).toBe('deduped');
    expect(liveState.presses[0].settled).toBe(true);
  });

  it('keeps two presses of the same button with different timestamps', () => {
    liveState.applyPress(PRESS_QUEUED_FRAME.data);
    liveState.applyPress({ ...PRESS_QUEUED_FRAME.data, pressed_at: '2026-11-15T02:13:05.000+00:00' });

    expect(liveState.presses).toHaveLength(2);
    expect(liveState.presses.map((press) => press.pressedAt)).toEqual([
      '2026-11-15T02:13:05.000+00:00',
      '2026-11-15T02:13:00.100+00:00'
    ]);
  });

  it('bounds the log at MAX_PRESSES however many frames arrive', () => {
    for (let index = 0; index < 5000; index += 1) {
      liveState.applyPress({
        button_id: 1,
        button_name: 'Front Door Chime',
        outcome: index % 2 === 0 ? 'queued' : 'played',
        pressed_at: `press-${index}`
      });
    }

    expect(liveState.presses).toHaveLength(MAX_PRESSES);
    expect(liveState.presses[0].pressedAt).toBe('press-4999');
  });
});

describe('health', () => {
  it('applies a frame with a detail and clears the detail when absent', () => {
    liveState.applyHealth(HEALTH_FRAME.data);
    expect(liveState.health).toEqual({
      ha: 'reconnecting',
      mqtt: 'reconnecting',
      audio: 'ready',
      detail: 'Home Assistant unreachable (timeout)'
    });

    liveState.applyHealth({ ha: 'connected', mqtt: 'connected', audio: 'ready' });
    expect(liveState.health).toEqual({
      ha: 'connected',
      mqtt: 'connected',
      audio: 'ready',
      detail: null
    });
  });
});

describe('config_changed', () => {
  it('bumps the version without touching the tiles and coalesces a burst', async () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    const before = liveState.entities.map((tile) => ({ ...tile }));

    const resolvers: Array<() => void> = [];
    let calls = 0;
    liveState.setConfigRefreshHandler(() => {
      calls += 1;
      return new Promise<void>((resolve) => resolvers.push(resolve));
    });

    for (let index = 0; index < 5; index += 1) {
      liveState.applyConfigChanged(CONFIG_CHANGED_ENTITIES_FRAME.data.what);
    }

    expect(liveState.configVersion).toBe(BEFORE_VERSION + 5);
    expect(calls).toBe(1);
    expect(liveState.refreshing).toBe(true);
    expect(liveState.entities).toEqual(before);

    resolvers.shift()?.();
    await flush();
    expect(calls).toBe(2);

    resolvers.shift()?.();
    await flush();
    expect(calls).toBe(2);
    expect(liveState.refreshing).toBe(false);
  });

  it('survives a rejected refetch with the state it had', async () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    liveState.setConfigRefreshHandler(() => Promise.reject(new Error('backend down')));

    liveState.applyConfigChanged('entities');
    await flush();

    expect(liveState.refreshing).toBe(false);
    expect(liveState.entities).toHaveLength(2);
    expect(liveState.configVersion).toBe(BEFORE_VERSION + 1);
  });

  it('keeps the live deltas when a refresh brings rows with a null state', async () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    liveState.applyEntityState(ENTITY_STATE_LOCKED_FRAME.data);

    const refreshed = [
      { ...liveState.entities[0], state: null, attributes: {}, lastChanged: null },
      { ...liveState.entities[1], displayName: 'Hall Lamp', state: null, attributes: {}, lastChanged: null }
    ];
    liveState.setConfigRefreshHandler(() => Promise.resolve({ entities: refreshed, buttons: [] }));

    liveState.applyConfigChanged('entities');
    await flush();

    expect(liveState.entities[0].state).toBe('locked');
    expect(liveState.entities[0].attributes).toEqual({ battery_level: 86 });
    expect(liveState.entities[0].lastChanged).toBe('2026-11-15T02:12:30.900+00:00');
    expect(liveState.entities[1].displayName).toBe('Hall Lamp');
    expect(liveState.buttons).toEqual([]);
    expect(liveState.stale).toBe(false);
    expect(liveState.refreshing).toBe(false);
  });
});

describe('preserved REST-only flags', () => {
  it('keeps hideWhenUnavailable across a snapshot, which does not carry it', async () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    liveState.setConfigRefreshHandler(() =>
      Promise.resolve({
        entities: liveState.entities.map((tile) => ({ ...tile, hideWhenUnavailable: tile.id === 1 }))
      })
    );

    liveState.refreshConfig('entities');
    await flush();

    expect(liveState.entities[0].hideWhenUnavailable).toBe(true);
    expect(liveState.entities[1].hideWhenUnavailable).toBe(false);

    // A snapshot row does not send the flag (hub.py omits it), so it must be
    // carried over by entity id rather than reset to the mapping default.
    liveState.applySnapshot(SNAPSHOT_FRAME.data);

    expect(liveState.entities[0].hideWhenUnavailable).toBe(true);
    expect(liveState.entities[1].hideWhenUnavailable).toBe(false);
  });

  it('keeps a selected tile lit across a config refresh, whose REST rows carry no selection', async () => {
    const [frontDoor, hall] = SNAPSHOT_FRAME.data.entities;
    liveState.applySnapshot({
      ...SNAPSHOT_FRAME.data,
      entities: [{ ...frontDoor, selected: true }, hall]
    });
    expect(liveState.isSelected('lock.front_door')).toBe(true);

    liveState.setConfigRefreshHandler(() =>
      Promise.resolve({
        entities: [
          toEntityTileFromResponse(restRow()),
          toEntityTileFromResponse(
            restRow({
              id: 2,
              entity_id: 'light.hall',
              display_name: 'Hall Light',
              icon: 'mdi:lightbulb',
              icon_source: 'fallback',
              icon_override: null,
              accent_color: null,
              sort_order: 1
            })
          )
        ]
      })
    );

    liveState.refreshConfig('entities');
    await flush();

    // `refreshConfig()` maps REST rows, which carry no selection by design, so
    // without the merge a refresh would silently clear every selected border.
    expect(liveState.entities[0].selected).toBe(true);
    expect(liveState.entities[1].selected).toBe(false);
    expect(liveState.entities[1].displayName).toBe('Hall Light');
  });

  it('takes a refreshed tone for the state it saw, and keeps the live one for an older state', async () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    liveState.applyEntityState(ENTITY_STATE_LOCKED_FRAME.data);
    expect(liveState.entities[0].tone).toBe('ok');

    // The owner edits the mapping: the REST row still says `locked`, but the
    // new table makes it amber. The refetch is how that reaches the panel, so
    // its tone wins while the states agree.
    liveState.setConfigRefreshHandler(() =>
      Promise.resolve({ entities: liveState.entities.map((tile) => ({ ...tile, tone: 'warn' })) })
    );
    liveState.refreshConfig('entities');
    await flush();

    expect(liveState.entities[0].tone).toBe('warn');

    // A refetch that resolved the tone from an OLDER state than a delta already
    // applied must not paint the newer word in the older colour.
    liveState.setConfigRefreshHandler(() =>
      Promise.resolve({
        entities: liveState.entities.map((tile) => ({ ...tile, state: null, tone: 'off' }))
      })
    );
    liveState.setSelected('lock.front_door', true);
    liveState.refreshConfig('entities');
    await flush();

    expect(liveState.entities[0].state).toBe('locked');
    expect(liveState.entities[0].tone).toBe('warn');
  });

  it('refreshConfig calls the installed handler once and bumps the version', async () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);

    const whats: string[] = [];
    liveState.setConfigRefreshHandler((what) => {
      whats.push(what);
      return Promise.resolve({ entities: [] });
    });

    liveState.refreshConfig('entities');
    expect(liveState.configVersion).toBe(BEFORE_VERSION + 1);

    await flush();

    expect(whats).toEqual(['entities']);
    expect(liveState.refreshing).toBe(false);
  });

  it('leaves no preserved flag behind after reset', async () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    liveState.setConfigRefreshHandler(() =>
      Promise.resolve({
        entities: liveState.entities.map((tile) => ({ ...tile, hideWhenUnavailable: true }))
      })
    );
    liveState.refreshConfig('entities');
    await flush();
    expect(liveState.entities[0].hideWhenUnavailable).toBe(true);

    liveState.reset();
    liveState.applySnapshot(SNAPSHOT_FRAME.data);

    expect(liveState.entities[0].hideWhenUnavailable).toBe(false);
  });
});

describe('connection', () => {
  it('goes stale in the same tick on markDisconnected', () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    expect(liveState.stale).toBe(false);

    const startedAt = Date.now();
    liveState.markDisconnected();
    const delta = Date.now() - startedAt;

    expect(liveState.stale).toBe(true);
    expect(delta).toBeLessThan(1000);
    expect(liveState.connection).toEqual({ status: 'closed', stale: true, attempts: 1 });
  });

  it('starts stale and has no snapshot, and reset returns to that', () => {
    expect(liveState.stale).toBe(true);
    expect(liveState.hasSnapshot).toBe(false);
    expect(liveState.connection).toEqual({ status: 'idle', stale: true, attempts: 0 });

    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    liveState.reset();

    expect(liveState.stale).toBe(true);
    expect(liveState.hasSnapshot).toBe(false);
    expect(liveState.entities).toEqual([]);
    expect(liveState.buttons).toEqual([]);
    expect(liveState.presses).toEqual([]);
    expect(liveState.configVersion).toBe(0);
  });
});

describe('selection', () => {
  it('maps the selected flag from a snapshot row', () => {
    const [frontDoor, hall] = SNAPSHOT_FRAME.data.entities;

    liveState.applySnapshot({
      ...SNAPSHOT_FRAME.data,
      entities: [{ ...frontDoor, selected: true }, hall]
    });

    expect(liveState.entities[0].selected).toBe(true);
    expect(liveState.entities[1].selected).toBe(false);
  });

  it('applies a selection-only frame without wiping the live state', () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    liveState.applyEntityState(ENTITY_STATE_LOCKED_FRAME.data);
    const before = { ...liveState.entities[0] };

    // A toggle with no state change arrives as a MINIMAL frame: exactly
    // `{entity_id, selected}`, with no `state` key at all.
    liveState.applyEntityState({ entity_id: 'lock.front_door', selected: true });

    const after = liveState.entities[0];
    expect(after.selected).toBe(true);
    expect(after.state).toBe('locked');
    expect(after.state).toBe(before.state);
    expect(after.attributes).toEqual(before.attributes);
    expect(after.lastChanged).toBe(before.lastChanged);
    expect(after.displayedState).toBe(before.displayedState);
    expect(after.unitOfMeasurement).toBe(before.unitOfMeasurement);
    expect(liveState.entities[1].selected).toBe(false);
    expect(liveState.entities[1].state).toBeNull();
  });

  it('keeps the current selection when a frame omits it', () => {
    const [frontDoor, hall] = SNAPSHOT_FRAME.data.entities;
    liveState.applySnapshot({
      ...SNAPSHOT_FRAME.data,
      entities: [{ ...frontDoor, selected: true }, hall]
    });

    liveState.applyEntityState(ENTITY_STATE_UNLOCKED_FRAME.data);

    expect(liveState.entities[0].state).toBe('unlocked');
    expect(liveState.entities[0].selected).toBe(true);
  });

  it('setSelected flips exactly one tile and replaces only that tile', () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    const beforeFrontDoor = liveState.entities[0];
    const beforeHall = liveState.entities[1];

    liveState.setSelected('lock.front_door', true);

    expect(liveState.isSelected('lock.front_door')).toBe(true);
    expect(liveState.isSelected('light.hall')).toBe(false);
    expect(liveState.entities).toHaveLength(2);
    expect(liveState.entities[0]).not.toBe(beforeFrontDoor);
    expect(liveState.entities[1]).toBe(beforeHall);
    // The tile that was replaced is untouched: nothing mutated in place.
    expect(beforeFrontDoor.selected).toBe(false);
    expect(liveState.entities[0].selected).toBe(true);
  });

  it('setSelected is a no-op for an unknown id and for the value it already has', () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    const before = liveState.entities;

    liveState.setSelected('sensor.never_configured', true);
    expect(liveState.entities).toBe(before);
    expect(liveState.entities).toHaveLength(2);

    liveState.setSelected('lock.front_door', false);
    expect(liveState.entities).toBe(before);

    liveState.setSelected('lock.front_door', true);
    const selected = liveState.entities;

    liveState.setSelected('lock.front_door', true);
    expect(liveState.entities).toBe(selected);
  });

  it('isSelected answers false for an entity the configuration does not know', () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);

    expect(liveState.isSelected('sensor.never_configured')).toBe(false);
    expect(liveState.isSelected('lock.front_door')).toBe(false);
  });
});

describe('cached snapshot restore', () => {
  function cachedPayload(): CachePayload {
    return {
      configVersion: 7,
      entities: [
        {
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
          selected: false
        }
      ],
      buttons: [{ id: 1, name: 'Front Door Chime', slug: 'front-door', showOnDashboard: true }],
      health: { ha: 'connected', mqtt: 'connected', audio: 'ready', detail: null }
    };
  }

  it('restores as stale, with a snapshot and the restored flag, never live', () => {
    liveState.restoreCached(cachedPayload());

    expect(liveState.stale).toBe(true);
    expect(liveState.hasSnapshot).toBe(true);
    expect(liveState.restoredFromCache).toBe(true);
    expect(liveState.connection.status).toBe('idle');
    expect(liveState.connection.stale).toBe(true);
    expect(liveState.entities[0].entityId).toBe('lock.front_door');
    expect(liveState.buttons).toHaveLength(1);
    expect(liveState.configVersion).toBe(7);
  });

  it('a live snapshot clears the restored flag and the staleness', () => {
    liveState.restoreCached(cachedPayload());

    liveState.applySnapshot(SNAPSHOT_FRAME.data);

    expect(liveState.restoredFromCache).toBe(false);
    expect(liveState.stale).toBe(false);
  });

  it('markDisconnected keeps the restored flag', () => {
    liveState.restoreCached(cachedPayload());

    liveState.markDisconnected();

    expect(liveState.restoredFromCache).toBe(true);
    expect(liveState.stale).toBe(true);
  });

  it('reset clears the restored flag', () => {
    liveState.restoreCached(cachedPayload());

    liveState.reset();

    expect(liveState.restoredFromCache).toBe(false);
    expect(liveState.hasSnapshot).toBe(false);
    expect(liveState.stale).toBe(true);
  });

  it('cachePayload is null before a snapshot and reflects the view shapes after', () => {
    expect(liveState.cachePayload()).toBeNull();

    liveState.applySnapshot(SNAPSHOT_FRAME.data);

    const payload = liveState.cachePayload();
    expect(payload).not.toBeNull();
    expect(payload?.configVersion).toBe(7);
    expect(payload?.entities.map((tile) => tile.entityId)).toEqual([
      'lock.front_door',
      'light.hall'
    ]);
    expect(payload?.health).toEqual({
      ha: 'not_configured',
      mqtt: 'reconnecting',
      audio: 'ready',
      detail: null
    });
    expect(payload?.buttons).toHaveLength(1);
  });
});
