import { describe, expect, it } from 'vitest';

import {
  SURFACE_DISPLAY,
  SURFACE_FORM,
  shouldApplyReloadNow,
} from '../resourceInvalidation.js';

// Which resource kind bumps which refresh token is covered through its owner,
// appController (`resource_changed` routing).
describe('shouldApplyReloadNow()', () => {
  it.each([
    ['a display', SURFACE_DISPLAY, undefined, true],
    [
      'a display with an open dropdown',
      SURFACE_DISPLAY,
      { dropdownOpen: true },
      true,
    ],
    ['an idle form', SURFACE_FORM, undefined, true],
    ['a form without busy signals', SURFACE_FORM, {}, true],
    [
      'a form with an open dropdown',
      SURFACE_FORM,
      { dropdownOpen: true },
      false,
    ],
    ['a form with a focused field', SURFACE_FORM, { focused: true }, false],
    ['a form with a pending save', SURFACE_FORM, { savePending: true }, false],
  ])('for %s returns %s', (_label, surface, signals, applyNow) => {
    expect(shouldApplyReloadNow(surface, signals)).toBe(applyNow);
  });
});
