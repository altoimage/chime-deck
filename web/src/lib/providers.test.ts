/**
 * Unit tests for the external-provider rules (HSD-s856).
 *
 * Node environment, no DOM and no fetch: `providers.ts` is where the draft, the save body
 * and the wording of one test synthesis are decided, so this is where they are pinned. The
 * load-bearing rules are the honesty ones — an untouched secret sends no key at all, a
 * cleared one sends `null` and never `''`, and a price the server did not measure is
 * reported as unknown rather than printed as `$0`.
 */

import { describe, expect, it } from 'vitest';

import type { TtsPresetSummary, TtsProvider, TtsProviderTestResponse } from './api.js';
import {
  UNPRICED,
  describeTestResult,
  emptyProviderDraft,
  formatCostPer100Chars,
  formatLatency,
  hasLanguagePlaceholder,
  hasVoicePlaceholder,
  paidSentence,
  presetOptions,
  providerBody,
  providerDraft,
  providerProblems,
  selectedProviderName,
  type ProviderDraft
} from './providers.js';

function presetOf(overrides: Partial<TtsPresetSummary> = {}): TtsPresetSummary {
  return {
    id: 'openai',
    label: 'OpenAI',
    description: 'OpenAI’s hosted speech API.',
    paid: true,
    format: 'mp3',
    cost_per_1k_chars_usd: 0.016,
    pricing_note: 'estimate, OpenAI tts-1',
    requires_base_url: false,
    shape: {
      method: 'POST',
      path: '/v1/audio/speech',
      headers: { 'Content-Type': 'application/json' },
      auth: { style: 'bearer', name: 'Authorization', prefix: 'Bearer ' },
      body_kind: 'json',
      body: { model: 'tts-1', input: '{text}', voice: { id: '{voice}' } },
      response: 'audio',
      format: 'mp3'
    },
    ...overrides
  };
}

/** A preset whose voice travels in the path, so a voice is required. */
function pathVoicePreset(overrides: Partial<TtsPresetSummary> = {}): TtsPresetSummary {
  return presetOf({
    id: 'elevenlabs',
    label: 'ElevenLabs',
    requires_base_url: true,
    shape: {
      ...presetOf().shape,
      path: '/v1/text-to-speech/{voice}',
      body: { text: '{text}', model_id: 'eleven_multilingual_v2' }
    },
    ...overrides
  });
}

function providerOf(overrides: Partial<TtsProvider> = {}): TtsProvider {
  return {
    id: 'p-1',
    name: 'OpenAI main',
    preset: 'openai',
    base_url: 'https://api.openai.com',
    voice: 'alloy',
    language: 'en',
    paid: true,
    cost_per_1k_chars_usd: 0.016,
    has_api_key: true,
    overrides: {},
    ...overrides
  };
}

function draftOf(overrides: Partial<ProviderDraft> = {}): ProviderDraft {
  return {
    name: 'OpenAI main',
    preset: 'openai',
    baseUrl: 'https://api.openai.com',
    voice: 'alloy',
    language: 'en',
    paid: true,
    pricePer1k: '0.016',
    apiKey: '',
    clearApiKey: false,
    ...overrides
  };
}

function testResultOf(overrides: Partial<TtsProviderTestResponse> = {}): TtsProviderTestResponse {
  return {
    ok: true,
    provider: 'p-1',
    latency_ms: 42.4,
    chars: 45,
    format: 'mp3',
    cost_per_100_chars_usd: 0.0016,
    estimated_cost_usd: 0.00072,
    error: null,
    ...overrides
  };
}

describe('emptyProviderDraft', () => {
  it('starts blank with the preset chosen and its billing flag', () => {
    const draft = emptyProviderDraft(presetOf());

    expect(draft).toEqual({
      name: '',
      preset: 'openai',
      baseUrl: '',
      voice: '',
      language: '',
      paid: true,
      pricePer1k: '',
      apiKey: '',
      clearApiKey: false
    });
  });

  it('leaves the price blank so the preset estimate is the one in force', () => {
    expect(emptyProviderDraft(presetOf()).pricePer1k).toBe('');
    expect(emptyProviderDraft(presetOf({ paid: false, cost_per_1k_chars_usd: null })).paid).toBe(
      false
    );
  });
});

describe('providerDraft', () => {
  it('maps a stored row onto the editable strings', () => {
    const draft = providerDraft(providerOf());

    expect(draft).toEqual({
      name: 'OpenAI main',
      preset: 'openai',
      baseUrl: 'https://api.openai.com',
      voice: 'alloy',
      language: 'en',
      paid: true,
      pricePer1k: '0.016',
      apiKey: '',
      clearApiKey: false
    });
  });

  it('never holds the stored key, only that one exists', () => {
    const draft = providerDraft(providerOf({ has_api_key: true }));
    expect(draft.apiKey).toBe('');
    expect(draft.clearApiKey).toBe(false);
  });

  it('shows an unpriced provider as an empty price, not 0', () => {
    expect(providerDraft(providerOf({ cost_per_1k_chars_usd: null })).pricePer1k).toBe('');
  });
});

describe('providerBody', () => {
  it('sends no api_key key at all for an untouched secret', () => {
    const body = providerBody(draftOf({ apiKey: '', clearApiKey: false }));
    const serialised = JSON.parse(JSON.stringify(body)) as Record<string, unknown>;

    expect('api_key' in serialised).toBe(false);
  });

  it('sends null for a pending clear', () => {
    const body = providerBody(draftOf({ apiKey: '', clearApiKey: true }));
    const serialised = JSON.parse(JSON.stringify(body)) as Record<string, unknown>;

    expect(serialised['api_key']).toBeNull();
  });

  it('sends the typed key as a string', () => {
    const body = providerBody(draftOf({ apiKey: 'sk-test' }));
    const serialised = JSON.parse(JSON.stringify(body)) as Record<string, unknown>;

    expect(serialised['api_key']).toBe('sk-test');
  });

  it('lets a pending clear win over a typed key', () => {
    const serialised = JSON.parse(
      JSON.stringify(providerBody(draftOf({ apiKey: 'sk-test', clearApiKey: true })))
    ) as Record<string, unknown>;

    expect(serialised['api_key']).toBeNull();
  });

  it('never sends an empty string as a secret', () => {
    const serialised = JSON.parse(JSON.stringify(providerBody(draftOf()))) as Record<
      string,
      unknown
    >;
    expect(serialised['api_key']).not.toBe('');
  });

  it('sends every plain field the form edits', () => {
    const body = providerBody(draftOf({ name: 'Kitchen', voice: 'nova', language: 'en_GB' }));

    expect(body.name).toBe('Kitchen');
    expect(body.preset).toBe('openai');
    expect(body.base_url).toBe('https://api.openai.com');
    expect(body.voice).toBe('nova');
    expect(body.language).toBe('en_GB');
    expect(body.paid).toBe(true);
  });

  it('turns an empty price into null, meaning the preset estimate', () => {
    expect(providerBody(draftOf({ pricePer1k: '' })).cost_per_1k_chars_usd).toBeNull();
    expect(providerBody(draftOf({ pricePer1k: '   ' })).cost_per_1k_chars_usd).toBeNull();
  });

  it('parses a numeric price and never rounds it to 0', () => {
    expect(providerBody(draftOf({ pricePer1k: '0.02' })).cost_per_1k_chars_usd).toBe(0.02);
    expect(providerBody(draftOf({ pricePer1k: '0' })).cost_per_1k_chars_usd).toBe(0);
  });

  it('omits a non-numeric price instead of inventing a 0', () => {
    const serialised = JSON.parse(
      JSON.stringify(providerBody(draftOf({ pricePer1k: 'about a cent' })))
    ) as Record<string, unknown>;

    expect('cost_per_1k_chars_usd' in serialised).toBe(false);
  });

  it('never carries overrides, so a save cannot wipe a custom shape', () => {
    const serialised = JSON.parse(JSON.stringify(providerBody(draftOf()))) as Record<
      string,
      unknown
    >;

    expect('overrides' in serialised).toBe(false);
  });
});

describe('providerProblems', () => {
  it('accepts a complete draft', () => {
    expect(providerProblems(draftOf(), presetOf())).toEqual([]);
    expect(providerProblems(draftOf({ preset: 'elevenlabs' }), pathVoicePreset())).toEqual([]);
  });

  it('accepts a blank base URL when the preset names its own server', () => {
    expect(providerProblems(draftOf({ baseUrl: '' }), presetOf())).toEqual([]);
  });

  it('refuses a blank name', () => {
    expect(providerProblems(draftOf({ name: '   ' }), presetOf())).toContain(
      'This provider needs a name.'
    );
  });

  it('requires a base URL when the preset has none of its own', () => {
    const problems = providerProblems(
      draftOf({ baseUrl: '' }),
      pathVoicePreset({ requires_base_url: true })
    );
    expect(problems).toContain(
      'This preset cannot name its own server, so the base URL is required.'
    );
  });

  it('refuses an address that is not http(s)://', () => {
    expect(providerProblems(draftOf({ baseUrl: 'api.example.com' }), presetOf())).toContain(
      'The base URL must start with http:// or https://.'
    );
    expect(providerProblems(draftOf({ baseUrl: 'https://api.example.com' }), presetOf())).toEqual(
      []
    );
  });

  it('requires a voice when the preset puts {voice} in the request', () => {
    const problems = providerProblems(
      draftOf({ voice: '' }),
      pathVoicePreset({ requires_base_url: true })
    );
    expect(problems).toContain('This preset puts the voice in the request, so a voice is required.');
  });

  it('does not demand a voice the preset never uses', () => {
    const noVoice = pathVoicePreset({
      shape: { ...presetOf().shape, path: '/v1/audio', body: { text: '{text}' } }
    });
    expect(providerProblems(draftOf({ voice: '' }), noVoice)).toEqual([]);
  });

  it('refuses a price that is not a number', () => {
    expect(providerProblems(draftOf({ pricePer1k: 'cheap' }), presetOf())).toContain(
      "The price per 1,000 characters must be a number, or empty to use the preset's estimate."
    );
  });

  it('refuses a negative price', () => {
    expect(providerProblems(draftOf({ pricePer1k: '-1' }), presetOf())).toContain(
      'The price per 1,000 characters cannot be negative.'
    );
  });

  it('accepts a price on a preset that bills nothing, without inventing one', () => {
    const freePreset = presetOf({ paid: false, cost_per_1k_chars_usd: null });
    expect(providerProblems(draftOf({ pricePer1k: '0.002' }), freePreset)).toEqual([]);
  });

  it('still checks the name and price when the preset is unknown', () => {
    expect(providerProblems(draftOf({ name: '', baseUrl: '' }), null)).toEqual([
      'This provider needs a name.'
    ]);
  });
});

describe('placeholder walks', () => {
  it('finds {voice} in the path, a header value or a nested body leaf', () => {
    expect(hasVoicePlaceholder(pathVoicePreset())).toBe(true);

    const headerVoice = presetOf({
      shape: {
        ...presetOf().shape,
        path: '/v1/audio',
        headers: { 'X-Voice-Id': '{voice}' },
        body: { text: '{text}' }
      }
    });
    expect(hasVoicePlaceholder(headerVoice)).toBe(true);

    const nestedVoice = presetOf({
      shape: {
        ...presetOf().shape,
        path: '/v1/audio',
        body: { audio: { speaker: { id: '{voice}' } } }
      }
    });
    expect(hasVoicePlaceholder(nestedVoice)).toBe(true);
  });

  it('says no when the preset never uses the voice', () => {
    const noVoice = presetOf({
      shape: { ...presetOf().shape, path: '/v1/audio', body: { text: '{text}', tags: ['a'] } }
    });
    expect(hasVoicePlaceholder(noVoice)).toBe(false);
  });

  it('walks the same places for {language}', () => {
    expect(hasLanguagePlaceholder(presetOf())).toBe(false);

    const langPath = presetOf({
      shape: { ...presetOf().shape, path: '/v1/speak/{language}' }
    });
    expect(hasLanguagePlaceholder(langPath)).toBe(true);

    const langBody = presetOf({
      shape: { ...presetOf().shape, body: { text: '{text}', language_code: '{language}' } }
    });
    expect(hasLanguagePlaceholder(langBody)).toBe(true);

    const langArray = presetOf({
      shape: { ...presetOf().shape, body: [{ value: '{language}' }] }
    });
    expect(hasLanguagePlaceholder(langArray)).toBe(true);
  });
});

describe('formatLatency', () => {
  it('prints a measured round trip in whole milliseconds', () => {
    expect(formatLatency(42.4)).toBe('42 ms');
    expect(formatLatency(0)).toBe('0 ms');
  });

  it('prints an em dash when nothing was measured', () => {
    expect(formatLatency(null)).toBe('—');
  });
});

describe('formatCostPer100Chars', () => {
  it('keeps enough decimals for a tiny per-100-character figure', () => {
    expect(formatCostPer100Chars(0.0015)).toBe('$0.0015');
    expect(formatCostPer100Chars(0.0016)).toBe('$0.0016');
  });

  it('prints a free price as $0, which is a real answer', () => {
    expect(formatCostPer100Chars(0)).toBe('$0');
  });

  it('prints an absent price as unknown, never as $0', () => {
    expect(formatCostPer100Chars(null)).toBe('cost unknown');
    expect(formatCostPer100Chars(null)).toBe(UNPRICED);
  });
});

describe('describeTestResult', () => {
  it('names the measured latency and the priced cost', () => {
    const sentence = describeTestResult(testResultOf());

    expect(sentence).toContain('42 ms');
    expect(sentence).toContain('$0.0016 per 100 characters');
  });

  it('refuses to invent a cost the server did not price', () => {
    const sentence = describeTestResult(
      testResultOf({ cost_per_100_chars_usd: null, estimated_cost_usd: null })
    );

    expect(sentence).toContain('unknown');
    expect(sentence).toContain('no price is configured');
    expect(sentence).not.toContain('$0');
  });

  it('says the latency was not measured instead of guessing one', () => {
    const sentence = describeTestResult(testResultOf({ latency_ms: null }));

    expect(sentence).toContain('did not report how long');
    expect(sentence).not.toContain(' ms');
  });

  it('renders a failed test as the server’s own sentence', () => {
    const sentence = describeTestResult(
      testResultOf({
        ok: false,
        latency_ms: null,
        chars: null,
        format: null,
        cost_per_100_chars_usd: null,
        estimated_cost_usd: null,
        error: 'the provider answered 401 — check the API key'
      })
    );

    expect(sentence).toBe('the provider answered 401 — check the API key');
  });

  it('never leaves a failure blank', () => {
    expect(
      describeTestResult(testResultOf({ ok: false, error: null }))
    ).toBe('The provider test failed and the server gave no reason.');
  });
});

describe('presetOptions', () => {
  it('keeps the server’s order and labels, with the billing fact in the description', () => {
    const presets = [
      pathVoicePreset({ id: 'elevenlabs', label: 'ElevenLabs', paid: true }),
      presetOf({ id: 'custom', label: 'Custom', paid: false, cost_per_1k_chars_usd: null })
    ];

    const options = presetOptions(presets);

    expect(options.map((option) => option.value)).toEqual(['elevenlabs', 'custom']);
    expect(options.map((option) => option.label)).toEqual(['ElevenLabs', 'Custom']);
    expect(options[0]?.description).toContain('is paid');
    expect(options[1]?.description).toContain('no billing');
  });

  it('keeps the preset’s own sentence ahead of the marker', () => {
    const option = presetOptions([presetOf({ description: 'Speaks on OpenAI.' })]).at(0);

    expect(option?.description?.startsWith('Speaks on OpenAI.')).toBe(true);
  });

  it('still words a preset that carries no description', () => {
    const option = presetOptions([presetOf({ description: '' })]).at(0);

    expect(option?.description).toBe('This preset is paid: it bills for every character sent.');
  });
});

describe('paidSentence', () => {
  it('names the estimated cost per 100 characters for a priced paid provider', () => {
    const sentence = paidSentence(providerOf(), [presetOf()]);

    expect(sentence).toContain('The OpenAI preset is paid');
    expect(sentence).toContain('$0.0016 per 100 characters');
  });

  it('says the price is unknown when a paid provider has no price', () => {
    const sentence = paidSentence(providerOf({ cost_per_1k_chars_usd: null }), [presetOf()]);

    expect(sentence).toContain('price is unknown');
    expect(sentence).not.toContain('$0');
  });

  it('falls back to a neutral subject when the preset is not in the list', () => {
    expect(paidSentence(providerOf(), [])).toContain('This provider is paid');
  });

  it('says nothing for a provider that bills nothing', () => {
    expect(paidSentence(providerOf({ paid: false }), [presetOf()])).toBeNull();
  });
});

describe('selectedProviderName', () => {
  it('names the selected provider', () => {
    expect(
      selectedProviderName({
        providers: [providerOf(), providerOf({ id: 'p-2', name: 'Backup' })],
        presets: [presetOf()],
        selected: 'p-2',
        warning: null
      })
    ).toBe('Backup');
  });

  it('answers empty when nothing is selected or the id is gone', () => {
    const base = { providers: [providerOf()], presets: [presetOf()], warning: null };

    expect(selectedProviderName({ ...base, selected: '' })).toBe('');
    expect(selectedProviderName({ ...base, selected: 'p-gone' })).toBe('');
  });
});
