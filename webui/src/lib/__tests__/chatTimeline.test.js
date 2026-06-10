import { describe, expect, it } from 'vitest';

import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { pruneRunEventsPersistedInHistory } from '../chatTimeline.js';

const CHAT_STATUS_RUNNING = 'running';
const CHAT_STATUS_COMPLETED = 'completed';

function finishedRunEvents(runId, messageId) {
  return [
    {
      type: 'user_message_persisted',
      run_id: runId,
      sequence: 1,
      payload: {
        message: { id: `user-${runId}`, role: 'user', content: 'Hi' },
      },
    },
    {
      type: 'run_started',
      run_id: runId,
      sequence: 2,
      payload: { status: CHAT_STATUS_RUNNING },
    },
    {
      type: 'assistant_output',
      run_id: runId,
      sequence: 3,
      payload: {
        message: { id: messageId, role: 'assistant', content: 'Done.' },
      },
    },
    {
      type: 'run_completed',
      run_id: runId,
      sequence: 4,
      payload: { status: CHAT_STATUS_COMPLETED },
    },
  ];
}

describe('pruneRunEventsPersistedInHistory (handoff3 B10)', () => {
  it('drops every event of a non-active run whose output messages are all persisted', () => {
    const runEvents = [
      ...finishedRunEvents('run-finished', 'assistant-finished'),
      {
        type: 'run_started',
        run_id: 'run-active',
        sequence: 1,
        payload: { status: CHAT_STATUS_RUNNING },
      },
    ];
    const messages = [
      { id: 'user-run-finished', role: 'user', content: 'Hi' },
      { id: 'assistant-finished', role: 'assistant', content: 'Done.' },
    ];

    const prunedEvents = pruneRunEventsPersistedInHistory(
      runEvents,
      messages,
      'run-active',
    );

    expect(prunedEvents.map((event) => event.run_id)).toEqual(['run-active']);
  });

  it('keeps a non-active run whose output is not fully persisted in the page', () => {
    const runEvents = finishedRunEvents('run-finished', 'assistant-finished');

    const prunedEvents = pruneRunEventsPersistedInHistory(
      runEvents,
      [{ id: 'user-other', role: 'user', content: 'Other' }],
      'run-active',
    );

    expect(prunedEvents).toBe(runEvents);
  });

  it('keeps runs that produced no persisted output messages at all', () => {
    const runEvents = [
      {
        type: 'run_started',
        run_id: 'run-empty',
        sequence: 1,
        payload: { status: CHAT_STATUS_RUNNING },
      },
      {
        type: 'run_completed',
        run_id: 'run-empty',
        sequence: 2,
        payload: { status: CHAT_STATUS_COMPLETED },
      },
    ];

    const prunedEvents = pruneRunEventsPersistedInHistory(
      runEvents,
      [],
      'run-active',
    );

    expect(prunedEvents).toBe(runEvents);
  });

  it('never prunes the active run, even when its output is already persisted', () => {
    const runEvents = finishedRunEvents('run-active', 'assistant-active');
    const messages = [
      { id: 'user-run-active', role: 'user', content: 'Hi' },
      { id: 'assistant-active', role: 'assistant', content: 'Done.' },
    ];

    const prunedEvents = pruneRunEventsPersistedInHistory(
      runEvents,
      messages,
      'run-active',
    );

    expect(prunedEvents).toBe(runEvents);
  });
});

describe('terminal-run projection memoization (handoff3 B10)', () => {
  function seedSession() {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-memo',
    );
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-finished',
      sequence: 1,
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-finished',
      sequence: 2,
      payload: {
        message: {
          id: 'assistant-finished',
          role: 'assistant',
          content: 'First answer.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-finished',
      sequence: 3,
      payload: { status: CHAT_STATUS_COMPLETED },
    });
    startRun(sessionState, {
      run_id: 'run-active',
      sse_url: '/api/runs/run-active/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-active',
      sequence: 1,
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-active',
      sequence: 2,
      payload: { content_delta: 'Streaming…' },
    });
    return sessionState;
  }

  function assistantRunById(timelineItems, runId) {
    return timelineItems.find(
      (item) => item.type === 'assistant_run' && item.runId === runId,
    );
  }

  it('reuses the finished run projection across flushes while rebuilding the active run', () => {
    const sessionState = seedSession();

    const firstRender = visibleTimelineItemsForRender(sessionState);
    const secondRender = visibleTimelineItemsForRender(sessionState);

    const firstFinishedRun = assistantRunById(firstRender, 'run-finished');
    const secondFinishedRun = assistantRunById(secondRender, 'run-finished');
    expect(secondFinishedRun.items).toBe(firstFinishedRun.items);
    expect(secondFinishedRun.events).toBe(firstFinishedRun.events);

    const firstActiveRun = assistantRunById(firstRender, 'run-active');
    const secondActiveRun = assistantRunById(secondRender, 'run-active');
    expect(secondActiveRun.items).not.toBe(firstActiveRun.items);
  });

  it('rebuilds a memoized run when a late event for it arrives', () => {
    const sessionState = seedSession();
    const initialRender = visibleTimelineItemsForRender(sessionState);
    const initialFinishedRun = assistantRunById(initialRender, 'run-finished');

    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-finished',
      sequence: 4,
      payload: {
        tool_call: { id: 'call-late', index: 0, name: 'read' },
        result: { ok: true },
      },
    });
    const nextRender = visibleTimelineItemsForRender(sessionState);
    const nextFinishedRun = assistantRunById(nextRender, 'run-finished');

    expect(nextFinishedRun.items).not.toBe(initialFinishedRun.items);
    expect(nextFinishedRun.tools.map((tool) => tool.toolCallId)).toEqual([
      'call-late',
    ]);
  });
});
