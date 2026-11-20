// @vitest-environment happy-dom
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  setFetchImpl,
  setUnauthorizedHandler,
  type FetchLike,
  type FetchResponse
} from '../api.js';
import { auth } from '../auth.svelte.js';
import SecurityPanel from './SecurityPanel.svelte';

interface Call {
  path: string;
  init?: RequestInit;
}

function response(body: unknown, status = 200): FetchResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body
  };
}

function backend(
  handle: (path: string, init?: RequestInit) => FetchResponse | Promise<FetchResponse>
): Call[] {
  const calls: Call[] = [];
  const impl: FetchLike = async (path, init) => {
    calls.push({ path, init });
    return handle(path, init);
  };
  setFetchImpl(impl);
  return calls;
}

const status = (passwordSet: boolean): unknown => ({
  listener: 'admin',
  password_set: passwordSet,
  token_required: false,
  protected: passwordSet,
  authenticated: true
});

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function render(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(SecurityPanel, { target }));
  flushSync();
  return target;
}

function unmountAll(): void {
  for (const component of mounted.splice(0)) {
    unmount(component);
  }
  document.body.innerHTML = '';
}

function inputByLabel(label: string): HTMLInputElement {
  const element = [...document.querySelectorAll('label')].find(
    (candidate) => candidate.textContent?.trim().startsWith(label)
  );
  const id = element?.getAttribute('for');
  const input = id ? document.getElementById(id) : null;
  if (!(input instanceof HTMLInputElement)) {
    throw new Error(`expected input labelled "${label}"`);
  }
  return input;
}

function type(input: HTMLInputElement, value: string): void {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function button(label: string): HTMLButtonElement {
  const result = [...document.querySelectorAll<HTMLButtonElement>('button')].find(
    (candidate) => candidate.textContent?.trim() === label
  );
  if (result === undefined) {
    throw new Error(`expected button "${label}"`);
  }
  return result;
}

async function loadStatus(passwordSet: boolean): Promise<void> {
  backend((path) => {
    if (path === '/api/auth/status') {
      return response(status(passwordSet));
    }
    throw new Error(`unexpected request ${path}`);
  });
  await auth.loadStatus();
}

beforeEach(() => {
  unmountAll();
  setFetchImpl(null);
  setUnauthorizedHandler(null);
});

afterEach(() => {
  unmountAll();
  setFetchImpl(null);
  setUnauthorizedHandler(null);
  vi.restoreAllMocks();
});

describe('SecurityPanel', () => {
  it('sets a password and sends its value only to the password endpoint', async () => {
    const calls: Call[] = [];
    setFetchImpl(async (path, init) => {
      calls.push({ path, init });
      if (path === '/api/auth/status') {
        return response(status(false));
      }
      if (path === '/api/auth/password') {
        return response({ password_set: true });
      }
      throw new Error(`unexpected request ${path}`);
    });
    await auth.loadStatus();
    render();

    const password = 'a-private-configuration-password';
    type(inputByLabel('New password'), password);
    type(inputByLabel('Confirm new password'), password);
    button('Set password').click();
    await vi.waitFor(() => expect(document.body.textContent).toContain('Configuration password set.'));
    flushSync();

    expect(calls.map((call) => call.path)).toEqual(['/api/auth/status', '/api/auth/password']);
    const write = calls[1];
    expect(write.init?.method).toBe('POST');
    expect(write.init?.body).toBe(JSON.stringify({ current: null, new: password }));
    expect(calls.filter((call) => call.path !== '/api/auth/password').every((call) =>
      !String(call.init?.body ?? '').includes(password)
    )).toBe(true);
    expect(document.body.textContent).toContain('A password is set');
    expect(document.body.textContent).toContain('Configuration password set.');
  });

  it('clears a set password through the clear endpoint', async () => {
    const calls: Call[] = [];
    setFetchImpl(async (path, init) => {
      calls.push({ path, init });
      if (path === '/api/auth/status') {
        return response(status(true));
      }
      if (path === '/api/auth/password/clear') {
        return response({ password_set: false });
      }
      throw new Error(`unexpected request ${path}`);
    });
    await auth.loadStatus();
    render();

    type(inputByLabel('Current password'), 'current-secret');
    button('Clear password').click();
    await vi.waitFor(() => expect(document.body.textContent).toContain('Configuration password cleared.'));
    flushSync();

    expect(calls[1].init?.method).toBe('POST');
    expect(calls[1].init?.body).toBe(JSON.stringify({ current: 'current-secret' }));
    expect(document.body.textContent).toContain('No password is set');
    expect(document.body.textContent).toContain('Configuration password cleared.');
  });

  it('validates empty, short and mismatched passwords without a request', async () => {
    let writes = 0;
    setFetchImpl(async (path) => {
      if (path === '/api/auth/status') {
        return response(status(false));
      }
      writes += 1;
      return response({ password_set: true });
    });
    await auth.loadStatus();
    render();

    expect(button('Set password').disabled).toBe(true);
    expect(document.body.textContent).toContain('Enter a new password.');
    expect(document.body.textContent).toContain('Confirm new password');

    type(inputByLabel('New password'), 'short');
    type(inputByLabel('Confirm new password'), 'short');
    expect(document.body.textContent).toContain('Use at least 8 characters.');

    type(inputByLabel('New password'), 'long-enough-password');
    type(inputByLabel('Confirm new password'), 'different-password');
    expect(document.body.textContent).toContain('The passwords do not match.');
    expect(button('Set password').disabled).toBe(true);
    expect(writes).toBe(0);
  });

  it('shows the server refusal message verbatim', async () => {
    const refusal = 'The supplied current password is incorrect.';
    setFetchImpl(async (path) => {
      if (path === '/api/auth/status') {
        return response(status(false));
      }
      return response({ error: { code: 'unauthorized', message: refusal, detail: null } }, 401);
    });
    await auth.loadStatus();
    render();
    const unauthorized = vi.fn();
    setUnauthorizedHandler(unauthorized);
    type(inputByLabel('New password'), 'long-enough-password');
    type(inputByLabel('Confirm new password'), 'long-enough-password');
    button('Set password').click();

    await vi.waitFor(() => expect(document.querySelector('[role="alert"]')?.textContent?.trim()).toBe(refusal));
    expect(unauthorized).not.toHaveBeenCalled();
  });
});
