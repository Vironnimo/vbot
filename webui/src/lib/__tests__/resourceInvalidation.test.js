import { describe, expect, it } from 'vitest';

import { shouldApplyReloadNow } from '../resourceInvalidation.js';

// Which resource kind bumps which refresh token is covered through its owner,
// appController (`resource_changed` routing).
describe('shouldApplyReloadNow()', () => {
  it.each([
    ['an idle form', undefined, true],
    ['a form without busy signals', {}, true],
    ['a form with an open dropdown', { dropdownOpen: true }, false],
    ['a form with a focused field', { focused: true }, false],
    ['a form with a pending save', { savePending: true }, false],
  ])('for %s returns %s', (_label, signals, applyNow) => {
    expect(shouldApplyReloadNow(signals)).toBe(applyNow);
  });
});
