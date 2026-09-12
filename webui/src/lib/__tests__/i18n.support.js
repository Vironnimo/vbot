import { expect } from 'vitest';
import { englishCatalog, t } from '../i18n.js';

function expectCatalogKeys(requiredKeys) {
  for (const key of requiredKeys) {
    expect(englishCatalog[key], key).toBeTruthy();
    expect(t(key), key).toBe(englishCatalog[key]);
  }
}

export { expectCatalogKeys };
