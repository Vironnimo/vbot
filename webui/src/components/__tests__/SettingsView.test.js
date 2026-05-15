// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  rpc: (...args) => rpcMock(...args),
}));

const { default: SettingsView } = await import('../SettingsView.svelte');

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

  it('shows one global refresh button when any provider appears refresh-eligible', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settings: settingsPayload({ includeSecondEligibleProvider: true }),
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    expect(buttonsByText('Update Model DB')).toHaveLength(1);
    expect(providerRow('OpenRouter').textContent).not.toContain(
      'Update Model DB',
    );
    expect(providerRow('Groq').textContent).not.toContain('Update Model DB');
  });

  it('hides the global refresh button when no provider appears refresh-eligible', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settings: settingsPayload({ eligibleProvider: false }),
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    expect(buttonsByText('Update Model DB')).toHaveLength(0);
  });

  it('refreshes the global model database, shows loading and success, and reloads model list', async () => {
    let resolveRefresh;
    const refreshPromise = new Promise((resolve) => {
      resolveRefresh = resolve;
    });
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settings: settingsPayload({ includeSecondEligibleProvider: true }),
        refreshResult: refreshPromise,
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    buttonByText('Update Model DB').click();
    flushSync();

    expect(buttonByText('Updating…')).toBeTruthy();
    expect(rpcMock).toHaveBeenCalledWith('model.refresh_db');
    expect(
      rpcMock.mock.calls.some(
        (call) => call[0] === 'model.refresh_db' && call[1]?.provider_id,
      ),
    ).toBe(false);

    resolveRefresh({
      providers: [
        {
          provider_id: 'openrouter',
          model_count: 2,
          fetched_at: '2026-05-08T19:08:00+00:00',
        },
        {
          provider_id: 'groq',
          model_count: 3,
          fetched_at: '2026-05-08T19:08:00+00:00',
        },
      ],
      refreshed_count: 2,
      model_count: 5,
    });
    await waitForCondition(() =>
      document.body.textContent.includes('5 models'),
    );

    expect(document.body.textContent).toContain(
      'Model DB updated: 2 providers, 5 models available.',
    );
    expect(providerRow('OpenRouter').textContent).toContain(
      '2 models available.',
    );
    expect(providerRow('Groq').textContent).toContain('3 models available.');
    expect(rpcMock.mock.calls.some((call) => call[0] === 'model.list')).toBe(
      true,
    );
  });

  it('updates provider counts from the compatible single-provider refresh shape', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        refreshResult: {
          provider_id: 'openrouter',
          model_count: 2,
          fetched_at: '2026-05-08T19:08:00+00:00',
        },
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    buttonByText('Update Model DB').click();
    await waitForCondition(() =>
      document.body.textContent.includes('2 models'),
    );

    expect(document.body.textContent).toContain(
      'Model DB updated: 1 providers, 2 models available.',
    );
    expect(rpcMock.mock.calls.some((call) => call[0] === 'model.list')).toBe(
      true,
    );
  });

  it('shows refresh errors and skips model list reload on failure', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        refreshError: new Error('fetch failed'),
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    buttonByText('Update Model DB').click();
    await waitForCondition(() =>
      document.body.textContent.includes('fetch failed'),
    );

    expect(document.body.textContent).toContain(
      'Model DB could not be updated. fetch failed',
    );
    expect(rpcMock.mock.calls.some((call) => call[0] === 'model.list')).toBe(
      false,
    );
  });

  it('loads channels panel and resolves running status for each channel', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        channels: [
          channelConfig('tg-assistant', {
            agent_id: 'assistant',
            enabled: true,
            dm_scope: 'per_conversation',
          }),
          channelConfig('tg-work', {
            agent_id: 'assistant-work',
            enabled: false,
            dm_scope: 'main',
          }),
        ],
        channelStatuses: {
          'tg-assistant': { running: true, enabled: true },
          'tg-work': { running: false, enabled: false },
        },
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openChannelsPanel();

    await waitForCondition(() => document.body.textContent.includes('tg-work'));

    expect(document.body.textContent).toContain('tg-assistant');
    expect(document.body.textContent).toContain('tg-work');
    expect(rpcMock).toHaveBeenCalledWith('channel.list');
    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'channel.status' && call[1]?.id === 'tg-assistant',
      ),
    ).toBe(true);
    expect(
      rpcMock.mock.calls.some(
        (call) => call[0] === 'channel.status' && call[1]?.id === 'tg-work',
      ),
    ).toBe(true);
  });

  it('creates a channel from the inline form', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        channels: [],
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openChannelsPanel();

    buttonByText('Add channel').click();
    flushSync();

    setInputValue('#channel-id-input', 'tg-new');
    setSelectValue('#channel-agent-select', 'assistant');
    setSelectValue('#channel-dm-scope-select', 'main');
    setInputValue('#channel-token-env-input', 'TELEGRAM_BOT_TOKEN_TG_NEW');
    setInputValue('#channel-allowed-chat-ids-input', '12345, -100123');

    submitChannelForm();

    await waitForCondition(() => document.body.textContent.includes('tg-new'));

    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'channel.create' &&
          call[1]?.id === 'tg-new' &&
          call[1]?.platform === 'telegram' &&
          call[1]?.agent_id === 'assistant' &&
          call[1]?.dm_scope === 'main' &&
          call[1]?.token_env_var === 'TELEGRAM_BOT_TOKEN_TG_NEW' &&
          JSON.stringify(call[1]?.allowed_chat_ids) ===
            JSON.stringify([12345, -100123]),
      ),
    ).toBe(true);
  });

  it('updates, toggles, and deletes channels from row actions', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        channels: [
          channelConfig('tg-assistant', {
            agent_id: 'assistant',
            enabled: true,
          }),
        ],
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openChannelsPanel();

    await waitForCondition(() =>
      document.body.textContent.includes('tg-assistant'),
    );

    buttonByAriaLabel('Edit channel tg-assistant').click();
    flushSync();

    setInputValue('#channel-token-env-input', 'TELEGRAM_BOT_TOKEN_UPDATED');
    submitChannelForm();

    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'channel.update'),
    );

    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'channel.update' &&
          call[1]?.id === 'tg-assistant' &&
          call[1]?.token_env_var === 'TELEGRAM_BOT_TOKEN_UPDATED',
      ),
    ).toBe(true);

    buttonByAriaLabel('Disable channel tg-assistant').click();
    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'channel.disable'),
    );

    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'channel.disable' && call[1]?.id === 'tg-assistant',
      ),
    ).toBe(true);

    buttonByAriaLabel('Delete channel tg-assistant').click();
    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'channel.delete'),
    );

    expect(confirmSpy).toHaveBeenCalled();
    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'channel.delete' && call[1]?.id === 'tg-assistant',
      ),
    ).toBe(true);

    confirmSpy.mockRestore();
  });
});

async function openProvidersPanel() {
  await waitForCondition(() => buttonByText('Providers'));
  buttonByText('Providers').click();
  flushSync();
  await waitForCondition(() =>
    document.body.textContent.includes('OpenRouter'),
  );
}

async function openChannelsPanel() {
  await waitForCondition(() => buttonByText('Channels'));
  buttonByText('Channels').click();
  flushSync();
  await waitForCondition(() => buttonByText('Add channel'));
  await waitForCondition(() => buttonByText('Add channel')?.disabled === false);
}

function providerRow(providerName) {
  const rows = Array.from(document.body.querySelectorAll('.s-row'));
  const row = rows.find((item) => item.textContent.includes(providerName));
  expect(row).toBeTruthy();
  return row;
}

function buttonByText(label) {
  return Array.from(document.body.querySelectorAll('button')).find(
    (button) => button.textContent.trim() === label,
  );
}

function buttonByAriaLabel(label) {
  return Array.from(document.body.querySelectorAll('button')).find(
    (button) => button.getAttribute('aria-label') === label,
  );
}

function buttonsByText(label) {
  return Array.from(document.body.querySelectorAll('button')).filter(
    (button) => button.textContent.trim() === label,
  );
}

function setInputValue(selector, value) {
  const input = document.body.querySelector(selector);
  expect(input).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function setSelectValue(selector, value) {
  const select = document.body.querySelector(selector);
  expect(select).toBeTruthy();
  select.value = value;
  select.dispatchEvent(new Event('change', { bubbles: true }));
  flushSync();
}

function submitChannelForm() {
  const form = document.body.querySelector('.s-channel-form');
  expect(form).toBeTruthy();
  form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  flushSync();
}

function createSettingsRpcMock(options = {}) {
  const channels = Array.isArray(options.channels)
    ? options.channels.map((item) => ({
        ...item,
        allowed_chat_ids: Array.isArray(item.allowed_chat_ids)
          ? [...item.allowed_chat_ids]
          : [],
      }))
    : [];
  const agents = Array.isArray(options.agents)
    ? options.agents
    : agentsPayload();
  const statusSource =
    options.channelStatuses !== null &&
    typeof options.channelStatuses === 'object'
      ? options.channelStatuses
      : {};
  const channelStatuses = new Map(
    channels.map((channel) => {
      const providedStatus = statusSource[channel.id] ?? {};

      return [
        channel.id,
        {
          id: channel.id,
          enabled:
            typeof providedStatus.enabled === 'boolean'
              ? providedStatus.enabled
              : channel.enabled !== false,
          running:
            typeof providedStatus.running === 'boolean'
              ? providedStatus.running
              : false,
        },
      ];
    }),
  );

  return async (method, params = {}) => {
    if (method === 'settings.get') {
      return options.settings ?? settingsPayload();
    }

    if (method === 'model.refresh_db') {
      if (options.refreshError) {
        throw options.refreshError;
      }

      return options.refreshResult ?? refreshResult();
    }

    if (method === 'model.list') {
      return { models: [{ id: 'openrouter/fresh-model' }] };
    }

    if (method === 'agent.list') {
      return { agents };
    }

    if (method === 'channel.list') {
      return {
        channels: channels.map((channel) => ({
          ...channel,
          allowed_chat_ids: [...channel.allowed_chat_ids],
        })),
      };
    }

    if (method === 'channel.status') {
      const status = channelStatuses.get(params.id);
      if (!status) {
        throw new Error(`Unknown channel status id: ${params.id}`);
      }

      return {
        id: status.id,
        enabled: status.enabled,
        running: status.running,
      };
    }

    if (method === 'channel.create') {
      const channel = channelConfig(params.id, {
        platform: params.platform,
        agent_id: params.agent_id,
        dm_scope: params.dm_scope,
        allowed_chat_ids: params.allowed_chat_ids,
        token_env_var: params.token_env_var,
        enabled: params.enabled,
      });

      channels.push(channel);
      channelStatuses.set(channel.id, {
        id: channel.id,
        enabled: channel.enabled,
        running: false,
      });
      return { id: channel.id };
    }

    if (method === 'channel.update') {
      const index = channels.findIndex((item) => item.id === params.id);
      if (index < 0) {
        throw new Error(`Unknown channel id: ${params.id}`);
      }

      channels[index] = {
        ...channels[index],
        ...params,
        allowed_chat_ids: Array.isArray(params.allowed_chat_ids)
          ? [...params.allowed_chat_ids]
          : channels[index].allowed_chat_ids,
      };

      const status = channelStatuses.get(params.id) ?? {
        id: params.id,
        enabled: channels[index].enabled,
        running: false,
      };
      status.enabled = channels[index].enabled;
      channelStatuses.set(params.id, status);

      return { ok: true };
    }

    if (method === 'channel.enable' || method === 'channel.disable') {
      const enabled = method === 'channel.enable';
      const index = channels.findIndex((item) => item.id === params.id);
      if (index < 0) {
        throw new Error(`Unknown channel id: ${params.id}`);
      }

      channels[index] = { ...channels[index], enabled };
      const status = channelStatuses.get(params.id) ?? {
        id: params.id,
        enabled,
        running: false,
      };
      status.enabled = enabled;
      channelStatuses.set(params.id, status);

      return { ok: true };
    }

    if (method === 'channel.delete') {
      const index = channels.findIndex((item) => item.id === params.id);
      if (index < 0) {
        throw new Error(`Unknown channel id: ${params.id}`);
      }

      channels.splice(index, 1);
      channelStatuses.delete(params.id);
      return { ok: true };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

function channelConfig(id, overrides = {}) {
  return {
    id,
    platform: 'telegram',
    agent_id: 'assistant',
    dm_scope: 'per_conversation',
    allowed_chat_ids: [12345],
    token_env_var: `TELEGRAM_BOT_TOKEN_${id.toUpperCase().replace(/-/gu, '_')}`,
    enabled: true,
    ...overrides,
  };
}

function agentsPayload() {
  return [
    {
      id: 'assistant',
      name: 'Assistant',
    },
    {
      id: 'assistant-work',
      name: 'Assistant Work',
    },
  ];
}

function settingsPayload(options = {}) {
  const openrouter = provider('openrouter', 'OpenRouter', '/models');

  if (options.eligibleProvider === false) {
    openrouter.credentials_configured = false;
    openrouter.status = 'missing_credentials';
  }

  const providers = [openrouter, provider('openai', 'OpenAI', null)];

  if (options.includeSecondEligibleProvider) {
    providers.push(provider('groq', 'Groq', '/models'));
  }

  return {
    general: {
      server: {
        listen_host: '127.0.0.1',
        listen_port: 8420,
        port_source: 'default',
      },
      data_directory: 'C:/data',
    },
    providers: {
      items: providers,
      custom_endpoints: { supported: false, items: [] },
    },
    skills: {
      default_directory: 'C:/data/skills',
      directories: [],
    },
    appearance: {
      language: 'en',
      available_languages: ['en'],
    },
  };
}

function provider(id, name, modelsEndpoint) {
  return {
    id,
    name,
    base_url: `https://${id}.example.test`,
    models_endpoint: modelsEndpoint,
    connections: [],
    credentials_configured: true,
    status: 'configured',
    model_count: 1,
    kind: 'remote',
    editable: false,
  };
}

function refreshResult() {
  return {
    providers: [
      {
        provider_id: 'openrouter',
        model_count: 2,
        fetched_at: '2026-05-08T19:08:00+00:00',
      },
    ],
    refreshed_count: 1,
    model_count: 2,
  };
}

async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();

    if (check()) {
      return;
    }
  }

  throw new Error('Timed out waiting for condition.');
}
