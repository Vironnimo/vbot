import { describe, expect, it } from 'vitest';

import {
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_CANCELLED,
  CHAT_STATUS_FAILED,
  CHAT_STATUS_IDLE,
  CHAT_STATUS_RUNNING,
  assistantRunChildProgressKey,
  addServerQueuedMessage,
  appendRunEvent,
  canCreateNewSession,
  createChatState,
  currentSessionState,
  ensureSessionState,
  highestContiguousRunEventSequence,
  highestRunEventSequence,
  isRunActive,
  loadHistory,
  removeQueuedMessage,
  resetStaleRun,
  selectedAgent,
  setAgents,
  syncQueueFromServer,
  startRun,
  updateQueuedMessageContent,
  prependHistory,
  updateSessionUsage,
  visibleTimelineItems,
  visibleTimelineItemsForRender,
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
    addServerQueuedMessage(sessionState, {
      id: 'queue-one',
      content: 'queued work',
      created_at: '2026-05-22T00:00:00+00:00',
    });

    loadHistory(sessionState, [
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);

    expect(sessionState.messages).toEqual([
      { id: 'message-one', role: 'user', content: 'Hi' },
    ]);
    expect(sessionState.queue).toHaveLength(1);
  });

  it('merges persisted tool timing and run summary into history assistant runs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-timing-history',
    );
    const timing = {
      started_at: '2026-05-03T14:30:01+00:00',
      completed_at: '2026-05-03T14:30:02.250+00:00',
      duration_ms: 1250,
    };

    loadHistory(sessionState, [
      {
        id: 'user-one',
        role: 'user',
        content: 'Run tool',
        timestamp: '2026-05-03T14:30:00+00:00',
      },
      {
        id: 'assistant-tool',
        role: 'assistant',
        content: null,
        timestamp: '2026-05-03T14:30:00+00:00',
        tool_calls: [{ id: 'call-one', name: 'read', arguments: {} }],
      },
      {
        id: 'tool-one',
        role: 'tool',
        tool_call_id: 'call-one',
        name: 'read',
        content: '{"ok":true,"error":null,"data":{},"artifacts":[]}',
        timestamp: '2026-05-03T14:30:02+00:00',
        timing,
      },
      {
        id: 'assistant-final',
        role: 'assistant',
        content: 'Done',
        timestamp: '2026-05-03T14:30:03+00:00',
      },
      {
        id: 'summary-one',
        role: 'run_summary',
        run_id: 'run-one',
        status: 'completed',
        timestamp: '2026-05-03T14:30:03+00:00',
        timing,
      },
    ]);

    const assistantRun = visibleTimelineItems(sessionState).find(
      (item) => item.type === 'assistant_run',
    );

    expect(assistantRun).toEqual(
      expect.objectContaining({
        runId: 'run-one',
        status: CHAT_STATUS_COMPLETED,
        durationMs: 1250,
      }),
    );
    expect(assistantRun.tools[0]).toEqual(
      expect.objectContaining({
        toolCallId: 'call-one',
        durationMs: 1250,
      }),
    );
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

    const assistantRun = visibleTimelineItems(sessionState).find(
      (item) => item.type === 'assistant_run',
    );

    expect(assistantRun.durationMs).toBe(1250);
    expect(assistantRun.tools[0].durationMs).toBe(1250);
  });

  it('prepends older history without duplicating loaded messages', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(
      sessionState,
      [
        { id: 'message-three', role: 'user', content: 'Three' },
        { id: 'message-four', role: 'assistant', content: 'Four' },
      ],
      { hasMore: true },
    );
    prependHistory(
      sessionState,
      [
        { id: 'message-one', role: 'user', content: 'One' },
        { id: 'message-two', role: 'assistant', content: 'Two' },
        { id: 'message-three', role: 'user', content: 'Three duplicate' },
        { id: 'note-one', role: 'note', content: 'Internal note' },
      ],
      { hasMore: false },
    );

    expect(sessionState.messages.map((message) => message.id)).toEqual([
      'message-one',
      'message-two',
      'message-three',
      'message-four',
    ]);
    expect(sessionState.hasOlderHistory).toBe(false);
  });

  it('filters internal notes from loaded history and visible timeline', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(sessionState, [
      { id: 'message-one', role: 'user', content: 'Hi' },
      { id: 'note-one', role: 'note', content: 'Internal reminder' },
      { id: 'unknown-one', role: 'debug', content: 'Internal debug data' },
      { id: 'message-two', role: 'assistant', content: 'Hello' },
    ]);

    expect(sessionState.messages.map((message) => message.role)).toEqual([
      'user',
      'assistant',
    ]);
    expect(visibleTimelineItems(sessionState)).toEqual([
      expect.objectContaining({
        id: 'message-one',
        type: 'message',
        message: expect.objectContaining({ content: 'Hi' }),
      }),
      expect.objectContaining({
        type: 'assistant_run',
        outputs: [expect.objectContaining({ content: 'Hello' })],
      }),
    ]);
    expect(JSON.stringify(visibleTimelineItems(sessionState))).not.toContain(
      'Internal reminder',
    );
  });

  it('splits consecutive assistant history messages into separate run blocks', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-automatic-follow-up-history',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Start background work' },
      {
        id: 'assistant-tool-call',
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'call-subagent',
            name: 'subagent',
            arguments: { agent_id: 'tester', blocking: false },
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
        content: 'Background sub-agent started.',
      },
      {
        id: 'assistant-follow-up',
        role: 'assistant',
        content: 'Background sub-agent finished.',
      },
    ]);

    const timelineItems = visibleTimelineItems(sessionState);

    expect(timelineItems.map((item) => item.type)).toEqual([
      'message',
      'assistant_run',
      'assistant_run',
    ]);
    expect(timelineItems[1].tools).toEqual([
      expect.objectContaining({ toolCallId: 'call-subagent' }),
    ]);
    expect(timelineItems[1].outputs).toEqual([
      expect.objectContaining({ content: 'Background sub-agent started.' }),
    ]);
    expect(timelineItems[2].outputs).toEqual([
      expect.objectContaining({ content: 'Background sub-agent finished.' }),
    ]);
  });

  it('keeps error history messages visible and outside assistant runs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Try the request' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'I will call the provider.',
      },
      {
        id: 'error-one',
        role: 'error',
        error_kind: 'rate_limit',
        content: 'Provider rate limit exceeded',
      },
      { id: 'user-two', role: 'user', content: 'Try again later' },
    ]);

    const timelineItems = visibleTimelineItems(sessionState);

    expect(sessionState.messages.map((message) => message.role)).toEqual([
      'user',
      'assistant',
      'error',
      'user',
    ]);
    expect(timelineItems).toEqual([
      expect.objectContaining({ id: 'user-one', type: 'message' }),
      expect.objectContaining({ type: 'assistant_run', source: 'history' }),
      expect.objectContaining({
        id: 'error-one',
        type: 'message',
        message: expect.objectContaining({
          role: 'error',
          error_kind: 'rate_limit',
          content: 'Provider rate limit exceeded',
        }),
      }),
      expect.objectContaining({ id: 'user-two', type: 'message' }),
    ]);
    expect(timelineItems[1].outputs).toEqual([
      expect.objectContaining({ content: 'I will call the provider.' }),
    ]);
  });

  it('keeps live error persisted events visible and outside assistant runs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-one',
      sequence: 1,
      payload: {
        message: { id: 'user-one', role: 'user', content: 'Try request' },
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
          content: 'Calling provider.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'error_message_persisted',
      run_id: 'run-one',
      sequence: 3,
      payload: {
        message: {
          id: 'error-one',
          role: 'error',
          error_kind: 'rate_limit',
          content: 'Provider rate limit exceeded',
        },
      },
    });

    const timelineItems = visibleTimelineItems(sessionState);

    expect(timelineItems).toEqual([
      expect.objectContaining({ id: 'event-run-one-1', type: 'event' }),
      expect.objectContaining({ type: 'assistant_run', runId: 'run-one' }),
      expect.objectContaining({
        id: 'error-one',
        type: 'message',
        message: expect.objectContaining({
          role: 'error',
          content: 'Provider rate limit exceeded',
        }),
      }),
    ]);
    expect(timelineItems[1].outputs).toEqual([
      expect.objectContaining({ content: 'Calling provider.' }),
    ]);
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

  it('keeps reload history ordering with assistant content before same-message tool rows', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reload-history-ordering',
    );

    loadHistory(sessionState, [
      {
        id: 'user-one',
        role: 'user',
        content: 'Investigate chat ordering.',
      },
      {
        id: 'assistant-plan',
        role: 'assistant',
        content: 'I will run bash first.',
        tool_calls: [
          {
            id: 'call-bash',
            name: 'bash',
            arguments: { command: 'ls -la' },
          },
        ],
      },
      {
        id: 'tool-bash',
        role: 'tool',
        tool_call_id: 'call-bash',
        name: 'bash',
        content:
          '{"ok":true,"data":{"status":"completed","exit_code":0,"output":"file.txt","truncated":false},"error":null,"artifacts":[]}',
      },
      {
        id: 'assistant-final',
        role: 'assistant',
        content: 'I found the file list.',
      },
    ]);

    const timelineItems = visibleTimelineItems(sessionState);
    const assistantRun = timelineItems[1];

    expect(timelineItems).toHaveLength(2);
    expect(assistantRun).toEqual(
      expect.objectContaining({ type: 'assistant_run', source: 'history' }),
    );
    expect(assistantRun.items.map((item) => item.type)).toEqual([
      'assistant_output',
      'tool_call',
      'assistant_output',
    ]);
    expect(assistantRun.outputs.map((item) => item.content)).toEqual([
      'I will run bash first.',
      'I found the file list.',
    ]);
    expect(assistantRun.tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-bash',
        name: 'bash',
        status: 'success',
      }),
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
        outputs: [expect.objectContaining({ content: 'The file says A.' })],
      }),
    );
    expect(timelineItems[1].reasoning).toEqual([]);
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
            arguments: { agent_id: 'tester', blocking: false },
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
      visibleTimelineItems(sessionState),
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
      visibleTimelineItems(sessionState),
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
            arguments: { agent_id: 'tester', blocking: false },
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

    const timelineItems = visibleTimelineItems(sessionState);

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
        created_at: '2026-05-22T01:00:00+00:00',
      },
      {
        id: 'queue-2',
        content: 'Second message',
        created_at: '2026-05-22T01:01:00+00:00',
      },
    ]);

    expect(sessionState.queue).toEqual([
      {
        id: 'queue-1',
        content: 'First message',
        created_at: '2026-05-22T01:00:00+00:00',
      },
      {
        id: 'queue-2',
        content: 'Second message',
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
      created_at: '2026-05-22T01:00:00+00:00',
    });

    const updated = updateQueuedMessageContent(
      sessionState,
      'queue-1',
      'Updated content',
    );

    expect(updated).toBe(true);
    expect(sessionState.queue[0].content).toBe('Updated content');
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

    expect(sessionState.runEvents).toHaveLength(3);
    expect(assistantRun).toEqual(
      expect.objectContaining({
        id: 'assistant-run-run-one',
        type: 'assistant_run',
        runId: 'run-one',
      }),
    );
    expect(assistantRun.items.map((item) => item.type)).toEqual([
      'tool_call',
      'assistant_output',
    ]);
    expect(assistantRun.reasoning).toEqual([]);
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

    const [assistantRun] = visibleTimelineItems(sessionState);
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

    const timelineItems = visibleTimelineItems(sessionState);
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

    const timelineItems = visibleTimelineItems(sessionState);

    expect(timelineItems.map((item) => item.type)).toEqual([
      'assistant_run',
      'streaming',
      'streaming',
      'streaming',
    ]);
    expect(timelineItems[0].items.map((item) => item.type)).toEqual([
      'tool_call',
    ]);
    expect(timelineItems[1]).toEqual(
      expect.objectContaining({
        type: 'streaming',
        streamingItem: expect.objectContaining({
          type: 'reasoning',
          content: 'Plan',
          sequence: 1,
        }),
      }),
    );
    expect(timelineItems[2]).toEqual(
      expect.objectContaining({
        type: 'streaming',
        streamingItem: expect.objectContaining({
          type: 'reasoning',
          content: ' more',
          sequence: 3,
        }),
      }),
    );
    expect(timelineItems[3]).toEqual(
      expect.objectContaining({
        type: 'streaming',
        streamingItem: expect.objectContaining({
          type: 'assistant',
          content: 'Done now',
          sequence: 5,
        }),
      }),
    );
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

    const timelineItems = visibleTimelineItems(sessionState);

    expect(timelineItems.map((item) => item.type)).toEqual([
      'assistant_run',
      'streaming',
      'streaming',
    ]);
    expect(timelineItems[0].items.map((item) => item.type)).toEqual([
      'tool_call',
    ]);
    expect(timelineItems[1]).toEqual(
      expect.objectContaining({
        type: 'streaming',
        streamingItem: expect.objectContaining({
          type: 'assistant',
          content: 'First answer',
          sequence: 1,
        }),
      }),
    );
    expect(timelineItems[2]).toEqual(
      expect.objectContaining({
        type: 'streaming',
        streamingItem: expect.objectContaining({
          type: 'assistant',
          content: 'Second answer',
          sequence: 4,
        }),
      }),
    );
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

    const timelineItems = visibleTimelineItems(sessionState);

    expect(timelineItems.map((item) => item.type)).toEqual([
      'assistant_run',
      'streaming',
      'streaming',
    ]);
    expect(timelineItems[0].items.map((item) => item.type)).toEqual([
      'tool_call',
    ]);
    expect(timelineItems[1]).toEqual(
      expect.objectContaining({
        type: 'streaming',
        streamingItem: expect.objectContaining({
          type: 'reasoning',
          content: 'Plan first',
          sequence: 1,
        }),
      }),
    );
    expect(timelineItems[2]).toEqual(
      expect.objectContaining({
        type: 'streaming',
        streamingItem: expect.objectContaining({
          type: 'reasoning',
          content: 'Plan second',
          sequence: 4,
        }),
      }),
    );
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

    const [assistantRun] = visibleTimelineItems(sessionState);

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
      'streaming',
    ]);
    expect(timelineItems[1].outputs).toEqual([
      expect.objectContaining({ content: 'First answer' }),
    ]);
    expect(timelineItems[2].event.payload.message.id).toBe('user-two');
    expect(timelineItems[3]).toEqual(
      expect.objectContaining({ runId: 'run-two', type: 'assistant_run' }),
    );
    expect(timelineItems[4]).toEqual(
      expect.objectContaining({
        type: 'streaming',
        streamingItem: expect.objectContaining({
          type: 'assistant',
          content: 'Second answer',
          sequence: 2,
        }),
      }),
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
      'streaming',
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
    expect(timelineItems[4]).toEqual(
      expect.objectContaining({
        type: 'streaming',
        streamingItem: expect.objectContaining({
          type: 'reasoning',
          content: 'Planning',
          sequence: 3,
        }),
      }),
    );
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
    expect(visibleTimelineItems(sessionState).map((item) => item.type)).toEqual(
      ['streaming', 'streaming'],
    );
  });

  it('keeps render selector assistant/reasoning streaming content inside assistant runs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-render-selector-text',
    );

    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-render-selector-text',
      sequence: 1,
      payload: { reasoning_delta: 'Plan first.' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-render-selector-text',
      sequence: 2,
      payload: { content_delta: 'Draft response.' },
    });

    const renderItems = visibleTimelineItemsForRender(sessionState);

    expect(renderItems).toEqual([
      expect.objectContaining({
        type: 'assistant_run',
        runId: 'run-render-selector-text',
        reasoning: [
          expect.objectContaining({
            content: 'Plan first.',
            streaming: true,
          }),
        ],
        outputs: [
          expect.objectContaining({
            content: 'Draft response.',
            streaming: true,
          }),
        ],
      }),
    ]);
    expect(
      renderItems.some(
        (item) =>
          item.type === 'streaming' &&
          ['assistant', 'reasoning'].includes(item.streamingItem?.type),
      ),
    ).toBe(false);
  });

  it('keeps render selector tool-call deltas out of standalone streaming wrappers', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-render-selector-tool-delta',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-render-selector-tool-delta',
      sequence: 1,
      payload: { content_delta: 'Preparing tool call.' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-render-selector-tool-delta',
      sequence: 2,
      payload: {
        tool_call_id: 'call-one',
        name_delta: 'read',
        arguments_delta: '{"path":"a.txt"}',
      },
    });

    const renderItems = visibleTimelineItemsForRender(sessionState);

    expect(renderItems.map((item) => item.type)).toEqual(['assistant_run']);
    expect(renderItems[0]).toEqual(
      expect.objectContaining({
        runId: 'run-render-selector-tool-delta',
        outputs: [
          expect.objectContaining({
            content: 'Preparing tool call.',
            streaming: true,
          }),
        ],
      }),
    );
    expect(
      renderItems.some(
        (item) =>
          item.type === 'streaming' && item.streamingItem?.type === 'tool_call',
      ),
    ).toBe(false);
  });

  it('suppresses render selector tool-call wrappers once assistant-run rows include the same call', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-render-selector-tool-call-dedup',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-render-selector-tool-call-dedup',
      sequence: 1,
      payload: { content_delta: 'Preparing tool call.' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-render-selector-tool-call-dedup',
      sequence: 2,
      payload: {
        tool_call_id: 'call-one',
        name_delta: 'read',
        arguments_delta: '{"path":"a.txt"}',
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-render-selector-tool-call-dedup',
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

    const renderItems = visibleTimelineItemsForRender(sessionState);

    expect(renderItems.map((item) => item.type)).toEqual(['assistant_run']);
    expect(renderItems[0]).toEqual(
      expect.objectContaining({
        runId: 'run-render-selector-tool-call-dedup',
        outputs: [
          expect.objectContaining({
            content: 'Preparing tool call.',
            streaming: true,
          }),
        ],
        tools: [
          expect.objectContaining({
            toolCallId: 'call-one',
            name: 'read',
            status: CHAT_STATUS_RUNNING,
          }),
        ],
      }),
    );
    expect(
      renderItems.some(
        (item) =>
          item.type === 'streaming' && item.streamingItem?.type === 'tool_call',
      ),
    ).toBe(false);
  });

  it('updates assistant-run child progress keys as compressed streaming chunks grow', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-render-selector-progress-key',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-render-selector-progress-key',
      sequence: 1,
      payload: { content_delta: 'Hel' },
    });

    const [firstRun] = visibleTimelineItemsForRender(sessionState);
    const firstOutput = firstRun.outputs[0];
    const firstKey = assistantRunChildProgressKey(firstOutput);

    expect(firstOutput.events).toHaveLength(1);
    expect(firstOutput.events[0]._streamChunkCount).toBe(1);
    expect(firstOutput.events[0]._streamLatestSequence).toBe(1);

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-render-selector-progress-key',
      sequence: 2,
      payload: { content_delta: 'lo' },
    });

    const [secondRun] = visibleTimelineItemsForRender(sessionState);
    const secondOutput = secondRun.outputs[0];
    const secondKey = assistantRunChildProgressKey(secondOutput);

    expect(secondOutput.content).toBe('Hello');
    expect(secondOutput.events).toHaveLength(1);
    expect(secondOutput.events[0]._streamChunkCount).toBe(2);
    expect(secondOutput.events[0]._streamLatestSequence).toBe(2);
    expect(secondKey).not.toBe(firstKey);
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

    let [streamingItem] = visibleTimelineItems(sessionState);

    expect(streamingItem).toEqual(
      expect.objectContaining({
        type: 'streaming',
        streamingItem: expect.objectContaining({
          type: 'assistant',
          content: 'Draft',
        }),
      }),
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 2,
      payload: { message: { role: 'assistant', content: 'Final' } },
    });

    const [assistantRun] = visibleTimelineItems(sessionState);

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
      'tool_call',
      'assistant_output',
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

  it('replaces final reasoning draft before final assistant content without duplicating it', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-final-reasoning-draft',
    );

    appendRunEvent(sessionState, {
      type: 'reasoning',
      run_id: 'run-final-reasoning-draft',
      sequence: 1,
      payload: {
        message: {
          id: 'assistant-reasoning-draft',
          role: 'assistant',
          reasoning: 'Summarize the result.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-final-reasoning-draft',
      sequence: 2,
      payload: { content_delta: 'The timeline is in chatState.js.' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-final-reasoning-draft',
      sequence: 3,
      payload: {
        message: {
          id: 'assistant-final',
          role: 'assistant',
          reasoning: 'Summarize the result.',
          content: 'The timeline is in chatState.js.',
        },
      },
    });

    const [assistantRun] = visibleTimelineItems(sessionState);

    expect(assistantRun.items.map((item) => item.type)).toEqual([
      'reasoning',
      'assistant_output',
    ]);
    expect(assistantRun.reasoning).toEqual([
      expect.objectContaining({
        content: 'Summarize the result.',
        streaming: false,
      }),
    ]);
    expect(assistantRun.outputs).toEqual([
      expect.objectContaining({
        content: 'The timeline is in chatState.js.',
        streaming: false,
      }),
    ]);
  });

  it('keeps repeated final reasoning distinct across separate tool phases', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-repeated-reasoning-tool-phases',
    );

    appendRunEvent(sessionState, {
      type: 'reasoning',
      run_id: 'run-repeated-reasoning-tool-phases',
      sequence: 1,
      payload: {
        message: { role: 'assistant', reasoning: 'Inspect the result.' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-repeated-reasoning-tool-phases',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-first',
          index: 0,
          name: 'read',
          arguments: { path: 'first.txt' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-repeated-reasoning-tool-phases',
      sequence: 3,
      payload: {
        tool_call: { id: 'call-first', index: 0, name: 'read' },
        result: { ok: true, data: { content: 'first' } },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-repeated-reasoning-tool-phases',
      sequence: 4,
      payload: {
        message: {
          role: 'assistant',
          reasoning: 'Inspect the result.',
          tool_calls: [
            {
              id: 'call-first',
              name: 'read',
              arguments: { path: 'first.txt' },
            },
          ],
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'reasoning',
      run_id: 'run-repeated-reasoning-tool-phases',
      sequence: 5,
      payload: {
        message: { role: 'assistant', reasoning: 'Inspect the result.' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-repeated-reasoning-tool-phases',
      sequence: 6,
      payload: {
        tool_call: {
          id: 'call-second',
          index: 1,
          name: 'read',
          arguments: { path: 'second.txt' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-repeated-reasoning-tool-phases',
      sequence: 7,
      payload: {
        tool_call: { id: 'call-second', index: 1, name: 'read' },
        result: { ok: true, data: { content: 'second' } },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-repeated-reasoning-tool-phases',
      sequence: 8,
      payload: {
        message: {
          role: 'assistant',
          reasoning: 'Inspect the result.',
          content: 'Done.',
          tool_calls: [
            {
              id: 'call-second',
              name: 'read',
              arguments: { path: 'second.txt' },
            },
          ],
        },
      },
    });

    const [assistantRun] = visibleTimelineItems(sessionState);

    expect(assistantRun.items.map((item) => item.type)).toEqual([
      'reasoning',
      'tool_call',
      'reasoning',
      'tool_call',
      'assistant_output',
    ]);
    expect(assistantRun.reasoning).toEqual([
      expect.objectContaining({ content: 'Inspect the result.', sequence: 1 }),
      expect.objectContaining({ content: 'Inspect the result.', sequence: 5 }),
    ]);
    expect(assistantRun.tools.map((tool) => tool.toolCallId)).toEqual([
      'call-first',
      'call-second',
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

  it('tracks the highest contiguous active-run sequence for replay handoff', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-one',
      sse_url: '/api/runs/run-one/events',
      status: CHAT_STATUS_RUNNING,
      events: [
        {
          type: 'run_started',
          run_id: 'run-one',
          sequence: 1,
          payload: { status: CHAT_STATUS_RUNNING },
        },
      ],
    });
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-one',
      sequence: 2,
      payload: { message: { role: 'user', content: 'Hi' } },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-one',
      sequence: 5,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read_file',
          arguments: { path: 'a.txt' },
        },
      },
    });

    expect(highestRunEventSequence(sessionState)).toBe(5);
    expect(highestContiguousRunEventSequence(sessionState)).toBe(2);

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-one',
      sequence: 3,
      payload: { content_delta: 'Working' },
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-one',
      sequence: 4,
      payload: { reasoning_delta: 'Checking' },
    });

    expect(highestContiguousRunEventSequence(sessionState)).toBe(5);
  });

  it('ignores older run sequences when choosing the active-run replay handoff', () => {
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
      type: 'run_completed',
      run_id: 'run-old',
      sequence: 8,
      payload: { status: CHAT_STATUS_COMPLETED },
    });
    startRun(sessionState, {
      run_id: 'run-new',
      sse_url: '/api/runs/run-new/events',
      status: CHAT_STATUS_RUNNING,
      events: [
        {
          type: 'run_started',
          run_id: 'run-new',
          sequence: 1,
          payload: { status: CHAT_STATUS_RUNNING },
        },
      ],
    });

    expect(highestRunEventSequence(sessionState)).toBe(8);
    expect(highestContiguousRunEventSequence(sessionState)).toBe(1);
  });

  it('initializes usage as null in new session state', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    expect(sessionState.usage).toBeNull();
  });

  it('sets usage via updateSessionUsage', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    const usage = { input_tokens: 8432, output_tokens: 512 };

    updateSessionUsage(sessionState, usage);

    expect(sessionState.usage).toEqual(usage);
  });

  it('updates session usage when finishRun processes a run_completed event with usage', () => {
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
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        status: CHAT_STATUS_COMPLETED,
        usage: { input_tokens: 8432, output_tokens: 512 },
      },
    });

    expect(sessionState.usage).toEqual({
      input_tokens: 8432,
      output_tokens: 512,
    });
  });

  it('does not update usage when finishRun processes a run_failed event', () => {
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
      type: 'run_failed',
      run_id: 'run-one',
      sequence: 2,
      payload: { status: CHAT_STATUS_FAILED, error: 'Something went wrong' },
    });

    expect(sessionState.usage).toBeNull();
  });

  it('does not update usage when run_completed event has no usage payload', () => {
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
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 2,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    expect(sessionState.usage).toBeNull();
  });

  it('preserves usage when run_completed event includes estimated flag', () => {
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
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        status: CHAT_STATUS_COMPLETED,
        usage: { input_tokens: 500, output_tokens: 200, estimated: true },
      },
    });

    expect(sessionState.usage).toEqual({
      input_tokens: 500,
      output_tokens: 200,
      estimated: true,
    });
  });

  it('sets usage from last assistant message when loading history', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Hi' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'Hello!',
        usage: { input_tokens: 100, output_tokens: 50 },
      },
    ]);

    expect(sessionState.usage).toEqual({
      input_tokens: 100,
      output_tokens: 50,
    });
  });

  it('picks the last assistant message usage when loading history with multiple assistant messages', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Hi' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'Hello!',
        usage: { input_tokens: 100, output_tokens: 50 },
      },
      { id: 'user-two', role: 'user', content: 'More' },
      {
        id: 'assistant-two',
        role: 'assistant',
        content: 'Sure!',
        usage: { input_tokens: 200, output_tokens: 75 },
      },
    ]);

    expect(sessionState.usage).toEqual({
      input_tokens: 200,
      output_tokens: 75,
    });
  });

  it('does not set usage when loading history with no assistant messages that have usage', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Hi' },
      { id: 'assistant-one', role: 'assistant', content: 'Hello!' },
    ]);

    expect(sessionState.usage).toBeNull();
  });

  it('does not overwrite usage from run_completed when loading history without usage', () => {
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
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        status: CHAT_STATUS_COMPLETED,
        usage: { input_tokens: 8432, output_tokens: 512 },
      },
    });

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Hi' },
      { id: 'assistant-one', role: 'assistant', content: 'Hello!' },
    ]);

    expect(sessionState.usage).toEqual({
      input_tokens: 8432,
      output_tokens: 512,
    });
  });

  it('resetStaleRun clears the live run state while preserving history and run events', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-stale',
    );

    // Seed the session with a running run, some streamed content, and a
    // tool-call phase transition so streamingPhase is non-zero before reset.
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

    // Confirm preconditions: the session is running with live run state
    expect(sessionState.status).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.streamStatus).toBe(CHAT_STATUS_RUNNING);
    expect(sessionState.currentRun).toEqual({
      runId: 'run-stale',
      sseUrl: '/api/runs/run-stale/events',
      status: CHAT_STATUS_RUNNING,
    });
    expect(sessionState.streamingItems).not.toHaveLength(0);
    expect(sessionState.streamingRunEvents).not.toHaveLength(0);
    expect(sessionState.seenStreamingEventKeys.size).toBeGreaterThan(0);
    expect(sessionState.streamingPhase).toBeGreaterThan(0);
    expect(sessionState.runEvents).not.toHaveLength(0);
    expect(sessionState.messages).toEqual([]);

    const runEventsBefore = sessionState.runEvents.slice();

    resetStaleRun(sessionState);

    // After reset: live run state is cleared
    expect(sessionState.status).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.streamStatus).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.currentRun).toBeNull();
    expect(sessionState.streamingItems).toEqual([]);
    expect(sessionState.streamingRunEvents).toEqual([]);
    expect(sessionState.streamingPhase).toBe(0);
    expect(sessionState.seenStreamingEventKeys).toEqual(new Set());

    // History source (about to be reloaded) is preserved
    expect(sessionState.runEvents).toEqual(runEventsBefore);

    // canCreateNewSession now allows a new session because no run is active
    expect(canCreateNewSession(sessionState)).toBe(true);
    expect(isRunActive(sessionState)).toBe(false);
  });

  it('resetStaleRun leaves loaded messages intact', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-stale-history',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Hi' },
      { id: 'assistant-one', role: 'assistant', content: 'Hello!' },
    ]);
    startRun(sessionState, {
      run_id: 'run-history',
      sse_url: '/api/runs/run-history/events',
      status: CHAT_STATUS_RUNNING,
      events: [
        {
          sequence: 1,
          run_id: 'run-history',
          type: 'run_started',
          payload: { status: CHAT_STATUS_RUNNING },
        },
      ],
    });

    const messagesBefore = sessionState.messages;
    const runEventsBefore = sessionState.runEvents.slice();

    resetStaleRun(sessionState);

    expect(sessionState.messages).toBe(messagesBefore);
    expect(sessionState.messages).toEqual([
      { id: 'user-one', role: 'user', content: 'Hi' },
      { id: 'assistant-one', role: 'assistant', content: 'Hello!' },
    ]);
    expect(sessionState.runEvents).toEqual(runEventsBefore);
    expect(sessionState.status).toBe(CHAT_STATUS_IDLE);
    expect(sessionState.currentRun).toBeNull();
  });
});

function countTimelineTextOccurrences(timelineItems, text) {
  let count = 0;
  for (const item of timelineItems) {
    if (item.type === 'message' && item.message?.content === text) {
      count += 1;
      continue;
    }
    if (item.type === 'assistant_run') {
      count += (item.outputs ?? []).filter(
        (output) => output.content === text,
      ).length;
    }
  }
  return count;
}

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
