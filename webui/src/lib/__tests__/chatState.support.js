import { vi } from 'vitest';
import {
  appendRunEvent,
  createChatController,
  createChatState,
} from '../chatState.js';

// Chat controller over fake operations and a fake Run stream. `translate`
// returns the i18n key, so messages are asserted through their key.
export function setupController({
  operationOverrides = {},
  isDisplayedSession = () => false,
  shouldLoadCurrentHistory = () => true,
} = {}) {
  const chatState = createChatState();
  const runStream = {
    applyConnectionSnapshot: vi.fn(),
    attachRunStream: vi.fn(),
    closeSubscriptionFor: vi.fn(),
    closeSubscriptions: vi.fn(),
    closeSubscriptionsExcept: vi.fn(),
    handleServerEvents: vi.fn(),
    mergeRunResponse: vi.fn(),
    subscribeToRun: vi.fn(),
  };
  const listQueue = vi.fn().mockResolvedValue({
    items: [{ id: 'queued-one', content: 'Next', editable: true }],
  });
  const onRestartQueueDiscarded = vi.fn();
  const onAgentsChanged = vi.fn();
  const onAgentSelected = vi.fn();
  const controller = createChatController({
    chatState,
    runStream,
    operations: {
      listQueue,
      loadReflectionRuns: vi.fn().mockResolvedValue({ reflection_runs: [] }),
      ...operationOverrides,
    },
    translate: (key) => key,
    isDisplayedSession,
    shouldLoadCurrentHistory,
    onRestartQueueDiscarded,
    onAgentsChanged,
    onAgentSelected,
  });
  return {
    chatState,
    controller,
    listQueue,
    onRestartQueueDiscarded,
    onAgentsChanged,
    onAgentSelected,
    runStream,
  };
}

export function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

// A Reflection Run row as History and reflection reads report it.
export function reflectionRun(status = 'completed', runId = 'review-one') {
  return {
    run_id: runId,
    session_id: 'review-session',
    run_kind: 'memory_reflection',
    status,
    started_at: '2026-09-05T10:00:00Z',
  };
}

export function countTimelineTextOccurrences(timelineItems, text) {
  let count = 0;
  for (const item of timelineItems) {
    if (item.type === 'message' && item.message?.content === text) {
      count += 1;
      continue;
    }
    if (item.type === 'assistant_run') {
      count += (item.outputs ?? []).filter(
        (output) => output.content === text,
      ).length;
    }
  }
  return count;
}

// A reported two-Tool Run (glob, then read, then a final answer) as persisted
// History; appendReportedLiveRunEvents streams the same Run live.
export function reportedMultiStepMessages() {
  return [
    {
      id: 'user-reported',
      role: 'user',
      content: 'Investigate the duplicated chat UI.',
      timestamp: '2026-05-08T10:00:00Z',
    },
    {
      id: 'assistant-glob',
      role: 'assistant',
      reasoning: 'Find candidate files.',
      timestamp: '2026-05-08T10:00:01Z',
      tool_calls: [
        {
          id: 'call-glob',
          name: 'glob',
          arguments: { pattern: 'webui/src/**/*.js' },
        },
      ],
    },
    {
      id: 'tool-glob',
      role: 'tool',
      tool_call_id: 'call-glob',
      name: 'glob',
      content: '{"ok":true,"data":{"content":"webui/src/lib/chatState.js"}}',
      timestamp: '2026-05-08T10:00:02Z',
    },
    {
      id: 'assistant-read',
      role: 'assistant',
      content: 'I found the timeline helper; now I will read it.',
      reasoning: 'Read the selected file.',
      timestamp: '2026-05-08T10:00:03Z',
      tool_calls: [
        {
          id: 'call-read',
          name: 'read',
          arguments: { path: 'webui/src/lib/chatState.js' },
        },
      ],
    },
    {
      id: 'tool-read',
      role: 'tool',
      tool_call_id: 'call-read',
      name: 'read',
      content: '{"ok":true,"data":{"content":"timeline code"}}',
      timestamp: '2026-05-08T10:00:04Z',
    },
    {
      id: 'assistant-final',
      role: 'assistant',
      content: 'The timeline is in chatState.js.',
      reasoning: 'Summarize the result.',
      timestamp: '2026-05-08T10:00:05Z',
    },
  ];
}

export function appendReportedLiveRunEvents(
  sessionState,
  runId,
  startSequence = 1,
) {
  const glob = { id: 'call-glob', index: 0, name: 'glob' };
  const read = { id: 'call-read', index: 0, name: 'read' };
  const readArguments = { path: 'webui/src/lib/chatState.js' };
  const events = [
    ['reasoning_delta', { reasoning_delta: 'Find candidate files.' }],
    [
      'tool_call_started',
      { tool_call: { ...glob, arguments: { pattern: 'webui/src/**/*.js' } } },
    ],
    [
      'tool_call_result',
      {
        tool_call: glob,
        result: { ok: true, data: { content: 'webui/src/lib/chatState.js' } },
      },
    ],
    ['reasoning_delta', { reasoning_delta: 'Read the selected file.' }],
    [
      'assistant_output_delta',
      { content_delta: 'I found the timeline helper; now I will read it.' },
    ],
    ['tool_call_started', { tool_call: { ...read, arguments: readArguments } }],
    [
      'tool_call_result',
      {
        tool_call: read,
        result: { ok: true, data: { content: 'timeline code' } },
      },
    ],
    [
      'assistant_output',
      {
        message: {
          id: 'assistant-read',
          role: 'assistant',
          content: 'I found the timeline helper; now I will read it.',
          reasoning: 'Read the selected file.',
          tool_calls: [
            { id: 'call-read', name: 'read', arguments: readArguments },
          ],
        },
      },
    ],
    ['reasoning_delta', { reasoning_delta: 'Summarize the result.' }],
    [
      'assistant_output_delta',
      { content_delta: 'The timeline is in chatState.js.' },
    ],
    [
      'assistant_output',
      {
        message: {
          id: 'assistant-final',
          role: 'assistant',
          content: 'The timeline is in chatState.js.',
          reasoning: 'Summarize the result.',
        },
      },
    ],
  ];
  events.forEach(([type, payload], offset) => {
    appendRunEvent(sessionState, {
      type,
      run_id: runId,
      sequence: startSequence + offset,
      payload: structuredClone(payload),
    });
  });
}
