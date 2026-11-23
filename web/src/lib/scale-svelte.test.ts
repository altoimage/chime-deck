// @vitest-environment happy-dom
/**
 * The UI scale state (HSD-nlbf): the step, the persisted cache, and the one
 * DOM effect — <html data-ui-scale>. The attribute is what `tokens.css` turns
 * into the root `zoom`, so a test pins that it is present for a non-default
 * step and absent at 100%. Needs a DOM and a localStorage, hence `happy-dom`
 * per file like `settings.test.ts`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { scale } from './scale.svelte.js';

beforeEach(() => {
  scale.set(100);
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('the default', () => {
  it('starts at the base size with no attribute on <html>', () => {
    expect(scale.step).toBe(100);
    expect(document.documentElement.hasAttribute('data-ui-scale')).toBe(false);
  });
});

describe('set', () => {
  it('stamps the step on <html> and into the pre-paint cache', () => {
    scale.set(130);

    expect(scale.step).toBe(130);
    expect(document.documentElement.dataset.uiScale).toBe('130');
    expect(localStorage.getItem('doorbell.scale')).toBe('130');
  });

  it('removes the attribute again at 100%, so the default path is untouched', () => {
    scale.set(130);
    scale.set(100);

    expect(document.documentElement.hasAttribute('data-ui-scale')).toBe(false);
    expect(localStorage.getItem('doorbell.scale')).toBe('100');
  });

  it('still applies the step when storage throws', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded');
    });

    expect(() => scale.set(85)).not.toThrow();
    expect(scale.step).toBe(85);
    expect(document.documentElement.dataset.uiScale).toBe('85');
  });
});

describe('adopt', () => {
  it('takes the registry value, off the wire, as the local step', () => {
    scale.adopt(150);

    expect(scale.step).toBe(150);
    expect(document.documentElement.dataset.uiScale).toBe('150');
  });

  it('degrades a value that is not an offered step to the default', () => {
    scale.adopt(130);
    scale.adopt(87);

    expect(scale.step).toBe(100);
    expect(document.documentElement.hasAttribute('data-ui-scale')).toBe(false);
  });

  it('degrades a value of the wrong type to the default too', () => {
    scale.adopt(130);
    scale.adopt('130');

    expect(scale.step).toBe(100);
    expect(document.documentElement.hasAttribute('data-ui-scale')).toBe(false);
  });
});
