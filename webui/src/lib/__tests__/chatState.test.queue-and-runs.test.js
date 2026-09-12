import { describe, expect, it } from 'vitest';
import {
  CHAT_STATUS_RUNNING,
  assistantRunChildProgressKey,
  addServerQueuedMessage,
  appendRunEvent,
  createChatState,
  ensureSessionState,
  isSessionEmpty,
  loadHistory,
  removeQueuedMessage,
  syncQueueFromServer,
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
      timestamp: '2026-08-05T18:04:00.000Z',
      payload: {},
    });

    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.streamStatus).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.currentRun).toEqual({
      runId: 'run-from-ws',
      sseUrl: '',
      controls: {},
      controlsSequence: 0,
      status: CHAT_STATUS_RUNNING,
      startedAt: '2026-08-05T18:04:00.000Z',
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
});
