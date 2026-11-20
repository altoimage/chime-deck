/**
 * The runtime *reading* of a stored `ui.accent_color`: the accent a tile would
 * be drawn with, the ink that would be drawn on it, and the ratio between them
 * — the number the Appearance panel shows and gates Save on.
 *
 * This is a tile accent, and it is deliberately never written into the live
 * palette. `--accent` is simultaneously a fill and a foreground in both
 * palettes, and no single colour can be readable as a foreground on both the
 * dark and the light background: `tokens.css` therefore ships two different
 * `--accent` values, so a `--accent` injected on `<html>` would silently break
 * the 4.5:1 foreground guarantee `npm run check:contrast` enforces for the
 * static tokens. Measuring a candidate costs nothing; applying one costs that
 * guarantee.
 */

import { TEXT_CONTRAST_MIN, bestAccentInk, parseHexColor } from './contrast.js';

/** The registry's `ui.accent_color` default (settings_registry.py). */
export const DEFAULT_ACCENT_COLOR = '#4f9cf9';

export interface AccentReading {
  accent: string;
  ink: string;
  ratio: number;
}

/**
 * The accent the page would render for *color*, with the ink it would pair it
 * with and the ratio between them.
 *
 * An unusable value falls back to the registry default for all three fields:
 * a malformed stored accent must leave the panel usable, never unreadable.
 */
export function readAccent(color: string): AccentReading {
  const accent = parseHexColor(color) === null ? DEFAULT_ACCENT_COLOR : color;
  const { ink, ratio } = bestAccentInk(accent);
  return { accent, ink, ratio };
}

/** Whether *color* clears WCAG 1.4.3 against the ink that would be painted on it. */
export function accentIsAcceptable(color: string): boolean {
  const { ratio } = readAccent(color);
  return ratio >= TEXT_CONTRAST_MIN;
}
