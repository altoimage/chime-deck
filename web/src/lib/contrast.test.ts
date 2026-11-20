/**
 * The WCAG maths and thresholds (HSD-16le).
 *
 * Two surfaces depend on this module and must agree: the offline token gate
 * (`scripts/check-contrast.mjs`) and the runtime accent check. So the reference
 * pairs are pinned here — a formula that drifted would keep both green while
 * measuring the wrong thing.
 *
 * Node environment, no DOM: this module is pure maths on purpose.
 */

import { describe, expect, it } from 'vitest';

import {
  ACCENT_CONTRAST_INKS,
  NON_TEXT_CONTRAST_MIN,
  TEXT_CONTRAST_MIN,
  bestAccentInk,
  contrastRatio,
  formatContrastRatio,
  parseHexColor,
  relativeLuminance
} from './contrast.js';
import { accentIsAcceptable } from './accent.js';

describe('contrast ratios', () => {
  it('matches the WCAG reference pairs', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 2);
    expect(contrastRatio('#ffffff', '#000000')).toBeCloseTo(21, 2);
    expect(contrastRatio('#4f9cf9', '#4f9cf9')).toBeCloseTo(1, 5);
    // Order does not matter: the ratio is symmetric.
    expect(contrastRatio('#ffffff', '#0e1116')).toBeCloseTo(
      contrastRatio('#0e1116', '#ffffff'),
      10
    );
  });

  it('expands #rgb to the #rrggbb form it stands for', () => {
    expect(contrastRatio('#fff', '#000')).toBeCloseTo(contrastRatio('#ffffff', '#000000'), 10);
    expect(parseHexColor('#f0a')).toEqual(parseHexColor('#ff00aa'));
    expect(relativeLuminance('#ABC')).toBeCloseTo(relativeLuminance('#aabbcc'), 10);
  });
});

describe('parseHexColor', () => {
  it('reads #rrggbb and #rgb, and rejects everything else without throwing', () => {
    expect(parseHexColor('#4f9cf9')).toEqual({ r: 79, g: 156, b: 249 });
    expect(parseHexColor('#4F9CF9')).toEqual({ r: 79, g: 156, b: 249 });
    expect(parseHexColor('#fff')).toEqual({ r: 255, g: 255, b: 255 });
    expect(parseHexColor('')).toBeNull();
    expect(parseHexColor('red')).toBeNull();
    expect(parseHexColor('#12345')).toBeNull();
    expect(parseHexColor('4f9cf9')).toBeNull();
  });
});

describe('relativeLuminance', () => {
  it('throws for a string that is not a hex colour', () => {
    expect(() => relativeLuminance('not-a-colour')).toThrow(Error);
    expect(() => relativeLuminance('')).toThrow(Error);
  });

  it('measures black at 0 and white at 1', () => {
    expect(relativeLuminance('#000000')).toBeCloseTo(0, 5);
    expect(relativeLuminance('#ffffff')).toBeCloseTo(1, 5);
  });
});

describe('formatContrastRatio', () => {
  it('prints two decimals and the :1 suffix, WCAG style', () => {
    expect(formatContrastRatio(4.5)).toBe('4.50:1');
    expect(formatContrastRatio(21)).toBe('21.00:1');
    expect(formatContrastRatio(contrastRatio('#4f9cf9', '#0e1116'))).toMatch(/^\d+\.\d{2}:1$/);
  });
});

describe('thresholds', () => {
  it('states the two WCAG numbers once', () => {
    expect(TEXT_CONTRAST_MIN).toBe(4.5);
    expect(NON_TEXT_CONTRAST_MIN).toBe(3.0);
  });

  it('documents exactly the two inks tokens.css carries', () => {
    expect(ACCENT_CONTRAST_INKS).toEqual(['#0e1116', '#ffffff']);
  });
});

describe('bestAccentInk', () => {
  it('picks the dark ink on a light accent', () => {
    const light = bestAccentInk('#4f9cf9');
    expect(light.ink).toBe('#0e1116');
    expect(light.ratio).toBeGreaterThanOrEqual(TEXT_CONTRAST_MIN);
    // The white ink would be unusable here, which is why the choice is made.
    expect(contrastRatio('#ffffff', '#4f9cf9')).toBeLessThan(TEXT_CONTRAST_MIN);

    expect(bestAccentInk('#ffb020').ink).toBe('#0e1116');
    expect(bestAccentInk('#ffffff').ink).toBe('#0e1116');
    expect(bestAccentInk('#ffffff').ratio).toBeCloseTo(contrastRatio('#ffffff', '#0e1116'), 10);
  });

  it('picks the white ink on a dark accent', () => {
    const dark = bestAccentInk('#0e1116');
    expect(dark.ink).toBe('#ffffff');
    expect(dark.ratio).toBeGreaterThanOrEqual(TEXT_CONTRAST_MIN);
  });

  it('never invents an ink: the answer is always one of the two documented', () => {
    const accents = ['#4f9cf9', '#0e1116', '#ffffff', '#ffb020', '#2ec4b6', '#787878'];
    for (const accent of accents) {
      const { ink, ratio } = bestAccentInk(accent);
      expect(ACCENT_CONTRAST_INKS).toContain(ink);
      expect(ratio).toBeCloseTo(contrastRatio(ink, accent), 10);
    }
  });
});

describe('accentIsAcceptable', () => {
  it('accepts the registry default accent', () => {
    expect(accentIsAcceptable('#4f9cf9')).toBe(true);
  });

  it('rejects a mid-grey that fails against both inks', () => {
    // Measured with this module's own maths, not guessed: a colour whose
    // luminance sits between the two inks is unreadable on either.
    const inkRatios = ACCENT_CONTRAST_INKS.map((ink) => contrastRatio('#787878', ink));
    expect(Math.max(...inkRatios)).toBeLessThan(TEXT_CONTRAST_MIN);
    expect(accentIsAcceptable('#787878')).toBe(false);
  });
});
