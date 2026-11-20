#!/usr/bin/env node
/**
 * Dev-time generator for the committed MDI icon manifest.
 *
 * Writes `src/chime_deck/icons/data/mdi/`: one `index.json` naming the icon
 * set and listing its shards, and one shard per first character of the icon name
 * (`a.json` … `z.json`, plus `_other.json` for anything else), each a compact
 * `{name: path}` object. The Python side (`chime_deck.icons.manifest`) reads
 * exactly those files, so the committed data IS the contract: end users need no
 * Node, and `pip install` never generates anything.
 *
 * The data is sharded by first character only to stay under the repository's
 * 500 kB committed-file policy (`check-added-large-files`). All 7447 path
 * strings are ~2.6 MB and the compact map is ~2.7 MB; the largest shard (`c`) is
 * ~404 kB.
 *
 * Source of truth is `@mdi/svg`'s `meta.json` (authoritative kebab-case names)
 * plus its `svg/<name>.svg` files. `@mdi/js`'s camelCase names CANNOT be
 * inverted (`mdiBattery10` is `battery-10`), which is why this reads `@mdi/svg`.
 *
 * Refuses to run unless the installed `@mdi/svg` version matches
 * `EXPECTED_VERSION`; that pin also lives in `web/package.json` and in a Python
 * test, so a bump is a deliberate three-file change. Every icon is validated
 * (unique name, kebab-case, one `<path d="…">`, no orphan file either way) and
 * any violation aborts the run naming the icon — never a silent skip.
 *
 * Deterministic: keys sorted in byte order, compact `JSON.stringify`, one
 * trailing newline, no timestamps and no absolute paths, so running it twice
 * produces byte-identical files. Not wired into `npm run verify`.
 *
 * Plain Node ESM, no dependencies.
 */

import { mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const webRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const repoRoot = join(webRoot, '..');
const packageDir = join(webRoot, 'node_modules', '@mdi', 'svg');
const targetDir = join(repoRoot, 'src', 'chime_deck', 'icons', 'data', 'mdi');

const EXPECTED_VERSION = '7.4.47'; // keep in step with web/package.json and
// tests/test_api_icons.py — the pin is what stops the manifest drifting.
const SOURCE_PACKAGE = '@mdi/svg';
const OTHER_SHARD = '_other.json'; // first character that is not a..z
const NAME_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const PATH_PATTERN = /<path\b[^>]*\sd="([^"]*)"/g;
const MAX_REPORTED_VIOLATIONS = 20; // a wall of noise hides the first problem

let failures = 0;

function report(ok, message) {
  if (!ok) failures += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${message}`);
}

function abort(message) {
  console.log(`FAIL  ${message}`);
  console.log('generate-mdi-manifest: nothing written');
  process.exit(1);
}

/** Compare two ASCII names in byte order, independent of the host locale. */
function byteOrder(a, b) {
  if (a === b) return 0;
  return a < b ? -1 : 1;
}

/** Print at most MAX_REPORTED_VIOLATIONS lines, then say how many were hidden. */
function reportViolations(violations) {
  for (const violation of violations.slice(0, MAX_REPORTED_VIOLATIONS)) {
    console.log(`FAIL  ${violation}`);
  }
  if (violations.length > MAX_REPORTED_VIOLATIONS) {
    console.log(`FAIL  … and ${violations.length - MAX_REPORTED_VIOLATIONS} more violation(s)`);
  }
  failures += violations.length;
}

/** Return the icon names `meta.json` declares, aborting on a broken shape. */
function readIconNames() {
  if (!statSync(packageDir, { throwIfNoEntry: false })?.isDirectory()) {
    abort(
      `${relative(repoRoot, packageDir)} is missing — run \`npm install\` in ${webRoot}`
    );
  }
  const manifest = JSON.parse(readFileSync(join(packageDir, 'package.json'), 'utf8'));
  if (manifest.version !== EXPECTED_VERSION) {
    abort(
      `${SOURCE_PACKAGE} is ${manifest.version}, this generator expects ${EXPECTED_VERSION} — ` +
        `run \`npm install\` in ${webRoot} (web/package.json pins it), or bump ` +
        'EXPECTED_VERSION here, the devDependency and the Python pin together'
    );
  }
  const entries = JSON.parse(readFileSync(join(packageDir, 'meta.json'), 'utf8'));
  if (!Array.isArray(entries)) {
    abort(`${SOURCE_PACKAGE}/meta.json is not an array`);
  }
  const names = [];
  for (const entry of entries) {
    if (entry === null || typeof entry !== 'object' || typeof entry.name !== 'string') {
      abort(`${SOURCE_PACKAGE}/meta.json has an entry without a string name`);
    }
    names.push(entry.name);
  }
  return names;
}

/** Return the single path string in one svg file, or null when it is unusable. */
function readPathData(svgPath) {
  const svg = readFileSync(svgPath, 'utf8');
  const paths = [...svg.matchAll(PATH_PATTERN)].map((match) => match[1]);
  if (paths.length !== 1 || paths[0].length === 0) return null;
  return paths[0];
}

/**
 * Validate the icon set and return `name -> path` for every icon.
 *
 * Every violation is collected and the run aborts before anything is written:
 * a manifest missing an icon is worse than no manifest at all.
 */
function collectIcons(names) {
  const violations = [];
  const seen = new Set();
  for (const name of names) {
    if (seen.has(name)) violations.push(`${name}: name appears twice in meta.json`);
    seen.add(name);
    if (!NAME_PATTERN.test(name)) {
      violations.push(`${name}: name is not lower-case kebab-case`);
    }
  }

  const svgDir = join(packageDir, 'svg');
  const svgFiles = new Set(
    readdirSync(svgDir)
      .filter((file) => file.endsWith('.svg'))
      .map((file) => file.slice(0, -'.svg'.length))
  );
  for (const name of seen) {
    if (!svgFiles.has(name)) violations.push(`${name}: listed in meta.json but svg/${name}.svg is missing`);
  }
  for (const name of svgFiles) {
    if (!seen.has(name)) violations.push(`${name}: svg/${name}.svg is not listed in meta.json`);
  }

  const icons = new Map();
  for (const name of seen) {
    if (!svgFiles.has(name)) continue;
    const path = readPathData(join(svgDir, `${name}.svg`));
    if (path === null) {
      violations.push(`${name}: svg/${name}.svg does not contain exactly one <path d="…">`);
      continue;
    }
    icons.set(name, path);
  }

  if (violations.length > 0) {
    reportViolations(violations);
    console.log(
      `generate-mdi-manifest: ${failures} violation(s) in ${SOURCE_PACKAGE}; the shipped ` +
        'manifest was left untouched'
    );
    process.exit(1);
  }
  report(true, `${SOURCE_PACKAGE} ${EXPECTED_VERSION}: ${icons.size} icons, all valid`);
  return icons;
}

/** Group `name -> path` into `shard file -> name -> path`, keys in byte order. */
function buildShards(icons) {
  const shards = new Map();
  for (const name of [...icons.keys()].sort(byteOrder)) {
    const first = name[0];
    const file = first >= 'a' && first <= 'z' ? `${first}.json` : OTHER_SHARD;
    if (!shards.has(file)) shards.set(file, {});
    shards.get(file)[name] = icons.get(name);
  }
  return new Map([...shards.entries()].sort(([a], [b]) => byteOrder(a, b)));
}

/** Replace the target directory's `*.json` with *contents* (`file -> text`). */
function writeManifest(contents) {
  mkdirSync(targetDir, { recursive: true });
  for (const file of readdirSync(targetDir)) {
    if (file.endsWith('.json')) rmSync(join(targetDir, file));
  }
  let total = 0;
  let largest = { file: '', size: 0 };
  for (const [file, text] of contents) {
    writeFileSync(join(targetDir, file), text);
    const size = statSync(join(targetDir, file)).size;
    total += size;
    if (size > largest.size) largest = { file, size };
    console.log(`  ${file} ${size} bytes`);
  }
  return { total, largest };
}

const names = readIconNames();
const icons = collectIcons(names);
const shards = buildShards(icons);

const index = {
  version: EXPECTED_VERSION,
  source: SOURCE_PACKAGE,
  icon_count: icons.size,
  shards: [...shards.keys()],
};
const contents = [['index.json', `${JSON.stringify(index)}\n`]];
for (const [file, shard] of shards) {
  contents.push([file, `${JSON.stringify(shard)}\n`]);
}

console.log(`generate-mdi-manifest: ${SOURCE_PACKAGE} ${index.version}, ${index.icon_count} icons`);
const { total, largest } = writeManifest(contents);
console.log(
  `generate-mdi-manifest: ${contents.length} files, ${total} bytes, ` +
    `largest ${largest.file} ${largest.size} bytes`
);
report(
  largest.size < 500000,
  `every file is under the 500 kB large-file policy (largest ${largest.file})`
);
console.log(`generate-mdi-manifest: wrote ${targetDir}`);
process.exitCode = failures === 0 ? 0 : 1;
