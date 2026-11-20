#!/usr/bin/env node
/**
 * Staleness gate for the committed artifact. The repo commits `web/dist`, so the
 * bundle a browser loads is whatever was last built and committed, not whatever
 * `web/src` says today.
 *
 * Asserts (printing the result, exit 1 on failure):
 *   - `npm run build` still succeeds, and
 *   - after that build, `git status --porcelain --untracked-files=normal --
 *     web/dist` is empty: the committed bundle is byte-identical to a fresh
 *     build of `web/src`.
 *
 * Why this script builds instead of only comparing: a compare-only check passes
 * the exact bug it exists to catch. When nobody rebuilt, `web/dist` is clean and
 * untouched, so a diff against HEAD is empty and a naive check reports PASS on a
 * panel serving month-old JavaScript. Building first makes the working tree the
 * source of truth: the bundle is regenerated from `web/src`, and any drift shows
 * up as a modified or newly hashed file under `web/dist`. The build is fast
 * (~2s), so paying for it on every run is cheap.
 *
 * Why the exports below, when the sibling scripts export nothing: this is the
 * only script pinned by a unit test (`check-dist-stale.test.mjs`), and the failure
 * message is the acceptance-relevant part — it must name the fix. The pure
 * parsing and reporting are therefore exported, and the CLI body only runs when
 * this module is the entry point, so importing it has no side effects.
 *
 * A missing git, or a repo root that is not a work tree, is a clean skip (exit 0
 * with a loud message): the script also runs inside a Docker build context and in
 * an unpacked sdist, where git state is irrelevant and the dist that was shipped
 * is the dist that was built. `check-storyboard.mjs` sets the same precedent with
 * its absent-document skip.
 *
 * Plain Node ESM, no dependencies. Node >= 22.12 (web/package.json engines).
 */

import { execFileSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const webRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const repoRoot = join(webRoot, '..');

const NOT_A_WORK_TREE =
  'SKIP  not a git work tree — cannot compare web/dist against a commit';
const NOT_A_WORK_TREE_WHY =
  '      (expected inside a Docker build context or an unpacked sdist, where git state is irrelevant)';

let failures = 0;

/**
 * Record and print one check, in the same shape as `check-dist.mjs`.
 *
 * @param {boolean} ok Whether the check passed.
 * @param {string} message What was checked, printed verbatim.
 */
function report(ok, message) {
  if (!ok) failures += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${message}`);
}

/**
 * Strip the surrounding double quotes git puts around an unusual path.
 *
 * Git also C-escapes characters inside a quoted path (`\303\244` for a UTF-8
 * byte); decoding that is a job for `-z`, which this script deliberately does not
 * use because the readable form is what a human needs to see in the failure
 * message. Stripping the quotes is enough to name the file.
 *
 * @param {string} path A path as git printed it, possibly quoted.
 * @returns {string} the path without surrounding quotes
 */
function unquote(path) {
  if (path.length >= 2 && path.startsWith('"') && path.endsWith('"')) {
    return path.slice(1, -1);
  }
  return path;
}

/**
 * Parse `git status --porcelain` output into the paths that changed.
 *
 * Porcelain v1 is `XY<space><path>`, and a rename is written `XY old -> new` with
 * each side quoted independently when needed. The NEW path is what gets reported:
 * an old path that no longer exists cannot be inspected.
 *
 * @param {string} porcelainStdout Raw stdout of the status command.
 * @returns {{status: string, path: string}[]} one entry per changed path
 */
export function parseDistStatus(porcelainStdout) {
  const changed = [];
  for (const line of porcelainStdout.split(/\r?\n/)) {
    if (line.trim().length === 0) continue;
    const status = line.slice(0, 2);
    let path = line.slice(3);
    // A path may legitimately contain " -> ", but git treats the first occurrence
    // as the separator, so matching git's own reading is the honest choice.
    const renameAt = path.indexOf(' -> ');
    if (renameAt !== -1) path = path.slice(renameAt + 4);
    changed.push({ status, path: unquote(path) });
  }
  return changed;
}

/**
 * Build the failure output: what changed and what to do about it.
 *
 * Returned rather than printed so the wording can be asserted in a unit test —
 * a stale-bundle gate whose message does not name the fix just teaches people to
 * ignore it.
 *
 * @param {{status: string, path: string}[]} changed Entries from parseDistStatus.
 * @returns {string[]} the lines to print, in order
 */
export function staleReport(changed) {
  const lines = ['FAIL  committed web/dist is stale or hand-edited:'];
  for (const entry of changed) {
    lines.push(`  ${entry.status} ${entry.path}`);
  }
  lines.push('  fix: run `npm run build` in web/ and commit the result.');
  lines.push(
    '  A changed frontend must ship its rebuilt bundle in the same commit — otherwise the panel'
  );
  lines.push('  keeps serving the old JavaScript.');
  return lines;
}

/**
 * Ask git whether a directory is inside a work tree.
 *
 * @param {string} dir The directory to test.
 * @returns {boolean} true only when git exists and answers "true"
 */
function isWorkTree(dir) {
  try {
    const stdout = execFileSync('git', ['-C', dir, 'rev-parse', '--is-inside-work-tree'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore']
    });
    return stdout.trim() === 'true';
  } catch {
    // ENOENT (no git on PATH) and "not a git repository" both land here, and both
    // mean the same thing for this check: there is no commit to compare against.
    return false;
  }
}

/**
 * Rebuild, then require the committed bundle to be untouched by that rebuild.
 */
function main() {
  console.log('check-dist-stale: rebuilding web/dist from web/src before comparing');

  try {
    execFileSync('npm', ['run', 'build'], {
      cwd: webRoot,
      stdio: ['ignore', 'inherit', 'inherit']
    });
  } catch {
    report(false, 'web/dist could not be built — fix the build before checking staleness');
    console.log(`check-dist-stale: ${failures} failure(s)`);
    process.exit(1);
  }

  if (!isWorkTree(repoRoot)) {
    console.log(NOT_A_WORK_TREE);
    console.log(NOT_A_WORK_TREE_WHY);
    console.log('check-dist-stale: OK (skipped)');
    process.exit(0);
  }

  const status = execFileSync(
    'git',
    ['-C', repoRoot, 'status', '--porcelain', '--untracked-files=normal', '--', 'web/dist'],
    { encoding: 'utf8' }
  );
  const changed = parseDistStatus(status);

  if (changed.length > 0) {
    for (const line of staleReport(changed)) console.log(line);
    console.log(`check-dist-stale: ${changed.length} changed path(s) under web/dist`);
    process.exit(1);
  }

  report(true, 'committed web/dist matches a fresh build of web/src');
  console.log('check-dist-stale: OK');
  process.exit(failures === 0 ? 0 : 1);
}

const entryPoint = process.argv[1] === undefined ? null : resolve(process.argv[1]);
if (entryPoint !== null && entryPoint === fileURLToPath(import.meta.url)) {
  main();
}
