import { describe, expect, it } from 'vitest';

import { englishCatalog, init, t } from '../i18n.js';

describe('i18n t()', () => {
  it('returns catalog text for known English keys', () => {
    expect(t('navigation.chat', 'Chat fallback')).toBe('Chat');
  });

  it('returns fallback for unknown keys when provided', () => {
    expect(t('test', 'hello')).toBe('hello');
  });

  it('returns key for unknown keys when no fallback is provided', () => {
    expect(t('key')).toBe('key');
  });

  it('returns key for unknown keys when fallback is empty string', () => {
    expect(t('key', '')).toBe('key');
  });

  it('returns key for unknown keys when fallback is null', () => {
    expect(t('key', null)).toBe('key');
  });

  it('uses English catalog after initializing an unsupported locale', () => {
    expect(init('zz')).toBe('en');
    expect(t('app.title')).toBe('vBot');
  });

  it('interpolates provided values in catalog text', () => {
    expect(t('queue.count', undefined, { count: 2 })).toBe('2 queued');
  });

  it('leaves missing interpolation tokens intact', () => {
    expect(t('agents.deleteConfirmTitle')).toBe('Delete {name}?');
  });

  it('contains Phase 4 labels for required WebUI areas', () => {
    const requiredKeys = [
      'app.title',
      'app.serverStatus',
      'app.serverReady',
      'navigation.chat',
      'navigation.agents',
      'navigation.systemPrompt',
      'navigation.settings',
      'chat.session',
      'chat.cancelRun',
      'chat.event.args',
      'chat.event.result',
      'chat.event.running',
      'chat.event.done',
      'queue.title',
      'cancel.confirm',
      'agents.create',
      'systemPrompt.comingSoon',
      'settings.comingSoon',
      'errors.network',
      'loading.history',
    ];

    for (const key of requiredKeys) {
      expect(englishCatalog[key], key).toBeTruthy();
      expect(t(key), key).toBe(englishCatalog[key]);
    }
  });
});
