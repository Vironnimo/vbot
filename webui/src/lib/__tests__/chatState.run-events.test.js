import { describe, expect, it } from 'vitest';
import {
  CHAT_STATUS_CANCELLED,
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_FAILED,
  CHAT_STATUS_RUNNING,
  appendRunEvent,
  applyRunControls,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';

describe('live Run events', () => {
  it('builds a visible timeline from history and live assistant runs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    loadHistory(sessionState, [
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      sequence: 1,
      payload: { message: { role: 'assistant', content: 'Hello' } },
    });

    expect(visibleTimelineItemsForRender(sessionState)).toEqual([
      {
        id: 'message-one',
        type: 'message',
        message: { id: 'message-one', role: 'user', content: 'Hi' },
      },
      expect.objectContaining({
        id: 'assistant-run-run',
        type: 'assistant_run',
        outputs: [expect.objectContaining({ content: 'Hello' })],
      }),
    ]);
  });

  it('groups live reasoning, tool lifecycle, and final output into one assistant run', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-one',
      sequence: 1,
      payload: { reasoning_delta: 'Think' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read_file',
          arguments: { path: 'a.txt' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-one',
      sequence: 3,
      payload: {
        tool_call: { id: 'call-one', index: 0, name: 'read_file' },
        result: { ok: true, content: 'File contents' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 4,
      payload: { message: { role: 'assistant', content: 'Hi' } },
    });

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);

    expect(assistantRun).toEqual(
      expect.objectContaining({
        id: 'assistant-run-run-one',
        type: 'assistant_run',
        runId: 'run-one',
      }),
    );
    expect(assistantRun.items.map((item) => item.type)).toEqual([
      'reasoning',
      'tool_call',
      'assistant_output',
    ]);
    expect(assistantRun.reasoning).toEqual([
      expect.objectContaining({ content: 'Think', streaming: true }),
    ]);
    expect(assistantRun.tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-one',
        name: 'read_file',
        arguments: { path: 'a.txt' },
        result: { ok: true, content: 'File contents' },
        status: 'success',
      }),
    ]);
    expect(assistantRun.outputs).toEqual([
      expect.objectContaining({ content: 'Hi', streaming: false }),
    ]);
  });

  it('appends each stable Run event once against the currently retained events', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    const event = (runId, sequence, type = 'tool_call_started') => ({
      type,
      run_id: runId,
      sequence,
      payload: {},
    });

    appendRunEvent(sessionState, event('run-a', 1, 'run_started'));
    appendRunEvent(sessionState, event('run-a', 2));
    appendRunEvent(sessionState, event('run-a', 2));
    appendRunEvent(sessionState, event('run-b', 2));
    expect(
      sessionState.runEvents.map(({ run_id, sequence }) => [run_id, sequence]),
    ).toEqual([
      ['run-a', 1],
      ['run-a', 2],
      ['run-b', 2],
    ]);

    // Another owner (History reconciliation, edits) replaces the retained
    // events; deduplication follows the replacement, not the old contents.
    sessionState.runEvents = sessionState.runEvents.filter(
      (runEvent) => runEvent.run_id === 'run-b',
    );
    appendRunEvent(sessionState, event('run-a', 2));
    appendRunEvent(sessionState, event('run-b', 2));
    expect(
      sessionState.runEvents.map(({ run_id, sequence }) => [run_id, sequence]),
    ).toEqual([
      ['run-b', 2],
      ['run-a', 2],
    ]);
  });

  it('marks a session running when a run_started event arrives from server push', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-from-ws',
      sequence: 1,
      timestamp: '2026-08-05T18:04:00.000Z',
      payload: {},
    });

    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.streamStatus).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.currentRun).toMatchObject({
      runId: 'run-from-ws',
      status: CHAT_STATUS_RUNNING,
      startedAt: '2026-08-05T18:04:00.000Z',
    });
  });

  it('treats model fallback activation as an assistant-run event and appends a fallback item', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'model_fallback_activated',
      run_id: 'run-one',
      sequence: 2,
      timestamp: '2026-05-15T10:00:00Z',
      payload: {
        from_model: 'openai/gpt-5',
        to_model: 'openrouter/anthropic/claude-sonnet-4',
      },
    });

    const timelineItems = visibleTimelineItemsForRender(sessionState);
    const [assistantRun] = timelineItems;

    expect(timelineItems).toHaveLength(1);
    expect(assistantRun).toEqual(
      expect.objectContaining({
        id: 'assistant-run-run-one',
        type: 'assistant_run',
        runId: 'run-one',
      }),
    );
    expect(assistantRun.items).toEqual([
      expect.objectContaining({
        type: 'model_fallback',
        content: 'openrouter/anthropic/claude-sonnet-4',
        from_model: 'openai/gpt-5',
        to_model: 'openrouter/anthropic/claude-sonnet-4',
      }),
    ]);
  });

  it('preserves first-seen child ordering when later reasoning updates arrive', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-one',
      sequence: 1,
      payload: { reasoning_delta: 'Plan' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read_file',
          arguments: { path: 'a.txt' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-one',
      sequence: 3,
      payload: { reasoning_delta: ' more' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 4,
      payload: { content_delta: 'Done' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 5,
      payload: { content_delta: ' now' },
    });

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);

    expect(assistantRun.items.map((item) => item.type)).toEqual([
      'reasoning',
      'tool_call',
      'assistant_output',
    ]);
    // Reasoning streamed before and after a still-pending tool call merges into
    // one reasoning block; the streamed answer stays a separate output row.
    expect(assistantRun.reasoning.map((item) => item.content)).toEqual([
      'Plan more',
    ]);
    expect(assistantRun.outputs.map((item) => item.content)).toEqual([
      'Done now',
    ]);
  });

  it('merges live tool and run timing from events', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-timing-live',
    );
    const timing = {
      started_at: '2026-05-03T14:30:01+00:00',
      completed_at: '2026-05-03T14:30:02.250+00:00',
      duration_ms: 1250,
    };

    appendRunEvent(sessionState, {
      sequence: 1,
      run_id: 'run-one',
      type: 'run_started',
      timestamp: '2026-05-03T14:30:00+00:00',
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      sequence: 2,
      run_id: 'run-one',
      type: 'tool_call_started',
      timestamp: '2026-05-03T14:30:01+00:00',
      payload: {
        tool_call: { id: 'call-one', index: 0, name: 'read', arguments: {} },
      },
    });
    appendRunEvent(sessionState, {
      sequence: 3,
      run_id: 'run-one',
      type: 'tool_call_result',
      timestamp: '2026-05-03T14:30:02+00:00',
      payload: {
        tool_call: { id: 'call-one', index: 0, name: 'read' },
        result: { ok: true, error: null, data: {}, artifacts: [] },
        timing,
      },
    });
    appendRunEvent(sessionState, {
      sequence: 4,
      run_id: 'run-one',
      type: 'run_completed',
      timestamp: '2026-05-03T14:30:03+00:00',
      payload: { status: CHAT_STATUS_COMPLETED, timing },
    });

    const assistantRun = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run',
    );

    expect(assistantRun.durationMs).toBe(1250);
    expect(assistantRun.tools[0].durationMs).toBe(1250);
  });

  it('keeps live Run controls authoritative across stale snapshots and other Runs', () => {
    const session = ensureSessionState(
      createChatState(),
      'agent@project',
      'session',
    );
    startRun(session, { run_id: 'one', status: 'running' });
    appendRunEvent(session, {
      run_id: 'one',
      sequence: 5,
      type: 'run_controls_changed',
      payload: { compaction: 'pending' },
    });
    applyRunControls(session, {
      run_id: 'one',
      controls_sequence: 4,
      controls: { compaction: 'idle' },
    });
    applyRunControls(session, {
      run_id: 'other',
      controls_sequence: 6,
      controls: { compaction: 'idle' },
    });
    expect(session.currentRun.controls.compaction).toBe('pending');
    applyRunControls(session, {
      run_id: 'one',
      controls_sequence: 6,
      controls: { compaction: 'running' },
    });
    expect(session.currentRun.controls.compaction).toBe('running');
  });
});

describe('Tool-use loops', () => {
  it.each([
    {
      phase: 'assistant output',
      type: 'assistant_output_delta',
      field: 'content_delta',
      child: 'assistant_output',
      list: 'outputs',
    },
    {
      phase: 'reasoning',
      type: 'reasoning_delta',
      field: 'reasoning_delta',
      child: 'reasoning',
      list: 'reasoning',
    },
  ])(
    'keeps distinct $phase phases across a tool-use loop',
    ({ type, field, child, list }) => {
      const sessionState = ensureSessionState(
        createChatState(),
        'alpha',
        'session-one',
      );
      const append = (sequence, eventType, payload) =>
        appendRunEvent(sessionState, {
          type: eventType,
          run_id: 'run-one',
          sequence,
          payload,
        });

      append(1, type, { [field]: 'First phase' });
      append(2, 'tool_call_started', {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read_file',
          arguments: { path: 'a.txt' },
        },
      });
      append(3, 'tool_call_result', {
        tool_call: { id: 'call-one', index: 0, name: 'read_file' },
        result: { ok: true, content: 'A' },
      });
      append(4, type, { [field]: 'Second phase' });

      const [assistantRun] = visibleTimelineItemsForRender(sessionState);
      expect(assistantRun.items.map((item) => item.type)).toEqual([
        child,
        'tool_call',
        child,
      ]);
      expect(assistantRun[list].map((item) => item.content)).toEqual([
        'First phase',
        'Second phase',
      ]);
    },
  );

  it('merges tool started and result events into success, running, and failed rows', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-one',
      sequence: 1,
      payload: {
        tool_call: { id: 'call-success', index: 0, name: 'ok_tool' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        tool_call: { id: 'call-success', index: 0, name: 'ok_tool' },
        result: { ok: true },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-one',
      sequence: 3,
      payload: {
        tool_call: { id: 'call-running', index: 1, name: 'slow_tool' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-one',
      sequence: 4,
      payload: {
        tool_call: { id: 'call-failed', index: 2, name: 'bad_tool' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-one',
      sequence: 5,
      payload: {
        tool_call: { id: 'call-failed', index: 2, name: 'bad_tool' },
        result: { ok: false, error: 'Denied' },
      },
    });

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);

    expect(assistantRun.tools).toHaveLength(3);
    expect(assistantRun.tools.map((tool) => tool.toolCallId)).toEqual([
      'call-success',
      'call-running',
      'call-failed',
    ]);
    expect(assistantRun.tools.map((tool) => tool.status)).toEqual([
      'success',
      CHAT_STATUS_RUNNING,
      CHAT_STATUS_FAILED,
    ]);
  });

  it('keeps every known Tool Call visible and settles the unfinished ones when the Run is cancelled', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-cancelled-siblings',
    );
    startRun(sessionState, {
      run_id: 'run-cancelled-siblings',
      sse_url: '/api/runs/run-cancelled-siblings/events',
      status: CHAT_STATUS_RUNNING,
    });
    const append = (sequence, type, payload, extra = {}) =>
      appendRunEvent(sessionState, {
        type,
        run_id: 'run-cancelled-siblings',
        sequence,
        payload,
        ...extra,
      });

    // 12 streamed calls: 4 started, 2 of those failed, 8 still preparing.
    for (let index = 0; index < 12; index += 1) {
      append(index + 1, 'tool_call_delta', {
        tool_call_id: `call-${index}`,
        name_delta: 'bash',
        arguments_delta: `{"command":"command ${index}"}`,
      });
    }
    for (let index = 0; index < 4; index += 1) {
      append(13 + index, 'tool_call_started', {
        tool_call: {
          id: `call-${index}`,
          index,
          name: 'bash',
          arguments: { command: `command ${index}` },
        },
      });
    }
    for (let index = 0; index < 2; index += 1) {
      append(17 + index, 'tool_call_result', {
        tool_call: { id: `call-${index}`, index, name: 'bash' },
        result: { ok: false, error: 'Command failed' },
      });
    }
    append(
      19,
      'run_cancelled',
      { status: CHAT_STATUS_CANCELLED },
      { timestamp: '2026-08-07T12:00:00.000Z' },
    );

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);
    expect(assistantRun.status).toBe(CHAT_STATUS_CANCELLED);
    expect(assistantRun.tools.map((tool) => tool.toolCallId)).toEqual(
      Array.from({ length: 12 }, (_value, index) => `call-${index}`),
    );
    expect(assistantRun.tools.map((tool) => tool.status)).toEqual([
      'failed',
      'failed',
      ...Array.from({ length: 10 }, () => CHAT_STATUS_CANCELLED),
    ]);
    expect(assistantRun.tools[2]).toMatchObject({
      endTimestamp: '2026-08-07T12:00:00.000Z',
      resultEvent: null,
    });
  });

  it('keeps finalized reasoning visible when a reasoning-only run is cancelled', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-cancelled',
      sse_url: '/api/runs/run-cancelled/events',
      status: CHAT_STATUS_RUNNING,
    });

    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-cancelled',
      sequence: 1,
      payload: { reasoning_delta: 'Inspect the evidence.' },
    });
    appendRunEvent(sessionState, {
      type: 'reasoning',
      run_id: 'run-cancelled',
      sequence: 2,
      payload: {
        message: {
          id: 'assistant-reasoning',
          role: 'assistant',
          content: null,
          reasoning: 'Inspect the evidence.',
          interrupted: true,
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-cancelled',
      sequence: 3,
      payload: {
        message: {
          id: 'assistant-reasoning',
          role: 'assistant',
          content: null,
          reasoning: 'Inspect the evidence.',
          interrupted: true,
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_cancelled',
      run_id: 'run-cancelled',
      sequence: 4,
      payload: { status: CHAT_STATUS_CANCELLED },
    });

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);

    expect(assistantRun.status).toBe(CHAT_STATUS_CANCELLED);
    expect(assistantRun.reasoning).toEqual([
      expect.objectContaining({
        content: 'Inspect the evidence.',
        streaming: false,
      }),
    ]);
    expect(assistantRun.outputs).toEqual([]);
  });
});

describe('Run order', () => {
  it('keeps new runs ordered after older runs without nesting tool rows', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-old',
      sequence: 1,
      payload: {
        tool_call: { id: 'old-tool', index: 0, name: 'old_tool' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-new',
      sequence: 3,
      payload: {
        tool_call: { id: 'new-tool', index: 0, name: 'new_tool' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-old',
      sequence: 4,
      payload: {
        tool_call: { id: 'old-tool', index: 0, name: 'old_tool' },
        result: { ok: true },
      },
    });

    const assistantRuns = visibleTimelineItemsForRender(sessionState);

    expect(assistantRuns.map((item) => item.runId)).toEqual([
      'run-old',
      'run-new',
    ]);
    expect(assistantRuns[0].tools).toEqual([
      expect.objectContaining({ toolCallId: 'old-tool', status: 'success' }),
    ]);
    expect(assistantRuns[1].tools).toEqual([
      expect.objectContaining({
        toolCallId: 'new-tool',
        status: CHAT_STATUS_RUNNING,
      }),
    ]);
  });

  it('orders each live Run user event before its assistant block by Run arrival', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-one',
      sequence: 1,
      timestamp: '2026-05-07T10:00:00Z',
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-one',
      sequence: 2,
      timestamp: '2026-05-07T10:00:01Z',
      payload: {
        message: { id: 'user-one', role: 'user', content: 'First request' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-one',
      sequence: 3,
      timestamp: '2026-05-07T10:00:02Z',
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read_file',
          arguments: { path: 'a.txt' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-one',
      sequence: 4,
      timestamp: '2026-05-07T10:00:03Z',
      payload: {
        tool_call: { id: 'call-one', index: 0, name: 'read_file' },
        result: { ok: true, content: 'A' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 5,
      timestamp: '2026-05-07T10:00:04Z',
      payload: { message: { role: 'assistant', content: 'First answer' } },
    });
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-two',
      sequence: 1,
      timestamp: '2026-05-07T10:01:00Z',
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-two',
      sequence: 2,
      timestamp: '2026-05-07T10:01:01Z',
      payload: {
        message: { id: 'user-two', role: 'user', content: 'Second request' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-two',
      sequence: 3,
      timestamp: '2026-05-07T10:01:02Z',
      payload: { reasoning_delta: 'Planning' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-two',
      sequence: 4,
      timestamp: '2026-05-07T10:01:03Z',
      payload: {
        tool_call: {
          id: 'call-two',
          index: 0,
          name: 'list_files',
          arguments: { path: '.' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-two',
      sequence: 5,
      timestamp: '2026-05-07T10:01:04Z',
      payload: {
        tool_call: { id: 'call-two', index: 0, name: 'list_files' },
        result: { ok: true, content: ['a.txt'] },
      },
    });

    const timelineItems = visibleTimelineItemsForRender(sessionState);

    // Run-local sequences restart; order follows Run arrival.
    expect(timelineItems.map((item) => item.id)).toEqual([
      'event-run-one-2',
      'assistant-run-run-one',
      'event-run-two-2',
      'assistant-run-run-two',
    ]);
    expect(timelineItems[0].event.payload.message.content).toBe(
      'First request',
    );
    expect(timelineItems[1]).toEqual(
      expect.objectContaining({ runId: 'run-one', type: 'assistant_run' }),
    );
    expect(timelineItems[2].event.payload.message.content).toBe(
      'Second request',
    );
    expect(timelineItems[3]).toEqual(
      expect.objectContaining({ runId: 'run-two', type: 'assistant_run' }),
    );
    expect(timelineItems[1].tools).toEqual([
      expect.objectContaining({ toolCallId: 'call-one', status: 'success' }),
    ]);
    expect(timelineItems[3].tools).toEqual([
      expect.objectContaining({ toolCallId: 'call-two', status: 'success' }),
    ]);
    expect(timelineItems[3].reasoning).toEqual([
      expect.objectContaining({ content: 'Planning', streaming: true }),
    ]);
  });
});
