/**
 * Unit tests for the chime-button editor's rules (HSD-bw8h).
 *
 * Node environment, no DOM and no fetch: `button-action.ts` is where every editing decision
 * lives, so this is where they are pinned. The load-bearing ones are the diff PATCH (an
 * untouched draft must send `{}` — a body that repeats stored values would let a stale form
 * rewrite a field nobody touched) and the two things that must never travel: `enabled` and
 * `sort_order`.
 */

import { describe, expect, it } from 'vitest';

import type { ButtonCreateBody, ButtonDetail, ButtonWriteBody } from './api.js';
import {
  EMPTY_DRAFT,
  changeBody,
  clientProblems,
  createBody,
  draftFrom,
  hasTemplateSyntax,
  isDirty,
  newDraft,
  parseAction,
  type ButtonDraft
} from './button-action.js';

function rowOf(overrides: Partial<ButtonDetail> = {}): ButtonDetail {
  return {
    id: 7,
    slug: 'front-door',
    name: 'Front Door',
    icon: 'mdi:door',
    action_type: 'sound',
    sound_id: 3,
    tts_text: null,
    tts_engine: null,
    volume_override: null,
    dedupe_seconds: null,
    show_on_dashboard: true,
    quiet_hours_exempt: false,
    enabled: true,
    sort_order: 1,
    created_at: '2026-01-01T00:00:00+00:00',
    updated_at: '2026-01-01T00:00:00+00:00',
    mqtt: { command: 'doorbell/front-door/press', discovery: 'homeassistant/button/front-door/config' },
    slug_change: null,
    ...overrides
  };
}

function draftOf(overrides: Partial<ButtonDraft> = {}): ButtonDraft {
  return { ...draftFrom(rowOf()), ...overrides };
}

describe('parseAction', () => {
  it('reads both stored action types', () => {
    expect(parseAction('tts')).toBe('tts');
    expect(parseAction('sound')).toBe('sound');
  });

  it('falls back to sound for a stored value this UI does not know', () => {
    expect(parseAction('beep')).toBe('sound');
    expect(parseAction('')).toBe('sound');
  });
});

describe('newDraft', () => {
  it('starts empty, with the sound action and both overrides untouched', () => {
    expect(newDraft()).toEqual({
      name: '',
      slug: '',
      icon: null,
      actionType: 'sound',
      soundChoice: '',
      ttsText: '',
      ttsEngine: '',
      volumeOverride: null,
      dedupeSeconds: null,
      showOnDashboard: false,
      quietHoursExempt: false
    });
  });

  it('hands out a copy, so two forms cannot share state', () => {
    const first = newDraft();
    first.name = 'Porch';
    first.ttsText = 'hello';

    expect(newDraft().name).toBe('');
    expect(EMPTY_DRAFT.name).toBe('');
  });
});

describe('draftFrom', () => {
  it('maps every null to an empty string and every number to a string', () => {
    const row = rowOf({
      slug: 'porch',
      icon: null,
      action_type: 'tts',
      sound_id: 12,
      tts_text: 'The doorbell is ringing',
      tts_engine: 'piper',
      volume_override: 40,
      dedupe_seconds: 15,
      show_on_dashboard: false,
      quiet_hours_exempt: true
    });

    expect(draftFrom(row)).toEqual({
      name: 'Front Door',
      slug: 'porch',
      icon: null,
      actionType: 'tts',
      soundChoice: '12',
      ttsText: 'The doorbell is ringing',
      ttsEngine: 'piper',
      volumeOverride: 40,
      dedupeSeconds: 15,
      showOnDashboard: false,
      quietHoursExempt: true
    });
  });

  it('leaves the sound choice blank when the button has no sound', () => {
    expect(draftFrom(rowOf({ action_type: 'tts', sound_id: null })).soundChoice).toBe('');
  });

  it('opens an unknown stored action type as a sound button', () => {
    expect(draftFrom(rowOf({ action_type: 'chime' })).actionType).toBe('sound');
  });
});

describe('changeBody', () => {
  it('sends nothing at all for an untouched draft', () => {
    const row = rowOf();

    expect(changeBody(draftOf(), row)).toEqual({});
    expect(isDirty(draftOf(), row)).toBe(false);
  });

  it('sends only the name when that is the only change', () => {
    expect(changeBody(draftOf({ name: 'Porch Bell' }), rowOf())).toEqual({ name: 'Porch Bell' });
  });

  it('trims the name before comparing it, and treats whitespace as blank', () => {
    expect(changeBody(draftOf({ name: '  Front Door  ' }), rowOf())).toEqual({});
    expect(changeBody(draftOf({ name: '   ' }), rowOf())).toEqual({ name: '' });
  });

  it('sends the slug when an edit renames the button', () => {
    expect(changeBody(draftOf({ slug: 'porch' }), rowOf())).toEqual({ slug: 'porch' });
  });

  it('sends no slug for a blank slug field', () => {
    // The server reads `slug: null` as "keep the stored slug", so sending nothing is the only
    // body that means what the empty field looks like.
    expect(changeBody(draftOf({ slug: '' }), rowOf())).toEqual({});
    expect(changeBody(draftOf({ slug: '   ' }), rowOf())).toEqual({});
  });

  it('never sends slug as null', () => {
    const body = changeBody(draftOf({ slug: '' }), rowOf({ slug: 'front-door' }));

    expect(Object.prototype.hasOwnProperty.call(body, 'slug')).toBe(false);
  });

  it('sends an icon change, and clears it with an explicit null', () => {
    expect(changeBody(draftOf({ icon: 'mdi:bell' }), rowOf())).toEqual({ icon: 'mdi:bell' });
    expect(changeBody(draftOf({ icon: null }), rowOf())).toEqual({ icon: null });
  });

  it('sends the action type when the user switches to speech', () => {
    expect(changeBody(draftOf({ actionType: 'tts' }), rowOf())).toEqual({ action_type: 'tts' });
  });

  it('sends the chosen sound as a number', () => {
    expect(changeBody(draftOf({ soundChoice: '9' }), rowOf())).toEqual({ sound_id: 9 });
  });

  it('clears the sound with null when the choice is blank', () => {
    expect(changeBody(draftOf({ soundChoice: '' }), rowOf())).toEqual({ sound_id: null });
  });

  it('sends no sound_id for a value that is not a positive integer', () => {
    // `Number('abc')` is NaN, and `JSON.stringify` would turn that into `null` — a silent
    // clear of the button's sound. The body keeps the key out; `clientProblems` explains.
    for (const choice of ['abc', '0', '-4', '2.5']) {
      expect(changeBody(draftOf({ soundChoice: choice }), rowOf())).toEqual({});
    }
  });

  it('treats a numeric string equal to the stored id as unchanged', () => {
    expect(changeBody(draftOf({ soundChoice: ' 3 ' }), rowOf())).toEqual({});
  });

  it('sends a typed phrase, and clears it with null', () => {
    const tts = rowOf({ action_type: 'tts', sound_id: null, tts_text: 'Dinner is ready' });

    expect(changeBody({ ...draftFrom(tts), ttsText: ' Supper is ready ' }, tts)).toEqual({
      tts_text: 'Supper is ready'
    });
    expect(changeBody({ ...draftFrom(tts), ttsText: '   ' }, tts)).toEqual({ tts_text: null });
  });

  it('sends the engine, and clears it with null so the default applies', () => {
    const row = rowOf({ action_type: 'tts', sound_id: null, tts_text: 'Hi', tts_engine: 'piper' });

    expect(changeBody({ ...draftFrom(row), ttsEngine: 'espeak_ng' }, row)).toEqual({
      tts_engine: 'espeak_ng'
    });
    expect(changeBody({ ...draftFrom(row), ttsEngine: '' }, row)).toEqual({ tts_engine: null });
  });

  it('sends the two overrides when they change, null included', () => {
    const row = rowOf({ volume_override: 40, dedupe_seconds: 10 });
    const draft = draftFrom(row);

    expect(changeBody({ ...draft, volumeOverride: 55 }, row)).toEqual({ volume_override: 55 });
    expect(changeBody({ ...draft, volumeOverride: null }, row)).toEqual({ volume_override: null });
    expect(changeBody({ ...draft, dedupeSeconds: 0 }, row)).toEqual({ dedupe_seconds: 0 });
    expect(changeBody({ ...draft, dedupeSeconds: null }, row)).toEqual({ dedupe_seconds: null });
  });

  it('sends the two flags when they change', () => {
    const row = rowOf({ show_on_dashboard: true, quiet_hours_exempt: false });

    expect(changeBody(draftOf({ showOnDashboard: false }), row)).toEqual({ show_on_dashboard: false });
    expect(changeBody(draftOf({ quietHoursExempt: true }), row)).toEqual({ quiet_hours_exempt: true });
  });

  it('reports several changes together and nothing else', () => {
    const row = rowOf();
    const draft = draftOf({ name: 'Porch Bell', volumeOverride: 30, quietHoursExempt: true });

    const body: ButtonWriteBody = changeBody(draft, row);

    expect(body).toEqual({ name: 'Porch Bell', volume_override: 30, quiet_hours_exempt: true });
    expect(Object.keys(body).sort()).toEqual(['name', 'quiet_hours_exempt', 'volume_override']);
  });

  it('never sends enabled or sort_order, even when the draft disagrees with them', () => {
    const row = rowOf({ enabled: false, sort_order: 4 });

    const body = changeBody(draftOf({ name: 'Porch Bell' }), row);

    expect(Object.prototype.hasOwnProperty.call(body, 'enabled')).toBe(false);
    expect(Object.prototype.hasOwnProperty.call(body, 'sort_order')).toBe(false);
  });

  it('reports a dirty draft', () => {
    expect(isDirty(draftOf({ name: 'Porch Bell' }), rowOf())).toBe(true);
  });
});

describe('createBody', () => {
  it('sends every field, slug null when it was left blank and enabled true', () => {
    const body: ButtonCreateBody = createBody({
      ...newDraft(),
      name: '  Porch Bell  ',
      icon: 'mdi:bell',
      actionType: 'tts',
      ttsText: '  Someone is at the door  ',
      ttsEngine: 'piper',
      volumeOverride: 40,
      dedupeSeconds: 5,
      showOnDashboard: true,
      quietHoursExempt: true
    });

    expect(body).toEqual({
      name: 'Porch Bell',
      action_type: 'tts',
      slug: null,
      icon: 'mdi:bell',
      sound_id: null,
      tts_text: 'Someone is at the door',
      tts_engine: 'piper',
      volume_override: 40,
      dedupe_seconds: 5,
      show_on_dashboard: true,
      quiet_hours_exempt: true,
      enabled: true
    });
  });

  it('sends an explicit slug, the chosen sound, and empty text as null', () => {
    const body = createBody({ ...newDraft(), name: 'Porch', slug: ' porch-bell ', soundChoice: '4' });

    expect(body.slug).toBe('porch-bell');
    expect(body.sound_id).toBe(4);
    expect(body.tts_text).toBeNull();
    expect(body.tts_engine).toBeNull();
  });

  it('sends no unusable sound as a number', () => {
    expect(createBody({ ...newDraft(), name: 'Porch', soundChoice: 'abc' }).sound_id).toBeNull();
  });

  it('keeps the inactive branch value it was given', () => {
    // A TTS button may still carry a sound: the API preserves a value the user did not ask
    // to drop, so the create body says exactly what the form holds.
    const body = createBody({ ...newDraft(), name: 'Porch', actionType: 'tts', soundChoice: '4' });

    expect(body.sound_id).toBe(4);
  });
});

describe('clientProblems', () => {
  const NAME = 'the name cannot be empty — it is what names the button in Home Assistant';
  const SLUG =
    'type a slug — an existing button keeps its slug unless you change it, and an empty field would not rename anything';
  const SOUND =
    'a sound button requires a sound_id — choose a sound from the library, or make this button speak';
  const PHRASE = 'a TTS button requires non-empty tts_text — type the phrase to speak';

  it('accepts the draft a stored row opens as', () => {
    expect(clientProblems(draftFrom(rowOf()), 'edit')).toEqual([]);
  });

  it('names a blank name with our own sentence', () => {
    expect(clientProblems(draftOf({ name: '  ' }), 'edit')).toEqual([{ field: 'name', message: NAME }]);
  });

  it('names a blank slug in edit mode only', () => {
    expect(clientProblems(draftOf({ slug: '' }), 'edit')).toEqual([{ field: 'slug', message: SLUG }]);

    const createFields = clientProblems(
      { ...newDraft(), name: 'Porch', actionType: 'tts', ttsText: 'Hello' },
      'create'
    ).map((problem) => problem.field);
    expect(createFields).not.toContain('slug');
  });

  it('names a sound button with no usable sound, in the server’s own sentence', () => {
    for (const soundChoice of ['', 'abc']) {
      expect(clientProblems(draftOf({ soundChoice }), 'edit')).toEqual([
        { field: 'sound_id', message: SOUND }
      ]);
    }
  });

  it('names a TTS button with no phrase, in the server’s own sentence', () => {
    expect(clientProblems(draftOf({ actionType: 'tts', ttsText: '  ' }), 'edit')).toEqual([
      { field: 'tts_text', message: PHRASE }
    ]);
  });

  it('accepts a TTS button with a phrase and no engine (the default applies)', () => {
    expect(clientProblems(draftOf({ actionType: 'tts', ttsText: 'Hi', ttsEngine: '' }), 'edit')).toEqual(
      []
    );
  });

  it('returns every problem in field order', () => {
    expect(clientProblems({ ...newDraft(), actionType: 'tts' }, 'edit')).toEqual([
      { field: 'name', message: NAME },
      { field: 'slug', message: SLUG },
      { field: 'tts_text', message: PHRASE }
    ]);
    expect(clientProblems(newDraft(), 'create')).toEqual([
      { field: 'name', message: NAME },
      { field: 'sound_id', message: SOUND }
    ]);
  });
});

describe('hasTemplateSyntax', () => {
  it('finds a Jinja expression and a statement', () => {
    expect(hasTemplateSyntax('a {{ b }} c')).toBe(true);
    expect(hasTemplateSyntax('x {% if y %}z')).toBe(true);
  });

  it('matches across line breaks, as the backend’s DOTALL does', () => {
    expect(hasTemplateSyntax('a {{\n b \n}} c')).toBe(true);
  });

  it('does not match an unclosed brace', () => {
    expect(hasTemplateSyntax('{{')).toBe(false);
    expect(hasTemplateSyntax('{% if')).toBe(false);
  });

  it('does not match plain text', () => {
    expect(hasTemplateSyntax('plain text')).toBe(false);
    expect(hasTemplateSyntax('')).toBe(false);
  });
});
