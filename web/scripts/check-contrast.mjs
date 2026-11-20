#!/usr/bin/env node
/**
 * Offline WCAG 2.x contrast gate for the design tokens.
 *
 * Parses src/styles/tokens.css into the dark (:root) and light
 * (:root[data-theme='light']) palettes and asserts:
 *   - text pairs at WCAG 1.4.3's 4.5:1
 *   - non-text pairs (control borders, focus ring, state colours on surfaces)
 *     at WCAG 1.4.11's 3.0:1
 *   - the two --accent-contrast values are exactly ACCENT_CONTRAST_INKS, the
 *     list the runtime ink chooser mirrors
 *   - D26 sizing: --tap is 48px, --tap-primary is 56px, and the first argument
 *     of every --text-* clamp is >= 16px (no text can render below 16px)
 *   - every asserted colour token exists in BOTH palettes
 *
 * The maths and both thresholds are imported from src/lib/contrast.ts, so this
 * gate and the runtime appearance check cannot disagree about what 4.5 means.
 *
 * Requires Node >= 22.18: the import above is a TypeScript module, which older
 * Node cannot load. That failure happens while this file's imports are being
 * resolved, before any line here runs, so it is documented rather than caught —
 * a try/catch around the body would never see it.
 *
 * Plain Node ESM, no dependencies, offline, deterministic. Exits 1 (with a
 * printed table) on any failure.
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  ACCENT_CONTRAST_INKS,
  NON_TEXT_CONTRAST_MIN,
  TEXT_CONTRAST_MIN,
  contrastRatio
} from '../src/lib/contrast.ts';

const webRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const cssPath = join(webRoot, 'src', 'styles', 'tokens.css');
const css = readFileSync(cssPath, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');

/** Extract a top-level token block by selector and parse its declarations. */
function parsePalette(re, label) {
  const match = re.exec(css);
  if (!match) {
    console.error(`FATAL  ${label} block not found in ${cssPath}`);
    process.exit(1);
  }
  const tokens = new Map();
  for (const decl of match[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    tokens.set(decl[1], decl[2].trim());
  }
  return tokens;
}

// `:root {` must not match `:root[data-theme='light'] {`.
const dark = parsePalette(/(?:^|\s):root\s*\{([^}]*)\}/, ':root (dark)');
const light = parsePalette(
  /:root\[data-theme=['"]light['"]\]\s*\{([^}]*)\}/,
  ":root[data-theme='light'] (light)"
);
const palettes = [
  ['dark', dark],
  ['light', light]
];

/* ---- the check tables ---- */

const SURFACES = ['--bg', '--surface', '--surface-hover'];
const STATES = ['--state-ok', '--state-warn', '--state-alert', '--state-off'];

/** @type {Array<[string, string, number]>} [foreground, background, threshold] */
const pairs = [];
for (const fg of ['--text', '--text-muted']) {
  for (const bg of SURFACES) pairs.push([fg, bg, TEXT_CONTRAST_MIN]);
}
for (const bg of ['--bg', '--surface']) pairs.push(['--accent', bg, TEXT_CONTRAST_MIN]);
pairs.push(['--accent-contrast', '--accent', TEXT_CONTRAST_MIN]);
for (const state of STATES) {
  for (const bg of ['--bg', '--surface']) pairs.push([state, bg, TEXT_CONTRAST_MIN]);
}
for (const bg of SURFACES) pairs.push(['--control-border', bg, NON_TEXT_CONTRAST_MIN]);
for (const bg of ['--bg', '--surface']) pairs.push(['--focus-ring', bg, NON_TEXT_CONTRAST_MIN]);
// The selected-tile edge is `inset 0 0 0 3px var(--accent)` on the card, whose
// background is `--surface` and can be `--surface-hover`. A selection must never
// be invisible (D29), so the edge is held to the non-text minimum on both.
pairs.push(['--accent', '--surface-hover', NON_TEXT_CONTRAST_MIN]);
for (const state of STATES) pairs.push([state, '--surface', NON_TEXT_CONTRAST_MIN]);

/** Every token asserted above must exist in BOTH palettes. */
const requiredTokens = [...new Set(pairs.flatMap(([fg, bg]) => [fg, bg]))];

let checks = 0;
let failures = 0;

function report(ok, pair, ratio, threshold) {
  checks += 1;
  if (!ok) failures += 1;
  console.log(
    `${pair.padEnd(44)} ${ratio.padEnd(10)} ${threshold.padEnd(8)} ${ok ? 'PASS' : 'FAIL'}`
  );
}

for (const [theme, palette] of palettes) {
  for (const token of requiredTokens) {
    report(palette.has(token), `${theme}  token ${token}`, palette.has(token) ? 'present' : 'MISSING', 'exists');
  }
  for (const [fg, bg, threshold] of pairs) {
    const fgValue = palette.get(fg);
    const bgValue = palette.get(bg);
    if (!fgValue || !bgValue) continue; // already reported as missing
    const ratio = contrastRatio(fgValue, bgValue);
    report(ratio >= threshold, `${theme}  ${fg} on ${bg}`, ratio.toFixed(2), `>= ${threshold.toFixed(1)}`);
  }
}

/* ---- the ink list the runtime chooser mirrors ---- */

// `bestAccentInk` in src/lib/contrast.ts only knows these two inks, so a palette
// that grows a third (or renames one) would leave the app picking an ink the CSS
// no longer defines. One set comparison catches both directions.
const paletteInks = new Set(palettes.map(([, palette]) => palette.get('--accent-contrast')));
report(
  paletteInks.size === ACCENT_CONTRAST_INKS.length &&
    ACCENT_CONTRAST_INKS.every((ink) => paletteInks.has(ink)),
  'contrast  ACCENT_CONTRAST_INKS == --accent-contrast values',
  [...paletteInks].join(','),
  ACCENT_CONTRAST_INKS.join(',')
);

/* ---- D26 sizing assertions (sizes live on :root, shared by both themes) ---- */

report(dark.get('--tap') === '48px', 'D26  --tap', dark.get('--tap') ?? 'MISSING', '=== 48px');
report(
  dark.get('--tap-primary') === '56px',
  'D26  --tap-primary',
  dark.get('--tap-primary') ?? 'MISSING',
  '=== 56px'
);

for (const [name, value] of dark) {
  if (!name.startsWith('--text-')) continue;
  const clamp = /^clamp\(\s*(\d+(?:\.\d+)?)px/.exec(value);
  const plain = /^(\d+(?:\.\d+)?)px$/.exec(value);
  const floor = clamp
    ? Number.parseFloat(clamp[1])
    : plain
      ? Number.parseFloat(plain[1])
      : null;
  if (floor === null) continue; // a colour token (e.g. --text-muted), not a type-scale token
  report(floor >= 16, `D26  ${name} floor`, `${floor}px`, '>= 16px');
}

console.log(`check-contrast: ${checks} checks, ${failures} failed`);
process.exit(failures === 0 ? 0 : 1);
