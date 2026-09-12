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

  it('keeps output streamed after a mid-run history snapshot when the run finishes', () => {
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
      { id: 'user-one', role: 'user', content: 'Finish the investigation' },
      {
        id: 'assistant-at-entry',
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'call-at-entry',
            name: 'read',
            arguments: { path: 'before.txt' },
          },
        ],
      },
      {
        id: 'tool-at-entry',
        role: 'tool',
        tool_call_id: 'call-at-entry',
        name: 'read',
        content: 'before',
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
          content: 'Finish the investigation',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-at-entry',
          index: 0,
          name: 'read',
          arguments: { path: 'before.txt' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-one',
      sequence: 3,
      payload: {
        tool_call: { id: 'call-at-entry', name: 'read' },
        result: 'before',
        message: {
          id: 'tool-at-entry',
          role: 'tool',
          tool_call_id: 'call-at-entry',
          name: 'read',
          content: 'before',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-one',
      sequence: 4,
      payload: {
        tool_call: {
          id: 'call-after-entry',
          index: 0,
          name: 'read',
          arguments: { path: 'after.txt' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-one',
      sequence: 5,
      payload: {
        tool_call: { id: 'call-after-entry', name: 'read' },
        result: 'after',
        message: {
          id: 'tool-after-entry',
          role: 'tool',
          tool_call_id: 'call-after-entry',
          name: 'read',
          content: 'after',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 6,
      payload: {
        message: {
          id: 'assistant-final',
          role: 'assistant',
          content: 'This is the final answer.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 7,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    const timelineItems = visibleTimelineItemsForRender(sessionState);

    expect(timelineItems).toHaveLength(2);
    expect(timelineItems[1]).toEqual(
      expect.objectContaining({
        id: 'assistant-run-run-one',
        type: 'assistant_run',
        status: CHAT_STATUS_COMPLETED,
      }),
    );
    expect(timelineItems[1].tools.map((tool) => tool.toolCallId)).toEqual([
      'call-at-entry',
      'call-after-entry',
    ]);
    expect(timelineItems[1].outputs.map((output) => output.content)).toEqual([
      'This is the final answer.',
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
});
