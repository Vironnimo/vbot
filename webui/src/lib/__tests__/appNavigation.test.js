import { describe, expect, it } from 'vitest';

import { englishCatalog } from '../i18n.js';
import { NAVIGATION_ITEMS } from '../../App.svelte';

describe('app navigation surface', () => {
  it('ships only the live navigation views', () => {
    expect(NAVIGATION_ITEMS.map((item) => item.id)).toEqual([
      'chat',
      'agents',
      'system-prompt',
      'settings',
    ]);
  });

  it('does not expose a Components navigation entry', () => {
    expect(NAVIGATION_ITEMS).not.toEqual(
      expect.arrayContaining([expect.objectContaining({ id: 'components' })]),
    );
    expect(NAVIGATION_ITEMS).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ labelKey: 'navigation.components' }),
      ]),
    );
  });

  it('maps each shipped navigation entry to translated live labels', () => {
    for (const item of NAVIGATION_ITEMS) {
      expect(englishCatalog[item.labelKey], item.labelKey).toBeTruthy();
      expect(
        englishCatalog[item.descriptionKey],
        item.descriptionKey,
      ).toBeTruthy();
    }
  });
});
