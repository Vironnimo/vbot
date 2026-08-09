import { describe, expect, it } from 'vitest';

import {
  CHAT_STATUS_CANCELLED,
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
  it('keeps one assistant run when SSE replay overlaps with persisted active run history', () => {
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

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'The file says A.',
      },
    ]);

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
      type: 'reasoning_delta',
      run_id: 'run-one',
      sequence: 2,
      payload: { reasoning_delta: 'Checking' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 3,
      payload: {
        message: {
          id: 'assistant-one',
          role: 'assistant',
          content: 'The file says A.',
        },
      },
    });

    const timelineItems = visibleTimelineItemsForRender(sessionState);

    expect(timelineItems).toHaveLength(2);
    expect(timelineItems[0]).toEqual(
      expect.objectContaining({ id: 'user-one', type: 'message' }),
    );
    expect(timelineItems[1]).toEqual(
      expect.objectContaining({
        id: 'assistant-run-run-one',
        type: 'assistant_run',
        outputs: [expect.objectContaining({ content: 'The file says A.' })],
      }),
    );
    expect(timelineItems[1].reasoning).toEqual([
      expect.objectContaining({ content: 'Checking', streaming: true }),
    ]);
  });

  it('uses persisted history after completed overlap instead of merging later live events', () => {
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

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'The file says A.',
      },
    ]);

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
      type: 'tool_call_started',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read',
          arguments: { path: 'a.txt' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 3,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    expect(visibleTimelineItemsForRender(sessionState)).toEqual([
      expect.objectContaining({ id: 'user-one', type: 'message' }),
      expect.objectContaining({
        id: 'history-run-assistant-one',
        type: 'assistant_run',
        status: CHAT_STATUS_COMPLETED,
        outputs: [expect.objectContaining({ content: 'The file says A.' })],
        tools: [],
      }),
    ]);
  });

  it('overlays a live cancellation onto history that has not loaded its Run Summary yet', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'orchestrator@project-one',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-parent',
      sse_url: '/api/runs/run-parent/events',
      status: CHAT_STATUS_RUNNING,
    });

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Start the planner' },
      {
        id: 'assistant-subagent',
        role: 'assistant',
        content: 'I will start the planner.',
        tool_calls: [
          {
            id: 'call-subagent',
            name: 'subagent',
            arguments: {
              agent_id: 'planner',
              background: false,
              content: 'Create the plan',
            },
          },
        ],
      },
    ]);

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-parent',
      sequence: 1,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Start the planner',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-parent',
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
    });
    appendRunEvent(sessionState, {
      type: 'run_cancelled',
      run_id: 'run-parent',
      sequence: 3,
      timestamp: '2026-07-27T12:11:57Z',
      payload: {
        status: CHAT_STATUS_CANCELLED,
        timing: { duration_ms: 1509909 },
      },
    });

    const timelineItems = visibleTimelineItemsForRender(sessionState);
    const assistantRun = timelineItems.find(
      (item) => item.type === 'assistant_run',
    );

    expect(timelineItems).toHaveLength(2);
    expect(assistantRun).toEqual(
      expect.objectContaining({
        id: 'history-run-assistant-subagent',
        runId: 'run-parent',
        status: CHAT_STATUS_CANCELLED,
        durationMs: 1509909,
      }),
    );
    expect(assistantRun.tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-subagent',
        status: CHAT_STATUS_CANCELLED,
      }),
    ]);
  });

  it('rehydrates interrupted reasoning-only output from persisted history', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Inspect the evidence' },
      {
        id: 'assistant-reasoning',
        role: 'assistant',
        content: null,
        reasoning: 'Inspect the evidence.',
        interrupted: true,
      },
      {
        id: 'summary-one',
        role: 'run_summary',
        run_id: 'run-cancelled',
        status: 'cancelled',
      },
    ]);

    const assistantRun = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run',
    );

    expect(assistantRun).toEqual(
      expect.objectContaining({
        source: 'history',
        status: 'cancelled',
        outputs: [],
        reasoning: [
          expect.objectContaining({
            content: 'Inspect the evidence.',
            streaming: false,
          }),
        ],
      }),
    );
  });

  it('uses persisted suffix history only when terminal live events overlap the same turn', () => {
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

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-tools',
        role: 'assistant',
        reasoning: 'Need to read it.',
        tool_calls: [
          {
            id: 'call-one',
            name: 'read',
            arguments: { path: 'a.txt' },
          },
        ],
      },
      {
        id: 'tool-one',
        role: 'tool',
        tool_call_id: 'call-one',
        name: 'read',
        content: '{"ok": true, "content": "A"}',
      },
      {
        id: 'assistant-final',
        role: 'assistant',
        content: 'The file says A.',
      },
    ]);

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
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 2,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    const timelineItems = visibleTimelineItemsForRender(sessionState);

    expect(timelineItems).toHaveLength(2);
    expect(timelineItems[1]).toEqual(
      expect.objectContaining({
        id: 'history-run-assistant-tools',
        type: 'assistant_run',
        status: CHAT_STATUS_COMPLETED,
      }),
    );
    expect(timelineItems[1].items.map((item) => item.type)).toEqual([
      'reasoning',
      'tool_call',
      'assistant_output',
    ]);
    expect(timelineItems[1].tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-one',
        name: 'read',
        result: '{"ok": true, "content": "A"}',
        status: 'success',
      }),
    ]);
    expect(timelineItems[1].outputs).toEqual([
      expect.objectContaining({ content: 'The file says A.' }),
    ]);
  });

  it('keeps one assistant run when terminal events arrive after history already overlaps the run', () => {
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

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'The file says A.',
      },
    ]);

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
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 3,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    expect(sessionState.status).toBe(CHAT_STATUS_COMPLETED);
    expect(visibleTimelineItemsForRender(sessionState)).toEqual([
      expect.objectContaining({ id: 'user-one', type: 'message' }),
      expect.objectContaining({
        id: 'history-run-assistant-one',
        type: 'assistant_run',
        status: CHAT_STATUS_COMPLETED,
        outputs: [expect.objectContaining({ content: 'The file says A.' })],
      }),
    ]);
  });

  it('does not duplicate a note-triggered run output that history already persisted', () => {
    // Refresh while the internal follow-up run a non-blocking sub-agent
    // completion spawns is still RUNNING. That run emits no
    // user_message_persisted event (its trigger is a hidden note), so it cannot
    // be anchored to a history user message. Its assistant output is already in
    // history, so the replayed live run must not render the turn a second time.
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-note-run',
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

    // Re-attach to the still-running note-triggered run (no user_message_persisted).
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

  it('keeps a note-triggered run output that history has not persisted yet', () => {
    // Same shape, but the run's output is not yet in history (mid-stream). The
    // live run must still render so the user sees in-flight output.
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-note-run-live',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Run a non-blocking worker' },
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
    ]);

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
