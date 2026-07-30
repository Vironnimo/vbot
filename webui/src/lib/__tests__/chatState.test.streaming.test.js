import { describe, expect, it } from 'vitest';

import {
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_RUNNING,
  assistantRunChildProgressKey,
  appendRunEvent,
  createChatState,
  ensureSessionState,
  highestContiguousRunEventSequence,
  loadHistory,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import {
  appendReportedLiveRunEvents,
  reportedMultiStepMessages,
} from './chatState.support.js';

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

  it('compresses interleaved sibling tool-call deltas into one retained event per call', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-compressed-sibling-tool-deltas',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-compressed-sibling-tool-deltas',
      sequence: 1,
      payload: {
        tool_call_id: 'call-one',
        name_delta: 'read',
        arguments_delta: '{"path"',
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-compressed-sibling-tool-deltas',
      sequence: 2,
      payload: {
        tool_call_id: 'call-two',
        name_delta: 'grep',
        arguments_delta: '{"pattern"',
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-compressed-sibling-tool-deltas',
      sequence: 3,
      payload: {
        tool_call_id: 'call-one',
        name_delta: '',
        arguments_delta: ': "a.txt"}',
      },
    });

    expect(sessionState.streamingRunEvents).toEqual([
      expect.objectContaining({
        type: 'tool_call_delta',
        sequence: 1,
        payload: expect.objectContaining({
          tool_call_id: 'call-one',
          name_delta: 'read',
          arguments_delta: '{"path": "a.txt"}',
        }),
        _streamChunkCount: 2,
        _streamLatestSequence: 3,
      }),
      expect.objectContaining({
        type: 'tool_call_delta',
        sequence: 2,
        payload: expect.objectContaining({
          tool_call_id: 'call-two',
          name_delta: 'grep',
          arguments_delta: '{"pattern"',
        }),
        _streamChunkCount: 1,
        _streamLatestSequence: 2,
      }),
    ]);

    const renderItems = visibleTimelineItemsForRender(sessionState);
    expect(renderItems[0].tools).toEqual([
      expect.objectContaining({ toolCallId: 'call-one', name: 'read' }),
      expect.objectContaining({ toolCallId: 'call-two', name: 'grep' }),
    ]);
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
            streaming: false,
            partialArgumentsText: null,
            arguments: { path: 'a.txt' },
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

    expect(sessionState.streamingRunEvents).toHaveLength(1);
    expect(sessionState.streamingRunEvents[0].payload.content_delta).toBe('Hi');
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

    expect(visibleTimelineItemsForRender(sessionState)).toEqual([
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

    const [draftRun] = visibleTimelineItemsForRender(sessionState);

    expect(draftRun).toEqual(
      expect.objectContaining({
        type: 'assistant_run',
        outputs: [
          expect.objectContaining({
            content: 'Draft',
            streaming: true,
          }),
        ],
      }),
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 2,
      payload: { message: { role: 'assistant', content: 'Final' } },
    });

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);

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

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);

    // The streamed answer precedes the tool call it announced, and the final
    // authoritative message replaces the streamed draft in place.
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

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);

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

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);

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

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);

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

    const timelineItems = visibleTimelineItemsForRender(sessionState);
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

  it('keeps the final streamed answer visible when completion arrives before canonical output', () => {
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
      type: 'tool_call_delta',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        tool_call_id: 'call-incomplete',
        name_delta: 'read',
      },
    });

    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 3,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    expect(sessionState.streamingRunEvents).toEqual([
      expect.objectContaining({
        type: 'assistant_output_delta',
        payload: expect.objectContaining({ content_delta: 'Draft' }),
      }),
    ]);
    expect(visibleTimelineItemsForRender(sessionState)).toEqual([
      expect.objectContaining({
        type: 'assistant_run',
        status: CHAT_STATUS_COMPLETED,
        outputs: [
          expect.objectContaining({
            content: 'Draft',
            streaming: true,
          }),
        ],
      }),
    ]);
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

    expect(highestContiguousRunEventSequence(sessionState)).toBe(1);
  });
});
