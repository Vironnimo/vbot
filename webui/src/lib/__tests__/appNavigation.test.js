import { describe, expect, it } from 'vitest';

import { englishCatalog } from '../i18n.js';
import { NAVIGATION_ITEMS } from '../../App.svelte';

describe('app navigation surface', () => {
  it('ships only the live navigation views', () => {
    expect(NAVIGATION_ITEMS.map((item) => item.id)).toEqual([
      'chat',
      'agents',
      'cron',
      'system-prompt',
      'settings',
      'logs',
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

  it('keeps existing live navigation labels translated', () => {
    for (const item of NAVIGATION_ITEMS) {
      expect(englishCatalog[item.labelKey], item.labelKey).toBeTruthy();
    }
  });

  it('uses the live Logs navigation label and avoids placeholder metadata', () => {
    expect(NAVIGATION_ITEMS).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: 'logs',
          labelKey: 'navigation.logs',
          labelFallback: 'Logs',
        }),
      ]),
    );
    for (const item of NAVIGATION_ITEMS) {
      expect(item).not.toHaveProperty('descriptionKey');
      expect(item).not.toHaveProperty('descriptionFallback');
    }
  });
});
