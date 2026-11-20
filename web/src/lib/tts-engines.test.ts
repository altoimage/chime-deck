/**
 * Unit tests for the speech-engine options (HSD-bw8h).
 *
 * Node environment, no DOM: `tts-engines.ts` decides what the editor offers and why a row
 * may be refused. The load-bearing rules are that `none` is never disabled (D27 makes the
 * off state a supported choice), that an engine the route did not mention is selectable and
 * openly unknown, and that a refusal quotes the server's own reason.
 */

import { describe, expect, it } from 'vitest';

import type { TtsEnginesResponse } from './api.js';
import { ENGINE_LABELS, engineChoices, speechDisabled } from './tts-engines.js';

function statusOf(overrides: Partial<TtsEnginesResponse> = {}): TtsEnginesResponse {
  return {
    disabled: false,
    engines: [
      { name: 'piper', available: true, reason: null, last_synthesis_ms: 120 },
      { name: 'espeak_ng', available: true, reason: null, last_synthesis_ms: 8 }
    ],
    warning: null,
    ...overrides
  };
}

const WIRE_ORDER = ['ha_tts', 'piper', 'remote_tts', 'espeak_ng', 'none'];

describe('engineChoices', () => {
  it('offers the default row first, then the five wire choices in registry order', () => {
    const choices = engineChoices(statusOf(), 'piper');

    expect(choices[0]).toEqual({
      value: '',
      label: 'Use the default engine (piper)',
      description: 'Whichever engine Settings names as the default.',
      disabled: false
    });
    expect(choices.slice(1).map((choice) => choice.value)).toEqual(WIRE_ORDER);
  });

  it('labels every wire choice with its stored name in brackets', () => {
    const choices = engineChoices(statusOf(), 'piper').slice(1);

    for (const choice of choices) {
      expect(choice.label).toBe(ENGINE_LABELS[choice.value]);
      expect(choice.label).toContain(`(${choice.value})`);
    }
  });

  it('leaves an available engine selectable with no reason', () => {
    const piper = engineChoices(statusOf(), 'piper').find((choice) => choice.value === 'piper');

    expect(piper?.disabled).toBe(false);
    expect(piper?.reason).toBeUndefined();
  });

  it('disables an unavailable engine, quotes its reason verbatim and names the alternatives', () => {
    const status = statusOf({
      engines: [
        { name: 'piper', available: false, reason: 'piper needs the optional extra installed', last_synthesis_ms: null },
        { name: 'espeak_ng', available: true, reason: null, last_synthesis_ms: 8 },
        { name: 'ha_tts', available: true, reason: null, last_synthesis_ms: 400 }
      ]
    });

    const piper = engineChoices(status, 'piper').find((choice) => choice.value === 'piper');

    expect(piper?.disabled).toBe(true);
    expect(piper?.reason).toBe('piper needs the optional extra installed');
    expect(piper?.description).toContain('available instead: espeak_ng, ha_tts');
  });

  it('omits the alternatives pointer when no engine is available', () => {
    const status = statusOf({
      engines: [
        { name: 'piper', available: false, reason: 'piper needs 512 MB of RAM', last_synthesis_ms: null },
        { name: 'espeak_ng', available: false, reason: 'espeak-ng is not installed', last_synthesis_ms: null }
      ]
    });

    const descriptions = engineChoices(status, 'piper').map((choice) => choice.description ?? '');

    expect(descriptions.some((description) => description.includes('available instead'))).toBe(false);
  });

  it('keeps a reason missing rather than inventing one', () => {
    const status = statusOf({
      engines: [{ name: 'piper', available: false, reason: null, last_synthesis_ms: null }]
    });

    const piper = engineChoices(status, 'piper').find((choice) => choice.value === 'piper');

    expect(piper?.disabled).toBe(true);
    expect(piper?.reason).toBeUndefined();
  });

  it('marks an engine the route never mentioned as selectable and unknown', () => {
    const choices = engineChoices(statusOf(), 'piper');
    const haTts = choices.find((choice) => choice.value === 'ha_tts');
    const remote = choices.find((choice) => choice.value === 'remote_tts');

    expect(haTts?.disabled).toBe(false);
    expect(haTts?.description).toContain('did not report it');
    expect(remote?.disabled).toBe(false);
    expect(remote?.description).toContain('did not report it');
  });

  it('treats a missing status as unknown availability for every engine', () => {
    const choices = engineChoices(null, 'ha_tts');

    expect(choices).toHaveLength(6);
    expect(choices[0].label).toBe('Use the default engine (ha_tts)');
    for (const choice of choices.slice(1)) {
      expect(choice.disabled).toBe(false);
    }
    expect(choices[1].description).toContain('did not report it');
  });

  it('never disables none or the default row, whatever the status says', () => {
    const status = statusOf({
      disabled: true,
      engines: [
        { name: 'piper', available: false, reason: 'speech is switched off', last_synthesis_ms: null }
      ]
    });

    const choices = engineChoices(status, 'none');

    expect(choices[0].disabled).toBe(false);
    expect(choices.find((choice) => choice.value === 'none')?.disabled).toBe(false);
  });

  it('describes none as the supported silent state', () => {
    const none = engineChoices(statusOf(), 'piper').find((choice) => choice.value === 'none');

    expect(none?.label).toBe('No speech — this button stays silent (none)');
    expect(none?.description).toContain('keeps the rest of its configuration');
    expect(none?.reason).toBeUndefined();
  });
});

describe('speechDisabled', () => {
  it('trusts the live route over the stored setting', () => {
    expect(speechDisabled(statusOf({ disabled: true }), 'ha_tts')).toBe(true);
    expect(speechDisabled(statusOf({ disabled: false }), 'none')).toBe(false);
  });

  it('falls back to the stored default engine when the route did not answer', () => {
    expect(speechDisabled(null, 'none')).toBe(true);
    expect(speechDisabled(null, 'ha_tts')).toBe(false);
  });
});
