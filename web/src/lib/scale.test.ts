import { describe, expect, it } from 'vitest';

import {
  DEFAULT_SCALE,
  SCALE_STEPS,
  isScaleStep,
  readScale,
  scaleLabel
} from './scale.js';

describe('isScaleStep', () => {
  it('accepts every offered step', () => {
    for (const step of SCALE_STEPS) {
      expect(isScaleStep(step)).toBe(true);
    }
  });

  it('rejects values between the steps and numbers of the wrong type', () => {
    for (const value of [84, 87, 200, -1, '130', null, undefined, Number.NaN, 1.3]) {
      expect(isScaleStep(value)).toBe(false);
    }
  });
});

describe('readScale', () => {
  it('returns a genuine step unchanged', () => {
    expect(readScale(85)).toBe(85);
    expect(readScale(100)).toBe(100);
    expect(readScale(130)).toBe(130);
    expect(readScale(150)).toBe(150);
  });

  it('degrades anything else to the default rather than trusting it', () => {
    for (const value of [84, 87, 200, -1, '130', null, undefined, Number.NaN, 1.3]) {
      expect(readScale(value)).toBe(DEFAULT_SCALE);
    }
    expect(DEFAULT_SCALE).toBe(100);
  });
});

describe('scaleLabel', () => {
  it('gives every step a distinct label that names its percentage', () => {
    const labels = SCALE_STEPS.map((step) => scaleLabel(step));

    expect(new Set(labels).size).toBe(SCALE_STEPS.length);
    for (const [index, step] of SCALE_STEPS.entries()) {
      expect(labels[index]).toContain(`${step}%`);
    }
  });

  it('calls the base size normal', () => {
    expect(scaleLabel(100)).toBe('Normal — 100%');
  });
});
