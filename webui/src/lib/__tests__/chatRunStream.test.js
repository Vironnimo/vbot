import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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

describe('createChatRunStream() SSE reconnect budget (regression for B2)', () => {
  let chatState;
  const DISPLAYED_AGENT_ID = 'alpha';
  const DISPLAYED_SESSION_ID = 'session-displayed';
  const RUN_ID = 'run-reconnect-1';

  beforeEach(() => {
    vi.useFakeTimers();
    // Pin reconnect jitter to its midpoint so the backoff delay equals the
    // base delay exactly, keeping the timing assertions below deterministic.
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
    chatState = createChatState();
    setAgents(chatState, [
      {
        id: DISPLAYED_AGENT_ID,
        name: 'Alpha',
        current_session_id: DISPLAYED_SESSION_ID,
      },
    ]);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function setupRunningStream() {
    const subscriptions = [];
    const subscribeRunEvents = vi.fn((sseUrl, handlers, options) => {
      const subscription = { sseUrl, handlers, options, close: vi.fn() };
      subscriptions.push(subscription);
      return subscription;
    });
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
      subscribeRunEvents,
    });
    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      epoch: 'epoch-b2',
      last_sequence: 0,
      active_runs: [
        {
          run_id: RUN_ID,
          agent_id: DISPLAYED_AGENT_ID,
          session_id: DISPLAYED_SESSION_ID,
          status: 'running',
          sse_url: `/api/runs/${RUN_ID}/events`,
        },
      ],
    });
    expect(subscriptions).toHaveLength(1);
    return { subscriptions, harness };
  }

  function runEvent(sequence) {
    return {
      data: {
        type: 'tool_call_started',
        run_id: RUN_ID,
        agent_id: DISPLAYED_AGENT_ID,
        session_id: DISPLAYED_SESSION_ID,
        sequence,
        payload: {},
      },
    };
  }

  it('resets the reconnect budget once events flow again, so transient drops spread over a run never exhaust it', () => {
    const { subscriptions } = setupRunningStream();

    // More drops than MAX_SSE_RECONNECT_ATTEMPTS, each preceded by a
    // successfully delivered event. With the per-run accumulating counter
    // this gave up on the 4th drop; with the reset every drop is attempt 0
    // and reconnects after the base 500ms delay.
    for (let drop = 0; drop < 5; drop += 1) {
      const subscription = subscriptions[subscriptions.length - 1];
      subscription.handlers.onEvent(runEvent(drop + 1));
      subscription.handlers.onError(new Error('transient drop'));
      vi.advanceTimersByTime(500);
      expect(subscriptions).toHaveLength(drop + 2);
    }
  });

  it('gives up only after consecutive failed reconnects, with exponential backoff between attempts', () => {
    const { subscriptions, harness } = setupRunningStream();

    // Attempt 0 → 500ms delay.
    subscriptions[0].handlers.onError(new Error('drop'));
    vi.advanceTimersByTime(499);
    expect(subscriptions).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(subscriptions).toHaveLength(2);

    // Attempt 1 → 1000ms delay.
    subscriptions[1].handlers.onError(new Error('drop'));
    vi.advanceTimersByTime(999);
    expect(subscriptions).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(subscriptions).toHaveLength(3);

    // Attempt 2 → 2000ms delay.
    subscriptions[2].handlers.onError(new Error('drop'));
    vi.advanceTimersByTime(1999);
    expect(subscriptions).toHaveLength(3);
    vi.advanceTimersByTime(1);
    expect(subscriptions).toHaveLength(4);

    // Attempt 3 hits MAX_SSE_RECONNECT_ATTEMPTS → permanent close.
    harness.setActionError.mockClear();
    subscriptions[3].handlers.onError(new Error('drop'));
    vi.runAllTimers();
    expect(subscriptions).toHaveLength(4);
    expect(subscriptions[3].close).toHaveBeenCalled();
    expect(harness.setActionError).toHaveBeenCalledTimes(1);
    expect(harness.setActionError.mock.calls[0][0]).not.toContain(
      'Reconnecting',
    );
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

  it('records a queueRun mapping on run_started so queued sub-agent rows resolve their own run id (B6)', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
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
          queue_item_id: QUEUED_ITEM_ID,
        },
      },
    });

    expect(harness.subAgentRunStatuses[`queueRun:${QUEUED_ITEM_ID}`]).toBe(
      DRAINED_RUN_ID,
    );
    expect(harness.subAgentRunStatuses[`run:${DRAINED_RUN_ID}`]).toBe(
      'running',
    );
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

describe('createChatRunStream() last-tool-name tracking for sub-agent rows', () => {
  let chatState;
  const DISPLAYED_AGENT_ID = 'alpha';
  const DISPLAYED_SESSION_ID = 'session-displayed';
  const CHILD_AGENT_ID = 'child-agent';
  const CHILD_SESSION_ID = 'session-child';
  const CHILD_RUN_ID = 'run-child-7';

  const childToolCallStartedEvent = (toolName, sequence = 2) => ({
    type: 'run_output',
    payload: {
      run_id: CHILD_RUN_ID,
      agent_id: CHILD_AGENT_ID,
      session_id: CHILD_SESSION_ID,
      run_event_type: 'tool_call_started',
      run_event_sequence: sequence,
      output: {
        tool_call: {
          id: `call-${sequence}`,
          index: 0,
          name: toolName,
          arguments: {},
        },
      },
    },
  });

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

  it('records runTool and sessionTool entries from a bridged child tool_call_started event, keeping only the latest name', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });

    harness.stream.handleServerEvents(childToolCallStartedEvent('read', 2));
    expect(harness.subAgentRunStatuses[`runTool:${CHILD_RUN_ID}`]).toBe('read');
    expect(
      harness.subAgentRunStatuses[
        `sessionTool:${CHILD_AGENT_ID}::${CHILD_SESSION_ID}`
      ],
    ).toBe('read');

    harness.stream.handleServerEvents(childToolCallStartedEvent('bash', 5));
    expect(harness.subAgentRunStatuses[`runTool:${CHILD_RUN_ID}`]).toBe('bash');
    expect(
      harness.subAgentRunStatuses[
        `sessionTool:${CHILD_AGENT_ID}::${CHILD_SESSION_ID}`
      ],
    ).toBe('bash');
  });

  it('records no tool entries when the tool_call_started payload has no usable name', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });

    const event = childToolCallStartedEvent('  ', 2);
    harness.stream.handleServerEvents(event);

    expect(harness.subAgentRunStatuses).toEqual({});
  });

  it('clears the session-scoped tool name when a new run starts in the same child session', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });

    harness.stream.handleServerEvents(childToolCallStartedEvent('bash', 2));
    expect(
      harness.subAgentRunStatuses[
        `sessionTool:${CHILD_AGENT_ID}::${CHILD_SESSION_ID}`
      ],
    ).toBe('bash');

    harness.stream.handleServerEvents({
      type: 'run_started',
      payload: {
        run_id: 'run-child-8',
        agent_id: CHILD_AGENT_ID,
        session_id: CHILD_SESSION_ID,
        run_event_type: 'run_started',
        run_event_sequence: 1,
        status: 'running',
        output: { status: 'running' },
      },
    });

    expect(
      harness.subAgentRunStatuses[
        `sessionTool:${CHILD_AGENT_ID}::${CHILD_SESSION_ID}`
      ],
    ).toBe('');
    // The previous run's run-scoped entry stays untouched; rows resolve it
    // strictly by run id, so it cannot leak into the new run's row.
    expect(harness.subAgentRunStatuses[`runTool:${CHILD_RUN_ID}`]).toBe('bash');
  });
});
