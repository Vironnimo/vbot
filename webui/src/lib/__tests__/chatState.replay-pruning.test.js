import { describe, expect, it } from 'vitest';
import {
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_IDLE,
  CHAT_STATUS_RUNNING,
  appendRunEvent,
  createChatState,
  ensureSessionState,
  highestContiguousRunEventSequence,
  isRunActive,
  loadHistory,
  resetStaleRun,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { countTimelineTextOccurrences } from './chatState.support.js';

describe('replayed Runs after a History refresh', () => {
  it('drops a completed prior run the WebSocket replays alongside the active run on refresh', () => {
    // On refresh the app WebSocket replays its retained lifecycle buffer from
    // sequence 0, re-injecting the already-completed parent run (the one that
    // spawned a non-blocking sub-agent) into runEvents next to the still-active
    // note-triggered follow-up run. The parent run carries its own
    // user_message_persisted plus assistant output, all already in history.
    // Its canonical summary must retire that replay even while another Run
    // is active, so the parent User/Assistant block appears only once.
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-ws-replay',
    );

    loadHistory(
      sessionState,
      [
        {
          history_run_id: 'run-one',
          id: 'user-one',
          role: 'user',
          content: 'Run a non-blocking worker',
        },
        {
          history_run_id: 'run-one',
          id: 'assistant-spawn',
          role: 'assistant',
          content: null,
          tool_calls: [
            {
              id: 'call-subagent',
              name: 'subagent',
              arguments: { agent_id: 'tester', background: true },
            },
          ],
        },
        {
          history_run_id: 'run-one',
          id: 'tool-subagent',
          role: 'tool',
          tool_call_id: 'call-subagent',
          name: 'subagent',
          content: '{"ok":true}',
        },
        {
          history_run_id: 'run-one',
          id: 'assistant-started',
          role: 'assistant',
          content: 'The worker is running.',
        },
        {
          history_run_id: 'run-one',
          id: 'summary-one',
          role: 'run_summary',
          run_id: 'run-one',
          status: 'completed',
          timing: { duration_ms: 10 },
        },
        {
          history_run_id: 'run-two',
          id: 'assistant-result',
          role: 'assistant',
          content: 'The worker finished: the answer is 42.',
        },
      ],
      { runs: [{ run_id: 'run-one', status: 'completed', complete: true }] },
    );

    // chat.history reports the still-running follow-up run as the active run.
    startRun(sessionState, {
      run_id: 'run-two',
      sse_url: '/api/runs/run-two/events',
      status: CHAT_STATUS_RUNNING,
    });
    // The WebSocket replays the completed parent run (run-one) in sequence order:
    // its user message, tool result, assistant output, and terminal event.
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-one',
      sequence: 1,
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Run a non-blocking worker',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-one',
      sequence: 3,
      payload: {
        tool_call: { id: 'call-subagent', name: 'subagent' },
        result: '{"ok":true}',
        message: { id: 'tool-subagent', role: 'tool', content: '{"ok":true}' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 4,
      payload: {
        message: {
          id: 'assistant-started',
          role: 'assistant',
          content: 'The worker is running.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 5,
      payload: { status: 'completed', timing: { duration_ms: 10 } },
    });
    // Then it replays the active follow-up run (run-two), restoring it as current.
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-two',
      sequence: 1,
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-two',
      sequence: 2,
      payload: {
        message: {
          id: 'assistant-result',
          role: 'assistant',
          content: 'The worker finished: the answer is 42.',
        },
      },
    });

    const timelineItems = visibleTimelineItemsForRender(sessionState);

    // The parent run's first assistant block must not render twice.
    expect(
      countTimelineTextOccurrences(timelineItems, 'The worker is running.'),
    ).toBe(1);
    // The active note-triggered Run's output is already persisted too.
    expect(
      countTimelineTextOccurrences(
        timelineItems,
        'The worker finished: the answer is 42.',
      ),
    ).toBe(1);
    // Its user message must not be re-rendered as a live user_message_persisted item.
    expect(
      timelineItems.filter(
        (item) =>
          item.type === 'event' &&
          item.event?.type === 'user_message_persisted' &&
          item.event?.run_id === 'run-one',
      ),
    ).toHaveLength(0);
    // No live block survives for the completed parent run.
    expect(
      timelineItems.filter(
        (item) =>
          item.type === 'assistant_run' &&
          item.source === 'live' &&
          (item.runId ?? item.run_id) === 'run-one',
      ),
    ).toHaveLength(0);
  });

  it('keeps a note-triggered run output that history has not persisted yet', () => {
    // Same shape, but the run's output is not yet in history (mid-stream). The
    // live run must still render so the user sees in-flight output.
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-note-run-live',
    );

    loadHistory(
      sessionState,
      [
        {
          history_run_id: 'run-one',
          id: 'user-one',
          role: 'user',
          content: 'Run a non-blocking worker',
        },
        {
          history_run_id: 'run-one',
          id: 'assistant-started',
          role: 'assistant',
          content: 'The worker is running.',
        },
        {
          history_run_id: 'run-one',
          id: 'summary-one',
          role: 'run_summary',
          run_id: 'run-one',
          status: 'completed',
          timing: { duration_ms: 10 },
        },
      ],
      { runs: [{ run_id: 'run-one', status: 'completed', complete: true }] },
    );

    startRun(sessionState, {
      run_id: 'run-two',
      sse_url: '/api/runs/run-two/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-two',
      sequence: 1,
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-two',
      sequence: 2,
      payload: {
        message: {
          id: 'assistant-result',
          role: 'assistant',
          content: 'The worker finished: the answer is 42.',
        },
      },
    });

    const occurrences = countTimelineTextOccurrences(
      visibleTimelineItemsForRender(sessionState),
      'The worker finished: the answer is 42.',
    );

    expect(occurrences).toBe(1);
  });

  it('drops sparse summarized Run replay while a newer Run keeps streaming', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-sparse-replay',
    );
    loadHistory(
      sessionState,
      [
        {
          history_run_id: 'run-one',
          id: 'user-one',
          role: 'user',
          content: 'First question',
        },
        {
          history_run_id: 'run-one',
          id: 'assistant-one',
          role: 'assistant',
          content: 'First answer',
        },
        {
          history_run_id: 'run-one',
          id: 'summary-one',
          role: 'run_summary',
          run_id: 'run-one',
          status: 'completed',
        },
        {
          history_run_id: 'run-two',
          id: 'user-two',
          role: 'user',
          content: 'Second question',
        },
      ],
      { runs: [{ run_id: 'run-one', status: 'completed', complete: true }] },
    );
    startRun(sessionState, {
      run_id: 'run-two',
      sse_url: '/api/runs/run-two/events',
      status: CHAT_STATUS_RUNNING,
    });

    // A remounted Chat consumes App's retained WebSocket list. The bounded
    // list may contain only the old Run's start and User event, without the
    // Assistant output that the previous Chat instance already observed.
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-one',
      sequence: 1,
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'First question',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-two',
      sequence: 1,
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-two',
      sequence: 2,
      payload: {
        message: {
          id: 'user-two',
          role: 'user',
          content: 'Second question',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-two',
      sequence: 3,
      payload: { content_delta: 'Second answer is still streaming' },
    });

    const timelineItems = visibleTimelineItemsForRender(sessionState);

    expect(
      timelineItems.flatMap((item) => {
        if (item.type === 'message' && item.message?.role === 'user') {
          return [item.message.content];
        }
        if (
          item.type === 'event' &&
          item.event?.type === 'user_message_persisted'
        ) {
          return [item.event.payload?.message?.content];
        }
        return [];
      }),
    ).toEqual(['First question', 'Second question']);
    expect(
      timelineItems.filter(
        (item) =>
          item.type === 'assistant_run' &&
          item.source === 'live' &&
          (item.runId ?? item.run_id) === 'run-one',
      ),
    ).toEqual([]);
    expect(timelineItems.at(-1)).toEqual(
      expect.objectContaining({
        type: 'assistant_run',
        runId: 'run-two',
        outputs: [
          expect.objectContaining({
            content: 'Second answer is still streaming',
            streaming: true,
          }),
        ],
      }),
    );
  });

  it('keeps the active Run live projection when History refreshes during the Run', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-one',
      sse_url: '/api/runs/run-one/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'reasoning',
      run_id: 'run-one',
      sequence: 1,
      payload: { message: { role: 'assistant', reasoning: 'Working' } },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 2,
      payload: { content_delta: 'Hel' },
    });

    loadHistory(sessionState, [
      {
        history_run_id: 'run-one',
        id: 'message-one',
        role: 'user',
        content: 'Hi',
      },
    ]);

    const [userMessage, assistantRun] =
      visibleTimelineItemsForRender(sessionState);
    expect(userMessage).toMatchObject({
      type: 'message',
      message: { id: 'message-one', content: 'Hi' },
    });
    expect(assistantRun).toMatchObject({
      type: 'assistant_run',
      runId: 'run-one',
    });
    expect(assistantRun.reasoning).toEqual([
      expect.objectContaining({ content: 'Working' }),
    ]);
    expect(assistantRun.outputs).toEqual([
      expect.objectContaining({ content: 'Hel', streaming: true }),
    ]);
  });

  it('keeps one assistant run when history refresh persists the active run output', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-one',
      sse_url: '/api/runs/run-one/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-one',
      sequence: 1,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Inspect the file',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        message: {
          id: 'assistant-one',
          role: 'assistant',
          content: 'The file says A.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-one',
      sequence: 3,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read',
          arguments: { path: 'a.txt' },
        },
      },
    });

    loadHistory(sessionState, [
      {
        history_run_id: 'run-one',
        id: 'user-one',
        role: 'user',
        content: 'Inspect the file',
      },
      {
        history_run_id: 'run-one',
        id: 'assistant-one',
        role: 'assistant',
        content: 'The file says A.',
      },
    ]);

    expect(visibleTimelineItemsForRender(sessionState)).toEqual([
      expect.objectContaining({
        id: 'user-one',
        type: 'message',
      }),
      expect.objectContaining({
        id: 'assistant-run-run-one',
        type: 'assistant_run',
        outputs: [
          expect.objectContaining({
            content: 'The file says A.',
          }),
        ],
        tools: [
          expect.objectContaining({
            toolCallId: 'call-one',
            status: CHAT_STATUS_RUNNING,
          }),
        ],
      }),
    ]);
  });

  it('clears run events when history refreshes after a run finishes', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-one',
      sse_url: '/api/runs/run-one/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 1,
      payload: { content_delta: 'Done' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        message: { id: 'message-one', role: 'assistant', content: 'Done' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 3,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    loadHistory(
      sessionState,
      [
        {
          history_run_id: 'run-one',
          id: 'message-one',
          role: 'assistant',
          content: 'Done',
        },
        {
          history_run_id: 'run-one',
          id: 'summary-one',
          role: 'run_summary',
          run_id: 'run-one',
          status: CHAT_STATUS_COMPLETED,
        },
      ],
      { runs: [{ run_id: 'run-one', complete: true }] },
    );

    expect(sessionState.runEvents).toEqual([]);
    expect(
      countTimelineTextOccurrences(
        visibleTimelineItemsForRender(sessionState),
        'Done',
      ),
    ).toBe(1);
  });
});

describe('finished Run retention during an active Run', () => {
  function seedFinishedRunEvents(sessionState, runId, messageId) {
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: runId,
      sequence: 1,
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: runId,
      sequence: 2,
      payload: {
        message: { id: messageId, role: 'assistant', content: 'Done.' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: runId,
      sequence: 3,
      payload: { status: CHAT_STATUS_COMPLETED },
    });
  }

  it('retires a summarized Run while retaining the active Run', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-prune',
    );
    seedFinishedRunEvents(sessionState, 'run-finished', 'assistant-finished');
    startRun(sessionState, {
      run_id: 'run-active',
      sse_url: '/api/runs/run-active/events',
      status: CHAT_STATUS_RUNNING,
      events: [
        {
          type: 'run_started',
          run_id: 'run-active',
          sequence: 1,
          payload: { status: CHAT_STATUS_RUNNING },
        },
      ],
    });

    loadHistory(
      sessionState,
      [
        {
          history_run_id: 'run-finished',
          id: 'user-one',
          role: 'user',
          content: 'Hi',
        },
        {
          history_run_id: 'run-finished',
          id: 'assistant-finished',
          role: 'assistant',
          content: 'Done.',
        },
        {
          history_run_id: 'run-finished',
          id: 'summary-finished',
          role: 'run_summary',
          run_id: 'run-finished',
          status: 'completed',
        },
        { id: 'user-two', role: 'user', content: 'Again' },
      ],
      {
        runs: [{ run_id: 'run-finished', status: 'completed', complete: true }],
      },
    );

    expect(sessionState.runEvents.map((event) => event.run_id)).toEqual([
      'run-active',
    ]);
    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
  });

  it.each([
    { name: 'without a Run snapshot', options: undefined },
    {
      name: 'whose snapshot is not complete',
      options: {
        runs: [
          { run_id: 'run-finished', status: 'completed', complete: false },
        ],
      },
    },
  ])(
    'keeps events of a finished Run $name while its output is not in the loaded page',
    ({ options }) => {
      const sessionState = ensureSessionState(
        createChatState(),
        'alpha',
        'session-prune-keep',
      );
      seedFinishedRunEvents(sessionState, 'run-finished', 'assistant-finished');
      startRun(sessionState, {
        run_id: 'run-active',
        sse_url: '/api/runs/run-active/events',
        status: CHAT_STATUS_RUNNING,
      });
      const runEventsBefore = [...sessionState.runEvents];

      loadHistory(
        sessionState,
        [{ id: 'user-one', role: 'user', content: 'Hi' }],
        options,
      );

      expect(sessionState.runEvents).toEqual(runEventsBefore);
      expect(
        countTimelineTextOccurrences(
          visibleTimelineItemsForRender(sessionState),
          'Done.',
        ),
      ).toBe(1);
    },
  );
});

describe('authoritative Timeline synchronization', () => {
  const state = () =>
    ensureSessionState(createChatState(), 'agent@project', 'session');

  const saved = (seq, run, role, content) => ({
    id: `message-${seq}`,
    history_sequence: seq,
    history_run_id: run,
    role,
    content,
  });

  const append = (session, run, sequence, type, payload = {}) =>
    appendRunEvent(session, {
      run_id: run,
      sequence,
      type,
      payload,
    });

  const outputs = (session) =>
    visibleTimelineItemsForRender(session).flatMap((item) =>
      item.type === 'message'
        ? [item.message.content]
        : (item.outputs ?? []).map((output) => output.content),
    );

  it('joins a bounded replay to its canonical prefix without a User or overlapping event', () => {
    const session = state();
    loadHistory(
      session,
      [
        saved(0, 'older', 'assistant', 'Previous Run'),
        saved(1, 'automatic', 'assistant', 'Persisted prefix'),
      ],
      { generation: 'g' },
    );
    startRun(session, { run_id: 'automatic' });
    append(session, 'automatic', 900, 'assistant_output_delta', {
      content_delta: 'New output',
    });
    const timeline = visibleTimelineItemsForRender(session);
    expect(outputs(session)).toEqual([
      'Previous Run',
      'Persisted prefix',
      'New output',
    ]);
    expect(timeline.at(-1).runId).toBe('automatic');
    expect(timeline.at(-1).outputs.map((item) => item.content)).toEqual([
      'Persisted prefix',
      'New output',
    ]);
  });

  it('does not treat matching stable output IDs as proof that a terminal Run is fully persisted', () => {
    const session = state();
    const prefix = saved(0, 'first', 'assistant', 'Persisted prefix');
    startRun(session, { run_id: 'first' });
    append(session, 'first', 1, 'assistant_output', { message: prefix });
    append(session, 'first', 2, 'assistant_output_delta', {
      content_delta: 'Still not durable',
    });
    append(session, 'first', 3, 'run_completed', { status: 'completed' });
    startRun(session, { run_id: 'second' });
    loadHistory(session, [prefix], { generation: 'g' });
    expect(outputs(session)).toContain('Still not durable');
    expect(session.runEvents.some((event) => event.run_id === 'first')).toBe(
      true,
    );
  });

  it('a lineage reset removes edited-away live Runs while retaining the new active Run', () => {
    const session = state();
    loadHistory(session, [saved(0, 'old', 'user', 'Old')], {
      generation: 'g',
      nextAfter: 'old-cursor',
    });
    startRun(session, { run_id: 'old' });
    append(session, 'old', 1, 'assistant_output_delta', {
      content_delta: 'Removed output',
    });
    append(session, 'old', 2, 'run_completed', { status: 'completed' });
    startRun(session, { run_id: 'new' });
    append(session, 'new', 1, 'assistant_output_delta', {
      content_delta: 'Current output',
    });
    loadHistory(session, [saved(3, 'new', 'user', 'Replacement')], {
      generation: 'g',
      reset: true,
      activeRunId: 'new',
      nextAfter: 'new-cursor',
    });
    expect(outputs(session)).toEqual(['Replacement', 'Current output']);
    expect(session.runEvents.every((event) => event.run_id === 'new')).toBe(
      true,
    );
  });
});

describe('stale Run reset', () => {
  it('clears the live Run projection and keeps loaded History', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-stale',
    );
    const history = [
      { id: 'user-one', role: 'user', content: 'Hi' },
      { id: 'assistant-one', role: 'assistant', content: 'Hello!' },
    ];
    loadHistory(sessionState, structuredClone(history));
    startRun(sessionState, {
      run_id: 'run-stale',
      sse_url: '/api/runs/run-stale/events',
      status: CHAT_STATUS_RUNNING,
      events: [
        {
          sequence: 1,
          run_id: 'run-stale',
          type: 'run_started',
          payload: { status: CHAT_STATUS_RUNNING },
        },
        {
          sequence: 2,
          run_id: 'run-stale',
          type: 'reasoning_delta',
          payload: { reasoning_delta: 'thinking...' },
        },
        {
          sequence: 3,
          run_id: 'run-stale',
          type: 'tool_call_started',
          payload: {
            tool_call: {
              id: 'call-one',
              index: 0,
              name: 'read',
              arguments: {},
            },
          },
        },
        {
          sequence: 4,
          run_id: 'run-stale',
          type: 'assistant_output_delta',
          payload: { content_delta: 'partial response' },
        },
      ],
    });
    const liveRunRendered = () =>
      visibleTimelineItemsForRender(sessionState).some(
        (item) => item.runId === 'run-stale',
      );
    expect(isRunActive(sessionState)).toBe(true);
    expect(liveRunRendered()).toBe(true);
    expect(highestContiguousRunEventSequence(sessionState)).toBe(4);

    resetStaleRun(sessionState);

    expect(sessionState.status).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.streamStatus).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.currentRun).toBeNull();
    expect(isRunActive(sessionState)).toBe(false);
    // Freshly loaded History is authoritative; no live replay renders or
    // resumes behind it after the stale Run marker is removed.
    expect(liveRunRendered()).toBe(false);
    expect(highestContiguousRunEventSequence(sessionState)).toBe(0);
    expect(sessionState.messages).toEqual(history);
  });
});
