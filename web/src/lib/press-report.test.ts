/**
 * Unit tests for the press report (HSD-bw8h).
 *
 * Node environment, no DOM: `press-report.ts` decides what a panel may claim about a press
 * it just made. The load-bearing rule is provenance — an engine is only a `'press'` fact
 * when the response's `detail` named it, and a sound file is never a press fact at all
 * (`press_log` holds `sound_name`, `PressResponse` does not).
 */

import { describe, expect, it } from 'vitest';

import type { PressResponse } from './api.js';
import { describePress, formatLatency } from './press-report.js';

function pressOf(overrides: Partial<PressResponse> = {}): PressResponse {
  return {
    button_id: 7,
    button_name: 'Front Door',
    source: 'ui_test',
    outcome: 'played',
    pressed_at: '2026-01-01T00:00:00+00:00',
    code: null,
    detail: null,
    synthesis_ms: null,
    ...overrides
  };
}

const TTS_BUTTON = { action_type: 'tts', tts_engine: 'piper' };
const SOUND_BUTTON = { action_type: 'sound', tts_engine: null };

describe('outcome word and colour', () => {
  it('renames queued for a finished request', () => {
    const report = describePress(pressOf({ outcome: 'queued' }), SOUND_BUTTON, 'Chime', 42, 'ha_tts');

    expect(report.outcomeLabel).toBe('Queued for playback');
    expect(report.kind).toBe('ok');
  });

  it('keeps the strip labels for the terminal outcomes', () => {
    expect(describePress(pressOf({ outcome: 'played' }), SOUND_BUTTON, 'Chime', 1, 'ha_tts').outcomeLabel).toBe(
      'Played'
    );
    expect(describePress(pressOf({ outcome: 'failed' }), SOUND_BUTTON, 'Chime', 1, 'ha_tts').outcomeLabel).toBe(
      'Failed'
    );
    expect(
      describePress(pressOf({ outcome: 'suppressed_quiet_hours' }), SOUND_BUTTON, 'Chime', 1, 'ha_tts')
        .outcomeLabel
    ).toBe('Quiet hours');
    expect(
      describePress(pressOf({ outcome: 'suppressed_muted' }), SOUND_BUTTON, 'Chime', 1, 'ha_tts')
        .outcomeLabel
    ).toBe('Muted');
  });

  it('prints an outcome this UI does not know verbatim, and stays neutral', () => {
    const report = describePress(pressOf({ outcome: 'throttled' }), SOUND_BUTTON, 'Chime', 1, 'ha_tts');

    expect(report.outcomeLabel).toBe('throttled');
    expect(report.kind).toBe('neutral');
  });

  it('colours each known outcome', () => {
    const kindOf = (outcome: string) =>
      describePress(pressOf({ outcome }), SOUND_BUTTON, 'Chime', 1, 'ha_tts').kind;

    expect(kindOf('played')).toBe('ok');
    expect(kindOf('queued')).toBe('ok');
    expect(kindOf('failed')).toBe('alert');
    expect(kindOf('deduped')).toBe('warn');
    expect(kindOf('suppressed_quiet_hours')).toBe('warn');
    expect(kindOf('suppressed_muted')).toBe('warn');
  });
});

describe('engine named by the press', () => {
  it('reports the engine the detail names, as a press fact', () => {
    const report = describePress(
      pressOf({ outcome: 'played', detail: 'engine piper' }),
      TTS_BUTTON,
      null,
      42,
      'ha_tts'
    );

    expect(report.engineOrFileLabel).toBe('Engine used');
    expect(report.engineOrFileValue).toBe('piper');
    expect(report.engineOrFileProvenance).toBe('press');
  });

  it('finds the engine inside a longer detail sentence', () => {
    const report = describePress(
      pressOf({ detail: 'engine ha_tts; the remote voice answered in 900 ms' }),
      TTS_BUTTON,
      null,
      10,
      'piper'
    );

    expect(report.engineOrFileValue).toBe('ha_tts');
    expect(report.engineOrFileProvenance).toBe('press');
  });
});

describe('sound presses', () => {
  it('reports the library name as configuration, never as a press fact', () => {
    const report = describePress(pressOf(), SOUND_BUTTON, 'Ding dong', 42, 'ha_tts');

    expect(report.engineOrFileLabel).toBe('Sound used');
    expect(report.engineOrFileValue).toBe('Ding dong');
    expect(report.engineOrFileProvenance).toBe('configured');
  });

  it('admits it does not know which file played when the library has no such name', () => {
    const report = describePress(pressOf(), SOUND_BUTTON, null, 42, 'ha_tts');

    expect(report.engineOrFileLabel).toBe('Sound used');
    expect(report.engineOrFileValue).toBe('the sound this button plays');
    expect(report.engineOrFileProvenance).toBe('unknown');
  });
});

describe('TTS presses that name no engine', () => {
  it('falls back to the button’s engine, as configuration', () => {
    const report = describePress(
      pressOf({ outcome: 'failed', code: 'tts_disabled', detail: 'speech is off' }),
      TTS_BUTTON,
      null,
      42,
      'ha_tts'
    );

    expect(report.engineOrFileLabel).toBe('Engine used');
    expect(report.engineOrFileValue).toBe('piper');
    expect(report.engineOrFileProvenance).toBe('configured');
    expect(report.detail).toBe('speech is off');
  });

  it('falls back to the default engine when the button names none', () => {
    const report = describePress(pressOf(), { action_type: 'tts', tts_engine: null }, null, 42, 'espeak_ng');

    expect(report.engineOrFileValue).toBe('espeak_ng');
    expect(report.engineOrFileProvenance).toBe('configured');
  });

  it('admits it has no engine to point at', () => {
    const report = describePress(pressOf(), { action_type: 'tts', tts_engine: null }, null, 42, '');

    expect(report.engineOrFileLabel).toBe('Engine used');
    expect(report.engineOrFileValue).toBe('an engine this press did not name');
    expect(report.engineOrFileProvenance).toBe('unknown');
  });

  it('works without the button at all', () => {
    const report = describePress(pressOf(), null, null, 42, 'ha_tts');

    expect(report.engineOrFileValue).toBe('ha_tts');
    expect(report.engineOrFileProvenance).toBe('configured');
  });
});

describe('detail and latency', () => {
  it('carries the detail verbatim', () => {
    const detail = 'the sound with id 3 is missing from the library — rescan the folder';

    expect(describePress(pressOf({ detail }), SOUND_BUTTON, 'Chime', 42, 'ha_tts').detail).toBe(detail);
  });

  it('passes the round trip through untouched', () => {
    expect(describePress(pressOf(), SOUND_BUTTON, 'Chime', 42.7, 'ha_tts').roundTripMs).toBe(42.7);
  });

  it('rounds latency to a whole millisecond', () => {
    expect(formatLatency(42.4)).toBe('42 ms');
    expect(formatLatency(42.5)).toBe('43 ms');
    expect(formatLatency(0)).toBe('0 ms');
  });
});
