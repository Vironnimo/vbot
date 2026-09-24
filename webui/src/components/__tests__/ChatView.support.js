// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync as svelteFlushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import {
  HOVER_CARD_SHOW_DELAY_MS,
  TOOLTIP_SHOW_DELAY_MS,
} from '../../lib/tooltip.js';
import { rpcBackedApiMock } from './apiMock.js';

export const rpcMock = vi.fn();
export const subscribeRunEventsMock = vi.fn(() => ({
  close: vi.fn(),
  source: null,
}));
export const listSessionsMock = vi.fn(async () => ({ sessions: [] }));
export const listSessionActivityMock = vi.fn(async () => ({ agents: [] }));
export const getSessionMock = vi.fn(async () => ({ session: null }));
export const listQueueMock = vi.fn(async () => ({ items: [] }));
export const removeFromQueueMock = vi.fn(async () => ({ ok: true }));
export const updateQueueItemMock = vi.fn(async () => ({ ok: true }));
export const cancelRunMock = vi.fn(async () => ({ ok: true }));
export const cancelToolCallMock = vi.fn(async () => ({ ok: true }));
export const showProjectMock = vi.fn(async () => ({ project: {}, scan: {} }));
export const applyConnectionSnapshotMock = vi.fn();
export const closeSubscriptionForMock = vi.fn();
// Per-mount references to the real chatState and runStream created inside
// ChatView. The reconcile tests use these to introspect live session state
// (and, for the staleRunId-guard test, to mutate `currentRun.runId` while
// a `chat.history` request is in flight).
export const testChatStateRefs = [];
export const testRunStreamRefs = [];

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
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
    subscribeRunEvents: (...args) => subscribeRunEventsMock(...args),
    listSessions: (...args) => listSessionsMock(...args),
    listSessionActivity: (...args) => listSessionActivityMock(...args),
    getSession: (...args) => getSessionMock(...args),
    markSessionRead: (agentId, sessionId, runId) =>
      rpcMock('session.mark_read', {
        agent_id: agentId,
        session_id: sessionId,
        run_id: runId,
      }),
    listQueue: (...args) => listQueueMock(...args),
    removeFromQueue: (...args) => removeFromQueueMock(...args),
    updateQueueItem: (...args) => updateQueueItemMock(...args),
    cancelRun: (...args) => cancelRunMock(...args),
    cancelToolCall: (...args) => cancelToolCallMock(...args),
    showProject: (...args) => showProjectMock(...args),
  }),
);

// Wrap the real run-stream factory so the wiring test can observe calls to
// `applyConnectionSnapshot` independently of whatever side effects the real
// implementation triggers (sub-agent status updates, `subscribeRunEvents`
// attach, etc.). The wiring assertion is purely "the effect called the run
// stream's `applyConnectionSnapshot` with the snapshot prop", which the spy
// captures cleanly while the real `chatRunStream.js` runs untouched.
//
// The reconcile tests need two more hooks: (1) a `closeSubscriptionFor` spy
// that records the session key the reconcile path passed in, and (2) access
// to the live `chatState` and `runStream` references created inside ChatView
// (so the staleRunId-guard test can mutate `currentRun.runId` while a
// `chat.history` request is in flight).
vi.mock('../../lib/chatRunStream.js', async () => {
  const actual = await vi.importActual('../../lib/chatRunStream.js');
  return {
    ...actual,
    createChatRunStream: (options) => {
      const stream = actual.createChatRunStream(options);
      testChatStateRefs.push(options.chatState);
      testRunStreamRefs.push(stream);
      return {
        ...stream,
        // The spy runs with the real stream as `this`, so a test can opt into
        // the real projection with `mockImplementation(function (s) {
        // return this.applyConnectionSnapshot(s); })`.
        applyConnectionSnapshot: (snapshot) =>
          applyConnectionSnapshotMock.call(stream, snapshot),
        closeSubscriptionFor: (sessionKey) => {
          closeSubscriptionForMock(sessionKey);
          return stream.closeSubscriptionFor(sessionKey);
        },
      };
    },
  };
});

const { default: ChatView } = await import('../ChatView.svelte');

export function setupChatViewTestSuite() {
  let mountedComponent = null;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    subscribeRunEventsMock.mockClear();
    // Restore the default per-call subscription factory: a test that sets
    // `mockReturnValue` (one shared subscription object) would otherwise leak
    // it into every later test and cross-pollute close() assertions.
    subscribeRunEventsMock.mockImplementation(() => ({
      close: vi.fn(),
      source: null,
    }));
    listSessionsMock.mockReset();
    listSessionsMock.mockResolvedValue({ sessions: [] });
    listSessionActivityMock.mockReset();
    listSessionActivityMock.mockResolvedValue({ agents: [] });
    getSessionMock.mockReset();
    getSessionMock.mockResolvedValue({ session: null });
    listQueueMock.mockReset();
    listQueueMock.mockResolvedValue({ items: [] });
    removeFromQueueMock.mockReset();
    removeFromQueueMock.mockResolvedValue({ ok: true });
    updateQueueItemMock.mockReset();
    updateQueueItemMock.mockResolvedValue({ ok: true });
    cancelRunMock.mockReset();
    cancelRunMock.mockResolvedValue({ ok: true });
    cancelToolCallMock.mockReset();
    cancelToolCallMock.mockResolvedValue({ ok: true });
    showProjectMock.mockReset();
    showProjectMock.mockResolvedValue({ project: {}, scan: {} });
    applyConnectionSnapshotMock.mockReset();
    closeSubscriptionForMock.mockReset();
    testChatStateRefs.length = 0;
    testRunStreamRefs.length = 0;
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  return {
    get mountedComponent() {
      return mountedComponent;
    },
    mount(options, Component = ChatView) {
      mountedComponent = mount(Component, options);
      return mountedComponent;
    },
  };
}

export function flushSync() {
  return svelteFlushSync();
}

export { describe, expect, it, vi };

export function createChatRpcMock({
  usage,
  sessionUsage,
  contextUsage,
  contextWindow = 262144,
  sessionMessages,
  activeRuns,
  streamResponse,
  streamHandler,
  commandsError = false,
  commandItems,
  agents,
} = {}) {
  const resolvedAgents = agents ?? [
    createAgent({ context_window: contextWindow }),
  ];
  const resolvedSessionMessages = {
    'session-1': [
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'Hello',
        usage,
      },
    ],
    'sub-session-1': [
      {
        id: 'sub-assistant-one',
        role: 'assistant',
        content: 'Sub-agent response',
      },
    ],
    ...(sessionMessages ?? {}),
  };

  return async (method, params) => {
    if (method === 'agent.list') {
      return { agents: resolvedAgents };
    }

    if (method === 'chat.history') {
      const messages = resolvedSessionMessages[params.session_id];
      if (messages) {
        const beforeIndex = params.before
          ? messages.findIndex((message) => message.id === params.before)
          : messages.length;
        if (beforeIndex < 0) {
          throw new Error(`Unexpected before message id: ${params.before}`);
        }
        const sourceMessages = messages.slice(0, beforeIndex);
        const pageMessages = params.limit
          ? sourceMessages.slice(-params.limit)
          : sourceMessages;
        const response = {
          session_id: params.session_id,
          messages: pageMessages,
          has_more: sourceMessages.length > pageMessages.length,
        };
        if (sessionUsage) {
          response.session_usage = sessionUsage;
        }
        const resolvedContextUsage =
          contextUsage ??
          (usage
            ? {
                tokens: (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0),
                estimated: usage.estimated === true,
              }
            : null);
        if (resolvedContextUsage) {
          response.context_usage = resolvedContextUsage;
        }
        if (activeRuns?.[params.session_id]) {
          response.active_run = activeRuns[params.session_id];
        }
        return response;
      }

      throw new Error(`Unexpected session id: ${params.session_id}`);
    }

    if (method === 'chat.reflections') {
      return { reflection_runs: [] };
    }

    if (method === 'chat.commands') {
      if (commandsError) {
        throw new Error('chat.commands unavailable');
      }
      return {
        items: commandItems ?? [
          {
            name: 'stop',
            description: 'Cancel the active run for this session.',
            type: 'command',
          },
          {
            name: 'debugging',
            description: 'Investigate unclear bugs.',
            type: 'skill',
          },
        ],
      };
    }

    if (method === 'chat.stream') {
      if (typeof streamHandler === 'function') {
        return streamHandler(params ?? {});
      }
      if (streamResponse) {
        return streamResponse;
      }
      throw new Error('Unexpected stream call');
    }

    if (method === 'chat.control_run') {
      return {
        run_id: params.run_id,
        status: 'running',
        sse_url: `/api/runs/${params.run_id}/events`,
        controls: { compaction: 'unavailable', background_tool_call_ids: [] },
        events: [],
      };
    }

    if (method === 'session.create') {
      // Deterministic session id derived from the address so project-agent
      // session-create tests can assert against it. `builder@vbot` →
      // `created-builder@vbot`.
      const agentId =
        typeof params?.agent_id === 'string' ? params.agent_id : '';
      return { agent_id: agentId, session_id: `created-${agentId}` };
    }

    if (method === 'session.mark_read') {
      return {
        agent_id: params.agent_id,
        session_id: params.session_id,
        latest_completion_run_id: params.run_id,
        has_unread_completion: false,
        unread_run_id: null,
        unread_run_status: null,
        unread_run_at: null,
        marked_read: true,
      };
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

export function createHistoryMessages(count) {
  return Array.from({ length: count }, (_item, index) => {
    const number = index + 1;
    return {
      id: `message-${String(number).padStart(3, '0')}`,
      role: 'user',
      content: `History message ${number}`,
    };
  });
}

export function createAgent(overrides = {}) {
  return {
    id: 'alpha',
    name: 'Alpha',
    model: 'openrouter/anthropic/claude-sonnet-4',
    fallback_models: [],
    workspace: 'C:/agents/alpha',
    temperature: '',
    thinking_effort: '',
    tool_access: { mode: 'all' },
    allowed_skills: ['*'],
    current_session_id: 'session-1',
    context_window: 262144,
    created_at: '2026-05-09T00:00:00+00:00',
    updated_at: '2026-05-09T00:00:00+00:00',
    ...overrides,
  };
}

export function findButtonByText(text) {
  return Array.from(document.querySelectorAll('button')).find((button) =>
    button.textContent.includes(text),
  );
}

// The run-level cancel is the icon-only stop button in the composer — no text
// content, so it is matched by its aria-label instead.
export function findCancelRunButton() {
  return Array.from(document.querySelectorAll('button')).find(
    (button) => button.getAttribute('aria-label') === 'Cancel run',
  );
}

// The New Session action is a floating icon-only button (no text content), so
// it is matched by its aria-label instead of visible text.
export function findNewSessionButton() {
  return Array.from(document.querySelectorAll('button')).find(
    (button) => button.getAttribute('aria-label') === 'New session',
  );
}

// Trigger of the Chat header's personal Agent picker. It shows the selected
// Agent's name, or the "Select agent" placeholder while a Project Agent is
// active.
export function agentPickerTrigger(root = document) {
  return root.querySelector(
    '.chat-header__agent-picker button[aria-haspopup="listbox"]',
  );
}

// Name of the personal Agent selected in the picker; '' when none is.
export function selectedPersonalAgentName(root = document) {
  const trigger = agentPickerTrigger(root);
  if (
    !trigger ||
    trigger.querySelector('[class*="trigger-label--placeholder"]')
  ) {
    return '';
  }
  return trigger.textContent.trim();
}

// The activity chip of another personal Agent (running or unread), found by
// the Agent name that starts its accessible label.
export function agentChip(name, root = document) {
  return Array.from(root.querySelectorAll('.agent-chips > button')).find(
    (chip) => chip.getAttribute('aria-label')?.startsWith(`${name}:`),
  );
}

// Selects a personal Agent the way a user does: open the picker, choose the
// option. The picker's list is portaled to <body>.
export async function selectAgentFromPicker(name, root = document) {
  await waitForCondition(
    () => agentPickerTrigger(root)?.disabled === false,
    100,
  );
  agentPickerTrigger(root).click();
  const option = () =>
    Array.from(document.querySelectorAll('[role="option"]')).find((item) =>
      item.getAttribute('aria-label')?.startsWith(`${name}:`),
    );
  await waitForCondition(() => Boolean(option()), 100);
  option().click();
  flushSync();
}

export function setInputValue(input, value) {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

export function sendComposerMessage(content) {
  const composerInput = document.querySelector('.msg-input');
  expect(composerInput).toBeTruthy();
  setInputValue(composerInput, content);
  flushSync();

  const sendButton = document.querySelector('.btn-primary.btn-icon');
  expect(sendButton).toBeTruthy();
  sendButton.click();
  flushSync();
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

// Hovers the context ring and reads its hover card: the headline
// "tokens / context window" and the remaining usage breakdown. Usage data may
// still be streaming in when the ring first renders; the card updates in place.
export async function hoveredContextRingCard() {
  await waitForCondition(
    () => document.body.querySelector('.context-ring') !== null,
    100,
  );
  vi.useFakeTimers();
  const anchor = document.body.querySelector('.context-ring');
  anchor.dispatchEvent(new Event('pointerenter'));
  await vi.advanceTimersByTimeAsync(HOVER_CARD_SHOW_DELAY_MS);
  flushSync();
  const card = document.body.querySelector('.context-card');
  expect(card.dataset.floatingOpen).toBe('true');
  // Sections as { title, meta, rows }; a sub-row (a share of the row above)
  // is prefixed with "· ".
  const content = {
    summary: card.querySelector('.context-card__usage')?.textContent ?? '',
    sections: [...card.querySelectorAll('.context-card__section')].map(
      (section) => ({
        title:
          section
            .querySelector('.context-card__section-title > span')
            ?.textContent.trim() ?? '',
        meta:
          section
            .querySelector('.context-card__section-meta')
            ?.textContent.trim() ?? '',
        rows: [...section.querySelectorAll('.context-card__row')].map(
          (row) =>
            `${row.classList.contains('context-card__row--sub') ? '· ' : ''}${row
              .querySelector('dt')
              .textContent.trim()}: ${row.querySelector('dd').textContent.trim()}`,
        ),
      }),
    ),
  };
  anchor.dispatchEvent(new Event('pointerleave'));
  return content;
}

// Opens the context card without hover intent (a press on the ring) and
// returns its Compaction action.
export function contextCompactionButton() {
  const trigger = document.body.querySelector('.context-ring-trigger');
  trigger.dispatchEvent(new Event('pointerdown', { bubbles: true }));
  flushSync();
  return document.body.querySelector('.context-card .context-card__action');
}

export async function hoveredTooltipText(element, expectedText) {
  element.dispatchEvent(new Event('pointerenter'));
  await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
  flushSync();
  const tooltipText = document.getElementById('app-tooltip')?.textContent ?? '';
  expect(tooltipText).toBe(expectedText);
  element.dispatchEvent(new Event('pointerleave'));
  return tooltipText;
}
