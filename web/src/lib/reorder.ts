/**
 * Reorder arithmetic for the shared sortable list, with no DOM in sight.
 *
 * The pointer gesture, the ▲/▼ buttons and the eventual `POST
 * /api/entities/reorder` all ask the same question — "which keys in which order
 * after this move?" — and a pure answer is the only way to test that without a
 * browser. `targetIndexFor` is the one piece that reads geometry, and it does so
 * from boxes the caller measured, so a test can hand it any layout.
 *
 * Every function returns a NEW array: a sortable list's `items` prop is owned by
 * the caller, and mutating it in place would reorder the caller's state behind
 * its back.
 */

/** One row of a sortable list: the key that identifies it, and its spoken name. */
export interface SortableItem {
  key: string | number;
  label: string;
}

/**
 * Move the item at `from` to index `to`, returning a NEW array.
 *
 * Out-of-range indices and a no-op (`from === to`) return a copy unchanged —
 * never a partially applied move, because a drag that ends outside the list must
 * leave the order exactly as it was.
 */
export function moveItem<T>(items: readonly T[], from: number, to: number): T[] {
  const copy = [...items];
  if (from === to || from < 0 || to < 0 || from >= items.length || to >= items.length) {
    return copy;
  }
  const [moved] = copy.splice(from, 1);
  copy.splice(to, 0, moved);
  return copy;
}

/** The keys in their new order after a drop — the body `onreorder` reports. */
export function reorderKeys(items: readonly SortableItem[], from: number, to: number): (string | number)[] {
  return moveItem(items, from, to).map((item) => item.key);
}

/**
 * The insertion index for a pointer at `y` given the measured row boxes, in
 * document order.
 *
 * The rule is "the row the pointer has passed": the answer is the last row whose
 * vertical midpoint is at or above `y`, so a pointer in the top half of a row
 * still belongs to that row and the order only changes once the finger crosses a
 * midpoint. The result is clamped to `[0, boxes.length - 1]`, and an empty list
 * of boxes (nothing measured yet) answers `0`.
 */
export function targetIndexFor(y: number, boxes: readonly { top: number; bottom: number }[]): number {
  let index = 0;
  for (let i = 0; i < boxes.length; i += 1) {
    const midpoint = (boxes[i].top + boxes[i].bottom) / 2;
    if (y >= midpoint) {
      index = i;
    }
  }
  return index;
}
