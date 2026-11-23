// @vitest-environment happy-dom
/**
 * The settings store, including the one DOM effect it owns besides the theme:
 * the browser title, adopted from the echo (`device.name`, HSD-vwdy). The file
 * needs a DOM for that, so it asks for `happy-dom` per file; the rest of the
 * suite keeps the default `node` environment.
 */

import { afterEach, describe, expect, it } from 'vitest';

import { type FetchLike, type FetchResponse, setFetchImpl } from './api.js';
import { scale } from './scale.svelte.js';
import { settings } from './settings.svelte.js';

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

/** One answer per call, the last one repeated (same pattern as `api.test.ts`). */
function fakeFetch(...answers: FetchResponse[]): Fake {
  const calls: Call[] = [];
  const impl: FetchLike = (path, init) => {
    const answer = answers[calls.length] ?? answers[answers.length - 1];
    calls.push({ path, init });
    return Promise.resolve(answer);
  };
  return { calls, impl };
}

const ERROR_ENVELOPE = {
  error: { code: 'validation_error', message: 'setting audio.volume expects an integer', detail: null }
};

afterEach(() => {
  settings.reset();
  setFetchImpl(null);
  document.title = '';
});

describe('defaults', () => {
  it('reads the documented defaults before any load', () => {
    expect(settings.loaded).toBe(false);
    expect(settings.error).toBeNull();
    expect(settings.showClock).toBe(true);
    expect(settings.hideCursor).toBe(true);
    expect(settings.showControlRail).toBe(true);
    expect(settings.dashboardColumns).toBe(0);
    expect(settings.volume).toBe(100);
    expect(settings.hardwareVolume).toBe(100);
    expect(settings.railTarget).toBe('software');
    expect(settings.muted).toBe(false);
  });

  it('keeps the defaults when a key is absent from the wire', async () => {
    setFetchImpl(fakeFetch(response({ settings: {}, secrets: {} })).impl);

    await settings.load();

    expect(settings.loaded).toBe(true);
    expect(settings.error).toBeNull();
    expect(settings.showClock).toBe(true);
    expect(settings.hideCursor).toBe(true);
    expect(settings.showControlRail).toBe(true);
    expect(settings.dashboardColumns).toBe(0);
    expect(settings.volume).toBe(100);
    expect(settings.hardwareVolume).toBe(100);
    expect(settings.railTarget).toBe('software');
  });

  it('ignores a value of the wrong type rather than rendering it', async () => {
    setFetchImpl(
      fakeFetch(
        response({
          settings: {
            'ui.show_clock': 'yes',
            'ui.hide_cursor': 'no',
            'ui.show_control_rail': 1,
            'ui.dashboard_columns': 2.5,
            'audio.volume': '90'
          },
          secrets: {}
        })
      ).impl
    );

    await settings.load();

    expect(settings.showClock).toBe(true);
    expect(settings.hideCursor).toBe(true);
    expect(settings.showControlRail).toBe(true);
    expect(settings.dashboardColumns).toBe(0);
    expect(settings.volume).toBe(100);
  });

  it('ignores an out-of-range column count and volume', async () => {
    setFetchImpl(
      fakeFetch(response({ settings: { 'ui.dashboard_columns': 9, 'audio.volume': 140 }, secrets: {} }))
        .impl
    );

    await settings.load();

    expect(settings.dashboardColumns).toBe(0);
    expect(settings.volume).toBe(100);
  });
});

describe('values', () => {
  it('falls back to software for an unreadable rail target', async () => {
    setFetchImpl(
      fakeFetch(response({ settings: { 'audio.rail_target': 'mixer' }, secrets: {} })).impl
    );

    await settings.load();

    expect(settings.railTarget).toBe('software');
  });

  it('honours the values the backend sent', async () => {
    setFetchImpl(
      fakeFetch(
        response({
          settings: {
            'ui.show_clock': false,
            'ui.hide_cursor': false,
            'ui.show_control_rail': false,
            'ui.dashboard_columns': 3,
            'audio.volume': 25,
            'audio.muted': true
          },
          secrets: { 'ha.token': true }
        })
      ).impl
    );

    await settings.load();

    expect(settings.loaded).toBe(true);
    expect(settings.showClock).toBe(false);
    expect(settings.hideCursor).toBe(false);
    expect(settings.showControlRail).toBe(false);
    expect(settings.dashboardColumns).toBe(3);
    expect(settings.volume).toBe(25);
    expect(settings.muted).toBe(true);
  });
});

describe('secrets', () => {
  it('stores the flags and reports hasSecret only for exactly true', async () => {
    setFetchImpl(
      fakeFetch(
        response({
          settings: {},
          secrets: { 'ha.token': true, 'mqtt.password': false }
        })
      ).impl
    );

    await settings.load();

    expect(settings.hasSecret('ha.token')).toBe(true);
    expect(settings.hasSecret('mqtt.password')).toBe(false);
    expect(settings.hasSecret('mqtt.username')).toBe(false);
  });

  it('treats a malformed flag as not stored', async () => {
    setFetchImpl(fakeFetch(response({ settings: {}, secrets: { 'ha.token': 'true' } })).impl);

    await settings.load();

    expect(settings.hasSecret('ha.token')).toBe(false);
  });

  it('clears the flags on reset', async () => {
    setFetchImpl(fakeFetch(response({ settings: {}, secrets: { 'ha.token': true } })).impl);
    await settings.load();
    expect(settings.hasSecret('ha.token')).toBe(true);

    settings.reset();

    expect(settings.hasSecret('ha.token')).toBe(false);
    expect(settings.loaded).toBe(false);
  });
});

describe('generic readers', () => {
  it('reads text, integer and flag with the documented fallbacks', async () => {
    setFetchImpl(
      fakeFetch(
        response({
          settings: { 'mqtt.host': 'broker.local', 'mqtt.port': 8883, 'mqtt.tls': true },
          secrets: {}
        })
      ).impl
    );

    await settings.load();

    expect(settings.text('mqtt.host', 'fallback')).toBe('broker.local');
    expect(settings.integer('mqtt.port', 1, 65535, 1883)).toBe(8883);
    expect(settings.flag('mqtt.tls', false)).toBe(true);
    expect(settings.text('missing.key', 'fallback')).toBe('fallback');
    expect(settings.integer('missing.key', 1, 65535, 1883)).toBe(1883);
    expect(settings.flag('missing.key', true)).toBe(true);
  });
});

describe('save', () => {
  it('sends exactly one PATCH with only the given keys and adopts the echo', async () => {
    const fake = fakeFetch(
      response({ settings: { 'ha.base_url': 'http://old:8123' }, secrets: { 'ha.token': true } }),
      response({ settings: { 'ha.base_url': 'http://new:8123' }, secrets: { 'ha.token': false } })
    );
    setFetchImpl(fake.impl);
    await settings.load();

    await expect(
      settings.save({ 'ha.base_url': 'http://new:8123', 'ha.token': null })
    ).resolves.toBe(true);

    const patches = fake.calls.filter((call) => call.init?.method === 'PATCH');
    expect(patches).toHaveLength(1);
    expect(patches[0].path).toBe('/api/settings');
    expect(JSON.parse(String(patches[0].init?.body))).toEqual({
      'ha.base_url': 'http://new:8123',
      'ha.token': null
    });
    // The echoed snapshot — settings AND secrets — is the new state.
    expect(settings.text('ha.base_url')).toBe('http://new:8123');
    expect(settings.hasSecret('ha.token')).toBe(false);
    expect(settings.error).toBeNull();
  });

  it('records the server message verbatim and returns false when the patch is rejected', async () => {
    const message = 'send null to clear a secret, not an empty string';
    setFetchImpl(
      fakeFetch(
        response({ settings: {}, secrets: { 'ha.token': true } }),
        response({ error: { code: 'invalid_setting', message, detail: null } }, 422)
      ).impl
    );
    await settings.load();

    await expect(settings.save({ 'ha.token': '' })).resolves.toBe(false);

    expect(settings.error).toBe(message);
    expect(settings.hasSecret('ha.token')).toBe(true);
  });

  it('clears the error after a successful save that follows a failure', async () => {
    const message = 'setting mqtt.port expects an integer';
    setFetchImpl(
      fakeFetch(
        response({ settings: { 'mqtt.port': 1883 }, secrets: {} }),
        response({ error: { code: 'invalid_setting', message, detail: null } }, 422),
        response({ settings: { 'mqtt.port': 8883 }, secrets: {} })
      ).impl
    );
    await settings.load();

    await expect(settings.save({ 'mqtt.port': 8883 })).resolves.toBe(false);
    expect(settings.error).toBe(message);

    await expect(settings.save({ 'mqtt.port': 8883 })).resolves.toBe(true);
    expect(settings.error).toBeNull();
    expect(settings.integer('mqtt.port', 1, 65535, 1883)).toBe(8883);
  });
});

describe('failures', () => {
  it('records the server message and keeps the defaults when the load fails', async () => {
    setFetchImpl(fakeFetch(response(ERROR_ENVELOPE, 422)).impl);

    await expect(settings.load()).resolves.toBeUndefined();

    expect(settings.loaded).toBe(false);
    expect(settings.error).toBe('setting audio.volume expects an integer');
    expect(settings.volume).toBe(100);
    expect(settings.showControlRail).toBe(true);
  });

  it('sends exactly one PATCH with the clamped volume and adopts the echo', async () => {
    const fake = fakeFetch(
      response({ settings: { 'audio.volume': 100 }, secrets: {} }),
      response(null, 500)
    );
    setFetchImpl(fake.impl);

    await expect(settings.setVolume(120)).resolves.toBe(true);

    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/settings');
    expect(fake.calls[0].init?.method).toBe('PATCH');
    expect(fake.calls[0].init?.body).toBe('{"audio.volume":100}');
    expect(settings.volume).toBe(100);
    expect(settings.error).toBeNull();
  });

  it('PATCHes exactly the selected rail volume key', async () => {
    const fake = fakeFetch(
      response({ settings: { 'audio.rail_target': 'hardware', 'audio.hardware_volume': 45 }, secrets: {} })
    );
    setFetchImpl(fake.impl);
    await settings.load();

    await expect(settings.setRailVolume(45)).resolves.toBe(true);

    expect(fake.calls).toHaveLength(2);
    expect(fake.calls[1].init?.body).toBe('{"audio.hardware_volume":45}');
  });

  it('clamps a negative volume to zero', async () => {
    const fake = fakeFetch(response({ settings: { 'audio.volume': 0 }, secrets: {} }));
    setFetchImpl(fake.impl);

    await expect(settings.setVolume(-12)).resolves.toBe(true);

    expect(fake.calls[0].init?.body).toBe('{"audio.volume":0}');
  });

  it('returns false, records the error and keeps the cached volume when the patch fails', async () => {
    setFetchImpl(
      fakeFetch(
        response({ settings: { 'audio.volume': 40 }, secrets: {} }),
        response(ERROR_ENVELOPE, 422)
      ).impl
    );
    await settings.load();
    expect(settings.volume).toBe(40);

    await expect(settings.setVolume(90)).resolves.toBe(false);

    expect(settings.error).toBe('setting audio.volume expects an integer');
    expect(settings.volume).toBe(40);
  });

  it('sends exactly one PATCH for setMuted(true) and adopts the echo', async () => {
    const fake = fakeFetch(response({ settings: { 'audio.muted': true }, secrets: {} }));
    setFetchImpl(fake.impl);

    await expect(settings.setMuted(true)).resolves.toBe(true);

    expect(fake.calls).toHaveLength(1);
    expect(fake.calls[0].path).toBe('/api/settings');
    expect(fake.calls[0].init?.method).toBe('PATCH');
    expect(fake.calls[0].init?.body).toBe('{"audio.muted":true}');
    expect(settings.muted).toBe(true);
    expect(settings.error).toBeNull();
  });

  it('returns false, records the error and leaves muted alone when the patch fails', async () => {
    setFetchImpl(
      fakeFetch(
        response({ settings: { 'audio.muted': false }, secrets: {} }),
        response(ERROR_ENVELOPE, 422)
      ).impl
    );
    await settings.load();
    expect(settings.muted).toBe(false);

    await expect(settings.setMuted(true)).resolves.toBe(false);

    expect(settings.error).toBe('setting audio.volume expects an integer');
    expect(settings.muted).toBe(false);
  });
});

describe('the browser title (HSD-vwdy)', () => {
  it('is the stored device name, and the registry default when the key is absent', async () => {
    setFetchImpl(fakeFetch(response({ settings: { 'device.name': 'Hall Doorbell' }, secrets: {} })).impl);
    await settings.load();
    expect(document.title).toBe('Hall Doorbell');

    settings.reset();
    setFetchImpl(fakeFetch(response({ settings: {}, secrets: {} })).impl);
    await settings.load();
    expect(document.title).toBe('ChimeDeck');
  });

  it('falls back to the default rather than rendering a value of the wrong type', async () => {
    setFetchImpl(fakeFetch(response({ settings: { 'device.name': 42 }, secrets: {} })).impl);
    await settings.load();
    expect(document.title).toBe('ChimeDeck');

    settings.reset();
    setFetchImpl(fakeFetch(response({ settings: { 'device.name': null }, secrets: {} })).impl);
    await settings.load();
    expect(document.title).toBe('ChimeDeck');
  });

  it('follows a PATCH echo, so a rename really renames the page', async () => {
    setFetchImpl(
      fakeFetch(
        response({ settings: { 'device.name': 'ChimeDeck' }, secrets: {} }),
        response({ settings: { 'device.name': 'Kitchen' }, secrets: {} })
      ).impl
    );
    await settings.load();
    expect(document.title).toBe('ChimeDeck');

    await expect(settings.save({ 'device.name': 'Kitchen' })).resolves.toBe(true);

    expect(document.title).toBe('Kitchen');
  });

  it('keeps the previous title when the server refuses the name', async () => {
    setFetchImpl(
      fakeFetch(
        response({ settings: { 'device.name': 'Kitchen' }, secrets: {} }),
        response(
          {
            error: {
              code: 'validation_error',
              message: 'setting device.name must contain at least one letter or digit',
              detail: null
            }
          },
          422
        )
      ).impl
    );
    await settings.load();
    expect(document.title).toBe('Kitchen');

    await expect(settings.save({ 'device.name': '---' })).resolves.toBe(false);

    // Adopted from the echo, never from the draft: a refused name must not end
    // up on the tab of a wall panel.
    expect(document.title).toBe('Kitchen');
  });
});

describe('the UI scale (HSD-nlbf)', () => {
  afterEach(() => {
    scale.set(100);
  });

  it('stamps ui.scale from the echo, and unsets it again at 100%', async () => {
    setFetchImpl(fakeFetch(response({ settings: { 'ui.scale': 130 }, secrets: {} })).impl);
    await settings.load();

    expect(settings.scale).toBe(130);
    expect(document.documentElement.dataset.uiScale).toBe('130');

    settings.reset();
    setFetchImpl(fakeFetch(response({ settings: { 'ui.scale': 100 }, secrets: {} })).impl);
    await settings.load();

    expect(document.documentElement.hasAttribute('data-ui-scale')).toBe(false);
  });

  it('degrades a value that is not an offered step to the default', async () => {
    scale.set(130);
    setFetchImpl(fakeFetch(response({ settings: { 'ui.scale': 87 }, secrets: {} })).impl);
    await settings.load();

    expect(settings.scale).toBe(100);
    expect(document.documentElement.hasAttribute('data-ui-scale')).toBe(false);
  });
});
