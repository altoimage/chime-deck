import { afterEach, describe, expect, it } from 'vitest';

import {
  ApiError,
  createButton,
  createEntity,
  deleteButton,
  deleteEntity,
  describeHaEntity,
  getHealth,
  getIconPath,
  getSettings,
  getStateToneDefaults,
  getTtsEngines,
  listAudioDevices,
  listButtonRows,
  listButtons,
  listCustomIcons,
  listEntities,
  listEntityRows,
  listSounds,
  patchSettings,
  pressButton,
  readServerClock,
  reorderEntities,
  republishAll,
  request,
  searchHaEntities,
  searchIcons,
  setFetchImpl,
  setUnauthorizedHandler,
  testAudio,
  testHa,
  testMqtt,
  toggleTileSelection,
  toEntityTile,
  toEntityTileFromResponse,
  updateButton,
  updateEntity,
  type AudioDevicesResponse,
  type EntityResponse,
  type EntityRow,
  type FetchLike,
  type FetchResponse
} from './api.js';

interface Call {
  path: string;
  init?: RequestInit;
}

interface Fake {
  calls: Call[];
  impl: FetchLike;
}

function response(body: unknown, status = 200): FetchResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body)
  };
}

function unparseable(status: number): FetchResponse {
  return {
    ok: false,
    status,
    json: () => Promise.reject(new SyntaxError('Unexpected token < in JSON'))
  };
}

/** A fake answer carrying response headers, case-insensitively like a real `Response`. */
function withHeaders(answer: FetchResponse, headers: Record<string, string>): FetchResponse {
  return {
    ...answer,
    headers: { get: (name) => headers[name.toLowerCase()] ?? null }
  };
}

function fakeFetch(...answers: FetchResponse[]): Fake {
  const calls: Call[] = [];
  const impl: FetchLike = (path, init) => {
    const answer = answers[calls.length] ?? answers[answers.length - 1];
    calls.push({ path, init });
    return Promise.resolve(answer);
  };
  return { calls, impl };
}

const ENTITY_ROW: EntityResponse = {
  id: 1,
  entity_id: 'lock.front_door',
  display_name: 'Front Door',
  icon: 'mdi:door',
  icon_source: 'user',
  icon_override: 'mdi:door',
  accent_color: null,
  show_attributes: ['battery_level'],
  hide_when_unavailable: false,
  sort_order: 0,
  created_at: '2026-01-01T00:00:00+00:00',
  state: null,
  displayed_state: null,
  unit_of_measurement: null,
  attribute_hints: [{ name: 'battery_level', label: 'Battery', value: '87 %' }],
  tone: null,
  state_tones: null,
  colour_by_state: true,
  warning: null
};

afterEach(() => {
  setFetchImpl(null);
  setUnauthorizedHandler(null);
});

describe('success', () => {
  it('maps an entity row onto the view type with exactly one request', async () => {
    const fake = fakeFetch(response([ENTITY_ROW]));
    setFetchImpl(fake.impl);

    const tiles = await listEntities();

    expect(tiles).toEqual([
      {
        id: 1,
        entityId: 'lock.front_door',
        displayName: 'Front Door',
        state: null,
        attributes: {},
        icon: 'mdi:door',
        iconSource: 'user',
        accentColor: null,
        lastChanged: null,
        displayedState: null,
        unitOfMeasurement: null,
        attributeHints: [{ name: 'battery_level', label: 'Battery', value: '87 %' }],
        tone: null,
        hideWhenUnavailable: false,
        selected: false
      }
    ]);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/entities');
  });

  it('maps the resolved tone onto the view type, and the row map only on the row', () => {
    const row = { ...ENTITY_ROW, tone: 'warn', state_tones: { locked: 'warn' }, colour_by_state: true };

    expect(toEntityTileFromResponse(row).tone).toBe('warn');
    expect(toEntityTile({ ...SNAPSHOT_ROW, tone: 'alert' }).tone).toBe('alert');
    // The tile carries the tone alone: painting it does not need the map, and a
    // view type that carried one would invite a second resolver.
    expect(Object.prototype.hasOwnProperty.call(toEntityTileFromResponse(row), 'stateTones')).toBe(
      false
    );
    // An older backend omits the key entirely; the view type then knows `null`
    // rather than `undefined`.
    const olderRow: Partial<EntityResponse> = { ...row };
    delete olderRow.tone;
    expect(toEntityTileFromResponse(olderRow as EntityResponse).tone).toBeNull();
  });

  it('maps the hide_when_unavailable flag onto the view type', () => {
    expect(toEntityTileFromResponse({ ...ENTITY_ROW, hide_when_unavailable: true }).hideWhenUnavailable).toBe(
      true
    );
    expect(toEntityTileFromResponse(ENTITY_ROW).hideWhenUnavailable).toBe(false);
  });

  it('maps a button row onto the view type', async () => {
    const fake = fakeFetch(
      response([{ id: 1, name: 'Front Door Chime', slug: 'front-door', show_on_dashboard: true }])
    );
    setFetchImpl(fake.impl);

    await expect(listButtons()).resolves.toEqual([
      { id: 1, name: 'Front Door Chime', slug: 'front-door', showOnDashboard: true }
    ]);
    expect(fake.calls[0].path).toBe('/api/buttons');
  });

  it('reads the liveness probe from /api/health', async () => {
    const fake = fakeFetch(response({ status: 'ok', version: '1.0.0', uptime_s: 12.5 }));
    setFetchImpl(fake.impl);

    await expect(getHealth()).resolves.toEqual({ status: 'ok', version: '1.0.0', uptime_s: 12.5 });
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/health');
  });

  it('reads the settings with exactly one GET and no body', async () => {
    const body = { settings: { 'audio.volume': 70 }, secrets: { 'ha.token': false } };
    const fake = fakeFetch(response(body));
    setFetchImpl(fake.impl);

    await expect(getSettings()).resolves.toEqual(body);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/settings');
    expect(fake.calls[0].init?.method).toBe('GET');
    expect(fake.calls[0].init?.body).toBeUndefined();
  });

  it('patches settings with the flat key→value map itself as the body', async () => {
    const body = { settings: { 'audio.volume': 55 }, secrets: {} };
    const fake = fakeFetch(response(body));
    setFetchImpl(fake.impl);

    await expect(patchSettings({ 'audio.volume': 55 })).resolves.toEqual(body);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/settings');
    expect(fake.calls[0].init?.method).toBe('PATCH');
    expect(fake.calls[0].init?.body).toBe('{"audio.volume":55}');
    const headers = fake.calls[0].init?.headers as Record<string, string>;
    expect(headers['content-type']).toBe('application/json');
    expect(headers.accept).toBe('application/json');
  });

  it('merges caller headers over the default accept instead of replacing it', async () => {
    const fake = fakeFetch(response({ ok: true }));
    setFetchImpl(fake.impl);

    await request<{ ok: boolean }>('/api/probe', {
      method: 'PUT',
      headers: { 'x-trace': 'abc' }
    });

    const headers = fake.calls[0].init?.headers as Record<string, string>;
    expect(headers.accept).toBe('application/json');
    expect(headers['x-trace']).toBe('abc');
    // A caller may still override the default explicitly.
    const override = fakeFetch(response({ ok: true }));
    setFetchImpl(override.impl);
    await request('/api/probe', { headers: { accept: 'text/plain' } });
    expect((override.calls[0].init?.headers as Record<string, string>).accept).toBe('text/plain');
  });

  it('tests Home Assistant at /api/settings/test/ha with POST and no body', async () => {
    const verdict = { ok: true, ha_version: '2024.10.1', entity_count: 42, error: null };
    const fake = fakeFetch(response(verdict));
    setFetchImpl(fake.impl);

    await expect(testHa()).resolves.toEqual(verdict);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/settings/test/ha');
    expect(fake.calls[0].init?.method).toBe('POST');
    expect(fake.calls[0].init?.body).toBeUndefined();
    const headers = fake.calls[0].init?.headers as Record<string, string>;
    expect(headers['content-type']).toBeUndefined();
  });

  it('tests MQTT at /api/settings/test/mqtt with POST and no body', async () => {
    const verdict = {
      ok: false,
      broker_version: null,
      error: 'the broker refused the connection',
      auth_failed: false
    };
    const fake = fakeFetch(response(verdict));
    setFetchImpl(fake.impl);

    await expect(testMqtt()).resolves.toEqual(verdict);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/settings/test/mqtt');
    expect(fake.calls[0].init?.method).toBe('POST');
    expect(fake.calls[0].init?.body).toBeUndefined();
  });

  it('re-announces at /api/mqtt/republish with POST and no body', async () => {
    const result = { ok: true, announced: 3, queued: false, failed: 0, error: null };
    const fake = fakeFetch(response(result));
    setFetchImpl(fake.impl);

    await expect(republishAll()).resolves.toEqual(result);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/mqtt/republish');
    expect(fake.calls[0].init?.method).toBe('POST');
    expect(fake.calls[0].init?.body).toBeUndefined();
    const headers = fake.calls[0].init?.headers as Record<string, string>;
    expect(headers['content-type']).toBeUndefined();
  });

  it('posts the audio test with no body and returns the verdict, ok false included', async () => {
    const verdict = { ok: false, device: null, latency_ms: null, error: 'no output device' };
    const fake = fakeFetch(response(verdict));
    setFetchImpl(fake.impl);

    await expect(testAudio()).resolves.toEqual(verdict);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/settings/test/audio');
    expect(fake.calls[0].init?.method).toBe('POST');
    expect(fake.calls[0].init?.body).toBeUndefined();
  });

  it('presses one button by id, encoded, with no body', async () => {
    const result = {
      button_id: 7,
      button_name: 'Front Door Chime',
      source: 'ui_test',
      outcome: 'played',
      pressed_at: '2026-11-15T02:13:00.100+00:00',
      code: null,
      detail: null
    };
    const fake = fakeFetch(response(result));
    setFetchImpl(fake.impl);

    await expect(pressButton(7)).resolves.toEqual(result);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/buttons/7/press');
    expect(fake.calls[0].init?.method).toBe('POST');
    expect(fake.calls[0].init?.body).toBeUndefined();
  });
});

describe('failures', () => {
  it('turns the error envelope into an ApiError with the server message verbatim', async () => {
    const message =
      "entity_id must look like 'domain.object_id' — lower-case letters, digits and underscores, e.g. 'lock.front_door'";
    const fake = fakeFetch(
      response({ error: { code: 'validation_error', message, detail: { field: 'entity_id' } } }, 422)
    );
    setFetchImpl(fake.impl);

    const failure = await listEntities().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    const apiError = failure as ApiError;
    expect(apiError.code).toBe('validation_error');
    expect(apiError.message).toBe(message);
    expect(apiError.status).toBe(422);
    expect(apiError.detail).toEqual({ field: 'entity_id' });
    expect(fake.calls).toHaveLength(1);
  });

  it('turns a non-JSON error body into an ApiError, never a raw SyntaxError', async () => {
    const fake = fakeFetch(unparseable(502));
    setFetchImpl(fake.impl);

    const failure = await listButtons().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    const apiError = failure as ApiError;
    expect(apiError.status).toBe(502);
    expect(apiError.code).toBe('invalid_response');
    expect(apiError.message).toContain('502');
    expect(fake.calls).toHaveLength(1);
  });

  it('turns an unparseable success body into an ApiError', async () => {
    const fake = fakeFetch({
      ok: true,
      status: 200,
      json: () => Promise.reject(new SyntaxError('Unexpected end of JSON input'))
    });
    setFetchImpl(fake.impl);

    const failure = await listEntities().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).code).toBe('invalid_response');
  });

  it('names a missing envelope instead of inventing a message', async () => {
    const fake = fakeFetch(response({ detail: 'plain fastapi shape' }, 500));
    setFetchImpl(fake.impl);

    const failure = await listEntities().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).code).toBe('invalid_response');
    expect((failure as ApiError).message).toContain('500');
  });

  it('reports an unreachable server as an ApiError with status 0', async () => {
    const calls: Call[] = [];
    setFetchImpl((path) => {
      calls.push({ path });
      return Promise.reject(new TypeError('fetch failed'));
    });

    const failure = await getHealth().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(0);
    expect((failure as ApiError).code).toBe('network_error');
    expect(calls).toHaveLength(1);
  });

  it('never retries: two calls mean two requests', async () => {
    const fake = fakeFetch(response([ENTITY_ROW]), response([]));
    setFetchImpl(fake.impl);

    await listEntities().catch(() => undefined);
    await listEntities().catch(() => undefined);

    expect(fake.calls).toHaveLength(2);
  });

  it('turns a rejected settings patch into an ApiError with the server message', async () => {
    const message = "setting 'ui.dashboard_columns' expects an integer";
    const fake = fakeFetch(
      response({ error: { code: 'invalid_setting', message, detail: { fields: [] } } }, 422)
    );
    setFetchImpl(fake.impl);

    const failure = await patchSettings({ 'ui.dashboard_columns': 9 }).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).message).toBe(message);
    expect((failure as ApiError).status).toBe(422);
    expect(fake.calls).toHaveLength(1);
  });

  it('turns a missing button into an ApiError, one request only', async () => {
    const fake = fakeFetch(
      response({ error: { code: 'not_found', message: 'there is no button with id 99', detail: null } }, 404)
    );
    setFetchImpl(fake.impl);

    const failure = await pressButton(99).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).code).toBe('not_found');
    expect(fake.calls).toHaveLength(1);
  });
});

/* ---- the login-prompt seam (HSD-0jke) ----------------------------------- */

describe('unauthorized hook', () => {
  const UNAUTHORIZED = {
    error: {
      code: 'unauthorized',
      message: 'this doorbell requires a shared token',
      detail: null
    }
  };

  it('raises the handler once and still rejects with the same ApiError', async () => {
    const fake = fakeFetch(response(UNAUTHORIZED, 401));
    setFetchImpl(fake.impl);
    let raised = 0;
    setUnauthorizedHandler(() => {
      raised += 1;
    });

    const failure = await listEntities().catch((error: unknown) => error);

    expect(raised).toBe(1);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(401);
    expect((failure as ApiError).code).toBe('unauthorized');
    expect(fake.calls).toHaveLength(1);
  });

  it('stays silent for a 500 even when an envelope carries a code', async () => {
    const fake = fakeFetch(
      response({ error: { code: 'internal_error', message: 'boom', detail: null } }, 500)
    );
    setFetchImpl(fake.impl);
    let raised = 0;
    setUnauthorizedHandler(() => {
      raised += 1;
    });

    const failure = await listEntities().catch((error: unknown) => error);

    expect(raised).toBe(0);
    expect((failure as ApiError).status).toBe(500);
    expect((failure as ApiError).code).toBe('internal_error');
  });

  it('stays silent for a 401 that is not the unauthorized code', async () => {
    const fake = fakeFetch(
      response({ error: { code: 'rate_limited', message: 'slow down', detail: null } }, 401)
    );
    setFetchImpl(fake.impl);
    let raised = 0;
    setUnauthorizedHandler(() => {
      raised += 1;
    });

    const failure = await listEntities().catch((error: unknown) => error);

    expect(raised).toBe(0);
    expect((failure as ApiError).code).toBe('rate_limited');
  });

  it('stops raising once the handler is cleared', async () => {
    const fake = fakeFetch(response(UNAUTHORIZED, 401));
    setFetchImpl(fake.impl);
    let raised = 0;
    setUnauthorizedHandler(() => {
      raised += 1;
    });

    await listEntities().catch(() => undefined);
    setUnauthorizedHandler(null);
    await listEntities().catch(() => undefined);

    expect(raised).toBe(1);
    expect(fake.calls).toHaveLength(2);
  });

  it('raises from readServerClock too, and still rejects', async () => {
    const fake = fakeFetch(response(UNAUTHORIZED, 401));
    setFetchImpl(fake.impl);
    let raised = 0;
    setUnauthorizedHandler(() => {
      raised += 1;
    });

    const failure = await readServerClock().catch((error: unknown) => error);

    expect(raised).toBe(1);
    expect((failure as ApiError).status).toBe(401);
    expect((failure as ApiError).code).toBe('unauthorized');
    expect(fake.calls).toHaveLength(1);
  });
});

/* ---- the entity editor's CRUD surface (HSD-umw) ------------------------- */

describe('entity rows and CRUD', () => {
  it('lists the raw rows with exactly one GET, warning included', async () => {
    const warned = { ...ENTITY_ROW, warning: 'the stored icon is not a usable file' };
    const fake = fakeFetch(response([ENTITY_ROW, warned]));
    setFetchImpl(fake.impl);

    await expect(listEntityRows()).resolves.toEqual([ENTITY_ROW, warned]);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/entities');
    expect(fake.calls[0].init?.method).toBe('GET');
    expect(fake.calls[0].init?.body).toBeUndefined();
  });

  it('creates at POST /api/entities with the body as JSON', async () => {
    const body = { entity_id: 'lock.front_door', display_name: 'Front Door' };
    const fake = fakeFetch(response(ENTITY_ROW, 201));
    setFetchImpl(fake.impl);

    await expect(createEntity(body)).resolves.toEqual(ENTITY_ROW);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/entities');
    expect(fake.calls[0].init?.method).toBe('POST');
    expect(fake.calls[0].init?.body).toBe(JSON.stringify(body));
    const headers = fake.calls[0].init?.headers as Record<string, string>;
    expect(headers['content-type']).toBe('application/json');
    expect(headers.accept).toBe('application/json');
  });

  it('patches one entity by id, sending only the override', async () => {
    const fake = fakeFetch(response(ENTITY_ROW));
    setFetchImpl(fake.impl);

    await expect(updateEntity(7, { icon: 'mdi:door' })).resolves.toEqual(ENTITY_ROW);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/entities/7');
    expect(fake.calls[0].init?.method).toBe('PATCH');
    expect(fake.calls[0].init?.body).toBe('{"icon":"mdi:door"}');
    const headers = fake.calls[0].init?.headers as Record<string, string>;
    expect(headers['content-type']).toBe('application/json');
  });

  it('deletes one entity by id and returns the deleted row it names', async () => {
    const answer = { deleted: true, id: 7, entity_id: 'lock.front_door' };
    const fake = fakeFetch(response(answer));
    setFetchImpl(fake.impl);

    await expect(deleteEntity(7)).resolves.toEqual(answer);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/entities/7');
    expect(fake.calls[0].init?.method).toBe('DELETE');
    expect(fake.calls[0].init?.body).toBeUndefined();
  });

  it('reorders by sending the whole id list', async () => {
    const fake = fakeFetch(response({ ids: [3, 1, 2] }));
    setFetchImpl(fake.impl);

    await expect(reorderEntities([3, 1, 2])).resolves.toEqual({ ids: [3, 1, 2] });
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/entities/reorder');
    expect(fake.calls[0].init?.method).toBe('POST');
    expect(fake.calls[0].init?.body).toBe('{"ids":[3,1,2]}');
  });

  it('reads the tone vocabulary and the built-in table with one body-less GET', async () => {
    const answer = { tones: ['ok', 'warn', 'alert', 'off'], defaults: { running: 'warn' } };
    const fake = fakeFetch(response(answer));
    setFetchImpl(fake.impl);

    await expect(getStateToneDefaults()).resolves.toEqual(answer);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/entities/state-tones');
    expect(fake.calls[0].init?.method).toBe('GET');
    expect(fake.calls[0].init?.body).toBeUndefined();
  });

  it('turns a rejected create into an ApiError with the server sentence', async () => {
    const message = "entity_id must look like 'domain.object_id' — lower-case letters, digits and underscores, e.g. 'lock.front_door'";
    const fake = fakeFetch(
      response({ error: { code: 'validation_error', message, detail: { field: 'entity_id' } } }, 422)
    );
    setFetchImpl(fake.impl);

    const failure = await createEntity({ entity_id: 'Front Door' }).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).message).toBe(message);
    expect(fake.calls).toHaveLength(1);
  });
});

/* ---- tile selection (D29, HSD-g4xa) ------------------------------------ */

/** A `snapshot` frame entity row: the hub shape, `selected` alongside the rest. */
const SNAPSHOT_ROW: EntityRow = {
  id: 1,
  entity_id: 'lock.front_door',
  display_name: 'Front Door',
  state: 'locked',
  attributes: { battery_level: 87 },
  icon: 'mdi:door',
  icon_source: 'user',
  accent_color: '#4f9cf9',
  last_changed: '2026-11-15T02:11:04.512+00:00',
  displayed_state: 'Locked',
  unit_of_measurement: null,
  attribute_hints: [],
  tone: 'ok',
  state_tones: null,
  colour_by_state: true
};

describe('tile selection', () => {
  it('maps a selected snapshot row and defaults an absent field to unselected', () => {
    expect(toEntityTile({ ...SNAPSHOT_ROW, selected: true }).selected).toBe(true);
    expect(toEntityTile({ ...SNAPSHOT_ROW, selected: false }).selected).toBe(false);
    // The field is newer than the frame that carries it: an older backend omits
    // it and a tile nobody tapped must not arrive lit.
    expect(toEntityTile(SNAPSHOT_ROW).selected).toBe(false);
  });

  it('always answers selected: false from a REST row, even one carrying the key', () => {
    expect(toEntityTileFromResponse(ENTITY_ROW).selected).toBe(false);
    // `/api/entities` has no selection field by design, so a row object that
    // somehow carried one must not resurrect a selection on a config import.
    const hypothetical = { ...ENTITY_ROW, selected: true } as EntityResponse;
    expect(toEntityTileFromResponse(hypothetical).selected).toBe(false);
  });

  it('toggles with a body-less POST to /api/tiles/{id}/selection', async () => {
    const answer = { id: 7, entity_id: 'lock.front_door', selected: true };
    const fake = fakeFetch(response(answer));
    setFetchImpl(fake.impl);

    await expect(toggleTileSelection(7)).resolves.toEqual(answer);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/tiles/7/selection');
    expect(fake.calls[0].init?.method).toBe('POST');
    expect(fake.calls[0].init?.body).toBeUndefined();
  });

  it('turns a rejected toggle into an ApiError carrying the server sentence', async () => {
    const message = 'no entity has the id 7';
    const fake = fakeFetch(
      response({ error: { code: 'not_found', message, detail: { id: 7 } } }, 404)
    );
    setFetchImpl(fake.impl);

    const failure = await toggleTileSelection(7).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).message).toBe(message);
    expect((failure as ApiError).status).toBe(404);
    expect(fake.calls).toHaveLength(1);
  });
});

/* ---- Home Assistant pickers (HSD-umw) ----------------------------------- */

const HA_ROW = {
  entity_id: 'lock.front_door',
  name: 'Front Door',
  state: 'locked',
  icon: 'mdi:lock',
  icon_source: 'ha-registry',
  domain: 'lock',
  device_class: 'lock'
};

describe('Home Assistant lookups', () => {
  it('searches with q and the default limit in the query string', async () => {
    const fake = fakeFetch(response({ entities: [HA_ROW], warning: null }));
    setFetchImpl(fake.impl);

    await expect(searchHaEntities('door')).resolves.toEqual({ entities: [HA_ROW], warning: null });
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/ha/entities?q=door&limit=50');
    expect(fake.calls[0].init?.method).toBe('GET');
  });

  it('encodes the search term and passes a caller limit', async () => {
    const fake = fakeFetch(response({ entities: [], warning: null }));
    setFetchImpl(fake.impl);

    await searchHaEntities('front door', 25);

    expect(fake.calls[0].path).toBe('/api/ha/entities?q=front+door&limit=25');
  });

  it('passes a warning-through untouched, and null is a real "no matches"', async () => {
    const sentence =
      'Home Assistant did not answer the live lookup, so this entity’s current state is unknown — check ha.base_url and ha.token in Settings, then try again';

    let fake = fakeFetch(response({ entities: [], warning: sentence }));
    setFetchImpl(fake.impl);
    await expect(searchHaEntities('')).resolves.toEqual({ entities: [], warning: sentence });

    fake = fakeFetch(response({ entities: [], warning: null }));
    setFetchImpl(fake.impl);
    await expect(searchHaEntities('nothing-matches-this')).resolves.toEqual({ entities: [], warning: null });
  });

  it('describes one entity live, with the id encoded, found three-state included', async () => {
    const detail = {
      ...HA_ROW,
      attributes: { friendly_name: 'Front Door', battery_level: 87 },
      last_changed: '2026-05-05T10:00:00+00:00',
      found: null,
      warning: 'Home Assistant did not answer the live lookup'
    };
    const fake = fakeFetch(response(detail));
    setFetchImpl(fake.impl);

    await expect(describeHaEntity('lock/front door')).resolves.toEqual(detail);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/ha/entities/lock%2Ffront%20door');
    expect(fake.calls[0].init?.method).toBe('GET');
  });

  it('keeps found false distinguishable from found null', async () => {
    const fake = fakeFetch(
      response({ ...HA_ROW, attributes: {}, last_changed: null, found: false, warning: 'Home Assistant does not know it' })
    );
    setFetchImpl(fake.impl);

    await expect(describeHaEntity('lock.nowhere')).resolves.toMatchObject({ found: false });
  });
});

/* ---- icon pickers (HSD-umw) --------------------------------------------- */

describe('icon lookups', () => {
  it('searches icons with q and the default limit', async () => {
    const body = {
      version: '7.4.47',
      total: 7447,
      matched: 12,
      icons: [{ name: 'mdi:door', path: 'M0 0H24V24' }]
    };
    const fake = fakeFetch(response(body));
    setFetchImpl(fake.impl);

    await expect(searchIcons('door')).resolves.toEqual(body);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/icons/search?q=door&limit=120');
  });

  it('passes a caller limit through', async () => {
    const fake = fakeFetch(response({ version: '7.4.47', total: 1, matched: 1, icons: [] }));
    setFetchImpl(fake.impl);

    await searchIcons('', 200);

    expect(fake.calls[0].path).toBe('/api/icons/search?q=&limit=200');
  });

  it('fetches one icon path with the name encoded', async () => {
    const answer = { name: 'mdi:door', path: 'M0 0H24V24', view_box: '0 0 24 24' };
    const fake = fakeFetch(response(answer));
    setFetchImpl(fake.impl);

    await expect(getIconPath('mdi:door')).resolves.toEqual(answer);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/icon/mdi%3Adoor');
  });

  it('turns an icon the set does not ship into an ApiError, one request only', async () => {
    const fake = fakeFetch(
      response({ error: { code: 'not_found', message: 'the set does not contain this icon', detail: null } }, 404)
    );
    setFetchImpl(fake.impl);

    const failure = await getIconPath('mdi:nope').catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(404);
    expect(fake.calls).toHaveLength(1);
  });

  it('lists the custom icons, an empty listing included', async () => {
    let fake = fakeFetch(response({ total: 0, icons: [], skipped: [] }));
    setFetchImpl(fake.impl);
    await expect(listCustomIcons()).resolves.toEqual({ total: 0, icons: [], skipped: [] });
    expect(fake.calls[0].path).toBe('/api/icons/custom');

    const body = {
      total: 1,
      icons: [{ name: 'cat.svg', reference: 'custom:cat.svg', bytes: 512 }],
      skipped: [{ name: 'evil.svg', reason: 'the file contains a script and cannot be served safely' }]
    };
    fake = fakeFetch(response(body));
    setFetchImpl(fake.impl);
    await expect(listCustomIcons()).resolves.toEqual(body);
    expect(fake.calls[0].path).toBe('/api/icons/custom');
  });
});

/* ---- the button editor's CRUD surface (HSD-bw8h) ------------------------ */

const BUTTON_DETAIL = {
  id: 7,
  slug: 'front-door',
  name: 'Front Door',
  icon: 'mdi:door',
  action_type: 'sound',
  sound_id: 3,
  tts_text: null,
  tts_engine: null,
  volume_override: null,
  dedupe_seconds: null,
  show_on_dashboard: true,
  quiet_hours_exempt: false,
  enabled: true,
  sort_order: 1,
  created_at: '2026-01-01T00:00:00+00:00',
  updated_at: '2026-01-01T00:00:00+00:00',
  mqtt: { command: 'doorbell/front-door/press', discovery: 'homeassistant/button/front-door/config' },
  slug_change: null
};

describe('button rows and CRUD', () => {
  it('lists the full stored rows with exactly one GET, mqtt included', async () => {
    const fake = fakeFetch(response([BUTTON_DETAIL]));
    setFetchImpl(fake.impl);

    const rows = await listButtonRows();

    expect(rows).toEqual([BUTTON_DETAIL]);
    expect(rows[0].mqtt?.command).toBe('doorbell/front-door/press');
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/buttons');
    expect(fake.calls[0].init?.method).toBe('GET');
    expect(fake.calls[0].init?.body).toBeUndefined();
  });

  it('creates at POST /api/buttons with the body as JSON', async () => {
    const body = { name: 'Front Door', action_type: 'sound', sound_id: 3 };
    const fake = fakeFetch(response(BUTTON_DETAIL, 201));
    setFetchImpl(fake.impl);

    await expect(createButton(body)).resolves.toEqual(BUTTON_DETAIL);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/buttons');
    expect(fake.calls[0].init?.method).toBe('POST');
    expect(fake.calls[0].init?.body).toBe(JSON.stringify(body));
    const headers = fake.calls[0].init?.headers as Record<string, string>;
    expect(headers['content-type']).toBe('application/json');
    expect(headers.accept).toBe('application/json');
  });

  it('patches one button by id and reads the rename report back', async () => {
    const slugChange = {
      old_slug: 'front-door',
      new_slug: 'porch',
      old_topics: null,
      new_topics: null,
      warning: "Home Assistant will see a NEW entity for slug 'porch'"
    };
    const fake = fakeFetch(response({ ...BUTTON_DETAIL, slug: 'porch', slug_change: slugChange }));
    setFetchImpl(fake.impl);

    const updated = await updateButton(7, { slug: 'porch' });

    expect(updated.slug_change).toEqual(slugChange);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/buttons/7');
    expect(fake.calls[0].init?.method).toBe('PATCH');
    expect(fake.calls[0].init?.body).toBe('{"slug":"porch"}');
    const headers = fake.calls[0].init?.headers as Record<string, string>;
    expect(headers['content-type']).toBe('application/json');
  });

  it('deletes one button by id and returns the row it names', async () => {
    const answer = { deleted: true, id: 7, slug: 'front-door' };
    const fake = fakeFetch(response(answer));
    setFetchImpl(fake.impl);

    await expect(deleteButton(7)).resolves.toEqual(answer);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/buttons/7');
    expect(fake.calls[0].init?.method).toBe('DELETE');
    expect(fake.calls[0].init?.body).toBeUndefined();
  });

  it('keeps the old list route and the press route untouched', async () => {
    const fake = fakeFetch(
      response([{ id: 1, name: 'Chime', slug: 'chime', show_on_dashboard: false }]),
      response({
        button_id: 7,
        button_name: 'Front Door',
        source: 'ui_test',
        outcome: 'played',
        pressed_at: '2026-01-01T00:00:00+00:00',
        code: null,
        detail: 'engine piper'
      })
    );
    setFetchImpl(fake.impl);

    await expect(listButtons()).resolves.toEqual([
      { id: 1, name: 'Chime', slug: 'chime', showOnDashboard: false }
    ]);
    const press = await pressButton(7);

    expect(press.detail).toBe('engine piper');
    expect(fake.calls[1].path).toBe('/api/buttons/7/press');
    expect(fake.calls[1].init?.method).toBe('POST');
  });

  it('turns a rejected patch into an ApiError with the server sentence and field', async () => {
    const message =
      'a sound button requires a sound_id — choose a sound from the library, or make this button speak';
    const fake = fakeFetch(
      response({ error: { code: 'validation_error', message, detail: { field: 'sound_id' } } }, 422)
    );
    setFetchImpl(fake.impl);

    const failure = await updateButton(7, { sound_id: null }).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).message).toBe(message);
    expect((failure as ApiError).detail).toEqual({ field: 'sound_id' });
    expect(fake.calls).toHaveLength(1);
  });
});

/* ---- the sound library and the speech engines (HSD-bw8h) ---------------- */

const SOUND_ROW = {
  id: 3,
  name: 'Ding dong',
  source_type: 'file',
  path: 'ding.mp3',
  url: null,
  sha256: 'a'.repeat(64),
  format: 'mp3',
  duration_ms: 1500,
  bytes: 24000,
  missing: false,
  created_at: '2026-01-01T00:00:00+00:00'
};

describe('sounds and speech engines', () => {
  it('lists the sound library, a missing file included', async () => {
    const missing = { ...SOUND_ROW, id: 4, name: 'Gone', missing: true };
    const fake = fakeFetch(response([SOUND_ROW, missing]));
    setFetchImpl(fake.impl);

    const rows = await listSounds();

    expect(rows).toEqual([SOUND_ROW, missing]);
    expect(rows[1].missing).toBe(true);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/sounds');
    expect(fake.calls[0].init?.method).toBe('GET');
  });

  it('reads the engine envelope, the off state included', async () => {
    const answer = {
      disabled: false,
      engines: [{ name: 'piper', available: true, reason: null, last_synthesis_ms: 120 }],
      warning: null
    };
    let fake = fakeFetch(response(answer));
    setFetchImpl(fake.impl);

    await expect(getTtsEngines()).resolves.toEqual(answer);
    expect(fake.calls[0].path).toBe('/api/tts/engines');
    expect(fake.calls[0].init?.method).toBe('GET');

    const off = { disabled: true, engines: [], warning: 'speech is switched off' };
    fake = fakeFetch(response(off));
    setFetchImpl(fake.impl);
    await expect(getTtsEngines()).resolves.toEqual(off);
  });
});

/* ---- the device picker and the device clock (HSD-wpm0) ------------------- */

const DEVICE_ANSWER: AudioDevicesResponse = {
  ok: true,
  hint: 'usb',
  resolved_device: 'alsa/hw:CARD=Speaker,DEV=0',
  system_default: 'auto',
  devices: [
    { name: 'auto', hostapi: 'auto', is_default: true },
    { name: 'alsa/hw:CARD=Speaker,DEV=0', hostapi: 'alsa', is_default: false }
  ],
  error: null
};

const DATE_HEADER = 'Wed, 12 Nov 2026 21:14:07 GMT';
const DATE_EPOCH_MS = Date.UTC(2026, 10, 12, 21, 14, 7);

describe('audio devices', () => {
  it('reads the discovery answer from /api/audio/devices with exactly one GET', async () => {
    const fake = fakeFetch(response(DEVICE_ANSWER));
    setFetchImpl(fake.impl);

    await expect(listAudioDevices()).resolves.toEqual(DEVICE_ANSWER);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/audio/devices');
    expect(fake.calls[0].init?.method).toBe('GET');
    expect(fake.calls[0].init?.body).toBeUndefined();
  });
});

describe('readServerClock', () => {
  it('parses the device clock from the Date header and stamps the client inside the call', async () => {
    const fake = fakeFetch(
      withHeaders(response({ status: 'ok', version: '0.1.0', uptime_s: 90 }), { date: DATE_HEADER })
    );
    setFetchImpl(fake.impl);

    const before = Date.now();
    const reading = await readServerClock();
    const after = Date.now();

    expect(reading.server_time_ms).toBe(DATE_EPOCH_MS);
    expect(reading.client_time_ms).toBeGreaterThanOrEqual(before);
    expect(reading.client_time_ms).toBeLessThanOrEqual(after);
    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/health');
    expect(fake.calls[0].init?.method).toBe('GET');
  });

  it('answers null when the response carries no headers object at all', async () => {
    const fake = fakeFetch(response({ status: 'ok', version: '0.1.0', uptime_s: 90 }));
    setFetchImpl(fake.impl);

    const reading = await readServerClock();

    expect(reading.server_time_ms).toBeNull();
    expect(Number.isFinite(reading.client_time_ms)).toBe(true);
    expect(fake.calls).toHaveLength(1);
  });

  it('answers null for a missing header and for an unparseable one', async () => {
    let fake = fakeFetch(withHeaders(response({ status: 'ok', version: '0.1.0', uptime_s: 1 }), {}));
    setFetchImpl(fake.impl);

    await expect(readServerClock()).resolves.toEqual({
      server_time_ms: null,
      client_time_ms: expect.any(Number)
    });
    expect(fake.calls).toHaveLength(1);

    fake = fakeFetch(
      withHeaders(response({ status: 'ok', version: '0.1.0', uptime_s: 1 }), { date: 'yesterday' })
    );
    setFetchImpl(fake.impl);

    const reading = await readServerClock();

    expect(reading.server_time_ms).toBeNull();
    expect(fake.calls).toHaveLength(1);
  });

  it('turns a non-ok answer into the ApiError the server envelope describes', async () => {
    const message = 'the health probe is unavailable';
    const fake = fakeFetch(
      withHeaders(
        response({ error: { code: 'internal_error', message, detail: null } }, 503),
        { date: DATE_HEADER }
      )
    );
    setFetchImpl(fake.impl);

    const failure = await readServerClock().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).message).toBe(message);
    expect((failure as ApiError).status).toBe(503);
    expect((failure as ApiError).code).toBe('internal_error');
    expect(fake.calls).toHaveLength(1);
  });

  it('reports an unreachable server as the network ApiError, with one request only', async () => {
    const calls: Call[] = [];
    setFetchImpl((path) => {
      calls.push({ path });
      return Promise.reject(new TypeError('fetch failed'));
    });

    const failure = await readServerClock().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(0);
    expect((failure as ApiError).code).toBe('network_error');
    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe('/api/health');
  });
});
