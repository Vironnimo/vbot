import { describe, expect, it } from 'vitest';

import {
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_FAILED,
  CHAT_STATUS_RUNNING,
  appendRunEvent,
  canCreateNewSession,
  createChatState,
  currentSessionState,
  dequeueMessage,
  enqueueMessage,
  ensureSessionState,
  highestRunEventSequence,
  loadHistory,
  removeQueuedMessage,
  restoreDequeuedMessage,
  selectedAgent,
  setAgents,
  startRun,
  visibleTimelineItems,
} from '../chatState.js';

describe('chat state helpers', () => {
  it('tracks selected agent and per-agent current session state', () => {
    const state = createChatState();

    const selectedAgentId = setAgents(state, [
      { id: 'alpha', current_session_id: 'session-one' },
      { id: 'beta', current_session_id: 'session-two' },
    ]);
    const sessionState = ensureSessionState(state, 'alpha', 'session-one');

    expect(selectedAgentId).toBe('alpha');
    expect(selectedAgent(state)).toEqual({
      id: 'alpha',
      current_session_id: 'session-one',
    });
    expect(sessionState.key).toBe('alpha::session-one');
  });

  it('does not create session state when reading the current session', () => {
    const state = createChatState();

    setAgents(state, [{ id: 'alpha', current_session_id: 'session-one' }]);

    expect(currentSessionState(state)).toBeNull();
    expect(state.sessions).toEqual({});

    const createdSessionState = ensureSessionState(
      state,
      'alpha',
      'session-one',
    );

    expect(currentSessionState(state)).toBe(createdSessionState);
  });

  it('loads history without losing the visible queue', () => {
    const state = createChatState();
    const sessionState = ensureSessionState(state, 'alpha', 'session-one');
    enqueueMessage(sessionState, 'queued work');

    loadHistory(sessionState, [
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);

    expect(sessionState.messages).toEqual([
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);
    expect(sessionState.queue).toHaveLength(1);
  });

  it('preserves active run events when history refreshes during a run', () => {
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

    loadHistory(sessionState, [
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);

    expect(sessionState.messages).toEqual([
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);
    expect(sessionState.runEvents).toEqual([
      {
        type: 'reasoning',
        run_id: 'run-one',
        sequence: 1,
        payload: { message: { role: 'assistant', reasoning: 'Working' } },
        agent_id: undefined,
        session_id: undefined,
        timestamp: undefined,
      },
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
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'The file says A.',
      },
    ]);

    expect(visibleTimelineItems(sessionState)).toEqual([
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

  it('groups reported persisted multi-step tool history into one assistant run', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reported-history',
    );

    loadHistory(sessionState, reportedMultiStepMessages());

    const timelineItems = visibleTimelineItems(sessionState);
    const assistantRun = timelineItems[1];

    expect(timelineItems).toHaveLength(2);
    expect(timelineItems[0]).toEqual(
      expect.objectContaining({ id: 'user-reported', type: 'message' }),
    );
    expect(assistantRun).toEqual(
      expect.objectContaining({ type: 'assistant_run', source: 'history' }),
    );
    expect(assistantRun.reasoning.map((item) => item.content)).toEqual([
      'Find candidate files.',
      'Read the selected file.',
      'Summarize the result.',
    ]);
    expect(assistantRun.outputs.map((item) => item.content)).toEqual([
      'I found the timeline helper; now I will read it.',
      'The timeline is in chatState.js.',
    ]);
    expect(assistantRun.tools.map((tool) => tool.toolCallId)).toEqual([
      'call-glob',
      'call-read',
    ]);
    expect(assistantRun.tools.map((tool) => tool.name)).toEqual([
      'glob',
      'read',
    ]);
  });

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

    const timelineItems = visibleTimelineItems(sessionState);

    expect(timelineItems).toHaveLength(2);
    expect(timelineItems[0]).toEqual(
      expect.objectContaining({ id: 'user-one', type: 'message' }),
    );
    expect(timelineItems[1]).toEqual(
      expect.objectContaining({
        id: 'assistant-run-run-one',
        type: 'assistant_run',
        reasoning: [expect.objectContaining({ content: 'Checking' })],
        outputs: [expect.objectContaining({ content: 'The file says A.' })],
      }),
    );
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

    expect(visibleTimelineItems(sessionState)).toEqual([
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

    const timelineItems = visibleTimelineItems(sessionState);

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
    expect(visibleTimelineItems(sessionState)).toEqual([
      expect.objectContaining({ id: 'user-one', type: 'message' }),
      expect.objectContaining({
        id: 'history-run-assistant-one',
        type: 'assistant_run',
        status: CHAT_STATUS_COMPLETED,
        outputs: [expect.objectContaining({ content: 'The file says A.' })],
      }),
    ]);
  });

  it('preserves active streaming items when history refreshes during a run', () => {
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

    expect(sessionState.streamingItems).toEqual([
      expect.objectContaining({ type: 'assistant', content: 'Hel' }),
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
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 1,
      payload: { message: { role: 'assistant', content: 'Done' } },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 2,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    loadHistory(sessionState, [
      { id: 'message-one', role: 'assistant', content: 'Done' },
    ]);

    expect(sessionState.runEvents).toEqual([]);
  });

  it('keeps queued messages visible, FIFO, and removable before send', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    const firstMessage = enqueueMessage(sessionState, 'first');
    const secondMessage = enqueueMessage(sessionState, 'second');
    const removed = removeQueuedMessage(sessionState, firstMessage.id);
    const nextMessage = dequeueMessage(sessionState);

    expect(removed).toBe(true);
    expect(nextMessage).toEqual(secondMessage);
    expect(sessionState.queue).toEqual([]);
  });

  it('restores a dequeued message to the front when queued send fails', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    const firstMessage = enqueueMessage(sessionState, 'first');
    const secondMessage = enqueueMessage(sessionState, 'second');

    const nextMessage = dequeueMessage(sessionState);
    restoreDequeuedMessage(sessionState, nextMessage);

    expect(sessionState.queue).toEqual([firstMessage, secondMessage]);
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

    expect(visibleTimelineItems(sessionState)).toEqual([
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

    const [assistantRun] = visibleTimelineItems(sessionState);

    expect(sessionState.runEvents).toHaveLength(4);
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

    const [assistantRun] = visibleTimelineItems(sessionState);

    expect(assistantRun.items.map((item) => item.type)).toEqual([
      'reasoning',
      'tool_call',
      'assistant_output',
    ]);
    expect(assistantRun.items.map((item) => item.sequence)).toEqual([1, 2, 4]);
    expect(assistantRun.reasoning).toEqual([
      expect.objectContaining({ content: 'Plan more', sequence: 1 }),
    ]);
    expect(assistantRun.outputs).toEqual([
      expect.objectContaining({ content: 'Done now', sequence: 4 }),
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

    const [assistantRun] = visibleTimelineItems(sessionState);

    expect(assistantRun.items.map((item) => item.type)).toEqual([
      'assistant_output',
      'tool_call',
      'assistant_output',
    ]);
    expect(assistantRun.outputs).toEqual([
      expect.objectContaining({ content: 'First answer', sequence: 1 }),
      expect.objectContaining({ content: 'Second answer', sequence: 4 }),
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

    const [assistantRun] = visibleTimelineItems(sessionState);

    expect(assistantRun.items.map((item) => item.type)).toEqual([
      'reasoning',
      'tool_call',
      'reasoning',
    ]);
    expect(assistantRun.reasoning).toEqual([
      expect.objectContaining({ content: 'Plan first', sequence: 1 }),
      expect.objectContaining({ content: 'Plan second', sequence: 4 }),
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

    const [assistantRun] = visibleTimelineItems(sessionState);

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

    const assistantRuns = visibleTimelineItems(sessionState);

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

    const timelineItems = visibleTimelineItems(sessionState);

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

    const timelineItems = visibleTimelineItems(sessionState);

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

    const timelineItems = visibleTimelineItems(sessionState);

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

  it('merges trailing text deltas while preserving interleaved order', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 1,
      payload: { content_delta: 'Hel' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 2,
      payload: { content_delta: 'lo' },
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-one',
      sequence: 3,
      payload: { reasoning_delta: 'Then' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 4,
      payload: { content_delta: ' again' },
    });

    expect(sessionState.streamingItems.map((item) => item.content)).toEqual([
      'Hello',
      'Then',
      ' again',
    ]);
    expect(sessionState.streamingItems.map((item) => item.type)).toEqual([
      'assistant',
      'reasoning',
      'assistant',
    ]);
  });

  it('accumulates partial tool call deltas without parsed final arguments', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-one',
      sequence: 1,
      payload: {
        tool_call_id: 'call-one',
        name_delta: 'read_',
        arguments_delta: '{"path"',
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        tool_call_id: 'call-one',
        name_delta: 'file',
        arguments_delta: ': "a.txt"}',
      },
    });

    expect(sessionState.streamingItems).toEqual([
      expect.objectContaining({
        type: 'tool_call',
        toolCallId: 'call-one',
        name: 'read_file',
        argumentsText: '{"path": "a.txt"}',
        complete: false,
      }),
    ]);
  });

  it('keeps a streaming tool call at its first chronological position', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-one',
      sequence: 1,
      payload: {
        tool_call_id: 'call-one',
        name_delta: 'read_',
        arguments_delta: '{"path"',
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 2,
      payload: { content_delta: 'Checking the file.' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-one',
      sequence: 3,
      payload: {
        tool_call_id: 'call-one',
        name_delta: 'file',
        arguments_delta: ': "a.txt"}',
      },
    });

    expect(sessionState.streamingItems).toEqual([
      expect.objectContaining({
        type: 'tool_call',
        toolCallId: 'call-one',
        name: 'read_file',
        argumentsText: '{"path": "a.txt"}',
        sequence: 1,
      }),
      expect.objectContaining({
        type: 'assistant',
        content: 'Checking the file.',
        sequence: 2,
      }),
    ]);
    expect(
      visibleTimelineItems(sessionState)[0].items.map((item) => item.type),
    ).toEqual(['tool_call', 'assistant_output']);
  });

  it('ignores duplicate streaming event sequences', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    const event = {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 1,
      payload: { content_delta: 'Hi' },
    };

    appendRunEvent(sessionState, event);
    appendRunEvent(sessionState, event);

    expect(sessionState.streamingItems).toHaveLength(1);
    expect(sessionState.streamingItems[0].content).toBe('Hi');
  });

  it('clears streaming items when final assistant output arrives', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 1,
      payload: { content_delta: 'Draft' },
    });

    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 2,
      payload: { message: { role: 'assistant', content: 'Final' } },
    });

    expect(sessionState.streamingItems).toEqual([]);
    expect(visibleTimelineItems(sessionState)).toEqual([
      expect.objectContaining({
        type: 'assistant_run',
        outputs: [
          expect.objectContaining({
            content: 'Final',
            streaming: false,
          }),
        ],
      }),
    ]);
  });

  it('replaces assistant streaming draft output with final output in the same run block', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 1,
      payload: { content_delta: 'Draft' },
    });

    let [assistantRun] = visibleTimelineItems(sessionState);

    expect(assistantRun.outputs).toEqual([
      expect.objectContaining({ content: 'Draft', streaming: true }),
    ]);

    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 2,
      payload: { message: { role: 'assistant', content: 'Final' } },
    });

    [assistantRun] = visibleTimelineItems(sessionState);

    expect(assistantRun).toEqual(
      expect.objectContaining({
        id: 'assistant-run-run-one',
        type: 'assistant_run',
      }),
    );
    expect(assistantRun.outputs).toEqual([
      expect.objectContaining({ content: 'Final', streaming: false }),
    ]);
    expect(assistantRun.items.map((item) => item.content)).not.toContain(
      'Draft',
    );
  });

  it('replaces streamed assistant content before a completed tool row with the final authoritative message', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-draft-tool-final',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-draft-tool-final',
      sequence: 1,
      payload: { content_delta: 'I will inspect the UI state helpers.' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-draft-tool-final',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-glob',
          index: 0,
          name: 'glob',
          arguments: { pattern: 'webui/src/**/*.js' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-draft-tool-final',
      sequence: 3,
      payload: {
        tool_call: { id: 'call-glob', index: 0, name: 'glob' },
        result: {
          ok: true,
          data: { content: 'webui/src/lib/chatState.js' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-draft-tool-final',
      sequence: 4,
      payload: {
        message: {
          id: 'assistant-glob',
          role: 'assistant',
          content: 'I will inspect the UI state helpers.',
          tool_calls: [
            {
              id: 'call-glob',
              name: 'glob',
              arguments: { pattern: 'webui/src/**/*.js' },
            },
          ],
        },
      },
    });

    const [assistantRun] = visibleTimelineItems(sessionState);

    expect(assistantRun.items.map((item) => item.type)).toEqual([
      'assistant_output',
      'tool_call',
    ]);
    expect(assistantRun.outputs).toEqual([
      expect.objectContaining({
        content: 'I will inspect the UI state helpers.',
        streaming: false,
      }),
    ]);
    expect(assistantRun.tools).toEqual([
      expect.objectContaining({ toolCallId: 'call-glob', status: 'success' }),
    ]);
  });

  it('keeps reported live multi-step tool run content and thinking visible once', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reported-live',
    );

    appendReportedLiveRunEvents(sessionState, 'run-reported-live');

    const [assistantRun] = visibleTimelineItems(sessionState);

    expect(assistantRun).toEqual(
      expect.objectContaining({
        id: 'assistant-run-run-reported-live',
        type: 'assistant_run',
      }),
    );
    expect(assistantRun.outputs.map((item) => item.content)).toEqual([
      'I found the timeline helper; now I will read it.',
      'The timeline is in chatState.js.',
    ]);
    expect(assistantRun.reasoning.map((item) => item.content)).toEqual([
      'Find candidate files.',
      'Read the selected file.',
      'Summarize the result.',
    ]);
    expect(assistantRun.tools.map((tool) => tool.toolCallId)).toEqual([
      'call-glob',
      'call-read',
    ]);
  });

  it('keeps reported active-overlap content and thinking visible once', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reported-overlap',
    );
    startRun(sessionState, {
      run_id: 'run-reported-overlap',
      sse_url: '/api/runs/run-reported-overlap/events',
      status: CHAT_STATUS_RUNNING,
    });

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-reported-overlap',
      sequence: 1,
      payload: { message: reportedMultiStepMessages()[0] },
    });
    appendReportedLiveRunEvents(sessionState, 'run-reported-overlap', 2);
    loadHistory(sessionState, reportedMultiStepMessages());

    const timelineItems = visibleTimelineItems(sessionState);
    const assistantRun = timelineItems[1];

    expect(timelineItems).toHaveLength(2);
    expect(assistantRun.outputs.map((item) => item.content)).toEqual([
      'I found the timeline helper; now I will read it.',
      'The timeline is in chatState.js.',
    ]);
    expect(assistantRun.reasoning.map((item) => item.content)).toEqual([
      'Find candidate files.',
      'Read the selected file.',
      'Summarize the result.',
    ]);
    expect(assistantRun.tools.map((tool) => tool.toolCallId)).toEqual([
      'call-glob',
      'call-read',
    ]);
  });

  it('groups persisted assistant, tool, and final assistant messages best-effort', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-tools',
        role: 'assistant',
        reasoning: 'Need to read it.',
        tool_calls: [
          {
            id: 'call-one',
            name: 'read_file',
            arguments: { path: 'a.txt' },
          },
        ],
      },
      {
        id: 'tool-one',
        role: 'tool',
        tool_call_id: 'call-one',
        name: 'read_file',
        content: '{"ok": true, "content": "A"}',
      },
      {
        id: 'assistant-final',
        role: 'assistant',
        content: 'The file says A.',
      },
      { id: 'user-two', role: 'user', content: 'Thanks' },
    ]);

    const timelineItems = visibleTimelineItems(sessionState);

    expect(timelineItems).toEqual([
      expect.objectContaining({ id: 'user-one', type: 'message' }),
      expect.objectContaining({ type: 'assistant_run', source: 'history' }),
      expect.objectContaining({ id: 'user-two', type: 'message' }),
    ]);
    expect(timelineItems[1].items.map((item) => item.type)).toEqual([
      'reasoning',
      'tool_call',
      'assistant_output',
    ]);
    expect(timelineItems[1].tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-one',
        name: 'read_file',
        arguments: { path: 'a.txt' },
        result: '{"ok": true, "content": "A"}',
        status: 'success',
      }),
    ]);
    expect(timelineItems[1].outputs).toEqual([
      expect.objectContaining({ content: 'The file says A.' }),
    ]);
  });

  it('clears streaming items on terminal cleanup', () => {
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
      payload: { content_delta: 'Draft' },
    });

    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 2,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    expect(sessionState.streamingItems).toEqual([]);
  });

  it('tracks the highest seen run sequence for reconnect handoff', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 3,
      payload: { content_delta: 'Hi' },
    });

    expect(highestRunEventSequence(sessionState)).toBe(3);
  });
});

function reportedMultiStepMessages() {
  return [
    {
      id: 'user-reported',
      role: 'user',
      content: 'Investigate the duplicated chat UI.',
      timestamp: '2026-05-08T10:00:00Z',
    },
    {
      id: 'assistant-glob',
      role: 'assistant',
      reasoning: 'Find candidate files.',
      timestamp: '2026-05-08T10:00:01Z',
      tool_calls: [
        {
          id: 'call-glob',
          name: 'glob',
          arguments: { pattern: 'webui/src/**/*.js' },
        },
      ],
    },
    {
      id: 'tool-glob',
      role: 'tool',
      tool_call_id: 'call-glob',
      name: 'glob',
      content: '{"ok":true,"data":{"content":"webui/src/lib/chatState.js"}}',
      timestamp: '2026-05-08T10:00:02Z',
    },
    {
      id: 'assistant-read',
      role: 'assistant',
      content: 'I found the timeline helper; now I will read it.',
      reasoning: 'Read the selected file.',
      timestamp: '2026-05-08T10:00:03Z',
      tool_calls: [
        {
          id: 'call-read',
          name: 'read',
          arguments: { path: 'webui/src/lib/chatState.js' },
        },
      ],
    },
    {
      id: 'tool-read',
      role: 'tool',
      tool_call_id: 'call-read',
      name: 'read',
      content: '{"ok":true,"data":{"content":"timeline code"}}',
      timestamp: '2026-05-08T10:00:04Z',
    },
    {
      id: 'assistant-final',
      role: 'assistant',
      content: 'The timeline is in chatState.js.',
      reasoning: 'Summarize the result.',
      timestamp: '2026-05-08T10:00:05Z',
    },
  ];
}

function appendReportedLiveRunEvents(sessionState, runId, startSequence = 1) {
  const sequence = (offset) => startSequence + offset;

  appendRunEvent(sessionState, {
    type: 'reasoning_delta',
    run_id: runId,
    sequence: sequence(0),
    payload: { reasoning_delta: 'Find candidate files.' },
  });
  appendRunEvent(sessionState, {
    type: 'tool_call_started',
    run_id: runId,
    sequence: sequence(1),
    payload: {
      tool_call: {
        id: 'call-glob',
        index: 0,
        name: 'glob',
        arguments: { pattern: 'webui/src/**/*.js' },
      },
    },
  });
  appendRunEvent(sessionState, {
    type: 'tool_call_result',
    run_id: runId,
    sequence: sequence(2),
    payload: {
      tool_call: { id: 'call-glob', index: 0, name: 'glob' },
      result: {
        ok: true,
        data: { content: 'webui/src/lib/chatState.js' },
      },
    },
  });
  appendRunEvent(sessionState, {
    type: 'reasoning_delta',
    run_id: runId,
    sequence: sequence(3),
    payload: { reasoning_delta: 'Read the selected file.' },
  });
  appendRunEvent(sessionState, {
    type: 'assistant_output_delta',
    run_id: runId,
    sequence: sequence(4),
    payload: {
      content_delta: 'I found the timeline helper; now I will read it.',
    },
  });
  appendRunEvent(sessionState, {
    type: 'tool_call_started',
    run_id: runId,
    sequence: sequence(5),
    payload: {
      tool_call: {
        id: 'call-read',
        index: 0,
        name: 'read',
        arguments: { path: 'webui/src/lib/chatState.js' },
      },
    },
  });
  appendRunEvent(sessionState, {
    type: 'tool_call_result',
    run_id: runId,
    sequence: sequence(6),
    payload: {
      tool_call: { id: 'call-read', index: 0, name: 'read' },
      result: { ok: true, data: { content: 'timeline code' } },
    },
  });
  appendRunEvent(sessionState, {
    type: 'assistant_output',
    run_id: runId,
    sequence: sequence(7),
    payload: {
      message: {
        id: 'assistant-read',
        role: 'assistant',
        content: 'I found the timeline helper; now I will read it.',
        reasoning: 'Read the selected file.',
        tool_calls: [
          {
            id: 'call-read',
            name: 'read',
            arguments: { path: 'webui/src/lib/chatState.js' },
          },
        ],
      },
    },
  });
  appendRunEvent(sessionState, {
    type: 'reasoning_delta',
    run_id: runId,
    sequence: sequence(8),
    payload: { reasoning_delta: 'Summarize the result.' },
  });
  appendRunEvent(sessionState, {
    type: 'assistant_output_delta',
    run_id: runId,
    sequence: sequence(9),
    payload: { content_delta: 'The timeline is in chatState.js.' },
  });
  appendRunEvent(sessionState, {
    type: 'assistant_output',
    run_id: runId,
    sequence: sequence(10),
    payload: {
      message: {
        id: 'assistant-final',
        role: 'assistant',
        content: 'The timeline is in chatState.js.',
        reasoning: 'Summarize the result.',
      },
    },
  });
}
