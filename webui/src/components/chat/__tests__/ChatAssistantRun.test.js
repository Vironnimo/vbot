// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import {
  assistantRun,
  bashChild,
  flushAsync,
  flushSync,
  outputItem,
  readChild,
  reasoningItem,
  setupChatAssistantRunSuite,
  subAgentChild,
  toolChild,
} from './ChatAssistantRun.support.js';
import { t } from '../../../lib/i18n.js';
import { TOOLTIP_SHOW_DELAY_MS } from '../../../lib/tooltip.js';

function rowCancel(kind) {
  return document.querySelector(`.row-cancel[data-cancel="${kind}"]`);
}

function toolDot() {
  return document.querySelector('.tool-event-line .te-dot');
}

function detailRow(key) {
  return Array.from(document.querySelectorAll('.teb-row')).find(
    (row) => row.querySelector('.teb-label')?.textContent === t(key),
  );
}

function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  });
  return writeText;
}

function editChild(path) {
  return toolChild(
    'edit',
    { path },
    {
      id: `edit-${path}`,
      status: 'success',
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
              { kind: 'line_change', change: 'added', value: 3 },
              { kind: 'line_change', change: 'removed', value: 2 },
            ],
          },
        },
      },
    },
  );
}

describe('ChatAssistantRun', () => {
  const run = setupChatAssistantRunSuite();

  describe('row actions', () => {
    it.each([
      [
        'a running bash row',
        () => bashChild({ id: 'bash-2' }),
        'tool',
        'onCancelToolCall',
        'chat.cancelToolCallAria',
        (child) => ({ runId: 'run-parent', toolCallId: child.toolCallId }),
      ],
      [
        'a running sub-agent row',
        () => subAgentChild(),
        'subagent',
        'onCancelSubAgent',
        'chat.cancelSubAgentAria',
        (child) => ({ tool: child }),
      ],
    ])(
      'cancels %s through an icon button',
      (_case, createChild, kind, callback, labelKey, expectedCall) => {
        const onCancel = vi.fn();
        const child = createChild();
        run.mount({
          item: assistantRun({ items: [child] }),
          [callback]: onCancel,
        });

        const button = rowCancel(kind);
        expect(button.textContent.trim()).toBe('');
        expect(button.querySelector('svg')).toBeTruthy();
        expect(button.getAttribute('aria-label')).toBe(t(labelKey));
        button.click();
        flushSync();

        expect(onCancel).toHaveBeenCalledWith(expectedCall(child));
      },
    );

    it.each([
      [
        'a completed bash row',
        bashChild({ status: 'success', withResult: true }),
      ],
      ['a running non-bash Tool row', readChild()],
      [
        'a completed sub-agent row',
        subAgentChild({ status: 'success', runStatus: 'completed' }),
      ],
    ])('offers no cancel button on %s', (_case, child) => {
      run.mount({
        item: assistantRun({ items: [child] }),
        onCancelToolCall: vi.fn(),
        onCancelSubAgent: vi.fn(),
      });

      expect(document.querySelector('.row-cancel')).toBeNull();
    });

    function backgroundButton() {
      return document.querySelector(
        `[aria-label="${t('chat.moveToBackground')}"]`,
      );
    }

    it('offers no background action without server capability', () => {
      run.mount({
        item: assistantRun({ items: [bashChild({ id: 'bash-2' })] }),
        backgroundToolCallIds: [],
        onBackgroundToolCall: vi.fn(),
      });

      expect(backgroundButton()).toBeNull();
    });

    it('moves a capable Tool call to the background without toggling its row', () => {
      const onBackgroundToolCall = vi.fn();
      const child = bashChild({ id: 'bash-2' });
      run.mount({
        item: assistantRun({ runId: 'run-parent-1', items: [child] }),
        backgroundToolCallIds: [child.toolCallId],
        onBackgroundToolCall,
      });

      const details = backgroundButton().closest('details');
      const wasOpen = details.open;
      backgroundButton().click();
      flushSync();
      expect(details.open).toBe(wasOpen);
      expect(onBackgroundToolCall).toHaveBeenCalledWith({
        runId: 'run-parent-1',
        toolCallId: child.toolCallId,
      });
    });
  });

  it.each([
    [
      "the Child Run's last Tool while running",
      subAgentChild(),
      { 'runTool:run-child': 'bash' },
      ['bash', true],
    ],
    [
      'the prompt until the Child Run makes a Tool call',
      subAgentChild(),
      {},
      ['Inspect', false],
    ],
    [
      'the prompt once the Child Run settled despite a leftover Tool entry',
      subAgentChild({ status: 'success', runStatus: 'completed' }),
      { 'runTool:run-child': 'bash' },
      ['Inspect', false],
    ],
  ])('previews %s on a sub-agent row', (_case, child, statuses, expected) => {
    run.mount({
      item: assistantRun({ items: [child] }),
      subAgentStatuses: statuses,
    });

    const preview = document.querySelector('.subagent-preview');
    expect([
      preview.textContent.trim(),
      preview.classList.contains('subagent-activity'),
    ]).toEqual(expected);
  });

  describe('Tool rows', () => {
    // A preparing row only exists from streamed deltas: it renders through
    // the `streaming` flag and carries no `tool_call_started` yet.
    function writeChild(status) {
      const preparing = status === 'preparing';
      return toolChild('write', preparing ? undefined : { path: 'f.txt' }, {
        status,
        streaming: preparing,
        previewArguments: preparing ? { path: 'f.txt' } : null,
        ...(preparing ? { startedEvent: null } : {}),
      });
    }

    it.each([
      [
        'a streamed (preparing) call as a hollow dot',
        'preparing',
        'running',
        null,
      ],
      ['a dispatched call as the running dot', 'running', 'preparing', null],
      [
        'a partial multi-edit as a steady partial state',
        'partial',
        'running',
        'chat.toolPartial',
      ],
    ])('renders %s', (_case, status, otherState, partialLabelKey) => {
      run.mount({ item: assistantRun({ items: [writeChild(status)] }) });

      expect(toolDot().classList.contains(status)).toBe(true);
      expect(toolDot().classList.contains(otherState)).toBe(false);
      expect(
        document.querySelector('.te-time.partial')?.textContent ?? null,
      ).toBe(partialLabelKey && t(partialLabelKey));
    });

    it('renders a minimal primary value without wrapper punctuation', () => {
      run.mount({
        item: assistantRun({
          items: [
            toolChild(
              'write',
              { path: 'f.txt' },
              {
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
            ),
          ],
        }),
      });

      const summaryLine = document.querySelector('.tool-event-line');
      expect(summaryLine.querySelector('.te-primary-value').textContent).toBe(
        'Update the toolbar',
      );
      expect(
        summaryLine.querySelector('.te-arg-mark, .te-primary-quote'),
      ).toBeNull();
    });

    it('shows recognized preview arguments before the Tool call is dispatched', () => {
      const command = 'cd C:\\Development\\projects\\vBot; npm install';
      run.mount({
        item: assistantRun({
          items: [
            toolChild('bash', undefined, {
              status: 'preparing',
              streaming: true,
              previewArguments: { command },
              partialArgumentsText: JSON.stringify({ command }),
              startedEvent: null,
            }),
          ],
        }),
      });

      const argsRow = detailRow('chat.toolArgs');
      expect(argsRow.querySelector('.teb-field-key').textContent).toBe(
        'command',
      );
      expect(argsRow.querySelector('.teb-field-value').textContent).toBe(
        command,
      );
    });

    it('adds the Rail only inside the disclosure body', () => {
      run.mount({
        item: assistantRun({
          items: [bashChild({ status: 'success', withResult: true })],
        }),
      });

      const details = document.querySelector('.tool-event');
      const summary = details.querySelector('.tool-event-line');
      const summaryMarkup = summary.innerHTML;
      const body = details.querySelector('.tool-event-body');
      expect(body.classList.contains('tool-event-details')).toBe(true);
      expect(body.querySelectorAll('.teb-section')).toHaveLength(2);
      expect(
        body.querySelector('.teb-section .teb-field-key').textContent,
      ).toBe('command');

      summary.click();
      flushSync();

      expect(details.open).toBe(true);
      expect(summary.innerHTML).toBe(summaryMarkup);
      expect(summary.textContent).not.toContain(t('chat.toolArgs'));
      expect(summary.textContent).not.toContain(t('chat.toolResultLabel'));
    });
  });

  describe('copy actions', () => {
    it('copies every Assistant Markdown section without reasoning or Tools', async () => {
      const writeText = stubClipboard();
      run.mount({
        item: assistantRun({
          items: [
            outputItem('# First section', { id: 'answer-1' }),
            reasoningItem('**Private reasoning**'),
            readChild({ status: 'success' }),
            outputItem('**Final section**', { id: 'answer-2' }),
          ],
        }),
      });

      document.querySelector('.message-copy').click();
      await flushAsync();

      expect(writeText).toHaveBeenCalledWith(
        '# First section\n\n**Final section**',
      );
    });

    it('copies Thinking through its independent action', async () => {
      const writeText = stubClipboard();
      run.mount({
        item: assistantRun({
          items: [reasoningItem('**Plan**\n\n<!-- -->\n\nInspect the state.')],
        }),
      });

      document.querySelector('.reasoning-copy').click();
      await flushAsync();

      expect(writeText).toHaveBeenCalledOnce();
      const copied = writeText.mock.calls[0][0];
      expect(copied).toContain('**Plan**');
      expect(copied).toContain('Inspect the state.');
      expect(copied).not.toContain('<!--');
      expect(document.querySelector('.message-copy')).toBeNull();
    });

    it('copies only the sanitized Tool argument display', async () => {
      const writeText = stubClipboard();
      run.mount({
        item: assistantRun({
          items: [
            toolChild(
              'write',
              { path: 'safe.txt', content: 'hidden file body' },
              { status: 'success' },
            ),
          ],
        }),
      });

      detailRow('chat.toolArgs').querySelector('.tool-detail-copy').click();
      await flushAsync();

      expect(writeText).toHaveBeenCalledWith('path: safe.txt');
    });
  });

  describe('compact Working blocks', () => {
    function workingBlock() {
      return document.querySelector('.working-block');
    }

    it('groups contiguous Thinking and Tool rows behind a completed disclosure', () => {
      run.mount({
        item: assistantRun({
          items: [
            reasoningItem('Inspect the repository.'),
            readChild({ status: 'success' }),
          ],
        }),
        chatWorkingMode: 'compact',
      });

      const block = workingBlock();
      expect(block.open).toBe(false);
      expect(block.querySelector('summary').textContent.trim()).toBe(
        t('chat.working.done'),
      );
      expect(
        block.querySelector(
          '.working-block__activity, .working-block__dot, .working-block__time',
        ),
      ).toBeNull();
      expect(block.querySelectorAll('.reasoning-block')).toHaveLength(1);
      expect(block.querySelectorAll('.tool-event')).toHaveLength(1);
    });

    it.each([
      [
        'the latest Tool',
        [reasoningItem('Inspect the repository.'), bashChild()],
        'bash',
      ],
      [
        'the latest Tool while later Thinking is active',
        [
          bashChild({ status: 'success' }),
          reasoningItem('Interpret the result.', { streaming: true }),
        ],
        'bash',
      ],
      [
        'no activity for Thinking only',
        [
          reasoningItem('First thought.', { id: 'first' }),
          reasoningItem('Second thought.', { id: 'second', streaming: true }),
        ],
        null,
      ],
    ])(
      'labels an active group as working with %s',
      (_case, items, activity) => {
        run.mount({
          item: assistantRun({ status: 'running', items }),
          chatWorkingMode: 'compact',
        });

        const block = workingBlock();
        expect(
          block.querySelector('.working-block__label').textContent.trim(),
        ).toBe(t('chat.working.active'));
        expect(
          block.querySelector('.working-block__activity')?.textContent.trim() ??
            null,
        ).toBe(activity);
        expect(block.querySelector('summary').textContent).not.toContain(
          t('chat.event.thinking'),
        );
      },
    );

    it('ends a Working block at visible Assistant output and starts a new one', () => {
      run.mount({
        item: assistantRun({
          items: [
            reasoningItem('First pass.'),
            readChild({ id: 'read-before', status: 'success' }),
            outputItem('I found the responsible module.'),
            bashChild({ id: 'after', status: 'success', withResult: true }),
            readChild({ id: 'read-after', status: 'success' }),
          ],
        }),
        chatWorkingMode: 'compact',
      });

      const blocks = document.querySelectorAll('.working-block');
      const output = document.querySelector('.msg-markdown');
      expect(blocks).toHaveLength(2);
      expect(output.textContent).toContain('I found the responsible module.');
      expect(blocks[0].compareDocumentPosition(output)).toBe(
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
      expect(output.compareDocumentPosition(blocks[1])).toBe(
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
    });

    it.each([
      ['a single Thinking row', 'compact', [reasoningItem('Inspect.')]],
      ['a single Tool row', 'compact', [readChild({ status: 'success' })]],
      [
        'normal mode',
        'normal',
        [reasoningItem('Inspect.'), readChild({ status: 'success' })],
      ],
    ])('keeps %s inline without a Working wrapper', (_case, mode, items) => {
      run.mount({ item: assistantRun({ items }), chatWorkingMode: mode });

      expect(workingBlock()).toBeNull();
      expect(
        document.querySelectorAll('.reasoning-block, .tool-event'),
      ).toHaveLength(items.length);
    });
  });

  describe('Run footer', () => {
    function footer() {
      return document.querySelector('.run-footer');
    }

    it('renders status, duration, and change stats under a completed Run', () => {
      run.mount({
        item: assistantRun({
          status: 'completed',
          durationMs: 8000,
          items: [editChild('a.txt')],
        }),
      });

      const filesChanged = `${t('chat.changeStats.filesOne')},`;
      expect(footer().getAttribute('aria-label')).toBe(
        `${t('chat.runStatus.completed')} · 8.0s · ${filesChanged} +3 -2`,
      );
      expect(
        [...footer().querySelectorAll('.run-footer__part')].map(
          (part) => part.textContent,
        ),
      ).toEqual([
        t('chat.runStatus.completed'),
        '8.0s',
        filesChanged,
        '+3',
        '-2',
      ]);
      expect(footer().querySelectorAll('.run-footer__sep')).toHaveLength(2);
      expect(
        footer().querySelector('.run-footer__part--added').textContent,
      ).toBe('+3');
      expect(
        footer().querySelector('.run-footer__part--removed').textContent,
      ).toBe('-2');
      // The change block is one unit, so its label and counts never wrap
      // apart from each other.
      expect(
        [
          ...footer().querySelectorAll(
            '.run-footer__changes .run-footer__part',
          ),
        ].map((part) => part.textContent),
      ).toEqual([filesChanged, '+3', '-2']);
    });

    it('shows the changed files in a hover tooltip on the change block', async () => {
      vi.useFakeTimers();
      run.mount({
        item: assistantRun({
          status: 'completed',
          durationMs: 8000,
          items: [editChild('a.txt'), editChild('b.txt')],
        }),
      });

      const changeBlock = document.querySelector('.run-footer__changes');
      changeBlock.dispatchEvent(new Event('pointerenter'));
      await vi.advanceTimersByTimeAsync(TOOLTIP_SHOW_DELAY_MS);
      flushSync();

      expect(document.getElementById('app-tooltip').textContent).toBe(
        'a.txt\nb.txt',
      );
      changeBlock.dispatchEvent(new Event('pointerleave'));
    });

    it.each([
      [
        'the live duration while running',
        {
          status: 'running',
          startTimestamp: '2026-08-05T18:00:00.000Z',
        },
        Date.parse('2026-08-05T18:00:05.250Z'),
        () => `${t('chat.runStatus.running')} · 5.3s`,
      ],
      [
        'only the status without duration or changes',
        {},
        undefined,
        () => t('chat.runStatus.running'),
      ],
      [
        'the iteration count',
        { status: 'completed', durationMs: 8000, iterationCount: 3 },
        undefined,
        () =>
          `${t('chat.runStatus.completed')} · 8.0s · ${t('chat.runIterations', '', { count: 3 })}`,
      ],
    ])('shows %s', (_case, fields, nowMs, label) => {
      run.mount({ item: assistantRun(fields), nowMs });

      expect(footer().getAttribute('aria-label')).toBe(label());
    });

    it('renders the Provider liveness notice on its own line below the footer', () => {
      run.mount({
        item: assistantRun({
          status: 'running',
          providerHeartbeat: { idleSeconds: 75.4 },
        }),
      });

      const notice = t('chat.providerWorking', '', { seconds: 75 });
      expect(document.querySelector('.run-footer__notice').textContent).toBe(
        notice,
      );
      expect(footer().textContent).not.toContain(notice);
    });

    it.each([undefined, 0.3, 13, 59.9])(
      'renders no notice line for an ordinary pause of %ss',
      (idleSeconds) => {
        run.mount({
          item: assistantRun({
            status: 'running',
            ...(idleSeconds === undefined
              ? {}
              : { providerHeartbeat: { idleSeconds } }),
          }),
        });

        expect(document.querySelector('.run-footer__notice')).toBeNull();
      },
    );

    it('uses the live clock to reveal a long request wait without heartbeats', () => {
      const startedAt = Date.parse('2026-09-16T12:00:00Z');
      run.mount({
        item: assistantRun({
          status: 'running',
          providerRequestStatus: {
            state: 'waiting',
            timestamp: new Date(startedAt).toISOString(),
          },
        }),
        nowMs: startedAt + 60_000,
      });

      expect(document.querySelector('.run-footer__notice')).not.toBeNull();
    });
  });

  describe('handed-off background bash rows', () => {
    const handedOffBash = () =>
      toolChild(
        'bash',
        { command: 'npm run dev', mode: 'background' },
        {
          id: 'bash-bg',
          status: 'success',
          timing: {
            started_at: '2026-09-04T12:00:00+00:00',
            completed_at: '2026-09-04T12:00:01+00:00',
            duration_ms: 1000,
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
        },
      );

    it.each([
      [
        'keeps the row running and ticking while the process runs',
        {},
        ['running', 'done'],
        '30m 0s',
        { shows: [], hides: [] },
      ],
      [
        'settles the row with the real runtime and actual result',
        {
          backgroundBashProcesses: {
            'process-one': {
              status: 'completed',
              exitCode: 0,
              cancelledByUser: false,
              startedAt: '2026-09-04T12:00:00+00:00',
              finishedAt: '2026-09-04T12:04:12+00:00',
              output: 'build finished cleanly',
              truncated: false,
              logFile: 'C:/logs/bash/process-one.log',
            },
          },
        },
        ['done', 'running'],
        '4m 12s',
        { shows: ['build finished cleanly'], hides: ['handed off to vBot'] },
      ],
      [
        'settles the dot from durable History without inventing a runtime',
        { backgroundBashStatuses: { 'process-one': 'completed' } },
        ['done', 'running'],
        '',
        { shows: [], hides: [] },
      ],
    ])('%s', (_case, props, [dot, otherDot], time, result) => {
      run.mount({
        item: assistantRun({ items: [handedOffBash()] }),
        nowMs: Date.parse('2026-09-04T12:30:00Z'),
        ...props,
      });

      expect(toolDot().classList.contains(dot)).toBe(true);
      expect(toolDot().classList.contains(otherDot)).toBe(false);
      expect(
        document.querySelector('.tool-event-line .te-time')?.textContent ?? '',
      ).toBe(time);
      const resultText = detailRow('chat.toolResultLabel').textContent;
      for (const text of result.shows) expect(resultText).toContain(text);
      for (const text of result.hides) expect(resultText).not.toContain(text);
    });
  });
});
