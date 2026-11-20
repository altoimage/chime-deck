import { describe, expect, it } from 'vitest';

import { stripParam } from './url.js';

describe('stripParam', () => {
  it('removes the named param and keeps the rest', () => {
    expect(stripParam('?setup=done&kiosk=1', 'setup')).toBe('?kiosk=1');
    expect(stripParam('?kiosk=1&setup=done', 'setup')).toBe('?kiosk=1');
  });

  it('returns an empty string when the named param was the only one', () => {
    expect(stripParam('?setup=done', 'setup')).toBe('');
    expect(stripParam('?setup=done&setup=done', 'setup')).toBe('');
  });

  it('returns an empty string for an empty or bare query', () => {
    expect(stripParam('', 'kiosk')).toBe('');
    expect(stripParam('?', 'kiosk')).toBe('');
  });

  it('removes every value of a repeated param', () => {
    expect(stripParam('?x=1&x=2&kiosk=1', 'x')).toBe('?kiosk=1');
    expect(stripParam('?kiosk=1&kiosk=0', 'kiosk')).toBe('');
  });

  it('leaves an unrelated param untouched', () => {
    expect(stripParam('?a=1&b=2', 'missing')).toBe('?a=1&b=2');
  });

  it('never throws on garbage', () => {
    expect(() => stripParam('??&&==', 'kiosk')).not.toThrow();
    expect(() => stripParam('?kiosk=%', 'setup')).not.toThrow();
    expect(() => stripParam('not-a-query', 'setup')).not.toThrow();
  });
});
