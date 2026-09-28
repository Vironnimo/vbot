import { describe, expect, it } from 'vitest';
import { englishCatalog, init, registerCatalog, t, tOr } from '../i18n.js';

describe('i18n t()', () => {
  it('renders the catalog text, or the key when it has no entry', () => {
    expect(t('navigation.chat')).toBe('Chat');
    expect(t('missing.key')).toBe('missing.key');
  });

  it('uses the English catalog after initializing an unsupported locale', () => {
    expect(init('zz')).toBe('en');
    expect(t('app.title')).toBe('vBot');
  });

  it('interpolates provided values and leaves missing tokens intact', () => {
    expect(t('statistics.limits.resetsIn', { duration: '2h' })).toBe(
      'Resets in 2h',
    );
    expect(t('chat.durationMinutesSeconds', { minutes: 2 })).toBe(
      '2m {seconds}s',
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

describe('i18n registerCatalog()', () => {
  it('adds Extension text and rejects keys that are already defined', () => {
    registerCatalog({ 'test.page.greeting': 'Hello {name}' });

    expect(t('test.page.greeting', { name: 'Ada' })).toBe('Hello Ada');
    expect(() => registerCatalog({ 'navigation.chat': 'Talk' })).toThrow(
      'Duplicate i18n keys: navigation.chat',
    );
    expect(() =>
      registerCatalog({
        'test.page.other': 'Other',
        'test.page.greeting': 'Hi',
      }),
    ).toThrow('Duplicate i18n keys: test.page.greeting');
    expect(t('navigation.chat')).toBe('Chat');
    expect(t('test.page.other')).toBe('test.page.other');
    expect(englishCatalog['test.page.greeting']).toBeUndefined();
  });
});
