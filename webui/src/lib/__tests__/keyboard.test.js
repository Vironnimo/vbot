import { describe, expect, it } from 'vitest';

import { isImeComposing } from '../keyboard.js';

describe('isImeComposing', () => {
  it('recognizes IME composition keydowns, including the legacy keyCode 229 confirming Enter', () => {
    expect(isImeComposing({ key: 'Enter', isComposing: true })).toBe(true);
    expect(
      isImeComposing({ key: 'Enter', isComposing: false, keyCode: 229 }),
    ).toBe(true);

    // An ordinary Enter is a normal keystroke.
    expect(
      isImeComposing({ key: 'Enter', isComposing: false, keyCode: 13 }),
    ).toBe(false);
    expect(isImeComposing(null)).toBe(false);
  });
});
