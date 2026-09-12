import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  CHAT_STATUS_IDLE,
  CHAT_STATUS_CANCELLED,
  CHAT_STATUS_RUNNING,
  agentActivityStatus,
  createChatState,
  ensureSessionState,
  resetStaleRun,
  setAgents,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { makeStreamHarness } from './chatRunStream.support.js';

it('restores Run controls from a reconnect snapshot with an evicted event prefix', () => {
  const chatState = createChatState();
  const { stream } = makeStreamHarness({
    chatState,
    displayedAgentId: 'alpha',
    displayedSessionId: 'session',
  });
  const controls = {
    compaction: 'pending',
    background_tool_call_ids: ['call-one'],
  };
  stream.applyConnectionSnapshot({
    active_runs: [
      {
        run_id: 'run-controls',
        agent_id: 'alpha',
        session_id: 'session',
        controls,
        controls_sequence: 5000,
      },
    ],
  });
  const session = ensureSessionState(chatState, 'alpha', 'session');
  expect(session.currentRun.controls).toEqual(controls);
  expect(session.currentRun.controlsSequence).toBe(5000);
  stream.closeSubscriptions();
});

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
          started_at: '2026-08-05T18:00:00.000Z',
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
    expect(sessionState.currentRun?.startedAt).toBe('2026-08-05T18:00:00.000Z');
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
          started_at: '2026-08-05T18:01:00.000Z',
          sse_url: '/api/runs/run-child-1/events',
        },
        {
          run_id: 'run-child-2',
          agent_id: 'gamma',
          session_id: 'session-child-2',
          status: 'running',
          started_at: '2026-08-05T18:02:00.000Z',
          sse_url: '/api/runs/run-child-2/events',
        },
      ],
    };

    harness.stream.applyConnectionSnapshot(snapshot);

    expect(subscribeRunEvents).not.toHaveBeenCalled();
    expect(harness.subAgentRunStatuses).toEqual({
      'run:run-child-1': 'running',
      'runStarted:run-child-1': '2026-08-05T18:01:00.000Z',
      'session:beta::session-child-1': 'running',
      'sessionStarted:beta::session-child-1': '2026-08-05T18:01:00.000Z',
      'run:run-child-2': 'running',
      'runStarted:run-child-2': '2026-08-05T18:02:00.000Z',
      'session:gamma::session-child-2': 'running',
      'sessionStarted:gamma::session-child-2': '2026-08-05T18:02:00.000Z',
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

  it('projects interrupted Run status and duration for Sub-Agent rows', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });

    harness.stream.handleServerEvents({
      type: 'run_interrupted',
      payload: {
        run_id: 'run-interrupted-1',
        agent_id: 'worker',
        session_id: 'session-child',
        run_event_type: 'run_interrupted',
        run_event_sequence: 3,
        status: 'interrupted',
        cause: 'network',
        timing: { duration_ms: 2500 },
      },
    });

    expect(harness.subAgentRunStatuses).toEqual(
      expect.objectContaining({
        'run:run-interrupted-1': 'interrupted',
        'session:worker::session-child': 'interrupted',
        'runDuration:run-interrupted-1': 2500,
        'sessionDuration:worker::session-child': 2500,
      }),
    );
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

  it('lets the displayed SSE stream settle final output before its mirrored WebSocket terminal event', () => {
    let onEvent;
    const close = vi.fn();
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
      subscribeRunEvents: vi.fn((_url, handlers) => {
        onEvent = handlers.onEvent;
        return { close };
      }),
    });

    harness.stream.handleServerEvents({
      type: 'run_started',
      payload: {
        run_id: 'run-ordered-terminal',
        agent_id: DISPLAYED_AGENT_ID,
        session_id: DISPLAYED_SESSION_ID,
        run_event_type: 'run_started',
        run_event_sequence: 1,
        status: 'running',
        output: { status: 'running' },
      },
    });
    onEvent({
      data: {
        type: 'assistant_output_delta',
        run_id: 'run-ordered-terminal',
        sequence: 2,
        payload: { content_delta: 'Final answer' },
      },
    });

    harness.stream.handleServerEvents({
      type: 'run_completed',
      payload: {
        run_id: 'run-ordered-terminal',
        agent_id: DISPLAYED_AGENT_ID,
        session_id: DISPLAYED_SESSION_ID,
        run_event_type: 'run_completed',
        run_event_sequence: 4,
        status: 'completed',
      },
    });

    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );
    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
    expect(close).not.toHaveBeenCalled();
    expect(
      visibleTimelineItemsForRender(sessionState)[0].outputs[0].content,
    ).toBe('Final answer');

    onEvent({
      data: {
        type: 'assistant_output',
        run_id: 'run-ordered-terminal',
        sequence: 3,
        payload: {
          message: { role: 'assistant', content: 'Final answer' },
        },
      },
    });
    onEvent({
      data: {
        type: 'run_completed',
        run_id: 'run-ordered-terminal',
        sequence: 4,
        payload: { status: 'completed' },
      },
    });

    expect(sessionState.status).toBe('completed');
    expect(close).toHaveBeenCalledOnce();
    expect(
      visibleTimelineItemsForRender(sessionState)[0].outputs[0].content,
    ).toBe('Final answer');
  });

  it('settles a non-displayed Run from its sparse stable WebSocket events', () => {
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
    });
    const basePayload = {
      run_id: 'child-run',
      agent_id: 'worker',
      session_id: 'child-session',
      run_kind: 'subagent',
    };

    harness.stream.handleServerEvents({
      type: 'run_started',
      payload: {
        ...basePayload,
        run_event_type: 'run_started',
        run_event_sequence: 1,
        run_event_timestamp: '2026-08-05T18:03:00.000Z',
        status: 'running',
      },
    });
    harness.stream.handleServerEvents({
      type: 'run_output',
      payload: {
        ...basePayload,
        run_event_type: 'assistant_output',
        run_event_sequence: 6,
        output: {
          message: { role: 'assistant', content: 'Child result' },
        },
      },
    });
    harness.stream.handleServerEvents({
      type: 'run_completed',
      payload: {
        ...basePayload,
        run_event_type: 'run_completed',
        run_event_sequence: 8,
        status: 'completed',
      },
    });

    const childSession = ensureSessionState(
      chatState,
      'worker',
      'child-session',
    );
    expect(childSession.status).toBe('completed');
    expect(childSession.runEvents.map((event) => event.sequence)).toEqual([
      1, 6, 8,
    ]);
    expect(harness.subAgentRunStatuses).toMatchObject({
      'run:child-run': 'completed',
      'runStarted:child-run': '2026-08-05T18:03:00.000Z',
      'session:worker::child-session': 'completed',
      'sessionStarted:worker::child-session': '2026-08-05T18:03:00.000Z',
    });
  });

  it('applies SSE events only after their Run sequence becomes contiguous', () => {
    let onEvent;
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
      subscribeRunEvents: vi.fn((_url, handlers) => {
        onEvent = handlers.onEvent;
        return { close: vi.fn() };
      }),
    });
    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      active_runs: [
        {
          run_id: 'run-out-of-order',
          agent_id: DISPLAYED_AGENT_ID,
          session_id: DISPLAYED_SESSION_ID,
          status: 'running',
          sse_url: '/api/runs/run-out-of-order/events',
        },
      ],
    });
    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );

    onEvent({
      data: {
        type: 'run_started',
        run_id: 'run-out-of-order',
        sequence: 1,
        payload: { status: 'running' },
      },
    });
    onEvent({
      data: {
        type: 'run_completed',
        run_id: 'run-out-of-order',
        sequence: 4,
        payload: { status: 'completed' },
      },
    });
    onEvent({
      data: {
        type: 'assistant_output',
        run_id: 'run-out-of-order',
        sequence: 3,
        payload: { message: { role: 'assistant', content: 'Ordered final' } },
      },
    });

    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.runEvents.map((event) => event.sequence)).toEqual([1]);

    onEvent({
      data: {
        type: 'reasoning',
        run_id: 'run-out-of-order',
        sequence: 2,
        payload: { reasoning: 'Missing event arrived.' },
      },
    });

    expect(sessionState.runEvents.map((event) => event.sequence)).toEqual([
      1, 2, 3, 4,
    ]);
    expect(sessionState.status).toBe('completed');
    expect(
      visibleTimelineItemsForRender(sessionState)[0].outputs.at(-1).content,
    ).toBe('Ordered final');
  });

  it('resumes from the first retained SSE event when the replay prefix was evicted', async () => {
    let onEvent;
    const harness = makeStreamHarness({
      chatState,
      displayedAgentId: DISPLAYED_AGENT_ID,
      displayedSessionId: DISPLAYED_SESSION_ID,
      subscribeRunEvents: vi.fn((_url, handlers) => {
        onEvent = handlers.onEvent;
        return { close: vi.fn() };
      }),
    });
    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      active_runs: [
        {
          run_id: 'run-truncated-replay',
          agent_id: DISPLAYED_AGENT_ID,
          session_id: DISPLAYED_SESSION_ID,
          status: 'running',
          sse_url: '/api/runs/run-truncated-replay/events',
        },
      ],
    });
    const sessionState = ensureSessionState(
      chatState,
      DISPLAYED_AGENT_ID,
      DISPLAYED_SESSION_ID,
    );

    onEvent({
      data: {
        type: 'assistant_output_delta',
        run_id: 'run-truncated-replay',
        sequence: 5_000,
        payload: { content_delta: 'Still ' },
      },
    });
    onEvent({
      data: {
        type: 'assistant_output_delta',
        run_id: 'run-truncated-replay',
        sequence: 5_001,
        payload: { content_delta: 'live' },
      },
    });

    await vi.waitFor(() =>
      expect(
        visibleTimelineItemsForRender(sessionState)[0].outputs[0].content,
      ).toBe('Still live'),
    );
    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
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
