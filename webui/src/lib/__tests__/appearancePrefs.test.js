import { beforeEach, describe, expect, it } from 'vitest';

import { appearancePrefs, setChatWidth } from '../appearancePrefs.svelte.js';

describe('appearancePrefs', () => {
  beforeEach(() => {
    // The store is a module singleton; reset between tests.
    setChatWidth('comfortable');
  });

  it('defaults chatWidth to comfortable', () => {
    expect(appearancePrefs.chatWidth).toBe('comfortable');
  });

  it('stores each supported chat width', () => {
    setChatWidth('wide');
    expect(appearancePrefs.chatWidth).toBe('wide');

    setChatWidth('full');
    expect(appearancePrefs.chatWidth).toBe('full');
  });

  it('coerces missing or unsupported values to the comfortable default', () => {
    setChatWidth('wide');
    setChatWidth('huge');
    expect(appearancePrefs.chatWidth).toBe('comfortable');

    setChatWidth('wide');
    setChatWidth(undefined);
    expect(appearancePrefs.chatWidth).toBe('comfortable');
  });
});
