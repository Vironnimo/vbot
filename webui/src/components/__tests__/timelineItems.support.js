// Server-shaped semantic fixtures. These builders describe a row directly;
// they never fold raw Run events or reconstruct persisted conversations.
export function messageItem(message, options = {}) {
  return { id: message.id, type: 'message', message, ...options };
}

export function outputItem(content, options = {}) {
  return {
    id: 'output-one',
    type: 'assistant_output',
    content,
    streaming: false,
    messages: [],
    events: [],
    ...options,
  };
}

export function reasoningItem(content, options = {}) {
  return {
    id: 'reasoning-one',
    type: 'reasoning',
    content,
    streaming: false,
    messages: [],
    events: [],
    ...options,
  };
}

export function toolItem({
  id = 'call-one',
  name = 'read',
  arguments: args = {},
  result,
  status = result === undefined ? 'running' : 'success',
  display = null,
  timing = null,
  ...options
} = {}) {
  return {
    id,
    type: 'tool_call',
    toolCallId: id,
    identityKey: `id-${id}`,
    key: `id-${id}`,
    name,
    arguments: args,
    status,
    display,
    timing,
    durationMs: timing?.duration_ms ?? null,
    result,
    started: true,
    startedAt: null,
    finished: result !== undefined,
    finishedAt: null,
    rawTruncated: false,
    ...options,
  };
}

export function assistantRun(runId, items, options = {}) {
  return {
    id: `assistant-run-${runId}`,
    type: 'assistant_run',
    runId,
    run_id: runId,
    source: 'live',
    status: 'running',
    items,
    tools: items.filter((item) => item.type === 'tool_call'),
    outputs: items.filter((item) => item.type === 'assistant_output'),
    reasoning: items.filter((item) => item.type === 'reasoning'),
    events: [],
    ...options,
  };
}

export function reportedMultiStepItems() {
  return [
    messageItem({
      id: 'user-reported',
      role: 'user',
      content: 'Investigate the duplicated chat UI.',
    }),
    assistantRun(
      'reported',
      [
        reasoningItem('Find candidate files.', { id: 'thinking-glob' }),
        toolItem({
          id: 'call-glob',
          name: 'glob',
          arguments: { pattern: 'webui/src/**/*.js' },
          result: { ok: true, data: { content: 'webui/src/lib/chatState.js' } },
        }),
        reasoningItem('Read the selected file.', { id: 'thinking-read' }),
        outputItem('I found the timeline helper; now I will read it.', {
          id: 'commentary',
        }),
        toolItem({
          id: 'call-read',
          name: 'read',
          arguments: { path: 'webui/src/lib/chatState.js' },
          result: { ok: true, data: { content: 'timeline code' } },
        }),
        reasoningItem('Summarize the result.', { id: 'thinking-final' }),
        outputItem('The timeline is in chatState.js.'),
      ],
      {
        id: 'history-run-assistant-glob',
        source: 'history',
        status: 'completed',
      },
    ),
  ];
}
