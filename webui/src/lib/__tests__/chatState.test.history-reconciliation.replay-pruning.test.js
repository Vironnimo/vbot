import { describe, expect, it } from 'vitest';
import {
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_RUNNING,
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { countTimelineTextOccurrences } from './chatState.support.js';

describe('chat state helpers', () => {
  it('drops a completed prior run the WebSocket replays alongside the active run on refresh', () => {
    // On refresh the app WebSocket replays its retained lifecycle buffer from
    // sequence 0, re-injecting the already-completed parent run (the one that
    // spawned a non-blocking sub-agent) into runEvents next to the still-active
    // note-triggered follow-up run. The parent run carries its own
    // user_message_persisted plus assistant output, all already in history.
    // selectTrackedRunTimelineSource only reconciles the active run, so without
    // the inactive-run drop the parent turn (user message + first assistant
    // block) renders a second time from the replayed live events.
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-ws-replay',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Run a non-blocking worker' },
      {
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
        id: 'tool-subagent',
        role: 'tool',
        tool_call_id: 'call-subagent',
        name: 'subagent',
        content: '{"ok":true}',
      },
      {
        id: 'assistant-started',
        role: 'assistant',
        content: 'The worker is running.',
      },
      {
        id: 'summary-one',
        role: 'run_summary',
        run_id: 'run-one',
        status: 'completed',
        timing: { duration_ms: 10 },
      },
      {
        id: 'assistant-result',
        role: 'assistant',
        content: 'The worker finished: the answer is 42.',
      },
    ]);

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

  it('drops sparse summarized Run replay while a newer Run keeps streaming', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-sparse-replay',
    );
    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'First question' },
      { id: 'assistant-one', role: 'assistant', content: 'First answer' },
      {
        id: 'summary-one',
        role: 'run_summary',
        run_id: 'run-one',
        status: 'completed',
      },
      { id: 'user-two', role: 'user', content: 'Second question' },
    ]);
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

  it('preserves active streaming run events when history refreshes during a run', () => {
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
      payload: { content_delta: 'Hel' },
    });

    loadHistory(sessionState, [
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);

    expect(sessionState.streamingRunEvents).toEqual([
      expect.objectContaining({
        type: 'assistant_output_delta',
        payload: expect.objectContaining({ content_delta: 'Hel' }),
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

    loadHistory(sessionState, [
      { id: 'message-one', role: 'assistant', content: 'Done' },
      {
        id: 'summary-one',
        role: 'run_summary',
        run_id: 'run-one',
        status: CHAT_STATUS_COMPLETED,
      },
    ]);

    expect(sessionState.runEvents).toEqual([]);
    expect(sessionState.streamingRunEvents).toEqual([]);
  });
});

describe('loadHistory run-event pruning during an active run (handoff3 B10)', () => {
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

  it('drops events of a finished run whose output the loaded history persists, keeping the active run', () => {
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

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Hi' },
      { id: 'assistant-finished', role: 'assistant', content: 'Done.' },
      { id: 'user-two', role: 'user', content: 'Again' },
    ]);

    expect(sessionState.runEvents.map((event) => event.run_id)).toEqual([
      'run-active',
    ]);
    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
  });

  it('keeps events of a finished run whose output is not yet in the loaded history page', () => {
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

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Hi' },
    ]);

    expect(sessionState.runEvents).toEqual(runEventsBefore);
  });
});
