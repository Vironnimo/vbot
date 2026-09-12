import { describe, expect, it } from 'vitest';
import {
  CHAT_STATUS_RUNNING,
  assistantRunChildProgressKey,
  appendRunEvent,
  createChatState,
  ensureSessionState,
  visibleTimelineItemsForRender,
} from '../chatState.js';

describe('chat state helpers', () => {
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

  it('compresses interleaved tool stdout/stderr chunks into one retained event per stream', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-compressed-tool-output',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_stdout',
      run_id: 'run-one',
      sequence: 1,
      payload: { tool_call_id: 'call-one', data: 'hel' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_stderr',
      run_id: 'run-one',
      sequence: 2,
      payload: { tool_call_id: 'call-one', data: 'warn' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_stdout',
      run_id: 'run-one',
      sequence: 3,
      payload: { tool_call_id: 'call-one', data: 'lo\n' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_stdout',
      run_id: 'run-one',
      sequence: 4,
      payload: { tool_call_id: 'call-two', data: 'other\n' },
    });

    expect(sessionState.streamingRunEvents).toEqual([
      expect.objectContaining({
        type: 'tool_call_stdout',
        sequence: 1,
        payload: expect.objectContaining({
          tool_call_id: 'call-one',
          data: 'hello\n',
        }),
        _streamChunkCount: 2,
        _streamLatestSequence: 3,
      }),
      expect.objectContaining({
        type: 'tool_call_stderr',
        sequence: 2,
        payload: expect.objectContaining({
          tool_call_id: 'call-one',
          data: 'warn',
        }),
        _streamChunkCount: 1,
        _streamLatestSequence: 2,
      }),
      expect.objectContaining({
        type: 'tool_call_stdout',
        sequence: 4,
        payload: expect.objectContaining({
          tool_call_id: 'call-two',
          data: 'other\n',
        }),
        _streamChunkCount: 1,
        _streamLatestSequence: 4,
      }),
    ]);

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);
    expect(assistantRun.tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-one',
        stdout: 'hello\n',
        stderr: 'warn',
      }),
      expect.objectContaining({ toolCallId: 'call-two', stdout: 'other\n' }),
    ]);
  });

  it('ignores duplicate tool stdout event sequences on replay', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-compressed-tool-output-replay',
    );
    const chunkEvent = {
      type: 'tool_call_stdout',
      run_id: 'run-one',
      sequence: 1,
      payload: { tool_call_id: 'call-one', data: 'hello\n' },
    };

    appendRunEvent(sessionState, chunkEvent);
    appendRunEvent(sessionState, chunkEvent);

    expect(sessionState.streamingRunEvents).toEqual([
      expect.objectContaining({
        type: 'tool_call_stdout',
        payload: expect.objectContaining({ data: 'hello\n' }),
        _streamChunkCount: 1,
      }),
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
});
