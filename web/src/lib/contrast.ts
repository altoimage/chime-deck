/**
 * WCAG 2.x contrast maths and the two thresholds this project enforces.
 *
 * The offline token gate (`scripts/check-contrast.mjs`) and the runtime
 * Appearance check must not each own a copy of the formula: two copies of
 * "4.5" eventually disagree, and then one surface passes an accent the other
 * refuses. The gate imports this module, so the number exists once in the repo.
 *
 * `ACCENT_CONTRAST_INKS` mirrors the two `--accent-contrast` values in
 * `src/styles/tokens.css` (dark, light). `npm run check:contrast` asserts the
 * mirror still holds, because a stale copy here would pick an ink the palette
 * no longer has.
 *
 * Pure TS: no DOM, no runes. A node test can pin every rule.
 */

/** WCAG 1.4.3, normal text. */
export const TEXT_CONTRAST_MIN = 4.5;

/** WCAG 1.4.11, UI components and graphical objects. */
export const NON_TEXT_CONTRAST_MIN = 3.0;

/** The two `--accent-contrast` values in `styles/tokens.css` (dark, light). */
export const ACCENT_CONTRAST_INKS = ['#0e1116', '#ffffff'] as const;

export interface Rgb {
  r: number;
  g: number;
  b: number;
}

/**
 * Parse `#rgb` or `#rrggbb` (case-insensitive) into channels, or `null` for
 * anything else.
 *
 * No throw: callers test a value a person typed, where "not a colour" is an
 * answer, not an exception.
 */
export function parseHexColor(value: string): Rgb | null {
  const match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(value.trim());
  if (match === null) {
    return null;
  }
  const hex = match[1].length === 3 ? [...match[1]].map((digit) => digit + digit).join('') : match[1];
  return {
    r: Number.parseInt(hex.slice(0, 2), 16),
    g: Number.parseInt(hex.slice(2, 4), 16),
    b: Number.parseInt(hex.slice(4, 6), 16)
  };
}

/** WCAG's linearised channel value: the sRGB curve, not a plain division. */
function linearise(channel: number): number {
  const c = channel / 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

/**
 * Relative luminance per WCAG 2.x, 0..1.
 *
 * Throws on a string that is not a hex colour: the token gate reads raw CSS
 * values, and a malformed token must fail the check loudly rather than silently
 * measure as black and pass.
 */
export function relativeLuminance(value: string): number {
  const rgb = parseHexColor(value);
  if (rgb === null) {
    throw new Error(`not a hex colour: "${value}"`);
  }
  return 0.2126 * linearise(rgb.r) + 0.7152 * linearise(rgb.g) + 0.0722 * linearise(rgb.b);
}

/** The WCAG contrast ratio between two colours, 1..21. */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** WCAG style, two decimals: `4.50:1`. */
export function formatContrastRatio(ratio: number): string {
  return `${ratio.toFixed(2)}:1`;
}

/**
 * The `--accent-contrast` ink that reads best on *accent*.
 *
 * One stored accent has to be readable in both palettes, so the ink is chosen
 * per accent instead of fixed: a light accent needs the dark ink and vice
 * versa. A tie keeps the first entry, so the choice is deterministic.
 */
export function bestAccentInk(accent: string): { ink: string; ratio: number } {
  let best: { ink: string; ratio: number } = { ink: ACCENT_CONTRAST_INKS[0], ratio: 0 };
  for (const ink of ACCENT_CONTRAST_INKS) {
    const ratio = contrastRatio(ink, accent);
    if (ratio > best.ratio) {
      best = { ink, ratio };
    }
  }
  return best;
}
