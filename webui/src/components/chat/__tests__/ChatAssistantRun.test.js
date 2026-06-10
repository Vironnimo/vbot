// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatAssistantRun } =
  await import('../ChatAssistantRun.svelte');

function createAssistantRunItem({
  runId = 'run-parent',
  startTimestamp = '2026-06-09T12:00:00+00:00',
  items = [],
} = {}) {
  return {
    type: 'assistant_run',
    id: `run-${runId}`,
    runId,
    agentId: 'alpha',
    sessionId: 'session-1',
    startTimestamp,
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
    arguments: { agent_id: 'worker', content: 'Inspect' },
    startedEvent: {
      type: 'tool_call_started',
      payload: { tool_call: { id: toolCallId, name: 'subagent' } },
    },
    subAgentSession: {
      agent_id: 'worker',
      session_id: 'session-child',
      run_id: dataRunId,
      status: dataStatus,
    },
    result: queueItemId
      ? {
          ok: true,
          data: {
            agent_id: 'worker',
            session_id: 'session-child',
            run_id: dataRunId,
            status: dataStatus,
            queue_item_id: queueItemId,
          },
          artifacts: [],
        }
      : {
          ok: true,
          data: {
            agent_id: 'worker',
            session_id: 'session-child',
            run_id: dataRunId,
            status: dataStatus,
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

describe('ChatAssistantRun cancel buttons', () => {
  let mountedComponent;

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
  });

  it('renders a cancel button on a running bash row', () => {
    const onCancelToolCall = vi.fn();
    const item = createAssistantRunItem({
      items: [createBashToolChild({ status: 'running' })],
    });
    mountedComponent = mountRun({
      item,
      onCancelToolCall,
    });

    const button = findRowCancel('tool');
    expect(button).toBeTruthy();
    expect(button.textContent.trim()).toBe('Cancel');
    expect(button.getAttribute('aria-label')).toBe('Cancel running tool call');
  });

  it('does not render a cancel button on a completed bash row', () => {
    const onCancelToolCall = vi.fn();
    const item = createAssistantRunItem({
      items: [createBashToolChild({ status: 'success', includeResult: true })],
    });
    mountedComponent = mountRun({
      item,
      onCancelToolCall,
    });

    expect(findRowCancel('tool')).toBeFalsy();
  });

  it('does not render a cancel button on a non-bash tool row', () => {
    const onCancelToolCall = vi.fn();
    const item = createAssistantRunItem({
      items: [createReadToolChild({ status: 'running' })],
    });
    mountedComponent = mountRun({
      item,
      onCancelToolCall,
    });

    expect(findRowCancel('tool')).toBeFalsy();
  });

  it('renders a cancel button on a running sub-agent row', () => {
    const onCancelSubAgent = vi.fn();
    const item = createAssistantRunItem({
      items: [
        createSubAgentChild({ status: 'running', dataStatus: 'running' }),
      ],
    });
    mountedComponent = mountRun({
      item,
      onCancelSubAgent,
    });

    const button = findRowCancel('subagent');
    expect(button).toBeTruthy();
    expect(button.textContent.trim()).toBe('Cancel');
    expect(button.getAttribute('aria-label')).toBe('Cancel running sub-agent');
  });

  it('does not render a cancel button on a completed sub-agent row', () => {
    const onCancelSubAgent = vi.fn();
    const item = createAssistantRunItem({
      items: [
        createSubAgentChild({ status: 'success', dataStatus: 'completed' }),
      ],
    });
    mountedComponent = mountRun({
      item,
      onCancelSubAgent,
    });

    expect(findRowCancel('subagent')).toBeFalsy();
  });

  it('invokes the bash cancel callback with runId and toolCallId', () => {
    const onCancelToolCall = vi.fn();
    const item = createAssistantRunItem({
      runId: 'run-parent-1',
      items: [
        createBashToolChild({
          id: 'tool-bash-2',
          toolCallId: 'call-bash-2',
          status: 'running',
        }),
      ],
    });
    mountedComponent = mountRun({
      item,
      onCancelToolCall,
    });

    const button = findRowCancel('tool');
    expect(button).toBeTruthy();
    button.click();
    flushSync();

    expect(onCancelToolCall).toHaveBeenCalledWith({
      runId: 'run-parent-1',
      toolCallId: 'call-bash-2',
    });
  });

  it('invokes the sub-agent cancel callback with the child tool', () => {
    const onCancelSubAgent = vi.fn();
    const child = createSubAgentChild({
      id: 'tool-subagent-2',
      toolCallId: 'call-subagent-2',
      status: 'running',
      dataRunId: 'run-child-2',
      dataStatus: 'running',
    });
    const item = createAssistantRunItem({ items: [child] });
    mountedComponent = mountRun({
      item,
      onCancelSubAgent,
    });

    const button = findRowCancel('subagent');
    expect(button).toBeTruthy();
    button.click();
    flushSync();

    expect(onCancelSubAgent).toHaveBeenCalledWith({ tool: child });
  });
});

describe('ChatAssistantRun sub-agent activity preview', () => {
  let mountedComponent;

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
  });

  function previewSpan() {
    return document.querySelector('.subagent-preview');
  }

  it('shows the child run last tool name instead of the prompt while running', () => {
    const item = createAssistantRunItem({
      items: [
        createSubAgentChild({ status: 'running', dataStatus: 'running' }),
      ],
    });
    mountedComponent = mountRun({
      item,
      subAgentStatuses: { 'runTool:run-child': 'bash' },
    });

    const preview = previewSpan();
    expect(preview).toBeTruthy();
    expect(preview.textContent.trim()).toBe('bash');
    expect(preview.classList.contains('subagent-activity')).toBe(true);
  });

  it('keeps the prompt preview while running until the child makes a tool call', () => {
    const item = createAssistantRunItem({
      items: [
        createSubAgentChild({ status: 'running', dataStatus: 'running' }),
      ],
    });
    mountedComponent = mountRun({
      item,
      subAgentStatuses: {},
    });

    const preview = previewSpan();
    expect(preview).toBeTruthy();
    expect(preview.textContent.trim()).toBe('Inspect');
    expect(preview.classList.contains('subagent-activity')).toBe(false);
  });

  it('reverts to the prompt preview once the child run settled, even with a leftover tool entry', () => {
    const item = createAssistantRunItem({
      items: [
        createSubAgentChild({ status: 'success', dataStatus: 'completed' }),
      ],
    });
    mountedComponent = mountRun({
      item,
      subAgentStatuses: { 'runTool:run-child': 'bash' },
    });

    const preview = previewSpan();
    expect(preview).toBeTruthy();
    expect(preview.textContent.trim()).toBe('Inspect');
    expect(preview.classList.contains('subagent-activity')).toBe(false);
  });
});
