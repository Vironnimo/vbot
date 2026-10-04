import { describe, expect, it, vi } from 'vitest';
import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import {
  reportedMultiStepMessages,
  setupController,
} from './chatState.support.js';

describe('History projection', () => {
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

    loadHistory(
      sessionState,
      [
        {
          history_run_id: 'run-one',
          id: 'user-one',
          role: 'user',
          content: 'Run tool',
          timestamp: '2026-05-03T14:30:00+00:00',
        },
        {
          history_run_id: 'run-one',
          id: 'assistant-tool',
          role: 'assistant',
          content: null,
          timestamp: '2026-05-03T14:30:00+00:00',
          tool_calls: [{ id: 'call-one', name: 'read', arguments: {} }],
        },
        {
          history_run_id: 'run-one',
          id: 'tool-one',
          role: 'tool',
          tool_call_id: 'call-one',
          name: 'read',
          content: '{"ok":true,"error":null,"data":{},"artifacts":[]}',
          timestamp: '2026-05-03T14:30:02+00:00',
          timing,
        },
        {
          history_run_id: 'run-one',
          id: 'assistant-final',
          role: 'assistant',
          content: 'Done',
          timestamp: '2026-05-03T14:30:03+00:00',
        },
        {
          history_run_id: 'run-one',
          id: 'summary-one',
          role: 'run_summary',
          run_id: 'run-one',
          status: 'completed',
          timestamp: '2026-05-03T14:30:03+00:00',
          timing,
        },
      ],
      { runs: [{ run_id: 'run-one', status: 'completed', complete: true }] },
    );

    const assistantRun = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run',
    );

    expect(assistantRun).toEqual(
      expect.objectContaining({
        runId: 'run-one',
        status: 'completed',
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

  it('prepends older history without duplicating loaded messages', async () => {
    const loadChatHistory = vi
      .fn()
      .mockResolvedValueOnce({
        messages: [
          { id: 'message-three', role: 'user', content: 'Three' },
          { id: 'message-four', role: 'assistant', content: 'Four' },
        ],
        has_more: true,
      })
      .mockResolvedValueOnce({
        messages: [
          { id: 'message-one', role: 'user', content: 'One' },
          { id: 'message-two', role: 'assistant', content: 'Two' },
          { id: 'message-three', role: 'user', content: 'Three duplicate' },
          { id: 'note-one', role: 'note', content: 'Internal note' },
        ],
        has_more: false,
      });
    const { chatState, controller } = setupController({
      operationOverrides: { loadChatHistory },
    });
    await controller.loadHistoryForSession('alpha', 'session-one');
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');

    expect(await controller.loadOlderHistory(sessionState)).toBe(true);

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

  it('keeps durable Background Bash statuses separate from visible history', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(
      sessionState,
      [
        { id: 'message-one', role: 'user', content: 'Hi' },
        { id: 'message-two', role: 'assistant', content: 'Hello' },
      ],
      {
        backgroundCommandStatuses: {
          term_running: 'running',
          term_finished: 'completed',
        },
      },
    );

    expect(sessionState.backgroundCommandStatuses).toEqual({
      term_running: 'running',
      term_finished: 'completed',
    });
    expect(sessionState.messages).toHaveLength(2);
  });

  it('splits consecutive assistant history messages into separate run blocks', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-automatic-follow-up-history',
    );

    loadHistory(sessionState, [
      {
        history_run_id: 'run-one',
        id: 'user-one',
        role: 'user',
        content: 'Start background work',
      },
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
      status: 'running',
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

    const timelineItems = visibleTimelineItemsForRender(sessionState);

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
        history_run_id: 'run-one',
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

describe('error messages', () => {
  it('keeps error history messages visible and outside assistant runs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(sessionState, [
      {
        history_run_id: 'run-one',
        id: 'user-one',
        role: 'user',
        content: 'Try the request',
      },
      {
        history_run_id: 'run-one',
        id: 'assistant-one',
        role: 'assistant',
        content: 'I will call the provider.',
      },
      {
        history_run_id: 'run-one',
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
});

describe('authoritative Timeline synchronization', () => {
  const state = () =>
    ensureSessionState(createChatState(), 'agent@project', 'session');

  const saved = (seq, run, role, content) => ({
    id: `message-${seq}`,
    history_sequence: seq,
    history_run_id: run,
    role,
    content,
  });

  const append = (session, run, sequence, type, payload = {}) =>
    appendRunEvent(session, {
      run_id: run,
      sequence,
      type,
      payload,
    });

  it('retains older pages and distinct occurrences of the same checkpoint on an incremental read', async () => {
    const checkpoint = {
      id: 'same-checkpoint',
      role: 'compaction_checkpoint',
      content: 'Summary',
    };
    const loadChatHistory = vi
      .fn()
      .mockResolvedValueOnce({
        messages: [{ ...checkpoint, history_sequence: 2 }],
        history_generation: 'g',
        next_after: 'cursor-3',
        has_more: true,
        next_before: 'before-2',
      })
      .mockResolvedValueOnce({
        messages: [
          saved(0, null, 'user', 'Earlier'),
          { ...checkpoint, history_sequence: 1 },
        ],
      })
      .mockResolvedValueOnce({
        messages: [saved(3, 'new', 'user', 'New')],
        history_generation: 'g',
        incremental: true,
        next_after: 'cursor-4',
      });
    const { chatState, controller } = setupController({
      operationOverrides: { loadChatHistory },
    });
    await controller.loadHistoryForSession('agent@project', 'session');
    const session = ensureSessionState(chatState, 'agent@project', 'session');
    expect(await controller.loadOlderHistory(session)).toBe(true);
    await controller.loadHistoryForSession('agent@project', 'session');
    expect(loadChatHistory.mock.calls[2][0]).toMatchObject({
      after: 'cursor-3',
    });
    expect(session.messages.map((message) => message.history_sequence)).toEqual(
      [0, 1, 2, 3],
    );
    const ids = visibleTimelineItemsForRender(session).map((item) => item.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('does not attach an identically worded User Message from another Run', () => {
    const session = state();
    const user = saved(0, 'previous', 'user', 'Again');
    loadHistory(session, [user]);
    startRun(session, { run_id: 'new' });
    append(session, 'new', 1, 'user_message_persisted', {
      message: { id: 'different', role: 'user', content: 'Again' },
    });
    expect(
      visibleTimelineItemsForRender(session).filter(
        (item) =>
          item.type === 'message' ||
          item.event?.type === 'user_message_persisted',
      ),
    ).toHaveLength(2);
  });

  it('does not apply a successor summary to an older Run with missing terminal persistence', () => {
    const session = state();
    loadHistory(session, [
      saved(0, 'older', 'user', 'First'),
      {
        ...saved(1, 'older', 'assistant', 'First output'),
        tool_calls: [{ id: 'call', name: 'read', arguments: {} }],
      },
      { ...saved(2, 'older', 'tool', 'Result'), tool_call_id: 'call' },
      {
        ...saved(3, 'new', 'run_summary', null),
        run_id: 'new',
        status: 'cancelled',
      },
    ]);
    const runs = visibleTimelineItemsForRender(session).filter(
      (item) => item.type === 'assistant_run',
    );
    expect(runs).toHaveLength(2);
    expect(runs[0].runId).toBe('older');
    expect(runs[0].outputs.map((item) => item.content)).toEqual([
      'First output',
    ]);
    expect(runs[1].runId).toBe('new');
    expect(runs[1].status).toBe('cancelled');
    expect(runs[1].items).toEqual([]);
  });
});
