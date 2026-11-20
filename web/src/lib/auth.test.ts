// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setFetchImpl, type FetchResponse } from './api.js';
import { AuthState, setReloadImpl } from './auth.svelte.js';

function response(body: unknown, status = 200): FetchResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body)
  };
}

afterEach(() => {
  setFetchImpl(null);
  setReloadImpl(null);
});

describe('AuthState', () => {
  it('raises one prompt when promptRequired is called repeatedly', () => {
    const state = new AuthState();

    for (let index = 0; index < 10; index += 1) {
      state.promptRequired();
    }

    expect(state.required).toBe(true);
  });

  it('makes one failed POST and ignores another submit while it is in flight', async () => {
    let calls = 0;
    let finish: ((answer: FetchResponse) => void) | undefined;
    setFetchImpl((path) => {
      calls += 1;
      expect(path).toBe('/api/auth/login');
      return new Promise<FetchResponse>((resolve) => {
        finish = resolve;
      });
    });
    const state = new AuthState();
    const token = 'private-shared-token';

    const first = state.submit(token);
    expect(state.busy).toBe(true);
    await expect(state.submit('second-token')).resolves.toBe(false);
    expect(calls).toBe(1);

    if (finish === undefined) {
      throw new Error('the fake login request was not started');
    }
    finish(
      response(
        { error: { code: 'unauthorized', message: `Rejected token ${token}`, detail: {} } },
        401
      )
    );
    await expect(first).resolves.toBe(false);

    expect(calls).toBe(1);
    expect(state.busy).toBe(false);
    expect(state.error).toBe('That token was not accepted.');
    expect(state.error).not.toContain(token);
  });

  it('defaults to a shared token when status is unknown', async () => {
    const calls: Array<{ path: string; init?: RequestInit }> = [];
    setFetchImpl((path, init) => {
      calls.push({ path, init });
      return Promise.resolve(response({ authenticated: true }));
    });
    const state = new AuthState();

    await expect(state.submit('kiosk-token')).resolves.toBe(true);

    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe('/api/auth/login');
    expect(calls[0].init?.body).toBe(JSON.stringify({ token: 'kiosk-token' }));
  });

  it('posts a configuration password when status says one is set', async () => {
    const calls: Array<{ path: string; init?: RequestInit }> = [];
    setFetchImpl((path, init) => {
      calls.push({ path, init });
      if (path === '/api/auth/status') {
        return Promise.resolve(response({
          listener: 'admin',
          password_set: true,
          token_required: false,
          protected: true,
          authenticated: false
        }));
      }
      return Promise.resolve(response({ authenticated: true }));
    });
    const state = new AuthState();
    const reload = vi.fn();
    setReloadImpl(reload);
    await state.loadStatus();

    await expect(state.submit('configuration-secret')).resolves.toBe(true);

    expect(calls.map((call) => call.path)).toEqual(['/api/auth/status', '/api/auth/login']);
    expect(calls[1].init?.body).toBe(JSON.stringify({ password: 'configuration-secret' }));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('posts the token verbatim and invokes the reload hook once on success', async () => {
    const calls: Array<{ path: string; init?: RequestInit }> = [];
    setFetchImpl((path, init) => {
      calls.push({ path, init });
      return Promise.resolve(response({ authenticated: true }));
    });
    const reload = vi.fn();
    setReloadImpl(reload);
    const state = new AuthState();
    state.promptRequired();
    const token = '  exact token with spaces  ';

    await expect(state.submit(token)).resolves.toBe(true);

    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe('/api/auth/login');
    expect(calls[0].init?.method).toBe('POST');
    expect(calls[0].init?.body).toBe(JSON.stringify({ token }));
    expect(reload).toHaveBeenCalledTimes(1);
    expect(state.required).toBe(true);
  });

  it('rejects a blank token locally without making a request', async () => {
    let calls = 0;
    setFetchImpl(() => {
      calls += 1;
      return Promise.resolve(response({ authenticated: true }));
    });
    const state = new AuthState();

    await expect(state.submit('')).resolves.toBe(false);

    expect(calls).toBe(0);
    expect(state.error).toBe('Enter the shared token.');
  });

  it('uses a generic network sentence without exposing the token', async () => {
    const token = 'never-echo-this-token';
    setFetchImpl(() => Promise.reject(new TypeError(`network failed for ${token}`)));
    const state = new AuthState();

    await expect(state.submit(token)).resolves.toBe(false);

    expect(state.error).toBe('The server could not be reached. Check your connection and try again.');
    expect(state.error).not.toContain(token);
  });
});
