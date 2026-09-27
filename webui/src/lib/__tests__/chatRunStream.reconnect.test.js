import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  DISPLAYED_AGENT_ID,
  DISPLAYED_SESSION_ID,
  activeRun,
  makeStreamHarness,
  serverRunEvent,
} from './chatRunStream.support.js';

// Stream warnings are asserted through their i18n key.
vi.mock('../i18n.js', async (importOriginal) => ({
  ...(await importOriginal()),
  t: (key) => key,
}));

const RUN_ID = 'run-reconnect-1';

describe('SSE reconnect and stall recovery', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    // Pin reconnect jitter to its midpoint so the backoff delay equals the
    // base delay exactly, keeping the timing assertions below deterministic.
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function setupRunningStream(options) {
    const harness = makeStreamHarness(options);
    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      epoch: 'epoch-b2',
      last_sequence: 0,
      active_runs: [activeRun(RUN_ID)],
    });
    expect(harness.subscriptions).toHaveLength(1);
    return {
      harness,
      subscriptions: harness.subscriptions,
      sessionState: harness.displayedSession(),
    };
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

  function runCompleted(sequence) {
    return serverRunEvent('run_completed', sequence, {
      run_id: RUN_ID,
      agent_id: DISPLAYED_AGENT_ID,
      session_id: DISPLAYED_SESSION_ID,
      status: 'completed',
    });
  }

  it('resets the reconnect budget once events flow again, so transient drops spread over a run never exhaust it', () => {
    const { subscriptions, sessionState } = setupRunningStream();

    // More drops than MAX_SSE_RECONNECT_ATTEMPTS, each preceded by a
    // successfully delivered event: every drop is attempt 0 and reconnects
    // after the base 500ms delay.
    for (let drop = 0; drop < 5; drop += 1) {
      const subscription = subscriptions[subscriptions.length - 1];
      subscription.handlers.onEvent(runEvent(drop + 1));
      expect(sessionState.streamError).toBe('');
      subscription.handlers.onError(new Error('transient drop'));
      expect(sessionState.streamError).toBe('errors.streamReconnecting');
      vi.advanceTimersByTime(500);
      expect(subscriptions).toHaveLength(drop + 2);
    }
  });

  it('uses transport heartbeats to detect a silently stalled EventSource connection', () => {
    const { subscriptions, sessionState } = setupRunningStream();

    vi.advanceTimersByTime(20_000);
    subscriptions[0].handlers.onHeartbeat();
    vi.advanceTimersByTime(20_000);
    expect(subscriptions).toHaveLength(1);
    expect(subscriptions[0].close).not.toHaveBeenCalled();

    vi.advanceTimersByTime(5_000);
    expect(subscriptions[0].close).toHaveBeenCalledOnce();
    expect(sessionState.streamError).toBe('errors.streamReconnecting');
    vi.advanceTimersByTime(500);
    expect(subscriptions).toHaveLength(2);

    // The reconnected subscription arms its own stall watchdog.
    vi.advanceTimersByTime(25_000);
    expect(subscriptions[1].close).toHaveBeenCalledOnce();
  });

  it('reconnects from the contiguous cursor when a sequence gap stays open', () => {
    const { harness, subscriptions, sessionState } = setupRunningStream();

    subscriptions[0].handlers.onEvent(runEvent(1));
    subscriptions[0].handlers.onEvent(runEvent(3));
    subscriptions[0].handlers.onHeartbeat();
    vi.advanceTimersByTime(2_000);

    expect(subscriptions[0].close).toHaveBeenCalledOnce();
    expect(sessionState.streamError).toBe('errors.streamReconnecting');
    vi.advanceTimersByTime(500);
    expect(subscriptions).toHaveLength(2);
    expect(subscriptions[1].options.afterSequence).toBe(1);
    expect(harness.reportStreamDiagnostic).toHaveBeenCalledWith(
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
    const { harness, subscriptions, sessionState } = setupRunningStream();
    subscriptions[0].handlers.onEvent(runEvent(1));

    harness.stream.handleServerEvents(runCompleted(3));
    await vi.advanceTimersByTimeAsync(1_000);

    expect(subscriptions[0].close).toHaveBeenCalledOnce();
    expect(sessionState.streamError).toBe('errors.streamClosed');
    expect(harness.reconcileRunSession).toHaveBeenCalledWith(
      sessionState,
      RUN_ID,
    );
    expect(harness.reportStreamDiagnostic).toHaveBeenCalledWith(
      expect.objectContaining({
        reason: 'terminal_event_blocked',
        runId: RUN_ID,
        expectedSequence: 2,
        receivedSequence: 3,
      }),
    );
  });

  it('closes the SSE subscription when a contiguous terminal event arrives over WebSocket', () => {
    const { harness, subscriptions, sessionState } = setupRunningStream();

    harness.stream.handleServerEvents(runCompleted(1));

    expect(sessionState.status).toBe('completed');
    expect(subscriptions[0].close).toHaveBeenCalledOnce();

    vi.advanceTimersByTime(25_000);
    subscriptions[0].handlers.onError(new Error('late EventSource error'));
    vi.advanceTimersByTime(500);
    expect(subscriptions).toHaveLength(1);
  });

  it('falls back to durable history after consecutive failed reconnects, with exponential backoff between attempts', async () => {
    const { harness, subscriptions, sessionState } = setupRunningStream();

    // Attempts 0, 1 and 2 reconnect after 500, 1000 and 2000 ms.
    for (const [attempt, delay] of [500, 1_000, 2_000].entries()) {
      subscriptions[attempt].handlers.onError(new Error('drop'));
      vi.advanceTimersByTime(delay - 1);
      expect(subscriptions).toHaveLength(attempt + 1);
      vi.advanceTimersByTime(1);
      expect(subscriptions).toHaveLength(attempt + 2);
    }

    // Attempt 3 hits MAX_SSE_RECONNECT_ATTEMPTS → durable reconciliation.
    subscriptions[3].handlers.onError(new Error('drop'));
    await vi.runAllTimersAsync();
    expect(subscriptions).toHaveLength(4);
    expect(subscriptions[3].close).toHaveBeenCalled();
    expect(sessionState.streamError).toBe('errors.streamClosed drop');
    expect(harness.reconcileRunSession).toHaveBeenCalledWith(
      sessionState,
      RUN_ID,
    );
  });

  it.each([
    { name: 'keeps retrying', closed: false, calls: 2 },
    { name: 'stops retrying after the stream closes', closed: true, calls: 1 },
  ])(
    '$name durable reconciliation while history is temporarily unavailable',
    async ({ closed, calls }) => {
      const reconcileRunSession = vi
        .fn()
        .mockResolvedValueOnce(false)
        .mockResolvedValueOnce(true);
      const { harness, subscriptions, sessionState } = setupRunningStream({
        reconcileRunSession,
      });

      subscriptions[0].handlers.onError(new Error('drop'));
      vi.advanceTimersByTime(500);
      subscriptions[1].handlers.onError(new Error('drop'));
      vi.advanceTimersByTime(1_000);
      subscriptions[2].handlers.onError(new Error('drop'));
      vi.advanceTimersByTime(2_000);
      subscriptions[3].handlers.onError(new Error('drop'));
      if (closed) harness.stream.closeSubscriptions();
      await vi.advanceTimersByTimeAsync(0);

      expect(reconcileRunSession).toHaveBeenCalledOnce();
      await vi.advanceTimersByTimeAsync(5_000);
      expect(reconcileRunSession).toHaveBeenCalledTimes(calls);
      expect(reconcileRunSession).toHaveBeenLastCalledWith(
        sessionState,
        RUN_ID,
      );
    },
  );
});
