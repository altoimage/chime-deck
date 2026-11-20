/**
 * Module-level id counter for label/control wiring (`for`/`id`,
 * `aria-describedby`, `aria-labelledby`). Components call this once per
 * instance at init. Deliberately not `$props.id()` — the kit pins its own
 * scheme so ids stay stable and readable in DOM snapshots.
 */

let nextId = 0;

export function nextComponentId(prefix: string): string {
  nextId += 1;
  return `${prefix}-${nextId}`;
}
