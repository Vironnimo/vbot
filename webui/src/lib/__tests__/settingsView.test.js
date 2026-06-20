// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../i18n.js';
import {
  AGENT_DEFAULTS_FIELDS,
  AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT,
  accountDisplayName,
  buildAgentDefaultsPayload,
  buildChatWidthOptions,
  buildClientPresenceRows,
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
  getWebSearchSettings,
  isAppearanceSaveDisabled,
  isConnectionConfigured,
  isValidAccountId,
  normalizeAccountId,
  normalizeAgentDefaultsSettings,
  normalizeCompactionSettings,
  normalizeSubAgentSettings,
} from '../settingsView.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
}));

const { default: SettingsView } =
  await import('../../components/SettingsView.svelte');

describe('SettingsView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
  });

  it('renders the desktop split layout, loads settings data, and keeps token-count controls absent', async () => {
    rpcMock.mockResolvedValue(createSettingsPayload());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();

    const root = document.body.querySelector(
      'section.settings-layout.view.active',
    );
    expect(root).not.toBeNull();
    expect(root?.firstElementChild?.classList.contains('settings-nav')).toBe(
      true,
    );
    expect(root?.lastElementChild?.classList.contains('settings-content')).toBe(
      true,
    );
    expect(document.body.textContent).toContain('Loading settings…');

    await waitForText('0.0.0.0:9001');

    expect(rpcMock).toHaveBeenCalledWith('settings.get');
    expect(document.body.textContent).toContain('Server host');
    expect(document.body.textContent).toContain('0.0.0.0:9001');
    expect(document.body.textContent).toContain('Data directory');
    expect(document.body.textContent).toContain('C:/Users/test/.vbot');
    expect(document.body.textContent).not.toMatch(
      /show[_ -]?token[_ -]?counts/i,
    );
    expect(document.body.textContent).not.toMatch(/token count/i);

    clickButton('Providers');

    expect(document.body.textContent).toContain('OpenAI');
    expect(document.body.textContent).toContain('Connected');
    expect(document.body.textContent).not.toContain('Anthropic');
    expect(document.body.textContent).toContain('Add provider');
    expect(document.body.textContent).toContain('Custom endpoint');
    expect(document.body.textContent).toContain('Placeholder');
  });

  it('adds, removes, and saves skill directories', async () => {
    const toastMock = vi.fn();
    rpcMock
      .mockResolvedValueOnce(createSettingsPayload())
      .mockImplementationOnce(async (_method, params) =>
        createSettingsPayload({
          skills: {
            default_directory: 'C:/Users/test/.vbot/skills',
            directories: params.skills.directories,
          },
        }),
      );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForText('0.0.0.0:9001');
    clickButton('Skills');

    expect(document.body.textContent).toContain('Default skill directory');
    expect(document.body.textContent).toContain('C:/Users/test/.vbot/skills');
    expect(document.body.textContent).toContain('C:/skills/shared');

    const input = document.body.querySelector('input.s-input');
    expect(input).not.toBeNull();
    input.value = 'D:/skills/team';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    clickButton('Add directory');
    expect(document.body.textContent).toContain('D:/skills/team');

    clickButton('Remove');
    expect(document.body.textContent).not.toContain('C:/skills/shared');

    clickButton('Save');

    expect(rpcMock).toHaveBeenNthCalledWith(2, 'settings.update', {
      skills: {
        directories: ['D:/skills/team'],
      },
    });

    await waitForCondition(() => toastMock.mock.calls.length > 0);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Skill directories updated.',
        variant: 'success',
      }),
    );
  });

  it('edits and saves sub-agent settings', async () => {
    const toastMock = vi.fn();
    rpcMock
      .mockResolvedValueOnce(createSettingsPayload())
      .mockImplementationOnce(async (_method, params) =>
        createSettingsPayload({
          subagents: params.subagents,
        }),
      );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForText('0.0.0.0:9001');
    clickButton('Sub-Agents');

    expect(document.body.textContent).toContain('Max sub-agent depth');
    expect(document.body.textContent).toContain('Max sub-agents per turn');
    expect(document.body.textContent).toContain('Timeout minutes');

    const inputs = document.body.querySelectorAll('input.s-input');
    expect(inputs).toHaveLength(3);
    expect(inputs[0].value).toBe('4');
    expect(inputs[1].value).toBe('8');
    expect(inputs[2].value).toBe('60');

    inputs[0].value = '5';
    inputs[0].dispatchEvent(new Event('input', { bubbles: true }));
    inputs[1].value = '12';
    inputs[1].dispatchEvent(new Event('input', { bubbles: true }));
    inputs[2].value = '45';
    inputs[2].dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    clickButton('Save');

    expect(rpcMock).toHaveBeenNthCalledWith(2, 'settings.update', {
      subagents: {
        max_subagent_depth: 5,
        max_subagents_per_turn: 12,
        subagent_timeout_minutes: 45,
      },
    });

    await waitForCondition(() => toastMock.mock.calls.length > 0);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Sub-agent settings updated.',
        variant: 'success',
      }),
    );
  });

  it('selects and saves the recall backend', async () => {
    const toastMock = vi.fn();
    rpcMock
      .mockResolvedValueOnce(createSettingsPayload())
      .mockImplementationOnce(async (_method, params) =>
        createSettingsPayload({
          recall: {
            backend: params.recall.backend,
            available_backends: ['jsonl_scan', 'sqlite_fts'],
          },
        }),
      );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForText('0.0.0.0:9001');
    clickButton('Recall');

    expect(document.body.textContent).toContain('Recall backend');

    const trigger = document.body.querySelector('#settings-recall-backend');
    expect(trigger).not.toBeNull();
    trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    const sqliteOption = Array.from(
      document.body.querySelectorAll('.dropdown-option'),
    ).find((option) => option.textContent.trim() === 'SQLite FTS');
    expect(sqliteOption).toBeTruthy();
    sqliteOption.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    clickButton('Save');

    expect(rpcMock).toHaveBeenNthCalledWith(2, 'settings.update', {
      recall: {
        backend: 'sqlite_fts',
      },
    });

    await waitForCondition(() => toastMock.mock.calls.length > 0);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Recall backend updated.',
        variant: 'success',
      }),
    );
  });

  it('selects and saves the web search provider', async () => {
    const toastMock = vi.fn();
    rpcMock
      .mockResolvedValueOnce(createSettingsPayload())
      .mockImplementationOnce(async (_method, params) =>
        createSettingsPayload({
          web_search: {
            provider: params.web_search.provider,
            available_providers: ['brave', 'searxng'],
            searxng: params.web_search.searxng,
          },
        }),
      );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForText('0.0.0.0:9001');
    clickButton('Web Search');

    expect(document.body.textContent).toContain('Search provider');

    const trigger = document.body.querySelector(
      '#settings-web-search-provider',
    );
    expect(trigger).not.toBeNull();
    trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    const searxngOption = Array.from(
      document.body.querySelectorAll('.dropdown-option'),
    ).find((option) => option.textContent.trim() === 'SearXNG');
    expect(searxngOption).toBeTruthy();
    searxngOption.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    const baseUrlInput = document.body.querySelector(
      '#settings-web-search-searxng-base-url',
    );
    expect(baseUrlInput).not.toBeNull();
    baseUrlInput.value = 'http://localhost:9999';
    baseUrlInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    clickButton('Save');

    expect(rpcMock).toHaveBeenNthCalledWith(2, 'settings.update', {
      web_search: {
        provider: 'searxng',
        searxng: {
          base_url: 'http://localhost:9999',
        },
      },
    });

    await waitForCondition(() => toastMock.mock.calls.length > 0);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Web search settings updated.',
        variant: 'success',
      }),
    );
  });

  it('renders load failures and retries settings.get successfully', async () => {
    rpcMock
      .mockRejectedValueOnce(new Error('server offline'))
      .mockResolvedValueOnce(createSettingsPayload());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();

    await waitForText('Settings could not be loaded. server offline');

    expect(document.body.textContent).toContain(
      'Settings could not be loaded. server offline',
    );

    clickButton('Retry');

    expect(rpcMock).toHaveBeenNthCalledWith(1, 'settings.get');
    expect(rpcMock).toHaveBeenNthCalledWith(2, 'settings.get');

    await waitForText('0.0.0.0:9001');

    expect(document.body.textContent).toContain('0.0.0.0:9001');
    expect(document.body.textContent).not.toContain('server offline');
  });

  it('keeps appearance save enabled and persists language through settings.update', async () => {
    const toastMock = vi.fn();
    rpcMock
      .mockResolvedValueOnce(createSettingsPayload({ appearanceLanguage: '' }))
      .mockResolvedValueOnce(
        createSettingsPayload({
          appearance: {
            language: 'fr',
            available_languages: ['en', 'fr'],
          },
        }),
      );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();

    await waitForText('0.0.0.0:9001');

    clickButton('Appearance');

    const saveButton = getButton('Save');
    const languageTrigger = document.body.querySelector(
      '#settings-appearance-language',
    );

    expect(languageTrigger).not.toBeNull();
    expect(saveButton.disabled).toBe(false);

    saveButton.click();
    flushSync();

    expect(rpcMock).toHaveBeenCalledTimes(1);

    languageTrigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    const frenchOption = Array.from(
      document.body.querySelectorAll('.dropdown-option'),
    ).find((option) => option.textContent.trim() === 'fr');
    expect(frenchOption).toBeTruthy();
    frenchOption.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    getButton('Save').click();
    flushSync();

    expect(rpcMock).toHaveBeenNthCalledWith(1, 'settings.get');
    expect(rpcMock).toHaveBeenNthCalledWith(2, 'settings.update', {
      appearance: {
        language: 'fr',
        chat_width: 'comfortable',
      },
    });

    await waitForCondition(() => toastMock.mock.calls.length > 0);
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Appearance updated.',
        variant: 'success',
      }),
    );
    expect(document.body.textContent).not.toContain('Appearance updated.');
    expect(
      document
        .querySelector(
          '#settings-appearance-language .dropdown-primitive__trigger-label',
        )
        ?.textContent.trim(),
    ).toBe('fr');
    expect(saveButton.disabled).toBe(false);
  });
});

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
      searxng: {
        base_url: 'http://localhost:8888',
      },
    });
    expect(
      getWebSearchSettings({
        web_search: {
          provider: 'searxng',
          available_providers: ['brave', 'searxng'],
          searxng: {
            base_url: ' http://localhost:9999 ',
          },
        },
      }),
    ).toEqual({
      provider: 'searxng',
      available_providers: ['brave', 'searxng'],
      searxng: {
        base_url: 'http://localhost:9999',
      },
    });
    expect(
      buildWebSearchSettingsPayload({
        provider: 'searxng',
        searxng: {
          base_url: ' http://localhost:9999 ',
        },
      }),
    ).toEqual({
      web_search: {
        provider: 'searxng',
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

function createSettingsPayload(overrides = {}) {
  const base = {
    general: {
      server: {
        listen_host: '0.0.0.0',
        listen_port: 9001,
        port_source: 'settings.server_port',
      },
      data_directory: 'C:/Users/test/.vbot',
    },
    providers: {
      items: [
        {
          id: 'anthropic',
          name: 'Anthropic',
          base_url: 'https://api.anthropic.com/v1',
          credentials_configured: false,
          status: 'missing_credentials',
          model_count: 1,
          connections: [
            {
              id: 'anthropic:api-key',
              type: 'api_key',
              label: 'API Key',
              configured: false,
              credential_key: 'ANTHROPIC_API_KEY',
              accounts: [],
            },
          ],
        },
        {
          id: 'openai',
          name: 'OpenAI',
          base_url: 'https://api.openai.com/v1',
          credentials_configured: true,
          status: 'configured',
          model_count: 2,
          connections: [
            {
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
            },
          ],
        },
      ],
    },
    skills: {
      default_directory: 'C:/Users/test/.vbot/skills',
      directories: ['C:/skills/shared'],
    },
    appearance: {
      language: 'en',
      available_languages: ['en', 'fr'],
    },
    subagents: {
      max_subagent_depth: 4,
      max_subagents_per_turn: 8,
      subagent_timeout_minutes: 60,
    },
    recall: {
      backend: 'jsonl_scan',
      available_backends: ['jsonl_scan', 'sqlite_fts'],
    },
    web_search: {
      provider: 'brave',
      available_providers: ['brave', 'searxng'],
      searxng: {
        base_url: 'http://localhost:8888',
      },
    },
  };

  return mergeSettings(base, overrides);
}

function mergeSettings(base, overrides) {
  if (!overrides || typeof overrides !== 'object' || Array.isArray(overrides)) {
    return base;
  }

  const result = { ...base };

  for (const [key, value] of Object.entries(overrides)) {
    if (Array.isArray(value)) {
      result[key] = value;
      continue;
    }

    if (value && typeof value === 'object') {
      result[key] = mergeSettings(base[key] ?? {}, value);
      continue;
    }

    result[key] = value;
  }

  return result;
}

function getButton(label) {
  const button = Array.from(document.body.querySelectorAll('button')).find(
    (candidate) => candidate.textContent?.trim() === label,
  );

  if (!button) {
    throw new Error(`Button not found: ${label}`);
  }

  return button;
}

function clickButton(label) {
  getButton(label).click();
  flushSync();
}

async function waitForText(text, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    flushSync();

    if (document.body.textContent?.includes(text)) {
      return;
    }
  }

  throw new Error(`Timed out waiting for text: ${text}`);
}

async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    flushSync();

    if (check()) {
      return;
    }
  }

  throw new Error('Timed out waiting for condition.');
}

function translate(key, fallback, values) {
  const templates = {
    'common.unknown': 'Unknown',
    'settings.providers.description.credentialKey':
      'Credential key: {credentialKey}.',
    'settings.providers.description.baseUrl': 'Endpoint: {baseUrl}.',
    'settings.providers.description.modelCount': '{count} models available.',
    'settings.providers.description.none':
      'Provider metadata is not available yet.',
    'settings.recall.backends.jsonl_scan': 'JSONL scan',
    'settings.recall.backends.sqlite_fts': 'SQLite FTS',
    'settings.webSearch.providers.brave': 'Brave Search',
    'settings.webSearch.providers.searxng': 'SearXNG',
  };
  const template = templates[key] ?? fallback ?? key;

  if (!values) {
    return template;
  }

  return template.replace(/\{([A-Za-z0-9_]+)\}/g, (match, name) => {
    return Object.prototype.hasOwnProperty.call(values, name)
      ? String(values[name])
      : match;
  });
}

describe('comma decimal separators', () => {
  it('parses a comma temperature in agent defaults payloads', () => {
    expect(
      buildAgentDefaultsPayload({
        model: '',
        fallback_model: '',
        temperature: '0,7',
        thinking_effort: '',
      }).defaults.agent.temperature,
    ).toBe(0.7);
  });

  it('parses a comma threshold in compaction settings', () => {
    expect(
      normalizeCompactionSettings({ compaction: { threshold: '0,35' } })
        .threshold,
    ).toBe(0.35);
  });
});

describe('buildClientPresenceRows()', () => {
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
    {
      id: 'reg-2',
      connection_id: 'tab-other',
      accessor: 'desktop',
      browser: 'Unknown',
      os: 'Linux',
      connected_at: '2026-06-20T11:00:00+00:00',
      status: 'connected',
    },
  ];

  it('maps registry fields into display rows', () => {
    const rows = buildClientPresenceRows(roster, 'tab-self');

    expect(rows).toHaveLength(2);
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
  });

  it('flags only the row matching the own connection id', () => {
    const rows = buildClientPresenceRows(roster, 'tab-other');

    expect(rows.map((row) => row.isOwn)).toEqual([false, true]);
  });

  it('marks nothing when the own connection id is empty', () => {
    const rows = buildClientPresenceRows(roster, '');

    expect(rows.every((row) => row.isOwn === false)).toBe(true);
  });

  it('returns an empty list for a non-array roster', () => {
    expect(buildClientPresenceRows(null, 'tab-self')).toEqual([]);
    expect(buildClientPresenceRows(undefined, 'tab-self')).toEqual([]);
  });

  it('tolerates missing fields without throwing', () => {
    const rows = buildClientPresenceRows([{}], 'tab-self');

    expect(rows[0]).toEqual({
      id: '',
      connectionId: '',
      accessor: '',
      browser: '',
      os: '',
      connectedAt: '',
      status: '',
      isOwn: false,
    });
  });
});
