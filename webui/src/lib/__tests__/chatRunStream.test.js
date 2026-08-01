import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createChatRunStream } from '../chatRunStream.js';
import {
  CHAT_STATUS_IDLE,
  CHAT_STATUS_CANCELLED,
  CHAT_STATUS_RUNNING,
  agentActivityStatus,
  addServerQueuedMessage,
  createChatState,
  ensureSessionState,
  resetStaleRun,
  setAgents,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';

function makeStreamHarness({
  chatState,
  displayedAgentId,
  displayedSessionId,
  subscribeRunEvents,
  reconcileRunSession = vi.fn(async () => true),
} = {}) {
  const subAgentRunStatuses = {};
  const isDisplayedSession = vi.fn(
    (agentId, sessionId) =>
      agentId === displayedAgentId && sessionId === displayedSessionId,
  );
  const syncSessionQueue = vi.fn(async () => {});

  const stream = createChatRunStream({
    chatState,
    subscribeRunEvents:
      subscribeRunEvents ??
      vi.fn(() => ({
        close: vi.fn(),
      })),
    syncSessionQueue,
    reconcileRunSession,
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
    subAgentRunStatuses,
    isDisplayedSession,
    reconcileRunSession,
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

  it('reattaches an excluded active Run without projecting Agent activity', () => {
    const subscribeRunEvents = vi.fn(() => ({ close: vi.fn() }));
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
      subscribeRunEvents,
    });

    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      active_runs: [
        {
          run_id: 'run-system',
          agent_id: DISPLAYED_AGENT_ID,
          session_id: DISPLAYED_SESSION_ID,
          status: 'running',
          sse_url: '/api/runs/run-system/events',
          contributes_to_agent_activity: false,
        },
      ],
    });

    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );
    expect(subscribeRunEvents).toHaveBeenCalledOnce();
    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.currentRun?.contributesToAgentActivity).toBe(false);
    expect(agentActivityStatus(chatState, DISPLAYED_AGENT_ID)).toBe('idle');
    expect(harness.subAgentRunStatuses).toEqual({});
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
    expect(agentActivityStatus(chatState, 'beta')).toBe('running');
    expect(agentActivityStatus(chatState, 'gamma')).toBe('running');
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

    // Stream warnings belong to their Session and only appear after a
    // subscription error. No subscription was opened here.
    expect(subscribeRunEvents).not.toHaveBeenCalled();
    expect(sessionState.streamError).toBe('');
    expect(harness.subAgentRunStatuses).toEqual({});
    expect(sessionState.status).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.currentRun).toBeNull();
  });

  it('reconciles a locally running Run that is absent from the authoritative reconnect snapshot against durable history', async () => {
    const subscriptions = [];
    const subscribeRunEvents = vi.fn(() => {
      const subscription = { close: vi.fn() };
      subscriptions.push(subscription);
      return subscription;
    });
    const reconcileRunSession = vi.fn(async (staleSessionState) => {
      resetStaleRun(staleSessionState);
      return true;
    });
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
      subscribeRunEvents,
      reconcileRunSession,
    });
    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );

    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      active_runs: [
        {
          run_id: 'run-before-restart',
          agent_id: DISPLAYED_AGENT_ID,
          session_id: DISPLAYED_SESSION_ID,
          status: 'running',
          sse_url: '/api/runs/run-before-restart/events',
        },
      ],
    });
    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);

    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      active_runs: [],
    });

    await vi.waitFor(() => {
      expect(reconcileRunSession).toHaveBeenCalledWith(
        sessionState,
        'run-before-restart',
      );
    });
    expect(sessionState.status).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.currentRun).toBeNull();
    expect(subscriptions[0].close).toHaveBeenCalledOnce();
  });

  it('replaces stale active sub-agent statuses while preserving terminal metadata', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });
    Object.assign(harness.subAgentRunStatuses, {
      'run:stale-run': 'running',
      'session:child::stale-session': 'queued',
      'run:completed-run': 'completed',
      'runDuration:completed-run': 1250,
      'queueRun:queue-one': 'completed-run',
    });

    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      active_runs: [],
    });

    expect(harness.subAgentRunStatuses).toEqual({
      'run:completed-run': 'completed',
      'runDuration:completed-run': 1250,
      'queueRun:queue-one': 'completed-run',
    });
  });

  it('preserves the server failure message when WebSocket is the terminal-event backstop', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });

    harness.stream.handleServerEvents({
      type: 'run_failed',
      payload: {
        run_id: 'run-failed-1',
        agent_id: DISPLAYED_AGENT_ID,
        session_id: DISPLAYED_SESSION_ID,
        run_event_type: 'run_failed',
        run_event_sequence: 2,
        status: 'failed',
        error: 'Provider request failed',
      },
    });

    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );
    expect(sessionState.error).toBe('Provider request failed');
  });

  it('keeps excluded WebSocket lifecycle events out of Agent activity', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });
    const basePayload = {
      run_id: 'run-system',
      agent_id: DISPLAYED_AGENT_ID,
      session_id: DISPLAYED_SESSION_ID,
      contributes_to_agent_activity: false,
    };

    harness.stream.handleServerEvents({
      type: 'run_started',
      payload: {
        ...basePayload,
        run_event_type: 'run_started',
        run_event_sequence: 1,
        output: { status: 'running' },
      },
    });

    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );
    expect(sessionState.currentRun?.contributesToAgentActivity).toBe(false);
    expect(agentActivityStatus(chatState, DISPLAYED_AGENT_ID)).toBe('idle');

    harness.stream.handleServerEvents({
      type: 'run_completed',
      payload: {
        ...basePayload,
        run_event_type: 'run_completed',
        run_event_sequence: 2,
        status: 'completed',
        context_usage: {
          tokens: 155489,
          estimated: true,
          provider_input_tokens: 154731,
          provider_output_tokens: 243,
          estimated_delta_tokens: 515,
        },
      },
    });

    expect(sessionState.contextUsage).toEqual({
      tokens: 155489,
      estimated: true,
      provider_input_tokens: 154731,
      provider_output_tokens: 243,
      estimated_delta_tokens: 515,
    });
    expect(sessionState.hasUnreadCompletion).toBe(false);
    expect(agentActivityStatus(chatState, DISPLAYED_AGENT_ID)).toBe('idle');
    expect(harness.subAgentRunStatuses).toEqual({});
  });
});

describe('createChatRunStream().mergeRunResponse()', () => {
  it('settles the active Run and its open Sub-Agent from the cancel RPC response', () => {
    const chatState = createChatState();
    const close = vi.fn();
    const subscribeRunEvents = vi.fn(() => ({ close }));
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: 'orchestrator@project-one',
      displayedSessionId: 'session-one',
      subscribeRunEvents,
    });
    const sessionState = ensureSessionState(
      chatState,
      'orchestrator@project-one',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-parent',
      status: CHAT_STATUS_RUNNING,
      sse_url: '/api/runs/run-parent/events',
    });
    harness.stream.subscribeToRun(sessionState, '/api/runs/run-parent/events');

    expect(
      harness.stream.mergeRunResponse(sessionState, {
        run_id: 'run-parent',
        status: CHAT_STATUS_CANCELLED,
        events: [
          {
            type: 'run_started',
            run_id: 'run-parent',
            agent_id: 'orchestrator',
            session_id: 'session-one',
            sequence: 1,
            payload: { status: CHAT_STATUS_RUNNING },
          },
          {
            type: 'tool_call_started',
            run_id: 'run-parent',
            agent_id: 'orchestrator',
            session_id: 'session-one',
            sequence: 2,
            payload: {
              tool_call: {
                id: 'call-subagent',
                index: 0,
                name: 'subagent',
                arguments: {
                  agent_id: 'planner',
                  background: false,
                  content: 'Create the plan',
                },
              },
            },
          },
          {
            type: 'run_cancelled',
            run_id: 'run-parent',
            agent_id: 'orchestrator',
            session_id: 'session-one',
            sequence: 3,
            payload: { status: CHAT_STATUS_CANCELLED },
          },
        ],
      }),
    ).toBe(true);

    expect(sessionState.status).toBe(CHAT_STATUS_CANCELLED);
    expect(sessionState.currentRun?.status).toBe(CHAT_STATUS_CANCELLED);
    expect(close).toHaveBeenCalledOnce();
    expect(harness.syncSessionQueue).toHaveBeenCalledOnce();
    expect(visibleTimelineItemsForRender(sessionState)[0].tools[0].status).toBe(
      CHAT_STATUS_CANCELLED,
    );

    harness.stream.mergeRunResponse(sessionState, {
      run_id: 'run-parent',
      status: CHAT_STATUS_CANCELLED,
      events: [...sessionState.runEvents],
    });

    expect(sessionState.runEvents).toHaveLength(3);
    expect(close).toHaveBeenCalledOnce();
  });

  it('does not let a delayed cancel response overwrite a newer Run', () => {
    const chatState = createChatState();
    const harness = makeStreamHarness({ chatState });
    const sessionState = ensureSessionState(
      chatState,
      'orchestrator',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-new',
      status: CHAT_STATUS_RUNNING,
      sse_url: '/api/runs/run-new/events',
    });

    expect(
      harness.stream.mergeRunResponse(sessionState, {
        run_id: 'run-old',
        status: CHAT_STATUS_CANCELLED,
        events: [
          {
            type: 'run_cancelled',
            run_id: 'run-old',
            sequence: 3,
            payload: { status: CHAT_STATUS_CANCELLED },
          },
        ],
      }),
    ).toBe(false);

    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.currentRun).toEqual(
      expect.objectContaining({
        runId: 'run-new',
        status: CHAT_STATUS_RUNNING,
      }),
    );
    expect(sessionState.runEvents).toEqual([]);
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

  function setupRunningStream({ reconcileRunSession } = {}) {
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
      reconcileRunSession,
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
    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );
    return { subscriptions, harness, sessionState };
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
    const { subscriptions, sessionState } = setupRunningStream();

    // More drops than MAX_SSE_RECONNECT_ATTEMPTS, each preceded by a
    // successfully delivered event. With the per-run accumulating counter
    // this gave up on the 4th drop; with the reset every drop is attempt 0
    // and reconnects after the base 500ms delay.
    for (let drop = 0; drop < 5; drop += 1) {
      const subscription = subscriptions[subscriptions.length - 1];
      subscription.handlers.onEvent(runEvent(drop + 1));
      expect(sessionState.streamError).toBe('');
      subscription.handlers.onError(new Error('transient drop'));
      expect(sessionState.streamError).toContain('Reconnecting');
      vi.advanceTimersByTime(500);
      expect(subscriptions).toHaveLength(drop + 2);
    }
  });

  it('uses transport heartbeats to detect a silently stalled EventSource connection', () => {
    const { subscriptions, sessionState } = setupRunningStream();

    vi.advanceTimersByTime(25_000);

    expect(subscriptions[0].close).toHaveBeenCalledOnce();
    expect(sessionState.streamError).toContain('Reconnecting');
    vi.advanceTimersByTime(500);
    expect(subscriptions).toHaveLength(2);
  });

  it('refreshes the stall watchdog when a heartbeat arrives', () => {
    const { subscriptions } = setupRunningStream();

    vi.advanceTimersByTime(20_000);
    subscriptions[0].handlers.onHeartbeat();
    vi.advanceTimersByTime(20_000);
    expect(subscriptions).toHaveLength(1);
    expect(subscriptions[0].close).not.toHaveBeenCalled();

    vi.advanceTimersByTime(5_000);
    expect(subscriptions[0].close).toHaveBeenCalledOnce();
  });

  it('falls back to durable history after consecutive failed reconnects, with exponential backoff between attempts', async () => {
    const reconcileRunSession = vi.fn(async () => true);
    const { subscriptions, sessionState } = setupRunningStream({
      reconcileRunSession,
    });

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

    // Attempt 3 hits MAX_SSE_RECONNECT_ATTEMPTS → durable reconciliation.
    subscriptions[3].handlers.onError(new Error('drop'));
    await vi.runAllTimersAsync();
    expect(subscriptions).toHaveLength(4);
    expect(subscriptions[3].close).toHaveBeenCalled();
    expect(sessionState.streamError).not.toContain('Reconnecting');
    expect(sessionState.streamError).toContain(
      'The live stream closed before the run finished',
    );
    expect(reconcileRunSession).toHaveBeenCalledWith(sessionState, RUN_ID);
  });

  it('keeps retrying durable reconciliation while history is temporarily unavailable', async () => {
    const reconcileRunSession = vi
      .fn()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);
    const { subscriptions, sessionState } = setupRunningStream({
      reconcileRunSession,
    });

    subscriptions[0].handlers.onError(new Error('drop'));
    vi.advanceTimersByTime(500);
    subscriptions[1].handlers.onError(new Error('drop'));
    vi.advanceTimersByTime(1_000);
    subscriptions[2].handlers.onError(new Error('drop'));
    vi.advanceTimersByTime(2_000);
    subscriptions[3].handlers.onError(new Error('drop'));
    await vi.advanceTimersByTimeAsync(0);

    expect(reconcileRunSession).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(reconcileRunSession).toHaveBeenCalledTimes(2);
    expect(reconcileRunSession).toHaveBeenLastCalledWith(sessionState, RUN_ID);
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

  it('projects an explicit Parent-Agent cancellation onto the exact child row', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });

    harness.stream.handleServerEvents({
      type: 'run_output',
      payload: {
        run_id: 'parent-run-two',
        agent_id: 'parent',
        session_id: 'parent-session',
        run_event_type: 'subagent_status_changed',
        run_event_sequence: 4,
        contributes_to_agent_activity: false,
        output: {
          data: {
            agent_id: DISPLAYED_AGENT_ID,
            session_id: DISPLAYED_SESSION_ID,
            run_id: DRAINED_RUN_ID,
            queue_item_id: QUEUED_ITEM_ID,
            status: 'cancelled',
          },
        },
      },
    });

    expect(harness.subAgentRunStatuses).toMatchObject({
      [`run:${DRAINED_RUN_ID}`]: 'cancelled',
      [`queue:${QUEUED_ITEM_ID}`]: 'cancelled',
      [`queueRun:${QUEUED_ITEM_ID}`]: DRAINED_RUN_ID,
      [`session:${DISPLAYED_AGENT_ID}::${DISPLAYED_SESSION_ID}`]: 'cancelled',
    });
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

describe('createChatRunStream() project-agent address reconstruction', () => {
  let chatState;
  const PROJECT_AGENT_ADDRESS = 'builder@vbot';
  const BARE_AGENT_ID = 'builder';
  const PROJECT_ID = 'vbot';
  const SESSION_ID = 'sess-project-1';
  const RUN_ID = 'run-project-1';

  beforeEach(() => {
    chatState = createChatState();
  });

  it('keys a project run server-event by the rebuilt agent@projekt address so the displayed project session matches and the backstop re-attaches', () => {
    const subscribeRunEvents = vi.fn(() => ({ close: vi.fn() }));
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: PROJECT_AGENT_ADDRESS,
      displayedSessionId: SESSION_ID,
      subscribeRunEvents,
    });

    harness.stream.handleServerEvents({
      type: 'run_started',
      payload: {
        run_id: RUN_ID,
        agent_id: BARE_AGENT_ID,
        project_id: PROJECT_ID,
        session_id: SESSION_ID,
        run_event_type: 'run_started',
        run_event_sequence: 1,
        status: 'running',
        output: { status: 'running' },
      },
    });

    // Session state lands under the rebuilt address, not the bare id — so it
    // matches the address-keyed displayed session instead of an orphan.
    expect(
      chatState.sessions[`${PROJECT_AGENT_ADDRESS}::${SESSION_ID}`],
    ).toBeTruthy();
    expect(
      chatState.sessions[`${BARE_AGENT_ID}::${SESSION_ID}`],
    ).toBeUndefined();
    // The status projection keys stay BARE: persisted spawn descriptors carry
    // the child's bare id, so bare keys are the only ones their reads meet.
    expect(
      harness.subAgentRunStatuses[`session:${BARE_AGENT_ID}::${SESSION_ID}`],
    ).toBe('running');
    expect(
      harness.subAgentRunStatuses[
        `session:${PROJECT_AGENT_ADDRESS}::${SESSION_ID}`
      ],
    ).toBeUndefined();
    expect(harness.isDisplayedSession).toHaveBeenCalledWith(
      PROJECT_AGENT_ADDRESS,
      SESSION_ID,
    );
    expect(subscribeRunEvents).toHaveBeenCalledTimes(1);
  });

  it('rebuilds the address from a snapshot active run for the re-attach while keeping the sub-agent status key bare', () => {
    const subscribeRunEvents = vi.fn(() => ({ close: vi.fn() }));
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: PROJECT_AGENT_ADDRESS,
      displayedSessionId: SESSION_ID,
      subscribeRunEvents,
    });

    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      epoch: 'epoch-project',
      last_sequence: 0,
      active_runs: [
        {
          run_id: RUN_ID,
          agent_id: BARE_AGENT_ID,
          project_id: PROJECT_ID,
          session_id: SESSION_ID,
          status: 'running',
          sse_url: `/api/runs/${RUN_ID}/events`,
        },
      ],
    });

    // Status key bare (descriptor-compatible); session STATE stays
    // address-keyed for the displayed-session match below.
    expect(
      harness.subAgentRunStatuses[`session:${BARE_AGENT_ID}::${SESSION_ID}`],
    ).toBe('running');
    expect(harness.isDisplayedSession).toHaveBeenCalledWith(
      PROJECT_AGENT_ADDRESS,
      SESSION_ID,
    );
    expect(subscribeRunEvents).toHaveBeenCalledTimes(1);
    expect(
      chatState.sessions[`${PROJECT_AGENT_ADDRESS}::${SESSION_ID}`],
    ).toBeTruthy();
  });

  it('keys an identity run (no project_id) by the bare id, byte-identical to today', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: BARE_AGENT_ID,
      displayedSessionId: SESSION_ID,
    });

    harness.stream.handleServerEvents({
      type: 'run_started',
      payload: {
        run_id: RUN_ID,
        agent_id: BARE_AGENT_ID,
        session_id: SESSION_ID,
        run_event_type: 'run_started',
        run_event_sequence: 1,
        status: 'running',
        output: { status: 'running' },
      },
    });

    expect(chatState.sessions[`${BARE_AGENT_ID}::${SESSION_ID}`]).toBeTruthy();
    expect(
      harness.subAgentRunStatuses[`session:${BARE_AGENT_ID}::${SESSION_ID}`],
    ).toBe('running');
  });
});
