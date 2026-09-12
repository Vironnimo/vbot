import { describe, expect, it } from 'vitest';
import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { pruneRunEventsPersistedInHistory } from '../chatTimeline.js';
import {
  CHAT_STATUS_RUNNING,
  CHAT_STATUS_COMPLETED,
  finishedRunEvents,
} from './chatTimeline.support.js';

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

describe('Provider heartbeat projection', () => {
  it('keeps the latest transport liveness measurement on the active run', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-heartbeat',
    );
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-heartbeat',
      sequence: 1,
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'provider_heartbeat',
      run_id: 'run-heartbeat',
      sequence: 2,
      timestamp: '2026-07-27T10:00:15Z',
      payload: {
        idle_seconds: 75.4,
        state: 'waiting_for_model_delta',
      },
    });

    const assistantRun = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run' && item.runId === 'run-heartbeat',
    );

    expect(assistantRun.providerHeartbeat).toEqual({
      idleSeconds: 75.4,
      timestamp: '2026-07-27T10:00:15Z',
    });
    expect(assistantRun.items).toEqual([]);

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-heartbeat',
      sequence: 3,
      payload: { content_delta: 'The buffered call is ready.' },
    });
    const progressedRun = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run' && item.runId === 'run-heartbeat',
    );

    expect(progressedRun.providerHeartbeat).toBeNull();
  });
});

describe('live compaction timeline projection', () => {
  it('keeps each checkpoint between the Tool steps surrounding its Run event', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-live-compaction',
    );
    loadHistory(sessionState, [
      { id: 'old-user', role: 'user', content: 'Earlier turn' },
      { id: 'old-assistant', role: 'assistant', content: 'Earlier answer' },
    ]);
    startRun(sessionState, {
      run_id: 'run-compaction',
      sse_url: '/api/runs/run-compaction/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-compaction',
      sequence: 1,
      payload: {
        message: {
          id: 'current-user',
          role: 'user',
          content: 'Keep working',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-compaction',
      sequence: 2,
      payload: {
        tool_call: { id: 'call-read', index: 0, name: 'read', arguments: {} },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-compaction',
      sequence: 3,
      payload: {
        tool_call: { id: 'call-read', index: 0, name: 'read' },
        result: { ok: true },
      },
    });
    appendRunEvent(sessionState, {
      type: 'compaction_started',
      run_id: 'run-compaction',
      sequence: 4,
      timestamp: '2026-07-29T17:55:20Z',
      payload: {
        context_tokens_before: 250_000,
      },
    });
    appendRunEvent(sessionState, {
      type: 'compaction_completed',
      run_id: 'run-compaction',
      sequence: 5,
      timestamp: '2026-07-29T17:55:25Z',
      payload: {
        context_tokens_before: 250_000,
        context_tokens_after: 30_000,
        message: {
          id: 'checkpoint-1',
          role: 'compaction_checkpoint',
          timestamp: '2026-07-29T17:55:25Z',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-compaction',
      sequence: 6,
      payload: {
        tool_call: { id: 'call-edit', index: 0, name: 'edit', arguments: {} },
      },
    });

    const items = visibleTimelineItemsForRender(sessionState);
    const liveRun = items.find(
      (item) =>
        item.type === 'assistant_run' && item.runId === 'run-compaction',
    );

    expect(liveRun.items.map((child) => child.type)).toEqual([
      'tool_call',
      'compaction_separator',
      'tool_call',
    ]);
    expect(liveRun.items[1].message.id).toBe('checkpoint-1');
    expect(liveRun.items[1].status).toBe(CHAT_STATUS_COMPLETED);
    expect(liveRun.items[1].contextTokensBefore).toBe(250_000);
    expect(liveRun.items[1].contextTokensAfter).toBe(30_000);
    expect(liveRun.items[1].events.map((event) => event.type)).toEqual([
      'compaction_started',
      'compaction_completed',
    ]);
    expect(
      sessionState.messages.some(
        (message) => message.role === 'compaction_checkpoint',
      ),
    ).toBe(false);
  });

  it('shows an in-progress checkpoint immediately and removes it when the attempt aborts', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-compaction-progress',
    );
    appendRunEvent(sessionState, {
      type: 'compaction_started',
      run_id: 'run-progress',
      sequence: 1,
      payload: { context_tokens_before: 250_000 },
    });

    // A run whose only child is the compaction divider renders the divider
    // bare instead of wrapped in a run block.
    const running = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'compaction_separator',
    );
    expect(running).toMatchObject({
      type: 'compaction_separator',
      status: CHAT_STATUS_RUNNING,
      contextTokensBefore: 250_000,
    });

    appendRunEvent(sessionState, {
      type: 'compaction_aborted',
      run_id: 'run-progress',
      sequence: 2,
      payload: { reason: 'failed' },
    });

    const items = visibleTimelineItemsForRender(sessionState);
    expect(
      items.filter((item) => item.type === 'compaction_separator'),
    ).toEqual([]);
    const aborted = items.find((item) => item.type === 'assistant_run');
    expect(aborted?.items ?? []).toEqual([]);
  });
});

describe('interrupted assistant turn projection', () => {
  function assistantOutputChild(sessionState, runId) {
    const run = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run' && item.runId === runId,
    );
    return run?.items.find((child) => child.type === 'assistant_output');
  }

  it('flags a live interrupted assistant_output event', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-live-interrupted',
    );
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-int',
      sequence: 1,
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-int',
      sequence: 2,
      payload: {
        message: {
          id: 'a-int',
          role: 'assistant',
          content: 'Half',
          interrupted: true,
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_interrupted',
      run_id: 'run-int',
      sequence: 3,
      payload: { status: 'interrupted', cause: 'network' },
    });

    const output = assistantOutputChild(sessionState, 'run-int');
    expect(output.content).toBe('Half');
    expect(output.interrupted).toBe(true);
  });

  it('drops an unfinished Tool preview when the Run is interrupted', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-interrupted-tool-preview',
    );
    startRun(sessionState, {
      run_id: 'run-tool-preview',
      sse_url: '/api/runs/run-tool-preview/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-tool-preview',
      sequence: 1,
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-tool-preview',
      sequence: 2,
      payload: {
        tool_call_id: 'call-partial',
        name_delta: 'subagent',
        arguments_delta: '{"action":"run","agent_id":"work',
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_interrupted',
      run_id: 'run-tool-preview',
      sequence: 3,
      payload: { status: 'interrupted', cause: 'network' },
    });

    const run = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run',
    );
    expect(run.status).toBe('interrupted');
    expect(run.tools).toEqual([]);
  });

  it('does not flag a normal assistant_output event', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-live-normal',
    );
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-normal',
      sequence: 1,
      payload: {
        message: { id: 'a-normal', role: 'assistant', content: 'All done' },
      },
    });

    const output = assistantOutputChild(sessionState, 'run-normal');
    expect(output.interrupted).toBe(false);
  });

  it('flags an interrupted assistant message loaded from history', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-history-interrupted',
    );
    loadHistory(sessionState, [
      { id: 'u1', role: 'user', content: 'Long question' },
      {
        id: 'a1',
        role: 'assistant',
        content: 'The first half',
        interrupted: true,
        run_id: 'run-hist',
      },
    ]);

    const run = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run',
    );
    const output = run.items.find((child) => child.type === 'assistant_output');
    expect(output.interrupted).toBe(true);
  });
});

describe('agent_takeover timeline projection', () => {
  it('projects a persisted agent_takeover message as a takeover_separator item', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-takeover',
    );
    loadHistory(sessionState, [
      { id: 'u1', role: 'user', content: 'Do the thing' },
      {
        id: 'a1',
        role: 'assistant',
        content: 'On it',
        run_id: 'run-1',
      },
      {
        id: 'takeover-1',
        role: 'agent_takeover',
        content: JSON.stringify({ from: 'assistant', to: 'builder@vbot' }),
        timestamp: '2026-06-22T10:00:00+00:00',
      },
      { id: 'u2', role: 'user', content: 'Continue' },
    ]);

    const items = visibleTimelineItemsForRender(sessionState);
    const separator = items.find((item) => item.type === 'takeover_separator');
    expect(separator).toBeTruthy();
    expect(separator.id).toBe('takeover-takeover-1');
    expect(separator.timestamp).toBe('2026-06-22T10:00:00+00:00');
    // The original message rides on the item so the presentation layer can
    // parse from/to from its content.
    expect(separator.message.content).toContain('builder@vbot');

    // It is a real divider between turns, not folded into an assistant run.
    const separatorIndex = items.indexOf(separator);
    expect(items[separatorIndex - 1].type).toBe('assistant_run');
    expect(items[separatorIndex + 1].type).toBe('message');
    expect(items[separatorIndex + 1].message.role).toBe('user');
  });

  it('breaks an assistant run at the takeover so it is not swallowed', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-takeover-break',
    );
    loadHistory(sessionState, [
      { id: 'u1', role: 'user', content: 'before turn' },
      { id: 'a1', role: 'assistant', content: 'before', run_id: 'run-1' },
      {
        id: 't1',
        role: 'agent_takeover',
        content: JSON.stringify({ from: 'a', to: 'b' }),
        timestamp: '2026-06-22T10:05:00+00:00',
      },
      { id: 'u2', role: 'user', content: 'after turn' },
      { id: 'a2', role: 'assistant', content: 'after', run_id: 'run-2' },
    ]);

    const items = visibleTimelineItemsForRender(sessionState);
    const separatorIndex = items.findIndex(
      (item) => item.type === 'takeover_separator',
    );
    expect(separatorIndex).toBeGreaterThan(0);
    // The assistant turn before the takeover is closed off at the divider, and
    // the turn after starts fresh — the takeover is never folded into a run.
    const beforeRun = items[separatorIndex - 1];
    expect(beforeRun.type).toBe('assistant_run');
    expect((beforeRun.outputs ?? []).map((output) => output.content)).toEqual([
      'before',
    ]);
    const afterUser = items[separatorIndex + 1];
    expect(afterUser.type).toBe('message');
    expect(afterUser.message.role).toBe('user');
  });
});
