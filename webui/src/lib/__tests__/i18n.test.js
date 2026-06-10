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

  it('contains Phase 5 per-row cancel control labels', () => {
    const requiredKeys = [
      'chat.cancelToolCall',
      'chat.cancelToolCallAria',
      'chat.cancelSubAgent',
      'chat.cancelSubAgentAria',
    ];

    for (const key of requiredKeys) {
      expect(englishCatalog[key], key).toBeTruthy();
      expect(t(key), key).toBe(englishCatalog[key]);
    }

    expect(t('chat.cancelToolCallAria').toLowerCase()).toContain('tool');
    expect(t('chat.cancelSubAgentAria').toLowerCase()).toContain('sub');
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
      'chat.tokenBadgeEstimated',
      'chat.tokenBadgeNoContext',
      'chat.tokenBadgeEstimatedNoContext',
      'chat.tokenBadgeNoUsage',
      'chat.skillsLoadError',
      'skillAutocomplete.label',
      'skillAutocomplete.eyebrow.commandsAndSkills',
      'skillAutocomplete.eyebrow.skills',
      'skillAutocomplete.noDescription',
      'chat.runIterations',
      'chat.runDurationSeconds',
      'chat.toolArgs',
      'chat.toolResultLabel',
      'chat.toolSucceeded',
      'chat.toolFailed',
      'chat.toolCancelled',
      'chat.subagent.label',
      'chat.subagent.starting',
      'chat.subagent.loadingResult',
      'chat.subagent.viewSession',
      'chat.subagentSessionNotice',
      'chat.subagentSessionHint',
      'chat.returnToCurrentSession',
      'sessions.subagent_parent',
      'status.connected',
      'status.activeRun',
      'status.notReachable',
      'status.reconnecting',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('app.statusPlaceholder')).toContain('placeholder');
    expect(t('app.serverStatus')).not.toMatch(/server:\d+/u);
    expect(t('chat.runIterations', undefined, { count: 2 })).toBe('2 iter');
    expect(t('chat.runDurationSeconds', undefined, { seconds: '1.5' })).toBe(
      '1.5s',
    );
    expect(
      t('chat.tokenBadge', undefined, { tokens: 1200, context: 8000 }),
    ).toBe('1200 / 8000 tok');
    expect(
      t('chat.tokenBadgeEstimated', undefined, { tokens: 1200, context: 8000 }),
    ).toBe('~1200 / 8000 tok');
    expect(t('chat.tokenBadgeNoContext', undefined, { tokens: 1200 })).toBe(
      '1200 tok',
    );
    expect(
      t('chat.tokenBadgeEstimatedNoContext', undefined, { tokens: 1200 }),
    ).toBe('~1200 tok');
    expect(t('chat.tokenBadgeNoUsage', undefined, { context: 8000 })).toBe(
      '— / 8000 tok',
    );
    expect(t('chat.subagentSessionHint')).toContain('continue this sub-agent');
    expect(t('chat.returnToCurrentSession')).toBe('Return to current session');
    expect(englishCatalog['navigation.components']).toBeUndefined();
  });

  it('contains Toasted design labels for Agents placeholders', () => {
    const requiredKeys = [
      'agents.detail.identity',
      'agents.detail.model',
      'agents.detail.systemPrompt',
      'agents.detail.access',
      'agents.detail.session',
      'agents.detail.idValue',
      'agents.form.modelPlaceholder',
      'agents.form.modelUnavailableOption',
      'agents.form.customSystemPrompt',
      'agents.access.noSkills',
      'agents.access.toggleTool',
      'agents.access.toggleSkill',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('agents.detail.idValue', undefined, { id: 'alpha' })).toBe(
      'id: alpha',
    );
    expect(
      t('agents.form.modelUnavailableOption', undefined, {
        model: 'custom/provider-model',
      }),
    ).toBe('Unavailable / custom: custom/provider-model');
  });

  it('contains System Prompt scope labels and states', () => {
    const requiredKeys = [
      'systemPrompt.scope.label',
      'systemPrompt.scope.default',
      'systemPrompt.fragmentEditor.save',
      'systemPrompt.fragmentEditor.reset',
      'systemPrompt.fragmentEditor.dirtyIndicator',
      'systemPrompt.fragmentEditor.modifiedIndicator',
      'systemPrompt.fragmentEditor.resetConfirm',
      'systemPrompt.fragmentEditor.resetAgentConfirm',
      'systemPrompt.preview.heading',
      'systemPrompt.preview.refresh',
      'systemPrompt.preview.copy',
      'systemPrompt.preview.tokenCount',
      'systemPrompt.preview.agentLabel',
      'systemPrompt.preview.empty',
      'systemPrompt.error.loadFailed',
      'systemPrompt.error.saveFailed',
      'systemPrompt.error.resetFailed',
      'systemPrompt.error.previewFailed',
      'systemPrompt.error.copyFailed',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('systemPrompt.scope.default')).toBe('Default');
    expect(t('systemPrompt.preview.tokenCount', undefined, { count: 42 })).toBe(
      '~42 tokens',
    );
  });

  it('contains Toasted design labels for Settings sections', () => {
    const requiredKeys = [
      'settings.title',
      'settings.sections',
      'settings.placeholder',
      'settings.loading',
      'settings.loadError',
      'settings.saveError',
      'settings.general.title',
      'settings.general.subtitle',
      'settings.general.serverHost',
      'settings.general.serverHostDescription',
      'settings.general.serverHostPlaceholder',
      'settings.general.dataDirectory',
      'settings.general.dataDirectoryDescription',
      'settings.general.dataDirectoryPlaceholder',
      'settings.recall.title',
      'settings.recall.subtitle',
      'settings.recall.backend',
      'settings.recall.backendDescription',
      'settings.recall.backends.jsonl_scan',
      'settings.recall.backends.sqlite_fts',
      'settings.recall.backends.vector',
      'settings.recall.save',
      'settings.recall.saveSuccess',
      'settings.specializedModels.embeddingModel',
      'settings.specializedModels.embeddingModelDescription',
      'settings.providers.title',
      'settings.providers.subtitle',
      'settings.providers.empty',
      'settings.providers.description.credentialKey',
      'settings.providers.description.baseUrl',
      'settings.providers.description.modelCount',
      'settings.providers.description.none',
      'settings.providers.status.configured',
      'settings.providers.status.missingCredentials',
      'settings.providers.status.placeholder',
      'settings.providers.customEndpoint',
      'settings.providers.customEndpointDescription',
      'settings.providers.customEndpointStatus',
      'settings.providers.configure',
      'settings.appearance.title',
      'settings.appearance.subtitle',
      'settings.appearance.language',
      'settings.appearance.languageDescription',
      'settings.appearance.saveSuccess',
      'settings.language.en',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('settings.recall.backends.vector')).toBe('Semantic (vector)');
    expect(t('settings.specializedModels.embeddingModel')).toBe(
      'Embedding model',
    );
    expect(t('settings.specializedModels.embeddingModelDescription')).toContain(
      'semantic session recall',
    );
    expect(englishCatalog['settings.placeholderNote']).toBeUndefined();
    expect(englishCatalog['settings.general.autoScroll']).toBeUndefined();
    expect(
      englishCatalog['settings.general.autoScrollDescription'],
    ).toBeUndefined();
    expect(
      englishCatalog['settings.appearance.showTokenCounts'],
    ).toBeUndefined();
    expect(
      englishCatalog['settings.appearance.showTokenCountsDescription'],
    ).toBeUndefined();
    expect(englishCatalog['settings.language.de']).toBeUndefined();
    expect(
      t('settings.providers.description.credentialKey', undefined, {
        credentialKey: 'OPENAI_API_KEY',
      }),
    ).toBe('Credential key: OPENAI_API_KEY.');
    expect(
      t('settings.providers.description.baseUrl', undefined, {
        baseUrl: 'https://api.example.com/v1',
      }),
    ).toBe('Endpoint: https://api.example.com/v1.');
    expect(
      t('settings.providers.description.modelCount', undefined, {
        count: 3,
      }),
    ).toBe('3 models available.');
    expect(t('settings.providers.subtitle')).toBe(
      'Credential status and endpoint metadata for available providers.',
    );
    expect(t('settings.providers.status.missingCredentials')).toBe(
      'Missing credentials',
    );
  });

  it('contains Logs tab copy for navigation, filters, and states', () => {
    const requiredKeys = [
      'navigation.logs',
      'logs.title',
      'logs.eyebrow',
      'logs.subtitle',
      'logs.file',
      'logs.emptyOption',
      'logs.levelFilter',
      'logs.sort',
      'logs.sort.newest',
      'logs.sort.oldest',
      'logs.level.all',
      'logs.level.info',
      'logs.level.warn',
      'logs.level.warning',
      'logs.level.error',
      'logs.level.unknown',
      'logs.search',
      'logs.searchPlaceholder',
      'logs.resultsCount',
      'logs.currentFile',
      'logs.entries',
      'logs.loadingCatalog',
      'logs.loadingFile',
      'logs.emptyTitle',
      'logs.emptySubtitle',
      'logs.fileEmptyTitle',
      'logs.fileEmptySubtitle',
      'logs.noMatchesTitle',
      'logs.noMatchesSubtitle',
      'logs.catalogLoadError',
      'logs.readError',
      'logs.streamError',
      'logs.stream.connecting',
      'logs.stream.connected',
      'logs.stream.reconnecting',
      'logs.stream.error',
      'logs.stream.idle',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('navigation.logs')).toBe('Logs');
    expect(t('logs.resultsCount', undefined, { count: 7 })).toBe(
      '7 visible entries',
    );
    expect(t('logs.currentFile', undefined, { file: '2026-05-11.log' })).toBe(
      'Current file: 2026-05-11.log',
    );
    expect(t('logs.level.warn')).toBe('WARN');
    expect(t('logs.level.error')).toBe('ERROR');
    expect(t('logs.sort')).toBe('Order');
    expect(t('logs.sort.newest')).toBe('Newest first');
    expect(t('logs.sort.oldest')).toBe('Oldest first');
    expect(t('logs.searchPlaceholder')).toContain('logger');
    expect(t('logs.stream.connected')).toBe('Live');
    expect(t('logs.stream.error')).toBe('Live update error');
  });

  it('contains Debug i18n copy with a meaningful empty heading and matching interpolation tokens', () => {
    const requiredKeys = [
      'debug.eyebrow',
      'debug.title',
      'debug.subtitle',
      'debug.statusCount',
      'debug.traceLimit',
      'debug.localWarning',
      'debug.emptyHeader',
      'debug.emptyState',
      'debug.clearConfirm',
      'debug.traceList',
      'debug.metadata',
      'debug.request',
      'debug.requestMethod',
      'debug.requestUrl',
      'debug.requestHeaders',
      'debug.requestBody',
      'debug.response',
      'debug.responseStatus',
      'debug.responseHeaders',
      'debug.responseBody',
      'debug.streamRaw',
      'debug.streamParsed',
      'debug.modelProbe',
      'debug.modelProbe.provider',
      'debug.modelProbe.connection',
      'debug.modelProbe.selectProvider',
      'debug.modelProbe.selectConnection',
      'debug.modelProbe.run',
      'debug.modelProbe.rawResponse',
      'debug.modelProbe.normalizedPreview',
      'debug.modelProbe.modelCount',
      'debug.expandRow',
      'debug.collapseRow',
    ];

    expectCatalogKeys(requiredKeys);

    // The removed stream-event copy must not return to the catalog.
    expect(englishCatalog['debug.streamEvents']).toBeUndefined();
    expect(englishCatalog['debug.streamEventIndex']).toBeUndefined();
    expect(englishCatalog['debug.noStreamEvents']).toBeUndefined();
    expect(t('debug.streamEvents')).toBe('debug.streamEvents');
    expect(t('debug.streamEventIndex')).toBe('debug.streamEventIndex');
    expect(t('debug.noStreamEvents')).toBe('debug.noStreamEvents');

    // The empty heading must be meaningful copy, never the bogus "(none)" placeholder.
    expect(t('debug.emptyHeader')).not.toBe('(none)');
    expect(t('debug.emptyHeader').trim().length).toBeGreaterThan(0);
    expect(t('debug.emptyHeader')).toBe('No traces captured yet');

    // The Debug subtitle must describe request/response inspection and must
    // not instruct users to inspect individual stream events.
    expect(t('debug.subtitle')).toMatch(/request/i);
    expect(t('debug.subtitle')).toMatch(/response/i);
    expect(t('debug.subtitle').toLowerCase()).not.toContain('stream event');

    expect(t('debug.statusCount', undefined, { count: 4, limit: 50 })).toBe(
      'Traces: 4 / 50',
    );
    expect(t('debug.modelProbe.modelCount', undefined, { count: 12 })).toBe(
      '12 models',
    );

    expect(t('debug.emptyState')).toContain('debug');
    expect(t('debug.emptyState').length).toBeGreaterThan(20);

    // Trace row expand/collapse aria labels must be non-empty catalog copy so
    // screen readers and tooltips don't fall back to the component hardcoded
    // default strings.
    expect(t('debug.expandRow').trim().length).toBeGreaterThan(0);
    expect(t('debug.collapseRow').trim().length).toBeGreaterThan(0);
    expect(t('debug.expandRow')).toBe('Expand row');
    expect(t('debug.collapseRow')).toBe('Collapse row');
    expect(t('debug.expandRow')).not.toBe(t('debug.collapseRow'));
  });

  it('does not expose Components showcase labels in the live catalog', () => {
    expect(englishCatalog['components.title']).toBeUndefined();
    expect(englishCatalog['components.toast.errorMessage']).toBeUndefined();
    expect(t('components.title')).toBe('components.title');
  });
});
