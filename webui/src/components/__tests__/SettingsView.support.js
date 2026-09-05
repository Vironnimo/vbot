// @vitest-environment jsdom

import { expect, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';

export { flushSync, mount };
export const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () =>
  rpcBackedApiMock(rpcMock, {
    listClients: () => Promise.resolve({ clients: [] }),
    getTaskModelOptions: (taskType, target) =>
      rpcMock('task_model.options', { task_type: taskType, target }),
    listTaskModelTargets: (taskType) =>
      rpcMock('task_model.list_targets', { task_type: taskType }),
    updateTaskModelSettings: (modelTasks) =>
      rpcMock('task_model.update', { model_tasks: modelTasks }),
  }),
);

export const { default: AgentsView } = await import('../AgentsView.svelte');

export const { default: SettingsView } = await import('../SettingsView.svelte');

export function resetSettingsViewHarness() {
  document.body.innerHTML = '';
  window.history.pushState({}, '', '/');
  delete window.pywebview;
  init('en');
  rpcMock.mockReset();
  activeSection = null;
}

export async function cleanupSettingsViewHarness(mountedComponent) {
  vi.useRealTimers();

  if (mountedComponent) {
    await unmount(mountedComponent);
  }

  document.body.innerHTML = '';
  return null;
}

// Topic navigation keeps editors mounted; helpers scope repeated control labels
// to the active topic just as the visible interface does.
export async function openSettingsSection(navLabel, sectionId) {
  const category = {
    providers: 'Connections',
    channels: 'Connections',
    extensions: 'Connections',
    specialized_models: 'Tools & Media',
    voice: 'Tools & Media',
    web_search: 'Tools & Media',
    subagents: 'Tools & Media',
    session_titles: 'Sessions & Memory',
    recall: 'Sessions & Memory',
    reflection: 'Sessions & Memory',
    general: 'System',
    debug: 'System',
    appearance: 'General',
    preferences: 'General',
  }[sectionId];
  if (sectionId === 'defaults' || sectionId === 'compaction') {
    await waitForCondition(() => buttonByText('Shared defaults'));
    buttonByText('Shared defaults').click();
    flushSync();
    await waitForCondition(() =>
      document.querySelector('#settings-defaults-model'),
    );
    if (sectionId === 'compaction')
      buttonByText('Configure Compaction').click();
  } else {
    await waitForCondition(() => buttonByText(category));
    buttonByText(category).click();
    flushSync();
    await waitForCondition(() =>
      document.querySelector(`#settings-section-${sectionId}`),
    );
    const heading = document.querySelector(`#settings-section-${sectionId}`);
    if (heading.getAttribute('aria-expanded') !== 'true') heading.click();
  }
  flushSync();
  activeSection = document.body.querySelector(
    `[data-settings-section="${sectionId}"]`,
  );
}

export async function openProvidersPanel() {
  await openSettingsSection('Providers', 'providers');
  await waitForCondition(() => buttonByText('Add provider'));
}

export async function openChannelsPanel() {
  await openSettingsSection('Channels', 'channels');
  await waitForCondition(() => buttonByText('Add channel'));
  await waitForCondition(() => buttonByText('Add channel')?.disabled === false);
}

export async function openSubAgentsPanel() {
  await openSettingsSection('Sub-Agents', 'subagents');
  await waitForCondition(() =>
    activeSection.textContent.includes('Max sub-agent depth'),
  );
}

export async function openCompactionPanel() {
  await openSettingsSection('Compaction', 'compaction');
  await waitForCondition(() =>
    activeSection.textContent.includes('Summary model'),
  );
}

export async function openRecallPanel() {
  await openSettingsSection('Recall', 'recall');
  await waitForCondition(() =>
    activeSection.textContent.includes('Recall backend'),
  );
}

export async function openWebSearchPanel() {
  await openSettingsSection('Web Search', 'web_search');
  await waitForCondition(() =>
    activeSection.textContent.includes('Search provider'),
  );
}

export async function openDefaultsPanel() {
  await openSettingsSection('Agent defaults', 'defaults');
  await waitForCondition(() =>
    activeSection.textContent.includes('Fallback model'),
  );
}

export async function openSpecializedModelsPanel() {
  await openSettingsSection('Specialized Models', 'specialized_models');
  // The panel calls task_model.list_targets on mount; waiting for that call
  // is the strongest signal the panel is mounted and its first paint is
  // committed.
  await waitForCondition(() =>
    rpcMock.mock.calls.some((call) => call[0] === 'task_model.list_targets'),
  );
}

export async function waitForModelCatalogs() {
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

export function scopedQueryAll(selector) {
  const scoped = activeSection
    ? Array.from(activeSection.querySelectorAll(selector))
    : [];
  return scoped.length > 0
    ? scoped
    : Array.from(document.body.querySelectorAll(selector));
}

export function providerRow(providerName) {
  const rows = scopedQueryAll('.s-provider-card');
  const row = rows.find((item) => item.textContent.includes(providerName));
  expect(row).toBeTruthy();
  return row;
}

export function buttonByText(label) {
  const match = (button) => button.textContent.trim() === label;
  const scoped = activeSection
    ? Array.from(activeSection.querySelectorAll('button')).find(match)
    : undefined;
  return (
    scoped ?? Array.from(document.body.querySelectorAll('button')).find(match)
  );
}

export function buttonByAriaLabel(label) {
  const match = (button) => button.getAttribute('aria-label') === label;
  const scoped = activeSection
    ? Array.from(activeSection.querySelectorAll('button')).find(match)
    : undefined;
  return (
    scoped ?? Array.from(document.body.querySelectorAll('button')).find(match)
  );
}

// Clicks a button in the open ConfirmDialog by its label (Delete / Cancel).
export function confirmChannelDialog(label) {
  const footer = document.body.querySelector('.modal-footer');
  expect(footer, 'confirm dialog not open').toBeTruthy();
  const button = Array.from(footer.querySelectorAll('button')).find(
    (item) => item.textContent.trim() === label,
  );
  expect(button, `confirm button not found: ${label}`).toBeTruthy();
  button.click();
}

export function getButton(label) {
  const button = buttonByText(label);
  expect(button).toBeTruthy();
  return button;
}

// Counting helper: stays strictly inside the active section (no body
// fallback), so absence assertions keep meaning "absent in this section".
export function buttonsByText(label) {
  const root = activeSection ?? document.body;
  return Array.from(root.querySelectorAll('button')).filter(
    (button) => button.textContent.trim() === label,
  );
}

export function getSettingsUpdateCalls() {
  return rpcMock.mock.calls.filter((call) => call[0] === 'settings.update');
}

export function scopedQuerySelector(selector) {
  return (
    activeSection?.querySelector(selector) ??
    document.body.querySelector(selector)
  );
}

export function setInputValue(selector, value) {
  const input = scopedQuerySelector(selector);
  expect(input).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

export function setTextareaValue(selector, value) {
  const textarea = scopedQuerySelector(selector);
  expect(textarea).toBeTruthy();
  textarea.value = value;
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

export async function openSearchableDropdown(id, rect = defaultTriggerRect()) {
  const trigger = getSearchableTrigger(id);
  stubTriggerRect(trigger, rect);
  trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();

  await waitForCondition(
    () => getSearchableRoot(id).dataset.state === 'open',
    100,
  );
}

export function selectSearchableOption(id, label) {
  const option = Array.from(
    getSearchablePanel(id)?.querySelectorAll('.searchable-dropdown__option') ??
      [],
  ).find((item) => item.textContent.trim() === label);
  expect(option).toBeTruthy();
  option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

export function getSearchableRoot(id) {
  return getSearchableTrigger(id)?.closest('.searchable-dropdown');
}

export function getSearchableTrigger(id) {
  const trigger = scopedQuerySelector(`button#${id}`);
  expect(trigger).toBeTruthy();
  return trigger;
}

export function getSearchablePanel() {
  // The panel is portaled to <body>; only the open dropdown renders one.
  return document.body.querySelector('.searchable-dropdown__panel');
}

export function openSimpleDropdown(id) {
  const trigger = getSimpleTrigger(id);
  trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

export function selectSimpleOption(id, label) {
  const option = Array.from(
    getSimpleList(id)?.querySelectorAll('.dropdown-option') ?? [],
  ).find((item) => item.textContent.trim() === label);
  expect(option).toBeTruthy();
  option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

export function getSimpleTrigger(id) {
  const trigger = scopedQuerySelector(`button#${id}`);
  expect(trigger).toBeTruthy();
  return trigger;
}

export function getSimpleList() {
  // The list is portaled to <body>; only the open dropdown renders one.
  return document.body.querySelector('.dropdown-primitive__list');
}

export function stubTriggerRect(trigger, rect) {
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

export function defaultTriggerRect() {
  return {
    left: 96,
    top: 144,
    right: 416,
    bottom: 176,
    width: 320,
    height: 32,
  };
}

export function submitChannelForm() {
  const form = document.body.querySelector('.s-channel-form');
  expect(form).toBeTruthy();
  form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  flushSync();
}

export async function flushAsyncUpdates(iterations = 4) {
  for (let index = 0; index < iterations; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}

export function createSettingsRpcMock(options = {}) {
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
  const accessSource =
    options.channelAccess !== null && typeof options.channelAccess === 'object'
      ? options.channelAccess
      : {};
  const channelAccess = new Map(
    channels.map((channel) => [
      channel.id,
      deepClone(
        accessSource[channel.id] ?? {
          channel_id: channel.id,
          self_user_id: null,
          groups: [],
        },
      ),
    ]),
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

    if (method === 'provider.routing_options') {
      return {
        providers: options.routingProviders ?? [
          { slug: 'anthropic', name: 'Anthropic' },
          { slug: 'deepinfra', name: 'DeepInfra' },
        ],
      };
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

    if (method === 'channel.access.get') {
      const access = channelAccess.get(params.id);
      if (!access) {
        throw new Error(`Unknown channel access id: ${params.id}`);
      }
      return deepClone(access);
    }

    if (method === 'channel.identity.set') {
      const access = channelAccess.get(params.id);
      if (!access) {
        throw new Error(`Unknown channel access id: ${params.id}`);
      }
      access.self_user_id = params.user_id;
      for (const group of access.groups) {
        if (!group.admin_user_ids.includes(params.user_id)) {
          group.admin_user_ids.push(params.user_id);
          group.admin_user_ids.sort();
        }
        for (const participant of group.participants) {
          if (participant.user_id === params.user_id) {
            participant.role = 'admin';
          }
        }
      }
      return deepClone(access);
    }

    if (method === 'channel.admin.grant' || method === 'channel.admin.revoke') {
      const access = channelAccess.get(params.id);
      if (!access) {
        throw new Error(`Unknown channel access id: ${params.id}`);
      }
      const group = access.groups.find(
        (item) => item.access_scope_id === params.access_scope_id,
      );
      if (!group) {
        throw new Error(
          `Unknown channel access group: ${params.access_scope_id}`,
        );
      }
      const granting = method === 'channel.admin.grant';
      const adminIds = new Set(group.admin_user_ids);
      if (granting) {
        adminIds.add(params.user_id);
      } else if (params.user_id !== access.self_user_id) {
        adminIds.delete(params.user_id);
      }
      group.admin_user_ids = [...adminIds].sort();
      for (const participant of group.participants) {
        if (participant.user_id === params.user_id) {
          participant.role =
            granting || params.user_id === access.self_user_id
              ? 'admin'
              : 'member';
        }
      }
      return deepClone(access);
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
      channelAccess.set(channel.id, {
        channel_id: channel.id,
        self_user_id: null,
        groups: [],
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
      channelAccess.delete(params.id);
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

export function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

export function mergeSettingsPayload(currentSettings, patch) {
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

export function channelConfig(id, overrides = {}) {
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

export function agentsPayload() {
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

export function modelsPayload() {
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

export function connectionsPayload() {
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

export function settingsPayload(options = {}) {
  const openrouter = provider('openrouter', 'OpenRouter', '/models');
  openrouter.routing = {
    default: {
      mode: 'automatic',
      providers: [],
      blocked: [],
      allow_fallbacks: true,
    },
    models: {},
  };

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
      custom_endpoints: { supported: true, items: [] },
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
      backend: 'canonical_scan',
      available_backends: ['canonical_scan', 'sqlite_fts'],
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

export function provider(id, name, modelsEndpoint) {
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

export function refreshResult() {
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

export async function waitForCondition(check, attempts = 20) {
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
