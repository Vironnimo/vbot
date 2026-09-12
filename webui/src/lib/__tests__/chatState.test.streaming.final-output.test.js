import { describe, expect, it } from 'vitest';
import {
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_RUNNING,
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

  it('keeps streamed tool output visible after the run completes until history confirms', () => {
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
      payload: { tool_call_id: 'call-one', data: 'hello\n' },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 3,
      payload: { status: CHAT_STATUS_COMPLETED },
    });

    expect(sessionState.streamingRunEvents).toEqual([
      expect.objectContaining({
        type: 'tool_call_stdout',
        payload: expect.objectContaining({ data: 'hello\n' }),
      }),
    ]);
    const [assistantRun] = visibleTimelineItemsForRender(sessionState);
    expect(assistantRun.status).toBe(CHAT_STATUS_COMPLETED);
    expect(assistantRun.tools[0]).toEqual(
      expect.objectContaining({ toolCallId: 'call-one', stdout: 'hello\n' }),
    );
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

  it('advances the replay cursor across interleaved tool output streams', () => {
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
