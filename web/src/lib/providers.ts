/**
 * The Speech panel's external-provider rules (HSD-s856): the draft a form edits, the body a
 * save sends, the client-side checks and the honest wording of one test synthesis.
 *
 * This module decides, the panel renders. Three things live here because they are rules
 * rather than markup:
 *
 *   - the **draft ↔ body** split. A secret has three states and they are not
 *     interchangeable: an untouched field sends **no `api_key` key at all**, a pending clear
 *     sends `null`, and a typed key sends the string. An empty string is never sent — the
 *     server rejects it ("send null to clear a secret, not an empty string"). The same
 *     absent/`null` split decides a price: `''` means "use the preset's estimate".
 *   - the **client-side checks**. They mirror the server's, so a save that can only fail is
 *     refused before the round trip; the server stays the authority. Each is a whole
 *     sentence, and none of them invents a price.
 *   - the **wording of a test result**. The server measures a latency and prices a phrase
 *     from the provider's configured cost; when it has no price it says `null`, and `null`
 *     is reported as unknown — never as `$0`.
 *
 * What this module deliberately does **not** do: it calls no API (the panel owns the
 * promises, the errors and the `ApiError` sentences), it holds no DOM, no runes and no
 * state, and it never fabricates a latency, a character count or a price. The preset's own
 * `description` and `pricing_note` are the server's sentences and travel through verbatim.
 *
 * Pure TS, no DOM and no runes: a component renders, this module decides.
 */

import type {
  TtsPresetSummary,
  TtsProvider,
  TtsProviderTestResponse,
  TtsProvidersResponse,
  TtsProviderWriteBody
} from './api.js';
import { formatLatency as formatMeasuredMs } from './press-report.js';

/** The one sentence for "the server measured no price", shared so it cannot drift. */
export const UNPRICED = 'cost unknown';

/** Mirrors the server's own `^$|^https?://\S+$` address rule, minus the empty case. */
const HTTP_URL_PATTERN = /^https?:\/\/\S+$/i;

/** One new provider's editable fields: the strings a form holds, not the wire shape. */
export interface ProviderDraft {
  name: string;
  preset: string;
  baseUrl: string;
  voice: string;
  language: string;
  paid: boolean;
  /** The string the price input shows; `''` means "use the preset's estimate". */
  pricePer1k: string;
  /** Always starts empty: the server never returns a stored key. */
  apiKey: string;
  /** The user asked to remove the stored key; wins over `apiKey`. */
  clearApiKey: boolean;
}

/**
 * The draft for adding one provider of *preset*.
 *
 * The name starts blank on purpose: prefilling the preset's label would collide with the
 * second provider of the same preset, which the server rejects (duplicate names are
 * refused), so the form asks for a name instead of proposing one that can only fail.
 * `pricePer1k` is blank, which is "use the preset's estimate" — the preset's own
 * `cost_per_1k_chars_usd` is never copied into the input, so nothing looks user-set that
 * is not.
 *
 * @param preset the preset library row the user picked.
 */
export function emptyProviderDraft(preset: TtsPresetSummary): ProviderDraft {
  return {
    name: '',
    preset: preset.id,
    baseUrl: '',
    voice: '',
    language: '',
    paid: preset.paid,
    pricePer1k: '',
    apiKey: '',
    clearApiKey: false
  };
}

/**
 * An existing provider as an editable draft (open the form; rebase after a save).
 *
 * `apiKey` is always empty and `clearApiKey` false: `has_api_key` is all the server ever
 * says about the key, so a draft must never hold a value it could render — type a new one
 * to replace it, or ask to clear it.
 *
 * @param provider the row from `GET /api/tts/providers`.
 */
export function providerDraft(provider: TtsProvider): ProviderDraft {
  return {
    name: provider.name,
    preset: provider.preset,
    baseUrl: provider.base_url,
    voice: provider.voice,
    language: provider.language,
    paid: provider.paid,
    pricePer1k: provider.cost_per_1k_chars_usd === null ? '' : String(provider.cost_per_1k_chars_usd),
    apiKey: '',
    clearApiKey: false
  };
}

/** A draft's price as the number to send, or `null` for "not a usable number". */
function priceNumber(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === '') {
    return null;
  }
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
}

/**
 * The `POST`/`PATCH` body for one draft.
 *
 * The secret rule, in the order that makes it safe: a pending clear wins and sends
 * `api_key: null`; a typed key sends the string; an untouched draft sends **no `api_key`
 * key at all** (never `''`, which the server rejects). `pricePer1k` becomes a number, and
 * `''` becomes `null` — "return to the preset's estimate", not a fabricated `0`. A value
 * that is not a number leaves the key absent, so a PATCH keeps the stored price while
 * `providerProblems` blocks the save; nothing is silently rounded to `0`.
 *
 * `overrides` is deliberately absent: the draft does not edit a `custom` provider's request
 * shape, so a save never wipes one (an omitted key keeps the stored value).
 *
 * @param draft the form's current values.
 */
export function providerBody(draft: ProviderDraft): TtsProviderWriteBody {
  const body: TtsProviderWriteBody = {
    name: draft.name,
    preset: draft.preset,
    base_url: draft.baseUrl,
    voice: draft.voice,
    language: draft.language,
    paid: draft.paid
  };

  if (draft.pricePer1k.trim() === '') {
    body.cost_per_1k_chars_usd = null;
  } else {
    const price = priceNumber(draft.pricePer1k);
    if (price !== null) {
      body.cost_per_1k_chars_usd = price;
    }
  }

  if (draft.clearApiKey) {
    body.api_key = null;
  } else if (draft.apiKey !== '') {
    body.api_key = draft.apiKey;
  }

  return body;
}

/**
 * The client-side problems with one draft, as whole sentences; `[]` when it is fine.
 *
 * These mirror the server's rules so a save that can only fail is refused before the round
 * trip, and the server stays the authority. With `preset === null` (a stored provider whose
 * preset this build no longer ships) only the checks that need no preset run — the name and
 * the price — rather than guessing at a shape nobody can describe.
 *
 * A price on a preset that is not paid is *not* a problem: a user may know their plan bills
 * where the preset says it does not. It is never invented, and never corrected here.
 *
 * @param draft the form's current values.
 * @param preset the picked preset's library row, or `null` when it is unknown.
 */
export function providerProblems(
  draft: ProviderDraft,
  preset: TtsPresetSummary | null
): string[] {
  const problems: string[] = [];

  if (draft.name.trim() === '') {
    problems.push('This provider needs a name.');
  }

  const baseUrl = draft.baseUrl.trim();
  if (preset !== null && preset.requires_base_url && baseUrl === '') {
    problems.push('This preset cannot name its own server, so the base URL is required.');
  }
  if (baseUrl !== '' && !HTTP_URL_PATTERN.test(baseUrl)) {
    problems.push('The base URL must start with http:// or https://.');
  }

  if (preset !== null && hasVoicePlaceholder(preset) && draft.voice.trim() === '') {
    problems.push('This preset puts the voice in the request, so a voice is required.');
  }

  if (draft.pricePer1k.trim() !== '') {
    const price = priceNumber(draft.pricePer1k);
    if (price === null) {
      problems.push(
        "The price per 1,000 characters must be a number, or empty to use the preset's estimate."
      );
    } else if (price < 0) {
      problems.push('The price per 1,000 characters cannot be negative.');
    }
  }

  return problems;
}

/** Every string leaf of a JSON value, into `into`; used by the placeholder walks. */
function bodyStrings(value: unknown, into: string[]): void {
  if (typeof value === 'string') {
    into.push(value);
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      bodyStrings(item, into);
    }
    return;
  }
  if (typeof value === 'object' && value !== null) {
    for (const item of Object.values(value)) {
      bodyStrings(item, into);
    }
  }
}

/** Whether the preset's request carries *placeholder* in its path, headers or body. */
function shapeCarries(preset: TtsPresetSummary, placeholder: string): boolean {
  const shape = preset.shape;
  if (shape.path.includes(placeholder)) {
    return true;
  }
  if (Object.values(shape.headers).some((value) => value.includes(placeholder))) {
    return true;
  }
  const texts: string[] = [];
  bodyStrings(shape.body, texts);
  return texts.some((text) => text.includes(placeholder));
}

/**
 * Whether this preset puts `{voice}` somewhere in the request it sends.
 *
 * Walks the path, every header value and every string leaf of the JSON body, so a preset
 * that addresses its voice in a nested object is found exactly like one that puts it in the
 * path. A preset that does not use the voice simply ignores the field, so the form must not
 * demand one.
 *
 * @param preset one row of the server's preset library.
 */
export function hasVoicePlaceholder(preset: TtsPresetSummary): boolean {
  return shapeCarries(preset, '{voice}');
}

/**
 * Whether this preset puts `{language}` somewhere in the request it sends.
 *
 * Same walk as `hasVoicePlaceholder`: path, header values and every string leaf of the body.
 *
 * @param preset one row of the server's preset library.
 */
export function hasLanguagePlaceholder(preset: TtsPresetSummary): boolean {
  return shapeCarries(preset, '{language}');
}

/**
 * A measured latency as the panel prints it, or an em dash when nothing was measured.
 *
 * The number itself is `press-report.ts`'s `formatLatency`, reused verbatim so the two
 * screens cannot round milliseconds differently; this module only adds the "no measurement"
 * state, which a press report never has (a press always has its own round trip).
 *
 * @param ms the server's measured round trip, or `null` when none was measured.
 */
export function formatLatency(ms: number | null): string {
  return ms === null ? '—' : formatMeasuredMs(ms);
}

/**
 * A price per 100 characters as dollars, to as many decimals as the figure needs.
 *
 * A zero price is a real, free price and prints as `$0`; `null` (and a non-finite number,
 * which no honest server sends) is `'cost unknown'`, never a made-up figure.
 */
function formatUsd(usd: number): string {
  if (usd === 0) {
    return '$0';
  }
  const magnitude = Math.abs(usd);
  const digits = magnitude < 0.01 ? 6 : magnitude < 1 ? 4 : 2;
  return `$${usd.toFixed(digits).replace(/0+$/, '').replace(/\.$/, '')}`;
}

/**
 * One price per 100 characters, or `'cost unknown'` when the server priced nothing.
 *
 * Per-100-character prices are tiny (`0.0016`), so a fixed two-decimal format would print
 * `$0.00` and read as free. The figure keeps enough decimals to stay meaningful and trims
 * the rest; `null` is reported as unknown, never as `$0`.
 *
 * @param usd dollars per 100 characters, or `null` when no price is known.
 */
export function formatCostPer100Chars(usd: number | null): string {
  if (usd === null || !Number.isFinite(usd)) {
    return UNPRICED;
  }
  return formatUsd(usd);
}

/**
 * One line for a test synthesis, built only from what the server measured.
 *
 * A failure is the server's own sentence, verbatim (nothing here rewords it). A success
 * names the measured latency and, when the provider has a price, what that costs per 100
 * characters — and when `cost_per_100_chars_usd` is `null` it says the cost is unknown
 * because no price is configured, rather than printing `$0`. A latency the server did not
 * report is named as unmeasured, never guessed.
 *
 * @param result the answer to `POST /api/tts/providers/{id}/test`.
 */
export function describeTestResult(result: TtsProviderTestResponse): string {
  if (!result.ok) {
    return result.error ?? 'The provider test failed and the server gave no reason.';
  }

  const spoken =
    result.latency_ms === null
      ? 'The provider spoke, but the server did not report how long the synthesis took'
      : `The provider spoke in ${formatLatency(result.latency_ms)}`;
  const cost =
    result.cost_per_100_chars_usd === null
      ? 'the cost per 100 characters is unknown because no price is configured for this provider'
      : `that costs ${formatCostPer100Chars(result.cost_per_100_chars_usd)} per 100 characters`;

  return `${spoken}; ${cost}.`;
}

/**
 * The preset library as `ui/Select` rows, in the order the server sent them.
 *
 * The description is the preset's own sentence with the billing fact appended as a word a
 * person can read *before* adding the provider — a paid row must never look free. The label
 * stays the server's, because that is what the provider row will be called.
 *
 * @param presets `TtsProvidersResponse.presets`, in server order.
 */
export function presetOptions(
  presets: TtsPresetSummary[]
): { value: string; label: string; description?: string }[] {
  return presets.map((preset) => {
    const billing = preset.paid
      ? 'This preset is paid: it bills for every character sent.'
      : 'This preset adds no billing of its own.';
    return {
      value: preset.id,
      label: preset.label,
      description: preset.description === '' ? billing : `${preset.description} ${billing}`
    };
  });
}

/**
 * One sentence about a paid provider, or `null` when it bills nothing.
 *
 * The estimate is the provider's own configured price per 100 characters; when the price is
 * unknown the sentence says so instead of implying a free request. The preset's label is
 * used only to name the subject — the price is never taken from the preset here, because
 * the provider row already carries the resolved figure.
 *
 * @param provider one row of `TtsProvidersResponse.providers`.
 * @param presets the same response's preset library, for the subject's name.
 */
export function paidSentence(
  provider: TtsProvider,
  presets: TtsPresetSummary[]
): string | null {
  if (!provider.paid) {
    return null;
  }

  const preset = presets.find((item) => item.id === provider.preset);
  const subject = preset === undefined ? 'This provider' : `The ${preset.label} preset`;

  if (provider.cost_per_1k_chars_usd === null) {
    return `${subject} is paid, but the price is unknown — set a price per 1,000 characters to see what a phrase costs.`;
  }

  return `${subject} is paid: about ${formatCostPer100Chars(
    provider.cost_per_1k_chars_usd / 10
  )} per 100 characters.`;
}

/**
 * The display name of the selected provider, or `''` when none is selected.
 *
 * A `selected` id no provider carries is "nothing selected", not the first row: a deleted
 * provider must never be shown as if it were still in charge.
 *
 * @param state the answer to `GET /api/tts/providers`.
 */
export function selectedProviderName(state: TtsProvidersResponse): string {
  const provider = state.providers.find((item) => item.id === state.selected);
  return provider === undefined ? '' : provider.name;
}
