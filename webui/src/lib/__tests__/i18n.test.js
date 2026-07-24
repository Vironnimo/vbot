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
    expect(t('queue.restartDiscardedMany', undefined, { count: 3 })).toBe(
      '3 queued messages were discarded because the server restarted.',
    );
  });

  it('leaves missing interpolation tokens intact', () => {
    expect(t('agents.detail.idValue')).toBe('id: {id}');
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
      'agents.create',
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
      'status.notReachable',
      'status.reconnecting',
    ];

    expectCatalogKeys(requiredKeys);
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

  it('contains the grouped sidebar navigation section labels', () => {
    const requiredKeys = [
      'nav.section.work',
      'nav.section.configure',
      'nav.section.insights',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('nav.section.work')).toBe('Work');
    expect(t('nav.section.configure')).toBe('Configure');
    expect(t('nav.section.insights')).toBe('Insights');
  });

  it('contains the agent takeover divider labels', () => {
    expect(englishCatalog['chat.takenOver']).toBeTruthy();
    expect(englishCatalog['chat.takenOverGeneric']).toBeTruthy();
    // The composed label weaves the two raw addresses into the localized phrase.
    expect(
      t('chat.takenOver', undefined, { from: 'assistant', to: 'builder@vbot' }),
    ).toBe('Taken over by assistant → builder@vbot');
    expect(t('chat.takenOverGeneric')).toBe('Session taken over');
  });

  it('contains first-run onboarding labels', () => {
    const requiredKeys = [
      'onboarding.title',
      'onboarding.dismiss',
      'onboarding.finishSetup',
      'onboarding.finishSetupHint',
      'onboarding.step.service.kicker',
      'onboarding.step.service.title',
      'onboarding.step.service.subtitle',
      'onboarding.hero.badge',
      'onboarding.hero.title',
      'onboarding.hero.description',
      'onboarding.hero.action',
      'onboarding.subscription.title',
      'onboarding.subscription.description',
      'onboarding.subscription.action',
      'onboarding.more.toggle',
      'onboarding.more.description',
      'onboarding.more.action',
      'onboarding.step.model.kicker',
      'onboarding.step.model.title',
      'onboarding.step.model.subtitle',
      'onboarding.model.label',
      'onboarding.model.placeholder',
      'onboarding.model.searchPlaceholder',
      'onboarding.model.searchEmpty',
      'onboarding.model.loading',
      'onboarding.model.loadError',
      'onboarding.model.empty',
      'onboarding.model.retry',
      'onboarding.model.start',
      'onboarding.model.assignError',
      'onboarding.model.back',
      'onboarding.provider.tip.openrouter',
      'chat.noProvider.title',
      'chat.noProvider.hint',
      'chat.noProvider.action',
      'chat.noModel.title',
      'chat.noModel.hint',
      'chat.noModel.action',
    ];

    expectCatalogKeys(requiredKeys);
    expect(
      t('onboarding.subscription.action', undefined, { provider: 'ChatGPT' }),
    ).toBe('Sign in with ChatGPT');
    expect(
      t('onboarding.more.action', undefined, { provider: 'Anthropic' }),
    ).toBe('Connect Anthropic');
    expect(t('onboarding.provider.tip.openrouter').toLowerCase()).toContain(
      'free',
    );
  });

  it('contains Toasted design labels for Agents placeholders', () => {
    const requiredKeys = [
      'agents.detail.identity',
      'agents.detail.model',
      'agents.detail.systemPrompt',
      'agents.detail.memory',
      'agents.detail.access',
      'agents.detail.metadata',
      'agents.detail.idValue',
      'agents.form.modelPlaceholder',
      'agents.form.modelUnavailableOption',
      'agents.form.customSystemPrompt',
      'agents.form.customPromptHelp',
      'agents.form.memoryPromptModeHelp',
      'agents.form.memoryModeHelp',
      'agents.form.fallbackModelHelp',
      'agents.form.temperatureHelp',
      'agents.form.thinkingEffortHelp',
      'agents.form.wildcardNote',
      'agents.form.memoryPromptModeOption.off',
      'agents.form.memoryPromptModeOption.agent',
      'agents.form.memoryPromptModeOption.agent_user',
      'agents.access.noSkills',
      'agents.access.toggleTool',
      'agents.access.toggleSkill',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('agents.detail.idValue', undefined, { id: 'alpha' })).toBe(
      'id: alpha',
    );
    expect(t('agents.form.memoryPromptModeOption.agent')).toBe(
      'Agent notes (MEMORY.md)',
    );
    expect(t('agents.form.memoryPromptModeOption.agent_user')).toBe(
      'Agent + user notes (MEMORY.md + USER.md)',
    );
    expect(t('agents.form.customPromptHelp')).toContain('System Prompt tab');
    expect(t('agents.form.memoryModeHelp')).toContain('memory tool follows');
    expect(
      t('agents.form.modelUnavailableOption', undefined, {
        model: 'custom/provider-model',
      }),
    ).toBe('Unavailable / custom: custom/provider-model');
  });

  it('contains the shared inherit-state, memory tool row, and disable-confirm copy', () => {
    const requiredKeys = [
      'inherit.option',
      'inherit.optionNotConfigured',
      'inherit.optionProviderDefault',
      'inherit.hint',
      'inherit.hintProviderDefault',
      'inherit.resetToInherit',
      'inherit.editGlobalDefaults',
      'agents.form.editAgentPrompt',
      'agents.tools.memoryFollowsActive',
      'agents.tools.memoryFollowsOff',
      'agents.tools.notReadyBadge',
      'agents.tools.openExtensions',
      'agents.confirmDisableCustomPrompt.title',
      'agents.confirmDisableCustomPrompt.body',
      'agents.confirmDisableCustomPrompt.confirm',
    ];

    expectCatalogKeys(requiredKeys);
    // The retired thinking-effort default key must not return to the catalog.
    expect(englishCatalog['agents.form.thinkingEffortDefault']).toBeUndefined();
    expect(t('agents.form.thinkingEffortDefault')).toBe(
      'agents.form.thinkingEffortDefault',
    );
    // The inherit keys moved from the agents.form.* namespace to the shared
    // inherit.* namespace — the old spellings must not linger in the catalog.
    for (const retiredKey of [
      'agents.form.inheritOption',
      'agents.form.inheritOptionNotConfigured',
      'agents.form.inheritOptionProviderDefault',
      'agents.form.inheritedHint',
      'agents.form.inheritedHintProviderDefault',
      'agents.form.resetToInherit',
      'agents.form.editGlobalDefaults',
    ]) {
      expect(englishCatalog[retiredKey]).toBeUndefined();
    }
    // The inherit-value keys interpolate the global-default value.
    expect(t('inherit.option', undefined, { value: 'openai/gpt-5.2' })).toBe(
      'Inherited: openai/gpt-5.2 (global default)',
    );
    expect(t('inherit.hint', undefined, { value: '0.7' })).toBe(
      'Inherited: 0.7 (global default)',
    );
    expect(t('inherit.optionNotConfigured')).toBe('Inherit (not configured)');
    expect(t('inherit.optionProviderDefault')).toBe(
      'Inherit (provider default)',
    );
    expect(t('inherit.resetToInherit')).toBe('Reset to inherited value');
    expect(t('inherit.editGlobalDefaults')).toBe('Edit global defaults');
    expect(t('inherit.hintProviderDefault')).toBe(
      'Provider default — nothing is set here or in the global defaults.',
    );
    expect(t('agents.form.editAgentPrompt')).toBe("Edit this agent's prompt");
    expect(t('agents.confirmDisableCustomPrompt.confirm')).toBe(
      'Disable custom prompt',
    );
    expect(t('agents.tools.notReadyBadge')).toBe('Currently unavailable');
  });

  it('contains System Prompt scope labels and states', () => {
    const requiredKeys = [
      'systemPrompt.scope.label',
      'systemPrompt.scope.default',
      'systemPrompt.fragmentEditor.save',
      'systemPrompt.fragmentEditor.reset',
      'systemPrompt.fragmentEditor.dirtyIndicator',
      'systemPrompt.fragmentEditor.modifiedIndicator',
      'systemPrompt.fragmentEditor.modifiedHint',
      'systemPrompt.fragmentEditor.resetConfirm',
      'systemPrompt.fragmentEditor.resetAgentConfirm',
      'systemPrompt.blockList.guide.label',
      'systemPrompt.blockList.guide.title',
      'systemPrompt.blockList.guide.assemblyLabel',
      'systemPrompt.blockList.guide.assembly',
      'systemPrompt.blockList.guide.scopeLabel',
      'systemPrompt.blockList.guide.scope',
      'systemPrompt.blockList.newBlockPrompt',
      'systemPrompt.blockList.invalidSlug',
      'systemPrompt.blockList.dataBadge',
      'systemPrompt.blockList.dataHint',
      'systemPrompt.blockList.ownerHint.always',
      'systemPrompt.blockList.ownerHint.memory',
      'systemPrompt.blockList.ownerHint.channel',
      'systemPrompt.blockList.ownerHint.tool',
      'systemPrompt.blockList.ownerHint.extension',
      'systemPrompt.preview.heading',
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
    expect(t('systemPrompt.blockList.guide.assembly')).toContain(
      'top to bottom',
    );
    expect(t('systemPrompt.blockList.guide.scope')).toContain(
      'Custom system prompt',
    );
    // Reset confirms speak of "block", never the retired "fragment" term.
    expect(t('systemPrompt.fragmentEditor.resetConfirm')).toBe(
      'Reset this block to its default? This cannot be undone.',
    );
    expect(t('systemPrompt.fragmentEditor.resetAgentConfirm')).toBe(
      'Reset this Agent block to the current Default content? This cannot be undone.',
    );
    expect(t('systemPrompt.fragmentEditor.resetConfirm')).not.toContain(
      'fragment',
    );
    expect(t('systemPrompt.fragmentEditor.resetAgentConfirm')).not.toContain(
      'fragment',
    );
    // The new-block copy no longer uses the jargon "slug".
    expect(t('systemPrompt.blockList.newBlockPrompt')).not.toContain('slug');
    expect(t('systemPrompt.blockList.invalidSlug')).toMatch(/^Invalid name —/u);
    expect(t('systemPrompt.fragmentEditor.modifiedHint')).toBe(
      'Edited — differs from the built-in default.',
    );
    expect(t('systemPrompt.blockList.dataHint')).toContain('Generated content');
    // The owner line is now a plain sentence stating the render condition; the
    // tool/extension variants weave the technical name into the sentence.
    expect(t('systemPrompt.blockList.ownerHint.always')).toBe(
      'Always included.',
    );
    expect(
      t('systemPrompt.blockList.ownerHint.tool', undefined, { name: 'bash' }),
    ).toBe('Included only while the bash tool is active.');
    expect(
      t('systemPrompt.blockList.ownerHint.extension', undefined, {
        name: 'homeassistant',
      }),
    ).toBe('Included only while the homeassistant extension is active.');
    expect(t('systemPrompt.blockList.ownerHint.memory')).toBe(
      'Included only while the memory tool is on.',
    );
    expect(t('systemPrompt.blockList.ownerHint.channel')).toBe(
      'Included only while the agent has an active channel.',
    );
    // The retired composed template and per-owner tokens must not linger.
    for (const retiredKey of [
      'systemPrompt.blockList.appearsWhen',
      'systemPrompt.blockList.owner.always',
      'systemPrompt.blockList.owner.memory',
      'systemPrompt.blockList.owner.channel',
      'systemPrompt.blockList.owner.tool',
      'systemPrompt.blockList.owner.extension',
    ]) {
      expect(englishCatalog[retiredKey], retiredKey).toBeUndefined();
    }
  });

  it('contains Toasted design labels for Settings sections', () => {
    const requiredKeys = [
      'settings.title',
      'settings.sections',
      'settings.loading',
      'settings.loadError',
      'settings.saveError',
      'settings.general.title',
      'settings.general.subtitle',
      'settings.general.serverHost',
      'settings.general.serverHostDescription',
      'settings.general.dataDirectory',
      'settings.general.dataDirectoryDescription',
      'settings.recall.title',
      'settings.recall.subtitle',
      'settings.recall.backend',
      'settings.recall.backendDescription',
      'settings.recall.backends.jsonl_scan',
      'settings.recall.backends.sqlite_fts',
      'settings.recall.backends.vector',
      'settings.recall.vectorHint',
      'settings.recall.saveSuccess',
      'settings.specializedModels.embeddingModel',
      'settings.specializedModels.embeddingModelDescription',
      'settings.specializedModels.imageUnderstanding',
      'settings.specializedModels.imageUnderstandingDescription',
      'settings.providers.title',
      'settings.providers.subtitle',
      'settings.providers.noneConnected',
      'settings.providers.description.credentialKey',
      'settings.providers.description.baseUrl',
      'settings.providers.description.modelCount',
      'settings.providers.description.none',
      'settings.providers.replaceKey',
      'settings.providers.removeKeySuccess',
      'settings.providers.removeKeyError',
      'settings.providers.removeKeyStillEnv',
      'settings.providers.add.button',
      'settings.providers.add.connectionButton',
      'settings.providers.add.title',
      'settings.providers.add.chooseProvider',
      'settings.providers.add.chooseMethod',
      'settings.providers.add.allConnected',
      'settings.providers.add.methodApiKey',
      'settings.providers.add.methodApiKeyDescription',
      'settings.providers.add.methodOAuth',
      'settings.providers.add.methodOAuthDescription',
      'settings.providers.add.apiKeyLabel',
      'settings.providers.add.apiKeyPlaceholder',
      'settings.providers.add.apiKeyHint',
      'settings.providers.add.saveKey',
      'settings.providers.add.keyError',
      'settings.providers.add.oauthIntro',
      'settings.appearance.title',
      'settings.appearance.subtitle',
      'settings.appearance.language',
      'settings.appearance.languageDescription',
      'settings.appearance.chatWidth.label',
      'settings.appearance.chatWidth.description',
      'settings.appearance.chatWidth.comfortable',
      'settings.appearance.chatWidth.wide',
      'settings.appearance.chatWidth.full',
      'settings.appearance.saveSuccess',
      'settings.language.en',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('settings.recall.backends.vector')).toBe(
      'Semantic — finds matches by meaning, needs an embedding model',
    );
    expect(t('settings.recall.vectorHint')).toContain('embedding model');
    expect(t('settings.specializedModels.embeddingModel')).toBe(
      'Embedding model',
    );
    expect(t('settings.specializedModels.embeddingModelDescription')).toContain(
      'meaning-based search',
    );
    expect(t('settings.specializedModels.imageUnderstanding')).toBe(
      'Image understanding',
    );
    expect(
      t('settings.specializedModels.imageUnderstandingDescription'),
    ).toContain('analyze_image');
    // The consolidated Save key replaced the per-panel bespoke variants.
    expect(englishCatalog['settings.recall.save']).toBeUndefined();
    expect(englishCatalog['settings.compaction.save']).toBeUndefined();
    expect(englishCatalog['settings.webSearch.save']).toBeUndefined();
    expect(englishCatalog['debug.save']).toBeUndefined();
    expect(t('common.save')).toBe('Save');
    // The dead custom-endpoint placeholder row and its keys were removed.
    expect(englishCatalog['settings.providers.customEndpoint']).toBeUndefined();
    expect(
      englishCatalog['settings.providers.customEndpointDescription'],
    ).toBeUndefined();
    expect(
      englishCatalog['settings.providers.customEndpointStatus'],
    ).toBeUndefined();
    expect(englishCatalog['settings.providers.configure']).toBeUndefined();
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
      'Connected providers and their credentials.',
    );
    expect(t('settings.providers.add.button')).toBe('Add provider');
  });

  it('contains skill-manager copy for the Skills settings panel', () => {
    const requiredKeys = [
      'settings.skills.manageLabel',
      'settings.skills.manageDescription',
      'settings.skills.scopeLabel',
      'settings.skills.scopeGlobal',
      'settings.skills.scopeAgent',
      'settings.skills.loadError',
      'settings.skills.empty',
      'settings.skills.newSkill',
      'settings.skills.nameLabel',
      'settings.skills.contentLabel',
      'settings.skills.namePlaceholder',
      'settings.skills.contentPlaceholder',
      'settings.skills.create',
      'settings.skills.created',
      'settings.skills.createError',
      'settings.skills.saved',
      'settings.skills.contentSaveError',
      'settings.skills.deleted',
      'settings.skills.deleteError',
    ];

    expectCatalogKeys(requiredKeys);
    expect(
      t('settings.skills.scopeAgent', undefined, { name: 'Builder' }),
    ).toBe('Builder (private)');
  });

  it('contains Connected clients copy for the General settings panel', () => {
    const requiredKeys = [
      'settings.general.clients.title',
      'settings.general.clients.description',
      'settings.general.clients.loading',
      'settings.general.clients.empty',
      'settings.general.clients.loadError',
      'settings.general.clients.thisWindow',
      'settings.general.clients.connectedAt',
      'settings.general.clients.accessor.browser',
      'settings.general.clients.accessor.desktop',
      'settings.general.clients.accessor.unknown',
      'settings.general.clients.status.connected',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('settings.general.clients.title')).toBe('Connected clients');
    expect(t('settings.general.clients.thisWindow')).toBe('This window');
    expect(
      t('settings.general.clients.connectedAt', undefined, { time: '10:00' }),
    ).toBe('Connected 10:00');
  });

  it('contains the Setup guide re-entry copy for the Server info panel', () => {
    const requiredKeys = [
      'settings.general.setupGuide',
      'settings.general.setupGuideDescription',
      'settings.general.setupGuideAction',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('settings.general.setupGuide')).toBe('Setup guide');
    expect(t('settings.general.setupGuideAction')).toBe('Open setup guide');
    expect(t('settings.general.setupGuideDescription')).toContain('provider');
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
      'logs.copyEntry',
      'logs.copied',
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
    expect(t('logs.copyEntry')).toBe('Copy log line');
    expect(t('logs.copied')).toBe('Copied');
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

  it('contains Statistics tab copy for navigation, sub-views, and metrics', () => {
    const requiredKeys = [
      'navigation.statistics',
      'statistics.eyebrow',
      'statistics.title',
      'statistics.subtitle',
      'statistics.loading',
      'statistics.loadError',
      'statistics.empty',
      'statistics.none',
      'statistics.generatedAt',
      'statistics.estimatedBadge',
      'statistics.estimatedHint',
      'statistics.derivedHint',
      'statistics.subview.overview',
      'statistics.subview.usage',
      'statistics.subview.runs',
      'statistics.subview.tools',
      'statistics.subview.skills',
      'statistics.granularity.day',
      'statistics.granularity.week',
      'statistics.granularity.month',
      'statistics.status.completed',
      'statistics.status.failed',
      'statistics.status.cancelled',
      'statistics.role.assistant',
      'statistics.role.run_summary',
      'statistics.role.agent_takeover',
      'statistics.overview.agents',
      'statistics.overview.runs',
      'statistics.overview.chatMessages',
      'statistics.overview.chatMessagesByRole',
      'statistics.overview.sessionRecords',
      'statistics.overview.sessionRecordsHint',
      'statistics.overview.runHealth',
      'statistics.overview.totalRuns',
      'statistics.overview.completedLabel',
      'statistics.overview.statusAria',
      'statistics.overview.nonCompleted',
      'statistics.overview.activityReliability',
      'statistics.overview.activityWindow.day',
      'statistics.overview.activityWindow.week',
      'statistics.overview.activityWindow.month',
      'statistics.overview.noActivityPeriod',
      'statistics.overview.periodRuns',
      'statistics.overview.completionRate',
      'statistics.overview.peak',
      'statistics.overview.weekOf',
      'statistics.overview.activityTooltip',
      'statistics.overview.activityAria',
      'statistics.overview.chatMessagesHint',
      'statistics.overview.modelSteps',
      'statistics.overview.modelStepsHint',
      'statistics.usage.measuredTokens',
      'statistics.usage.estimatedTokens',
      'statistics.usage.cacheIntro',
      'statistics.usage.runAttributionHint',
      'statistics.usage.providers',
      'statistics.usage.models',
      'statistics.runs.p50Hint',
      'statistics.runs.p90Hint',
      'statistics.runs.p95Hint',
      'statistics.runs.cancelRate',
      'statistics.runs.failureRate',
      'statistics.runs.fallbackRuns',
      'statistics.runs.avgAgentMessagesPerRun',
      'statistics.runs.avgAgentMessagesHint',
      'statistics.runs.avgModelStepsPerRun',
      'statistics.runs.avgModelStepsHint',
      'statistics.runs.longest',
      'statistics.errors.byKind',
      'statistics.errors.byHour',
      'statistics.errors.scopeHint',
      'statistics.tools.perTool',
      'statistics.tools.outcomeNote',
      'statistics.skills.total',
      'statistics.skills.used',
      'statistics.skills.offeredUnactivated',
      'statistics.skills.withoutOfferData',
      'statistics.skills.intro',
      'statistics.skills.perSkill',
      'statistics.skills.empty',
      'statistics.skills.neverUsedBadge',
      'statistics.skills.neverUsedRowTitle',
      'statistics.skills.noOfferDataBadge',
      'statistics.skills.noOfferDataRowTitle',
      'statistics.skills.byAgent',
      'statistics.skills.origin.bundled',
      'statistics.skills.origin.global',
      'statistics.skills.origin.agent',
      'statistics.skills.origin.project',
      'statistics.col.tokens',
      'statistics.col.share',
      'statistics.col.skill',
      'statistics.col.origins',
      'statistics.col.offered',
      'statistics.col.activated',
      'statistics.col.usageRate',
      'statistics.col.firstActivated',
      'statistics.col.lastActivated',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('navigation.statistics')).toBe('Statistics');
    expect(t('statistics.generatedAt', undefined, { time: '12:00' })).toBe(
      'Generated 12:00',
    );
    expect(t('statistics.estimatedBadge')).toContain('estimated');
    expect(t('statistics.subview.runs')).toBe('Runs & errors');
    expect(t('statistics.subview.skills')).toBe('Skills');
    // The scoped origin labels interpolate the agent id / project name detail.
    expect(
      t('statistics.skills.origin.agent', undefined, { detail: 'assistant' }),
    ).toBe('agent: assistant');
    expect(
      t('statistics.skills.origin.project', undefined, { detail: 'vBot' }),
    ).toBe('project: vBot');
  });

  it('contains the project-agent badge label for the statistics tab', () => {
    expect(englishCatalog['statistics.agent.projectBadgeTitle']).toBeTruthy();
    expect(
      t('statistics.agent.projectBadgeTitle', undefined, { project: 'vbot' }),
    ).toBe('Project: vbot');
  });

  it('contains cron project-agent dropdown group labels', () => {
    expectCatalogKeys([
      'cron.form.agentGroup.identity',
      'cron.form.agentGroup.project',
    ]);
    expect(t('cron.form.agentGroup.identity')).toBe('Identity agents');
    expect(t('cron.form.agentGroup.project')).toBe('Project agents');
  });

  it('contains cron master-detail labels, info rows, and schedule presets', () => {
    expectCatalogKeys([
      'cron.list.ariaLabel',
      'cron.detail.createTitle',
      'cron.detail.editTitle',
      'cron.detail.status',
      'cron.detail.lastFired',
      'cron.detail.nextFire',
      'cron.form.preset',
      'cron.presets.custom',
      'cron.presets.every15Minutes',
      'cron.presets.hourly',
      'cron.presets.dailyMorning',
      'cron.presets.weekdayMornings',
      'cron.presets.mondayMornings',
      'cron.presets.monthlyFirst',
    ]);
    expect(t('cron.list.ariaLabel')).toBe('Scheduled Runs');
    expect(t('cron.detail.createTitle')).toBe('Create Scheduled Run');
    expect(t('cron.detail.editTitle')).toBe('Edit Scheduled Run');
    expect(t('cron.form.preset')).toBe('Schedule preset');
    expect(t('cron.presets.custom')).toBe('Custom');
    expect(t('cron.presets.every15Minutes')).toBe('Every 15 minutes');
    expect(t('cron.presets.monthlyFirst')).toBe('Monthly on the 1st at 9:00');
  });

  it('drops the retired cron table headers, modal titles, and edit action', () => {
    // The table+modal rebuild removed these — the master-detail view reuses none
    // of them, and the create/edit heading moved under `cron.detail.*`.
    for (const retiredKey of [
      'cron.table.caption',
      'cron.table.agent',
      'cron.table.prompt',
      'cron.table.schedule',
      'cron.table.timezone',
      'cron.table.status',
      'cron.table.lastFired',
      'cron.table.nextFire',
      'cron.table.actions',
      'cron.modal.createTitle',
      'cron.modal.editTitle',
      'cron.actions.editJob',
    ]) {
      expect(englishCatalog[retiredKey], retiredKey).toBeUndefined();
    }
  });

  it('contains Projects tab copy for navigation, add, list, manage, report, and re-point', () => {
    const requiredKeys = [
      'navigation.projects',
      'projects.eyebrow',
      'projects.title',
      'projects.subtitle',
      'projects.loading',
      'projects.loadError',
      'projects.emptyTitle',
      'projects.emptySubtitle',
      'projects.add.title',
      'projects.add.subtitle',
      'projects.add.cwd',
      'projects.add.cwdPlaceholder',
      'projects.add.cwdHelp',
      'projects.add.displayName',
      'projects.add.submit',
      'projects.add.submitting',
      'projects.add.missingCwd',
      'projects.add.error',
      'projects.add.success',
      'projects.list.title',
      'projects.manage.displayName',
      'projects.manage.defaultAgent',
      'projects.manage.defaultModel',
      'projects.manage.autoLoad',
      'projects.manage.save',
      'projects.manage.saving',
      'projects.manage.saveError',
      'projects.manage.saveSuccess',
      'projects.manage.unavailableToolHint',
      'projects.remove',
      'projects.remove.confirm',
      'projects.remove.error',
      'projects.remove.success',
      'projects.remove.busy',
      'projects.remove.inUse',
      'projects.team.title',
      'projects.repository.rescan',
      'projects.repository.rescanning',
      'projects.team.empty',
      'projects.team.noModel',
      'projects.report.title',
      'projects.report.findingCount',
      'projects.report.group.slug_collision',
      'projects.report.group.unslugifiable_name',
      'projects.report.group.bad_model',
      'projects.report.group.orphan',
      'projects.report.group.unavailable_tool',
      'projects.report.finding.agent',
      'projects.report.finding.source',
      'projects.rePoint.title',
      'projects.rePoint.description',
      'projects.rePoint.cwd',
      'projects.rePoint.submit',
      'projects.rePoint.submitting',
      'projects.rePoint.missingCwd',
      'projects.rePoint.error',
      'projects.rePoint.success',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('navigation.projects')).toBe('Projects');
    expect(t('projects.report.findingCount', undefined, { count: 3 })).toBe(
      '3 issues found',
    );
    expect(
      t('projects.report.finding.agent', undefined, { agentId: 'builder' }),
    ).toBe('Agent builder');
  });

  it('contains two-bar project chat copy for the dropdown, team bar, and scan banner', () => {
    const requiredKeys = [
      'chat.project.none',
      'chat.personalBarLabel',
      'chat.personalBarHint',
      'chat.project.selectAria',
      'chat.project.teamLabel',
      'chat.project.teamBarHint',
      'chat.project.teamEmpty',
      'chat.project.loadError',
      'chat.project.sessionError',
      'chat.project.scanBanner',
      'chat.project.scanBannerCount',
      'chat.project.scanBannerLink',
    ];

    expectCatalogKeys(requiredKeys);
    expect(t('chat.project.none')).toBe('No project selected');
    expect(t('chat.personalBarLabel')).toBe('Personal');
    expect(
      t('chat.project.scanBannerCount', undefined, { count: 2 }),
    ).toContain('2');
    expect(t('chat.project.scanBannerLink').toLowerCase()).toContain('project');
  });

  it('contains confirm-dialog titles, verbs, and consequence bodies', () => {
    const requiredKeys = [
      'common.reset',
      'projects.remove.confirmTitle',
      'sessions.delete_confirm_title',
      'cron.deleteConfirmTitle',
      'settings.channels.delete_confirm_title',
      'settings.channels.delete_confirm',
      'settings.skills.deleteConfirmTitle',
      'settings.skills.deleteConfirm',
      'systemPrompt.fragmentEditor.resetConfirmTitle',
      'systemPrompt.blockList.removeConfirmTitle',
      'systemPrompt.blockList.resetLayoutConfirmTitle',
    ];

    expectCatalogKeys(requiredKeys);

    // Confirm buttons carry the action verb; titles name the entity.
    expect(t('common.reset')).toBe('Reset');
    expect(t('projects.remove.confirmTitle')).toBe('Remove project');
    expect(t('sessions.delete_confirm_title')).toBe('Delete session');
    expect(t('cron.deleteConfirmTitle')).toBe('Delete Scheduled Run');
    expect(t('settings.channels.delete_confirm_title')).toBe('Delete channel');
    expect(t('settings.skills.deleteConfirmTitle')).toBe('Delete skill');
    expect(t('systemPrompt.fragmentEditor.resetConfirmTitle')).toBe(
      'Reset block',
    );
    expect(t('systemPrompt.blockList.removeConfirmTitle')).toBe('Remove block');
    expect(t('systemPrompt.blockList.resetLayoutConfirmTitle')).toBe(
      'Reset layout',
    );

    // The rewritten permanent-delete bodies state the consequence honestly and
    // interpolate the entity id/name.
    expect(
      t('settings.channels.delete_confirm', undefined, { id: 'tg-main' }),
    ).toBe(
      'Delete channel "tg-main" permanently? vBot stops listening on it and its configuration is removed.',
    );
    expect(
      t('settings.skills.deleteConfirm', undefined, { name: 'deploy' }),
    ).toBe(
      'Delete skill “deploy” permanently? The skill file is removed from disk.',
    );
  });

  it('contains the Extensions reload help', () => {
    expectCatalogKeys(['settings.extensions.reloadHelp']);
    expect(t('settings.extensions.reloadHelp')).toBe(
      'Rebuilds all extensions from disk — picks up code edits, new and removed extensions.',
    );
  });

  it('contains the shared toggle-chip allow-list copy', () => {
    expectCatalogKeys([
      'access.searchPlaceholder',
      'access.count',
      'access.allOn',
      'access.allOff',
      'access.noMatches',
      'access.toggle',
      'access.lockedAuto',
    ]);
    expect(t('access.count', undefined, { on: 3, total: 12 })).toBe(
      '3 / 12 on',
    );
    expect(t('access.toggle', undefined, { name: 'bash' })).toBe('Toggle bash');
    expect(t('access.allOn')).toBe('all on');
    expect(t('access.allOff')).toBe('all off');
  });

  it('does not expose Components showcase labels in the live catalog', () => {
    expect(englishCatalog['components.title']).toBeUndefined();
    expect(englishCatalog['components.toast.errorMessage']).toBeUndefined();
    expect(t('components.title')).toBe('components.title');
  });
});
