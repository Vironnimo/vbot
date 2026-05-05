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
      'navigation.chat',
      'navigation.agents',
      'navigation.systemPrompt',
      'navigation.settings',
      'chat.cancelRun',
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

  function expectCatalogKeys(requiredKeys) {
    for (const key of requiredKeys) {
      expect(englishCatalog[key], key).toBeTruthy();
      expect(t(key), key).toBe(englishCatalog[key]);
    }
  }

  it('contains Toasted design labels for navigation and status polish', () => {
    const requiredKeys = [
      'app.serverStatus',
      'app.statusPlaceholder',
      'chat.tokenBadge',
      'chat.attachPlaceholder',
      'chat.toolArgs',
      'chat.toolResultLabel',
      'status.connected',
      'status.activeRun',
      'status.notReachable',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('app.statusPlaceholder')).toContain('placeholder');
    expect(t('app.serverStatus')).not.toMatch(/server:\d+/u);
    expect(englishCatalog['navigation.components']).toBeUndefined();
  });

  it('contains Toasted design labels for Agents placeholders', () => {
    const requiredKeys = [
      'agents.detail.identity',
      'agents.detail.model',
      'agents.detail.access',
      'agents.detail.session',
      'agents.detail.idValue',
      'agents.form.modelPlaceholder',
      'agents.form.modelManualHelp',
      'agents.access.noTools',
      'agents.access.noSkills',
      'agents.access.toggleTool',
      'agents.access.toggleSkill',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('agents.detail.idValue', undefined, { id: 'alpha' })).toBe(
      'id: alpha',
    );
  });

  it('contains Toasted design labels for Settings sections', () => {
    const requiredKeys = [
      'settings.sections',
      'settings.placeholder',
      'settings.placeholderNote',
      'settings.general.title',
      'settings.general.serverHost',
      'settings.general.serverHostPlaceholder',
      'settings.general.dataDirectory',
      'settings.general.dataDirectoryPlaceholder',
      'settings.general.autoScroll',
      'settings.providers.title',
      'settings.providers.openRouter',
      'settings.providers.envPath',
      'settings.providers.anthropic',
      'settings.providers.ollama',
      'settings.providers.ollamaDescription',
      'settings.providers.customEndpoint',
      'settings.appearance.title',
      'settings.appearance.language',
      'settings.appearance.showTokenCounts',
      'settings.language.en',
    ];

    expectCatalogKeys(requiredKeys);
    expect(
      t('settings.providers.ollamaDescription', undefined, {
        host: 'localhost:11434',
      }),
    ).toBe('Local model server placeholder: localhost:11434');
  });

  it('does not expose Components showcase labels in the live catalog', () => {
    expect(englishCatalog['components.title']).toBeUndefined();
    expect(englishCatalog['components.toast.errorMessage']).toBeUndefined();
    expect(t('components.title')).toBe('components.title');
  });
});
