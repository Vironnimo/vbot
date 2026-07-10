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
  listClients: () => Promise.resolve({ clients: [] }),
  getTaskModelOptions: (taskType, target) =>
    rpcMock('task_model.options', { task_type: taskType, target }),
  listTaskModelTargets: (taskType) =>
    rpcMock('task_model.list_targets', { task_type: taskType }),
  updateTaskModelSettings: (modelTasks) =>
    rpcMock('task_model.update', { model_tasks: modelTasks }),
}));

const { default: SettingsView } = await import('../SettingsView.svelte');

describe('SettingsView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    window.history.pushState({}, '', '/');
    delete window.pywebview;
    init('en');
    rpcMock.mockReset();
    mountedComponent = null;
    activeSection = null;
  });

  afterEach(async () => {
    vi.useRealTimers();

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

  it('refreshes the global model database, toasts success, and reloads model list', async () => {
    const toastMock = vi.fn();
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

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
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
    // The refresh result is now a success toast (auto-dismiss), not inline
    // text — an inline result would flash and vanish when the settings reload
    // briefly unmounts the panel.
    await waitForCondition(() =>
      toastMock.mock.calls.some(
        ([toast]) =>
          toast?.variant === 'success' &&
          toast?.title === 'Model DB updated: 2 providers, 5 models available.',
      ),
    );

    // The updated per-provider model counts still land in the provider rows.
    await waitForCondition(() =>
      providerRow('OpenRouter').textContent.includes('2 models available.'),
    );
    expect(providerRow('Groq').textContent).toContain('3 models available.');
    expect(rpcMock.mock.calls.some((call) => call[0] === 'model.list')).toBe(
      true,
    );
  });

  it('toasts the compatible single-provider refresh result and updates counts', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        refreshResult: {
          provider_id: 'openrouter',
          model_count: 2,
          fetched_at: '2026-05-08T19:08:00+00:00',
        },
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();
    await openProvidersPanel();

    buttonByText('Update Model DB').click();
    await waitForCondition(() =>
      toastMock.mock.calls.some(
        ([toast]) =>
          toast?.variant === 'success' &&
          toast?.title === 'Model DB updated: 1 providers, 2 models available.',
      ),
    );

    expect(rpcMock.mock.calls.some((call) => call[0] === 'model.list')).toBe(
      true,
    );
  });

  it('toasts refresh errors and skips model list reload on failure', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        refreshError: new Error('fetch failed'),
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();
    await openProvidersPanel();

    // Other always-mounted sections (Defaults, Compaction) load the model
    // catalog on mount; only the refresh-triggered reload must be absent.
    const modelListCallsBeforeRefresh = rpcMock.mock.calls.filter(
      (call) => call[0] === 'model.list',
    ).length;

    buttonByText('Update Model DB').click();
    // The failure is a sticky error toast (routed through the unified settings
    // error seam), carrying the server detail as the toast message.
    await waitForCondition(() =>
      toastMock.mock.calls.some(
        ([toast]) =>
          toast?.variant === 'error' &&
          toast?.message === 'Model DB could not be updated. fetch failed',
      ),
    );

    expect(
      rpcMock.mock.calls.filter((call) => call[0] === 'model.list').length,
    ).toBe(modelListCallsBeforeRefresh);
  });

  it('renders a keyless connection and the local model context editor', async () => {
    const ollama = {
      id: 'ollama',
      name: 'Ollama',
      base_url: 'http://localhost:11434',
      models_endpoint: '/api/tags',
      connections: [
        {
          id: 'ollama:local',
          type: 'none',
          label: 'Local',
          configured: true,
          accounts: [{ id: 'default', usable: true, source: 'none' }],
        },
      ],
      credentials_configured: true,
      status: 'configured',
      model_count: 1,
      kind: 'remote',
      editable: false,
    };
    const settings = settingsPayload();
    settings.providers.items.push(ollama);
    settings.local_models = { context_windows: {} };
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settings,
        models: [
          {
            id: 'ollama/ministral-3:8b',
            provider_id: 'ollama',
            model_id: 'ministral-3:8b',
            name: 'ministral-3:8b',
            capabilities: { tools: true },
            context_window: 262144,
            effective_context_window: 32768,
            local: true,
          },
        ],
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openProvidersPanel();

    // Keyless connection: descriptive text, no key management actions.
    await waitForCondition(() =>
      document.body.textContent.includes('No key required'),
    );
    expect(providerRow('Ollama').textContent).not.toContain('Replace key');

    // The local-context editor lists the flagged-local model.
    await waitForCondition(() =>
      document.body.textContent.includes('Local model context'),
    );
    const input = document.body.querySelector('.s-local-context-input');
    expect(input).toBeTruthy();
    expect(input.placeholder).toBe('32768');

    // Setting a value writes the sparse local_models update.
    input.value = '16384';
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await waitForCondition(() => getSettingsUpdateCalls().length >= 1);
    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      local_models: {
        context_windows: { 'ollama/ministral-3:8b': 16384 },
      },
    });
  });

  it('renders and saves the Defaults section', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openDefaultsPanel();

    expect(document.body.textContent).toContain('Model');
    expect(document.body.textContent).toContain('Fallback model');
    expect(document.body.textContent).toContain('Temperature');
    expect(document.body.textContent).toContain('Thinking effort');

    expect(buttonsByText('Clear')).toHaveLength(0);

    await waitForModelCatalogs();

    await openSearchableDropdown('settings-defaults-model');
    selectSearchableOption('settings-defaults-model', 'openai/gpt-5.2');

    await openSearchableDropdown('settings-defaults-fallback-model');
    selectSearchableOption(
      'settings-defaults-fallback-model',
      'openai/gpt-5.2-mini',
    );

    setInputValue('#settings-defaults-temperature', '0.7');
    openSimpleDropdown('settings-defaults-thinking-effort');
    selectSimpleOption('settings-defaults-thinking-effort', 'high');

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 1);

    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      defaults: {
        agent: {
          model: 'openai/gpt-5.2::api-key',
          fallback_model: 'openai/gpt-5.2-mini::api-key',
          temperature: 0.7,
          thinking_effort: 'high',
        },
      },
    });

    await openSearchableDropdown('settings-defaults-model');
    selectSearchableOption('settings-defaults-model', '— (no default)');

    await openSearchableDropdown('settings-defaults-fallback-model');
    selectSearchableOption(
      'settings-defaults-fallback-model',
      '— (no default)',
    );

    setInputValue('#settings-defaults-temperature', '');

    openSimpleDropdown('settings-defaults-thinking-effort');
    selectSimpleOption('settings-defaults-thinking-effort', '— (no default)');

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 2);

    expect(getSettingsUpdateCalls()[1][1]).toEqual({
      defaults: {
        agent: {
          model: null,
          fallback_model: null,
          temperature: null,
          thinking_effort: null,
        },
      },
    });

    openSimpleDropdown('settings-defaults-thinking-effort');
    selectSimpleOption(
      'settings-defaults-thinking-effort',
      '— (provider default)',
    );

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 3);

    expect(getSettingsUpdateCalls()[2][1]).toEqual({
      defaults: {
        agent: {
          model: null,
          fallback_model: null,
          temperature: null,
          thinking_effort: '',
        },
      },
    });
  });

  it('uses the model picker for compaction summary model', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openCompactionPanel();
    await waitForModelCatalogs();

    await openSearchableDropdown('settings-compaction-summary-model');
    selectSearchableOption(
      'settings-compaction-summary-model',
      'openai/gpt-5.2-mini',
    );

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 1);

    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      compaction: {
        enabled: true,
        trigger: { type: 'context_ratio', threshold: 0.8 },
        strategy: {
          type: 'summary_tail',
          tail_tokens: 15000,
          summary_model: 'openai/gpt-5.2-mini::api-key',
        },
      },
    });

    await openSearchableDropdown('settings-compaction-summary-model');
    selectSearchableOption(
      'settings-compaction-summary-model',
      'Active agent model',
    );

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 2);

    expect(getSettingsUpdateCalls()[1][1]).toEqual({
      compaction: {
        enabled: true,
        trigger: { type: 'context_ratio', threshold: 0.8 },
        strategy: {
          type: 'summary_tail',
          tail_tokens: 15000,
          summary_model: null,
        },
      },
    });
  });

  it('renders and saves the Recall backend dropdown', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openRecallPanel();

    expect(document.body.textContent).toContain('Recall backend');
    expect(getSimpleTrigger('settings-recall-backend').textContent).toContain(
      'Simple scan — exact keyword match, no index',
    );

    openSimpleDropdown('settings-recall-backend');
    selectSimpleOption(
      'settings-recall-backend',
      'Full-text search — fast keyword search with an index',
    );

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 1);

    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      recall: {
        backend: 'sqlite_fts',
      },
    });
  });

  it('renders and saves the Web Search provider settings', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openWebSearchPanel();

    expect(document.body.textContent).toContain('Search provider');
    expect(
      getSimpleTrigger('settings-web-search-provider').textContent,
    ).toContain('Brave Search');

    openSimpleDropdown('settings-web-search-provider');
    selectSimpleOption('settings-web-search-provider', 'SearXNG');

    const baseUrlInput = document.body.querySelector(
      '#settings-web-search-searxng-base-url',
    );
    expect(baseUrlInput).not.toBeNull();
    baseUrlInput.value = 'http://localhost:9999';
    baseUrlInput.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 1);

    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      web_search: {
        provider: 'searxng',
        default_count: 12,
        searxng: {
          base_url: 'http://localhost:9999',
        },
      },
    });
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

  it('lists denied chats and allows one from the channel card', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        channels: [
          channelConfig('tg-assistant', {
            agent_id: 'assistant',
            enabled: true,
            allowed_chat_ids: [12345],
          }),
        ],
        channelStatuses: {
          'tg-assistant': {
            running: true,
            enabled: true,
            denied_chats: [
              {
                chat_id: '99999',
                kind: 'direct',
                display_name: 'Julian B.',
                last_seen_at: '2026-07-05T12:00:00+00:00',
                count: 3,
              },
            ],
          },
        },
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openChannelsPanel();

    await waitForCondition(() =>
      document.body.textContent.includes('Julian B.'),
    );
    expect(document.body.textContent).toContain(
      'Recent requests from chats not on the allowlist',
    );
    expect(document.body.textContent).toContain('ID 99999');

    buttonByAriaLabel('Allow chat 99999').click();

    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'channel.update'),
    );

    const updateCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'channel.update',
    );
    expect(updateCall[1]).toEqual({
      id: 'tg-assistant',
      allowed_chat_ids: ['12345', '99999'],
    });
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
    openSimpleDropdown('channel-agent-select');
    selectSimpleOption('channel-agent-select', 'Assistant');
    openSimpleDropdown('channel-dm-scope-select');
    selectSimpleOption('channel-dm-scope-select', 'Main');
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
    flushSync();

    // The row action opens the shared ConfirmDialog; the delete RPC only fires
    // once it is confirmed.
    expect(
      rpcMock.mock.calls.some((call) => call[0] === 'channel.delete'),
    ).toBe(false);
    confirmChannelDialog('Delete');

    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'channel.delete'),
    );

    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'channel.delete' && call[1]?.id === 'tg-assistant',
      ),
    ).toBe(true);
  });

  it('auto-saves sub-agent settings 800 ms after the last change', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openSubAgentsPanel();

    vi.useFakeTimers();

    setInputValue('input[aria-label="Max sub-agent depth"]', '6');

    expect(getSettingsUpdateCalls()).toHaveLength(0);

    vi.advanceTimersByTime(799);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(0);

    vi.advanceTimersByTime(1);
    await flushAsyncUpdates();

    expect(getSettingsUpdateCalls()).toHaveLength(1);
    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      subagents: {
        max_subagent_depth: 6,
        max_subagents_per_turn: 8,
        subagent_timeout_minutes: 60,
      },
    });
  });

  it('manual save cancels a pending debounce timer', async () => {
    let resolveFirstUpdate;
    let settingsUpdateCallCount = 0;

    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settingsUpdate: async () => {
          settingsUpdateCallCount += 1;

          if (settingsUpdateCallCount === 1) {
            await new Promise((resolve) => {
              resolveFirstUpdate = resolve;
            });
          }

          return null;
        },
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openSubAgentsPanel();

    vi.useFakeTimers();

    setInputValue('input[aria-label="Max sub-agent depth"]', '6');

    getButton('Save').click();
    flushSync();

    expect(getSettingsUpdateCalls()).toHaveLength(1);

    setInputValue('input[aria-label="Max sub-agent depth"]', '7');

    vi.advanceTimersByTime(799);
    await flushAsyncUpdates();

    resolveFirstUpdate();
    await flushAsyncUpdates();

    vi.advanceTimersByTime(1);
    await flushAsyncUpdates();

    expect(getSettingsUpdateCalls()).toHaveLength(1);

    vi.advanceTimersByTime(799);
    await flushAsyncUpdates();
    expect(getSettingsUpdateCalls()).toHaveLength(2);
    expect(getSettingsUpdateCalls()[1][1]).toEqual({
      subagents: {
        max_subagent_depth: 7,
        max_subagents_per_turn: 8,
        subagent_timeout_minutes: 60,
      },
    });
  });

  it('shows Already saved when manual save is clicked with no changes', async () => {
    const toastMock = vi.fn();
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: { onToast: toastMock },
    });
    flushSync();
    await openSubAgentsPanel();

    getButton('Save').click();
    flushSync();

    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Already saved', variant: 'success' }),
    );
    expect(document.body.textContent).not.toContain('Already saved');
    expect(getSettingsUpdateCalls()).toHaveLength(0);
  });

  it('hides the Voice panel outside Desktop wakeword capabilities', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await waitForCondition(() => buttonByText('Appearance'));

    expect(buttonByText('Voice')).toBeUndefined();
  });

  it('highlights the Voice section once for a target panel request', async () => {
    rpcMock.mockImplementation(createSettingsRpcMock());
    window.history.pushState({}, '', '/?accessor=desktop');
    window.pywebview = {
      api: {
        getWakewordStatus: vi.fn().mockResolvedValue({
          enabled: false,
          state: 'off',
        }),
      },
    };

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: {
        agents: agentsPayload(),
        desktopCapabilities: { wakeword: true },
        targetPanelId: 'voice',
        targetPanelRequestId: 1,
      },
    });
    flushSync();

    // The Voice section renders (desktop capability) and the target request
    // marks its index entry active.
    await waitForCondition(() =>
      document.body.textContent.includes('Wakeword listening'),
    );
    await waitForCondition(
      () =>
        buttonByText('Voice')?.classList.contains('snav-item--active') === true,
    );

    // Navigating elsewhere moves the index highlight; the Voice section stays
    // in the document (sections are never unmounted).
    buttonByText('Server info').click();
    flushSync();

    await waitForCondition(
      () =>
        buttonByText('Server info')?.classList.contains('snav-item--active') ===
        true,
    );
    expect(buttonByText('Voice')?.classList.contains('snav-item--active')).toBe(
      false,
    );
    expect(document.body.textContent).toContain('Wakeword listening');
    expect(document.body.textContent).toContain('Server host');
  });

  it('keeps in-progress values while an auto-save request is in flight', async () => {
    let resolveFirstUpdate;
    let settingsUpdateCallCount = 0;

    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settingsUpdate: async () => {
          settingsUpdateCallCount += 1;

          if (settingsUpdateCallCount === 1) {
            await new Promise((resolve) => {
              resolveFirstUpdate = resolve;
            });
          }

          return null;
        },
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openSubAgentsPanel();

    vi.useFakeTimers();

    setInputValue('input[aria-label="Max sub-agent depth"]', '6');

    vi.advanceTimersByTime(800);
    await flushAsyncUpdates();

    expect(getSettingsUpdateCalls()).toHaveLength(1);

    setInputValue('input[aria-label="Max sub-agent depth"]', '7');

    resolveFirstUpdate();
    await flushAsyncUpdates();

    const depthInput = document.body.querySelector(
      'input[aria-label="Max sub-agent depth"]',
    );
    expect(depthInput).toBeTruthy();
    expect(depthInput.value).toBe('7');

    vi.advanceTimersByTime(800);
    await flushAsyncUpdates();

    expect(getSettingsUpdateCalls()).toHaveLength(2);
    expect(getSettingsUpdateCalls()[1][1]).toEqual({
      subagents: {
        max_subagent_depth: 7,
        max_subagents_per_turn: 8,
        subagent_timeout_minutes: 60,
      },
    });
  });

  it('renders a json option field and stores the parsed structure on valid input', async () => {
    const target = 'openrouter/recraft/recraft-v3::api-key';
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        taskModelTargets: [
          {
            id: target,
            kind: 'provider',
            provider_id: 'openrouter',
            model_id: 'recraft/recraft-v3',
            connection_id: 'openrouter:api-key',
            connection_label: 'API Key',
            label: 'Recraft v3',
            task_types: ['image_generation'],
            usable: true,
          },
        ],
        taskModelOptions: {
          [target]: {
            schema: {
              task_type: 'image_generation',
              target,
              fields: [
                {
                  name: 'text_layout',
                  type: 'json',
                  label: 'Text layout',
                  default: [],
                  description: 'Array of {text, bbox} entries (recraft-v3).',
                },
              ],
            },
          },
        },
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: {
        targetPanelId: 'specialized_models',
        targetPanelRequestId: 1,
      },
    });
    flushSync();
    await openSpecializedModelsPanel();

    openSimpleDropdown('settings-specialized-image_generation');
    await waitForCondition(() => getSimpleList() !== null);
    selectSimpleOption('settings-specialized-image_generation', 'Recraft v3');

    await waitForCondition(
      () => document.body.querySelector('.text-area--code') !== null,
    );

    const jsonTextarea = document.body.querySelector('.text-area--code');
    expect(jsonTextarea).toBeTruthy();
    // Default empty array serializes to "[]" — confirms the renderer
    // stringifies structured defaults rather than treating them as text.
    expect(jsonTextarea.value).toBe('[]');
    expect(jsonTextarea.getAttribute('aria-invalid')).toBe('false');

    const validText = JSON.stringify([
      {
        text: 'hi',
        bbox: [
          [0, 0],
          [1, 0],
          [1, 1],
          [0, 1],
        ],
      },
    ]);
    setTextareaValue('.text-area--code', validText);

    await waitForCondition(
      () =>
        document.body.querySelector('.form-field__error') === null &&
        jsonTextarea.getAttribute('aria-invalid') === 'false',
    );

    // Save and inspect what reached the settings update — must be the
    // parsed array, not the raw JSON string.
    vi.useFakeTimers();
    getButton('Save').click();
    vi.advanceTimersByTime(1);
    await flushAsyncUpdates();
    vi.useRealTimers();

    await waitForCondition(() =>
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'task_model.update' &&
          call[1]?.model_tasks?.image_generation?.options?.text_layout !==
            undefined,
      ),
    );

    const updateCall = rpcMock.mock.calls
      .filter((call) => call[0] === 'task_model.update')
      .pop();
    expect(updateCall[1].model_tasks.image_generation.target).toBe(target);
    expect(
      updateCall[1].model_tasks.image_generation.options.text_layout,
    ).toEqual([
      {
        text: 'hi',
        bbox: [
          [0, 0],
          [1, 0],
          [1, 1],
          [0, 1],
        ],
      },
    ]);
  });

  it('shows an inline parse error for invalid JSON and does not update the binding', async () => {
    const target = 'openrouter/recraft/recraft-v3::api-key';
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        taskModelTargets: [
          {
            id: target,
            kind: 'provider',
            provider_id: 'openrouter',
            model_id: 'recraft/recraft-v3',
            connection_id: 'openrouter:api-key',
            connection_label: 'API Key',
            label: 'Recraft v3',
            task_types: ['image_generation'],
            usable: true,
          },
        ],
        taskModelOptions: {
          [target]: {
            schema: {
              task_type: 'image_generation',
              target,
              fields: [
                {
                  name: 'text_layout',
                  type: 'json',
                  label: 'Text layout',
                  default: [],
                },
              ],
            },
          },
        },
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: {
        targetPanelId: 'specialized_models',
        targetPanelRequestId: 1,
      },
    });
    flushSync();
    await openSpecializedModelsPanel();

    openSimpleDropdown('settings-specialized-image_generation');
    await waitForCondition(() => getSimpleList() !== null);
    selectSimpleOption('settings-specialized-image_generation', 'Recraft v3');

    await waitForCondition(
      () => document.body.querySelector('.text-area--code') !== null,
    );

    setTextareaValue('.text-area--code', '[{"text": "hi"');

    await waitForCondition(
      () => document.body.querySelector('.form-field__error') !== null,
    );

    const jsonTextarea = document.body.querySelector('.text-area--code');
    expect(jsonTextarea.getAttribute('aria-invalid')).toBe('true');
    expect(document.body.querySelector('.form-field__error')).toBeTruthy();
    expect(document.body.textContent).toContain('Invalid JSON');

    // The parse error means the typed text was NOT applied to the
    // binding — saving now persists only the default `[]` (from the
    // schema), not the malformed string the user typed.
    vi.useFakeTimers();
    getButton('Save').click();
    vi.advanceTimersByTime(1);
    await flushAsyncUpdates();
    vi.useRealTimers();

    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'task_model.update'),
    );

    const updateCall = rpcMock.mock.calls
      .filter((call) => call[0] === 'task_model.update')
      .pop();
    expect(updateCall[1].model_tasks.image_generation.target).toBe(target);
    // The options either contain the parsed default (empty array) or no
    // text_layout key at all — never the malformed input string.
    const savedTextLayout =
      updateCall[1].model_tasks.image_generation.options?.text_layout;
    expect(savedTextLayout).not.toBe('[{"text": "hi"');
    expect(
      savedTextLayout === undefined || Array.isArray(savedTextLayout),
    ).toBe(true);

    // Repairing the input clears the error and lets the binding update.
    setTextareaValue(
      '.text-area--code',
      JSON.stringify([
        {
          text: 'repaired',
          bbox: [
            [0, 0],
            [1, 1],
          ],
        },
      ]),
    );

    await waitForCondition(
      () => document.body.querySelector('.form-field__error') === null,
    );
    expect(jsonTextarea.getAttribute('aria-invalid')).toBe('false');
  });

  it('renders the Recall picker with the vector backend when available', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        settings: {
          ...settingsPayload(),
          recall: {
            backend: 'jsonl_scan',
            available_backends: ['jsonl_scan', 'sqlite_fts', 'vector'],
          },
        },
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openRecallPanel();

    expect(document.body.textContent).toContain('Recall backend');
    openSimpleDropdown('settings-recall-backend');
    await waitForCondition(() => getSimpleList() !== null);
    selectSimpleOption(
      'settings-recall-backend',
      'Semantic — finds matches by meaning, needs an embedding model',
    );

    getButton('Save').click();
    await waitForCondition(() => getSettingsUpdateCalls().length >= 1);

    expect(getSettingsUpdateCalls()[0][1]).toEqual({
      recall: {
        backend: 'vector',
      },
    });
  });

  it('renders and saves the embedding model row in the Specialized Models panel', async () => {
    const target = 'openrouter/google/gemini-embedding-2::api-key';
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        taskModelTargets: [
          {
            id: target,
            kind: 'provider',
            provider_id: 'openrouter',
            model_id: 'google/gemini-embedding-2',
            connection_id: 'openrouter:api-key',
            connection_label: 'API Key',
            label: 'Gemini Embedding 2',
            task_types: ['text_embedding'],
            usable: true,
          },
        ],
        taskModelOptions: {
          [target]: {
            schema: {
              task_type: 'text_embedding',
              target,
              fields: [],
            },
          },
        },
      }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props: {
        targetPanelId: 'specialized_models',
        targetPanelRequestId: 1,
      },
    });
    flushSync();
    await openSpecializedModelsPanel();

    // The row title renders the i18n label, the panel called
    // list_targets for text_embedding alongside the other task types,
    // and the dropdown is in the DOM.
    expect(document.body.textContent).toContain('Embedding model');
    expect(document.body.textContent).toContain(
      'Turns text into numeric vectors for meaning-based search. Required when Recall is set to Semantic.',
    );
    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'task_model.list_targets' &&
          call[1]?.task_type === 'text_embedding',
      ),
    ).toBe(true);

    const embeddingTrigger = getSimpleTrigger(
      'settings-specialized-text_embedding',
    );
    expect(embeddingTrigger).toBeTruthy();
    expect(embeddingTrigger.textContent).toContain('Not configured');

    openSimpleDropdown('settings-specialized-text_embedding');
    await waitForCondition(() => getSimpleList() !== null);
    selectSimpleOption(
      'settings-specialized-text_embedding',
      'Gemini Embedding 2',
    );

    // Manually click Save to bypass the auto-save debounce for a
    // deterministic assertion on the exact persisted payload.
    vi.useFakeTimers();
    getButton('Save').click();
    vi.advanceTimersByTime(1);
    await flushAsyncUpdates();
    vi.useRealTimers();

    await waitForCondition(() =>
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'task_model.update' &&
          call[1]?.model_tasks?.text_embedding?.target === target,
      ),
    );

    const updateCall = rpcMock.mock.calls
      .filter((call) => call[0] === 'task_model.update')
      .pop();
    expect(updateCall[1].model_tasks.text_embedding.target).toBe(target);
    expect(updateCall[1].model_tasks.text_embedding.options).toEqual({});
  });
});

// All settings sections are always rendered in one scrolling document, so
// "opening" a section means clicking its index entry and scoping the query
// helpers to that section's DOM subtree (same-labeled controls — e.g. the
// per-section Save buttons — exist once per section).
async function openSettingsSection(navLabel, sectionId) {
  await waitForCondition(() => buttonByText(navLabel));
  buttonByText(navLabel).click();
  flushSync();
  await waitForCondition(() =>
    document.body.querySelector(`[data-settings-section="${sectionId}"]`),
  );
  activeSection = document.body.querySelector(
    `[data-settings-section="${sectionId}"]`,
  );
}

async function openProvidersPanel() {
  await openSettingsSection('Providers', 'providers');
  await waitForCondition(() => buttonByText('Add provider'));
}

async function openChannelsPanel() {
  await openSettingsSection('Channels', 'channels');
  await waitForCondition(() => buttonByText('Add channel'));
  await waitForCondition(() => buttonByText('Add channel')?.disabled === false);
}

async function openSubAgentsPanel() {
  await openSettingsSection('Sub-Agents', 'subagents');
  await waitForCondition(() =>
    activeSection.textContent.includes('Max sub-agent depth'),
  );
}

async function openCompactionPanel() {
  await openSettingsSection('Compaction', 'compaction');
  await waitForCondition(() =>
    activeSection.textContent.includes('Summary model'),
  );
}

async function openRecallPanel() {
  await openSettingsSection('Recall', 'recall');
  await waitForCondition(() =>
    activeSection.textContent.includes('Recall backend'),
  );
}

async function openWebSearchPanel() {
  await openSettingsSection('Web Search', 'web_search');
  await waitForCondition(() =>
    activeSection.textContent.includes('Search provider'),
  );
}

async function openDefaultsPanel() {
  await openSettingsSection('Agent defaults', 'defaults');
  await waitForCondition(() =>
    activeSection.textContent.includes('Fallback model'),
  );
}

async function openSpecializedModelsPanel() {
  await openSettingsSection('Specialized Models', 'specialized_models');
  // The panel calls task_model.list_targets on mount; waiting for that call
  // is the strongest signal the panel is mounted and its first paint is
  // committed.
  await waitForCondition(() =>
    rpcMock.mock.calls.some((call) => call[0] === 'task_model.list_targets'),
  );
}

async function waitForModelCatalogs() {
  await waitForCondition(
    () =>
      rpcMock.mock.calls.some((call) => call[0] === 'model.list') &&
      rpcMock.mock.calls.some((call) => call[0] === 'connection.list'),
  );
}

// The section the current test interacted with last (via openSettingsSection).
// Query helpers search this subtree first and fall back to the whole document
// (index nav, portaled dropdowns, and modals live outside the section).
let activeSection = null;

function scopedQueryAll(selector) {
  const scoped = activeSection
    ? Array.from(activeSection.querySelectorAll(selector))
    : [];
  return scoped.length > 0
    ? scoped
    : Array.from(document.body.querySelectorAll(selector));
}

function providerRow(providerName) {
  const rows = scopedQueryAll('.s-provider-card');
  const row = rows.find((item) => item.textContent.includes(providerName));
  expect(row).toBeTruthy();
  return row;
}

function buttonByText(label) {
  const match = (button) => button.textContent.trim() === label;
  const scoped = activeSection
    ? Array.from(activeSection.querySelectorAll('button')).find(match)
    : undefined;
  return (
    scoped ?? Array.from(document.body.querySelectorAll('button')).find(match)
  );
}

function buttonByAriaLabel(label) {
  const match = (button) => button.getAttribute('aria-label') === label;
  const scoped = activeSection
    ? Array.from(activeSection.querySelectorAll('button')).find(match)
    : undefined;
  return (
    scoped ?? Array.from(document.body.querySelectorAll('button')).find(match)
  );
}

// Clicks a button in the open ConfirmDialog by its label (Delete / Cancel).
function confirmChannelDialog(label) {
  const footer = document.body.querySelector('.modal-footer');
  expect(footer, 'confirm dialog not open').toBeTruthy();
  const button = Array.from(footer.querySelectorAll('button')).find(
    (item) => item.textContent.trim() === label,
  );
  expect(button, `confirm button not found: ${label}`).toBeTruthy();
  button.click();
}

function getButton(label) {
  const button = buttonByText(label);
  expect(button).toBeTruthy();
  return button;
}

// Counting helper: stays strictly inside the active section (no body
// fallback), so absence assertions keep meaning "absent in this section".
function buttonsByText(label) {
  const root = activeSection ?? document.body;
  return Array.from(root.querySelectorAll('button')).filter(
    (button) => button.textContent.trim() === label,
  );
}

function getSettingsUpdateCalls() {
  return rpcMock.mock.calls.filter((call) => call[0] === 'settings.update');
}

function scopedQuerySelector(selector) {
  return (
    activeSection?.querySelector(selector) ??
    document.body.querySelector(selector)
  );
}

function setInputValue(selector, value) {
  const input = scopedQuerySelector(selector);
  expect(input).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function setTextareaValue(selector, value) {
  const textarea = scopedQuerySelector(selector);
  expect(textarea).toBeTruthy();
  textarea.value = value;
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

async function openSearchableDropdown(id, rect = defaultTriggerRect()) {
  const trigger = getSearchableTrigger(id);
  stubTriggerRect(trigger, rect);
  trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();

  await waitForCondition(
    () => getSearchableRoot(id).dataset.state === 'open',
    100,
  );
}

function selectSearchableOption(id, label) {
  const option = Array.from(
    getSearchablePanel(id)?.querySelectorAll('.searchable-dropdown__option') ??
      [],
  ).find((item) => item.textContent.trim() === label);
  expect(option).toBeTruthy();
  option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

function getSearchableRoot(id) {
  return getSearchableTrigger(id)?.closest('.searchable-dropdown');
}

function getSearchableTrigger(id) {
  const trigger = scopedQuerySelector(`button#${id}`);
  expect(trigger).toBeTruthy();
  return trigger;
}

function getSearchablePanel() {
  // The panel is portaled to <body>; only the open dropdown renders one.
  return document.body.querySelector('.searchable-dropdown__panel');
}

function openSimpleDropdown(id) {
  const trigger = getSimpleTrigger(id);
  trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

function selectSimpleOption(id, label) {
  const option = Array.from(
    getSimpleList(id)?.querySelectorAll('.dropdown-option') ?? [],
  ).find((item) => item.textContent.trim() === label);
  expect(option).toBeTruthy();
  option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

function getSimpleTrigger(id) {
  const trigger = scopedQuerySelector(`button#${id}`);
  expect(trigger).toBeTruthy();
  return trigger;
}

function getSimpleList() {
  // The list is portaled to <body>; only the open dropdown renders one.
  return document.body.querySelector('.dropdown-primitive__list');
}

function stubTriggerRect(trigger, rect) {
  trigger.getBoundingClientRect = () => ({
    x: rect.left,
    y: rect.top,
    left: rect.left,
    top: rect.top,
    right: rect.right,
    bottom: rect.bottom,
    width: rect.width,
    height: rect.height,
    toJSON: () => rect,
  });
}

function defaultTriggerRect() {
  return {
    left: 96,
    top: 144,
    right: 416,
    bottom: 176,
    width: 320,
    height: 32,
  };
}

function submitChannelForm() {
  const form = document.body.querySelector('.s-channel-form');
  expect(form).toBeTruthy();
  form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  flushSync();
}

async function flushAsyncUpdates(iterations = 4) {
  for (let index = 0; index < iterations; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}

function createSettingsRpcMock(options = {}) {
  let currentSettings = deepClone(options.settings ?? settingsPayload());
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
          denied_chats: Array.isArray(providedStatus.denied_chats)
            ? providedStatus.denied_chats.map((entry) => ({ ...entry }))
            : [],
        },
      ];
    }),
  );
  const taskModelTargets = Array.isArray(options.taskModelTargets)
    ? options.taskModelTargets.map((target) => ({ ...target }))
    : [];
  const taskModelOptionsByTarget = new Map(
    Object.entries(options.taskModelOptions ?? {}).map(([targetId, schema]) => [
      targetId,
      deepClone(schema),
    ]),
  );

  return async (method, params = {}) => {
    if (method === 'settings.get') {
      return deepClone(currentSettings);
    }

    if (method === 'settings.update') {
      if (options.settingsUpdateError) {
        throw options.settingsUpdateError;
      }

      if (typeof options.settingsUpdate === 'function') {
        const nextSettings = await options.settingsUpdate(
          params,
          deepClone(currentSettings),
        );

        if (nextSettings && typeof nextSettings === 'object') {
          currentSettings = deepClone(nextSettings);
          return deepClone(currentSettings);
        }
      }

      currentSettings = mergeSettingsPayload(currentSettings, params);
      return deepClone(currentSettings);
    }

    if (method === 'model.refresh_db') {
      if (options.refreshError) {
        throw options.refreshError;
      }

      return options.refreshResult ?? refreshResult();
    }

    if (method === 'model.list') {
      return { models: options.models ?? modelsPayload() };
    }

    if (method === 'connection.list') {
      return { connections: options.connections ?? connectionsPayload() };
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
        denied_chats: status.denied_chats.map((entry) => ({ ...entry })),
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

    if (method === 'task_model.list_targets') {
      return {
        targets: taskModelTargets
          .filter((target) =>
            Array.isArray(target.task_types)
              ? target.task_types.includes(params.task_type)
              : target.task_type === params.task_type,
          )
          .map((target) => ({ ...target })),
      };
    }

    if (method === 'task_model.options') {
      const schema = taskModelOptionsByTarget.get(params.target);
      if (!schema) {
        throw new Error(`No task model options for target: ${params.target}`);
      }
      return deepClone(schema);
    }

    if (method === 'task_model.update') {
      const nextModelTasks = deepClone(params.model_tasks ?? {});
      currentSettings = mergeSettingsPayload(currentSettings, {
        model_tasks: nextModelTasks,
      });
      return { model_tasks: deepClone(nextModelTasks) };
    }

    // The Extensions and Skills sections are always mounted in the settings
    // document and self-load on mount; give them empty-but-valid payloads.
    if (method === 'extensions.list') {
      return { extensions: options.extensions ?? [] };
    }

    if (method === 'skill.read') {
      return { skills: options.skillFiles ?? [] };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

function mergeSettingsPayload(currentSettings, patch) {
  const nextSettings = deepClone(currentSettings);

  if (patch?.appearance && typeof patch.appearance === 'object') {
    nextSettings.appearance = {
      ...(nextSettings.appearance ?? {}),
      ...patch.appearance,
    };
  }

  if (patch?.skills && typeof patch.skills === 'object') {
    nextSettings.skills = {
      ...(nextSettings.skills ?? {}),
      ...patch.skills,
    };

    if (Array.isArray(patch.skills.directories)) {
      nextSettings.skills.directories = [...patch.skills.directories];
    }
  }

  if (patch?.subagents && typeof patch.subagents === 'object') {
    nextSettings.subagents = {
      ...(nextSettings.subagents ?? {}),
      ...patch.subagents,
    };
  }

  if (patch?.compaction && typeof patch.compaction === 'object') {
    nextSettings.compaction = {
      ...(nextSettings.compaction ?? {}),
      ...patch.compaction,
    };
  }

  if (patch?.recall && typeof patch.recall === 'object') {
    nextSettings.recall = {
      ...(nextSettings.recall ?? {}),
      ...patch.recall,
    };
  }

  if (patch?.web_search && typeof patch.web_search === 'object') {
    nextSettings.web_search = {
      ...(nextSettings.web_search ?? {}),
      ...patch.web_search,
    };

    if (
      patch.web_search.searxng &&
      typeof patch.web_search.searxng === 'object'
    ) {
      nextSettings.web_search.searxng = {
        ...(nextSettings.web_search.searxng ?? {}),
        ...patch.web_search.searxng,
      };
    }
  }

  if (patch?.defaults && typeof patch.defaults === 'object') {
    nextSettings.defaults = {
      ...(nextSettings.defaults ?? {}),
      ...patch.defaults,
    };

    if (patch.defaults.agent && typeof patch.defaults.agent === 'object') {
      const nextAgentDefaults = {
        ...(nextSettings.defaults.agent ?? {}),
      };

      for (const [field, value] of Object.entries(patch.defaults.agent)) {
        if (value === null) {
          delete nextAgentDefaults[field];
          continue;
        }

        nextAgentDefaults[field] = value;
      }

      nextSettings.defaults.agent = nextAgentDefaults;
    }
  }

  if (patch?.model_tasks && typeof patch.model_tasks === 'object') {
    nextSettings.model_tasks = {
      ...(nextSettings.model_tasks ?? {}),
      ...deepClone(patch.model_tasks),
    };
  }

  return nextSettings;
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

function modelsPayload() {
  return [
    {
      id: 'openai/gpt-5.2',
      provider_id: 'openai',
      model_id: 'gpt-5.2',
      name: 'GPT-5.2',
      capabilities: { tools: true },
      context_window: 256000,
      effective_context_window: 256000,
    },
    {
      id: 'openai/gpt-5.2-mini',
      provider_id: 'openai',
      model_id: 'gpt-5.2-mini',
      name: 'GPT-5.2 Mini',
      capabilities: { tools: true },
      context_window: 128000,
      effective_context_window: 128000,
    },
    {
      id: 'openrouter/fresh-model',
      provider_id: 'openrouter',
      model_id: 'fresh-model',
      name: 'Fresh Model',
      capabilities: { tools: true },
      context_window: 128000,
      effective_context_window: 128000,
    },
  ];
}

function connectionsPayload() {
  return [
    {
      id: 'openai:api-key',
      provider_id: 'openai',
      type: 'api_key',
      label: 'API Key',
      usable: true,
    },
    {
      id: 'openrouter:api-key',
      provider_id: 'openrouter',
      type: 'api_key',
      label: 'API Key',
      usable: true,
    },
  ];
}

function settingsPayload(options = {}) {
  const openrouter = provider('openrouter', 'OpenRouter', '/models');

  if (options.eligibleProvider === false) {
    openrouter.credentials_configured = false;
    openrouter.status = 'missing_credentials';
    openrouter.connections[0].configured = false;
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
    subagents: {
      max_subagent_depth: 4,
      max_subagents_per_turn: 8,
      subagent_timeout_minutes: 60,
    },
    compaction: {
      enabled: true,
      trigger: { type: 'context_ratio', threshold: 0.8 },
      strategy: {
        type: 'summary_tail',
        tail_tokens: 15000,
        summary_model: null,
      },
    },
    recall: {
      backend: 'jsonl_scan',
      available_backends: ['jsonl_scan', 'sqlite_fts'],
    },
    web_search: {
      provider: 'brave',
      available_providers: ['brave', 'searxng'],
      default_count: 12,
      searxng: {
        base_url: 'http://localhost:8888',
      },
    },
    defaults: {
      agent: {},
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
    connections: [
      {
        id: `${id}:api-key`,
        type: 'api_key',
        label: 'API Key',
        configured: true,
        credential_key: `${id.toUpperCase()}_API_KEY`,
      },
    ],
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
