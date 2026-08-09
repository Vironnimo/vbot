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
    expect(button.textContent.trim()).toBe('');
    expect(button.querySelector('svg')).toBeTruthy();
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
    expect(button.textContent.trim()).toBe('');
    expect(button.querySelector('svg')).toBeTruthy();
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

describe('ChatAssistantRun tool dot state', () => {
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

  function createWriteToolChild({ status = 'running' } = {}) {
    const preparing = status === 'preparing';
    return {
      type: 'tool_call',
      id: `tool-write-${status}`,
      name: 'write',
      toolCallId: `call-write-${status}`,
      status,
      // A preparing row only exists from streamed deltas: it renders via the
      // `streaming` flag and carries no `tool_call_started` yet.
      streaming: preparing,
      arguments: preparing ? undefined : { path: 'f.txt' },
      previewArguments: preparing ? { path: 'f.txt' } : null,
      startedEvent: preparing
        ? null
        : {
            type: 'tool_call_started',
            payload: {
              tool_call: { id: `call-write-${status}`, name: 'write' },
            },
          },
    };
  }

  function toolDot() {
    return document.querySelector('.tool-event-line .te-dot');
  }

  it('renders a streamed (preparing) call as a hollow dot, not the running dot', () => {
    const item = createAssistantRunItem({
      items: [createWriteToolChild({ status: 'preparing' })],
    });
    mountedComponent = mountRun({ item });

    const dot = toolDot();
    expect(dot).toBeTruthy();
    expect(dot.classList.contains('preparing')).toBe(true);
    expect(dot.classList.contains('running')).toBe(false);
  });

  it('renders a dispatched (running) call as the running dot, not preparing', () => {
    const item = createAssistantRunItem({
      items: [createWriteToolChild({ status: 'running' })],
    });
    mountedComponent = mountRun({ item });

    const dot = toolDot();
    expect(dot).toBeTruthy();
    expect(dot.classList.contains('running')).toBe(true);
    expect(dot.classList.contains('preparing')).toBe(false);
  });

  it('shows recognized preview arguments before the tool call is dispatched', () => {
    const item = createAssistantRunItem({
      items: [
        {
          type: 'tool_call',
          id: 'tool-bash-preparing',
          name: 'bash',
          toolCallId: 'call-bash-preparing',
          status: 'preparing',
          streaming: true,
          arguments: undefined,
          previewArguments: {
            command: 'cd C:\\Development\\projects\\vBot; npm install',
          },
          partialArgumentsText:
            '{"command":"cd C:\\\\Development\\\\projects\\\\vBot; npm install"}',
          startedEvent: null,
        },
      ],
    });
    mountedComponent = mountRun({ item });

    const argsRow = Array.from(document.querySelectorAll('.teb-row')).find(
      (row) => row.querySelector('.teb-label')?.textContent === 'Args',
    );

    expect(argsRow.querySelector('.teb-code').textContent).toBe(
      'command: "cd C:\\\\Development\\\\projects\\\\vBot; npm install"',
    );
  });

  it('renders active Tool primary values without wrapper punctuation', () => {
    const item = createAssistantRunItem({
      items: [
        {
          ...createWriteToolChild({ status: 'running' }),
          display: {
            primary: [
              {
                kind: 'description',
                value: 'Update the toolbar',
                quote: true,
              },
            ],
          },
        },
      ],
    });
    mountedComponent = mountRun({ item });

    const summaryLine = document.querySelector('.tool-event-line');
    expect(summaryLine.querySelector('.te-primary-value').textContent).toBe(
      'Update the toolbar',
    );
    expect(summaryLine.querySelector('.te-arg-mark')).toBeNull();
    expect(summaryLine.querySelector('.te-primary-quote')).toBeNull();
  });
});

describe('ChatAssistantRun copy actions', () => {
  let mountedComponent;
  let writeText;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    vi.restoreAllMocks();
  });

  it('copies every assistant Markdown section without reasoning or tools', async () => {
    const item = createAssistantRunItem({
      items: [
        {
          type: 'assistant_output',
          id: 'answer-1',
          content: '# First section',
          streaming: false,
        },
        {
          type: 'reasoning',
          id: 'reasoning-1',
          content: '**Private reasoning**',
          streaming: false,
        },
        createReadToolChild({ status: 'success' }),
        {
          type: 'assistant_output',
          id: 'answer-2',
          content: '**Final section**',
          streaming: false,
        },
      ],
    });
    mountedComponent = mountRun({ item });

    document.querySelector('.message-copy').click();
    await flushAsync();

    expect(writeText).toHaveBeenCalledWith(
      '# First section\n\n**Final section**',
    );
    expect(writeText.mock.calls[0][0]).not.toContain('Private reasoning');
    expect(writeText.mock.calls[0][0]).not.toContain('README.md');
  });

  it('copies thinking through its independent action', async () => {
    const item = createAssistantRunItem({
      items: [
        {
          type: 'reasoning',
          id: 'reasoning-copy',
          content: '**Plan**\n\n<!-- -->\n\nInspect the state.',
          streaming: false,
        },
      ],
    });
    mountedComponent = mountRun({ item });

    document.querySelector('.reasoning-copy').click();
    await flushAsync();

    expect(writeText).toHaveBeenCalledOnce();
    expect(writeText.mock.calls[0][0]).toContain('**Plan**');
    expect(writeText.mock.calls[0][0]).toContain('Inspect the state.');
    expect(writeText.mock.calls[0][0]).not.toContain('<!--');
    expect(document.querySelector('.message-copy')).toBeNull();
  });

  it('copies only the sanitized Tool argument display', async () => {
    const item = createAssistantRunItem({
      items: [
        {
          type: 'tool_call',
          id: 'write-copy',
          name: 'write',
          toolCallId: 'call-write-copy',
          status: 'success',
          arguments: {
            path: 'safe.txt',
            content: 'hidden file body',
          },
          startedEvent: {
            type: 'tool_call_started',
            payload: {
              tool_call: { id: 'call-write-copy', name: 'write' },
            },
          },
        },
      ],
    });
    mountedComponent = mountRun({ item });

    const argsRow = Array.from(document.querySelectorAll('.teb-row')).find(
      (row) => row.querySelector('.teb-label')?.textContent === 'Args',
    );
    argsRow.querySelector('.tool-detail-copy').click();
    await flushAsync();

    expect(writeText).toHaveBeenCalledWith('path: "safe.txt"');
    expect(writeText.mock.calls[0][0]).not.toContain('hidden file body');
  });
});

async function flushAsync() {
  for (let index = 0; index < 5; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}
