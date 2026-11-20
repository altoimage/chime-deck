/**
 * The first-run wizard's routing truth (HSD-vl7t), proved in the default
 * `node` environment: no DOM, no runes, no component.
 *
 * Why a separate file from the wizard's DOM tests: these four rules are what
 * decides whether a browser is hijacked at all, and a rule that can only be
 * proved through a mounted component is a rule nobody will re-check. The
 * redirect, the dismissal, the exit marker and the LAN hint are pure functions
 * of a settings snapshot, so they are tested as such — the DOM test then only
 * has to prove that the wizard renders what these functions decided.
 *
 * Offline by contract: every snapshot comes from `setFetchImpl`, so no test can
 * reach a real install.
 */

import { afterEach, describe, expect, it } from 'vitest';

import { type FetchLike, type FetchResponse, setFetchImpl } from './api.js';
import { settings } from './settings.svelte.js';
import {
  isSetupPath,
  lanSetupHint,
  setupDoneUrl,
  setupOutcome,
  setupRedirectAfterLoad,
  setupStateFrom
} from './setup.js';

function jsonResponse(body: unknown, status = 200): FetchResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body
  };
}

/** A backend whose `GET /api/settings` answers one fixed snapshot. */
function settingsResponds(
  snapshot: Record<string, unknown>,
  secrets: Record<string, boolean>
): void {
  const impl: FetchLike = async () => jsonResponse({ settings: snapshot, secrets });
  setFetchImpl(impl);
}

/** A backend that never answered: the load fails and the state stays unknown. */
const unreachable: FetchLike = async () => {
  throw new Error('connection refused');
};

afterEach(() => {
  settings.reset();
  setFetchImpl(null);
});

/* ---- 1. a fresh install redirects ---------------------------------------- */

describe('1. a fresh install reaches the wizard', () => {
  it('sends the dashboard root to /setup when the address is empty', async () => {
    settingsResponds({ 'ha.base_url': '' }, { 'ha.token': false });

    await settings.load();

    expect(settings.loaded).toBe(true);
    expect(setupRedirectAfterLoad(settings.loaded, '/', setupStateFrom(settings))).toBe('/setup');
  });

  it('still sends the root to /setup when only the default address is stored', async () => {
    settingsResponds(
      { 'ha.base_url': 'http://homeassistant.local:8123' },
      { 'ha.token': false }
    );

    await settings.load();

    expect(setupRedirectAfterLoad(settings.loaded, '/', setupStateFrom(settings))).toBe('/setup');
  });
});

/* ---- 2. every other state declines --------------------------------------- */

describe('2. the redirect declines every other state', () => {
  it('does not redirect an install that has both a URL and a token', async () => {
    settingsResponds({ 'ha.base_url': 'http://ha.local:8123' }, { 'ha.token': true });

    await settings.load();

    expect(setupRedirectAfterLoad(settings.loaded, '/', setupStateFrom(settings))).toBeNull();
  });

  it('does not redirect an install that dismissed setup, even with no token', async () => {
    settingsResponds(
      { 'ha.base_url': 'http://ha.local:8123', 'ui.setup_dismissed': true },
      { 'ha.token': false }
    );

    await settings.load();

    expect(setupRedirectAfterLoad(settings.loaded, '/', setupStateFrom(settings))).toBeNull();
  });

  it('does not redirect from any path but the dashboard root', async () => {
    settingsResponds({ 'ha.base_url': '' }, { 'ha.token': false });

    await settings.load();

    const state = setupStateFrom(settings);
    expect(settings.loaded).toBe(true);
    expect(setupRedirectAfterLoad(settings.loaded, '/settings/connection', state)).toBeNull();
    expect(setupRedirectAfterLoad(settings.loaded, '/setup', state)).toBeNull();
  });

  it('does not redirect after a failed load, because the state is unknown', async () => {
    setFetchImpl(unreachable);

    await settings.load();

    expect(settings.loaded).toBe(false);
    expect(settings.error).not.toBeNull();
    expect(setupRedirectAfterLoad(settings.loaded, '/', setupStateFrom(settings))).toBeNull();
    // The rule is proven, not left to the caller: an unloaded store asked
    // directly still gets no redirect.
    expect(setupRedirectAfterLoad(false, '/', setupStateFrom(settings))).toBeNull();
  });
});

/* ---- 3. the exit marker and the route ------------------------------------ */

describe('3. the exit marker and the route', () => {
  it('reads only ?setup=done as the completion outcome', () => {
    expect(setupOutcome('?setup=done')).toBe('done');
    expect(setupOutcome('setup=done')).toBe('done');
    expect(setupOutcome('')).toBeNull();
    expect(setupOutcome('?setup=1')).toBeNull();
    expect(setupOutcome('?done=setup')).toBeNull();
  });

  it('treats /setup and /setup/ as the wizard and nothing else', () => {
    expect(isSetupPath('/setup')).toBe(true);
    expect(isSetupPath('/setup/')).toBe(true);
    expect(isSetupPath('/setup/1')).toBe(false);
    expect(isSetupPath('/settings')).toBe(false);
  });

  it('returns to the dashboard with the completion flag', () => {
    expect(setupDoneUrl()).toBe('/?setup=done');
  });
});

/* ---- 4. the LAN hint ----------------------------------------------------- */

describe('4. the "configure from another device" hint', () => {
  it('names no address when the server only accepts loopback connections', () => {
    expect(
      lanSetupHint({
        bindHost: '127.0.0.1',
        serverPort: 8080,
        browserHostname: 'panel.local',
        browserPort: '8080'
      })
    ).toEqual({ kind: 'loopback', url: null });

    expect(
      lanSetupHint({
        bindHost: '',
        serverPort: 8080,
        browserHostname: '127.0.0.1',
        browserPort: '8080'
      })
    ).toEqual({ kind: 'loopback', url: null });
  });

  it('uses the browser’s own host name when the page was reached over the network', () => {
    expect(
      lanSetupHint({
        bindHost: '0.0.0.0',
        serverPort: 8080,
        browserHostname: 'doorbell.lan',
        browserPort: '9000'
      })
    ).toEqual({ kind: 'network', url: 'http://doorbell.lan:9000/setup' });
  });

  it('falls back to a concrete bind address seen from loopback', () => {
    expect(
      lanSetupHint({
        bindHost: '192.168.0.50',
        serverPort: 8080,
        browserHostname: '127.0.0.1',
        browserPort: '8080'
      })
    ).toEqual({ kind: 'network', url: 'http://192.168.0.50:8080/setup' });
  });

  it('invents no address for a wildcard bind seen from loopback', () => {
    expect(
      lanSetupHint({
        bindHost: '0.0.0.0',
        serverPort: 8080,
        browserHostname: 'localhost',
        browserPort: '8080'
      })
    ).toEqual({ kind: 'network', url: null });
  });
});
