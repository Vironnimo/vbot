import { flushSync, tick } from 'svelte';

import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
} from '../../lib/chatState.js';

export function scrollMemorySessions() {
  const chatState = createChatState();
  const parentSession = ensureSessionState(
    chatState,
    'alpha',
    'session-scroll-memory-parent',
  );
  parentSession.messages = [
    {
      id: 'parent-user-one',
      role: 'user',
      content: 'Parent question',
      timestamp: '2026-05-10T09:00:00',
    },
    {
      id: 'parent-assistant-one',
      role: 'assistant',
      content: 'Parent answer',
      timestamp: '2026-05-10T09:01:00',
    },
  ];
  const childSession = ensureSessionState(
    chatState,
    'subagent',
    'session-scroll-memory-child',
  );
  childSession.messages = [
    {
      id: 'child-user-one',
      role: 'user',
      content: 'Child task',
      timestamp: '2026-05-10T09:02:00',
    },
  ];
  return { parentSession, childSession };
}

// jsdom has no layout: pin the container to a 2000px-tall content area in a
// 500px viewport and route scrollTo through the same writable scrollTop.
export function mockScrollGeometry(container) {
  let scrollTop = 0;
  let scrollHeight = 2000;
  Object.defineProperty(container, 'scrollHeight', {
    configurable: true,
    get: () => scrollHeight,
  });
  Object.defineProperty(container, 'offsetHeight', {
    configurable: true,
    get: () => 500,
  });
  Object.defineProperty(container, 'clientHeight', {
    configurable: true,
    get: () => 500,
  });
  Object.defineProperty(container, 'scrollTop', {
    configurable: true,
    get: () => scrollTop,
    set: (value) => {
      scrollTop = value;
    },
  });
  container.scrollTo = (x, y) => {
    scrollTop = typeof x === 'object' ? x.top : y;
  };
  return {
    setScrollTop: (value) => {
      scrollTop = value;
    },
    setScrollHeight: (value) => {
      scrollHeight = value;
    },
    currentScrollTop: () => scrollTop,
  };
}

export async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await tick();
    await Promise.resolve();
    flushSync();

    if (check()) {
      return;
    }
    if (typeof requestAnimationFrame === 'function') {
      await new Promise((resolve) => requestAnimationFrame(resolve));
      flushSync();
      if (check()) {
        return;
      }
    }
  }

  throw new Error('Timed out waiting for condition.');
}

export function reportedMultiStepMessages() {
  return [
    {
      id: 'user-reported',
      role: 'user',
      content: 'Investigate the duplicated chat UI.',
    },
    {
      id: 'assistant-glob',
      role: 'assistant',
      reasoning: 'Find candidate files.',
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
    },
    {
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
    {
      id: 'tool-read',
      role: 'tool',
      tool_call_id: 'call-read',
      name: 'read',
      content: '{"ok":true,"data":{"content":"timeline code"}}',
    },
    {
      id: 'assistant-final',
      role: 'assistant',
      content: 'The timeline is in chatState.js.',
      reasoning: 'Summarize the result.',
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
      result: { ok: true, data: { content: 'webui/src/lib/chatState.js' } },
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
