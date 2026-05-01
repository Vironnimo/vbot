import { describe, it, expect } from 'vitest';
import { t } from '../../lib/i18n.js';

describe('App', () => {
  it('has i18n available', () => {
    expect(t('test', 'hello')).toBe('hello');
  });
});
