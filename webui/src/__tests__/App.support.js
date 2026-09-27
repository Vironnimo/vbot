// @vitest-environment jsdom

import { vi } from 'vitest';
import { flushSync, unmount } from 'svelte';

import { init, t } from '../lib/i18n.js';
import { rpcBackedApiMock } from '../components/__tests__/apiMock.support.js';

export const rpcMock = vi.fn();
export const listSessionActivityMock = vi.fn(() =>
  Promise.resolve({ agents: [] }),
);
export const subscribeRunEventsMock = vi.fn(() => ({
  close: vi.fn(),
  source: null,
}));
export const subscribeServerEventsMock = vi.fn(() => ({
  close: vi.fn(),
  socket: null,
}));
export const debugStatusMock = vi.fn().mockResolvedValue({ enabled: false });
const listClientsMock = vi.fn();
const listQueueMock = vi.fn();
const listSessionsMock = vi.fn();
const listLogsMock = vi.fn();
const readLogFileMock = vi.fn();
const subscribeLogEventsMock = vi.fn(() => ({ close: vi.fn(), socket: null }));

vi.mock('svelte', async () => {
  return import('../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () =>
  rpcBackedApiMock(rpcMock, {
    RUN_EVENT_ASSISTANT_OUTPUT_DELTA: 'assistant_output_delta',
    RUN_EVENT_REASONING_DELTA: 'reasoning_delta',
    RUN_EVENT_PROVIDER_HEARTBEAT: 'provider_heartbeat',
    RUN_EVENT_PROVIDER_REQUEST_STATUS: 'provider_request_status',
    RUN_EVENT_CHANGE_STATS: 'run_change_stats',
    RUN_EVENT_STREAM_ATTEMPT_RESTARTED: 'stream_attempt_restarted',
    RUN_EVENT_TOOL_CALL_DELTA: 'tool_call_delta',
    RUN_EVENT_TOOL_CALL_STDERR: 'tool_call_stderr',
    RUN_EVENT_TOOL_CALL_STDOUT: 'tool_call_stdout',
    debugStatus: (...args) => debugStatusMock(...args),
    listClients: (...args) => listClientsMock(...args),
    listQueue: (...args) => listQueueMock(...args),
    listSessions: (...args) => listSessionsMock(...args),
    listSessionActivity: (...args) => listSessionActivityMock(...args),
    listLogs: (...args) => listLogsMock(...args),
    readLogFile: (...args) => readLogFileMock(...args),
    subscribeLogEvents: (...args) => subscribeLogEventsMock(...args),
    subscribeRunEvents: (...args) => subscribeRunEventsMock(...args),
    subscribeServerEvents: (...args) => subscribeServerEventsMock(...args),
  }),
);

export const { default: App, NAVIGATION_ITEMS } = await import('../App.svelte');

export function resetAppHarness() {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  document.body.innerHTML = '';
  localStorage.clear();
  // Tests share one jsdom window: drop the location hash a previous test's
  // history navigation left behind so every mount starts on the default tab.
  window.history.replaceState(null, '', window.location.pathname);
  delete window.pywebview;
  init('en');
  listClientsMock.mockReset();
  listClientsMock.mockResolvedValue({ clients: [] });
  listQueueMock.mockReset();
  listQueueMock.mockResolvedValue({ items: [] });
  listSessionsMock.mockReset();
  listSessionsMock.mockResolvedValue({ sessions: [] });
  listSessionActivityMock.mockReset();
  listSessionActivityMock.mockResolvedValue({ agents: [] });
  listLogsMock.mockReset();
  listLogsMock.mockResolvedValue({
    files: ['2026-05-11.log'],
    default_file: '2026-05-11.log',
  });
  readLogFileMock.mockReset();
  readLogFileMock.mockResolvedValue({
    file: '2026-05-11.log',
    entries: [],
    cursor: 'app-log-cursor',
  });
  subscribeLogEventsMock.mockClear();
  subscribeRunEventsMock.mockClear();
  subscribeServerEventsMock.mockClear();
  debugStatusMock.mockReset();
  debugStatusMock.mockResolvedValue({ enabled: false });
  rpcMock.mockImplementation(createAppRpcMock());
}

export async function cleanupAppHarness(mountedComponent) {
  if (mountedComponent) {
    await unmount(mountedComponent);
  }

  document.body.innerHTML = '';
  localStorage.clear();
  delete window.pywebview;
  rpcMock.mockReset();
  vi.unstubAllGlobals();
  return null;
}

const QUICK_POLLS = 10;
const SLOW_POLLS = 60;
const SLOW_POLL_MS = 50;

// Retries an assertion until it passes: first after every pending task, then
// every 50 ms for about three seconds.
export async function waitForCondition(assertion) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      assertion();
      return;
    } catch (error) {
      if (attempt >= QUICK_POLLS + SLOW_POLLS) {
        throw error;
      }
      const delay = attempt < QUICK_POLLS ? 0 : SLOW_POLL_MS;
      await new Promise((resolve) => setTimeout(resolve, delay));
      flushSync();
    }
  }
}

/**
 * RPC fake for a mounted App. It answers the Chat startup reads for `agents`
 * (`history(params)` adds fields to each `chat.history` reply) and any method
 * in `methods`; every other method fails like an unknown RPC.
 */
export function createAppRpcMock({
  agents = [],
  history = () => ({}),
  methods = {},
} = {}) {
  return async (method, params = {}) => {
    if (Object.hasOwn(methods, method)) {
      return methods[method](params);
    }
    switch (method) {
      case 'agent.list':
        return { agents };
      case 'chat.commands':
      case 'chat.queue_list':
        return { items: [] };
      case 'skill.list':
        return { skills: [], invalid_skills: [] };
      case 'chat.history':
        return {
          agent_id: params.agent_id ?? '',
          session_id: params.session_id ?? '',
          messages: [],
          ...history(params),
        };
      default:
        throw new Error(`Unexpected RPC method: ${method}`);
    }
  };
}

function subAgentCall(callId, sessionId, runId, status, content) {
  return [
    {
      id: 'parent-assistant-tool',
      role: 'assistant',
      content: null,
      tool_calls: [
        {
          id: callId,
          name: 'subagent',
          arguments: { agent_id: 'alpha', background: true, content },
        },
      ],
    },
    {
      id: 'parent-tool-result',
      role: 'tool',
      tool_call_id: callId,
      name: 'subagent',
      content: JSON.stringify({
        ok: true,
        data: {
          agent_id: 'alpha',
          session_id: sessionId,
          run_id: runId,
          status,
        },
      }),
    },
  ];
}

// `session-parent` holds a completed background sub-agent call ('Inspect
// again') whose child Session `sub-session-repeat` answered 'Sub-agent
// response'.
export function createSubAgentNavigationRpcMock(agents) {
  const messagesBySession = {
    'session-parent': [
      { id: 'parent-user', role: 'user', content: 'Start sub-agent' },
      ...subAgentCall(
        'call-subagent-repeat',
        'sub-session-repeat',
        'sub-run-repeat',
        'completed',
        'Inspect again',
      ),
    ],
    'sub-session-repeat': [
      {
        id: 'sub-agent-assistant',
        role: 'assistant',
        content: 'Sub-agent response',
      },
    ],
  };
  return createAppRpcMock({
    agents,
    history: (params) => ({
      messages: messagesBySession[params.session_id] ?? [],
    }),
  });
}

// `session-parent` holds a background sub-agent call whose child Run
// `sub-run-running` still runs; every other Session reports an active Run.
export function createRunningSubAgentRpcMock(agents) {
  return createAppRpcMock({
    agents,
    history: (params) =>
      params.session_id === 'session-parent'
        ? {
            messages: subAgentCall(
              'call-subagent-running',
              'sub-session-running',
              'sub-run-running',
              'running',
              'Inspect in the background',
            ),
          }
        : {
            active_run: {
              run_id:
                params.session_id === 'sub-session-running'
                  ? 'sub-run-running'
                  : `run-${params.session_id ?? 'other'}`,
              agent_id: params.agent_id ?? '',
              session_id: params.session_id ?? '',
              status: 'running',
              events: [],
            },
          },
  });
}

export function runServerEvent(type, runId, sequence, payload = {}) {
  return {
    type,
    sequence,
    payload: {
      run_id: runId,
      agent_id: payload.agent_id ?? 'alpha',
      session_id: payload.session_id ?? 'session-parent',
      run_event_timestamp: `2026-05-26T00:00:0${sequence}+00:00`,
      ...payload,
      run_event_sequence: sequence,
    },
  };
}

// Trigger of the Chat header's personal Agent picker.
function agentPickerTrigger() {
  return document.querySelector(
    '.chat-header__agent-picker button[aria-haspopup="listbox"]',
  );
}

// Name of the personal Agent selected in the picker; '' when none is.
export function selectedPersonalAgentName() {
  const trigger = agentPickerTrigger();
  if (
    !trigger ||
    trigger.querySelector('[class*="trigger-label--placeholder"]')
  ) {
    return '';
  }
  return trigger.textContent.trim();
}

// Selects a personal Agent the way a user does: open the picker, choose the
// option (the picker's list is portaled to <body>).
export async function selectPersonalAgent(name) {
  const option = () =>
    Array.from(document.querySelectorAll('[role="option"]')).find((item) =>
      item.getAttribute('aria-label')?.startsWith(`${name}:`),
    );
  await waitForCondition(() => {
    if (agentPickerTrigger()?.disabled !== false) {
      throw new Error('The Agent picker is not ready.');
    }
  });
  agentPickerTrigger().click();
  flushSync();
  await waitForCondition(() => {
    if (!option()) {
      throw new Error(`The Agent picker has no ${name} option.`);
    }
  });
  option().click();
  flushSync();
}

export function viewSessionButton() {
  return document.querySelector(
    `button[aria-label="${t('chat.subagent.openSession')}"]`,
  );
}

export function returnToCurrentSessionButton() {
  return buttonWithText('button', t('chat.returnToCurrentSession'));
}

// First element matching `selector` whose trimmed text is `text`.
export function buttonWithText(selector, text) {
  return Array.from(document.querySelectorAll(selector)).find(
    (button) => button.textContent?.trim() === text,
  );
}

// Sidebar button of a NAVIGATION_ITEMS view id, or of an Extension page by
// its title.
export function sidebarNavButton(viewIdOrTitle) {
  const item = NAVIGATION_ITEMS.find(({ id }) => id === viewIdOrTitle);
  return buttonWithText(
    'nav.app-shell__navigation .app-shell__nav-item',
    item ? t(item.labelKey, item.labelFallback) : viewIdOrTitle,
  );
}

// Settings navigation button of a page id (general, tools, system, ...).
export function settingsPanelButton(pageId) {
  return buttonWithText(
    'nav.settings-nav .snav-item',
    t(`settings.pages.${pageId}`),
  );
}

export function debugEnabledToggle() {
  return document.querySelector(
    `button.toggle[role="switch"][aria-label="${t('debug.enabled')}"]`,
  );
}

function onboardingSettings(connected) {
  return {
    general: {
      server: { listen_host: '127.0.0.1', listen_port: 8420 },
      data_directory: 'C:/data',
    },
    appearance: { language: 'en', available_languages: ['en'] },
    providers: {
      items: [
        {
          id: 'openrouter',
          name: 'OpenRouter',
          connections: [
            {
              id: 'openrouter:api-key',
              type: 'api_key',
              label: 'API Key',
              configured: connected,
              enabled: true,
              usable: connected,
              credential_key: 'OPENROUTER_API_KEY',
              accounts: connected
                ? [{ id: 'default', usable: true, source: 'data_dir' }]
                : [],
            },
          ],
        },
        {
          id: 'ollama',
          name: 'Ollama',
          connections: [
            {
              id: 'ollama:local',
              type: 'none',
              label: 'Local',
              configured: true,
              enabled: false,
              usable: false,
              accounts: [{ id: 'default', usable: true, source: 'none' }],
            },
          ],
        },
      ],
      custom_endpoints: { supported: true, items: [] },
    },
    defaults: { agent: {} },
    debug: { enabled: false, trace_limit: 50 },
  };
}

// A single `main` Agent that has a Model only when a Provider is connected.
export function createOnboardingRpcMock({ connected = false } = {}) {
  return createAppRpcMock({
    agents: [
      {
        id: 'main',
        name: 'Main',
        model: connected ? 'openrouter/anthropic/claude-sonnet-4' : '',
        fallback_models: [],
        workspace: '/data/workspace-main',
        temperature: null,
        thinking_effort: '',
        memory_prompt_mode: 'agent_user',
        tool_access: { mode: 'all' },
        allowed_skills: ['*'],
        custom_system_prompt_enabled: false,
        current_session_id: '',
      },
    ],
    methods: {
      'settings.get': () => onboardingSettings(connected),
      'model.list': () => ({ models: [] }),
      'connection.list': () => ({ connections: [] }),
    },
  });
}

// Settings that keep what `settings.update` saved for sub-agent limits and
// Debug Mode.
export function createSettingsRpcMock({ initialDebugEnabled = false } = {}) {
  let debug = { enabled: initialDebugEnabled, trace_limit: 50 };
  let subagents = {
    max_subagent_depth: 4,
    max_subagents_per_turn: 8,
    subagent_timeout_minutes: 60,
  };

  const settings = () => ({
    general: {
      server: { listen_host: '127.0.0.1', listen_port: 8420 },
      data_directory: 'C:/data',
    },
    appearance: { language: 'en', available_languages: ['en'] },
    skills: { default_directory: 'C:/data/skills', directories: [] },
    subagents: { ...subagents },
    compaction: {
      auto: true,
      threshold: 0.8,
      tail_tokens: 15000,
      summary_model: null,
    },
    recall: {
      backend: 'sqlite_fts',
      available_backends: ['sqlite_fts'],
    },
    web_search: {
      provider: 'brave',
      available_providers: ['brave', 'searxng'],
      searxng: { base_url: 'http://localhost:8888' },
    },
    providers: {
      items: [],
      custom_endpoints: { supported: true, items: [] },
    },
    defaults: { agent: {} },
    debug: { ...debug },
  });

  return createAppRpcMock({
    methods: {
      'settings.get': settings,
      'settings.update': (params) => {
        if (params.subagents) subagents = { ...subagents, ...params.subagents };
        if (params.debug) debug = { ...debug, ...params.debug };
        return settings();
      },
    },
  });
}
