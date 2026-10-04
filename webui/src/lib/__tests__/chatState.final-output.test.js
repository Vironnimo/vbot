import { describe, expect, it } from 'vitest';
import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import {
  appendReportedLiveRunEvents,
  reportedMultiStepMessages,
} from './chatState.support.js';

describe('final assistant output', () => {
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
      status: 'running',
    });

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-reported-overlap',
      sequence: 1,
      payload: { message: reportedMultiStepMessages()[0] },
    });
    appendReportedLiveRunEvents(sessionState, 'run-reported-overlap', 2);
    loadHistory(
      sessionState,
      reportedMultiStepMessages().map((message) => ({
        ...message,
        history_run_id: 'run-reported-overlap',
      })),
    );

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

  it('keeps the final streamed answer visible when completion arrives before canonical output', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );
    startRun(sessionState, {
      run_id: 'run-one',
      sse_url: '/api/runs/run-one/events',
      status: 'running',
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
      payload: { status: 'completed' },
    });

    expect(visibleTimelineItemsForRender(sessionState)).toEqual([
      expect.objectContaining({
        type: 'assistant_run',
        status: 'completed',
        outputs: [
          expect.objectContaining({
            content: 'Draft',
            streaming: true,
          }),
        ],
        // The unfinished Tool Call preview ends with the Run.
        tools: [],
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
      status: 'running',
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
      type: 'tool_call_output',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        tool_call_id: 'call-one',
        terminal_id: 'term_one',
        screen: 'hello',
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-one',
      sequence: 3,
      payload: { status: 'completed' },
    });

    const [assistantRun] = visibleTimelineItemsForRender(sessionState);
    expect(assistantRun.status).toBe('completed');
    expect(assistantRun.tools[0]).toEqual(
      expect.objectContaining({ toolCallId: 'call-one', output: 'hello' }),
    );
  });
});
