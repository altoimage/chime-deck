#!/usr/bin/env node
/**
 * Storyboard coverage gate for `Project_state/design/05b-storyboards.md`.
 *
 * That document is a deliberately UNTRACKED design artifact: `Project_state/` is
 * git-ignored by design (.gitignore line 6) and AGENTS.md hard rule 5 forbids
 * committing anything from it. This script exists because the ticket that
 * produced the storyboards ("Storyboard every screen before UI implementation",
 * HSD-byp6) must still leave a reproducible, commit-able check behind — one that
 * is honest about what it can prove.
 *
 * So it validates STRUCTURE and SIZES, never visual taste:
 *
 *   - every screen/flow the ticket names has a section, and no invented extra one;
 *   - every screen shows all three storyboard resolutions (1024x600, 1280x800,
 *     1920x1080) in at least one frame label;
 *   - every screen declares which of the six canonical states apply and, for each
 *     one it excludes, gives a one-sentence reason — a bare "n/a" fails;
 *   - every interactive element annotation parses to a size, and meets D26:
 *     >= 48px, >= 56px when marked `primary`, >= 32px for a `thumb` (the one
 *     deliberate exception, design 05 §8);
 *   - every guillemet-quoted component reference resolves to a real `.svelte`
 *     file under `web/src/lib`, so the storyboard can never invent a primitive;
 *   - the document itself states its honesty limit, so the gate cannot be
 *     mistaken for a design review.
 *
 * What a human must still judge: whether the frames are readable, whether the
 * layout is right, whether a flow is sensible, legibility and contrast on the
 * real panel, and whether 8px of space actually separates adjacent targets.
 *
 * A missing document is a clean skip (exit 0 with a clear message), exactly like
 * the integration tests that skip when a system binary is absent: a fresh clone
 * does not have the file, and `npm run verify` must stay green there.
 *
 * Plain Node ESM, no dependencies, offline, deterministic. Exit 1 on any failure.
 */

import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const webRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const repoRoot = join(webRoot, '..');
const docPath = join(repoRoot, 'Project_state', 'design', '05b-storyboards.md');
const libDir = join(webRoot, 'src', 'lib');

/** The ticket's screen/flow list, one id per storyboarded screen. */
const SCREENS = [
  'first-run-wizard',
  'dashboard-grid',
  'dashboard-tile',
  'dashboard-chime-strip',
  'dashboard-connection',
  'volume-sheet',
  'entity-picker',
  'entity-editor',
  'entity-reorder',
  'buttons-panel',
  'button-editor',
  'sounds-panel',
  'speech-panel',
  'audio-panel',
  'history-panel',
  'about-panel',
  'settings-shell',
  'appearance-panel'
];

const STATES = ['happy', 'empty', 'loading', 'error', 'stale', 'success'];

/** Labels that satisfy each canonical state. A stale view may honestly be called
 *  "disconnected" or "offline"; a failure "failed" or "rejected". */
const STATE_SYNONYMS = {
  happy: ['happy', 'hot path', 'loaded'],
  empty: ['empty', 'nothing'],
  loading: ['loading', 'in flight'],
  error: ['error', 'failure', 'failed', 'rejected'],
  stale: ['stale', 'disconnected', 'offline', 'no live data'],
  success: ['success', 'saved', 'done', 'confirmed']
};

const RESOLUTIONS = ['1024x600', '1280x800', '1920x1080'];

/** D26 minimums. --tap is 48px and --tap-primary is 56px in tokens.css. */
const MIN_TARGET = 48;
const MIN_PRIMARY = 56;
const MIN_THUMB = 32; // --thumb; the one documented exception
const MIN_FRAMES = 6;
const MIN_TARGETS = 3;

const HONESTY_SENTENCE = 'validates structure and sizes, not visual taste';

let failures = 0;
let checks = 0;

/**
 * Record and print one check.
 *
 * @param {boolean} ok Whether the check passed.
 * @param {string} message What was checked, printed verbatim.
 */
function report(ok, message) {
  checks += 1;
  if (!ok) failures += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${message}`);
}

/**
 * Record a fine-grained check that is only worth printing when it fails.
 *
 * The per-target and per-state assertions are numerous (they are what makes the
 * gate real) and each screen prints a rollup instead, so a green run stays
 * readable while a red one still names the exact offending annotation.
 *
 * @param {boolean} ok Whether the check passed.
 * @param {string} message What was checked, printed only on failure.
 */
function quiet(ok, message) {
  checks += 1;
  if (!ok) {
    failures += 1;
    console.log(`FAIL  ${message}`);
  }
}

/** `×` and `x` are the same thing; the storyboards are written by hand. */
function normalise(text) {
  return text.replaceAll('\u00d7', 'x');
}

/**
 * Components a decision has since retired. The storyboard in `Project_state/`
 * is a git-ignored design artifact (HSD-byp6) drawn before the decision landed,
 * and implementation tickets must not edit it, so a reference to a deleted
 * component is expected there. Naming it here keeps the check strict for every
 * component that still exists: an unknown name is still a failure.
 */
const RETIRED_COMPONENTS = new Map([['ChimeStrip', 'D29 / HSD-g4xa']]);

/**
 * Index every Svelte component shipped under web/src/lib, by basename.
 *
 * @returns {Set<string>} component names without the `.svelte` suffix
 */
function collectComponents() {
  const names = new Set();
  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
      } else if (entry.name.endsWith('.svelte')) {
        names.add(entry.name.slice(0, -'.svelte'.length));
      }
    }
  };
  if (existsSync(libDir)) walk(libDir);
  return names;
}

/**
 * Split the document into `###` blocks, the unit one screen is written in.
 *
 * @param {string[]} lines The document, one line per element.
 * @returns {{title: string, body: string[]}[]} the blocks in document order
 */
function splitBlocks(lines) {
  const blocks = [];
  let current = null;
  for (const [index, line] of lines.entries()) {
    if (line.startsWith('### ')) {
      if (current !== null) current.end = index;
      current = { title: line.slice(4).trim(), body: [], start: index, end: lines.length };
      blocks.push(current);
      continue;
    }
    if (line.startsWith('## ') && current !== null) {
      // A `##` section ends the current `###` block but is not one itself.
      current.end = index;
      current = null;
      continue;
    }
    if (current !== null) current.body.push(line);
  }
  return blocks;
}

/**
 * Read one `**Field:**` line out of a block.
 *
 * @param {string[]} body The block body.
 * @param {string} field The field name without asterisks, e.g. "States".
 * @returns {string | null} the value, or null when the line is absent
 */
function field(body, field) {
  const prefix = `**${field}:**`;
  for (const line of body) {
    if (line.startsWith(prefix)) return line.slice(prefix.length).trim();
  }
  return null;
}

function main() {
  if (!existsSync(docPath)) {
    console.log(
      'check-storyboard: Project_state/design/05b-storyboards.md is not present — it is a ' +
        'git-ignored design artifact (HSD-byp6), so a fresh clone has no storyboard to check. ' +
        'Structure and size checks skipped; nothing to verify.'
    );
    process.exit(0);
  }

  const doc = readFileSync(docPath, 'utf8');
  const flat = normalise(doc);
  // Markdown wraps prose, so a required sentence may be split over two lines.
  const unwrapped = flat.toLowerCase().replace(/\s+/g, ' ');
  const lines = doc.split(/\r?\n/);
  const blocks = splitBlocks(lines);

  // ---------------------------------------------------------------- document
  report(
    unwrapped.includes(HONESTY_SENTENCE),
    `the document states its own honesty limit ("${HONESTY_SENTENCE}")`
  );
  for (const [pattern, what] of [
    [/^##\s+.*reconciliation/im, 'a Reconciliation / errata section against design 05'],
    [/^##\s+.*backend dependenc/im, 'a per-panel backend dependency matrix'],
    [/^##\s+.*no implementation ticket/im, 'a section listing screens with no implementation ticket']
  ]) {
    report(pattern.test(doc), `the document has ${what}`);
  }

  // ------------------------------------------------------------------ screens
  const declared = new Map();
  for (const block of blocks) {
    const screenLine = field(block.body, 'Screen');
    if (screenLine === null) continue;
    const match = /^`([a-z0-9-]+)`/.exec(screenLine);
    const id = match === null ? '?' : match[1];
    if (declared.has(id)) {
      report(false, `screen id ${id} is declared twice ("${block.title}" and "${declared.get(id).title}")`);
      continue;
    }
    declared.set(id, block);
  }

  for (const id of SCREENS) {
    report(declared.has(id), `screen "${id}" has a section`);
  }
  for (const id of declared.keys()) {
    report(SCREENS.includes(id), `screen id ${id} is one the ticket names (no invented screens)`);
  }

  // ----------------------------------------------------------------- per screen
  let frameTotal = 0;
  let targetTotal = 0;
  for (const id of SCREENS) {
    const block = declared.get(id);
    if (block === undefined) continue;
    const body = block.body;

    // The four definition lines.
    const statesLine = field(body, 'States');
    const notApplicable = field(body, 'Not applicable');
    const resolutionsLine = field(body, 'Resolutions');
    report(statesLine !== null, `${id}: declares its states`);
    report(notApplicable !== null, `${id}: declares what is not applicable, with reasons`);
    report(resolutionsLine !== null, `${id}: declares its storyboard resolutions`);

    const states = (statesLine ?? '')
      .split(',')
      .map((token) => token.trim().toLowerCase())
      .filter((token) => token.length > 0);
    for (const state of states) {
      quiet(STATES.includes(state), `${id}: state "${state}" is one of the six canonical states`);
    }

    // Every excluded state must carry a reason; a bare "stale" fails. `;` is the
    // entry separator, so a `;` inside a sentence is read as a continuation of the
    // previous entry's reason rather than as a second, reasonless entry.
    const entries = [];
    for (const raw of (notApplicable ?? '').split(';')) {
      const chunk = raw.trim();
      if (chunk.length === 0) continue;
      if (chunk.toLowerCase() === 'none') continue;
      const head = /^([a-z ]+?)\s*[—–-]\s*(.*)$/i.exec(chunk);
      if (head === null) {
        if (entries.length > 0) {
          entries.at(-1).reason += `; ${chunk}`;
        } else {
          report(false, `${id}: the exclusion "${chunk}" is written "<state> — <reason>"`);
        }
        continue;
      }
      const state = head[1].trim().toLowerCase();
      if (state === 'none') continue;
      entries.push({ state, reason: head[2].trim() });
    }
    const excluded = entries.map((entry) => entry.state);
    for (const entry of entries) {
      quiet(
        entry.reason.length >= 20,
        `${id}: the exclusion "${entry.state}" carries a sentence of reasoning`
      );
    }

    const covered = new Set([...states, ...excluded]);
    for (const state of STATES) {
      quiet(
        covered.has(state),
        `${id}: state "${state}" is either storyboarded or explicitly excluded with a reason`
      );
    }
    report(
      STATES.every((state) => covered.has(state)),
      `${id}: all six states accounted for (${states.length} storyboarded, ` +
        `${excluded.length} excluded with a reason)`
    );

    // Frames: `**F<n> · text · resolution**`.
    const frames = [];
    for (const line of body) {
      const match = /^\*\*F\d+\s*·\s*(.+?)\*\*\s*$/.exec(line);
      if (match !== null) frames.push(normalise(match[1]).toLowerCase());
    }
    frameTotal += frames.length;
    report(
      frames.length >= MIN_FRAMES,
      `${id}: ${frames.length} frames (at least ${MIN_FRAMES} required)`
    );

    const missingStateFrames = [];
    for (const state of states) {
      const synonyms = STATE_SYNONYMS[state] ?? [state];
      const drawn = frames.some((label) => synonyms.some((word) => label.includes(word)));
      if (!drawn) missingStateFrames.push(state);
      quiet(drawn, `${id}: has a frame for the "${state}" state`);
    }
    report(
      missingStateFrames.length === 0,
      `${id}: every declared state has a frame` +
        (missingStateFrames.length === 0 ? `` : ` — missing: ${missingStateFrames.join(', ')}`)
    );

    const declaredResolutions = normalise(resolutionsLine ?? '').toLowerCase();
    const missingResolutions = [];
    for (const resolution of RESOLUTIONS) {
      quiet(
        declaredResolutions.includes(resolution),
        `${id}: Resolution line lists ${resolution}`
      );
      const drawn = frames.some((label) => label.includes(resolution));
      if (!drawn) missingResolutions.push(resolution);
      quiet(drawn, `${id}: has a frame drawn at ${resolution}`);
    }
    report(
      missingResolutions.length === 0,
      `${id}: drawn at all three resolutions` +
        (missingResolutions.length === 0 ? `` : ` — missing: ${missingResolutions.join(', ')}`)
    );

    // Every interactive element annotation parses and meets D26.
    const annotations = [...body.join('\n').matchAll(/\[target\s+([^\]]+)\]/gi)];
    targetTotal += annotations.length;
    report(
      annotations.length >= MIN_TARGETS,
      `${id}: annotates ${annotations.length} interactive targets (at least ${MIN_TARGETS})`
    );
    let smallest = Infinity;
    let badTargets = 0;
    for (const [, raw] of annotations) {
      const content = normalise(raw).trim().toLowerCase();
      const size = /^(\d+)\s*x\s*(\d+)\b(.*)$/.exec(content);
      if (size === null) {
        quiet(false, `${id}: target annotation "${raw}" parses as WxH`);
        badTargets += 1;
        continue;
      }
      const small = Math.min(Number(size[1]), Number(size[2]));
      const modifiers = size[3];
      const floor = modifiers.includes('thumb')
        ? MIN_THUMB
        : modifiers.includes('primary')
          ? MIN_PRIMARY
          : MIN_TARGET;
      if (!modifiers.includes('thumb')) smallest = Math.min(smallest, small);
      if (small < floor) badTargets += 1;
      quiet(
        small >= floor,
        `${id}: ${modifiers.includes('primary') ? 'primary ' : ''}target "${raw}" is ` +
          `${small}px (>= ${floor}px)`
      );
    }
    report(
      badTargets === 0,
      `${id}: ${annotations.length} targets meet D26` +
        (smallest === Infinity ? '' : ` (smallest non-thumb ${smallest}px)`) +
        (badTargets === 0 ? '' : ` — ${badTargets} below the minimum`)
    );
  }

  // ------------------------------------------------- prose, outside the screens
  // A stray annotation in the prose (a convention example, an errata table) is
  // still an annotation a reader will trust, so it obeys the same minimums.
  const coveredLines = new Uint8Array(lines.length);
  for (const block of declared.values()) {
    coveredLines.fill(1, block.start, block.end);
  }
  for (const [index, line] of lines.entries()) {
    if (coveredLines[index] === 1) continue;
    for (const [, raw] of line.matchAll(/\[target\s+([^\]]+)\]/gi)) {
      const content = normalise(raw).trim().toLowerCase();
      const size = /^(\d+)\s*x\s*(\d+)\b(.*)$/.exec(content);
      if (size === null) {
        quiet(false, `prose target annotation "${raw}" parses as WxH`);
        continue;
      }
      const small = Math.min(Number(size[1]), Number(size[2]));
      const modifiers = size[3];
      const floor = modifiers.includes('thumb')
        ? MIN_THUMB
        : modifiers.includes('primary')
          ? MIN_PRIMARY
          : MIN_TARGET;
      quiet(small >= floor, `prose target annotation "${raw}" is ${small}px (>= ${floor}px)`);
    }
  }
  // ---------------------------------------------------------------- components
  const components = collectComponents();
  const referenced = new Set(
    [...doc.matchAll(/«([^»]+)»/g)].map((match) => match[1].trim()).filter((name) => name.length > 0)
  );
  report(referenced.size > 0, 'the document references the component kit');
  for (const name of [...referenced].sort()) {
    const retiredBy = RETIRED_COMPONENTS.get(name);
    report(
      components.has(name) || retiredBy !== undefined,
      retiredBy === undefined
        ? `«${name}» exists as a component under web/src/lib`
        : `«${name}» was retired by ${retiredBy}, so the storyboard's reference is expected`
    );
  }

  console.log(
    `check-storyboard: ${SCREENS.length} screens, ${frameTotal} frames, ${targetTotal} targets ` +
      `(min ${MIN_TARGET}px, ${MIN_PRIMARY}px primary), ${referenced.size} components, ` +
      `${checks} checks, ${failures} failed`
  );
  if (failures === 0) {
    console.log(
      'check-storyboard: OK — structure and sizes only; layout, legibility and 8px target ' +
        'spacing still need a human eye on the real panel.'
    );
  }
  process.exit(failures === 0 ? 0 : 1);
}

main();
