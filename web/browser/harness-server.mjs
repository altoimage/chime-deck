/**
 * A dependency-free local harness for the real-browser tier (HSD-0qtn phase 2).
 *
 * It does three jobs and nothing else:
 *
 *   1. serve the built SPA from `web/dist` (the shipped artifact, not a dev
 *      server), with the SPA fallback the Python app provides in production;
 *   2. answer the handful of `/api/*` GETs the dashboard needs on boot, plus a
 *      fake `/ws` that sends one `snapshot` frame and no heartbeat. That is
 *      fine because the client arms its watchdog only after a heartbeat, so
 *      the kiosk tier exercises the pre-heartbeat behaviour;
 *   3. expose `/__control/*` so a Playwright test can force an offline backend,
 *      change the tile/chime counts, and read the connection counters.
 *
 * Everything is `127.0.0.1` only and fully offline. Plain Node ESM: no
 * dependencies, so this file runs with the same interpreter that built dist.
 * The browser tests are a separate tier — `npm run verify` never starts this.
 */

import { createHash } from 'node:crypto';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

/** The RFC 6455 handshake GUID, fixed by the spec. */
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

/** `web/dist/`, resolved from this file so the cwd never matters. */
const DIST_DIR = fileURLToPath(new URL('../dist/', import.meta.url));

/** The status codes/methods this harness answers. */
const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.json': 'application/json; charset=utf-8',
  '.woff2': 'font/woff2'
};

/**
 * The entity fixture: the REST shape (`EntityResponse` in `src/lib/api.ts`), a
 * superset of what the snapshot frame carries. Ten rows, deliberately awkward:
 * one long display name, one unit, one `unavailable`, one two-word
 * `displayed_state`, a negative temperature and a humidity percentage.
 *
 * `attributes` / `last_changed` are snapshot-only and stripped from the REST
 * answer below; they are kept together so the two shapes cannot drift.
 *
 * `tone` is the colour the shipped backend resolves for the same state
 * (`chime_deck/icons/tones.py`), and it travels in both shapes: `locked` and
 * `closed` are `ok`, `on` and `open` are `warn`, `off` and every numeric reading
 * are `off`. It is spelled out per row rather than computed here because a fake
 * that omits a field the real wire always sends, or derives one its own way, is
 * how a fake starts lying about the contract it is standing in for.
 */
const ENTITY_FIXTURES = [
  {
    id: 1,
    entity_id: 'lock.front_door',
    display_name: 'Front Door',
    icon: 'mdi:lock',
    icon_source: 'user',
    icon_override: 'mdi:lock',
    accent_color: '#4f9cf9',
    show_attributes: [],
    hide_when_unavailable: false,
    state_tones: null,
    colour_by_state: true,
    sort_order: 1,
    created_at: '2026-01-02T03:04:05.000+00:00',
    state: 'locked',
    displayed_state: 'Locked',
    tone: 'ok',
    unit_of_measurement: null,
    attribute_hints: [],
    warning: null,
    attributes: { battery_level: 92 },
    last_changed: '2026-01-02T03:10:00.000+00:00'
  },
  {
    id: 2,
    entity_id: 'sensor.hallway_temperature',
    display_name: 'Hallway Temperature Sensor',
    icon: 'mdi:thermometer',
    icon_source: 'ha-registry',
    icon_override: null,
    accent_color: null,
    show_attributes: [],
    hide_when_unavailable: false,
    state_tones: null,
    colour_by_state: true,
    sort_order: 2,
    created_at: '2026-01-02T03:04:06.000+00:00',
    state: '21.4',
    displayed_state: '21.4',
    tone: 'off',
    unit_of_measurement: '°C',
    attribute_hints: [],
    warning: null,
    attributes: { battery_level: 78 },
    last_changed: '2026-01-02T03:11:00.000+00:00'
  },
  {
    id: 3,
    entity_id: 'light.kitchen_ceiling',
    display_name: 'Kitchen Ceiling',
    icon: 'mdi:lightbulb',
    icon_source: 'ha-registry',
    icon_override: null,
    accent_color: null,
    show_attributes: [],
    hide_when_unavailable: false,
    state_tones: null,
    colour_by_state: true,
    sort_order: 3,
    created_at: '2026-01-02T03:04:07.000+00:00',
    state: 'on',
    displayed_state: 'On',
    tone: 'warn',
    unit_of_measurement: null,
    attribute_hints: [],
    warning: null,
    attributes: {},
    last_changed: '2026-01-02T03:12:00.000+00:00'
  },
  {
    id: 4,
    entity_id: 'binary_sensor.garage_door',
    display_name: 'Garage Door',
    icon: 'mdi:garage',
    icon_source: 'ha-attribute',
    icon_override: null,
    accent_color: null,
    show_attributes: [],
    hide_when_unavailable: false,
    state_tones: null,
    colour_by_state: true,
    sort_order: 4,
    created_at: '2026-01-02T03:04:08.000+00:00',
    state: 'open',
    displayed_state: 'Garage Door: Open',
    tone: 'warn',
    unit_of_measurement: null,
    attribute_hints: [],
    warning: null,
    attributes: {},
    last_changed: '2026-01-02T03:13:00.000+00:00'
  },
  {
    id: 5,
    entity_id: 'sensor.outside_temperature',
    display_name: 'Outside Temperature',
    icon: 'mdi:thermometer',
    icon_source: 'ha-registry',
    icon_override: null,
    accent_color: null,
    show_attributes: [],
    hide_when_unavailable: false,
    state_tones: null,
    colour_by_state: true,
    sort_order: 5,
    created_at: '2026-01-02T03:04:09.000+00:00',
    state: '-3.2',
    displayed_state: '-3.2',
    tone: 'off',
    unit_of_measurement: '°C',
    attribute_hints: [],
    warning: null,
    attributes: {},
    last_changed: '2026-01-02T03:14:00.000+00:00'
  },
  {
    id: 6,
    entity_id: 'lock.back_door',
    display_name: 'Back Door',
    icon: 'mdi:lock',
    icon_source: 'fallback',
    icon_override: null,
    accent_color: null,
    show_attributes: [],
    hide_when_unavailable: false,
    state_tones: null,
    colour_by_state: true,
    sort_order: 6,
    created_at: '2026-01-02T03:04:10.000+00:00',
    state: 'unavailable',
    displayed_state: null,
    tone: 'off',
    unit_of_measurement: null,
    attribute_hints: [],
    warning: null,
    attributes: {},
    last_changed: '2026-01-02T03:15:00.000+00:00'
  },
  {
    id: 7,
    entity_id: 'light.porch',
    display_name: 'Porch Light',
    icon: 'mdi:lightbulb-outline',
    icon_source: 'ha-registry',
    icon_override: null,
    accent_color: null,
    show_attributes: [],
    hide_when_unavailable: false,
    state_tones: null,
    colour_by_state: true,
    sort_order: 7,
    created_at: '2026-01-02T03:04:11.000+00:00',
    state: 'off',
    displayed_state: 'Off',
    tone: 'off',
    unit_of_measurement: null,
    attribute_hints: [],
    warning: null,
    attributes: {},
    last_changed: '2026-01-02T03:16:00.000+00:00'
  },
  {
    id: 8,
    entity_id: 'binary_sensor.side_gate',
    display_name: 'Side Gate',
    icon: 'mdi:door-open',
    icon_source: 'ha-registry',
    icon_override: null,
    accent_color: null,
    show_attributes: [],
    hide_when_unavailable: false,
    state_tones: null,
    colour_by_state: true,
    sort_order: 8,
    created_at: '2026-01-02T03:04:12.000+00:00',
    state: 'closed',
    displayed_state: 'Closed',
    tone: 'ok',
    unit_of_measurement: null,
    attribute_hints: [],
    warning: null,
    attributes: {},
    last_changed: '2026-01-02T03:17:00.000+00:00'
  },
  {
    id: 9,
    entity_id: 'sensor.living_room_humidity',
    display_name: 'Living Room Humidity',
    icon: 'mdi:water',
    icon_source: 'ha-registry',
    icon_override: null,
    accent_color: null,
    show_attributes: [],
    hide_when_unavailable: false,
    state_tones: null,
    colour_by_state: true,
    sort_order: 9,
    created_at: '2026-01-02T03:04:13.000+00:00',
    state: '48',
    displayed_state: '48',
    tone: 'off',
    unit_of_measurement: '%',
    attribute_hints: [],
    warning: null,
    attributes: {},
    last_changed: '2026-01-02T03:18:00.000+00:00'
  },
  {
    id: 10,
    entity_id: 'switch.hallway_lamp',
    display_name: 'Hallway Lamp',
    icon: 'mdi:lightbulb-outline',
    icon_source: 'ha-registry',
    icon_override: null,
    accent_color: null,
    show_attributes: [],
    hide_when_unavailable: false,
    state_tones: null,
    colour_by_state: true,
    sort_order: 10,
    created_at: '2026-01-02T03:04:14.000+00:00',
    state: 'off',
    displayed_state: 'Off',
    tone: 'off',
    unit_of_measurement: null,
    attribute_hints: [],
    warning: null,
    attributes: {},
    last_changed: '2026-01-02T03:19:00.000+00:00'
  }
];

/** The button fixture: the full stored row (`ButtonResponse` in api.ts). */
const BUTTON_FIXTURES = [
  {
    id: 1,
    slug: 'front-door',
    name: 'Front Door Chime',
    icon: null,
    action_type: 'sound',
    sound_id: 1,
    tts_text: null,
    tts_engine: null,
    volume_override: null,
    dedupe_seconds: 5,
    show_on_dashboard: true,
    quiet_hours_exempt: false,
    enabled: true,
    sort_order: 1,
    created_at: '2026-01-02T03:04:15.000+00:00',
    updated_at: '2026-01-02T03:04:15.000+00:00',
    mqtt: { command: 'chime-deck/button/front-door/press', discovery: 'homeassistant/device/front-door/config' },
    slug_change: null
  },
  {
    id: 2,
    slug: 'back-door',
    name: 'Back Door Chime',
    icon: null,
    action_type: 'tts',
    sound_id: null,
    tts_text: 'Someone is at the back door',
    tts_engine: 'piper',
    volume_override: 80,
    dedupe_seconds: 5,
    show_on_dashboard: true,
    quiet_hours_exempt: true,
    enabled: true,
    sort_order: 2,
    created_at: '2026-01-02T03:04:16.000+00:00',
    updated_at: '2026-01-02T03:04:16.000+00:00',
    mqtt: { command: 'chime-deck/button/back-door/press', discovery: 'homeassistant/device/back-door/config' },
    slug_change: null
  },
  {
    id: 3,
    slug: 'garage',
    name: 'Garage Chime',
    icon: null,
    action_type: 'sound',
    sound_id: 2,
    tts_text: null,
    tts_engine: null,
    volume_override: null,
    dedupe_seconds: null,
    show_on_dashboard: true,
    quiet_hours_exempt: false,
    enabled: true,
    sort_order: 3,
    created_at: '2026-01-02T03:04:17.000+00:00',
    updated_at: '2026-01-02T03:04:17.000+00:00',
    mqtt: { command: 'chime-deck/button/garage/press', discovery: 'homeassistant/device/garage/config' },
    slug_change: null
  },
  {
    id: 4,
    slug: 'porch',
    name: 'Porch Chime',
    icon: null,
    action_type: 'sound',
    sound_id: 3,
    tts_text: null,
    tts_engine: null,
    volume_override: null,
    dedupe_seconds: 5,
    show_on_dashboard: true,
    quiet_hours_exempt: false,
    enabled: true,
    sort_order: 4,
    created_at: '2026-01-02T03:04:18.000+00:00',
    updated_at: '2026-01-02T03:04:18.000+00:00',
    mqtt: { command: 'chime-deck/button/porch/press', discovery: 'homeassistant/device/porch/config' },
    slug_change: null
  }
];

/** Rows for the REST answer: drop the snapshot-only fields. */
function restEntityRows(count) {
  return sliceCycle(ENTITY_FIXTURES, count).map((fixture) => {
    const { attributes: _attributes, last_changed: _lastChanged, ...rest } = fixture;
    return rest;
  });
}

/** Rows for the snapshot frame: the `EntityRow` shape the store maps. */
function snapshotEntityRows(count) {
  return sliceCycle(ENTITY_FIXTURES, count).map((fixture) => ({
    id: fixture.id,
    entity_id: fixture.entity_id,
    display_name: fixture.display_name,
    state: fixture.state,
    attributes: { ...fixture.attributes },
    icon: fixture.icon,
    icon_source: fixture.icon_source,
    accent_color: fixture.accent_color,
    last_changed: fixture.last_changed,
    displayed_state: fixture.displayed_state,
    tone: fixture.tone,
    unit_of_measurement: fixture.unit_of_measurement,
    attribute_hints: fixture.attribute_hints,
    hide_when_unavailable: fixture.hide_when_unavailable
  }));
}

/** Buttons for the snapshot frame: the four-field `ButtonRow`. */
function buttonRows(count) {
  return sliceCycle(BUTTON_FIXTURES, count).map((fixture) => ({
    id: fixture.id,
    name: fixture.name,
    slug: fixture.slug,
    show_on_dashboard: fixture.show_on_dashboard
  }));
}

/** The full stored button rows for `GET /api/buttons`. */
function restButtonRows(count) {
  return sliceCycle(BUTTON_FIXTURES, count).map((fixture) => ({ ...fixture }));
}

/**
 * Take *count* rows from a fixture table, cycling when a test asks for more
 * rows than the table has. Cycled rows get a unique id/entity id so the store's
 * `{#each ... (tile.entityId)}` key never collides.
 */
function sliceCycle(table, count) {
  const wanted = Number.isInteger(count) && count > 0 ? count : 0;
  const rows = [];
  for (let index = 0; index < wanted; index += 1) {
    const base = table[index % table.length];
    if (index < table.length) {
      rows.push(base);
      continue;
    }
    const cycle = Math.floor(index / table.length);
    rows.push({
      ...base,
      id: base.id + cycle * 1000,
      entity_id: `${base.entity_id}_${index + 1}`,
      sort_order: base.sort_order + cycle * 1000
    });
  }
  return rows;
}

/**
 * The settings the fake serves. `audio.muted` belongs here because the dashboard
 * rail reads it: without the key the mute button could only ever render "Mute",
 * so the browser tier could not prove a tap flips it.
 */
const DEFAULT_SETTINGS = {
  'ui.show_clock': true,
  'ui.show_control_rail': true,
  'ui.dashboard_columns': 0,
  'ui.setup_dismissed': true,
  'audio.volume': 70,
  'audio.muted': false
};

/** The one snapshot frame, with `config_version` bumped per connection. */
function buildSnapshot(state) {
  const frame = {
    type: 'snapshot',
    data: {
      entities: snapshotEntityRows(state.tiles),
      buttons: buttonRows(state.chimes),
      health: { ha: 'connected', mqtt: 'connected', audio: 'ready' },
      config_version: state.snapshotSeq
    }
  };
  state.snapshotSeq += 1;
  return frame;
}

/** Encode one unmasked server→client text frame (7-bit or 16-bit length). */
function encodeTextFrame(text) {
  const payload = Buffer.from(text, 'utf8');
  const length = payload.length;
  if (length < 126) {
    const header = Buffer.from([0x81, length]);
    return Buffer.concat([header, payload]);
  }
  if (length <= 0xffff) {
    const header = Buffer.alloc(4);
    header[0] = 0x81;
    header[1] = 126;
    header.writeUInt16BE(length, 2);
    return Buffer.concat([header, payload]);
  }
  throw new RangeError('harness frame payload exceeds 64 KiB');
}

/**
 * A stateful frame skipper for the client→server direction. The real backend
 * ignores inbound frames; so does this, but the bytes must still be consumed
 * frame-by-frame or a later frame would be parsed as garbage.
 */
function createFrameSkipper() {
  let buffered = Buffer.alloc(0);
  return (chunk) => {
    buffered = Buffer.concat([buffered, chunk]);
    for (;;) {
      if (buffered.length < 2) {
        return;
      }
      const masked = (buffered[1] & 0x80) !== 0;
      let length = buffered[1] & 0x7f;
      let offset = 2;
      if (length === 126) {
        if (buffered.length < 4) {
          return;
        }
        length = buffered.readUInt16BE(2);
        offset = 4;
      } else if (length === 127) {
        if (buffered.length < 10) {
          return;
        }
        const big = buffered.readBigUInt64BE(2);
        if (big > BigInt(Number.MAX_SAFE_INTEGER)) {
          buffered = Buffer.alloc(0);
          return;
        }
        length = Number(big);
        offset = 10;
      }
      const frameLength = offset + (masked ? 4 : 0) + length;
      if (buffered.length < frameLength) {
        return;
      }
      buffered = buffered.subarray(frameLength);
    }
  };
}

/** Read a small JSON control body; never let an oversized one grow memory. */
function readJsonBody(request) {
  return new Promise((resolveBody, rejectBody) => {
    const chunks = [];
    let size = 0;
    request.on('data', (chunk) => {
      size += chunk.length;
      if (size > 65536) {
        rejectBody(new Error('control body too large'));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on('end', () => {
      if (chunks.length === 0) {
        resolveBody({});
        return;
      }
      try {
        resolveBody(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch (error) {
        rejectBody(error);
      }
    });
    request.on('error', rejectBody);
  });
}

/**
 * Build the harness. Returns the HTTP server, the mutable state a test can
 * inspect, and idempotent `listen`/`close` wrappers.
 *
 * @param {{ distDir?: string }} [options]
 */
export function createHarnessServer(options = {}) {
  const distRoot = resolve(options.distDir ?? DIST_DIR);
  const indexPath = join(distRoot, 'index.html');

  const state = {
    online: true,
    tiles: 4,
    chimes: 1,
    /** Accept API connections but never answer (a hung backend). */
    hang: false,
    wsConnections: 0,
    snapshotSeq: 0
  };

  /**
   * The settings map `GET /api/settings` serves and `PATCH` merges into.
   *
   * A fake that answers `{}` to a mutation is not benign: a client adopts the
   * echo as its whole snapshot, so an empty echo silently wipes every setting
   * the page holds. The rail's taps then cannot be tested, and the fake claims
   * a backend contract the real one does not have.
   */
  let settings = { ...DEFAULT_SETTINGS };

  /** Every open TCP socket, upgraded or not, so `close()` cannot leak one. */
  const sockets = new Set();
  /** The subset that completed the websocket handshake. */
  const wsSockets = new Set();

  let closing = null;
  let boundPort = 0;

  function statusBody() {
    return {
      online: state.online,
      hang: state.hang,
      tiles: state.tiles,
      chimes: state.chimes,
      wsConnections: state.wsConnections,
      openSockets: sockets.size
    };
  }

  function sendJson(response, status, body, extraHeaders = {}) {
    if (response.writableEnded) {
      return;
    }
    const payload = JSON.stringify(body);
    response.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      'content-length': Buffer.byteLength(payload),
      ...extraHeaders
    });
    response.end(payload);
  }

  function sendOffline(response) {
    sendJson(response, 503, {
      error: { code: 'unavailable', message: 'harness is offline', detail: null }
    });
  }

  /** Apply a `POST /__control/state` body; unknown keys are ignored. */
  function applyState(body) {
    if (typeof body.online === 'boolean') {
      state.online = body.online;
      if (!state.online) {
        for (const socket of wsSockets) {
          wsSockets.delete(socket);
          socket.end();
        }
      }
    }
    if (typeof body.hang === 'boolean') {
      state.hang = body.hang;
    }
    if (Number.isInteger(body.tiles) && body.tiles >= 0) {
      state.tiles = body.tiles;
    }
    if (Number.isInteger(body.chimes) && body.chimes >= 0) {
      state.chimes = body.chimes;
    }
  }

  /**
   * `/__control/*`. Returns true when the request was handled here, so the
   * static/API router below never sees it. `reset` returns online=true,
   * tiles=4, chimes=1 and **resets `wsConnections` to 0** (the counter is
   * per-scenario); `snapshotSeq` stays monotonic.
   */
  async function handleControl(request, response, pathname) {
    if (pathname === '/__control/status') {
      if (request.method !== 'GET' && request.method !== 'HEAD') {
        sendJson(response, 405, { error: { code: 'method_not_allowed', message: 'use GET', detail: null } });
        return true;
      }
      sendJson(response, 200, statusBody());
      return true;
    }
    if (pathname === '/__control/reset') {
      if (request.method !== 'POST') {
        sendJson(response, 405, { error: { code: 'method_not_allowed', message: 'use POST', detail: null } });
        return true;
      }
      state.online = true;
      state.hang = false;
      state.tiles = 4;
      state.chimes = 1;
      state.wsConnections = 0;
      settings = { ...DEFAULT_SETTINGS };
      sendJson(response, 200, statusBody());
      return true;
    }
    if (pathname === '/__control/state') {
      if (request.method !== 'POST') {
        sendJson(response, 405, { error: { code: 'method_not_allowed', message: 'use POST', detail: null } });
        return true;
      }
      let body;
      try {
        body = await readJsonBody(request);
      } catch {
        sendJson(response, 400, {
          error: { code: 'bad_request', message: 'control body is not JSON', detail: null }
        });
        return true;
      }
      applyState(body !== null && typeof body === 'object' ? body : {});
      sendJson(response, 200, statusBody());
      return true;
    }
    return false;
  }

  /** The fake backend's API routes. Returns true when it answered. */
  async function handleApi(request, response, pathname) {
    if (!pathname.startsWith('/api/')) {
      return false;
    }
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      if (pathname === '/api/settings' && request.method === 'PATCH') {
        let body;
        try {
          body = await readJsonBody(request);
        } catch {
          sendJson(response, 400, {
            error: { code: 'bad_request', message: 'settings body is not JSON', detail: null }
          });
          return true;
        }
        if (body !== null && typeof body === 'object' && !Array.isArray(body)) {
          settings = { ...settings, ...body };
        }
        // The real backend answers the whole effective config, so the client
        // can adopt it as its source of truth. Echoing `{}` would delete it.
        sendJson(response, 200, { settings, secrets: {} });
        return true;
      }
      // Every other mutation is not implemented; a benign body is enough.
      sendJson(response, 200, {});
      return true;
    }
    if (!state.online) {
      sendOffline(response);
      return true;
    }
    if (state.hang) {
      // A backend that accepts the connection and never answers. The socket is
      // already in `sockets`, so `close()` cannot leak it; the browser's fetch
      // stays pending until the page navigates away.
      return true;
    }
    if (pathname === '/api/settings') {
      // `ui.setup_dismissed` is required for the dashboard to mount: without a
      // configured Home Assistant (secrets: {}) the real shell would redirect
      // `/` to `/setup`. The harness is a configured install.
      sendJson(response, 200, {
        settings,
        secrets: {}
      });
      return true;
    }
    if (pathname === '/api/health') {
      sendJson(
        response,
        200,
        { status: 'ok', version: '0.0.0-harness', uptime_s: 1 },
        { date: new Date().toUTCString() }
      );
      return true;
    }
    if (pathname === '/api/entities') {
      sendJson(response, 200, restEntityRows(state.tiles));
      return true;
    }
    if (pathname === '/api/buttons') {
      sendJson(response, 200, restButtonRows(state.chimes));
      return true;
    }
    // Any other GET, including `/api/icon/<name>`: a benign empty object.
    sendJson(response, 200, {});
    return true;
  }

  /** Serve one file from dist, or the SPA fallback. Traversal is a 404. */
  function serveStatic(request, response, pathname) {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      response.writeHead(405, { 'cache-control': 'no-store', 'content-length': 0 });
      response.end();
      return;
    }
    if (pathname === '/favicon.ico') {
      // 204, no body: a browser never logs a 404 for the implicit favicon.
      response.writeHead(204, { 'cache-control': 'no-store' });
      response.end();
      return;
    }

    let decoded;
    try {
      decoded = decodeURIComponent(pathname);
    } catch {
      response.writeHead(404, { 'cache-control': 'no-store', 'content-length': 0 });
      response.end();
      return;
    }

    const relative = decoded.replace(/^\/+/, '');
    const target = resolve(distRoot, relative === '' ? 'index.html' : relative);
    const inside = target === distRoot || target.startsWith(distRoot + sep);
    if (!inside) {
      response.writeHead(404, { 'cache-control': 'no-store', 'content-length': 0 });
      response.end();
      return;
    }

    let filePath = target;
    if (existsSync(filePath) && statSync(filePath).isDirectory()) {
      filePath = join(filePath, 'index.html');
    }
    if (!existsSync(filePath) || !statSync(filePath).isFile()) {
      // SPA contract: unknown paths paint the app shell with a 200.
      filePath = indexPath;
    }

    let body;
    try {
      body = readFileSync(filePath);
    } catch {
      response.writeHead(500, { 'cache-control': 'no-store', 'content-length': 0 });
      response.end();
      return;
    }
    const contentType = CONTENT_TYPES[extname(filePath).toLowerCase()] ?? 'application/octet-stream';
    response.writeHead(200, {
      'content-type': contentType,
      'cache-control': 'no-store',
      'content-length': body.length
    });
    if (request.method === 'HEAD') {
      response.end();
      return;
    }
    response.end(body);
  }

  const server = createServer((request, response) => {
    const url = new URL(request.url ?? '/', 'http://127.0.0.1');
    void (async () => {
      try {
        if (await handleControl(request, response, url.pathname)) {
          return;
        }
        if (await handleApi(request, response, url.pathname)) {
          return;
        }
        serveStatic(request, response, url.pathname);
      } catch {
        if (!response.writableEnded) {
          response.writeHead(500, { 'cache-control': 'no-store', 'content-length': 0 });
          response.end();
        }
      }
    })();
  });

  server.on('connection', (socket) => {
    sockets.add(socket);
    socket.on('close', () => {
      sockets.delete(socket);
      wsSockets.delete(socket);
    });
    // A browser that closes abruptly must not become an unhandled 'error'.
    socket.on('error', () => {});
  });

  server.on('upgrade', (request, socket) => {
    const url = new URL(request.url ?? '/', 'http://127.0.0.1');
    if (url.pathname !== '/ws' || !state.online) {
      socket.write('HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
      socket.end();
      socket.destroy();
      return;
    }

    const key = request.headers['sec-websocket-key'];
    const upgrade = request.headers.upgrade;
    if (typeof key !== 'string' || typeof upgrade !== 'string' || upgrade.toLowerCase() !== 'websocket') {
      socket.write('HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
      socket.end();
      socket.destroy();
      return;
    }

    const accept = createHash('sha1').update(key + WS_GUID).digest('base64');
    socket.write(
      'HTTP/1.1 101 Switching Protocols\r\n' +
        'Upgrade: websocket\r\n' +
        'Connection: Upgrade\r\n' +
        `Sec-WebSocket-Accept: ${accept}\r\n\r\n`
    );

    wsSockets.add(socket);
    state.wsConnections += 1;

    const skip = createFrameSkipper();
    socket.on('data', skip);

    socket.write(encodeTextFrame(JSON.stringify(buildSnapshot(state))));
  });

  async function listen(port = 0) {
    if (closing !== null) {
      throw new Error('harness server is closed');
    }
    return await new Promise((resolvePort, rejectListen) => {
      const onError = (error) => {
        server.off('listening', onListening);
        rejectListen(error);
      };
      const onListening = () => {
        server.off('error', onError);
        const address = server.address();
        boundPort = typeof address === 'object' && address !== null ? address.port : port;
        resolvePort(boundPort);
      };
      server.once('error', onError);
      server.once('listening', onListening);
      server.listen(port, '127.0.0.1');
    });
  }

  /**
   * Destroy every open socket (upgraded or not) and close the server. Safe to
   * call twice; the second call returns the first call's promise.
   */
  async function close() {
    if (closing !== null) {
      return await closing;
    }
    closing = new Promise((resolveClose) => {
      for (const socket of sockets) {
        socket.destroy();
      }
      sockets.clear();
      wsSockets.clear();
      if (!server.listening) {
        resolveClose();
        return;
      }
      server.close(() => resolveClose());
      // Keep-alive connections would otherwise hold `close()` open.
      server.closeAllConnections?.();
    });
    return await closing;
  }

  return {
    server,
    get port() {
      return boundPort;
    },
    state,
    listen,
    close
  };
}

/** `--name <value>` from argv, or the fallback. */
function readNumberArg(name, fallback) {
  const index = process.argv.indexOf(name);
  if (index === -1) {
    return fallback;
  }
  const value = Number(process.argv[index + 1]);
  return Number.isInteger(value) && value >= 0 ? value : fallback;
}

/* ---- direct execution: a standalone harness for manual poking ---- */

const directEntry =
  process.argv[1] !== undefined && resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (directEntry) {
  const port = readNumberArg('--port', 4173);
  const harness = createHarnessServer();
  try {
    const actual = await harness.listen(port);
    process.stdout.write(`harness listening on http://127.0.0.1:${actual}\n`);
    const shutdown = () => {
      void harness.close().then(() => process.exit(0));
    };
    process.on('SIGINT', shutdown);
    process.on('SIGTERM', shutdown);
  } catch (error) {
    process.stderr.write(`harness failed to listen: ${String(error)}\n`);
    process.exit(1);
  }
}
