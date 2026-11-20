/*
 * The stale-bundle check is only useful if its failure output names the fix, so the
 * message text is asserted here rather than left to a human reading the script.
 */

import { describe, expect, it } from 'vitest';

import { parseDistStatus, staleReport } from './check-dist-stale.mjs';

describe('parseDistStatus', () => {
  it('reports nothing for a clean tree (a fresh bundle)', () => {
    expect(parseDistStatus('')).toEqual([]);
  });

  it('parses a modified tracked asset', () => {
    expect(parseDistStatus(' M web/dist/assets/index-Bf_6mFMq.js')).toEqual([
      { status: ' M', path: 'web/dist/assets/index-Bf_6mFMq.js' }
    ]);
  });

  it('parses an untracked asset rebuilt under a new hash', () => {
    expect(parseDistStatus('?? web/dist/assets/index-NEWHASH.js')).toEqual([
      { status: '??', path: 'web/dist/assets/index-NEWHASH.js' }
    ]);
  });

  it('parses a modified and a new asset in the same run', () => {
    const stdout = ' M web/dist/assets/index-Bf_6mFMq.js\n?? web/dist/assets/index-NEWHASH.js\n';
    const changed = parseDistStatus(stdout);

    expect(changed.map((entry) => entry.path)).toEqual([
      'web/dist/assets/index-Bf_6mFMq.js',
      'web/dist/assets/index-NEWHASH.js'
    ]);
  });

  it('reports the new path of a rename', () => {
    expect(parseDistStatus('R  web/dist/old.js -> web/dist/new.js')).toEqual([
      { status: 'R ', path: 'web/dist/new.js' }
    ]);
  });

  it('strips the quotes git puts around an unusual path', () => {
    expect(parseDistStatus('?? "web/dist/weird name.js"')).toEqual([
      { status: '??', path: 'web/dist/weird name.js' }
    ]);
  });
});

describe('staleReport', () => {
  it('names the fix and every changed path', () => {
    const changed = parseDistStatus(
      ' M web/dist/assets/index-Bf_6mFMq.js\n?? web/dist/assets/index-NEWHASH.js'
    );
    const lines = staleReport(changed);
    const text = lines.join('\n');

    expect(text).toContain('npm run build');
    for (const entry of changed) {
      expect(lines.some((line) => line.includes(entry.path))).toBe(true);
    }
  });
});
