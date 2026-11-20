// @vitest-environment happy-dom
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import type { AuthStatus } from '../auth.svelte.js';
import UnprotectedBanner from './UnprotectedBanner.svelte';

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function render(status: AuthStatus | null, link = true): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(UnprotectedBanner, { target, props: { status, link } }));
  flushSync();
  return target;
}

function unmountAll(): void {
  for (const component of mounted.splice(0)) {
    unmount(component);
  }
  document.body.innerHTML = '';
}

afterEach(unmountAll);

describe('UnprotectedBanner', () => {
  it('shows the persistent risk message and Security link only for an unprotected admin listener', () => {
    const root = render({
      listener: 'admin',
      passwordSet: false,
      tokenRequired: false,
      protected: false,
      authenticated: false
    });

    const banner = root.querySelector('[role="status"]');
    expect(banner).not.toBeNull();
    expect((banner?.textContent ?? '').replace(/\s+/g, ' ')).toContain(
      'anyone on this network can read and change these settings'
    );
    expect(banner?.textContent).toContain("each tile's current state");
    expect(root.querySelector('a[href="/settings/security"]')?.textContent).toBe('Set a password.');
    expect(root.querySelector('button')).toBeNull();
  });

  it('stays hidden for loopback and for a protected admin listener', () => {
    const loopback = render({
      listener: 'loopback',
      passwordSet: false,
      tokenRequired: false,
      protected: false,
      authenticated: false
    });
    expect(loopback.querySelector('[role="status"]')).toBeNull();
    unmountAll();

    const protectedAdmin = render({
      listener: 'admin',
      passwordSet: false,
      tokenRequired: true,
      protected: true,
      authenticated: false
    });
    expect(protectedAdmin.querySelector('[role="status"]')).toBeNull();
  });

  it('renders the wizard sentence without a link', () => {
    const root = render(
      {
        listener: 'admin',
        passwordSet: false,
        tokenRequired: false,
        protected: false,
        authenticated: false
      },
      false
    );
    expect(root.querySelector('[role="status"]')?.textContent).toContain('Set a password.');
    expect(root.querySelector('a')).toBeNull();
  });
});
