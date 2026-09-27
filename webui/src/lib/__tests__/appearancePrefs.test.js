import { beforeEach, describe, expect, it } from 'vitest';

import {
  appearancePrefs,
  setChatWidth,
  setChatWorkingMode,
} from '../appearancePrefs.svelte.js';

describe('appearancePrefs', () => {
  beforeEach(() => {
    // The store is a module singleton; reset between tests.
    setChatWidth('comfortable');
    setChatWorkingMode('normal');
  });

  it('defaults chatWidth to comfortable', () => {
    expect(appearancePrefs.chatWidth).toBe('comfortable');
  });

  it.each([
    ['wide', 'wide'],
    ['full', 'full'],
    ['huge', 'comfortable'],
    [undefined, 'comfortable'],
  ])('stores the chat width %j as %j', (value, stored) => {
    setChatWidth('wide');
    setChatWidth(value);

    expect(appearancePrefs.chatWidth).toBe(stored);
  });

  it.each([
    ['compact', 'compact'],
    ['dense', 'normal'],
  ])('stores the chat working mode %j as %j', (value, stored) => {
    setChatWorkingMode('compact');
    setChatWorkingMode(value);

    expect(appearancePrefs.chatWorkingMode).toBe(stored);
  });
});
