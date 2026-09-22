import { describe, expect, it } from 'vitest';

import { isImeComposing } from '../keyboard.js';

describe('isImeComposing', () => {
  it('recognizes a keydown marked as part of an IME composition', () => {
    expect(isImeComposing({ key: 'Enter', isComposing: true })).toBe(true);
  });

  it('recognizes the legacy keyCode 229 used for a confirming Enter', () => {
    expect(
      isImeComposing({ key: 'Enter', isComposing: false, keyCode: 229 }),
    ).toBe(true);
  });

  it('treats an ordinary Enter as a normal keystroke', () => {
    expect(
      isImeComposing({ key: 'Enter', isComposing: false, keyCode: 13 }),
    ).toBe(false);
    expect(isImeComposing(null)).toBe(false);
  });
});
