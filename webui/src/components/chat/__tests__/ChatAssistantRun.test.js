// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  unmount,
  createAssistantRunItem,
  createBashToolChild,
  createReadToolChild,
  createSubAgentChild,
  mountRun,
  findRowCancel,
  flushAsync,
} from './ChatAssistantRun.support.js';
import { init } from '../../../lib/i18n.js';

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

  it.each([true, false])(
    'shows the background action only with server capability: %s',
    (available) => {
      const onBackgroundToolCall = vi.fn();
      mountedComponent = mountRun({
        item: createAssistantRunItem({
          runId: 'run-parent-1',
          items: [
            createBashToolChild({
              toolCallId: 'call-bash-2',
              status: 'running',
            }),
          ],
        }),
        backgroundToolCallIds: available ? ['call-bash-2'] : [],
        onBackgroundToolCall,
      });
      const button = document.querySelector(
        '[aria-label="Move to background"]',
      );
      expect(Boolean(button)).toBe(available);
      if (available) {
        const details = button.closest('details');
        const wasOpen = details.open;
        button.click();
        flushSync();
        expect(details.open).toBe(wasOpen);
        expect(onBackgroundToolCall).toHaveBeenCalledWith({
          runId: 'run-parent-1',
          toolCallId: 'call-bash-2',
        });
      }
    },
  );

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

  it('renders a partial multi-edit as a steady amber partial state', () => {
    const item = createAssistantRunItem({
      items: [createWriteToolChild({ status: 'partial' })],
    });
    mountedComponent = mountRun({ item });

    const dot = toolDot();
    expect(dot).toBeTruthy();
    expect(dot.classList.contains('partial')).toBe(true);
    expect(dot.classList.contains('running')).toBe(false);
    expect(document.querySelector('.te-time.partial')?.textContent).toContain(
      'partial',
    );
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
