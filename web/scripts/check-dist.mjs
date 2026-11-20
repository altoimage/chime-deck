#!/usr/bin/env node
/**
 * Post-build gate for the committed artifact. Runs after `npm run build`.
 *
 * Asserts (printing each check, exit 1 on any failure):
 *   - dist/index.html exists, has <html lang="en">, id="app" and a
 *     src="/assets/..." reference
 *   - every file under dist/ is below 500 kB (512000 bytes — the pre-commit
 *     large-file policy); the largest file is named
 *   - no @font-face and no .woff/.woff2 anywhere in dist/ (system fonts only)
 *   - the built CSS keeps prefers-reduced-motion:reduce, --tap:48px and
 *     --tap-primary:56px (custom properties survive minification)
 *   - the built CSS keeps the dashboard grid: a counted auto-fit
 *     grid-template-columns carrying the --tiles-columns property and the
 *     240px track floor, with grid-auto-rows:1fr in the same rule
 *   - no absolute filesystem path (e.g. /home/) in any dist file — the tree
 *     must be relocatable
 *   - the gallery sentinel (GALLERY_DEV_ONLY_SENTINEL, which Phase 2's
 *     Gallery.svelte will contain) is absent from every dist file, and no
 *     file in dist/assets/ matches ^Gallery — the dev gallery must never
 *     leak into production
 *
 * Plain Node ESM, no dependencies.
 */

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const webRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const distDir = join(webRoot, 'dist');
const indexPath = join(distDir, 'index.html');
const SIZE_LIMIT = 512000; // 500 kB — pre-commit check-added-large-files policy
const GALLERY_SENTINEL = 'GALLERY_DEV_ONLY_SENTINEL';

let failures = 0;

function report(ok, message) {
  if (!ok) failures += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${message}`);
}

report(existsSync(indexPath), 'dist/index.html exists');
if (!existsSync(indexPath)) {
  console.log('check-dist: dist/ missing — run `npm run build` first');
  console.log(`check-dist: ${failures} failure(s)`);
  process.exit(1);
}

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* walk(full);
    } else {
      yield full;
    }
  }
}

const files = [...walk(distDir)];

/* ---- per-file size limit, naming the largest ---- */

let largest = null;
for (const file of files) {
  const size = statSync(file).size;
  if (!largest || size > largest.size) largest = { file, size };
  report(size < SIZE_LIMIT, `${relative(webRoot, file)} is ${size} bytes (< ${SIZE_LIMIT})`);
}
if (largest) {
  console.log(`  largest file: ${relative(webRoot, largest.file)} (${largest.size} bytes)`);
}

/* ---- index.html shape ---- */

const index = readFileSync(indexPath, 'utf8');
report(index.includes('<html lang="en"'), 'index.html contains <html lang="en"');
report(index.includes('id="app"'), 'index.html contains id="app"');
report(/src="\/assets\//.test(index), 'index.html references a src="/assets/..." bundle');

/* ---- built CSS keeps the reduced-motion guard and the D26 tap tokens ---- */

const cssFiles = files.filter((file) => file.endsWith('.css'));
const css = cssFiles.map((file) => readFileSync(file, 'utf8')).join('\n');
report(cssFiles.length > 0, 'at least one built CSS file exists');
report(/prefers-reduced-motion\s*:\s*reduce/.test(css), 'built CSS contains prefers-reduced-motion:reduce');
report(/--tap\s*:\s*48px/.test(css), 'built CSS contains --tap:48px');
report(/--tap-primary\s*:\s*56px/.test(css), 'built CSS contains --tap-primary:56px');
// The machine half of the comment in Tile.svelte/Dashboard.svelte: that comment
// states the intent (container-relative units, no fixed shape), this fails the
// build if someone introduces one. No `aspect-ratio` may reach the built CSS,
// because the dashboard layout is resolution- and aspect-ratio-agnostic.
report(
  !/aspect-ratio/.test(css),
  'built CSS contains no aspect-ratio (the dashboard layout is resolution- and aspect-ratio-agnostic)'
);

/*
 * ---- dashboard reflow contract ----
 *
 * A post-build CSS assertion is the only honest way to check the reflow
 * contract without a browser: it proves the declarations survived the build,
 * not that the layout behaves. Real layout/overflow behaviour is HSD-rj5j's
 * Playwright job. The regexes are lenient about whitespace because the minifier
 * rewrites it; the matched text is printed so a drift is visible.
 *
 * The grid's column count comes from the `--tiles-columns` custom property the
 * component sets inline, so the checks are split by mechanism rather than one
 * long expression the minifier may format differently:
 * a counted `auto-fit` column definition, the 240px track floor, the count
 * property itself, and the vertical fill in that same declaration.
 */
const GRID_COLUMNS = /grid-template-columns\s*:\s*repeat\(\s*auto-fit\b[^;{}]*\b1fr\b[^;{}]*/;
// The whole rule body, because the minifier reorders declarations: the vertical
// fill is a sibling of the column definition, not part of its value.
const gridBody =
  [...css.matchAll(/\{([^{}]*)\}/g)]
    .map((match) => match[1])
    .find((body) => GRID_COLUMNS.test(body)) ?? null;
const gridColumns = gridBody === null ? null : GRID_COLUMNS.exec(gridBody);
report(
  gridColumns !== null,
  `built CSS keeps the counted auto-fit grid: ${gridColumns ? gridColumns[0] : 'grid-template-columns:repeat(auto-fit,...) not found'}`
);
report(
  gridColumns !== null && gridColumns[0].includes('min(240px'),
  'built CSS keeps the 240px track floor inside that grid-template-columns'
);
report(
  gridColumns !== null && gridColumns[0].includes('var(--tiles-columns'),
  'built CSS keeps the --tiles-columns count inside that grid-template-columns'
);
report(
  gridBody !== null && /grid-auto-rows\s*:\s*1fr\b/.test(gridBody),
  'built CSS keeps grid-auto-rows:1fr in the same rule (the vertical fill)'
);

// Lightning CSS rewrites `(max-width: 639px)` to `(width<=639px)` in the
// minified output, so the media prelude accepts either spelling.
const NARROW_COLUMN =
  /@media\s*\(\s*(?:max-width\s*:\s*639px|width\s*<=\s*639px)\s*\)\s*\{[^{}]*\{[^{}]*grid-template-columns\s*:\s*1fr/;
const narrowMatch = NARROW_COLUMN.exec(css);
report(
  narrowMatch !== null,
  `built CSS keeps the ≤639px single-column rule: ${narrowMatch ? narrowMatch[0] : '@media (max-width:639px){...grid-template-columns:1fr...} not found'}`
);

/* ---- no webfonts, no absolute paths, no gallery leakage ---- */

report(
  !files.some((file) => /\.woff2?$/i.test(file)),
  'no .woff/.woff2 files in dist/'
);

for (const file of files) {
  const rel = relative(webRoot, file);
  const text = readFileSync(file, 'utf8');
  report(!text.includes('@font-face'), `${rel} contains no @font-face`);
  report(!text.includes('/home/'), `${rel} contains no absolute filesystem path (/home/)`);
  report(!text.includes(GALLERY_SENTINEL), `${rel} contains no gallery sentinel (${GALLERY_SENTINEL})`);
}

const assetsDir = join(distDir, 'assets');
const assetNames = existsSync(assetsDir) ? readdirSync(assetsDir) : [];
report(
  !assetNames.some((name) => /^Gallery/.test(name)),
  'no file in dist/assets/ matches ^Gallery'
);

console.log(
  failures === 0 ? 'check-dist: OK' : `check-dist: ${failures} failure(s)`
);
process.exit(failures === 0 ? 0 : 1);
