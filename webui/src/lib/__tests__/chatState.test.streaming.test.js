import { describe, expect, it } from 'vitest';
import {
  CHAT_STATUS_RUNNING,
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';

describe('chat state helpers', () => {
  it('discards failed-attempt reasoning and Tool previews before a stream restart', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-stream-restart',
    );

    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-stream-restart',
      sequence: 1,
      payload: { reasoning_delta: 'Discard this plan.' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-stream-restart',
      sequence: 2,
      payload: {
        tool_call_id: 'call-discarded',
        name_delta: 'read',
        arguments_delta: '{"path":"partial',
      },
    });
    appendRunEvent(sessionState, {
      type: 'stream_attempt_restarted',
      run_id: 'run-stream-restart',
      sequence: 3,
      payload: {},
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-stream-restart',
      sequence: 4,
      payload: { reasoning_delta: 'Recovered plan.' },
    });

    const renderItems = visibleTimelineItemsForRender(sessionState);

    expect(sessionState.streamingPhase).toBe(1);
    expect(sessionState.streamingRunEvents).toHaveLength(1);
    expect(renderItems[0].tools).toEqual([]);
    expect(renderItems[0].reasoning).toEqual([
      expect.objectContaining({
        type: 'reasoning',
        content: 'Recovered plan.',
      }),
    ]);
  });

  it('captures the persisted reasoning duration from the stable reasoning event', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reasoning-duration',
    );
    startRun(sessionState, {
      run_id: 'run-reasoning-duration',
      sse_url: '/api/runs/run-reasoning-duration/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-reasoning-duration',
      sequence: 1,
      payload: { reasoning_delta: 'Thinking' },
    });
    appendRunEvent(sessionState, {
      type: 'reasoning',
      run_id: 'run-reasoning-duration',
      sequence: 2,
      payload: {
        message: {
          id: 'assistant-one',
          role: 'assistant',
          model: 'openai/gpt-5.2',
          content: 'Answer.',
          reasoning: 'Thinking',
          reasoning_timing: {
            started_at: '2026-08-24T10:00:00+00:00',
            completed_at: '2026-08-24T10:00:04+00:00',
            duration_ms: 4200,
          },
        },
      },
    });

    const renderItems = visibleTimelineItemsForRender(sessionState);

    expect(renderItems[0].reasoning).toEqual([
      expect.objectContaining({
        type: 'reasoning',
        durationMs: 4200,
        streaming: false,
      }),
    ]);
  });

  it('carries the reasoning duration from history assistant messages', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reasoning-duration-history',
    );

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Hi' },
      {
        id: 'assistant-one',
        role: 'assistant',
        model: 'openai/gpt-5.2',
        content: 'Answer.',
        reasoning: 'Thinking',
        reasoning_timing: {
          started_at: '2026-08-24T10:00:00+00:00',
          completed_at: '2026-08-24T10:00:02+00:00',
          duration_ms: 2000,
        },
      },
    ]);

    const renderItems = visibleTimelineItemsForRender(sessionState);

    expect(renderItems[1].reasoning).toEqual([
      expect.objectContaining({
        type: 'reasoning',
        content: 'Thinking',
        durationMs: 2000,
      }),
    ]);
  });

  it('freezes the streamed reasoning estimate when Tool Calls begin and replaces it at the stable boundary', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reasoning-freeze',
    );
    startRun(sessionState, {
      run_id: 'run-reasoning-freeze',
      sse_url: '/api/runs/run-reasoning-freeze/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-reasoning-freeze',
      sequence: 1,
      timestamp: '2026-08-24T10:00:00+00:00',
      payload: { reasoning_delta: 'Thinking' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-reasoning-freeze',
      sequence: 2,
      timestamp: '2026-08-24T10:00:03+00:00',
      payload: {
        tool_call: { id: 'call-one', index: 0, name: 'read', arguments: {} },
      },
    });

    const renderItems = visibleTimelineItemsForRender(sessionState);

    expect(renderItems[0].reasoning).toEqual([
      expect.objectContaining({
        type: 'reasoning',
        streaming: true,
        durationMs: null,
        durationEstimateMs: 3000,
      }),
    ]);

    appendRunEvent(sessionState, {
      type: 'reasoning',
      run_id: 'run-reasoning-freeze',
      sequence: 3,
      timestamp: '2026-08-24T10:00:09+00:00',
      payload: {
        message: {
          id: 'assistant-one',
          role: 'assistant',
          model: 'openai/gpt-5.2',
          content: 'Answer.',
          reasoning: 'Thinking',
          reasoning_timing: {
            started_at: '2026-08-24T10:00:00+00:00',
            completed_at: '2026-08-24T10:00:04.2+00:00',
            duration_ms: 4200,
          },
        },
      },
    });

    expect(visibleTimelineItemsForRender(sessionState)[0].reasoning).toEqual([
      expect.objectContaining({
        type: 'reasoning',
        streaming: false,
        durationMs: 4200,
        durationEstimateMs: null,
      }),
    ]);
  });

  it('keeps the reasoning draft streaming across a provider heartbeat', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reasoning-heartbeat',
    );
    startRun(sessionState, {
      run_id: 'run-reasoning-heartbeat',
      sse_url: '/api/runs/run-reasoning-heartbeat/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-reasoning-heartbeat',
      sequence: 1,
      timestamp: '2026-08-24T10:00:00+00:00',
      payload: { reasoning_delta: 'Thinking' },
    });
    appendRunEvent(sessionState, {
      type: 'provider_heartbeat',
      run_id: 'run-reasoning-heartbeat',
      sequence: 2,
      timestamp: '2026-08-24T10:00:05+00:00',
      payload: {
        idle_seconds: 75.4,
        state: 'waiting_for_model_delta',
      },
    });

    const renderItems = visibleTimelineItemsForRender(sessionState);

    expect(renderItems[0].reasoning).toEqual([
      expect.objectContaining({
        type: 'reasoning',
        streaming: true,
        durationMs: null,
        durationEstimateMs: null,
      }),
    ]);
  });

  it('freezes the streamed reasoning estimate at a terminal event without a stable boundary', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reasoning-terminal-freeze',
    );
    startRun(sessionState, {
      run_id: 'run-reasoning-terminal-freeze',
      sse_url: '/api/runs/run-reasoning-terminal-freeze/events',
      status: CHAT_STATUS_RUNNING,
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-reasoning-terminal-freeze',
      sequence: 1,
      timestamp: '2026-08-24T10:00:00+00:00',
      payload: { reasoning_delta: 'Thinking' },
    });
    appendRunEvent(sessionState, {
      type: 'run_failed',
      run_id: 'run-reasoning-terminal-freeze',
      sequence: 2,
      timestamp: '2026-08-24T10:00:07+00:00',
      payload: { status: 'failed' },
    });

    const renderItems = visibleTimelineItemsForRender(sessionState);

    expect(renderItems[0].reasoning).toEqual([
      expect.objectContaining({
        type: 'reasoning',
        durationMs: null,
        durationEstimateMs: 7000,
      }),
    ]);
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

  it('renders a preparing tool row inside the assistant run from tool-call deltas', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-render-selector-tool-preview',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-render-selector-tool-preview',
      sequence: 1,
      payload: { content_delta: 'Searching past sessions.' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-render-selector-tool-preview',
      sequence: 2,
      payload: {
        tool_call_id: 'call-one',
        name_delta: 'session_search',
        arguments_delta: '{"query": "ca',
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-render-selector-tool-preview',
      sequence: 3,
      payload: {
        tool_call_id: 'call-one',
        name_delta: '',
        arguments_delta: 'rs"}',
      },
    });

    const renderItems = visibleTimelineItemsForRender(sessionState);

    expect(renderItems.map((item) => item.type)).toEqual(['assistant_run']);
    expect(renderItems[0].tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-one',
        name: 'session_search',
        partialArgumentsText: '{"query": "cars"}',
        streaming: true,
        status: 'preparing',
        startedEvent: null,
      }),
    ]);
  });

  it('keeps every known sibling Tool call visible when the Run is cancelled', () => {
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

    for (let index = 0; index < 12; index += 1) {
      appendRunEvent(sessionState, {
        type: 'tool_call_delta',
        run_id: 'run-cancelled-siblings',
        sequence: index + 1,
        payload: {
          tool_call_id: `call-${index}`,
          name_delta: 'bash',
          arguments_delta: `{"command":"command ${index}"}`,
        },
      });
    }
    for (let index = 0; index < 4; index += 1) {
      appendRunEvent(sessionState, {
        type: 'tool_call_started',
        run_id: 'run-cancelled-siblings',
        sequence: 13 + index,
        payload: {
          tool_call: {
            id: `call-${index}`,
            index,
            name: 'bash',
            arguments: { command: `command ${index}` },
          },
        },
      });
    }
    for (let index = 0; index < 2; index += 1) {
      appendRunEvent(sessionState, {
        type: 'tool_call_result',
        run_id: 'run-cancelled-siblings',
        sequence: 17 + index,
        payload: {
          tool_call: { id: `call-${index}`, index, name: 'bash' },
          result: { ok: false, error: 'Command failed' },
        },
      });
    }

    appendRunEvent(sessionState, {
      type: 'run_cancelled',
      run_id: 'run-cancelled-siblings',
      sequence: 19,
      timestamp: '2026-08-07T12:00:00.000Z',
      payload: { status: 'cancelled' },
    });

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);

    expect(assistantRun.tools).toHaveLength(12);
    expect(assistantRun.tools.map((tool) => tool.toolCallId)).toEqual(
      Array.from({ length: 12 }, (_value, index) => `call-${index}`),
    );
    expect(assistantRun.tools.map((tool) => tool.status)).toEqual([
      'failed',
      'failed',
      ...Array.from({ length: 10 }, () => 'cancelled'),
    ]);
  });

  it('surfaces preview arguments on a preparing tool row before the value stream ends', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-tool-argument-preview',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-tool-argument-preview',
      sequence: 1,
      payload: {
        tool_call_id: 'call-one',
        name_delta: 'write',
        arguments_delta: '{"path": "notes/to',
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-tool-argument-preview',
      sequence: 2,
      payload: {
        tool_call_id: 'call-one',
        name_delta: '',
        arguments_delta: 'do.md", "content": "# Title',
      },
    });

    const renderItems = visibleTimelineItemsForRender(sessionState);

    expect(renderItems[0].tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-one',
        name: 'write',
        status: 'preparing',
        previewArguments: { path: 'notes/todo.md' },
      }),
    ]);
  });

  it('drops preview arguments once the tool call actually starts', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-tool-argument-preview-cleared',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-tool-argument-preview-cleared',
      sequence: 1,
      payload: {
        tool_call_id: 'call-one',
        name_delta: 'write',
        arguments_delta: '{"path": "notes/todo.md", "content": "# Title"}',
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-tool-argument-preview-cleared',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'write',
          arguments: { path: 'notes/todo.md', content: '# Title' },
        },
      },
    });

    const renderItems = visibleTimelineItemsForRender(sessionState);

    expect(renderItems[0].tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-one',
        name: 'write',
        previewArguments: null,
        arguments: { path: 'notes/todo.md', content: '# Title' },
      }),
    ]);
  });
});
