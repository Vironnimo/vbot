// @vitest-environment jsdom
import { afterEach, beforeEach, vi } from 'vitest';
import { flushSync as svelteFlushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatAssistantRun } =
  await import('../ChatAssistantRun.svelte');

// Registers the per-test lifecycle and returns `mount(props)`, which mounts
// ChatAssistantRun into the body.
export function setupChatAssistantRunSuite() {
  let mountedComponent = null;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    vi.useRealTimers();
  });

  return {
    mount(props) {
      mountedComponent = mount(ChatAssistantRun, {
        target: document.body,
        props,
      });
      flushSync();
      return mountedComponent;
    },
  };
}

export function flushSync() {
  return svelteFlushSync();
}

export async function flushAsync() {
  for (let index = 0; index < 5; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}

export function assistantRun({
  runId = 'run-parent',
  startTimestamp = '2026-06-09T12:00:00+00:00',
  status,
  items = [],
  ...fields
} = {}) {
  return {
    type: 'assistant_run',
    id: `run-${runId}`,
    runId,
    agentId: 'alpha',
    sessionId: 'session-1',
    startTimestamp,
    ...(status ? { status } : {}),
    items,
    ...fields,
  };
}

// A Tool child row; `startedEvent` marks it dispatched.
export function toolChild(
  name,
  args,
  { id, status = 'running', ...fields } = {},
) {
  const toolCallId = `call-${id ?? name}`;
  return {
    type: 'tool_call',
    id: `tool-${id ?? name}`,
    name,
    toolCallId,
    status,
    arguments: args,
    startedEvent: {
      type: 'tool_call_started',
      payload: { tool_call: { id: toolCallId, name } },
    },
    ...fields,
  };
}

export function bashChild({
  status = 'running',
  withResult = false,
  ...fields
} = {}) {
  const child = toolChild('bash', { command: 'ls -la' }, { status, ...fields });
  if (withResult) {
    child.resultEvent = {
      type: 'tool_call_result',
      payload: {
        tool_call: { id: child.toolCallId, name: 'bash' },
        result: { ok: true, data: { output: 'file.txt' }, artifacts: [] },
      },
    };
  }
  return child;
}

export function readChild({ status = 'running', ...fields } = {}) {
  return toolChild('read', { path: 'README.md' }, { status, ...fields });
}

export function subAgentChild({
  status = 'running',
  runStatus = 'running',
} = {}) {
  const data = {
    id: 'sub_child',
    agent_id: 'worker',
    session_id: 'session-child',
    status: runStatus,
    delivery: 'automatic',
  };
  return toolChild(
    'subagent',
    { action: 'run', agent_id: 'worker', content: 'Inspect' },
    {
      status,
      subAgentSession: { ...data, run_id: 'run-child' },
      result: { ok: true, data, artifacts: [] },
    },
  );
}

export function reasoningItem(
  content,
  { id = 'reasoning', streaming = false } = {},
) {
  return { type: 'reasoning', id, content, streaming };
}

export function outputItem(content, { id = 'answer' } = {}) {
  return { type: 'assistant_output', id, content, streaming: false };
}
