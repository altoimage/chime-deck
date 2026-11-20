#!/usr/bin/env node
/**
 * Gallery coverage gate.
 *
 * Every src/lib/ui/*.svelte component must be referenced by name in
 * src/routes/Gallery.svelte, so the dev gallery can never silently drift
 * behind the kit. In Phase 1 the gallery does not exist yet: the script
 * exits 0 with a clear message while the file is absent, and enforces
 * fully once Phase 2 adds it.
 *
 * Also enforces — always, gallery or not — the ban on importing
 * svelte/transition or svelte/animate: their Web Animations API cannot be
 * disabled by the prefers-reduced-motion CSS, so they are forbidden.
 *
 * Plain Node ESM, no dependencies. Exit 1 on any failure.
 */

import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const webRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const srcDir = join(webRoot, 'src');
const uiDir = join(srcDir, 'lib', 'ui');
const galleryPath = join(srcDir, 'routes', 'Gallery.svelte');

let failures = 0;

/* ---- svelte/transition + svelte/animate import ban (always enforced) ---- */

const BANNED_IMPORT = /from\s+['"]svelte\/(transition|animate)['"]/;

function* walkSources(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* walkSources(full);
    } else if (/\.(svelte|ts)$/.test(entry.name)) {
      yield full;
    }
  }
}

for (const file of walkSources(srcDir)) {
  if (BANNED_IMPORT.test(readFileSync(file, 'utf8'))) {
    failures += 1;
    console.log(
      `FAIL  ${file}: imports svelte/transition or svelte/animate ` +
        '(banned — the Web Animations API ignores prefers-reduced-motion CSS)'
    );
  }
}

/* ---- gallery coverage ---- */

const components = readdirSync(uiDir)
  .filter((file) => file.endsWith('.svelte'))
  .sort();

if (!existsSync(galleryPath)) {
  console.log(
    `check-gallery: src/routes/Gallery.svelte is not present yet (arrives in Phase 2) — ` +
      `coverage not enforced for ${components.length} ui component(s): ${components.join(', ')}`
  );
} else {
  const source = readFileSync(galleryPath, 'utf8');
  for (const file of components) {
    const name = file.replace(/\.svelte$/, '');
    const ok = source.includes(name);
    if (!ok) failures += 1;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${name} ${ok ? 'referenced in' : 'MISSING from'} Gallery.svelte`);
  }
}

console.log(
  failures === 0 ? 'check-gallery: OK' : `check-gallery: ${failures} failure(s)`
);
process.exit(failures === 0 ? 0 : 1);
