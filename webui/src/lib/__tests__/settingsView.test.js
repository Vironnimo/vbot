import { describe, expect, it } from 'vitest';

import {
  AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT,
  accountDisplayName,
  applyChannelPanelList,
  buildAgentDefaultsPayload,
  buildChannelCreatePayload,
  buildChannelUpdatePayload,
  buildChatWidthOptions,
  buildChatWorkingModeOptions,
  buildClientPresenceRows,
  buildLanguageOptions,
  buildProviderConnectPayload,
  buildProviderDisconnectPayload,
  buildRecallBackendOptions,
  buildRecallSettingsPayload,
  buildSessionTitleSettingsPayload,
  buildSubAgentSettingsPayload,
  buildTranscriptionAudioSettingsPayload,
  buildWebSearchProviderOptions,
  buildWebSearchSettingsPayload,
  connectionReachability,
  connectionSupportsAddAccount,
  createAppearanceUpdatePayload,
  createChannelPanelState,
  createSkillDirectoriesUpdatePayload,
  deriveAccountCredentialKey,
  describeAccountSource,
  describeProvider,
  formatServerHost,
  getAddProviderCandidates,
  getAddableConnections,
  getConfiguredConnections,
  getConnectedProviderItems,
  getConnectionAccounts,
  getDefaultSkillDirectoryValue,
  getPersistedChatWidth,
  getPersistedChatWorkingMode,
  getRecallSettings,
  getSkillDirectories,
  getUsableProviderItems,
  getWebSearchSettings,
  isAppearanceSaveDisabled,
  isConnectionEnabled,
  isValidAccountId,
  normalizeAccountId,
  normalizeAgentDefaultsSettings,
  normalizeSessionTitleSettings,
  normalizeSubAgentSettings,
  normalizeTranscriptionAudio,
} from '../settingsView.js';

// Echoes the i18n key and its parameters: the key and parameters are the
// contract, not the English fallback wording.
const t = (key, _fallback, params) =>
  params ? `${key} ${JSON.stringify(params)}` : key;

function providerSettings(items) {
  return { providers: { items } };
}

const ids = (items) => items.map((item) => item.id);

describe('providers and accounts', () => {
  it('derives connected providers and add candidates from connection state', () => {
    const apiKeyConfigured = {
      id: 'openai:api-key',
      type: 'api_key',
      configured: true,
      accounts: [{ id: 'default', usable: true, source: 'data_dir' }],
    };
    const oauthConnectable = {
      id: 'openai:subscription',
      type: 'oauth',
      configured: false,
      connectable: true,
      accounts: [],
    };
    const apiKeyMissing = {
      id: 'anthropic:api-key',
      type: 'api_key',
      configured: false,
      accounts: [],
    };
    // A static OAuth token cannot be added from the UI.
    const oauthStatic = {
      id: 'minimax:oauth',
      type: 'oauth',
      configured: false,
      connectable: false,
      accounts: [],
    };
    const settings = providerSettings([
      { id: 'openai', connections: [apiKeyConfigured, oauthConnectable] },
      { id: 'anthropic', connections: [apiKeyMissing] },
      { id: 'minimax', connections: [oauthStatic] },
    ]);

    expect(ids(getConnectedProviderItems(settings))).toEqual(['openai']);
    expect(ids(getAddableConnections(settings.providers.items[0]))).toEqual([
      'openai:subscription',
    ]);
    expect(ids(getAddProviderCandidates(settings))).toEqual(['anthropic']);

    // A usable account configures a connection without the configured flag.
    const accountOnly = {
      id: 'work',
      type: 'api_key',
      accounts: [{ id: 'work', usable: true, source: 'data_dir' }],
    };
    const unusableAccount = {
      id: 'unusable',
      type: 'api_key',
      accounts: [{ id: 'default', usable: false, source: 'data_dir' }],
    };
    expect(
      ids(
        getConfiguredConnections({
          connections: [accountOnly, unusableAccount, { accounts: [] }],
        }),
      ),
    ).toEqual(['work']);
  });

  it('keeps keyless providers addable until added and unusable while disabled', () => {
    const freshLocal = {
      id: 'ollama:local',
      type: 'none',
      added: false,
      configured: true,
      enabled: false,
      usable: false,
      accounts: [{ id: 'default', usable: true, source: 'none' }],
    };
    const fresh = providerSettings([
      { id: 'ollama', connections: [freshLocal] },
    ]);

    expect(getConnectedProviderItems(fresh)).toEqual([]);
    expect(getAddableConnections(fresh.providers.items[0])).toEqual([
      freshLocal,
    ]);
    expect(ids(getAddProviderCandidates(fresh))).toEqual(['ollama']);

    const disabledAddition = providerSettings([
      { id: 'ollama', connections: [{ ...freshLocal, added: true }] },
    ]);
    expect(ids(getConnectedProviderItems(disabledAddition))).toEqual([
      'ollama',
    ]);
    expect(getAddProviderCandidates(disabledAddition)).toEqual([]);
    expect(getUsableProviderItems(disabledAddition)).toEqual([]);
  });

  it('reads the enabled and reachability flags only when the server states them', () => {
    expect(isConnectionEnabled({ enabled: false })).toBe(false);
    expect(isConnectionEnabled({ enabled: true })).toBe(true);
    // An absent field never hides a keyed connection.
    expect(isConnectionEnabled({ id: 'openai:api-key' })).toBe(true);

    expect(connectionReachability({ reachable: false })).toBe(false);
    expect(connectionReachability({ reachable: true })).toBe(true);
    expect(connectionReachability({ id: 'openai:api-key' })).toBeNull();
  });

  it('extracts accounts and validates account ids', () => {
    const connection = {
      accounts: [
        { id: 'default', usable: true, source: 'process_env' },
        { id: 'work', usable: false, source: 'data_dir' },
        { id: '', usable: true },
        { usable: true },
      ],
    };
    expect(ids(getConnectionAccounts(connection))).toEqual(['default', 'work']);
    expect(getConnectionAccounts({})).toEqual([]);
    expect(getConnectionAccounts(null)).toEqual([]);

    for (const valid of ['default', 'work_2', '9lives', 'a'.repeat(32)]) {
      expect(isValidAccountId(valid), valid).toBe(true);
    }
    for (const invalid of [
      '',
      '_leading',
      'Upper',
      'with-dash',
      'a'.repeat(33),
      42,
    ]) {
      expect(isValidAccountId(invalid), String(invalid)).toBe(false);
    }

    expect(normalizeAccountId(' work ')).toBe('work');
    expect(normalizeAccountId('   ')).toBe('default');
    expect(normalizeAccountId(undefined)).toBe('default');
  });

  it('labels accounts and builds account-aware provider payloads', () => {
    expect(accountDisplayName({ id: 'default' }, t)).toBe(
      'settings.providers.accounts.defaultLabel',
    );
    expect(accountDisplayName({ id: 'work' }, t)).toBe('work');
    expect(describeAccountSource({ source: 'process_env' }, t)).toBe(
      'settings.providers.accounts.source.processEnv',
    );
    expect(describeAccountSource({ source: 'data_dir' }, t)).toBe(
      'settings.providers.accounts.source.dataDir',
    );
    expect(describeAccountSource({ source: 'oauth' }, t)).toBe(
      'settings.providers.accounts.source.oauth',
    );
    expect(describeAccountSource({}, t)).toBe('');

    expect(connectionSupportsAddAccount({ type: 'api_key' })).toBe(true);
    expect(
      connectionSupportsAddAccount({ type: 'oauth', connectable: true }),
    ).toBe(true);
    expect(
      connectionSupportsAddAccount({ type: 'oauth', connectable: false }),
    ).toBe(false);

    expect(deriveAccountCredentialKey('OPENAI_API_KEY', 'default')).toBe(
      'OPENAI_API_KEY',
    );
    expect(deriveAccountCredentialKey('OPENAI_API_KEY', '')).toBe(
      'OPENAI_API_KEY',
    );
    expect(deriveAccountCredentialKey('OPENAI_API_KEY', 'work')).toBe(
      'OPENAI_API_KEY__WORK',
    );

    expect(
      buildProviderConnectPayload('openai', 'openai:subscription', 'work'),
    ).toEqual({
      provider_id: 'openai',
      connection_id: 'openai:subscription',
      account: 'work',
    });
    expect(
      buildProviderConnectPayload('openai', 'openai:subscription').account,
    ).toBe('default');
    expect(
      buildProviderDisconnectPayload('openai', 'openai:subscription', ''),
    ).toEqual({
      provider_id: 'openai',
      connection_id: 'openai:subscription',
      account: 'default',
    });
  });

  it('describes provider metadata through i18n keys', () => {
    expect(
      describeProvider(
        {
          credential_key: 'OPENAI_API_KEY',
          base_url: 'https://api.openai.com/v1',
          model_count: 2,
        },
        t,
      ),
    ).toBe(
      [
        'settings.providers.description.credentialKey {"credentialKey":"OPENAI_API_KEY"}',
        'settings.providers.description.baseUrl {"baseUrl":"https://api.openai.com/v1"}',
        'settings.providers.description.modelCount {"count":2}',
      ].join(' '),
    );
    expect(describeProvider({}, t)).toBe('settings.providers.description.none');
  });
});

describe('general and appearance', () => {
  it('formats the server host and lists connected clients with the own row flagged', () => {
    expect(
      formatServerHost({ listen_host: '127.0.0.1', listen_port: 8420 }, t),
    ).toBe('127.0.0.1:8420');

    const roster = [
      {
        id: 'reg-1',
        connection_id: 'tab-self',
        accessor: 'browser',
        browser: 'Chrome',
        os: 'Windows',
        connected_at: '2026-06-20T10:00:00+00:00',
        status: 'connected',
      },
      { id: 'reg-2', connection_id: 'tab-other', status: 'connected' },
    ];
    const rows = buildClientPresenceRows(roster, 'tab-self');

    expect(rows[0]).toEqual({
      id: 'reg-1',
      connectionId: 'tab-self',
      accessor: 'browser',
      browser: 'Chrome',
      os: 'Windows',
      connectedAt: '2026-06-20T10:00:00+00:00',
      status: 'connected',
      isOwn: true,
    });
    expect(rows.map((row) => row.isOwn)).toEqual([true, false]);
    // An empty own id matches nothing, not the rows without a connection id.
    expect(
      buildClientPresenceRows([...roster, {}], '').map((row) => row.isOwn),
    ).toEqual([false, false, false]);
  });

  it('tolerates a malformed client roster', () => {
    expect(buildClientPresenceRows(null, 'tab-self')).toEqual([]);
    expect(buildClientPresenceRows([{}], 'tab-self')).toEqual([
      {
        id: '',
        connectionId: '',
        accessor: '',
        browser: '',
        os: '',
        connectedAt: '',
        status: '',
        isOwn: false,
      },
    ]);
  });

  it('reads appearance preferences and saves them as one section', () => {
    expect(
      buildLanguageOptions({ language: 'en', available_languages: ['en'] }),
    ).toEqual([
      { id: 'en', labelKey: 'settings.language.en', labelFallback: 'en' },
    ]);
    expect(buildChatWidthOptions()).toContainEqual({
      id: 'wide',
      labelKey: 'settings.appearance.chatWidth.wide',
      labelFallback: 'wide',
    });
    expect(buildChatWorkingModeOptions()).toContainEqual({
      id: 'compact',
      labelKey: 'settings.appearance.chatWorkingMode.compact',
      labelFallback: 'compact',
    });

    expect(getPersistedChatWidth({ appearance: { chat_width: 'full' } })).toBe(
      'full',
    );
    // Missing or unknown values fall back to the defaults.
    expect(getPersistedChatWidth({ appearance: { chat_width: 'huge' } })).toBe(
      'comfortable',
    );
    expect(getPersistedChatWidth(null)).toBe('comfortable');
    expect(
      getPersistedChatWorkingMode({
        appearance: { chat_working_mode: 'compact' },
      }),
    ).toBe('compact');
    expect(getPersistedChatWorkingMode(null)).toBe('normal');

    const unchanged = {
      loading: false,
      saving: false,
      selectedLanguageId: 'en',
      selectedChatWidth: 'comfortable',
      selectedChatWorkingMode: 'normal',
      persistedLanguageId: 'en',
      persistedChatWidth: 'comfortable',
      persistedChatWorkingMode: 'normal',
    };
    expect(isAppearanceSaveDisabled(unchanged)).toBe(true);
    expect(
      isAppearanceSaveDisabled({ ...unchanged, selectedChatWidth: 'wide' }),
    ).toBe(false);

    expect(
      createAppearanceUpdatePayload({
        language: 'fr',
        chatWidth: 'wide',
        chatWorkingMode: 'compact',
      }),
    ).toEqual({
      appearance: {
        language: 'fr',
        chat_width: 'wide',
        chat_working_mode: 'compact',
      },
    });
  });

  it('reads and saves trimmed skill directories', () => {
    const settings = {
      skills: {
        default_directory: 'C:/Users/test/.vbot/skills',
        directories: ['C:/skills/shared'],
      },
    };

    expect(getDefaultSkillDirectoryValue(settings, t)).toBe(
      'C:/Users/test/.vbot/skills',
    );
    expect(getSkillDirectories(settings)).toEqual(['C:/skills/shared']);
    expect(createSkillDirectoriesUpdatePayload([' C:/skills ', ''])).toEqual({
      skills: { directories: ['C:/skills'] },
    });
  });

  it('normalizes transcription audio to a preset or a validated custom profile', () => {
    expect(normalizeTranscriptionAudio({})).toEqual({
      profile: 'compatibility',
      format: 'wav',
      sample_rate_hz: 16000,
    });
    expect(
      buildTranscriptionAudioSettingsPayload({
        profile: 'custom',
        format: 'flac',
        sample_rate_hz: 24000,
      }),
    ).toEqual({
      speech: {
        transcription_audio: {
          profile: 'custom',
          format: 'flac',
          sample_rate_hz: 24000,
        },
      },
    });
  });
});

describe('agent defaults, sub-agents and session titles', () => {
  it('normalizes persisted Agent defaults', () => {
    expect(normalizeAgentDefaultsSettings({})).toEqual({
      model: '',
      fallback_models: [],
      temperature: null,
      thinking_effort: null,
    });
    expect(
      normalizeAgentDefaultsSettings({
        defaults: {
          agent: {
            model: ' openai/gpt-5.2 ',
            fallback_models: [' openai/gpt-5.1 ', ''],
            temperature: '0.6',
            thinking_effort: ' high ',
          },
        },
      }),
    ).toEqual({
      model: 'openai/gpt-5.2',
      fallback_models: ['openai/gpt-5.1'],
      temperature: 0.6,
      thinking_effort: 'high',
    });
    // An empty thinking effort is a stored choice, distinct from no default.
    expect(
      normalizeAgentDefaultsSettings({
        defaults: { agent: { thinking_effort: '' } },
      }).thinking_effort,
    ).toBe('');
  });

  it('builds the Agent defaults payload with cleared fields as null', () => {
    expect(
      buildAgentDefaultsPayload({
        model: ' openai/gpt-5.2 ',
        fallback_models: ['openai/gpt-5.1'],
        // Comma decimal separator typed in comma-decimal locales.
        temperature: '0,7',
        thinking_effort: '',
      }),
    ).toEqual({
      defaults: {
        agent: {
          model: 'openai/gpt-5.2',
          fallback_models: ['openai/gpt-5.1'],
          temperature: 0.7,
          thinking_effort: '',
        },
      },
    });
    expect(
      buildAgentDefaultsPayload({
        model: '',
        fallback_models: [],
        temperature: '',
        thinking_effort: AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT,
      }),
    ).toEqual({
      defaults: {
        agent: {
          model: null,
          fallback_models: null,
          temperature: null,
          thinking_effort: null,
        },
      },
    });
  });

  it('normalizes sub-agent limits to positive integers', () => {
    expect(normalizeSubAgentSettings({})).toEqual({
      max_subagent_depth: 4,
      max_subagents_per_turn: 8,
      subagent_timeout_minutes: 60,
    });
    expect(
      normalizeSubAgentSettings({
        subagents: {
          max_subagent_depth: '6',
          max_subagents_per_turn: 0,
          subagent_timeout_minutes: 90,
        },
      }),
    ).toEqual({
      max_subagent_depth: 6,
      max_subagents_per_turn: 8,
      subagent_timeout_minutes: 90,
    });
    expect(
      buildSubAgentSettingsPayload({
        max_subagent_depth: '7',
        max_subagents_per_turn: '9',
        subagent_timeout_minutes: '30',
      }),
    ).toEqual({
      subagents: {
        max_subagent_depth: 7,
        max_subagents_per_turn: 9,
        subagent_timeout_minutes: 30,
      },
    });
  });

  it('normalizes session title settings and builds the complete section', () => {
    expect(normalizeSessionTitleSettings({})).toEqual({
      enabled: false,
      model: '',
    });
    expect(
      buildSessionTitleSettingsPayload({
        enabled: true,
        model: 'openai/gpt-4.1-mini::api-key',
      }),
    ).toEqual({
      session_titles: { enabled: true, model: 'openai/gpt-4.1-mini::api-key' },
    });
  });
});

describe('retrieval', () => {
  it('keeps the recall backend within the offered backends', () => {
    expect(getRecallSettings({})).toEqual({
      backend: 'sqlite_fts',
      available_backends: ['sqlite_fts', 'vector', 'hybrid'],
    });
    expect(
      getRecallSettings({
        recall: { backend: 'hybrid', available_backends: ['sqlite_fts'] },
      }),
    ).toEqual({ backend: 'sqlite_fts', available_backends: ['sqlite_fts'] });
    expect(buildRecallSettingsPayload({ backend: 'sqlite_fts' })).toEqual({
      recall: { backend: 'sqlite_fts' },
    });
    expect(
      buildRecallBackendOptions({ available_backends: ['vector'] }, t),
    ).toEqual([{ value: 'vector', label: 'settings.recall.backends.vector' }]);
  });

  it('normalizes web search settings and builds a trimmed payload', () => {
    expect(getWebSearchSettings({})).toMatchObject({
      provider: 'brave',
      default_count: 12,
      searxng: { base_url: 'http://localhost:8888' },
    });
    expect(
      getWebSearchSettings({
        web_search: {
          provider: 'searxng',
          available_providers: ['brave', 'searxng'],
          default_count: 15,
          searxng: { base_url: ' http://localhost:9999 ' },
        },
      }),
    ).toEqual({
      provider: 'searxng',
      available_providers: ['brave', 'searxng'],
      default_count: 15,
      searxng: { base_url: 'http://localhost:9999' },
    });
    expect(
      getWebSearchSettings({
        web_search: { provider: 'brave', default_count: '0' },
      }).default_count,
    ).toBe(12);
    expect(
      buildWebSearchSettingsPayload({
        provider: 'searxng',
        default_count: 15,
        searxng: { base_url: ' http://localhost:9999 ' },
      }),
    ).toEqual({
      web_search: {
        provider: 'searxng',
        default_count: 15,
        searxng: { base_url: 'http://localhost:9999' },
      },
    });
    expect(
      buildWebSearchProviderOptions(
        { available_providers: ['brave', 'searxng'] },
        t,
      ),
    ).toEqual([
      { value: 'brave', label: 'settings.webSearch.providers.brave' },
      { value: 'searxng', label: 'settings.webSearch.providers.searxng' },
    ]);
  });
});

describe('channels', () => {
  const telegram = {
    id: 'tg-assistant',
    platform: 'telegram',
    agent_id: 'assistant',
    token_env_var: 'TELEGRAM_BOT_TOKEN_TG_ASSISTANT',
  };

  it('normalizes the channel list and keeps the selected channel', () => {
    const state = {
      ...createChannelPanelState(),
      loading: true,
      error: 'failed',
      selectedChannelId: 'tg-assistant-b',
    };

    const next = applyChannelPanelList(state, {
      channels: [
        {
          id: 'tg-assistant-b',
          platform: 'telegram',
          agent_id: 'assistant-b',
          dm_scope: 'main',
          allowed_chat_ids: ['12345', -100],
          token_env_var: 'TELEGRAM_BOT_TOKEN_B',
          enabled: false,
          running: 'true',
        },
        {
          id: 'tg-assistant-a',
          platform: 'telegram',
          agent_id: 'assistant-a',
          token_env_var: 'TELEGRAM_BOT_TOKEN_A',
          allowed_chat_ids: [777],
        },
      ],
    });

    expect(next.loading).toBe(false);
    expect(next.error).toBeNull();
    expect(next.selectedChannelId).toBe('tg-assistant-b');
    expect(ids(next.channels)).toEqual(['tg-assistant-a', 'tg-assistant-b']);
    expect(next.channels[1]).toMatchObject({
      dm_scope: 'main',
      allowed_chat_ids: ['12345', '-100'],
      enabled: false,
      running: true,
    });
  });

  it('builds create payloads with defaults, parsed chat ids and coerced booleans', () => {
    expect(buildChannelCreatePayload(telegram)).toEqual({
      ...telegram,
      dm_scope: 'per_conversation',
      allowed_chat_ids: [],
      enabled: true,
    });
    expect(
      buildChannelCreatePayload({
        ...telegram,
        dm_scope: 'main',
        allowed_chat_ids: '12345, -100\n12345',
        enabled: 'false',
      }),
    ).toEqual({
      ...telegram,
      dm_scope: 'main',
      allowed_chat_ids: ['12345', '-100'],
      enabled: false,
    });
  });

  it('applies the platform requirements when creating a channel', () => {
    const base = {
      id: 'chat',
      agent_id: 'assistant',
      allowed_chat_ids: 'self',
    };

    // WhatsApp pairs by QR code: created disabled and without a token.
    const whatsapp = buildChannelCreatePayload({
      ...base,
      platform: 'whatsapp',
    });
    expect(whatsapp.enabled).toBe(false);
    expect(whatsapp.token_env_var).toBeUndefined();

    expect(
      buildChannelCreatePayload({
        ...base,
        platform: 'slack',
        token_env_var: 'BOT',
        app_token_env_var: 'APP',
      }).app_token_env_var,
    ).toBe('APP');
    expect(() =>
      buildChannelCreatePayload({
        ...base,
        platform: 'slack',
        token_env_var: 'BOT',
      }),
    ).toThrow(/app_token_env_var/u);
    expect(() =>
      buildChannelCreatePayload({
        ...base,
        platform: 'mattermost',
        token_env_var: 'BOT',
      }),
    ).toThrow(/server_url/u);
    expect(() =>
      buildChannelCreatePayload({ ...telegram, platform: 'invalid' }),
    ).toThrow(/platform must be one of/u);
    expect(() =>
      buildChannelCreatePayload({ ...telegram, allowed_chat_ids: [false] }),
    ).toThrow(/allowed_chat_ids/u);
  });

  it('builds update payloads from partial form data', () => {
    expect(
      buildChannelUpdatePayload({ id: 'tg-assistant', enabled: true }),
    ).toEqual({ id: 'tg-assistant', enabled: true });
    // Opaque and large platform ids stay strings, never rounded numbers.
    expect(
      buildChannelUpdatePayload({
        id: 'chat',
        allowed_chat_ids: 'C123, 123456789012345678, self',
      }),
    ).toEqual({
      id: 'chat',
      allowed_chat_ids: ['C123', '123456789012345678', 'self'],
    });
    expect(() => buildChannelUpdatePayload({ id: 'tg-assistant' })).toThrow(
      /At least one channel field/u,
    );
    expect(() =>
      buildChannelUpdatePayload({ id: 'tg-assistant', dm_scope: 'invalid' }),
    ).toThrow(/dm_scope must be one of/u);
  });
});
