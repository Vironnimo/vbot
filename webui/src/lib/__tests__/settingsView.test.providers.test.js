// @vitest-environment jsdom

import { describe, expect, it } from 'vitest';

import {
  AGENT_DEFAULTS_FIELDS,
  AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT,
  accountDisplayName,
  buildAgentDefaultsPayload,
  buildChatWidthOptions,
  buildLanguageOptions,
  buildProviderConnectPayload,
  buildProviderDisconnectPayload,
  buildRecallBackendOptions,
  buildRecallSettingsPayload,
  buildSubAgentSettingsPayload,
  buildWebSearchProviderOptions,
  buildWebSearchSettingsPayload,
  connectionSupportsAddAccount,
  createAppearanceUpdatePayload,
  createSkillDirectoriesUpdatePayload,
  deriveAccountCredentialKey,
  describeAccountSource,
  describeProvider,
  formatServerHost,
  getAddProviderCandidates,
  getAddableConnections,
  getConnectedProviderItems,
  getConnectionAccounts,
  getDefaultSkillDirectoryValue,
  getPersistedChatWidth,
  getRecallSettings,
  getSkillDirectories,
  getUsableProviderItems,
  getWebSearchSettings,
  connectionReachability,
  isAppearanceSaveDisabled,
  isConnectionConfigured,
  isConnectionEnabled,
  isConnectionUsable,
  isValidAccountId,
  normalizeAccountId,
  normalizeAgentDefaultsSettings,
  normalizeSubAgentSettings,
} from '../settingsView.js';
import { createSettingsPayload, translate } from './settingsView.support.js';

describe('settingsView helpers', () => {
  it('filters connected providers and add candidates by connection state', () => {
    const apiKeyConfigured = {
      id: 'openai:api-key',
      type: 'api_key',
      label: 'API Key',
      configured: true,
      credential_key: 'OPENAI_API_KEY',
      accounts: [
        {
          id: 'default',
          usable: true,
          source: 'data_dir',
          credential_key: 'OPENAI_API_KEY',
        },
      ],
    };
    const apiKeyMissing = {
      id: 'anthropic:api-key',
      type: 'api_key',
      label: 'API Key',
      configured: false,
      credential_key: 'ANTHROPIC_API_KEY',
      accounts: [],
    };
    const oauthConnectable = {
      id: 'openai:subscription',
      type: 'oauth',
      label: 'ChatGPT Plus/Pro',
      configured: false,
      connectable: true,
      accounts: [],
    };
    const oauthStatic = {
      id: 'minimax:oauth',
      type: 'oauth',
      label: 'Token',
      configured: false,
      connectable: false,
      accounts: [],
    };
    const settings = {
      providers: {
        items: [
          {
            id: 'openai',
            name: 'OpenAI',
            connections: [apiKeyConfigured, oauthConnectable],
          },
          {
            id: 'anthropic',
            name: 'Anthropic',
            connections: [apiKeyMissing],
          },
          {
            id: 'minimax',
            name: 'MiniMax',
            connections: [oauthStatic],
          },
        ],
      },
    };

    expect(isConnectionConfigured(apiKeyConfigured)).toBe(true);
    expect(isConnectionConfigured(apiKeyMissing)).toBe(false);
    expect(
      getConnectedProviderItems(settings).map((provider) => provider.id),
    ).toEqual(['openai']);
    expect(
      getAddableConnections(settings.providers.items[0]).map(
        (connection) => connection.id,
      ),
    ).toEqual(['openai:subscription']);
    // MiniMax has no UI-addable connection (static oauth token), so only
    // Anthropic remains an add candidate.
    expect(
      getAddProviderCandidates(settings).map((provider) => provider.id),
    ).toEqual(['anthropic']);
  });

  it('treats a connection with a usable account as configured without flags', () => {
    const accountOnlyConnection = {
      id: 'openai:api-key',
      type: 'api_key',
      accounts: [
        {
          id: 'work',
          usable: true,
          source: 'data_dir',
          credential_key: 'OPENAI_API_KEY__WORK',
        },
      ],
    };

    expect(isConnectionConfigured(accountOnlyConnection)).toBe(true);
    expect(
      isConnectionConfigured({
        id: 'openai:api-key',
        type: 'api_key',
        accounts: [{ id: 'default', usable: false, source: 'data_dir' }],
      }),
    ).toBe(false);
    expect(isConnectionConfigured({ accounts: [] })).toBe(false);
  });

  it('reads the connection enabled flag with enabled as the fallback', () => {
    expect(isConnectionEnabled({ id: 'ollama:local', enabled: false })).toBe(
      false,
    );
    expect(isConnectionEnabled({ id: 'ollama:local', enabled: true })).toBe(
      true,
    );
    // Absent field never hides a keyed connection.
    expect(isConnectionEnabled({ id: 'openai:api-key' })).toBe(true);
  });

  it('keeps a configured but disabled keyless provider out of usable providers', () => {
    const localConnection = {
      id: 'ollama:local',
      type: 'none',
      configured: true,
      enabled: false,
      usable: false,
      accounts: [{ id: 'default', usable: true, source: 'none' }],
    };
    const settings = {
      providers: {
        items: [{ id: 'ollama', connections: [localConnection] }],
      },
    };

    expect(isConnectionConfigured(localConnection)).toBe(true);
    expect(isConnectionUsable(localConnection)).toBe(false);
    expect(
      getConnectedProviderItems(settings).map((provider) => provider.id),
    ).toEqual(['ollama']);
    expect(getUsableProviderItems(settings)).toEqual([]);
  });

  it('reads probe reachability only when the server states it', () => {
    expect(connectionReachability({ reachable: false })).toBe(false);
    expect(connectionReachability({ reachable: true })).toBe(true);
    expect(connectionReachability({ id: 'openai:api-key' })).toBe(null);
  });

  it('extracts connection accounts and drops malformed entries', () => {
    const connection = {
      id: 'openai:api-key',
      type: 'api_key',
      accounts: [
        {
          id: 'default',
          usable: true,
          source: 'process_env',
          credential_key: 'OPENAI_API_KEY',
        },
        {
          id: 'work',
          usable: false,
          source: 'data_dir',
          credential_key: 'OPENAI_API_KEY__WORK',
        },
        { id: '', usable: true },
        { usable: true },
      ],
    };

    expect(getConnectionAccounts(connection).map((item) => item.id)).toEqual([
      'default',
      'work',
    ]);
    expect(getConnectionAccounts({})).toEqual([]);
    expect(getConnectionAccounts(null)).toEqual([]);
  });

  it('validates and normalizes account ids', () => {
    expect(isValidAccountId('default')).toBe(true);
    expect(isValidAccountId('work_2')).toBe(true);
    expect(isValidAccountId('9lives')).toBe(true);
    expect(isValidAccountId('a'.repeat(32))).toBe(true);

    expect(isValidAccountId('')).toBe(false);
    expect(isValidAccountId('_leading')).toBe(false);
    expect(isValidAccountId('Upper')).toBe(false);
    expect(isValidAccountId('with-dash')).toBe(false);
    expect(isValidAccountId('a'.repeat(33))).toBe(false);
    expect(isValidAccountId(42)).toBe(false);

    expect(normalizeAccountId('')).toBe('default');
    expect(normalizeAccountId('   ')).toBe('default');
    expect(normalizeAccountId(' work ')).toBe('work');
    expect(normalizeAccountId(undefined)).toBe('default');
  });

  it('describes accounts and builds account-aware provider payloads', () => {
    expect(accountDisplayName({ id: 'default', usable: true }, translate)).toBe(
      'Default',
    );
    expect(accountDisplayName({ id: 'work', usable: true }, translate)).toBe(
      'work',
    );

    expect(describeAccountSource({ source: 'process_env' }, translate)).toBe(
      'Process env',
    );
    expect(describeAccountSource({ source: 'data_dir' }, translate)).toBe(
      '.env file',
    );
    expect(describeAccountSource({ source: 'oauth' }, translate)).toBe('OAuth');
    expect(describeAccountSource({}, translate)).toBe('');

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
      buildProviderConnectPayload('openai', 'openai:subscription'),
    ).toEqual({
      provider_id: 'openai',
      connection_id: 'openai:subscription',
      account: 'default',
    });
    expect(
      buildProviderDisconnectPayload('openai', 'openai:subscription', ''),
    ).toEqual({
      provider_id: 'openai',
      connection_id: 'openai:subscription',
      account: 'default',
    });
  });

  it('formats provider metadata and current status labels', () => {
    const provider = {
      name: 'OpenAI',
      base_url: 'https://api.openai.com/v1',
      credential_key: 'OPENAI_API_KEY',
      credentials_configured: true,
      status: 'configured',
      model_count: 2,
    };

    expect(
      formatServerHost(
        { listen_host: '127.0.0.1', listen_port: 8420 },
        translate,
      ),
    ).toBe('127.0.0.1:8420');
    expect(
      buildLanguageOptions({ language: 'en', available_languages: ['en'] }),
    ).toEqual([
      {
        id: 'en',
        labelKey: 'settings.language.en',
        labelFallback: 'en',
      },
    ]);
    expect(
      createAppearanceUpdatePayload({ language: 'fr', chatWidth: 'wide' }),
    ).toEqual({
      appearance: {
        language: 'fr',
        chat_width: 'wide',
      },
    });
    expect(buildChatWidthOptions().map((option) => option.id)).toEqual([
      'comfortable',
      'wide',
      'full',
    ]);
    expect(getPersistedChatWidth({ appearance: { chat_width: 'full' } })).toBe(
      'full',
    );
    // Missing or unknown values fall back to the comfortable default.
    expect(getPersistedChatWidth({ appearance: { chat_width: 'huge' } })).toBe(
      'comfortable',
    );
    expect(getPersistedChatWidth(null)).toBe('comfortable');
    expect(
      isAppearanceSaveDisabled({
        loading: false,
        saving: false,
        selectedLanguageId: 'en',
        selectedChatWidth: 'wide',
        persistedLanguageId: 'en',
        persistedChatWidth: 'comfortable',
      }),
    ).toBe(false);
    expect(
      isAppearanceSaveDisabled({
        loading: false,
        saving: false,
        selectedLanguageId: 'en',
        selectedChatWidth: 'comfortable',
        persistedLanguageId: 'en',
        persistedChatWidth: 'comfortable',
      }),
    ).toBe(true);
    expect(createSkillDirectoriesUpdatePayload([' C:/skills ', ''])).toEqual({
      skills: {
        directories: ['C:/skills'],
      },
    });
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
    expect(getRecallSettings({})).toEqual({
      backend: 'jsonl_scan',
      available_backends: ['jsonl_scan', 'sqlite_fts'],
    });
    expect(
      getRecallSettings({
        recall: {
          backend: 'sqlite_fts',
          available_backends: ['jsonl_scan', 'sqlite_fts'],
        },
      }),
    ).toEqual({
      backend: 'sqlite_fts',
      available_backends: ['jsonl_scan', 'sqlite_fts'],
    });
    expect(buildRecallSettingsPayload({ backend: 'sqlite_fts' })).toEqual({
      recall: {
        backend: 'sqlite_fts',
      },
    });
    expect(buildRecallBackendOptions(getRecallSettings({}), translate)).toEqual(
      [
        { value: 'jsonl_scan', label: 'JSONL scan' },
        { value: 'sqlite_fts', label: 'SQLite FTS' },
      ],
    );
    expect(getWebSearchSettings({})).toEqual({
      provider: 'brave',
      available_providers: ['brave', 'searxng'],
      default_count: 12,
      searxng: {
        base_url: 'http://localhost:8888',
      },
    });
    expect(
      getWebSearchSettings({
        web_search: {
          provider: 'searxng',
          available_providers: ['brave', 'searxng'],
          default_count: 15,
          searxng: {
            base_url: ' http://localhost:9999 ',
          },
        },
      }),
    ).toEqual({
      provider: 'searxng',
      available_providers: ['brave', 'searxng'],
      default_count: 15,
      searxng: {
        base_url: 'http://localhost:9999',
      },
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
        searxng: {
          base_url: ' http://localhost:9999 ',
        },
      }),
    ).toEqual({
      web_search: {
        provider: 'searxng',
        default_count: 15,
        searxng: {
          base_url: 'http://localhost:9999',
        },
      },
    });
    expect(
      buildWebSearchProviderOptions(getWebSearchSettings({}), translate),
    ).toEqual([
      { value: 'brave', label: 'Brave Search' },
      { value: 'searxng', label: 'SearXNG' },
    ]);
    expect(
      getDefaultSkillDirectoryValue(createSettingsPayload(), translate),
    ).toBe('C:/Users/test/.vbot/skills');
    expect(getSkillDirectories(createSettingsPayload())).toEqual([
      'C:/skills/shared',
    ]);
    expect(describeProvider(provider, translate)).toBe(
      'Credential key: OPENAI_API_KEY. Endpoint: https://api.openai.com/v1. 2 models available.',
    );
    expect(AGENT_DEFAULTS_FIELDS).toEqual([
      'model',
      'fallback_model',
      'temperature',
      'thinking_effort',
    ]);
    expect(normalizeAgentDefaultsSettings({})).toEqual({
      model: '',
      fallback_model: '',
      temperature: null,
      thinking_effort: null,
    });
    expect(
      normalizeAgentDefaultsSettings({
        defaults: {
          agent: {
            model: ' openai/gpt-5.2 ',
            fallback_model: ' ',
            temperature: '0.6',
            thinking_effort: ' high ',
          },
        },
      }),
    ).toEqual({
      model: 'openai/gpt-5.2',
      fallback_model: '',
      temperature: 0.6,
      thinking_effort: 'high',
    });
    expect(
      normalizeAgentDefaultsSettings({
        defaults: {
          agent: {
            thinking_effort: '',
          },
        },
      }),
    ).toEqual({
      model: '',
      fallback_model: '',
      temperature: null,
      thinking_effort: '',
    });
    expect(
      buildAgentDefaultsPayload({
        model: ' openai/gpt-5.2 ',
        fallback_model: '',
        temperature: '',
        thinking_effort: '',
      }),
    ).toEqual({
      defaults: {
        agent: {
          model: 'openai/gpt-5.2',
          fallback_model: null,
          temperature: null,
          thinking_effort: '',
        },
      },
    });
    expect(
      buildAgentDefaultsPayload({
        model: '',
        fallback_model: ' ',
        temperature: '',
        thinking_effort: AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT,
      }),
    ).toEqual({
      defaults: {
        agent: {
          model: null,
          fallback_model: null,
          temperature: null,
          thinking_effort: null,
        },
      },
    });
  });
});
