import { describe, expect, it } from 'vitest';

import {
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_CANCELLED,
  CHAT_STATUS_FAILED,
  CHAT_STATUS_RUNNING,
  assistantRunChildProgressKey,
  addServerQueuedMessage,
  appendRunEvent,
  canCreateNewSession,
  createChatState,
  ensureSessionState,
  isSessionEmpty,
  loadHistory,
  removeQueuedMessage,
  syncQueueFromServer,
  startRun,
  updateQueuedMessageContent,
  visibleTimelineItemsForRender,
} from '../chatState.js';

describe('chat state helpers', () => {
  it('test_syncQueueFromServer_replaces_entire_queue', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    addServerQueuedMessage(sessionState, {
      id: 'queue-old',
      content: 'Old message',
      created_at: '2026-05-21T00:00:00+00:00',
    });
    syncQueueFromServer(sessionState, [
      {
        id: 'queue-1',
        content: 'First message',
        editable: true,
        created_at: '2026-05-22T01:00:00+00:00',
      },
      {
        id: 'queue-2',
        content: 'Second message',
        editable: false,
        created_at: '2026-05-22T01:01:00+00:00',
      },
    ]);

    expect(sessionState.queue).toEqual([
      {
        id: 'queue-1',
        content: 'First message',
        editable: true,
        created_at: '2026-05-22T01:00:00+00:00',
      },
      {
        id: 'queue-2',
        content: 'Second message',
        editable: false,
        created_at: '2026-05-22T01:01:00+00:00',
      },
    ]);
  });

  it('test_addServerQueuedMessage_appends_to_queue', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    addServerQueuedMessage(sessionState, {
      id: 'queue-1',
      content: 'First message',
      created_at: '2026-05-22T01:00:00+00:00',
    });
    addServerQueuedMessage(sessionState, {
      id: 'queue-2',
      content: 'Second message',
      created_at: '2026-05-22T01:01:00+00:00',
    });

    expect(sessionState.queue.map((item) => item.id)).toEqual([
      'queue-1',
      'queue-2',
    ]);
  });

  it('test_updateQueuedMessageContent_mutates_matching_item', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    addServerQueuedMessage(sessionState, {
      id: 'queue-1',
      content: 'Original content',
      editable: true,
      created_at: '2026-05-22T01:00:00+00:00',
    });

    const updated = updateQueuedMessageContent(
      sessionState,
      'queue-1',
      'Updated content',
      { editable: false },
    );

    expect(updated).toBe(true);
    expect(sessionState.queue[0].content).toBe('Updated content');
    expect(sessionState.queue[0].editable).toBe(false);
    expect(
      updateQueuedMessageContent(sessionState, 'queue-missing', 'Anything'),
    ).toBe(false);
  });

  it('removeQueuedMessage removes the matching queued item', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    addServerQueuedMessage(sessionState, {
      id: 'queue-1',
      content: 'First message',
      created_at: '2026-05-22T01:00:00+00:00',
    });
    addServerQueuedMessage(sessionState, {
      id: 'queue-2',
      content: 'Second message',
      created_at: '2026-05-22T01:01:00+00:00',
    });

    expect(removeQueuedMessage(sessionState, 'queue-1')).toBe(true);
    expect(sessionState.queue).toEqual([
      {
        id: 'queue-2',
        content: 'Second message',
        editable: false,
        created_at: '2026-05-22T01:01:00+00:00',
      },
    ]);
    expect(removeQueuedMessage(sessionState, 'queue-missing')).toBe(false);
  });

  it('blocks new session creation only while the current session has a run', () => {
    const state = createChatState();
    const sessionState = ensureSessionState(state, 'alpha', 'session-one');

    expect(canCreateNewSession(sessionState)).toBe(true);

    startRun(sessionState, {
      run_id: 'run-one',
      sse_url: '/api/runs/run-one/events',
      status: CHAT_STATUS_RUNNING,
    });

    expect(canCreateNewSession(sessionState)).toBe(false);

    appendRunEvent(sessionState, {
      type: 'run_completed',
      sequence: 2,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    expect(canCreateNewSession(sessionState)).toBe(true);
  });

  it('classifies only loaded sessions without conversation activity as empty', () => {
    const state = createChatState();
    const sessionState = ensureSessionState(state, 'alpha', 'session-one');

    expect(isSessionEmpty(sessionState)).toBe(false);

    loadHistory(sessionState, []);
    expect(isSessionEmpty(sessionState)).toBe(true);

    addServerQueuedMessage(sessionState, {
      id: 'queue-one',
      content: 'Waiting message',
      created_at: '2026-05-22T01:00:00+00:00',
    });
    expect(isSessionEmpty(sessionState)).toBe(false);

    syncQueueFromServer(sessionState, []);
    loadHistory(sessionState, [
      { id: 'message-one', role: 'user', content: 'Hello' },
    ]);
    expect(isSessionEmpty(sessionState)).toBe(false);
  });

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

    expect(sessionState.runEvents).toHaveLength(3);
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
      payload: {},
    });

    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.streamStatus).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.currentRun).toEqual({
      runId: 'run-from-ws',
      sseUrl: '',
      status: CHAT_STATUS_RUNNING,
      iterationCount: 0,
    });
  });

  it('merges live tool stdout and stderr into the matching assistant-run tool row', () => {
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
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'bash',
          arguments: { command: 'printf hello' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_stdout',
      run_id: 'run-one',
      sequence: 2,
      payload: { tool_call_id: 'call-one', data: 'hel' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_stdout',
      run_id: 'run-one',
      sequence: 3,
      payload: { tool_call_id: 'call-one', data: 'lo\n' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_stderr',
      run_id: 'run-one',
      sequence: 4,
      payload: { tool_call_id: 'call-one', data: 'warn\n' },
    });

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);
    const [tool] = assistantRun.tools;

    expect(tool).toEqual(
      expect.objectContaining({
        toolCallId: 'call-one',
        stdout: 'hello\n',
        stderr: 'warn\n',
      }),
    );
    expect(assistantRunChildProgressKey(tool)).toContain(':11:');
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

  it('keeps distinct assistant output phases across a tool-use loop', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 1,
      payload: { content_delta: 'First answer' },
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
        result: { ok: true, content: 'A' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 4,
      payload: { content_delta: 'Second answer' },
    });

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);

    expect(assistantRun.items.map((item) => item.type)).toEqual([
      'assistant_output',
      'tool_call',
      'assistant_output',
    ]);
    expect(assistantRun.outputs.map((item) => item.content)).toEqual([
      'First answer',
      'Second answer',
    ]);
  });

  it('keeps distinct reasoning phases across a tool-use loop', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-one',
      sequence: 1,
      payload: { reasoning_delta: 'Plan first' },
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
        result: { ok: true, content: 'A' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-one',
      sequence: 4,
      payload: { reasoning_delta: 'Plan second' },
    });

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);

    expect(assistantRun.items.map((item) => item.type)).toEqual([
      'reasoning',
      'tool_call',
      'reasoning',
    ]);
    expect(assistantRun.reasoning.map((item) => item.content)).toEqual([
      'Plan first',
      'Plan second',
    ]);
  });

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

  it('marks pending tool rows cancelled when a run is cancelled', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-cancelled',
      sequence: 1,
      timestamp: '2026-01-01T00:00:00.000Z',
      payload: {
        tool_call: { id: 'call-bash', index: 0, name: 'bash' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_cancelled',
      run_id: 'run-cancelled',
      sequence: 2,
      timestamp: '2026-01-01T00:00:01.000Z',
      payload: { status: CHAT_STATUS_CANCELLED },
    });

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);

    expect(assistantRun.status).toBe(CHAT_STATUS_CANCELLED);
    expect(assistantRun.tools).toHaveLength(1);
    expect(assistantRun.tools[0]).toEqual(
      expect.objectContaining({
        status: CHAT_STATUS_CANCELLED,
        endTimestamp: '2026-01-01T00:00:01.000Z',
        cancelledEvent: expect.objectContaining({ type: 'run_cancelled' }),
      }),
    );
    expect(assistantRun.tools[0].resultEvent).toBeNull();
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

    expect(sessionState.streamingRunEvents).toEqual([
      expect.objectContaining({
        type: 'reasoning_delta',
        payload: expect.objectContaining({
          reasoning_delta: 'Inspect the evidence.',
        }),
      }),
    ]);
    expect(assistantRun.status).toBe(CHAT_STATUS_CANCELLED);
    expect(assistantRun.reasoning).toEqual([
      expect.objectContaining({
        content: 'Inspect the evidence.',
        streaming: false,
      }),
    ]);
    expect(assistantRun.outputs).toEqual([]);
  });

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

  it('renders a separate live run after non-overlapping persisted history', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-non-overlap',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'First request' },
      { id: 'assistant-one', role: 'assistant', content: 'First answer' },
    ]);
    startRun(sessionState, {
      run_id: 'run-two',
      sse_url: '/api/runs/run-two/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-two',
      sequence: 1,
      payload: {
        message: { id: 'user-two', role: 'user', content: 'Second request' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-two',
      sequence: 2,
      payload: { content_delta: 'Second answer' },
    });

    const timelineItems = visibleTimelineItemsForRender(sessionState);

    expect(timelineItems.map((item) => item.type)).toEqual([
      'message',
      'assistant_run',
      'event',
      'assistant_run',
    ]);
    expect(timelineItems[1].outputs).toEqual([
      expect.objectContaining({ content: 'First answer' }),
    ]);
    expect(timelineItems[2].event.payload.message.id).toBe('user-two');
    expect(timelineItems[3]).toEqual(
      expect.objectContaining({ runId: 'run-two', type: 'assistant_run' }),
    );
    expect(timelineItems[3].outputs).toEqual([
      expect.objectContaining({ content: 'Second answer', streaming: true }),
    ]);
  });

  it('orders each live run user event before its assistant block using run arrival', () => {
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

    expect(timelineItems.map((item) => item.type)).toEqual([
      'event',
      'assistant_run',
      'event',
      'assistant_run',
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

  it('appends later runs after older runs even when run-local sequences restart', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-old',
      sequence: 1,
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-old',
      sequence: 2,
      payload: {
        message: { id: 'user-old', role: 'user', content: 'Old request' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-old',
      sequence: 3,
      payload: {
        tool_call: { id: 'old-tool', index: 0, name: 'old_tool' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-new',
      sequence: 1,
      payload: { status: CHAT_STATUS_RUNNING },
    });
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-new',
      sequence: 2,
      payload: {
        message: { id: 'user-new', role: 'user', content: 'New request' },
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

    const timelineItems = visibleTimelineItemsForRender(sessionState);

    expect(timelineItems.map((item) => item.id)).toEqual([
      'event-run-old-2',
      'assistant-run-run-old',
      'event-run-new-2',
      'assistant-run-run-new',
    ]);
    expect(timelineItems[1].tools).toEqual([
      expect.objectContaining({ toolCallId: 'old-tool' }),
    ]);
    expect(timelineItems[3].tools).toEqual([
      expect.objectContaining({ toolCallId: 'new-tool' }),
    ]);
  });
});
