/**
 * Unit tests for the pre-render rules (HSD-dzek).
 *
 * Node environment, no DOM and no fetch: `bake.ts` is where the bake decisions live, so this
 * is where they are pinned. The load-bearing ones are the own-key feature check (a row with
 * no bake key means an older server, not an unbaked button — rendering "Not pre-rendered"
 * there would invent a state the backend never reported) and the failure precedence (a stale
 * digest next to a stored error must never read as a successful bake).
 */

import { describe, expect, it } from 'vitest';

import type { BakeAllResponse, ButtonDetail } from './api.js';
import {
  bakeReportIsEmpty,
  bakeStateOf,
  canPreRender,
  describeBakeReport,
  describeBakeStatus,
  isBakeAllResponse,
  isBakeUsage,
  type BakeStatus
} from './bake.js';

/**
 * A complete `GET /api/buttons` row with **no** `baked_*` key at all.
 *
 * That absence is deliberate: it is the older-server shape, so every bake test opts into the
 * feature by adding the keys it needs.
 */
function buttonRow(overrides: Partial<ButtonDetail> = {}): ButtonDetail {
  return {
    id: 1,
    slug: 'front-door',
    name: 'Front Door',
    icon: null,
    action_type: 'sound',
    sound_id: 1,
    tts_text: null,
    tts_engine: null,
    volume_override: null,
    dedupe_seconds: null,
    show_on_dashboard: true,
    quiet_hours_exempt: false,
    enabled: true,
    sort_order: 1,
    created_at: '2026-11-12T00:00:00Z',
    updated_at: '2026-11-12T00:00:00Z',
    mqtt: {
      command: 'chime_deck/default/button/front-door/set',
      discovery: 'homeassistant/button/chime_deck_default_front-door/config'
    },
    slug_change: null,
    ...overrides
  };
}

/** A speech row, ready for the pre-render checks. */
function speechRow(overrides: Partial<ButtonDetail> = {}): ButtonDetail {
  return buttonRow({
    action_type: 'tts',
    sound_id: null,
    tts_text: 'Someone is at the door',
    tts_engine: null,
    ...overrides
  });
}

/** A completed batch with no work in any category. */
function report(overrides: Partial<BakeAllResponse> = {}): BakeAllResponse {
  return {
    baked: [],
    skipped_dynamic: [],
    skipped_current: [],
    failed: [],
    ...overrides
  };
}

describe('bakeStateOf', () => {
  it('reports no state at all when the row carries none of the bake keys', () => {
    const row = buttonRow();

    expect(bakeStateOf(row)).toBeNull();
    expect('baked_sha' in row).toBe(false);
    expect('baked_error' in row).toBe(false);
  });

  it('treats bake keys inherited from a prototype as absent', () => {
    const base = buttonRow({
      baked_sha: 'deadbeef',
      baked_format: 'wav',
      baked_at: '2026-11-12T00:00:00Z',
      baked_error: null
    });
    const inherited = Object.create(base) as ButtonDetail;

    expect(bakeStateOf(inherited)).toBeNull();
  });

  it('reads a stored digest with its format and timestamp', () => {
    const row = buttonRow({
      baked_sha: 'deadbeef',
      baked_format: 'wav',
      baked_at: '2026-11-12T00:00:00Z'
    });

    expect(bakeStateOf(row)).toEqual({
      kind: 'baked',
      sha: 'deadbeef',
      format: 'wav',
      at: '2026-11-12T00:00:00Z'
    });
  });

  it('normalises blank metadata to a null format and an empty timestamp', () => {
    const row = buttonRow({ baked_sha: 'deadbeef', baked_format: '   ', baked_at: '  ' });

    expect(bakeStateOf(row)).toEqual({ kind: 'baked', sha: 'deadbeef', format: null, at: '' });
  });

  it('normalises metadata the row omits to a null format and an empty timestamp', () => {
    const row = buttonRow({ baked_sha: 'deadbeef' });

    expect(bakeStateOf(row)).toEqual({ kind: 'baked', sha: 'deadbeef', format: null, at: '' });
  });

  it('reports a stored failure even when a stale digest is present', () => {
    const row = buttonRow({
      baked_sha: 'stale-sha',
      baked_format: 'wav',
      baked_at: '2026-11-12T00:00:00Z',
      baked_error: 'espeak-ng exited 1'
    });

    expect(bakeStateOf(row)).toEqual({ kind: 'failed', reason: 'espeak-ng exited 1' });
  });

  it('treats cleared keys as not baked', () => {
    const row = buttonRow({ baked_sha: null, baked_error: null });

    expect(bakeStateOf(row)).toEqual({ kind: 'not_baked' });
  });

  it('treats a row of blank metadata as not baked', () => {
    const row = buttonRow({ baked_sha: '', baked_format: '', baked_at: '', baked_error: '' });

    expect(bakeStateOf(row)).toEqual({ kind: 'not_baked' });
  });
});

describe('describeBakeStatus', () => {
  it('describes a stored bake as pre-rendered', () => {
    expect(describeBakeStatus({ kind: 'baked', sha: 'deadbeef', format: 'wav', at: '' })).toEqual({
      kind: 'ok',
      label: 'Pre-rendered'
    });
  });

  it('describes an unbaked row with the warning tone', () => {
    expect(describeBakeStatus({ kind: 'not_baked' })).toEqual({
      kind: 'warn',
      label: 'Not pre-rendered'
    });
  });

  it('keeps the engine sentence verbatim after the failed prefix', () => {
    const reason = 'espeak-ng exited 1: "no voice" (see log)';
    const status: BakeStatus = { kind: 'failed', reason };

    expect(describeBakeStatus(status)).toEqual({ kind: 'alert', label: `failed: ${reason}` });
    expect(describeBakeStatus(status).label.endsWith(reason)).toBe(true);
  });
});

describe('canPreRender', () => {
  it('offers a pre-render for static spoken text', () => {
    expect(canPreRender(speechRow())).toBe(true);
  });

  it('refuses a Jinja expression', () => {
    expect(canPreRender(speechRow({ tts_text: 'The {{ states("sensor.temp") }} is here' }))).toBe(false);
  });

  it('refuses a Jinja statement', () => {
    expect(canPreRender(speechRow({ tts_text: '{% if now().hour > 18 %}Evening{% endif %}' }))).toBe(false);
  });

  it('refuses a template that spans lines', () => {
    expect(canPreRender(speechRow({ tts_text: 'Hello\n{{ states("sensor.who") }}\ndoor' }))).toBe(false);
  });

  it('accepts an unclosed brace pair, matching the backend', () => {
    expect(canPreRender(speechRow({ tts_text: 'Odd {{ but not a template' }))).toBe(true);
  });

  it('refuses a sound row', () => {
    expect(canPreRender(buttonRow({ action_type: 'sound' }))).toBe(false);
  });

  it('offers a pre-render when there is no text to inspect', () => {
    expect(canPreRender(speechRow({ tts_text: null }))).toBe(true);
  });
});

describe('isBakeUsage', () => {
  it('accepts a measured body', () => {
    expect(isBakeUsage({ baked: { files: 3, bytes: 4096 }, cache: { files: 0, bytes: 0 } })).toBe(true);
  });

  it('rejects a body with no cache directory', () => {
    expect(isBakeUsage({ baked: { files: 3, bytes: 4096 } })).toBe(false);
  });

  it('rejects a file count that is a string', () => {
    expect(isBakeUsage({ baked: { files: '3', bytes: 4096 }, cache: { files: 0, bytes: 0 } })).toBe(false);
  });

  it('rejects a fractional byte total', () => {
    expect(isBakeUsage({ baked: { files: 3, bytes: 4096.5 }, cache: { files: 0, bytes: 0 } })).toBe(false);
  });

  it('rejects a negative count', () => {
    expect(isBakeUsage({ baked: { files: -1, bytes: 0 }, cache: { files: 0, bytes: 0 } })).toBe(false);
  });

  it('rejects null, an array and an HTML answer', () => {
    expect(isBakeUsage(null)).toBe(false);
    expect(isBakeUsage([{ files: 0, bytes: 0 }])).toBe(false);
    expect(isBakeUsage('<!doctype html><title>proxy error</title>')).toBe(false);
  });
});

describe('isBakeAllResponse', () => {
  it('accepts a complete four-list body', () => {
    expect(
      isBakeAllResponse({
        baked: [1, 2],
        skipped_dynamic: [3],
        skipped_current: [],
        failed: [{ button: 4, error: 'espeak-ng exited 1' }]
      })
    ).toBe(true);
  });

  it('rejects a body missing the baked list', () => {
    expect(
      isBakeAllResponse({ skipped_dynamic: [], skipped_current: [], failed: [] })
    ).toBe(false);
  });

  it('rejects a body missing the dynamic list', () => {
    expect(isBakeAllResponse({ baked: [], skipped_current: [], failed: [] })).toBe(false);
  });

  it('rejects a body missing the current list', () => {
    expect(isBakeAllResponse({ baked: [], skipped_dynamic: [], failed: [] })).toBe(false);
  });

  it('rejects a body missing the failed list', () => {
    expect(isBakeAllResponse({ baked: [], skipped_dynamic: [], skipped_current: [] })).toBe(false);
  });

  it('rejects a failure entry with no error sentence', () => {
    expect(
      isBakeAllResponse({ baked: [], skipped_dynamic: [], skipped_current: [], failed: [{ button: 1 }] })
    ).toBe(false);
  });

  it('rejects ids sent as strings', () => {
    expect(
      isBakeAllResponse({ baked: ['1'], skipped_dynamic: [], skipped_current: [], failed: [] })
    ).toBe(false);
  });

  it('rejects ids sent as floats', () => {
    expect(
      isBakeAllResponse({ baked: [1.5], skipped_dynamic: [], skipped_current: [], failed: [] })
    ).toBe(false);
  });
});

describe('describeBakeReport', () => {
  it('keeps the four categories separate and in order', () => {
    const sections = describeBakeReport(report({ baked: [1], skipped_current: [2], skipped_dynamic: [3], failed: [] }));

    expect(sections.map((section) => [section.kind, section.label])).toEqual([
      ['baked', 'Baked now'],
      ['current', 'Already up to date'],
      ['dynamic', 'Rendered on every press'],
      ['failed', 'Failed']
    ]);
  });

  it('resolves names from the caller snapshot', () => {
    const names = new Map([
      [1, 'Front Door'],
      [2, 'Back Door']
    ]);

    const sections = describeBakeReport(
      report({ baked: [1], skipped_current: [2], skipped_dynamic: [] }),
      names
    );

    expect(sections[0].entries).toEqual(['Front Door']);
    expect(sections[1].entries).toEqual(['Back Door']);
  });

  it('falls back to the id when the snapshot misses a button', () => {
    const sections = describeBakeReport(report({ baked: [9] }), new Map([[1, 'Front Door']]));

    expect(sections[0].entries).toEqual(['Button #9']);
  });

  it('falls back to the id when no snapshot is given', () => {
    const sections = describeBakeReport(report({ skipped_dynamic: [12] }));

    expect(sections[2].entries).toEqual(['Button #12']);
  });

  it('renders a failure as name, em dash and the untouched error', () => {
    const error = 'espeak-ng exited 1: "no voice"';
    const sections = describeBakeReport(report({ failed: [{ button: 4, error }] }), new Map([[4, 'Front Door']]));

    expect(sections[3].entries).toEqual([`Front Door — ${error}`]);
  });
});

describe('bakeReportIsEmpty', () => {
  it('is true when every category is empty', () => {
    expect(bakeReportIsEmpty(report())).toBe(true);
  });

  it('is false when a button was baked', () => {
    expect(bakeReportIsEmpty(report({ baked: [1] }))).toBe(false);
  });

  it('is false when a button was already current', () => {
    expect(bakeReportIsEmpty(report({ skipped_current: [1] }))).toBe(false);
  });

  it('is false when a button was skipped as dynamic', () => {
    expect(bakeReportIsEmpty(report({ skipped_dynamic: [1] }))).toBe(false);
  });

  it('is false when a button failed', () => {
    expect(bakeReportIsEmpty(report({ failed: [{ button: 1, error: 'espeak-ng exited 1' }] }))).toBe(false);
  });
});
