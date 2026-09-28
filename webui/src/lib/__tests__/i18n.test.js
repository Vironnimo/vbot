import { describe, expect, it } from 'vitest';
import { englishCatalog, init, t, tOr } from '../i18n.js';
import { REQUIRED_CATALOG_KEYS, RETIRED_CATALOG_KEYS } from './i18n.support.js';

describe('i18n t()', () => {
  it('prefers the catalog text, then a non-empty fallback, then the key', () => {
    expect(t('navigation.chat', 'Chat fallback')).toBe('Chat');
    expect(t('test', 'hello')).toBe('hello');
    expect(t('key')).toBe('key');
    expect(t('key', '')).toBe('key');
    expect(t('key', null)).toBe('key');
  });

  it('uses the English catalog after initializing an unsupported locale', () => {
    expect(init('zz')).toBe('en');
    expect(t('app.title')).toBe('vBot');
  });

  it('interpolates provided values and leaves missing tokens intact', () => {
    expect(t('queue.count', undefined, { count: 2 })).toBe('2 queued');
    expect(t('missing.key', 'Hi {name}, {count} left', { name: 'Ada' })).toBe(
      'Hi Ada, {count} left',
    );
    expect(t('agents.detail.idValue')).toBe('id: {id}');
  });
});

describe('i18n tOr()', () => {
  it('renders a known code from the catalog and an unknown one as the fallback', () => {
    expect(tOr('logs.level.warn', 'WARN')).toBe(
      englishCatalog['logs.level.warn'],
    );
    expect(tOr('logs.level.trace', 'TRACE')).toBe('TRACE');
    expect(
      tOr('statistics.skills.origin.x', 'x: {detail}', { detail: 'y' }),
    ).toBe('x: y');
  });
});

describe('English catalog', () => {
  it('contains non-empty copy for every required key', () => {
    for (const key of REQUIRED_CATALOG_KEYS) {
      expect(englishCatalog[key], key).toBeTruthy();
    }
  });

  it('keeps retired copy out of the live catalog', () => {
    for (const key of RETIRED_CATALOG_KEYS) {
      expect(englishCatalog[key], key).toBeUndefined();
    }
  });
});
