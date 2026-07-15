import { appendRunEvent } from '../chatState.js';

export function countTimelineTextOccurrences(timelineItems, text) {
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

export function reportedMultiStepMessages() {
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

export function appendReportedLiveRunEvents(
  sessionState,
  runId,
  startSequence = 1,
) {
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
