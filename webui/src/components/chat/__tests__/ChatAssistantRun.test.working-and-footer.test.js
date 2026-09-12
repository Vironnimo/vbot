// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  unmount,
  createAssistantRunItem,
  createBashToolChild,
  createReadToolChild,
  mountRun,
} from './ChatAssistantRun.support.js';
import { init } from '../../../lib/i18n.js';
import { TOOLTIP_SHOW_DELAY_MS } from '../../../lib/tooltip.js';

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

  it('renders no notice line while the provider streams with a tiny idle time', () => {
    const item = createAssistantRunItem({
      status: 'running',
      items: [],
    });
    item.providerHeartbeat = { idleSeconds: 0.3 };
    mountedComponent = mountRun({ item });

    expect(document.querySelector('.run-footer__notice')).toBeNull();
  });
});

describe('ChatAssistantRun handed-off background Bash rows', () => {
  let mountedComponent;

  function createHandedOffBashChild() {
    return {
      type: 'tool_call',
      id: 'tool-bash-bg',
      name: 'bash',
      toolCallId: 'call-bash-bg',
      status: 'success',
      arguments: { command: 'npm run dev', mode: 'background' },
      timing: {
        started_at: '2026-09-04T12:00:00+00:00',
        completed_at: '2026-09-04T12:00:01+00:00',
        duration_ms: 1000,
      },
      startedEvent: {
        type: 'tool_call_started',
        payload: { tool_call: { id: 'call-bash-bg', name: 'bash' } },
      },
      result: {
        ok: true,
        error: null,
        data: {
          status: 'running',
          process_id: 'process-one',
          mode: 'background',
          delivery: 'automatic',
          output: 'VITE ready in 830ms',
          handoff_note:
            'The command is still running and has been handed off to vBot.',
        },
        artifacts: [],
      },
    };
  }

  const terminalProcess = {
    status: 'completed',
    exitCode: 0,
    cancelledByUser: false,
    startedAt: '2026-09-04T12:00:00+00:00',
    finishedAt: '2026-09-04T12:04:12+00:00',
    output: 'build finished cleanly',
    truncated: false,
    logFile: 'C:/logs/bash/process-one.log',
  };

  function toolDot() {
    return document.querySelector('.tool-event-line .te-dot');
  }

  function timeLabel() {
    return (
      document.querySelector('.tool-event-line .te-time')?.textContent ?? ''
    );
  }

  function resultRows() {
    return Array.from(document.querySelectorAll('.teb-row')).filter(
      (row) => row.querySelector('.teb-label')?.textContent === 'Result',
    );
  }

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

  it('keeps a handed-off row running and ticking while the process runs', () => {
    const item = createAssistantRunItem({
      items: [createHandedOffBashChild()],
    });
    mountedComponent = mountRun({
      item,
      nowMs: Date.parse('2026-09-04T12:30:00Z'),
    });

    expect(toolDot().classList.contains('running')).toBe(true);
    expect(toolDot().classList.contains('done')).toBe(false);
    expect(timeLabel()).toBe('30m 0s');
  });

  it('settles the row with the real runtime and the actual result once terminal data arrives', () => {
    const item = createAssistantRunItem({
      items: [createHandedOffBashChild()],
    });
    mountedComponent = mountRun({
      item,
      backgroundBashProcesses: { 'process-one': terminalProcess },
      nowMs: Date.parse('2026-09-04T12:30:00Z'),
    });

    expect(toolDot().classList.contains('done')).toBe(true);
    expect(timeLabel()).toBe('4m 12s');
    const resultText = resultRows()
      .map((row) => row.textContent)
      .join('\n');
    expect(resultText).toContain('build finished cleanly');
    expect(resultText).not.toContain('handed off to vBot');
  });

  it('settles the dot from durable history without inventing a runtime', () => {
    const item = createAssistantRunItem({
      items: [createHandedOffBashChild()],
    });
    mountedComponent = mountRun({
      item,
      backgroundBashStatuses: { 'process-one': 'completed' },
      nowMs: Date.parse('2026-09-04T12:30:00Z'),
    });

    expect(toolDot().classList.contains('done')).toBe(true);
    expect(timeLabel()).toBe('');
  });
});
