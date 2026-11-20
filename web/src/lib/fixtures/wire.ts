/**
 * Wire frames recorded from the real backend, not invented.
 *
 * Recorded 2026-11-15 from a throwaway in-process client (fastapi `TestClient`,
 * temp data dir) that created two entities and one button through the REST API,
 * opened a real `/ws` connection and captured the `snapshot` frame, then drove
 * `StateHub.notify_entity_state` / `notify_press` / `notify_health` plus
 * `flush_once()` to capture the remaining frames. The JSON is pasted verbatim;
 * only the wrapper (typed consts) was added. The two `entity_state` frames were
 * re-recorded when the hub started carrying the presentation block (HSD-7oto),
 * and the display words plus the three tone fields were updated for HSD-d5x2
 * (the backend now title-cases the word and resolves the tone:
 * `tone`, `state_tones`, `colour_by_state`). A row with no live state carries
 * `tone: null`, which "No state" already covers.
 *
 * The tests replay these frames, so a change to the backend contract shows up
 * as a failing test rather than as a broken tile.
 */

import type {
  ConfigChangedMessage,
  EntityStateMessage,
  HeartbeatMessage,
  HealthMessage,
  PressMessage,
  SnapshotMessage,
  WireMessage
} from '../api.js';

export const SNAPSHOT_FRAME: SnapshotMessage = {
  type: 'snapshot',
  data: {
    entities: [
      {
        id: 1,
        entity_id: 'lock.front_door',
        display_name: 'Front Door',
        state: null,
        attributes: {},
        icon: 'mdi:door',
        icon_source: 'user',
        accent_color: '#4f9cf9',
        last_changed: null,
        displayed_state: null,
        unit_of_measurement: null,
        attribute_hints: [],
        tone: null,
        state_tones: null,
        colour_by_state: true
      },
      {
        id: 2,
        entity_id: 'light.hall',
        display_name: 'Hall Light',
        state: null,
        attributes: {},
        icon: 'mdi:lightbulb',
        icon_source: 'fallback',
        accent_color: null,
        last_changed: null,
        displayed_state: null,
        unit_of_measurement: null,
        attribute_hints: [],
        tone: null,
        state_tones: null,
        colour_by_state: true
      }
    ],
    buttons: [
      {
        id: 1,
        name: 'Front Door Chime',
        slug: 'front-door',
        show_on_dashboard: true
      }
    ],
    health: {
      ha: 'not_configured',
      mqtt: 'reconnecting',
      audio: 'ready'
    },
    config_version: 7
  }
};

export const CONFIG_CHANGED_ENTITIES_FRAME: ConfigChangedMessage = {
  type: 'config_changed',
  data: {
    what: 'entities'
  }
};

export const CONFIG_CHANGED_BUTTONS_FRAME: ConfigChangedMessage = {
  type: 'config_changed',
  data: {
    what: 'buttons'
  }
};

export const ENTITY_STATE_UNLOCKED_FRAME: EntityStateMessage = {
  type: 'entity_state',
  data: {
    entity_id: 'lock.front_door',
    state: 'unlocked',
    attributes: {
      battery_level: 87
    },
    last_changed: '2026-11-15T02:11:04.512+00:00',
    displayed_state: 'Unlocked',
    unit_of_measurement: null,
    attribute_hints: [],
    tone: 'alert'
  }
};

export const ENTITY_STATE_LOCKED_FRAME: EntityStateMessage = {
  type: 'entity_state',
  data: {
    entity_id: 'lock.front_door',
    state: 'locked',
    attributes: {
      battery_level: 86
    },
    last_changed: '2026-11-15T02:12:30.900+00:00',
    displayed_state: 'Locked',
    unit_of_measurement: null,
    attribute_hints: [],
    tone: 'ok'
  }
};

export const PRESS_QUEUED_FRAME: PressMessage = {
  type: 'press',
  data: {
    button_id: 1,
    button_name: 'Front Door Chime',
    outcome: 'queued',
    pressed_at: '2026-11-15T02:13:00.100+00:00'
  }
};

export const PRESS_PLAYED_FRAME: PressMessage = {
  type: 'press',
  data: {
    button_id: 1,
    button_name: 'Front Door Chime',
    outcome: 'played',
    pressed_at: '2026-11-15T02:13:00.100+00:00'
  }
};

/** Real hub frame from `src/chime_deck/hub.py`'s DEFAULT_HEARTBEAT_INTERVAL. */
export const HEARTBEAT_FRAME: HeartbeatMessage = {
  type: 'heartbeat',
  data: {
    interval_s: 5.0
  }
};

export const HEALTH_FRAME: HealthMessage = {
  type: 'health',
  data: {
    ha: 'reconnecting',
    mqtt: 'reconnecting',
    audio: 'ready',
    detail: 'Home Assistant unreachable (timeout)'
  }
};

export const CONFIG_CHANGED_LAST_FRAME: ConfigChangedMessage = {
  type: 'config_changed',
  data: {
    what: 'entities'
  }
};

/** Every recorded frame, in capture order. */
export const WIRE_FRAMES: WireMessage[] = [
  SNAPSHOT_FRAME,
  CONFIG_CHANGED_ENTITIES_FRAME,
  CONFIG_CHANGED_BUTTONS_FRAME,
  ENTITY_STATE_UNLOCKED_FRAME,
  ENTITY_STATE_LOCKED_FRAME,
  PRESS_QUEUED_FRAME,
  PRESS_PLAYED_FRAME,
  HEALTH_FRAME,
  CONFIG_CHANGED_LAST_FRAME
];
