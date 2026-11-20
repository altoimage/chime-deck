// @vitest-environment happy-dom
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import StatusDots from './StatusDots.svelte';
import { HEALTH_FRAME, SNAPSHOT_FRAME } from './fixtures/wire.js';
import { connectionPhase, connectionView } from './health-view.js';
import { liveState } from './stores.svelte.js';

const mounted: Array<Parameters<typeof unmount>[0]> = [];

function renderStatusDots(): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(StatusDots, { target }));
  flushSync();
  return target;
}

function unmountAll(): void {
  for (const component of mounted.splice(0)) {
    unmount(component);
  }
  document.body.innerHTML = '';
}

function connectionChip(root: ParentNode = document): HTMLElement {
  const chip = root.querySelector<HTMLElement>('[data-connection-state]');
  if (chip === null) {
    throw new Error('expected the connection chip');
  }
  return chip;
}

function connectionDot(root: ParentNode = document): HTMLElement {
  const dot = root.querySelector<HTMLElement>('[data-health-key="connection"]');
  if (dot === null) {
    throw new Error('expected the connection dot');
  }
  return dot;
}

function componentRow(root: ParentNode, key: string): HTMLElement {
  const dot = root.querySelector<HTMLElement>(`.component [data-health-key="${key}"]`);
  const row = dot?.closest<HTMLElement>('.component');
  if (row === null || row === undefined) {
    throw new Error(`expected the ${key} component row`);
  }
  return row;
}

function pageText(): string {
  return (document.body.textContent ?? '').replace(/\s+/g, ' ').trim();
}

beforeEach(() => {
  unmountAll();
  liveState.reset();
});

afterEach(() => {
  unmountAll();
  liveState.reset();
});

describe('StatusDots', () => {
  it('renders fresh boot, live and retrying states with explicit labels and marks', () => {
    const root = renderStatusDots();
    const button = root.querySelector<HTMLButtonElement>('button.status');

    expect(button).not.toBeNull();
    expect(button?.getAttribute('aria-haspopup')).toBe('dialog');
    expect(button?.getAttribute('aria-label')).toContain('Connection status:');
    expect(button?.getAttribute('aria-label')).toContain('Open details.');

    // A reset store is idle with no attempts, so the prescribed boot state is Connecting.
    const renderedPhases = [connectionChip(root).dataset.connectionState];
    expect(connectionChip(root).textContent).toContain('Connecting…');
    expect(connectionDot(root).dataset.healthKind).toBe('warn');
    expect(connectionDot(root).className).toContain('mark-ring');

    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    liveState.markOpen();
    flushSync();
    renderedPhases.push(connectionChip(root).dataset.connectionState);
    expect(connectionChip(root).textContent).toContain('Live');
    expect(connectionDot(root).dataset.healthKind).toBe('ok');
    expect(connectionDot(root).className).toContain('mark-filled');

    liveState.markDisconnected();
    flushSync();
    renderedPhases.push(connectionChip(root).dataset.connectionState);
    expect(connectionChip(root).textContent).toContain('Reconnecting');
    expect(connectionDot(root).dataset.healthKind).toBe('warn');
    expect(connectionDot(root).className).toContain('mark-ring');

    expect(new Set(renderedPhases).size).toBe(3);
  });

  it('shows no-entities and not-configured words on the same HA header row', () => {
    liveState.applyHealth({
      ha: 'no_entities',
      mqtt: 'connected',
      audio: 'ready'
    });
    const root = renderStatusDots();

    let ha = componentRow(root, 'ha');
    expect(ha.querySelector('.word')?.textContent?.trim()).toBe('waiting for tiles');
    expect(ha.textContent).not.toContain('not configured');

    liveState.applyHealth({
      ha: 'not_configured',
      mqtt: 'connected',
      audio: 'ready'
    });
    flushSync();

    ha = componentRow(root, 'ha');
    expect(ha.querySelector('.word')?.textContent?.trim()).toBe('not configured');
  });

  it('renders connected, reconnecting and disconnected component dots distinctly and explains them in the sheet', () => {
    liveState.applyHealth({
      ha: 'connected',
      mqtt: 'reconnecting',
      audio: 'error',
      detail: 'Home Assistant unreachable (timeout)'
    });
    const root = renderStatusDots();

    const ha = root.querySelector<HTMLElement>('[data-health-key="ha"]');
    const mqtt = root.querySelector<HTMLElement>('[data-health-key="mqtt"]');
    const audio = root.querySelector<HTMLElement>('[data-health-key="audio"]');
    expect(ha).not.toBeNull();
    expect(mqtt).not.toBeNull();
    expect(audio).not.toBeNull();

    expect(ha?.dataset.healthKind).toBe('ok');
    expect(ha?.classList.contains('mark-filled')).toBe(true);
    expect(mqtt?.dataset.healthKind).toBe('warn');
    expect(mqtt?.classList.contains('mark-ring')).toBe(true);
    expect(audio?.dataset.healthKind).toBe('alert');
    expect(audio?.classList.contains('mark-cross')).toBe(true);

    const words = [...root.querySelectorAll<HTMLElement>('.component .word')]
      .map((word) => word.textContent?.trim());
    expect(words).toEqual(['connected', 'reconnecting', 'disconnected']);
    expect(new Set([ha?.dataset.healthKind, mqtt?.dataset.healthKind, audio?.dataset.healthKind]).size).toBe(3);
    expect(new Set([
      ha?.classList.contains('mark-filled') ? 'mark-filled' : '',
      mqtt?.classList.contains('mark-ring') ? 'mark-ring' : '',
      audio?.classList.contains('mark-cross') ? 'mark-cross' : ''
    ]).size).toBe(3);

    root.querySelector<HTMLButtonElement>('button.status')?.click();
    flushSync();

    const dialog = document.querySelector('dialog');
    expect(dialog).not.toBeNull();
    const expectedRows = [
      {
        key: 'ha',
        word: 'connected',
        kind: 'ok',
        mark: 'mark-filled',
        explanation: 'Home Assistant is connected and pushing state changes.'
      },
      {
        key: 'mqtt',
        word: 'reconnecting',
        kind: 'warn',
        mark: 'mark-ring',
        explanation: 'The connection dropped and the app is retrying with a growing delay.'
      },
      {
        key: 'audio',
        word: 'disconnected',
        kind: 'alert',
        mark: 'mark-cross',
        explanation: 'The last connection attempt failed, so this is disconnected. The reported reason below has the detail; open Connection to test it.'
      }
    ];

    for (const expected of expectedRows) {
      const row = dialog?.querySelector<HTMLElement>(`[data-row-key="${expected.key}"]`);
      expect(row?.querySelector('.row-word')?.textContent?.trim()).toBe(expected.word);
      expect(row?.querySelector('.explanation')?.textContent?.trim()).toBe(expected.explanation);
      const dot = row?.querySelector<HTMLElement>('[data-health-key]');
      expect(dot?.dataset.healthKind).toBe(expected.kind);
      expect(dot?.classList.contains(expected.mark)).toBe(true);
    }

    expect(dialog?.querySelector('.reported-reason h3')?.textContent?.trim()).toBe('Reported reason');
    expect(dialog?.querySelector('.reported-reason p')?.textContent?.trim())
      .toBe('Home Assistant unreachable (timeout)');
  });

  it('shows the disconnected cross state for a closed socket with no attempts and no snapshot', () => {
    // The store's public markDisconnected() increments attempts, so it cannot
    // produce this exact pure-state input; reset() instead correctly renders Connecting.
    const view = connectionView({
      status: 'closed',
      stale: true,
      attempts: 0,
      hasSnapshot: false
    });

    expect(connectionPhase({
      status: 'closed',
      stale: true,
      attempts: 0,
      hasSnapshot: false
    })).toBe('disconnected');
    expect(view.kind).toBe('alert');
    expect(view.mark).toBe('cross');
    expect(view.word).toBe('Disconnected');

    const live = connectionView({
      status: 'open',
      stale: false,
      attempts: 0,
      hasSnapshot: true
    });
    const reconnecting = connectionView({
      status: 'closed',
      stale: true,
      attempts: 1,
      hasSnapshot: true
    });
    expect(new Set([
      connectionPhase({ status: 'open', stale: false, attempts: 0, hasSnapshot: true }),
      connectionPhase({ status: 'closed', stale: true, attempts: 1, hasSnapshot: true }),
      connectionPhase({ status: 'closed', stale: true, attempts: 0, hasSnapshot: false })
    ]).size).toBe(3);
    expect(new Set([live.mark, reconnecting.mark, view.mark]).size).toBe(3);
    expect(new Set([live.kind, reconnecting.kind, view.kind]).size).toBe(3);
    expect(new Set([live.word, reconnecting.word, view.word]).size).toBe(3);
  });

  it('opens the touch-reachable sheet with explanations and verbatim backend detail', () => {
    liveState.applySnapshot(SNAPSHOT_FRAME.data);
    liveState.markOpen();
    liveState.applyHealth(HEALTH_FRAME.data);
    const root = renderStatusDots();

    const button = root.querySelector<HTMLButtonElement>('button.status');
    expect(button).not.toBeNull();
    button?.click();
    flushSync();

    const dialog = document.querySelector('dialog');
    expect(dialog).not.toBeNull();
    expect(pageText()).toContain('Connection status');
    expect(pageText()).toContain('The live connection is open and the last snapshot is current.');
    expect(pageText()).toContain('Reported reason');
    expect(pageText()).toContain('Home Assistant unreachable (timeout)');
  });
});
