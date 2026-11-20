/**
 * Pure tests for the Connection form rules, in the default `node` environment
 * with no DOM and no socket: the merge that keeps a draft's typing across a
 * late load (HSD-8rwk), the device-name draft and the instance-id pin it
 * triggers (HSD-vwdy), and the one render shape both screens read a test
 * verdict through (HSD-9xpm).
 *
 * The panel is on screen before the shell's first load lands, so the draft
 * opens on the registry defaults and only later learns the stored values.
 * `rebaseDraft` is what makes that safe, and these tests pin the two halves of
 * the contract: an untouched field adopts the stored value, and anything the
 * user has already typed — a secret included, since the server never returns
 * one — survives the load.
 */

import { describe, expect, it } from 'vitest';

import {
  blankDraft,
  buildPatch,
  draftDirty,
  draftFrom,
  haTestView,
  mqttTestView,
  rebaseDraft,
  type SettingsReader
} from './connection-form.js';

/**
 * A record-backed reader with the same type-checking the real store does, so
 * these tests exercise the rules against a plain snapshot rather than a mock:
 * a value of the wrong type reads as absent, exactly as it does on the wire.
 */
function readerOf(values: Record<string, unknown>): SettingsReader {
  return {
    text(key: string, fallback = ''): string {
      const value = values[key];
      return typeof value === 'string' ? value : fallback;
    },
    integer(key: string, minimum: number, maximum: number, fallback: number): number {
      const value = values[key];
      return typeof value === 'number' &&
        Number.isInteger(value) &&
        value >= minimum &&
        value <= maximum
        ? value
        : fallback;
    },
    flag(key: string, fallback: boolean): boolean {
      const value = values[key];
      return typeof value === 'boolean' ? value : fallback;
    },
    hasSecret(key: string): boolean {
      return values[key] === true;
    }
  };
}

describe('rebaseDraft', () => {
  it('adopts every stored value when the form is untouched', () => {
    const reader = readerOf({
      'ha.base_url': 'http://stored-ha.example:8123',
      'device.name': 'Hall Doorbell',
      'mqtt.host': 'stored-broker.example',
      'mqtt.port': 1884,
      'mqtt.discovery_prefix': 'stored-prefix',
      'mqtt.instance_id': 'stored-instance',
      'mqtt.tls': true
    });

    const rebased = rebaseDraft(blankDraft(), reader);

    expect(rebased).toEqual(draftFrom(reader));
    expect(rebased.baseUrl).toBe('http://stored-ha.example:8123');
    expect(rebased.deviceName).toBe('Hall Doorbell');
    expect(rebased.host).toBe('stored-broker.example');
    expect(rebased.port).toBe('1884');
    expect(rebased.discoveryPrefix).toBe('stored-prefix');
    expect(rebased.instanceId).toBe('stored-instance');
    expect(rebased.tls).toBe(true);
  });

  it('keeps a field the user has already typed and still adopts the ones they left alone', () => {
    const typedUrl = 'http://typed-before-load.example:8123';
    const draft = { ...blankDraft(), baseUrl: typedUrl };
    const reader = readerOf({
      'ha.base_url': 'http://homeassistant.local:8123',
      'mqtt.host': 'stored-broker.local'
    });

    const rebased = rebaseDraft(draft, reader);

    expect(rebased.baseUrl).toBe(typedUrl);
    expect(rebased.host).toBe('stored-broker.local');
  });

  it('keeps a secret the user typed, since no load can ever return one', () => {
    const draft = { ...blankDraft(), token: 'new-token' };
    const reader = readerOf({ 'ha.token': 'SENTINEL-SERVER-VALUE' });

    expect(rebaseDraft(draft, reader).token).toBe('new-token');
  });

  it('keeps a pending clear', () => {
    const draft = { ...blankDraft(), clearToken: true, token: '' };
    const reader = readerOf({ 'ha.token': 'SENTINEL-SERVER-VALUE' });

    const rebased = rebaseDraft(draft, reader);

    expect(rebased.clearToken).toBe(true);
    expect(rebased.token).toBe('');
  });

  it('never adopts a stored secret value into an untouched draft', () => {
    const reader = readerOf({ 'ha.token': 'SENTINEL-SERVER-VALUE' });

    expect(rebaseDraft(blankDraft(), reader).token).toBe('');
  });
});

describe('device.name (HSD-vwdy)', () => {
  it('round-trips the stored name through the draft, and defaults when it is absent', () => {
    expect(draftFrom(readerOf({ 'device.name': 'Hall Doorbell' })).deviceName).toBe('Hall Doorbell');
    expect(draftFrom(readerOf({})).deviceName).toBe('ChimeDeck');
    expect(blankDraft().deviceName).toBe('ChimeDeck');
  });

  it('is clean when the draft matches the stored name and dirty when it does not', () => {
    const reader = readerOf({ 'device.name': 'ChimeDeck' });

    expect(draftDirty(blankDraft(), reader)).toBe(false);

    const renamed = { ...blankDraft(), deviceName: 'Kitchen' };

    expect(draftDirty(renamed, reader)).toBe(true);
  });

  it('sends exactly the one changed key, and no key when the name is unchanged', () => {
    const reader = readerOf({ 'device.name': 'ChimeDeck' });

    expect(buildPatch(blankDraft(), reader)).toEqual({});
    expect(buildPatch({ ...blankDraft(), deviceName: 'Kitchen' }, reader)).toEqual({
      'device.name': 'Kitchen'
    });
  });

  it('adopts a name a load lands with, keeping one the user has already typed', () => {
    const reader = readerOf({ 'device.name': 'Hall Doorbell' });

    expect(rebaseDraft(blankDraft(), reader).deviceName).toBe('Hall Doorbell');
    expect(rebaseDraft({ ...blankDraft(), deviceName: 'Kitchen' }, reader).deviceName).toBe('Kitchen');
  });

  it('is clean after the rebase onto the pin echo, so Save cannot send a second PATCH', () => {
    // The first name write is not a one-key patch: the server pins
    // `mqtt.instance_id` from it, so the echo carries an id the draft never
    // sent. Rebasing onto the echo is what keeps the form clean afterwards.
    const echo = readerOf({
      'device.name': 'Hall Doorbell',
      'mqtt.instance_id': 'hall-doorbell'
    });

    const rebased = rebaseDraft(blankDraft(), echo);

    expect(rebased.deviceName).toBe('Hall Doorbell');
    expect(rebased.instanceId).toBe('hall-doorbell');
    expect(draftDirty(rebased, echo)).toBe(false);
    expect(buildPatch(rebased, echo)).toEqual({});
  });
});

describe('mqttTestView', () => {
  it('reports a rejected credential as authFailed and keeps the sentence verbatim', () => {
    const prose =
      'the MQTT broker at core-mosquitto.local:1883 rejected the credentials (Not authorized) — check mqtt.username and mqtt.password in Settings';

    const view = mqttTestView({
      ok: false,
      broker_version: null,
      error: prose,
      auth_failed: true
    });

    expect(view).toEqual({ ok: false, summary: '', error: prose, authFailed: true });
  });

  it('reports any other failure as not authFailed', () => {
    const prose = 'the MQTT broker at core-mosquitto.local:1883 refused the connection';

    const view = mqttTestView({
      ok: false,
      broker_version: null,
      error: prose,
      auth_failed: false
    });

    expect(view).toEqual({ ok: false, summary: '', error: prose, authFailed: false });
  });

  it('never reports authFailed on a pass', () => {
    const view = mqttTestView({
      ok: true,
      broker_version: '2.0.18',
      error: null,
      auth_failed: false
    });

    expect(view).toEqual({
      ok: true,
      summary: 'Connected to the MQTT broker 2.0.18',
      error: null,
      authFailed: false
    });
  });

  it('passes null through untouched', () => {
    expect(mqttTestView(null)).toBeNull();
  });
});

describe('haTestView', () => {
  it('never reports authFailed, on a pass or on a failure', () => {
    expect(
      haTestView({ ok: true, ha_version: '2024.10.1', entity_count: 42, error: null })
    ).toEqual({
      ok: true,
      summary: 'Connected to Home Assistant 2024.10.1 — 42 entities',
      error: null,
      authFailed: false
    });

    expect(
      haTestView({ ok: false, ha_version: null, entity_count: null, error: 'the token was rejected' })
    ).toEqual({ ok: false, summary: '', error: 'the token was rejected', authFailed: false });
  });
});
