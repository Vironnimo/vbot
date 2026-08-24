// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';
import { TOOLTIP_SHOW_DELAY_MS } from '../../../lib/tooltip.js';

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

    expect(argsRow.querySelector('.teb-field-key').textContent).toBe('command');
    expect(argsRow.querySelector('.teb-field-value').textContent).toBe(
      'cd C:\\Development\\projects\\vBot; npm install',
    );
  });

  it('adds the Rail only inside the disclosure body', () => {
    const item = createAssistantRunItem({
      items: [createBashToolChild({ status: 'success', includeResult: true })],
    });
    mountedComponent = mountRun({ item });

    const details = document.querySelector('.tool-event');
    const summary = details.querySelector('.tool-event-line');
    const summaryMarkup = summary.innerHTML;
    const body = details.querySelector('.tool-event-body');

    expect(body.classList.contains('tool-event-details')).toBe(true);
    expect(body.querySelectorAll('.teb-section')).toHaveLength(2);
    expect(body.querySelector('.teb-section .teb-field-key').textContent).toBe(
      'command',
    );

    summary.click();
    flushSync();

    expect(details.open).toBe(true);
    expect(summary.innerHTML).toBe(summaryMarkup);
    expect(summary.textContent).not.toContain('Args');
    expect(summary.textContent).not.toContain('Result');
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

    expect(writeText).toHaveBeenCalledWith('path: safe.txt');
    expect(writeText.mock.calls[0][0]).not.toContain('hidden file body');
  });
});

describe('ChatAssistantRun compact Working blocks', () => {
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

  it('groups contiguous Thinking and Tool rows behind a completed disclosure', () => {
    const item = createAssistantRunItem({
      items: [
        {
          type: 'reasoning',
          id: 'reasoning-first',
          content: 'Inspect the repository.',
          streaming: false,
        },
        createReadToolChild({ status: 'success' }),
      ],
    });
    mountedComponent = mountRun({ item, chatWorkingMode: 'compact' });

    const block = document.querySelector('.working-block');
    expect(block).toBeTruthy();
    expect(block.open).toBe(false);
    expect(
      block.querySelector('.working-block__label').textContent.trim(),
    ).toBe('done working');
    expect(block.querySelector('.working-block__activity')).toBeNull();
    const summaryText = block.querySelector('summary').textContent;
    expect(summaryText.trim()).toBe('done working');
    expect(summaryText).not.toContain('read');
    expect(summaryText).not.toContain('README.md');
    expect(block.querySelector('.working-block__dot')).toBeNull();
    expect(block.querySelector('.working-block__time')).toBeNull();
    expect(block.querySelectorAll('.reasoning-block')).toHaveLength(1);
    expect(block.querySelectorAll('.tool-event')).toHaveLength(1);
  });

  it('labels the latest grouped activity as working while the Run is active', () => {
    const item = createAssistantRunItem({
      status: 'running',
      items: [
        {
          type: 'reasoning',
          id: 'reasoning-active',
          content: 'Inspect the repository.',
          streaming: false,
        },
        createBashToolChild({ status: 'running' }),
      ],
    });
    mountedComponent = mountRun({ item, chatWorkingMode: 'compact' });

    const block = document.querySelector('.working-block');
    expect(
      block.querySelector('.working-block__label').textContent.trim(),
    ).toBe('working...');
    expect(
      block.querySelector('.working-block__activity').textContent.trim(),
    ).toBe('bash');
  });

  it('keeps the latest Tool name while later Thinking is active', () => {
    const item = createAssistantRunItem({
      status: 'running',
      items: [
        createBashToolChild({ status: 'success' }),
        {
          type: 'reasoning',
          id: 'reasoning-after-tool',
          content: 'Interpret the result.',
          streaming: true,
        },
      ],
    });
    mountedComponent = mountRun({ item, chatWorkingMode: 'compact' });

    const block = document.querySelector('.working-block');
    expect(
      block.querySelector('.working-block__activity').textContent.trim(),
    ).toBe('bash');
    expect(block.querySelector('summary').textContent).not.toContain(
      'Thinking',
    );
  });

  it('shows no activity text for an active Thinking-only group', () => {
    const item = createAssistantRunItem({
      status: 'running',
      items: [
        {
          type: 'reasoning',
          id: 'reasoning-only-first',
          content: 'First thought.',
          streaming: false,
        },
        {
          type: 'reasoning',
          id: 'reasoning-only-second',
          content: 'Second thought.',
          streaming: true,
        },
      ],
    });
    mountedComponent = mountRun({ item, chatWorkingMode: 'compact' });

    const block = document.querySelector('.working-block');
    expect(block.querySelector('.working-block__activity')).toBeNull();
    expect(block.querySelector('summary').textContent.trim()).toBe(
      'working...',
    );
  });

  it('ends a Working block at visible Assistant output and starts a new one', () => {
    const item = createAssistantRunItem({
      items: [
        {
          type: 'reasoning',
          id: 'reasoning-before',
          content: 'First pass.',
          streaming: false,
        },
        createReadToolChild({ id: 'tool-before', status: 'success' }),
        {
          type: 'assistant_output',
          id: 'assistant-middle',
          content: 'I found the responsible module.',
          streaming: false,
        },
        createBashToolChild({
          id: 'tool-after',
          toolCallId: 'call-after',
          status: 'success',
          includeResult: true,
        }),
        createReadToolChild({
          id: 'tool-after-read',
          toolCallId: 'call-after-read',
          status: 'success',
        }),
      ],
    });
    mountedComponent = mountRun({ item, chatWorkingMode: 'compact' });

    const blocks = document.querySelectorAll('.working-block');
    expect(blocks).toHaveLength(2);
    expect(document.querySelector('.msg-markdown').textContent).toContain(
      'I found the responsible module.',
    );
    expect(blocks[0].compareDocumentPosition(blocks[1])).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it('keeps a single Thinking row inline in compact mode', () => {
    const item = createAssistantRunItem({
      items: [
        {
          type: 'reasoning',
          id: 'reasoning-single',
          content: 'Inspect.',
          streaming: false,
        },
      ],
    });
    mountedComponent = mountRun({ item, chatWorkingMode: 'compact' });

    expect(document.querySelector('.working-block')).toBeNull();
    expect(document.querySelector('.reasoning-block')).toBeTruthy();
  });

  it('keeps a single Tool row inline in compact mode', () => {
    const item = createAssistantRunItem({
      items: [createReadToolChild({ status: 'success' })],
    });
    mountedComponent = mountRun({ item, chatWorkingMode: 'compact' });

    expect(document.querySelector('.working-block')).toBeNull();
    expect(document.querySelector('.tool-event')).toBeTruthy();
  });

  it('keeps the normal mode inline without a Working wrapper', () => {
    const item = createAssistantRunItem({
      items: [
        {
          type: 'reasoning',
          id: 'reasoning-normal',
          content: 'Inspect.',
          streaming: false,
        },
        createReadToolChild({ status: 'success' }),
      ],
    });
    mountedComponent = mountRun({ item, chatWorkingMode: 'normal' });

    expect(document.querySelector('.working-block')).toBeNull();
    expect(document.querySelector('.reasoning-block')).toBeTruthy();
    expect(document.querySelector('.tool-event')).toBeTruthy();
  });
});

describe('ChatAssistantRun run footer', () => {
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

  function createEditToolChild({
    path = 'a.txt',
    added = 3,
    removed = 2,
  } = {}) {
    return {
      type: 'tool_call',
      id: `tool-edit-${path}`,
      name: 'edit',
      toolCallId: `call-edit-${path}`,
      status: 'success',
      arguments: { path },
      startedEvent: {
        type: 'tool_call_started',
        payload: { tool_call: { id: `call-edit-${path}`, name: 'edit' } },
      },
      resultEvent: {
        type: 'tool_call_result',
        payload: {
          tool_call: { id: `call-edit-${path}`, name: 'edit' },
          display: {
            version: 1,
            summary: path,
            hidden_argument_keys: [],
            primary: [],
            facts: [
              { kind: 'line_change', change: 'added', value: added },
              { kind: 'line_change', change: 'removed', value: removed },
            ],
          },
        },
      },
    };
  }

  it('renders status, duration, and change stats under a completed run', () => {
    const item = createAssistantRunItem({
      status: 'completed',
      items: [createEditToolChild()],
    });
    item.durationMs = 8000;
    mountedComponent = mountRun({ item });

    const footer = document.querySelector('.run-footer');
    expect(footer).toBeTruthy();
    expect(footer.getAttribute('aria-label')).toBe(
      'Completed · 8.0s · 1 file changed, +3 -2',
    );
    const parts = [...footer.querySelectorAll('.run-footer__part')];
    expect(parts.map((part) => part.textContent)).toEqual([
      'Completed',
      '8.0s',
      '1 file changed,',
      '+3',
      '-2',
    ]);
    expect(footer.querySelector('.run-footer__part--added').textContent).toBe(
      '+3',
    );
    expect(footer.querySelector('.run-footer__part--removed').textContent).toBe(
      '-2',
    );
    expect(footer.querySelectorAll('.run-footer__sep')).toHaveLength(2);
    // The change block is one contiguous unit: file label, +N, and -N live
    // inside a single wrapper so they never wrap apart from each other.
    const changeBlock = footer.querySelector('.run-footer__changes');
    expect(changeBlock).toBeTruthy();
    expect(
      [...changeBlock.querySelectorAll('.run-footer__part')].map(
        (part) => part.textContent,
      ),
    ).toEqual(['1 file changed,', '+3', '-2']);
  });

  it('shows the changed files in a hover tooltip on the change block', async () => {
    vi.useFakeTimers();
    const item = createAssistantRunItem({
      status: 'completed',
      items: [
        createEditToolChild({ path: 'a.txt' }),
        createEditToolChild({ path: 'b.txt' }),
      ],
    });
    item.durationMs = 8000;
    mountedComponent = mountRun({ item });

    const changeBlock = document.querySelector('.run-footer__changes');
    expect(changeBlock).toBeTruthy();
    changeBlock.dispatchEvent(new Event('pointerenter'));
    await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
    flushSync();

    expect(document.getElementById('app-tooltip')?.textContent).toBe(
      'a.txt\nb.txt',
    );
    changeBlock.dispatchEvent(new Event('pointerleave'));
    vi.useRealTimers();
  });

  it('ticks the live duration while the run is running', () => {
    const item = createAssistantRunItem({
      status: 'running',
      startTimestamp: '2026-08-05T18:00:00.000Z',
      items: [],
    });
    mountedComponent = mountRun({
      item,
      nowMs: Date.parse('2026-08-05T18:00:05.250Z'),
    });

    const footer = document.querySelector('.run-footer');
    expect(footer).toBeTruthy();
    expect(footer.getAttribute('aria-label')).toBe('Running · 5.3s');
  });

  it('shows only the status when the run has no duration and no changes', () => {
    const item = createAssistantRunItem({ items: [] });
    mountedComponent = mountRun({ item });

    const footer = document.querySelector('.run-footer');
    expect(footer).toBeTruthy();
    expect(footer.getAttribute('aria-label')).toBe('Running');
    expect(footer.querySelectorAll('.run-footer__part')).toHaveLength(1);
    expect(footer.querySelectorAll('.run-footer__sep')).toHaveLength(0);
  });

  it('shows the iteration count in the footer', () => {
    const item = createAssistantRunItem({
      status: 'completed',
      items: [],
    });
    item.durationMs = 8000;
    item.iterationCount = 3;
    mountedComponent = mountRun({ item });

    const footer = document.querySelector('.run-footer');
    expect(footer.getAttribute('aria-label')).toBe('Completed · 8.0s · 3 iter');
  });

  it('renders the provider liveness notice on its own line below the footer', () => {
    const item = createAssistantRunItem({
      status: 'running',
      items: [],
    });
    item.providerHeartbeat = { idleSeconds: 75.4 };
    mountedComponent = mountRun({ item });

    const notice = document.querySelector('.run-footer__notice');
    expect(notice).toBeTruthy();
    expect(notice.textContent).toBe(
      'Provider connected · waiting 75s for the next model chunk',
    );
    // The stable footer line itself carries no notice part.
    const footer = document.querySelector('.run-footer');
    expect(footer.textContent).not.toContain('Provider connected');
  });

  it('renders no notice line without a provider heartbeat', () => {
    const item = createAssistantRunItem({
      status: 'running',
      items: [],
    });
    mountedComponent = mountRun({ item });

    expect(document.querySelector('.run-footer__notice')).toBeNull();
  });
});

async function flushAsync() {
  for (let index = 0; index < 5; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}
