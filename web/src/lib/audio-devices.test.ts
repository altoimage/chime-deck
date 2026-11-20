/**
 * Unit tests for the audio-device picker rows (HSD-wpm0).
 *
 * Node environment, no DOM. The load-bearing rules are the backend's own:
 * `resolve_device` matches a case-insensitive substring of the device NAME only,
 * so a row must store the full name; mpv's own `auto` entry must not become a
 * second system-default row; and the stored hint that matched nothing has to be
 * said out loud, because the backend only logs it (HSD-h13q).
 */

import { describe, expect, it } from 'vitest';

import type { AudioDevicesResponse } from './api.js';
import { deviceChoices, matchNotice } from './audio-devices.js';

const PIPEWIRE = 'pipewire/alsa_output.pci-0000_00_1f.3.analog-stereo';
const USB = 'alsa/hw:CARD=Speaker,DEV=0';

function discoveryOf(overrides: Partial<AudioDevicesResponse> = {}): AudioDevicesResponse {
  return {
    ok: true,
    hint: '',
    resolved_device: null,
    system_default: 'auto',
    devices: [
      { name: 'auto', hostapi: 'auto', is_default: true },
      { name: PIPEWIRE, hostapi: 'pipewire', is_default: false },
      { name: USB, hostapi: 'alsa', is_default: false }
    ],
    error: null,
    ...overrides
  };
}

describe('deviceChoices', () => {
  it('offers the system default first, storing the sentinel the backend chose', () => {
    const first = deviceChoices(discoveryOf())[0];

    expect(first).toEqual({
      value: 'auto',
      label: 'System default',
      description: 'Let the audio engine use the host default output',
      resolved: true
    });
  });

  it('stores the FULL mpv device name, because the hint is a name substring', () => {
    const row = deviceChoices(discoveryOf()).find((choice) => choice.value === USB);

    expect(row).toEqual({
      value: USB,
      label: USB,
      description: 'alsa',
      resolved: false
    });
  });

  it('keeps mpv order and marks a host-default device in its description', () => {
    const choices = deviceChoices(
      discoveryOf({
        devices: [{ name: PIPEWIRE, hostapi: 'pipewire', is_default: true }]
      })
    );

    expect(choices.map((choice) => choice.value)).toEqual(['auto', PIPEWIRE]);
    expect(choices[1].description).toBe('pipewire · host default');
  });

  it('marks exactly the row the backend resolved', () => {
    const choices = deviceChoices(discoveryOf({ hint: USB, resolved_device: USB }));

    expect(choices.filter((choice) => choice.resolved).map((choice) => choice.value)).toEqual([USB]);
    expect(choices[0].resolved).toBe(false);
  });

  it('marks the system-default row as the resolved one when nothing was named', () => {
    const choices = deviceChoices(discoveryOf({ resolved_device: null }));

    expect(choices.filter((choice) => choice.resolved).map((choice) => choice.value)).toEqual(['auto']);
  });

  it('marks the system-default row when the backend resolves the sentinel itself', () => {
    const choices = deviceChoices(discoveryOf({ hint: 'auto', resolved_device: 'auto' }));

    expect(choices.filter((choice) => choice.resolved).map((choice) => choice.value)).toEqual(['auto']);
  });

  it('never duplicates the sentinel when mpv lists an `auto` device of its own', () => {
    const choices = deviceChoices(discoveryOf());

    expect(choices.map((choice) => choice.value)).toEqual(['auto', PIPEWIRE, USB]);
  });

  it('still offers the system-default row when discovery failed', () => {
    const choices = deviceChoices(
      discoveryOf({ ok: false, devices: [], error: 'the audio backend is not running' })
    );

    expect(choices).toHaveLength(1);
    expect(choices[0].value).toBe('auto');
    expect(choices[0].resolved).toBe(true);
  });
});

describe('matchNotice', () => {
  it('names the stored hint and the fallback when it matched no discovered output', () => {
    const notice = matchNotice(discoveryOf({ hint: 'usb audio', resolved_device: null }));

    expect(notice).toContain('usb audio');
    expect(notice).toContain('system default');
  });

  it('stays quiet for a blank hint, which asks for the system default', () => {
    expect(matchNotice(discoveryOf({ hint: '   ' }))).toBeNull();
  });

  it('stays quiet when the hint did resolve a device', () => {
    expect(matchNotice(discoveryOf({ hint: 'usb', resolved_device: USB }))).toBeNull();
  });

  it('stays quiet for the `auto` sentinel', () => {
    expect(matchNotice(discoveryOf({ hint: 'AUTO' }))).toBeNull();
  });

  it('stays quiet when discovery failed, because the error row already explains it', () => {
    expect(
      matchNotice(discoveryOf({ ok: false, hint: 'usb audio', devices: [], error: 'no backend' }))
    ).toBeNull();
  });
});
