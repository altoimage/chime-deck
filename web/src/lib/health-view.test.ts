import { describe, expect, it } from 'vitest';

import {
  componentViews,
  connectionPhase,
  connectionView,
  healthView,
  summarySentence,
  type ConnectionInput
} from './health-view.js';
import type { HealthState } from './api.js';

const LIVE: ConnectionInput = {
  status: 'open',
  stale: false,
  attempts: 0,
  hasSnapshot: true
};

const RECONNECTING: ConnectionInput = {
  status: 'closed',
  stale: true,
  attempts: 1,
  hasSnapshot: true
};

const NEVER_CONNECTED: ConnectionInput = {
  status: 'closed',
  stale: true,
  attempts: 0,
  hasSnapshot: false
};

describe('healthView', () => {
  it('preserves every established component word and adds a distinct mark', () => {
    const expected: Record<string, { word: string; mark: string }> = {
      connected: { word: 'connected', mark: 'filled' },
      ready: { word: 'ready', mark: 'filled' },
      reconnecting: { word: 'reconnecting', mark: 'ring' },
      degraded: { word: 'degraded', mark: 'ring' },
      error: { word: 'disconnected', mark: 'cross' },
      disabled: { word: 'disabled', mark: 'hollow' },
      not_configured: { word: 'not configured', mark: 'hollow' },
      no_entities: { word: 'waiting for tiles', mark: 'hollow' }
    };

    for (const [status, state] of Object.entries(expected)) {
      const view = healthView(status);
      expect(view.word, status).toBe(state.word);
      expect(view.mark, status).toBe(state.mark);
      expect(view.explanation.length, status).toBeGreaterThanOrEqual(20);
    }
  });

  it('presents error as disconnected with the alert cross mark', () => {
    expect(healthView('error')).toMatchObject({
      word: 'disconnected',
      kind: 'alert',
      mark: 'cross'
    });
  });

  it('keeps unknown status muted and explains that the state cannot be confirmed', () => {
    const view = healthView('future_status');

    expect(view.word).toBe('not configured');
    expect(view.kind).toBe('off');
    expect(view.mark).toBe('hollow');
    expect(view.explanation.length).toBeGreaterThanOrEqual(20);
  });

  it('distinguishes configured Home Assistant with no selected tiles from an unconfigured setup', () => {
    const noEntities = healthView('no_entities');
    const notConfigured = healthView('not_configured');

    expect(noEntities.word).not.toBe(notConfigured.word);
    expect(noEntities.explanation).not.toBe(notConfigured.explanation);
    expect(noEntities.explanation).toContain('tiles');
    expect(noEntities.explanation).not.toMatch(/address|token|missing/i);
    expect(noEntities.explanation).not.toBe(
      'This status is not recognized, so the component state cannot be confirmed.'
    );
  });

  it('builds the stable component order and accessible summary', () => {
    const health: HealthState = {
      ha: 'connected',
      mqtt: 'reconnecting',
      audio: 'disabled',
      detail: null
    };
    const views = componentViews(health);

    expect(views.map(({ key, label }) => [key, label])).toEqual([
      ['ha', 'Home Assistant'],
      ['mqtt', 'MQTT'],
      ['audio', 'Audio']
    ]);
    expect(summarySentence(views)).toBe(
      'Home Assistant is connected, MQTT is reconnecting, Audio is disabled'
    );
  });
});

describe('connectionView', () => {
  it('distinguishes live, reconnecting and never-connected states by kind, mark and word', () => {
    const inputs = [LIVE, RECONNECTING, NEVER_CONNECTED];
    const views = inputs.map(connectionView);

    expect(new Set(views.map((view) => view.kind)).size).toBe(3);
    expect(new Set(views.map((view) => view.mark)).size).toBe(3);
    expect(new Set(views.map((view) => view.word)).size).toBe(3);
    expect(inputs.map(connectionPhase)).toEqual(['live', 'reconnecting', 'disconnected']);
    expect(views.map((view) => view.explanation.length).every((length) => length >= 20)).toBe(true);
  });

  it('keeps the first boot attempt transient rather than alerting', () => {
    const boot: ConnectionInput = {
      status: 'idle',
      stale: true,
      attempts: 0,
      hasSnapshot: false
    };
    const view = connectionView(boot);

    expect(connectionPhase(boot)).toBe('connecting');
    expect(view.kind).toBe('warn');
    expect(view.mark).toBe('ring');
    expect(view.word).toBe('Connecting…');
    expect(view.kind).not.toBe('alert');
    expect(view.explanation.length).toBeGreaterThanOrEqual(20);
  });

  it('keeps every connection explanation at least twenty characters long', () => {
    const inputs: ConnectionInput[] = [
      LIVE,
      RECONNECTING,
      NEVER_CONNECTED,
      { status: 'connecting', stale: false, attempts: 0, hasSnapshot: false },
      { status: 'open', stale: true, attempts: 0, hasSnapshot: false },
      { status: 'closed', stale: false, attempts: 0, hasSnapshot: true }
    ];

    for (const input of inputs) {
      expect(connectionView(input).explanation.length).toBeGreaterThanOrEqual(20);
    }
  });
});
