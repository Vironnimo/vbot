// @vitest-environment jsdom

import { vi } from 'vitest';
import { flushSync, unmount } from 'svelte';

import { init } from '../lib/i18n.js';
import { rpcBackedApiMock } from '../components/__tests__/apiMock.js';

export const rpcMock = vi.fn();
export const listClientsMock = vi.fn(() => Promise.resolve({ clients: [] }));
export const listQueueMock = vi.fn(() => Promise.resolve({ items: [] }));
export const listSessionsMock = vi.fn(() => Promise.resolve({ sessions: [] }));
export const listSessionActivityMock = vi.fn(() =>
  Promise.resolve({ agents: [] }),
);
export const listLogsMock = vi.fn();
export const readLogFileMock = vi.fn();
export const subscribeLogEventsMock = vi.fn(() => ({
  close: vi.fn(),
  socket: null,
}));
export const subscribeRunEventsMock = vi.fn(() => ({
  close: vi.fn(),
  source: null,
}));
export const subscribeServerEventsMock = vi.fn(() => ({
  close: vi.fn(),
  socket: null,
}));
export const debugStatusMock = vi.fn().mockResolvedValue({ enabled: false });

vi.mock('svelte', async () => {
  return import('../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () =>
  rpcBackedApiMock(rpcMock, {
    RUN_EVENT_ASSISTANT_OUTPUT_DELTA: 'assistant_output_delta',
    RUN_EVENT_REASONING_DELTA: 'reasoning_delta',
    RUN_EVENT_PROVIDER_HEARTBEAT: 'provider_heartbeat',
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

export const { default: App } = await import('../App.svelte');

export function resetAppHarness() {
  document.body.innerHTML = '';
  localStorage.clear();
  // Tests share one jsdom window: drop the location hash a previous test's
  // history navigation left behind so every mount starts on the default tab.
  window.history.replaceState(null, '', window.location.pathname);
  delete window.pywebview;
  init('en');
  listLogsMock.mockReset();
  listClientsMock.mockReset();
  listClientsMock.mockResolvedValue({ clients: [] });
  listQueueMock.mockReset();
  listQueueMock.mockResolvedValue({ items: [] });
  listSessionsMock.mockReset();
  listSessionsMock.mockResolvedValue({ sessions: [] });
  listSessionActivityMock.mockReset();
  listSessionActivityMock.mockResolvedValue({ agents: [] });
  readLogFileMock.mockReset();
  subscribeLogEventsMock.mockClear();
  subscribeRunEventsMock.mockClear();
  subscribeServerEventsMock.mockClear();
  debugStatusMock.mockReset();
  debugStatusMock.mockResolvedValue({ enabled: false });
  rpcMock.mockImplementation(createEmptyChatRpcMock());
  listLogsMock.mockResolvedValue({
    files: ['2026-05-11.log'],
    default_file: '2026-05-11.log',
  });
  readLogFileMock.mockResolvedValue({
    file: '2026-05-11.log',
    entries: [],
    cursor: 'app-log-cursor',
  });
}

export async function cleanupAppHarness(mountedComponent) {
  if (mountedComponent) {
    await unmount(mountedComponent);
  }

  document.body.innerHTML = '';
  localStorage.clear();
  delete window.pywebview;
  rpcMock.mockReset();
  return null;
}

export async function waitForAssertion(assertion) {
  let lastError = null;

  for (let attempt = 0; attempt < 10; attempt += 1) {
    try {
      assertion();
      return;
    } catch (error) {
      lastError = error;
      await Promise.resolve();
      await new Promise((resolve) => setTimeout(resolve, 0));
      flushSync();
    }
  }

  throw lastError;
}

export function createEmptyChatRpcMock() {
  return async (method) => {
    if (method === 'agent.list') {
      return { agents: [] };
    }

    if (method === 'chat.commands') {
      return { items: [] };
    }

    if (method === 'skill.list') {
      return { skills: [], invalid_skills: [] };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

export function createChatRpcMock(agents) {
  return async (method, params) => {
    if (method === 'agent.list') {
      return { agents };
    }

    if (method === 'chat.commands') {
      return { items: [] };
    }

    if (method === 'chat.history') {
      return {
        agent_id: params?.agent_id ?? '',
        session_id: params?.session_id ?? '',
        messages: [],
      };
    }

    if (method === 'skill.list') {
      return { skills: [], invalid_skills: [] };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

export function createSubAgentNavigationRpcMock(agents) {
  const messagesBySession = {
    'session-parent': [
      {
        id: 'parent-user',
        role: 'user',
        content: 'Start sub-agent',
      },
      {
        id: 'parent-assistant-tool',
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'call-subagent-repeat',
            name: 'subagent',
            arguments: {
              agent_id: 'alpha',
              background: true,
              content: 'Inspect again',
            },
          },
        ],
      },
      {
        id: 'parent-tool-result',
        role: 'tool',
        tool_call_id: 'call-subagent-repeat',
        name: 'subagent',
        content: JSON.stringify({
          ok: true,
          data: {
            agent_id: 'alpha',
            session_id: 'sub-session-repeat',
            run_id: 'sub-run-repeat',
            status: 'completed',
          },
        }),
      },
    ],
    'sub-session-repeat': [
      {
        id: 'sub-agent-assistant',
        role: 'assistant',
        content: 'Sub-agent response',
      },
    ],
  };

  return async (method, params) => {
    if (method === 'agent.list') {
      return { agents };
    }

    if (method === 'chat.commands') {
      return { items: [] };
    }

    if (method === 'chat.history') {
      return {
        agent_id: params?.agent_id ?? '',
        session_id: params?.session_id ?? '',
        messages: messagesBySession[params?.session_id] ?? [],
      };
    }

    if (method === 'chat.queue_list') {
      return { items: [] };
    }

    if (method === 'skill.list') {
      return { skills: [], invalid_skills: [] };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

export function createRunningSubAgentRpcMock(agents) {
  return async (method, params) => {
    if (method === 'agent.list') {
      return { agents };
    }

    if (method === 'chat.commands') {
      return { items: [] };
    }

    if (method === 'chat.history') {
      if (params?.session_id !== 'session-parent') {
        return {
          agent_id: params?.agent_id ?? '',
          session_id: params?.session_id ?? '',
          messages: [],
          active_run: {
            run_id:
              params?.session_id === 'sub-session-running'
                ? 'sub-run-running'
                : `run-${params?.session_id ?? 'other'}`,
            agent_id: params?.agent_id ?? '',
            session_id: params?.session_id ?? '',
            status: 'running',
            events: [],
          },
        };
      }
      return {
        agent_id: params?.agent_id ?? '',
        session_id: params?.session_id ?? '',
        messages:
          params?.session_id === 'session-parent'
            ? [
                {
                  id: 'parent-assistant-tool',
                  role: 'assistant',
                  content: null,
                  tool_calls: [
                    {
                      id: 'call-subagent-running',
                      name: 'subagent',
                      arguments: {
                        agent_id: 'alpha',
                        background: true,
                        content: 'Inspect in the background',
                      },
                    },
                  ],
                },
                {
                  id: 'parent-tool-result',
                  role: 'tool',
                  tool_call_id: 'call-subagent-running',
                  name: 'subagent',
                  content: JSON.stringify({
                    ok: true,
                    data: {
                      agent_id: 'alpha',
                      session_id: 'sub-session-running',
                      run_id: 'sub-run-running',
                      status: 'running',
                    },
                  }),
                },
              ]
            : [],
      };
    }

    if (method === 'chat.queue_list') {
      return { items: [] };
    }

    if (method === 'skill.list') {
      return { skills: [], invalid_skills: [] };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
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

export function agentTabByName(name) {
  return Array.from(document.querySelectorAll('.agent-tabs .agent-tab')).find(
    (button) => button.textContent?.includes(name),
  );
}

export function activeAgentTab() {
  return document.querySelector('.agent-tabs .agent-tab.active');
}

export function viewSessionButton() {
  return document.querySelector('button[aria-label="Open Sub-Agent Session"]');
}

export function returnToCurrentSessionButton() {
  return Array.from(document.querySelectorAll('button')).find(
    (button) => button.textContent?.trim() === 'Return to current session',
  );
}

export function sidebarNavButton(text) {
  return Array.from(
    document.querySelectorAll('nav.app-shell__navigation .app-shell__nav-item'),
  ).find((button) => button.textContent?.trim() === text);
}

export function settingsPanelButton(text) {
  return Array.from(
    document.querySelectorAll('nav.settings-nav .snav-item'),
  ).find((button) => button.textContent?.trim() === text);
}

export function debugEnabledToggle() {
  return document.querySelector(
    'button.toggle[role="switch"][aria-label="Enable debug mode"]',
  );
}

export async function waitForCondition(assertion, options = {}) {
  const attempts = options.attempts ?? 60;
  const intervalMs = options.intervalMs ?? 50;

  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      assertion();
      return;
    } catch (error) {
      if (attempt === attempts - 1) {
        throw error;
      }
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
      flushSync();
    }
  }
}

export function onboardingSettings(connected) {
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

export function createOnboardingRpcMock({ connected = false } = {}) {
  return async (method) => {
    if (method === 'agent.list') {
      return {
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
      };
    }
    if (method === 'chat.commands') {
      return { items: [] };
    }
    if (method === 'chat.history') {
      return { messages: [] };
    }
    if (method === 'chat.queue_list') {
      return { items: [] };
    }
    if (method === 'skill.list') {
      return { skills: [], invalid_skills: [] };
    }
    if (method === 'settings.get') {
      return onboardingSettings(connected);
    }
    if (method === 'model.list') {
      return { models: [] };
    }
    if (method === 'connection.list') {
      return { connections: [] };
    }
    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

export function createSettingsRpcMock(options = {}) {
  let debugEnabled = options.initialDebugEnabled ?? false;
  let traceLimit = options.initialTraceLimit ?? 50;

  const baseSettings = () => ({
    general: {
      server: { listen_host: '127.0.0.1', listen_port: 8420 },
      data_directory: 'C:/data',
    },
    appearance: { language: 'en', available_languages: ['en'] },
    skills: { default_directory: 'C:/data/skills', directories: [] },
    subagents: {
      max_subagent_depth: 4,
      max_subagents_per_turn: 8,
      subagent_timeout_minutes: 60,
    },
    compaction: {
      auto: true,
      threshold: 0.8,
      tail_tokens: 15000,
      summary_model: null,
    },
    recall: {
      backend: 'canonical_scan',
      available_backends: ['canonical_scan', 'sqlite_fts'],
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
    debug: { enabled: debugEnabled, trace_limit: traceLimit },
  });

  return async (method, params = {}) => {
    if (method === 'agent.list') {
      return { agents: [] };
    }

    if (method === 'chat.commands') {
      return { items: [] };
    }

    if (method === 'chat.history') {
      return {
        agent_id: params?.agent_id ?? '',
        session_id: params?.session_id ?? '',
        messages: [],
      };
    }

    if (method === 'chat.queue_list') {
      return { items: [] };
    }

    if (method === 'skill.list') {
      return { skills: [], invalid_skills: [] };
    }

    if (method === 'settings.get') {
      return baseSettings();
    }

    if (method === 'settings.update') {
      if (params?.debug && typeof params.debug === 'object') {
        if (typeof params.debug.enabled === 'boolean') {
          debugEnabled = params.debug.enabled;
        }
        if (Number.isInteger(params.debug.trace_limit)) {
          traceLimit = params.debug.trace_limit;
        }
      }
      return baseSettings();
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}
