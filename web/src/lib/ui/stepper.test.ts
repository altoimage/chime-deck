// @vitest-environment happy-dom
/**
 * DOM tests for the Stepper's opt-in `labelHidden` prop (cd-slte).
 *
 * Settings → Audio printed each volume label twice: a `Slider` draws a
 * `<label class="field-label">` and a `Stepper` draws its own
 * `<span class="field-label">` above the −/+ group. Removing the Stepper's copy
 * is not an option — that span is the `aria-labelledby` target of the
 * `role="group"` row, so deleting it would leave the group unnamed. The shared
 * `.sr-only` rule (`web/src/styles/global.css`) is a 1px clip
 * (`position: absolute; width: 1px; height: 1px; clip: rect(0, 0, 0, 0)`), not
 * `display: none`, so a clipped span stays in the accessibility tree and keeps
 * naming the group while painting nothing. That is why the prop reuses the
 * class instead of removing the node.
 *
 * These tests assert the wiring (`id` ↔ `aria-labelledby`), not a computed
 * accessible name: happy-dom does not implement the accname algorithm, so the
 * honest offline assertion is that the group still points at the element that
 * carries the label text.
 */

import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import Stepper from './Stepper.svelte';

type StepperProps = ComponentProps<typeof Stepper>;

/**
 * The prop under test is not in `Stepper`'s Props until the change lands, so the
 * helper accepts it explicitly and the mount call casts back to the component's
 * own props — this file must fail on the assertion below, not on a type error.
 */
type StepperTestProps = Partial<StepperProps> & { labelHidden?: boolean };

const LABEL = 'Software volume';
const VALUE = 40;

const mounted: Array<Parameters<typeof unmount>[0]> = [];

/** Mount one Stepper and return its host element. */
function renderStepper(props: StepperTestProps): HTMLElement {
  const target = document.createElement('div');
  document.body.appendChild(target);
  mounted.push(mount(Stepper, { target, props: props as StepperProps }));
  flushSync();
  return target;
}

/** The label span the group is named by — the node this prop exists for. */
function labelSpan(target: HTMLElement): HTMLElement {
  const span = target.querySelector<HTMLElement>('.field-label');
  if (span === null) {
    throw new Error('expected a .field-label span');
  }
  return span;
}

/** The `role="group"` row that `aria-labelledby` ties to the label span. */
function namedGroup(target: HTMLElement): HTMLElement {
  const group = target.querySelector<HTMLElement>('[role="group"]');
  if (group === null) {
    throw new Error('expected a role="group" row');
  }
  return group;
}

function stepperButton(target: HTMLElement, label: string): HTMLButtonElement {
  const button = target.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  if (button === null) {
    throw new Error(`expected a button labelled "${label}"`);
  }
  return button;
}

afterEach(() => {
  for (const component of mounted.splice(0)) {
    unmount(component);
  }
  document.body.innerHTML = '';
});

describe('Stepper label', () => {
  it('draws the label above the named group by default', () => {
    const target = renderStepper({ label: LABEL, value: VALUE, unit: '%' });

    const label = labelSpan(target);
    expect(label.classList.contains('field-label')).toBe(true);
    expect(label.textContent).toBe(LABEL);
    expect(label.classList.contains('sr-only')).toBe(false);

    const group = namedGroup(target);
    expect(group.getAttribute('role')).toBe('group');
    expect(group.getAttribute('aria-labelledby')).toBe(label.id);
  });

  it('clips the label without removing it or the group’s name when labelHidden is true', () => {
    const target = renderStepper({ label: LABEL, value: VALUE, unit: '%', labelHidden: true });

    const label = labelSpan(target);
    expect(label.textContent).toBe(LABEL);
    expect(label.classList.contains('field-label')).toBe(true);
    expect(label.classList.contains('sr-only')).toBe(true);

    // The name is unchanged: the group still points at the same element, and
    // that element still carries the label text.
    const group = namedGroup(target);
    expect(group.getAttribute('aria-labelledby')).toBe(label.id);
    expect(label.id).not.toBe('');

    // The controls keep their own names; they never depended on the span.
    expect(stepperButton(target, `Increase ${LABEL}`).getAttribute('aria-label')).toBe(
      `Increase ${LABEL}`
    );
    expect(stepperButton(target, `Decrease ${LABEL}`).getAttribute('aria-label')).toBe(
      `Decrease ${LABEL}`
    );

    // The live value is untouched by the label treatment.
    const value = target.querySelector<HTMLElement>('span[aria-live="polite"]');
    expect(value).not.toBeNull();
    expect(value?.classList.contains('sr-only')).toBe(false);
    expect(value?.textContent).toBe(`${VALUE}%`);
  });

  it('is opt-in: an explicit labelHidden of false behaves exactly like the default', () => {
    const target = renderStepper({ label: LABEL, value: VALUE, unit: '%', labelHidden: false });

    const label = labelSpan(target);
    expect(label.classList.contains('field-label')).toBe(true);
    expect(label.classList.contains('sr-only')).toBe(false);
    expect(label.textContent).toBe(LABEL);

    const group = namedGroup(target);
    expect(group.getAttribute('aria-labelledby')).toBe(label.id);
  });
});
