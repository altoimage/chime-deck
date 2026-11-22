/**
 * Unit tests for the app-gain volume semantics (HSD-wpm0).
 *
 * Node environment, no DOM: `volume.ts` is what the dashboard sheet and the
 * settings panel both import, so these are the rules that keep the two screens
 * from inventing two meanings of "mute". The load-bearing cases are that an
 * unreadable number clamps to the documented default (never to silence) and that
 * unmuting from a remembered `0` lands on a real volume.
 */

import { describe, expect, it } from 'vitest';

import {
  DEFAULT_HARDWARE_VOLUME,
  DEFAULT_RAIL_TARGET,
  DEFAULT_VOLUME,
  RAIL_TARGETS,
  VOLUME_STEP,
  clampVolume,
  isMuted,
  isRailTarget,
  isSilent,
  railVolumeKey,
  stepVolume,
  unmuteTarget
} from './volume.js';

describe('volume defaults and rail target', () => {
  it('uses 100 for both volume defaults and software for the rail', () => {
    expect(DEFAULT_VOLUME).toBe(100);
    expect(DEFAULT_HARDWARE_VOLUME).toBe(100);
    expect(RAIL_TARGETS).toEqual(['software', 'hardware']);
    expect(DEFAULT_RAIL_TARGET).toBe('software');
  });

  it('maps each rail target to exactly one setting key', () => {
    expect(railVolumeKey('software')).toBe('audio.volume');
    expect(railVolumeKey('hardware')).toBe('audio.hardware_volume');
  });

  it('recognises only supported rail targets', () => {
    expect(isRailTarget('software')).toBe(true);
    expect(isRailTarget('hardware')).toBe(true);
    expect(isRailTarget('mixer')).toBe(false);
  });
});

describe('clampVolume', () => {
  it('turns a non-finite value into the registry default, never into silence', () => {
    expect(clampVolume(Number.NaN)).toBe(DEFAULT_VOLUME);
    expect(clampVolume(Number.POSITIVE_INFINITY)).toBe(DEFAULT_VOLUME);
  });

  it('rounds a fractional value, because the backend setting is an integer', () => {
    expect(clampVolume(42.6)).toBe(43);
  });

  it('clamps onto the range the backend accepts', () => {
    expect(clampVolume(-5)).toBe(0);
    expect(clampVolume(105)).toBe(100);
    expect(clampVolume(0)).toBe(0);
    expect(clampVolume(100)).toBe(100);
  });
});

describe('isMuted', () => {
  it('is app gain only: zero is muted', () => {
    expect(isMuted(0)).toBe(true);
  });

  it('treats any audible volume as unmuted', () => {
    expect(isMuted(1)).toBe(false);
    expect(isMuted(70)).toBe(false);
  });
});

describe('isSilent', () => {
  it('answers the device-wide crossed-speaker predicate', () => {
    expect(isSilent({ muted: true, volume: 100, hardwareVolume: 100 })).toBe(true);
    expect(isSilent({ muted: false, volume: 0, hardwareVolume: 100 })).toBe(true);
    expect(isSilent({ muted: false, volume: 100, hardwareVolume: 0 })).toBe(true);
    expect(isSilent({ muted: false, volume: 100, hardwareVolume: 100 })).toBe(false);
  });
});

describe('unmuteTarget', () => {
  it('restores the last non-zero value used in this session', () => {
    expect(unmuteTarget(35)).toBe(35);
  });

  it('falls back to the default when the remembered value is zero', () => {
    expect(unmuteTarget(0)).toBe(DEFAULT_VOLUME);
  });
});

describe('stepVolume', () => {
  it('moves exactly one 5-point step per tap (D28)', () => {
    expect(VOLUME_STEP).toBe(5);
    expect(stepVolume(40, 'up')).toBe(45);
    expect(stepVolume(40, 'down')).toBe(35);
  });

  it('clamps at the floor instead of going negative', () => {
    expect(stepVolume(0, 'down')).toBe(0);
    expect(stepVolume(3, 'down')).toBe(0);
  });

  it('clamps at the ceiling instead of overshooting the accepted maximum', () => {
    expect(stepVolume(100, 'up')).toBe(100);
    expect(stepVolume(98, 'up')).toBe(100);
  });

  it('treats a non-finite reading as the default before stepping', () => {
    expect(stepVolume(Number.NaN, 'up')).toBe(DEFAULT_VOLUME);
    expect(stepVolume(Number.POSITIVE_INFINITY, 'down')).toBe(DEFAULT_VOLUME - VOLUME_STEP);
  });
});
