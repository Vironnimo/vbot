import { describe, expect, it } from 'vitest';
import { englishCatalog, init, t } from '../i18n.js';
import { expectCatalogKeys } from './i18n.support.js';

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
      'chat.activity.cancelSubAgentAria',
      'chat.activity.cancelBashAria',
      'chat.cancelBackgroundTaskError',
    ];

    for (const key of requiredKeys) {
      expect(englishCatalog[key], key).toBeTruthy();
      expect(t(key), key).toBe(englishCatalog[key]);
    }

    expect(t('chat.cancelToolCallAria').toLowerCase()).toContain('tool');
    expect(t('chat.cancelSubAgentAria').toLowerCase()).toContain('sub');
  });

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
      'chat.durationMinutesSeconds',
      'chat.durationHoursMinutes',
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
      'agents.form.fallbackModelsHelp',
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
    expect(t('agents.form.memoryModeHelp')).toContain(
      'Tool access is independent',
    );
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
    for (const owner of ['always', 'memory', 'channel']) {
      const key = `systemPrompt.blockList.ownerHint.${owner}`;
      expect(t(key)).not.toBe(key);
      expect(t(key).length).toBeGreaterThan(0);
    }
    for (const owner of ['tool', 'extension']) {
      const key = `systemPrompt.blockList.ownerHint.${owner}`;
      expect(t(key, undefined, { name: 'OWNER-NAME-FIXTURE' })).toContain(
        'OWNER-NAME-FIXTURE',
      );
    }
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
      'settings.general.timezone',
      'settings.general.timezoneDescription',
      'settings.general.timezoneSearch',
      'settings.recall.title',
      'settings.recall.subtitle',
      'settings.recall.backend',
      'settings.recall.backendDescription',
      'settings.recall.backends.canonical_scan',
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
      'settings.appearance.chatWorkingMode.label',
      'settings.appearance.chatWorkingMode.description',
      'settings.appearance.chatWorkingMode.normal',
      'settings.appearance.chatWorkingMode.compact',
      'chat.working.active',
      'chat.working.done',
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
});
