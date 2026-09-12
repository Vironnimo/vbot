import { describe, expect, it } from 'vitest';
import {
  CHAT_STATUS_CANCELLED,
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
  it.each(['assistant_output_delta', 'reasoning_delta', 'tool_call_started'])(
    'keeps new %s visible after loading an automatic Run prefix',
    (type) => {
      const session = ensureSessionState(
        createChatState(),
        'alpha',
        'session-one',
      );
      const prefix = {
        id: 'prefix',
        role: 'assistant',
        content: 'Earlier saved output',
      };
      const saved = { id: 'saved', role: 'assistant', content: 'Saved output' };
      startRun(session, { run_id: 'run-one' });
      appendRunEvent(session, {
        run_id: 'run-one',
        sequence: 1,
        type: 'assistant_output',
        payload: { message: saved },
      });
      loadHistory(session, [prefix, saved]);
      const payload =
        type === 'tool_call_started'
          ? {
              tool_call: {
                id: 'new-tool',
                name: 'read',
                arguments: { path: 'example.txt' },
              },
            }
          : {
              content_delta: 'Fresh output',
              reasoning_delta: 'Fresh reasoning',
            };

      appendRunEvent(session, {
        run_id: 'run-one',
        sequence: 2,
        type,
        payload,
      });
      const timeline = visibleTimelineItemsForRender(session);

      expect(countTimelineTextOccurrences(timeline, prefix.content)).toBe(1);
      expect(countTimelineTextOccurrences(timeline, saved.content)).toBe(1);
      const children = timeline.flatMap((item) => item.items ?? []);
      if (type === 'tool_call_started') {
        expect(children.some((item) => item.toolCallId === 'new-tool')).toBe(
          true,
        );
      } else {
        expect(
          children.some(
            (item) =>
              item.content ===
              (type === 'reasoning_delta' ? 'Fresh reasoning' : 'Fresh output'),
          ),
        ).toBe(true);
      }
    },
  );

  it('keeps an unanchored replay head after its already persisted User message', () => {
    const session = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    const user = {
      id: 'user',
      role: 'user',
      content: 'Question',
      timestamp: '2026-09-05T10:00:01Z',
    };
    loadHistory(session, [user]);
    startRun(session, {
      run_id: 'run-one',
      events: [
        {
          run_id: 'run-one',
          sequence: 1,
          type: 'run_started',
          timestamp: '2026-09-05T10:00:00Z',
        },
      ],
    });

    expect(
      visibleTimelineItemsForRender(session).map((item) => item.id),
    ).toEqual(['user', 'assistant-run-run-one']);
  });

  it('retains canonical Tool results while overlaying a partial automatic-Run replay', () => {
    const session = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    const assistant = {
      id: 'saved',
      role: 'assistant',
      content: 'Inspecting',
      tool_calls: [
        { id: 'call-one', name: 'bash', arguments: { command: 'example' } },
      ],
    };
    const result = {
      id: 'result',
      role: 'tool',
      name: 'bash',
      tool_call_id: 'call-one',
      content: '{"ok":true,"data":{"content":"Canonical result"}}',
    };
    loadHistory(session, [assistant, result]);
    startRun(session, { run_id: 'run-one' });
    appendRunEvent(session, {
      run_id: 'run-one',
      sequence: 1,
      type: 'assistant_output',
      payload: { message: assistant },
    });
    appendRunEvent(session, {
      run_id: 'run-one',
      sequence: 2,
      type: 'tool_call_started',
      payload: { tool_call: assistant.tool_calls[0] },
    });
    appendRunEvent(session, {
      run_id: 'run-one',
      sequence: 3,
      type: 'tool_call_stdout',
      payload: { tool_call_id: 'call-one', data: 'Live stdout' },
    });
    appendRunEvent(session, {
      run_id: 'run-one',
      sequence: 4,
      type: 'reasoning_delta',
      payload: { reasoning_delta: 'Next step' },
    });

    const [run] = visibleTimelineItemsForRender(session);

    expect(run.tools).toHaveLength(1);
    expect(run.tools[0]).toMatchObject({
      result: result.content,
      stdout: 'Live stdout',
      status: 'success',
    });
    expect(run.reasoning[0].content).toBe('Next step');
  });

  it('keeps successive automatic-Run answers distinct through terminal History reconciliation', () => {
    const session = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    const first = { id: 'first', role: 'assistant', content: 'First step' };
    const second = { id: 'second', role: 'assistant', content: 'Second step' };
    loadHistory(session, [first]);
    startRun(session, { run_id: 'run-one' });
    appendRunEvent(session, {
      run_id: 'run-one',
      sequence: 1,
      type: 'assistant_output',
      payload: { message: first },
    });
    appendRunEvent(session, {
      run_id: 'run-one',
      sequence: 2,
      type: 'assistant_output_delta',
      payload: { content_delta: 'Second' },
    });
    appendRunEvent(session, {
      run_id: 'run-one',
      sequence: 3,
      type: 'assistant_output',
      payload: { message: second },
    });
    appendRunEvent(session, {
      run_id: 'run-one',
      sequence: 4,
      type: 'run_completed',
      payload: { status: 'completed' },
    });
    for (const message of [first, second])
      expect(
        countTimelineTextOccurrences(
          visibleTimelineItemsForRender(session),
          message.content,
        ),
      ).toBe(1);

    loadHistory(session, [
      first,
      second,
      {
        id: 'summary',
        role: 'run_summary',
        run_id: 'run-one',
        status: 'completed',
      },
    ]);

    for (const message of [first, second])
      expect(
        countTimelineTextOccurrences(
          visibleTimelineItemsForRender(session),
          message.content,
        ),
      ).toBe(1);
  });

  it('merges an automatic Run across a persisted Compaction checkpoint without duplication', () => {
    const session = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    const first = {
      id: 'first',
      role: 'assistant',
      content: 'Before compaction',
    };
    const checkpoint = {
      id: 'checkpoint',
      role: 'compaction_checkpoint',
      content: 'Checkpoint',
    };
    const second = {
      id: 'second',
      role: 'assistant',
      content: 'After compaction',
    };
    loadHistory(session, [first, checkpoint, second]);
    startRun(session, { run_id: 'run-one' });
    for (const [index, message] of [first, checkpoint, second].entries()) {
      appendRunEvent(session, {
        run_id: 'run-one',
        sequence: index + 1,
        type:
          message === checkpoint ? 'compaction_completed' : 'assistant_output',
        payload: { message },
      });
    }
    appendRunEvent(session, {
      run_id: 'run-one',
      sequence: 4,
      type: 'assistant_output_delta',
      payload: { content_delta: 'Fresh continuation' },
    });

    const timeline = visibleTimelineItemsForRender(session);

    expect(timeline).toHaveLength(1);
    expect(
      timeline[0].items.map((item) => item.content ?? item.message?.content),
    ).toEqual([
      'Before compaction',
      'Checkpoint',
      'After compaction',
      'Fresh continuation',
    ]);
  });

  it('keeps retained older Runs before the newest loaded page', () => {
    const session = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    for (const [index, runId] of ['older', 'newer'].entries()) {
      const timestamp = `2026-09-05T10:0${index}:00Z`;
      startRun(session, { run_id: runId });
      appendRunEvent(session, {
        run_id: runId,
        sequence: 1,
        timestamp,
        type: 'user_message_persisted',
        payload: {
          message: {
            id: `${runId}-user`,
            role: 'user',
            content: `${runId} question`,
            timestamp,
          },
        },
      });
      appendRunEvent(session, {
        run_id: runId,
        sequence: 2,
        timestamp,
        type: 'assistant_output',
        payload: {
          message: {
            id: `${runId}-answer`,
            role: 'assistant',
            content: `${runId} answer`,
            timestamp,
          },
        },
      });
      if (runId === 'older') {
        appendRunEvent(session, {
          run_id: runId,
          sequence: 3,
          timestamp,
          type: 'run_completed',
          payload: { status: 'completed' },
        });
      }
    }

    loadHistory(
      session,
      [
        {
          id: 'newer-user',
          role: 'user',
          content: 'newer question',
          timestamp: '2026-09-05T10:01:00Z',
        },
      ],
      { hasMore: true, nextBefore: 'newer-cursor' },
    );

    expect(
      visibleTimelineItemsForRender(session).map(
        (item) =>
          item.message?.content ??
          item.event?.payload?.message?.content ??
          item.outputs?.map((output) => output.content).join(''),
      ),
    ).toEqual([
      'older question',
      'older answer',
      'newer question',
      'newer answer',
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

    const timelineItems = visibleTimelineItemsForRender(sessionState);

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
    expect(timelineItems[1].reasoning).toEqual([
      expect.objectContaining({ content: 'Checking', streaming: true }),
    ]);
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

    expect(visibleTimelineItemsForRender(sessionState)).toEqual([
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

  it('overlays a live cancellation onto history that has not loaded its Run Summary yet', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'orchestrator@project-one',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-parent',
      sse_url: '/api/runs/run-parent/events',
      status: CHAT_STATUS_RUNNING,
    });

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Start the planner' },
      {
        id: 'assistant-subagent',
        role: 'assistant',
        content: 'I will start the planner.',
        tool_calls: [
          {
            id: 'call-subagent',
            name: 'subagent',
            arguments: {
              agent_id: 'planner',
              background: false,
              content: 'Create the plan',
            },
          },
        ],
      },
    ]);

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-parent',
      sequence: 1,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Start the planner',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-parent',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
          arguments: {
            agent_id: 'planner',
            background: false,
            content: 'Create the plan',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_cancelled',
      run_id: 'run-parent',
      sequence: 3,
      timestamp: '2026-07-27T12:11:57Z',
      payload: {
        status: CHAT_STATUS_CANCELLED,
        timing: { duration_ms: 1509909 },
      },
    });

    const timelineItems = visibleTimelineItemsForRender(sessionState);
    const assistantRun = timelineItems.find(
      (item) => item.type === 'assistant_run',
    );

    expect(timelineItems).toHaveLength(2);
    expect(assistantRun).toEqual(
      expect.objectContaining({
        id: 'history-run-assistant-subagent',
        runId: 'run-parent',
        status: CHAT_STATUS_CANCELLED,
        durationMs: 1509909,
      }),
    );
    expect(assistantRun.tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-subagent',
        status: CHAT_STATUS_CANCELLED,
      }),
    ]);
  });

  it('rehydrates interrupted reasoning-only output from persisted history', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Inspect the evidence' },
      {
        id: 'assistant-reasoning',
        role: 'assistant',
        content: null,
        reasoning: 'Inspect the evidence.',
        interrupted: true,
      },
      {
        id: 'summary-one',
        role: 'run_summary',
        run_id: 'run-cancelled',
        status: 'cancelled',
      },
    ]);

    const assistantRun = visibleTimelineItemsForRender(sessionState).find(
      (item) => item.type === 'assistant_run',
    );

    expect(assistantRun).toEqual(
      expect.objectContaining({
        source: 'history',
        status: 'cancelled',
        outputs: [],
        reasoning: [
          expect.objectContaining({
            content: 'Inspect the evidence.',
            streaming: false,
          }),
        ],
      }),
    );
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

    const timelineItems = visibleTimelineItemsForRender(sessionState);

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
});
