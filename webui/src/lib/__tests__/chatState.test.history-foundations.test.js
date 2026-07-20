import { describe, expect, it } from 'vitest';

import {
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_RUNNING,
  addServerQueuedMessage,
  appendRunEvent,
  createChatState,
  currentSessionState,
  ensureSessionState,
  loadHistory,
  selectedAgent,
  setAgents,
  startRun,
  prependHistory,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { reportedMultiStepMessages } from './chatState.support.js';

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

  it('does not expose internal continuation data as client state', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-continuation',
    );
    const continuation = {
      checkpoint_id: 'checkpoint-one',
      cause: 'network',
    };

    loadHistory(sessionState, [], { continuation });
    expect(sessionState).not.toHaveProperty('continuation');

    startRun(sessionState, {
      run_id: 'run-two',
      sse_url: '/runs/run-two',
      status: 'running',
    });
    appendRunEvent(sessionState, {
      type: 'run_failed',
      run_id: 'run-two',
      sequence: 1,
      payload: { status: 'failed', continuation },
    });
    expect(sessionState).not.toHaveProperty('continuation');
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

    const assistantRun = visibleTimelineItemsForRender(sessionState).find(
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

    const assistantRun = visibleTimelineItemsForRender(sessionState).find(
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
    expect(visibleTimelineItemsForRender(sessionState)).toEqual([
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
    expect(
      JSON.stringify(visibleTimelineItemsForRender(sessionState)),
    ).not.toContain('Internal reminder');
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
        content: 'Background sub-agent started.',
      },
      {
        id: 'assistant-follow-up',
        role: 'assistant',
        content: 'Background sub-agent finished.',
      },
    ]);

    const timelineItems = visibleTimelineItemsForRender(sessionState);

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

    const timelineItems = visibleTimelineItemsForRender(sessionState);

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

    const timelineItems = visibleTimelineItemsForRender(sessionState);

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

    expect(visibleTimelineItemsForRender(sessionState)).toEqual([
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

    const timelineItems = visibleTimelineItemsForRender(sessionState);
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

    const timelineItems = visibleTimelineItemsForRender(sessionState);
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
});
