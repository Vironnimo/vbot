import { vi } from 'vitest';
import { createChatState, ensureSessionState } from '../chatState.js';
import { createChatRunStream } from '../chatRunStream.js';

export const DISPLAYED_AGENT_ID = 'alpha';
export const DISPLAYED_SESSION_ID = 'session-displayed';

// A Run stream over a fake SSE transport. Every opened subscription is
// recorded with its URL, handlers, options and close spy; Sub-Agent status
// updates land in `subAgentRunStatuses` the way the Chat view merges them.
export function makeStreamHarness({
  chatState = createChatState(),
  displayedAgentId = DISPLAYED_AGENT_ID,
  displayedSessionId = DISPLAYED_SESSION_ID,
  isDisplayedSession = (agentId, sessionId) =>
    agentId === displayedAgentId && sessionId === displayedSessionId,
  reconcileRunSession = vi.fn(async () => true),
  reportStreamDiagnostic = vi.fn(),
  onReflectionFinished = vi.fn(),
} = {}) {
  const subAgentRunStatuses = {};
  const subscriptions = [];
  const refreshSessionQueue = vi.fn(async () => {});

  const stream = createChatRunStream({
    chatState,
    subscribeRunEvents: (sseUrl, handlers, options) => {
      const subscription = { sseUrl, handlers, options, close: vi.fn() };
      subscriptions.push(subscription);
      return subscription;
    },
    refreshSessionQueue,
    reconcileRunSession,
    reportStreamDiagnostic,
    onReflectionFinished,
    isDisplayedSession,
    updateSubAgentRunStatuses: (updates, { replaceActive = false } = {}) => {
      if (replaceActive) {
        for (const [key, value] of Object.entries(subAgentRunStatuses)) {
          if (
            (key.startsWith('run:') || key.startsWith('session:')) &&
            (value === 'running' || value === 'queued')
          ) {
            delete subAgentRunStatuses[key];
          }
        }
      }
      Object.assign(subAgentRunStatuses, updates);
    },
  });

  return {
    stream,
    chatState,
    subscriptions,
    subAgentRunStatuses,
    reconcileRunSession,
    reportStreamDiagnostic,
    refreshSessionQueue,
    displayedSession: () =>
      ensureSessionState(chatState, displayedAgentId, displayedSessionId),
    // Delivers raw Run events through the newest SSE subscription.
    sse: (...events) => {
      for (const data of events)
        subscriptions.at(-1).handlers.onEvent({ type: data.type, data });
    },
    // Hands WebSocket Run server events over in feed order.
    ws: (...serverEvents) => {
      for (const serverEvent of serverEvents)
        stream.handleServerEvent(serverEvent);
    },
  };
}

const ENVELOPE_TYPES = new Set([
  'run_started',
  'run_completed',
  'run_failed',
  'run_cancelled',
  'run_interrupted',
]);

// The WebSocket server event that mirrors one Run event: lifecycle events
// travel under their own type, every other Run event as `run_output`.
export function serverRunEvent(runEventType, sequence, fields = {}) {
  return {
    type: ENVELOPE_TYPES.has(runEventType) ? runEventType : 'run_output',
    payload: {
      run_event_type: runEventType,
      run_event_sequence: sequence,
      ...fields,
    },
  };
}

// A connection snapshot announcing active Runs of the displayed Session
// (fields override the defaults per Run).
export function activeRun(runId, fields = {}) {
  return {
    run_id: runId,
    agent_id: DISPLAYED_AGENT_ID,
    session_id: DISPLAYED_SESSION_ID,
    status: 'running',
    sse_url: `/api/runs/${runId}/events`,
    ...fields,
  };
}

// Explicit server projections: tests exercise transport/application, never
// reconstruct the server's semantic grouping from raw Model events.
export function timelineSnapshot(
  runId,
  sequence = 10,
  { content = 'Hello', status = 'running', state = {} } = {},
) {
  return {
    type: 'timeline_snapshot',
    run_id: runId,
    sequence,
    items: [
      {
        id: `assistant-run-${runId}`,
        type: 'assistant_run',
        runId,
        run_id: runId,
        source: 'live',
        status,
        items: [
          {
            id: `${runId}-output`,
            type: 'assistant_output',
            content,
            streaming: status === 'running',
          },
        ],
      },
    ],
    state: { status, ...state },
    terminal: status !== 'running',
  };
}

export function timelinePatch(
  runId,
  baseSequence,
  { content = '!', status = 'running', state = {} } = {},
) {
  return {
    type: 'timeline_update',
    run_id: runId,
    sequence: baseSequence + 1,
    base_sequence: baseSequence,
    run: { id: `assistant-run-${runId}`, status },
    child_updates: [
      {
        id: `${runId}-output`,
        append: { content },
        set: { streaming: status === 'running' },
      },
    ],
    state: { status, ...state },
    terminal: status !== 'running',
  };
}
