/**
 * Unit tests for the sound library's rules (HSD-w9eq).
 *
 * Node environment, no DOM and no fetch: `sound-library.ts` is where every string the sounds
 * panel prints is decided, so this is where the wording is pinned. The load-bearing rules are
 * the honesty ones — a rescan's `removed` is "flagged missing", a confirmed delete really
 * deletes the buttons and never the file, and a malformed 409 body degrades to "no names"
 * instead of throwing inside a click handler.
 */

import { describe, expect, it } from 'vitest';

import type { AffectedButton, SoundRow } from './api.js';
import {
  affectedButtonNames,
  affectedButtonsFromError,
  deleteConfirmLabel,
  deleteConfirmMessage,
  deleteConfirmTitle,
  describeSourceType,
  formatSoundDuration,
  rescanSummary
} from './sound-library.js';

function rowOf(overrides: Partial<SoundRow> = {}): SoundRow {
  return {
    id: 3,
    name: 'Ding dong',
    source_type: 'file',
    path: 'ding.mp3',
    url: null,
    sha256: 'a'.repeat(64),
    format: 'mp3',
    duration_ms: 1800,
    bytes: 24000,
    missing: false,
    created_at: '2026-01-01T00:00:00+00:00',
    ...overrides
  };
}

function buttonOf(id: number, name: string): AffectedButton {
  return { id, name, slug: name.toLowerCase().replace(/[^a-z0-9]+/g, '-') };
}

describe('formatSoundDuration', () => {
  it('prints seconds with one decimal, as the storyboard does', () => {
    expect(formatSoundDuration(1800)).toBe('1.8 s');
    expect(formatSoundDuration(400)).toBe('0.4 s');
    expect(formatSoundDuration(0)).toBe('0.0 s');
  });

  it('stays in seconds for very long sounds', () => {
    expect(formatSoundDuration(125_000)).toBe('125.0 s');
  });

  it('renders an unstatable duration as a placeholder', () => {
    expect(formatSoundDuration(null)).toBe('—');
    expect(formatSoundDuration(-400)).toBe('—');
    expect(formatSoundDuration(Number.NaN)).toBe('—');
    expect(formatSoundDuration(Number.POSITIVE_INFINITY)).toBe('—');
    expect(formatSoundDuration(Number.NEGATIVE_INFINITY)).toBe('—');
  });
});

describe('describeSourceType', () => {
  it('names the two stored source types the library writes', () => {
    expect(describeSourceType('file')).toBe('folder');
    expect(describeSourceType('url')).toBe('URL download');
  });

  it('returns a source type this build does not know verbatim', () => {
    expect(describeSourceType('microphone')).toBe('microphone');
    expect(describeSourceType('')).toBe('');
  });
});

describe('rescanSummary', () => {
  it('joins the three counts in the frame’s exact wording', () => {
    expect(rescanSummary({ added: 2, removed: 1, missing: 3 })).toBe('added: 2 · removed: 1 · missing: 3');
  });

  it('keeps zeros rather than dropping a count', () => {
    expect(rescanSummary({ added: 0, removed: 0, missing: 0 })).toBe('added: 0 · removed: 0 · missing: 0');
  });
});

describe('affectedButtonNames', () => {
  it('is empty when nothing is affected', () => {
    expect(affectedButtonNames([])).toBe('');
  });

  it('prints one name unchanged', () => {
    expect(affectedButtonNames([buttonOf(1, 'Front Door')])).toBe('Front Door');
  });

  it('joins two names with and', () => {
    expect(affectedButtonNames([buttonOf(1, 'Front Door'), buttonOf(2, 'Evening notice')])).toBe(
      'Front Door and Evening notice'
    );
  });

  it('joins three names with a comma and a final and, no Oxford comma', () => {
    expect(
      affectedButtonNames([buttonOf(1, 'A'), buttonOf(2, 'B'), buttonOf(3, 'C')])
    ).toBe('A, B and C');
  });

  it('joins four names the same way', () => {
    expect(
      affectedButtonNames([buttonOf(1, 'A'), buttonOf(2, 'B'), buttonOf(3, 'C'), buttonOf(4, 'D')])
    ).toBe('A, B, C and D');
  });
});

describe('deleteConfirmTitle', () => {
  it('uses the typographic quotes the other panels use', () => {
    expect(deleteConfirmTitle('Front Door')).toBe('Delete “Front Door”?');
    expect(deleteConfirmTitle('Ding dong')).toBe('Delete “Ding dong”?');
  });
});

describe('deleteConfirmMessage', () => {
  it('names the buttons and says the buttons are deleted, because they are', () => {
    const message = deleteConfirmMessage(rowOf(), [
      buttonOf(1, 'Front Door'),
      buttonOf(2, 'Evening notice')
    ]);

    expect(message).toBe(
      'This sound is used by: Front Door and Evening notice. Confirming deletes the sound AND these buttons.'
    );
    expect(message).toContain('Front Door');
    expect(message).toContain('Evening notice');
  });

  it('says the file on disk survives when no button uses the sound', () => {
    expect(deleteConfirmMessage(rowOf(), [])).toBe(
      'Deleting this sound removes it from the library. The file on disk is not deleted.'
    );
  });

  it('reads the same for a missing row, because the route treats both alike', () => {
    expect(deleteConfirmMessage(rowOf({ missing: true, path: 'gone.mp3' }), [])).toBe(
      'Deleting this sound removes it from the library. The file on disk is not deleted.'
    );
  });
});

describe('deleteConfirmLabel', () => {
  it('names the buttons when they will go too', () => {
    expect(deleteConfirmLabel([buttonOf(1, 'Front Door')])).toBe('Delete sound and buttons');
  });

  it('stays short when no button is affected', () => {
    expect(deleteConfirmLabel([])).toBe('Delete sound');
  });
});

describe('affectedButtonsFromError', () => {
  it('reads a well-formed 409 detail', () => {
    const detail = {
      affected_buttons: [
        { id: 1, name: 'Front Door', slug: 'front-door' },
        { id: 2, name: 'Evening notice', slug: 'evening-notice' }
      ]
    };

    expect(affectedButtonsFromError(detail)).toEqual([
      { id: 1, name: 'Front Door', slug: 'front-door' },
      { id: 2, name: 'Evening notice', slug: 'evening-notice' }
    ]);
  });

  it('returns nothing for a detail that is not an object', () => {
    expect(affectedButtonsFromError(undefined)).toEqual([]);
    expect(affectedButtonsFromError(null)).toEqual([]);
    expect(affectedButtonsFromError('Consult the manual.')).toEqual([]);
    expect(affectedButtonsFromError(42)).toEqual([]);
  });

  it('returns nothing for an object that does not name the key', () => {
    expect(affectedButtonsFromError({ field: 'name' })).toEqual([]);
    expect(affectedButtonsFromError({})).toEqual([]);
  });

  it('returns nothing when the key is not an array', () => {
    expect(affectedButtonsFromError({ affected_buttons: 'Front Door' })).toEqual([]);
    expect(affectedButtonsFromError({ affected_buttons: { id: 1 } })).toEqual([]);
    expect(affectedButtonsFromError({ affected_buttons: null })).toEqual([]);
  });

  it('drops junk entries and keeps the valid ones', () => {
    const detail = {
      affected_buttons: [
        null,
        7,
        'Front Door',
        { name: 'Front Door', slug: 'front-door' },
        { id: '1', name: 'Front Door', slug: 'front-door' },
        { id: 1, name: 42, slug: 'front-door' },
        { id: 1, name: 'Front Door', slug: null },
        { id: 2, name: 'Evening notice', slug: 'evening-notice' }
      ]
    };

    expect(affectedButtonsFromError(detail)).toEqual([
      { id: 2, name: 'Evening notice', slug: 'evening-notice' }
    ]);
  });

  it('copies only the three fields it needs, never the raw entry', () => {
    const detail = {
      affected_buttons: [{ id: 1, name: 'Front Door', slug: 'front-door', enabled: false, secret: 'nope' }]
    };

    const buttons = affectedButtonsFromError(detail);

    expect(buttons).toEqual([{ id: 1, name: 'Front Door', slug: 'front-door' }]);
    expect(Object.keys(buttons[0])).toEqual(['id', 'name', 'slug']);
    expect(buttons[0]).not.toHaveProperty('enabled');
  });
});
