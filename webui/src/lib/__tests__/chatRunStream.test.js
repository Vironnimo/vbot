import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createChatRunStream } from '../chatRunStream.js';
import {
  CHAT_STATUS_IDLE,
  CHAT_STATUS_RUNNING,
  addServerQueuedMessage,
  createChatState,
  ensureSessionState,
  setAgents,
} from '../chatState.js';

function makeStreamHarness({
  chatState,
  displayedAgentId,
  displayedSessionId,
  subscribeRunEvents,
} = {}) {
  const subAgentRunStatuses = {};
  const isDisplayedSession = vi.fn(
    (agentId, sessionId) =>
      agentId === displayedAgentId && sessionId === displayedSessionId,
  );
  const setActionError = vi.fn();
  const syncSessionQueue = vi.fn(async () => {});

  const stream = createChatRunStream({
    chatState,
    subscribeRunEvents:
      subscribeRunEvents ??
      vi.fn(() => ({
        close: vi.fn(),
      })),
    syncSessionQueue,
    isDisplayedSession,
    setActionError,
    updateSubAgentRunStatuses: (updates) => {
      Object.assign(subAgentRunStatuses, updates);
    },
  });

  return {
    stream,
    subAgentRunStatuses,
    isDisplayedSession,
    setActionError,
    syncSessionQueue,
  };
}

describe('createChatRunStream().applyConnectionSnapshot()', () => {
  let chatState;
  const DISPLAYED_AGENT_ID = 'alpha';
  const DISPLAYED_SESSION_ID = 'session-displayed';

  beforeEach(() => {
    chatState = createChatState();
    setAgents(chatState, [
      {
        id: DISPLAYED_AGENT_ID,
        name: 'Alpha',
        current_session_id: DISPLAYED_SESSION_ID,
      },
    ]);
  });

  it('attaches the SSE stream exactly once when the snapshot has one active run for the displayed session and leaves the session state running', () => {
    const subscribeRunEvents = vi.fn(() => ({ close: vi.fn() }));
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
      subscribeRunEvents,
    });

    const snapshot = {
      type: 'connection_ready',
      epoch: 'epoch-1',
      last_sequence: 0,
      active_runs: [
        {
          run_id: 'run-snapshot-1',
          agent_id: DISPLAYED_AGENT_ID,
          session_id: DISPLAYED_SESSION_ID,
          status: 'running',
          sse_url: '/api/runs/run-snapshot-1/events',
        },
      ],
    };

    harness.stream.applyConnectionSnapshot(snapshot);

    expect(subscribeRunEvents).toHaveBeenCalledTimes(1);
    expect(subscribeRunEvents).toHaveBeenCalledWith(
      '/api/runs/run-snapshot-1/events',
      expect.objectContaining({
        onEvent: expect.any(Function),
        onError: expect.any(Function),
      }),
      expect.objectContaining({ afterSequence: expect.any(Number) }),
    );

    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );
    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.currentRun?.runId).toBe('run-snapshot-1');
    expect(sessionState.currentRun?.sseUrl).toBe(
      '/api/runs/run-snapshot-1/events',
    );
  });

  it('records sub-agent run/session running entries without opening any SSE stream when active runs are in other sessions only', () => {
    const subscribeRunEvents = vi.fn(() => ({ close: vi.fn() }));
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
      subscribeRunEvents,
    });

    const snapshot = {
      type: 'connection_ready',
      epoch: 'epoch-2',
      last_sequence: 0,
      active_runs: [
        {
          run_id: 'run-child-1',
          agent_id: 'beta',
          session_id: 'session-child-1',
          status: 'running',
          sse_url: '/api/runs/run-child-1/events',
        },
        {
          run_id: 'run-child-2',
          agent_id: 'gamma',
          session_id: 'session-child-2',
          status: 'running',
          sse_url: '/api/runs/run-child-2/events',
        },
      ],
    };

    harness.stream.applyConnectionSnapshot(snapshot);

    expect(subscribeRunEvents).not.toHaveBeenCalled();
    expect(harness.subAgentRunStatuses).toEqual({
      'run:run-child-1': 'running',
      'session:beta::session-child-1': 'running',
      'run:run-child-2': 'running',
      'session:gamma::session-child-2': 'running',
    });
    expect(harness.isDisplayedSession).toHaveBeenCalledWith(
      'beta',
      'session-child-1',
    );
    expect(harness.isDisplayedSession).toHaveBeenCalledWith(
      'gamma',
      'session-child-2',
    );
  });

  it('regression for B11: a connection_ready with empty active_runs and no replayed run_started events opens zero subscriptions and leaves the session idle without an action error', () => {
    const subscribeRunEvents = vi.fn(() => ({ close: vi.fn() }));
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
      subscribeRunEvents,
    });

    // Pre-create the displayed session state so the "idle" assertion has
    // something concrete to inspect.
    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );
    expect(sessionState.status).toBe(CHAT_STATUS_IDLE);

    const snapshot = {
      type: 'connection_ready',
      epoch: 'epoch-3',
      last_sequence: 0,
      active_runs: [],
    };

    harness.stream.applyConnectionSnapshot(snapshot);

    // The only way `setActionError` could be called here is via
    // `recoverRunStream`, which only fires after a subscription error.
    // No subscription was opened, so the action-error path is unreachable.
    expect(subscribeRunEvents).not.toHaveBeenCalled();
    expect(harness.setActionError).not.toHaveBeenCalled();
    expect(harness.subAgentRunStatuses).toEqual({});
    expect(sessionState.status).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.currentRun).toBeNull();
  });
});

describe('createChatRunStream() queue removal on run_started (regression for B7)', () => {
  let chatState;
  const DISPLAYED_AGENT_ID = 'alpha';
  const DISPLAYED_SESSION_ID = 'session-displayed';
  const QUEUED_ITEM_ID = 'queue-item-42';
  const DRAINED_RUN_ID = 'run-drained-1';

  beforeEach(() => {
    chatState = createChatState();
    setAgents(chatState, [
      {
        id: DISPLAYED_AGENT_ID,
        name: 'Alpha',
        current_session_id: DISPLAYED_SESSION_ID,
      },
    ]);
  });

  it('removes the queued item from sessionState.queue when a WS run_started event carries its queue_item_id, without any chat.queue_list round-trip', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });

    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );
    addServerQueuedMessage(sessionState, {
      id: QUEUED_ITEM_ID,
      content: 'queued work to drain',
      created_at: '2026-06-10T00:00:00+00:00',
    });
    expect(sessionState.queue.map((item) => item.id)).toEqual([QUEUED_ITEM_ID]);

    // WS server-event envelope: the bridge includes the run-event payload
    // under `payload.output` (see Phase 2.3 Task 2). The run-event
    // `run_started` itself carries the queue_item_id the server added in
    // _start_run_locked.
    harness.stream.handleServerEvents({
      type: 'run_started',
      payload: {
        run_id: DRAINED_RUN_ID,
        agent_id: DISPLAYED_AGENT_ID,
        session_id: DISPLAYED_SESSION_ID,
        run_event_type: 'run_started',
        run_event_sequence: 1,
        status: 'running',
        output: {
          status: 'running',
          queue_item_id: QUEUED_ITEM_ID,
        },
      },
    });

    expect(sessionState.queue).toEqual([]);
    // The queue removal happens on the run_started branch; the
    // `syncSessionQueue` round-trip is the terminal-event backstop and
    // must not fire for a non-terminal run_started.
    expect(harness.syncSessionQueue).not.toHaveBeenCalled();
  });

  it('removes the queued item when an SSE run_started event carries its queue_item_id, without any chat.queue_list round-trip', () => {
    let capturedOnEvent = null;
    const subscribeRunEvents = vi.fn((_sseUrl, handlers) => {
      capturedOnEvent = handlers.onEvent;
      return { close: vi.fn() };
    });
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
      subscribeRunEvents,
    });

    // Apply a snapshot with one active run for the displayed session so
    // the SSE path is wired up; that path forwards raw run-event
    // payloads (not WS envelopes) into the handler.
    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      epoch: 'epoch-b7',
      last_sequence: 0,
      active_runs: [
        {
          run_id: DRAINED_RUN_ID,
          agent_id: DISPLAYED_AGENT_ID,
          session_id: DISPLAYED_SESSION_ID,
          status: 'running',
          sse_url: '/api/runs/run-drained-1/events',
        },
      ],
    });
    expect(typeof capturedOnEvent).toBe('function');

    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );
    addServerQueuedMessage(sessionState, {
      id: QUEUED_ITEM_ID,
      content: 'queued work to drain',
      created_at: '2026-06-10T00:00:00+00:00',
    });
    expect(sessionState.queue.map((item) => item.id)).toEqual([QUEUED_ITEM_ID]);

    // SSE delivers the raw run event payload.
    capturedOnEvent({
      data: {
        type: 'run_started',
        run_id: DRAINED_RUN_ID,
        agent_id: DISPLAYED_AGENT_ID,
        session_id: DISPLAYED_SESSION_ID,
        sequence: 1,
        payload: {
          status: 'running',
          queue_item_id: QUEUED_ITEM_ID,
        },
      },
    });

    expect(sessionState.queue).toEqual([]);
    expect(harness.syncSessionQueue).not.toHaveBeenCalled();
  });

  it('leaves the queue untouched when a run_started event has no queue_item_id', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });

    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );
    addServerQueuedMessage(sessionState, {
      id: QUEUED_ITEM_ID,
      content: 'queued work',
      created_at: '2026-06-10T00:00:00+00:00',
    });

    harness.stream.handleServerEvents({
      type: 'run_started',
      payload: {
        run_id: DRAINED_RUN_ID,
        agent_id: DISPLAYED_AGENT_ID,
        session_id: DISPLAYED_SESSION_ID,
        run_event_type: 'run_started',
        run_event_sequence: 1,
        status: 'running',
        output: {
          status: 'running',
        },
      },
    });

    expect(sessionState.queue.map((item) => item.id)).toEqual([QUEUED_ITEM_ID]);
    expect(harness.syncSessionQueue).not.toHaveBeenCalled();
  });
});
