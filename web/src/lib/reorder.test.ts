/**
 * Unit tests for the reorder arithmetic (HSD-umw).
 *
 * Node environment, no DOM: `moveItem`/`reorderKeys` are pure array logic, and
 * `targetIndexFor` takes the row boxes it would measure in a browser — so the
 * drop rule is testable without layout, which no DOM test can really provide.
 */

import { describe, expect, it } from 'vitest';

import { moveItem, reorderKeys, targetIndexFor, type SortableItem } from './reorder.js';

const ITEMS: readonly SortableItem[] = [
  { key: 'front', label: 'Front door' },
  { key: 'gate', label: 'Back gate' },
  { key: 'garage', label: 'Garage' },
  { key: 'porch', label: 'Porch light' }
];

/* 48px rows with an 8px gap, starting at y=0. */
const BOXES = [
  { top: 0, bottom: 48 },
  { top: 56, bottom: 104 },
  { top: 112, bottom: 160 }
];

describe('moveItem', () => {
  it('moves an item down, returning a new array and leaving the input untouched', () => {
    const next = moveItem(ITEMS, 0, 2);

    expect(next.map((item) => item.key)).toEqual(['gate', 'garage', 'front', 'porch']);
    expect(ITEMS.map((item) => item.key)).toEqual(['front', 'gate', 'garage', 'porch']);
    expect(next).not.toBe(ITEMS);
  });

  it('moves an item up', () => {
    expect(moveItem(ITEMS, 3, 1).map((item) => item.key)).toEqual(['front', 'porch', 'gate', 'garage']);
  });

  it('returns a copy unchanged for a no-op', () => {
    const next = moveItem(ITEMS, 2, 2);

    expect(next.map((item) => item.key)).toEqual(ITEMS.map((item) => item.key));
    expect(next).not.toBe(ITEMS);
  });

  it('returns a copy unchanged for an out-of-range index', () => {
    for (const [from, to] of [
      [-1, 2],
      [2, -1],
      [4, 0],
      [0, 4]
    ]) {
      const next = moveItem(ITEMS, from, to);
      expect(next.map((item) => item.key)).toEqual(ITEMS.map((item) => item.key));
      expect(next).not.toBe(ITEMS);
    }
  });

  it('handles the empty list', () => {
    expect(moveItem<number>([], 0, 0)).toEqual([]);
  });
});

describe('reorderKeys', () => {
  it('reports the keys in their new order', () => {
    expect(reorderKeys(ITEMS, 0, 3)).toEqual(['gate', 'garage', 'porch', 'front']);
    expect(reorderKeys(ITEMS, 3, 0)).toEqual(['porch', 'front', 'gate', 'garage']);
  });

  it('reports the original order for a no-op', () => {
    expect(reorderKeys(ITEMS, 1, 1)).toEqual(['front', 'gate', 'garage', 'porch']);
  });

  it('keeps numeric keys numeric', () => {
    const numeric: SortableItem[] = [
      { key: 10, label: 'ten' },
      { key: 20, label: 'twenty' }
    ];
    expect(reorderKeys(numeric, 0, 1)).toEqual([20, 10]);
  });
});

describe('targetIndexFor', () => {
  it('answers the last row whose midpoint the pointer has passed', () => {
    expect(targetIndexFor(0, BOXES)).toBe(0);
    expect(targetIndexFor(23, BOXES)).toBe(0);
    // Reaching a midpoint counts as passing it; the gap between rows belongs to
    // the row above, so a small move does not flip the order back and forth.
    expect(targetIndexFor(24, BOXES)).toBe(0);
    expect(targetIndexFor(25, BOXES)).toBe(0);
    expect(targetIndexFor(79, BOXES)).toBe(0);
    expect(targetIndexFor(80, BOXES)).toBe(1);
    expect(targetIndexFor(100, BOXES)).toBe(1);
    expect(targetIndexFor(135, BOXES)).toBe(1);
    expect(targetIndexFor(136, BOXES)).toBe(2);
  });

  it('clamps above and below the list', () => {
    expect(targetIndexFor(-400, BOXES)).toBe(0);
    expect(targetIndexFor(4000, BOXES)).toBe(BOXES.length - 1);
  });

  it('answers 0 when nothing has been measured', () => {
    expect(targetIndexFor(42, [])).toBe(0);
  });

  it('answers one row for a single box', () => {
    expect(targetIndexFor(-1, [BOXES[0]])).toBe(0);
    expect(targetIndexFor(1000, [BOXES[0]])).toBe(0);
  });
});
