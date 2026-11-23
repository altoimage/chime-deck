/**
 * The offered UI scales, in percent of the base size (HSD-nlbf).
 *
 * The scale is one multiplier for the whole interface, applied as a single root
 * `zoom` on <html> (see the `html[data-ui-scale]` block in `tokens.css`). The
 * steps are the registry's `ui.scale` integers themselves — the wire value 130
 * is the step 130, not 1.3 — so a panel can PATCH the step directly.
 *
 * Nothing here renders and nothing here keeps state, so a test pins the
 * semantics without a DOM; the live state is `scale.svelte.ts`.
 */

/** The offered steps, as percent of the base size (HSD-nlbf). */
export const SCALE_STEPS = [85, 100, 115, 130, 150] as const;

export type ScaleStep = (typeof SCALE_STEPS)[number];

/** The registry's `ui.scale` default: the base size, no zoom. */
export const DEFAULT_SCALE: ScaleStep = 100;

/** Whether *value* is one of the offered steps, e.g. a value off the wire. */
export function isScaleStep(value: unknown): value is ScaleStep {
  return typeof value === 'number' && (SCALE_STEPS as readonly number[]).includes(value);
}

/** *value* if it is one of the steps, else DEFAULT_SCALE — for a wire value or the cache. */
export function readScale(value: unknown): ScaleStep {
  return isScaleStep(value) ? value : DEFAULT_SCALE;
}

/** The human label for a step, e.g. 'Normal — 100%'. */
export function scaleLabel(step: ScaleStep): string {
  switch (step) {
    case 85:
      return 'Smaller — 85%';
    case 115:
      return 'Larger — 115%';
    case 130:
      return 'Large — 130%';
    case 150:
      return 'Largest — 150%';
    default:
      return 'Normal — 100%';
  }
}
