const { unmount, flushSync, mount } = await import('svelte');
// @vitest-environment jsdom
import { vi } from 'vitest';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatAssistantRun } =
  await import('../ChatAssistantRun.svelte');

function createAssistantRunItem({
  runId = 'run-parent',
  startTimestamp = '2026-06-09T12:00:00+00:00',
  status,
  items = [],
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
  };
}

function createBashToolChild({
  id = 'tool-bash-1',
  toolCallId = 'call-bash-1',
  status = 'running',
  includeResult = false,
} = {}) {
  const tool = {
    type: 'tool_call',
    id,
    name: 'bash',
    toolCallId,
    status,
    arguments: { command: 'ls -la' },
    startedEvent: {
      type: 'tool_call_started',
      payload: { tool_call: { id: toolCallId, name: 'bash' } },
    },
  };
  if (includeResult) {
    tool.resultEvent = {
      type: 'tool_call_result',
      payload: {
        tool_call: { id: toolCallId, name: 'bash' },
        result: { ok: true, data: { output: 'file.txt' }, artifacts: [] },
      },
    };
  }
  return tool;
}

function createReadToolChild({
  id = 'tool-read-1',
  toolCallId = 'call-read-1',
  status = 'running',
} = {}) {
  return {
    type: 'tool_call',
    id,
    name: 'read',
    toolCallId,
    status,
    arguments: { path: 'README.md' },
    startedEvent: {
      type: 'tool_call_started',
      payload: { tool_call: { id: toolCallId, name: 'read' } },
    },
  };
}

function createSubAgentChild({
  id = 'tool-subagent-1',
  toolCallId = 'call-subagent-1',
  status = 'running',
  dataRunId = 'run-child',
  dataStatus = 'running',
  queueItemId = '',
} = {}) {
  return {
    type: 'tool_call',
    id,
    name: 'subagent',
    toolCallId,
    status,
    arguments: { action: 'run', agent_id: 'worker', content: 'Inspect' },
    startedEvent: {
      type: 'tool_call_started',
      payload: { tool_call: { id: toolCallId, name: 'subagent' } },
    },
    subAgentSession: {
      id: 'sub_child',
      agent_id: 'worker',
      session_id: 'session-child',
      run_id: dataRunId,
      status: dataStatus,
      delivery: 'automatic',
      ...(queueItemId ? { queue_item_id: queueItemId } : {}),
    },
    result: queueItemId
      ? {
          ok: true,
          data: {
            id: 'sub_child',
            agent_id: 'worker',
            session_id: 'session-child',
            status: dataStatus,
            delivery: 'automatic',
          },
          artifacts: [],
        }
      : {
          ok: true,
          data: {
            id: 'sub_child',
            agent_id: 'worker',
            session_id: 'session-child',
            status: dataStatus,
            delivery: 'automatic',
          },
          artifacts: [],
        },
  };
}

function mountRun(props) {
  const target = document.body;
  const component = mount(ChatAssistantRun, { target, props });
  flushSync();
  return component;
}

function findRowCancel(kind) {
  return Array.from(document.querySelectorAll('.row-cancel')).find(
    (button) => button.getAttribute('data-cancel') === kind,
  );
}

async function flushAsync() {
  for (let index = 0; index < 5; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}

export {
  createAssistantRunItem,
  createBashToolChild,
  createReadToolChild,
  createSubAgentChild,
  mountRun,
  findRowCancel,
  flushAsync,
};

export { flushSync, unmount };
