import { describe, it, expect } from 'vitest';
import { t } from '../i18n.js';

describe('i18n t()', () => {
  it('returns fallback when provided', () => {
    expect(t('test', 'hello')).toBe('hello');
  });

  it('returns key when no fallback is provided', () => {
    expect(t('key')).toBe('key');
  });

  it('returns key when fallback is empty string', () => {
    expect(t('key', '')).toBe('key');
  });

  it('returns key when fallback is null', () => {
    expect(t('key', null)).toBe('key');
  });
});
