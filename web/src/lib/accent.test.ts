/**
 * The runtime accent reading (HSD-16le).
 *
 * Nothing here writes to `<html>`: the accent is a tile accent, read and
 * measured, never injected into the live palette. What this covers is the
 * measurement itself — that the accent, its ink and the ratio agree with
 * `bestAccentInk`, that a malformed stored value falls back to the registry
 * default, and that the default is never refused.
 */

import { describe, expect, it } from 'vitest';

import { DEFAULT_ACCENT_COLOR, accentIsAcceptable, readAccent } from './accent.js';
import { contrastRatio } from './contrast.js';

describe('readAccent', () => {
  it('reports the accent, its ink and the ratio between them', () => {
    const reading = readAccent('#4f9cf9');

    expect(reading.accent).toBe('#4f9cf9');
    expect(reading.ink).toBe('#0e1116');
    expect(reading.ratio).toBeCloseTo(contrastRatio('#0e1116', '#4f9cf9'), 10);
    expect(reading.ratio).toBeGreaterThanOrEqual(4.5);
  });

  it('falls back to the registry default for a value that is not a colour', () => {
    const fallback = readAccent(DEFAULT_ACCENT_COLOR);

    expect(fallback.accent).toBe('#4f9cf9');
    expect(readAccent('garbage')).toEqual(fallback);
    expect(readAccent('')).toEqual(fallback);
  });

  it('never refuses the registry default', () => {
    // The default has to clear both palettes' inks, or a fresh install would
    // open the panel showing a warning about its own default.
    expect(accentIsAcceptable(DEFAULT_ACCENT_COLOR)).toBe(true);
  });
});
