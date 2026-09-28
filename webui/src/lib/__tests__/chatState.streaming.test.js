import { describe, expect, it } from 'vitest';
import {
  appendRunEvent,
  assistantRunChildProgressKey,
  createChatState,
  ensureSessionState,
  highestContiguousRunEventSequence,
  loadHistory,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';

describe('streamed drafts', () => {
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

  it('renders streamed Tool Call deltas as a preparing row inside the assistant run until the call starts', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-tool-preview',
    );
    const append = (sequence, type, payload) =>
      appendRunEvent(sessionState, {
        type,
        run_id: 'run-tool-preview',
        sequence,
        payload,
      });
    const hasStandaloneToolWrapper = (items) =>
      items.some(
        (item) =>
          item.type === 'streaming' && item.streamingItem?.type === 'tool_call',
      );

    append(1, 'assistant_output_delta', { content_delta: 'Writing notes.' });
    append(2, 'tool_call_delta', {
      tool_call_id: 'call-one',
      name_delta: 'write',
      arguments_delta: '{"path": "notes/to',
    });
    append(3, 'tool_call_delta', {
      tool_call_id: 'call-one',
      name_delta: '',
      arguments_delta: 'do.md", "content": "# Title',
    });

    let renderItems = visibleTimelineItemsForRender(sessionState);
    expect(renderItems.map((item) => item.type)).toEqual(['assistant_run']);
    expect(hasStandaloneToolWrapper(renderItems)).toBe(false);
    expect(renderItems[0].outputs).toEqual([
      expect.objectContaining({ content: 'Writing notes.', streaming: true }),
    ]);
    // Preview arguments only carry values whose stream already ended.
    expect(renderItems[0].tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-one',
        name: 'write',
        status: 'preparing',
        streaming: true,
        startedEvent: null,
        partialArgumentsText: '{"path": "notes/todo.md", "content": "# Title',
        previewArguments: { path: 'notes/todo.md' },
      }),
    ]);

    append(4, 'tool_call_started', {
      tool_call: {
        id: 'call-one',
        index: 0,
        name: 'write',
        arguments: { path: 'notes/todo.md', content: '# Title' },
      },
    });

    renderItems = visibleTimelineItemsForRender(sessionState);
    expect(renderItems.map((item) => item.type)).toEqual(['assistant_run']);
    expect(hasStandaloneToolWrapper(renderItems)).toBe(false);
    expect(renderItems[0].tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-one',
        name: 'write',
        status: 'running',
        streaming: false,
        partialArgumentsText: null,
        previewArguments: null,
        arguments: { path: 'notes/todo.md', content: '# Title' },
      }),
    ]);
  });

  it('keeps interleaved sibling Tool Call deltas on their own rows', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-sibling-tool-deltas',
    );
    const delta = (sequence, toolCallId, nameDelta, argumentsDelta) =>
      appendRunEvent(sessionState, {
        type: 'tool_call_delta',
        run_id: 'run-sibling-tool-deltas',
        sequence,
        payload: {
          tool_call_id: toolCallId,
          name_delta: nameDelta,
          arguments_delta: argumentsDelta,
        },
      });

    delta(1, 'call-one', 'read', '{"path"');
    delta(2, 'call-two', 'grep', '{"pattern"');
    delta(3, 'call-one', '', ': "a.txt"}');

    expect(visibleTimelineItemsForRender(sessionState)[0].tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-one',
        name: 'read',
        partialArgumentsText: '{"path": "a.txt"}',
      }),
      expect.objectContaining({
        toolCallId: 'call-two',
        name: 'grep',
        partialArgumentsText: '{"pattern"',
      }),
    ]);
  });

  it('merges interleaved Tool stdout and stderr chunks into the matching Tool rows', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-tool-output',
    );
    const chunk = (sequence, type, toolCallId, data) =>
      appendRunEvent(sessionState, {
        type,
        run_id: 'run-one',
        sequence,
        payload: { tool_call_id: toolCallId, data },
      });
    const tools = () => visibleTimelineItemsForRender(sessionState)[0].tools;

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
    chunk(2, 'tool_call_stdout', 'call-one', 'hel');
    const earlyKey = assistantRunChildProgressKey(tools()[0]);
    chunk(3, 'tool_call_stderr', 'call-one', 'warn');
    chunk(4, 'tool_call_stdout', 'call-one', 'lo\n');
    chunk(5, 'tool_call_stdout', 'call-two', 'other\n');

    expect(tools()).toEqual([
      expect.objectContaining({
        toolCallId: 'call-one',
        stdout: 'hello\n',
        stderr: 'warn',
      }),
      expect.objectContaining({ toolCallId: 'call-two', stdout: 'other\n' }),
    ]);
    // A growing output re-renders its row.
    expect(assistantRunChildProgressKey(tools()[0])).not.toBe(earlyKey);
  });

  it('updates the assistant-run child progress key as streamed text grows', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-progress-key',
    );
    const delta = (sequence, content) =>
      appendRunEvent(sessionState, {
        type: 'assistant_output_delta',
        run_id: 'run-progress-key',
        sequence,
        payload: { content_delta: content },
      });
    const output = () =>
      visibleTimelineItemsForRender(sessionState)[0].outputs[0];

    delta(1, 'Hel');
    const firstKey = assistantRunChildProgressKey(output());
    delta(2, 'lo');

    expect(output().content).toBe('Hello');
    expect(assistantRunChildProgressKey(output())).not.toBe(firstKey);
  });

  it.each([
    {
      type: 'tool_call_stdout',
      payload: { tool_call_id: 'call-one', data: 'hello\n' },
      rendered: (run) => run.tools[0].stdout,
      expected: 'hello\n',
    },
    {
      type: 'assistant_output_delta',
      payload: { content_delta: 'Hi' },
      rendered: (run) => run.outputs[0].content,
      expected: 'Hi',
    },
  ])(
    'ignores a replayed $type chunk with an already applied sequence',
    ({ type, payload, rendered, expected }) => {
      const sessionState = ensureSessionState(
        createChatState(),
        'alpha',
        'session-one',
      );
      const event = { type, run_id: 'run-one', sequence: 1, payload };

      appendRunEvent(sessionState, event);
      appendRunEvent(sessionState, structuredClone(event));

      expect(rendered(visibleTimelineItemsForRender(sessionState)[0])).toBe(
        expected,
      );
    },
  );
});

describe('stream attempt restarts', () => {
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

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);
    expect(assistantRun.tools).toEqual([]);
    expect(assistantRun.reasoning).toEqual([
      expect.objectContaining({
        type: 'reasoning',
        content: 'Recovered plan.',
      }),
    ]);
  });

  it('drops replayed deltas of an attempt whose restart was already applied', () => {
    const runId = 'run-returned';
    const event = (sequence, type, payload = {}) => ({
      run_id: runId,
      sequence,
      type,
      payload,
    });
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-returned',
    );
    startRun(sessionState, { run_id: runId, sse_url: '/x' });
    appendRunEvent(sessionState, event(1, 'run_started'));

    // The stable restart boundary arrived before SSE replays the deltas.
    appendRunEvent(sessionState, event(4, 'stream_attempt_restarted'));
    for (const [sequence, delta] of [
      [2, 'Failed '],
      [3, 'attempt'],
      [5, 'Recovered'],
    ]) {
      appendRunEvent(
        sessionState,
        event(sequence, 'assistant_output_delta', { content_delta: delta }),
      );
    }

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);
    expect(
      assistantRun.items.map((child) => `${child.type}:${child.content}`),
    ).toEqual(['assistant_output:Recovered']);
  });
});

describe('reasoning duration', () => {
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
      status: 'running',
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

  it.each([
    {
      name: 'stays streaming without an estimate across a provider heartbeat',
      event: {
        type: 'provider_heartbeat',
        timestamp: '2026-08-24T10:00:05+00:00',
        payload: { idle_seconds: 75.4, state: 'waiting_for_model_delta' },
      },
      reasoning: {
        streaming: true,
        durationMs: null,
        durationEstimateMs: null,
      },
    },
    {
      name: 'freezes its estimate at a terminal event without a stable boundary',
      event: {
        type: 'run_failed',
        timestamp: '2026-08-24T10:00:07+00:00',
        payload: { status: 'failed' },
      },
      reasoning: { durationMs: null, durationEstimateMs: 7000 },
    },
  ])('a streamed reasoning draft $name', ({ event, reasoning }) => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reasoning-estimate',
    );
    startRun(sessionState, {
      run_id: 'run-reasoning-estimate',
      sse_url: '/api/runs/run-reasoning-estimate/events',
      status: 'running',
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-reasoning-estimate',
      sequence: 1,
      timestamp: '2026-08-24T10:00:00+00:00',
      payload: { reasoning_delta: 'Thinking' },
    });
    appendRunEvent(sessionState, {
      ...event,
      run_id: 'run-reasoning-estimate',
      sequence: 2,
    });

    expect(visibleTimelineItemsForRender(sessionState)[0].reasoning).toEqual([
      expect.objectContaining({ type: 'reasoning', ...reasoning }),
    ]);
  });
});

describe('replay cursor', () => {
  it('tracks the highest contiguous active-run sequence for replay handoff', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-one',
      sse_url: '/api/runs/run-one/events',
      status: 'running',
      events: [
        {
          type: 'run_started',
          run_id: 'run-one',
          sequence: 1,
          payload: { status: 'running' },
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

  it('advances the replay cursor across interleaved tool output streams', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-one',
      sse_url: '/api/runs/run-one/events',
      status: 'running',
      events: [
        {
          type: 'run_started',
          run_id: 'run-one',
          sequence: 1,
          payload: { status: 'running' },
        },
      ],
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_stdout',
      run_id: 'run-one',
      sequence: 2,
      payload: { tool_call_id: 'call-one', data: 'a' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_stderr',
      run_id: 'run-one',
      sequence: 3,
      payload: { tool_call_id: 'call-one', data: 'b' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_stdout',
      run_id: 'run-one',
      sequence: 4,
      payload: { tool_call_id: 'call-one', data: 'c' },
    });

    // The compressed stdout event spans sequences 2 and 4, so only the raw
    // per-chunk keys keep the cursor contiguous across the interleaved stream.
    expect(highestContiguousRunEventSequence(sessionState)).toBe(4);
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
      payload: { status: 'running' },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-old',
      sequence: 8,
      payload: { status: 'completed' },
    });
    startRun(sessionState, {
      run_id: 'run-new',
      sse_url: '/api/runs/run-new/events',
      status: 'running',
      events: [
        {
          type: 'run_started',
          run_id: 'run-new',
          sequence: 1,
          payload: { status: 'running' },
        },
      ],
    });

    expect(highestContiguousRunEventSequence(sessionState)).toBe(1);
  });
});
