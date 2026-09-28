import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  agentActivityStatus,
  ensureSessionState,
  resetStaleRun,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import {
  DISPLAYED_AGENT_ID,
  DISPLAYED_SESSION_ID,
  activeRun,
  makeStreamHarness,
  serverRunEvent,
} from './chatRunStream.support.js';

const displayed = {
  agent_id: DISPLAYED_AGENT_ID,
  session_id: DISPLAYED_SESSION_ID,
};

describe('live Run events of the displayed Session', () => {
  it.each(['run_completed', 'run_interrupted'])(
    'does not let an older %s event close the current Run stream',
    (type) => {
      const harness = makeStreamHarness();
      const session = harness.displayedSession();
      harness.stream.attachRunStream(session, {
        run_id: 'new',
        status: 'running',
        sse_url: '/new',
      });
      session.contextUsage = { tokens: 100, estimated: false };

      harness.stream.handleServerEvents(
        serverRunEvent(type, 8, {
          ...displayed,
          run_id: 'old',
          status: type.slice(4),
          output: { iteration_count: 99 },
          context_usage: { tokens: 50, estimated: false },
        }),
      );

      expect(session.currentRun).toMatchObject({
        runId: 'new',
        status: 'running',
        iterationCount: 0,
      });
      expect(session.status).toBe('running');
      expect(session.contextUsage.tokens).toBe(100);
      expect(harness.subscriptions[0].close).not.toHaveBeenCalled();
      expect(
        session.runEvents.some(
          (event) => event.run_id === 'old' && event.type === type,
        ),
      ).toBe(true);
      harness.stream.closeSubscriptions();
    },
  );

  it.each([
    { type: 'run_completed', error: null },
    { type: 'run_failed', error: 'Provider request failed' },
  ])(
    'settles a $type WebSocket backstop and reconciles canonical History',
    async ({ type, error }) => {
      const harness = makeStreamHarness();
      const session = harness.displayedSession();
      harness.stream.attachRunStream(session, {
        run_id: 'run',
        sse_url: '/run',
        status: 'running',
      });

      harness.stream.handleServerEvents(
        serverRunEvent(type, 1, {
          ...displayed,
          run_id: 'run',
          status: type.slice(4),
          ...(error ? { error } : {}),
        }),
      );

      expect(session.status).toBe(type.slice(4));
      expect(session.error).toBe(error);
      expect(harness.subscriptions[0].close).toHaveBeenCalledOnce();
      await vi.waitFor(() =>
        expect(harness.reconcileRunSession).toHaveBeenCalledWith(
          session,
          'run',
        ),
      );
    },
  );

  it('lets the displayed SSE stream settle final output before its mirrored WebSocket terminal event', () => {
    const harness = makeStreamHarness();
    const run = { ...displayed, run_id: 'run-ordered-terminal' };

    harness.stream.handleServerEvents(
      serverRunEvent('run_started', 1, {
        ...run,
        status: 'running',
        output: { status: 'running' },
      }),
    );
    harness.sse({
      type: 'assistant_output_delta',
      run_id: run.run_id,
      sequence: 2,
      payload: { content_delta: 'Final answer' },
    });
    harness.stream.handleServerEvents(
      serverRunEvent('run_completed', 4, { ...run, status: 'completed' }),
    );

    const sessionState = harness.displayedSession();
    const close = harness.subscriptions[0].close;
    expect(sessionState.status).toBe('running');
    expect(close).not.toHaveBeenCalled();
    expect(
      visibleTimelineItemsForRender(sessionState)[0].outputs[0].content,
    ).toBe('Final answer');

    harness.sse(
      {
        type: 'assistant_output',
        run_id: run.run_id,
        sequence: 3,
        payload: { message: { role: 'assistant', content: 'Final answer' } },
      },
      {
        type: 'run_completed',
        run_id: run.run_id,
        sequence: 4,
        payload: { status: 'completed' },
      },
    );

    expect(sessionState.status).toBe('completed');
    expect(close).toHaveBeenCalledOnce();
    expect(
      visibleTimelineItemsForRender(sessionState)[0].outputs[0].content,
    ).toBe('Final answer');
  });

  it('applies SSE events only after their Run sequence becomes contiguous', () => {
    const harness = makeStreamHarness();
    const runId = 'run-out-of-order';
    harness.stream.applyConnectionSnapshot({ active_runs: [activeRun(runId)] });
    const sessionState = harness.displayedSession();

    harness.sse(
      { type: 'run_started', run_id: runId, sequence: 1, payload: {} },
      {
        type: 'run_completed',
        run_id: runId,
        sequence: 4,
        payload: { status: 'completed' },
      },
      {
        type: 'assistant_output',
        run_id: runId,
        sequence: 3,
        payload: { message: { role: 'assistant', content: 'Ordered final' } },
      },
    );

    expect(sessionState.status).toBe('running');
    expect(sessionState.runEvents.map((event) => event.sequence)).toEqual([1]);

    harness.sse({
      type: 'reasoning',
      run_id: runId,
      sequence: 2,
      payload: { reasoning: 'Missing event arrived.' },
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
    const harness = makeStreamHarness();
    const runId = 'run-truncated-replay';
    harness.stream.applyConnectionSnapshot({ active_runs: [activeRun(runId)] });

    harness.sse(
      {
        type: 'assistant_output_delta',
        run_id: runId,
        sequence: 5_000,
        payload: { content_delta: 'Still ' },
      },
      {
        type: 'assistant_output_delta',
        run_id: runId,
        sequence: 5_001,
        payload: { content_delta: 'live' },
      },
    );

    const sessionState = harness.displayedSession();
    await vi.waitFor(() =>
      expect(
        visibleTimelineItemsForRender(sessionState)[0].outputs[0].content,
      ).toBe('Still live'),
    );
    expect(sessionState.status).toBe('running');
  });

  describe('Tool output', () => {
    afterEach(() => {
      vi.useRealTimers();
    });

    it('renders streamed Tool stdout with the next streaming flush', async () => {
      vi.useFakeTimers();
      const harness = makeStreamHarness();
      const runId = 'run-chunks';
      harness.stream.handleServerEvents(
        serverRunEvent('run_started', 1, {
          ...displayed,
          run_id: runId,
          status: 'running',
          output: { status: 'running' },
        }),
      );
      harness.sse(
        {
          type: 'tool_call_started',
          run_id: runId,
          sequence: 2,
          payload: {
            tool_call: {
              id: 'call-one',
              index: 0,
              name: 'bash',
              arguments: { command: 'x' },
            },
          },
        },
        {
          type: 'tool_call_stdout',
          run_id: runId,
          sequence: 3,
          payload: { tool_call_id: 'call-one', data: 'chunk-one' },
        },
      );
      const sessionState = harness.displayedSession();
      const tool = () =>
        visibleTimelineItemsForRender(sessionState)[0].tools[0];

      // Tool starts render at once; stdout chunks ride the ~33 ms flush.
      expect(tool()).toMatchObject({ toolCallId: 'call-one' });
      expect(tool().stdout).toBeFalsy();

      await vi.advanceTimersByTimeAsync(50);

      expect(tool()).toMatchObject({
        toolCallId: 'call-one',
        stdout: 'chunk-one',
      });
    });

    it('keeps answer text streamed after hidden Tool boundaries visible when returning to a running Session', () => {
      vi.useFakeTimers();
      let isDisplayed = true;
      const harness = makeStreamHarness({
        isDisplayedSession: () => isDisplayed,
      });
      const identity = { ...displayed, run_id: 'run-returned' };
      const sseUrl = '/api/runs/run-returned/events';
      const firstAnswer = {
        id: 'm1',
        role: 'assistant',
        content: 'Checking files.',
        tool_calls: [{ id: 'tc1', name: 'read' }],
      };
      const stable = [
        [4, 'assistant_output', { message: firstAnswer }],
        [5, 'tool_call_started', { tool_call_id: 'tc1', name: 'read' }],
        [6, 'tool_call_result', { tool_call_id: 'tc1', name: 'read' }],
      ];
      const sessionState = harness.displayedSession();
      startRun(sessionState, {
        run_id: identity.run_id,
        sse_url: sseUrl,
        status: 'running',
        events: [
          { ...identity, sequence: 1, type: 'run_started', payload: {} },
        ],
      });
      harness.stream.subscribeToRun(sessionState, sseUrl, { afterSequence: 0 });

      // Leaving the Session closes its SSE; the WebSocket mirrors stable events.
      isDisplayed = false;
      harness.stream.closeSubscriptionsExcept('alpha::other');
      harness.stream.handleServerEvents(
        null,
        stable.map(([sequence, type, output]) =>
          serverRunEvent(type, sequence, { ...identity, output }),
        ),
      );

      // Returning replays SSE from the highest contiguous sequence.
      isDisplayed = true;
      harness.stream.attachRunStream(sessionState, {
        run_id: identity.run_id,
        status: 'running',
        sse_url: sseUrl,
        events: [],
      });
      expect(harness.subscriptions.at(-1).options.afterSequence).toBe(1);
      harness.sse(
        ...[
          [2, 'assistant_output_delta', { content_delta: 'Checking ' }],
          [3, 'assistant_output_delta', { content_delta: 'files.' }],
          ...stable,
          [7, 'assistant_output_delta', { content_delta: 'Second ' }],
          [8, 'assistant_output_delta', { content_delta: 'answer' }],
        ].map(([sequence, type, payload]) => ({
          ...identity,
          sequence,
          type,
          payload,
        })),
      );
      vi.advanceTimersByTime(100);

      const answers = visibleTimelineItemsForRender(sessionState)
        .flatMap((item) => item.items ?? [])
        .filter((child) => child.type === 'assistant_output')
        .map((child) => child.content);
      expect(answers).toEqual(['Checking files.', 'Second answer']);
      harness.stream.closeSubscriptions();
    });
  });

  describe('Queue', () => {
    const queued = {
      id: 'queue-item-42',
      content: 'queued work to drain',
      created_at: '2026-06-10T00:00:00+00:00',
    };
    const runId = 'run-drained-1';

    it.each([
      { transport: 'WebSocket', queueItemId: queued.id, remaining: [] },
      { transport: 'SSE', queueItemId: queued.id, remaining: [] },
      {
        transport: 'WebSocket',
        queueItemId: undefined,
        remaining: [queued.id],
      },
    ])(
      'drains the Queue item a $transport run_started names ($queueItemId) without a Queue read',
      ({ transport, queueItemId, remaining }) => {
        const harness = makeStreamHarness();
        const startedPayload = {
          status: 'running',
          ...(queueItemId ? { queue_item_id: queueItemId } : {}),
        };
        if (transport === 'SSE') {
          harness.stream.applyConnectionSnapshot({
            active_runs: [activeRun(runId)],
          });
        }
        const sessionState = harness.displayedSession();
        sessionState.queue = [{ ...queued }];

        if (transport === 'SSE') {
          harness.sse({
            ...displayed,
            type: 'run_started',
            run_id: runId,
            sequence: 1,
            payload: startedPayload,
          });
        } else {
          harness.stream.handleServerEvents(
            serverRunEvent('run_started', 1, {
              ...displayed,
              run_id: runId,
              status: 'running',
              output: startedPayload,
            }),
          );
        }

        expect(sessionState.queue.map((item) => item.id)).toEqual(remaining);
        // The Queue read is the terminal-event backstop only.
        expect(harness.syncSessionQueue).not.toHaveBeenCalled();
      },
    );
  });

  describe('cancel responses', () => {
    it('settles the active Run and its open Sub-Agent from the cancel RPC response', () => {
      const harness = makeStreamHarness({
        displayedAgentId: 'orchestrator@project-one',
        displayedSessionId: 'session-one',
      });
      const sessionState = harness.displayedSession();
      const identity = {
        run_id: 'run-parent',
        agent_id: 'orchestrator',
        session_id: 'session-one',
      };
      startRun(sessionState, {
        run_id: 'run-parent',
        status: 'running',
        sse_url: '/api/runs/run-parent/events',
      });
      harness.stream.subscribeToRun(
        sessionState,
        '/api/runs/run-parent/events',
      );

      expect(
        harness.stream.mergeRunResponse(sessionState, {
          run_id: 'run-parent',
          status: 'cancelled',
          events: [
            {
              ...identity,
              type: 'run_started',
              sequence: 1,
              payload: { status: 'running' },
            },
            {
              ...identity,
              type: 'tool_call_started',
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
              ...identity,
              type: 'run_cancelled',
              sequence: 3,
              payload: { status: 'cancelled' },
            },
          ],
        }),
      ).toBe(true);

      const close = harness.subscriptions[0].close;
      expect(sessionState.status).toBe('cancelled');
      expect(sessionState.currentRun?.status).toBe('cancelled');
      expect(close).toHaveBeenCalledOnce();
      expect(harness.syncSessionQueue).toHaveBeenCalledOnce();
      expect(
        visibleTimelineItemsForRender(sessionState)[0].tools[0].status,
      ).toBe('cancelled');

      harness.stream.mergeRunResponse(sessionState, {
        run_id: 'run-parent',
        status: 'cancelled',
        events: [...sessionState.runEvents],
      });

      expect(sessionState.runEvents).toHaveLength(3);
      expect(close).toHaveBeenCalledOnce();
    });

    it('does not let a delayed cancel response overwrite a newer Run', () => {
      const harness = makeStreamHarness();
      const sessionState = harness.displayedSession();
      startRun(sessionState, {
        run_id: 'run-new',
        status: 'running',
        sse_url: '/api/runs/run-new/events',
      });

      expect(
        harness.stream.mergeRunResponse(sessionState, {
          run_id: 'run-old',
          status: 'cancelled',
          events: [
            {
              type: 'run_cancelled',
              run_id: 'run-old',
              sequence: 3,
              payload: { status: 'cancelled' },
            },
          ],
        }),
      ).toBe(false);

      expect(sessionState.status).toBe('running');
      expect(sessionState.currentRun).toMatchObject({
        runId: 'run-new',
        status: 'running',
      });
      expect(sessionState.runEvents).toEqual([]);
    });
  });
});

describe('connection snapshots', () => {
  it('attaches the SSE stream exactly once for an active Run of the displayed Session', () => {
    const harness = makeStreamHarness();

    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      epoch: 'epoch-1',
      last_sequence: 0,
      active_runs: [
        activeRun('run-snapshot-1', { started_at: '2026-08-05T18:00:00.000Z' }),
      ],
    });

    expect(harness.subscriptions).toEqual([
      expect.objectContaining({
        sseUrl: '/api/runs/run-snapshot-1/events',
        handlers: expect.objectContaining({
          onEvent: expect.any(Function),
          onError: expect.any(Function),
        }),
        options: expect.objectContaining({ afterSequence: expect.any(Number) }),
      }),
    ]);
    const sessionState = harness.displayedSession();
    expect(sessionState.status).toBe('running');
    expect(sessionState.currentRun).toMatchObject({
      runId: 'run-snapshot-1',
      sseUrl: '/api/runs/run-snapshot-1/events',
      startedAt: '2026-08-05T18:00:00.000Z',
    });
  });

  it('restores Run controls from a minimal reconnect snapshot with an evicted event prefix', () => {
    const harness = makeStreamHarness();
    const controls = {
      compaction: 'pending',
      background_tool_call_ids: ['call-one'],
    };

    harness.stream.applyConnectionSnapshot({
      active_runs: [
        activeRun('run-controls', {
          controls,
          controls_sequence: 5000,
          sse_url: undefined,
        }),
      ],
    });

    expect(harness.displayedSession().currentRun).toMatchObject({
      controls,
      controlsSequence: 5000,
    });
    // Without an advertised SSE URL the stream derives the Run's own URL.
    expect(harness.subscriptions.map(({ sseUrl }) => sseUrl)).toEqual([
      '/api/runs/run-controls/events',
    ]);
    harness.stream.closeSubscriptions();
  });

  it('records Sub-Agent running entries without opening SSE for Runs of other Sessions', () => {
    const harness = makeStreamHarness();

    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      active_runs: [
        activeRun('run-child-1', {
          agent_id: 'beta',
          session_id: 'session-child-1',
          started_at: '2026-08-05T18:01:00.000Z',
        }),
        activeRun('run-child-2', {
          agent_id: 'gamma',
          session_id: 'session-child-2',
          started_at: '2026-08-05T18:02:00.000Z',
        }),
      ],
    });

    expect(harness.subscriptions).toEqual([]);
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
    expect(agentActivityStatus(harness.chatState, 'beta')).toBe('running');
    expect(agentActivityStatus(harness.chatState, 'gamma')).toBe('running');
  });

  it('opens no subscription and leaves the Session idle for a snapshot without active Runs', () => {
    const harness = makeStreamHarness();
    const sessionState = harness.displayedSession();

    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      epoch: 'epoch-3',
      last_sequence: 0,
      active_runs: [],
    });

    // Stream warnings belong to their Session and only appear after a
    // subscription error.
    expect(harness.subscriptions).toEqual([]);
    expect(sessionState.streamError).toBe('');
    expect(harness.subAgentRunStatuses).toEqual({});
    expect(sessionState.status).toBe('idle');
    expect(sessionState.currentRun).toBeNull();
  });

  it('reconciles a locally running Run that is absent from the authoritative reconnect snapshot against durable history', async () => {
    const reconcileRunSession = vi.fn(async (staleSessionState) => {
      resetStaleRun(staleSessionState);
      return true;
    });
    const harness = makeStreamHarness({ reconcileRunSession });
    const sessionState = harness.displayedSession();

    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      active_runs: [activeRun('run-before-restart')],
    });
    expect(sessionState.status).toBe('running');

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
    expect(sessionState.status).toBe('idle');
    expect(sessionState.currentRun).toBeNull();
    expect(harness.subscriptions[0].close).toHaveBeenCalledOnce();
  });

  it('replaces stale active Sub-Agent statuses while preserving terminal metadata', () => {
    const harness = makeStreamHarness();
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

  it.each([
    {
      name: 'Project Run by its rebuilt agent@project address',
      identity: { agent_id: 'builder', project_id: 'vbot' },
      address: 'builder@vbot',
    },
    {
      name: 'Identity Run by its bare id',
      identity: { agent_id: 'builder' },
      address: 'builder',
    },
  ])('keys a $name', ({ identity, address }) => {
    const sessionId = 'sess-project-1';
    const runId = 'run-project-1';
    const viaEvent = makeStreamHarness({
      displayedAgentId: address,
      displayedSessionId: sessionId,
    });
    const viaSnapshot = makeStreamHarness({
      displayedAgentId: address,
      displayedSessionId: sessionId,
    });

    viaEvent.stream.handleServerEvents(
      serverRunEvent('run_started', 1, {
        ...identity,
        run_id: runId,
        session_id: sessionId,
        status: 'running',
        output: { status: 'running' },
      }),
    );
    viaSnapshot.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      active_runs: [activeRun(runId, { ...identity, session_id: sessionId })],
    });

    // Session state and Sub-Agent status keys use the address the displayed
    // Session and Sub-Agent rows read; the displayed Session re-attaches SSE.
    for (const harness of [viaEvent, viaSnapshot]) {
      expect(Object.keys(harness.chatState.sessions)).toEqual([
        `${address}::${sessionId}`,
      ]);
      expect(harness.subAgentRunStatuses).toMatchObject({
        [`run:${runId}`]: 'running',
        [`session:${address}::${sessionId}`]: 'running',
      });
      expect(
        Object.keys(harness.subAgentRunStatuses).filter((key) =>
          key.startsWith('session:'),
        ),
      ).toEqual([`session:${address}::${sessionId}`]);
      expect(harness.subscriptions).toHaveLength(1);
    }
  });
});

describe('Runs excluded from Agent activity', () => {
  it('reattaches an excluded active Run from a snapshot without projecting Agent activity', () => {
    const harness = makeStreamHarness();

    harness.stream.applyConnectionSnapshot({
      type: 'connection_ready',
      active_runs: [
        activeRun('run-system', { contributes_to_agent_activity: false }),
      ],
    });

    const sessionState = harness.displayedSession();
    expect(harness.subscriptions).toHaveLength(1);
    expect(sessionState.status).toBe('running');
    expect(sessionState.currentRun?.contributesToAgentActivity).toBe(false);
    expect(agentActivityStatus(harness.chatState, DISPLAYED_AGENT_ID)).toBe(
      'idle',
    );
    expect(harness.subAgentRunStatuses).toEqual({});
  });

  it('keeps excluded WebSocket lifecycle events out of Agent activity', () => {
    const harness = makeStreamHarness();
    const run = {
      ...displayed,
      run_id: 'run-system',
      contributes_to_agent_activity: false,
    };
    const contextUsage = {
      tokens: 155489,
      estimated: true,
      provider_input_tokens: 154731,
      provider_output_tokens: 243,
      estimated_delta_tokens: 515,
    };

    harness.stream.handleServerEvents(
      serverRunEvent('run_started', 1, {
        ...run,
        output: { status: 'running' },
      }),
    );

    const sessionState = harness.displayedSession();
    expect(sessionState.currentRun?.contributesToAgentActivity).toBe(false);
    expect(agentActivityStatus(harness.chatState, DISPLAYED_AGENT_ID)).toBe(
      'idle',
    );

    harness.stream.handleServerEvents(
      serverRunEvent('run_completed', 2, {
        ...run,
        status: 'completed',
        context_usage: contextUsage,
      }),
    );

    expect(sessionState.contextUsage).toEqual(contextUsage);
    expect(sessionState.hasUnreadCompletion).toBe(false);
    expect(agentActivityStatus(harness.chatState, DISPLAYED_AGENT_ID)).toBe(
      'idle',
    );
    expect(harness.subAgentRunStatuses).toEqual({});
  });
});

describe('Runs of Sessions that are not displayed', () => {
  it('settles a non-displayed Run from its sparse stable WebSocket events', () => {
    const harness = makeStreamHarness();
    const child = {
      run_id: 'child-run',
      agent_id: 'worker',
      session_id: 'child-session',
      run_kind: 'subagent',
    };

    harness.stream.handleServerEvents(
      serverRunEvent('run_started', 1, {
        ...child,
        run_event_timestamp: '2026-08-05T18:03:00.000Z',
        status: 'running',
      }),
    );
    harness.stream.handleServerEvents(
      serverRunEvent('assistant_output', 6, {
        ...child,
        output: { message: { role: 'assistant', content: 'Child result' } },
      }),
    );
    harness.stream.handleServerEvents(
      serverRunEvent('run_completed', 8, { ...child, status: 'completed' }),
    );

    const childSession = ensureSessionState(
      harness.chatState,
      'worker',
      'child-session',
    );
    expect(childSession.status).toBe('completed');
    expect(childSession.currentRun).toMatchObject({
      runId: 'child-run',
      status: 'completed',
    });
    expect(childSession).toMatchObject({
      hasUnreadCompletion: true,
      unreadRunId: 'child-run',
      unreadRunStatus: 'completed',
    });
    // Nothing renders this Session's live projection: opening it loads
    // canonical History, so the finished Run's events are released.
    expect(childSession.runEvents).toEqual([]);
    expect(harness.reconcileRunSession).not.toHaveBeenCalled();
    expect(harness.syncSessionQueue).toHaveBeenCalledWith(childSession);
    expect(harness.subAgentRunStatuses).toMatchObject({
      'run:child-run': 'completed',
      'runStarted:child-run': '2026-08-05T18:03:00.000Z',
      'session:worker::child-session': 'completed',
      'sessionStarted:worker::child-session': '2026-08-05T18:03:00.000Z',
    });

    // A late re-delivery (here through an owner whose dedup window no longer
    // holds the event) cannot revive the finished Run or re-grow its events.
    const lateOwner = makeStreamHarness({ chatState: harness.chatState });
    lateOwner.stream.handleServerEvents(
      serverRunEvent('run_started', 1, { ...child, status: 'running' }),
    );
    expect(childSession.status).toBe('completed');
    expect(childSession.currentRun.status).toBe('completed');
    expect(childSession.runEvents).toEqual([]);
    expect(agentActivityStatus(harness.chatState, 'worker')).toBe('unread');
  });

  it('keeps and reconciles a finished Run of a non-displayed Session whose History is loaded', async () => {
    const harness = makeStreamHarness();
    const background = ensureSessionState(
      harness.chatState,
      'alpha',
      'background',
    );
    background.historyLoaded = true;
    const run = {
      run_id: 'background-run',
      agent_id: 'alpha',
      session_id: 'background',
    };

    harness.stream.handleServerEvents(null, [
      serverRunEvent('run_started', 1, run),
      serverRunEvent('tool_call_started', 2, run),
      serverRunEvent('run_completed', 3, { ...run, status: 'completed' }),
    ]);

    expect(background.status).toBe('completed');
    expect(background.runEvents.map((event) => event.sequence)).toEqual([
      1, 2, 3,
    ]);
    await vi.waitFor(() =>
      expect(harness.reconcileRunSession).toHaveBeenCalledWith(
        background,
        'background-run',
      ),
    );
  });

  it('releases only the finished Run when a predecessor terminal arrives late', () => {
    const harness = makeStreamHarness();
    const run = (runId) => ({
      run_id: runId,
      agent_id: 'alpha',
      session_id: 'background',
    });

    harness.stream.handleServerEvents(null, [
      serverRunEvent('run_started', 1, run('first')),
      serverRunEvent('run_started', 1, run('second')),
      serverRunEvent('tool_call_started', 2, run('second')),
      serverRunEvent('run_completed', 5, {
        ...run('first'),
        status: 'completed',
      }),
    ]);

    const background = ensureSessionState(
      harness.chatState,
      'alpha',
      'background',
    );
    expect(background.currentRun).toMatchObject({
      runId: 'second',
      status: 'running',
    });
    expect(
      background.runEvents.map((runEvent) => [
        runEvent.run_id,
        runEvent.sequence,
      ]),
    ).toEqual([
      ['second', 1],
      ['second', 2],
    ]);
  });
});
