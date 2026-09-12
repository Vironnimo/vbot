import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  addServerQueuedMessage,
  createChatState,
  ensureSessionState,
  setAgents,
} from '../chatState.js';
import { makeStreamHarness } from './chatRunStream.support.js';

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

  function setupRunningStream({
    reconcileRunSession,
    reportStreamDiagnostic,
  } = {}) {
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
      reportStreamDiagnostic,
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

  it('reconnects from the contiguous cursor when a sequence gap stays open', () => {
    const reportStreamDiagnostic = vi.fn();
    const { subscriptions, sessionState } = setupRunningStream({
      reportStreamDiagnostic,
    });

    subscriptions[0].handlers.onEvent(runEvent(1));
    subscriptions[0].handlers.onEvent(runEvent(3));
    subscriptions[0].handlers.onHeartbeat();
    vi.advanceTimersByTime(2_000);

    expect(subscriptions[0].close).toHaveBeenCalledOnce();
    expect(sessionState.streamError).toContain('Reconnecting');
    vi.advanceTimersByTime(500);
    expect(subscriptions).toHaveLength(2);
    expect(subscriptions[1].options.afterSequence).toBe(1);
    expect(reportStreamDiagnostic).toHaveBeenCalledWith(
      expect.objectContaining({
        reason: 'sequence_gap_timeout',
        runId: RUN_ID,
        expectedSequence: 2,
        receivedSequence: 3,
      }),
    );

    subscriptions[1].handlers.onEvent(runEvent(3));
    expect(sessionState.runEvents.map((event) => event.sequence)).toEqual([
      1, 3,
    ]);
  });

  it('cancels the gap watchdog when the missing event arrives in time', () => {
    const { subscriptions, sessionState } = setupRunningStream();

    subscriptions[0].handlers.onEvent(runEvent(1));
    subscriptions[0].handlers.onEvent(runEvent(3));
    subscriptions[0].handlers.onEvent(runEvent(2));
    vi.advanceTimersByTime(2_000);

    expect(subscriptions).toHaveLength(1);
    expect(subscriptions[0].close).not.toHaveBeenCalled();
    expect(sessionState.runEvents.map((event) => event.sequence)).toEqual([
      1, 2, 3,
    ]);
  });

  it('loads durable history when a terminal WebSocket event is blocked by a gap', async () => {
    const reconcileRunSession = vi.fn(async () => true);
    const reportStreamDiagnostic = vi.fn();
    const { subscriptions, harness, sessionState } = setupRunningStream({
      reconcileRunSession,
      reportStreamDiagnostic,
    });
    subscriptions[0].handlers.onEvent(runEvent(1));

    harness.stream.handleServerEvents({
      type: 'run_completed',
      payload: {
        run_id: RUN_ID,
        agent_id: DISPLAYED_AGENT_ID,
        session_id: DISPLAYED_SESSION_ID,
        run_event_type: 'run_completed',
        run_event_sequence: 3,
        status: 'completed',
      },
    });
    await vi.advanceTimersByTimeAsync(1_000);

    expect(subscriptions[0].close).toHaveBeenCalledOnce();
    expect(reconcileRunSession).toHaveBeenCalledWith(sessionState, RUN_ID);
    expect(reportStreamDiagnostic).toHaveBeenCalledWith(
      expect.objectContaining({
        reason: 'terminal_event_blocked',
        runId: RUN_ID,
        expectedSequence: 2,
        receivedSequence: 3,
      }),
    );
  });

  it('closes the SSE subscription when a contiguous terminal event arrives over WebSocket', () => {
    const { subscriptions, harness, sessionState } = setupRunningStream();

    harness.stream.handleServerEvents({
      type: 'run_completed',
      payload: {
        run_id: RUN_ID,
        agent_id: DISPLAYED_AGENT_ID,
        session_id: DISPLAYED_SESSION_ID,
        run_event_type: 'run_completed',
        run_event_sequence: 1,
        status: 'completed',
      },
    });

    expect(sessionState.status).toBe('completed');
    expect(subscriptions[0].close).toHaveBeenCalledOnce();

    vi.advanceTimersByTime(25_000);
    subscriptions[0].handlers.onError(new Error('late EventSource error'));
    vi.advanceTimersByTime(500);
    expect(subscriptions).toHaveLength(1);
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
