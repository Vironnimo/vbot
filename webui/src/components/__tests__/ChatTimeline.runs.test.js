// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { fileURLToPath } from 'node:url';

import { readStyleSheet } from '../../__tests__/styles.support.js';
import {
  detailRow,
  detailText,
  flushSync,
  occurrences,
  openDisclosures,
  runChildren,
  setupChatTimelineSuite,
  timelineSession,
} from './ChatTimeline.support.js';
import {
  assistantRun,
  messageItem,
  outputItem,
  reasoningItem,
  toolItem,
  reportedMultiStepItems,
} from './timelineItems.support.js';
import { applyTimelineEvent } from '../../lib/chatTimeline.js';
import { t } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

// A variable path keeps Vite from rewriting the URL into an asset URL.
const APP_STYLES_PATH = '../../styles/app.css';

function appendAppStyles() {
  const stylesheet = document.createElement('style');
  stylesheet.textContent = readStyleSheet(
    fileURLToPath(new URL(APP_STYLES_PATH, import.meta.url)),
  );
  document.body.append(stylesheet);
}

const REPORTED_TEXTS = [
  'I found the timeline helper; now I will read it.',
  'The timeline is in chatState.js.',
  'Find candidate files.',
  'Read the selected file.',
  'Summarize the result.',
];

describe('ChatTimeline Runs', () => {
  const timeline = setupChatTimelineSuite();

  it('marks omitted Run steps for loading without a visible action', () => {
    const session = timelineSession('windowed');
    session.timelineItems = [
      assistantRun('one', [
        {
          id: 'history-gap',
          type: 'history_gap',
          run_id: 'one',
          before: 'first-visible',
          position: -1,
        },
        outputItem('Latest output'),
      ]),
    ];
    timeline.render(session);
    const marker = document.querySelector('[data-history-gap]');
    expect(marker.dataset).toMatchObject({
      historyGap: 'one',
      historyGapBefore: 'first-visible',
    });
    expect(marker.getAttribute('aria-hidden')).toBe('true');
    expect(marker.textContent).toBe('');
  });

  it.each([
    [
      'Thinking before a Tool and answer',
      [
        reasoningItem('Thinking starts and keeps going'),
        toolItem({
          id: 'call-order',
          name: 'read',
          arguments: { path: 'MEMORY.md' },
        }),
        outputItem('Done'),
      ],
      [
        ['reasoning-block', 'Thinking starts and keeps going'],
        ['run-tool-event', 'read'],
        ['msg-markdown', 'Done'],
      ],
    ],
    [
      'distinct output phases around a Tool',
      [
        outputItem('First answer', { id: 'first' }),
        toolItem({
          id: 'call-output',
          name: 'read',
          arguments: { path: 'MEMORY.md' },
          result: { ok: true, data: { content: 'A' } },
        }),
        outputItem('Second answer', { id: 'second', streaming: true }),
      ],
      [
        ['msg-markdown', 'First answer'],
        ['run-tool-event', 'read'],
        ['msg-markdown', 'Second answer'],
      ],
    ],
    [
      'final reasoning before the answer',
      [
        reasoningItem('Summarize the result.'),
        outputItem('The timeline is in chatState.js.'),
      ],
      [
        ['reasoning-block', 'Summarize the result.'],
        ['msg-markdown', 'The timeline is in chatState.js.'],
      ],
    ],
  ])('renders server-ordered %s', (_case, items, expected) => {
    const sessionState = timelineSession();
    sessionState.timelineItems = [assistantRun('order', items)];
    timeline.render(sessionState);
    openDisclosures('.reasoning-block');
    expect(
      runChildren().map((child) => [
        expected.find(([className]) =>
          child.classList.contains(className),
        )?.[0],
        child.textContent,
      ]),
    ).toEqual(
      expected.map(([className, text]) => [
        className,
        expect.stringContaining(text),
      ]),
    );
    for (const [className, text] of expected)
      if (className !== 'run-tool-event') expect(occurrences(text)).toBe(1);
  });

  it.each(['live', 'history'])(
    'renders a complete multi-step %s Run as one block',
    (source) => {
      const sessionState = timelineSession();
      sessionState.timelineItems = reportedMultiStepItems();
      sessionState.timelineItems[1].source = source;
      timeline.render(sessionState);
      openDisclosures('.reasoning-block');
      expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
      expect(document.querySelectorAll('.reasoning-block')).toHaveLength(3);
      expect(
        Array.from(
          document.querySelectorAll('.te-fn'),
          (element) => element.textContent,
        ),
      ).toEqual(['glob', 'read']);
      for (const text of REPORTED_TEXTS) expect(occurrences(text)).toBe(1);
    },
  );

  it.each(['live', 'history'])(
    'renders separate %s Runs without a divider',
    (source) => {
      const sessionState = timelineSession();
      sessionState.timelineItems = [
        assistantRun('first', [outputItem('First answer.')], {
          source,
          status: 'completed',
        }),
        assistantRun(
          'second',
          [outputItem('Second answer.', { streaming: source === 'live' })],
          { source, status: source === 'live' ? 'running' : 'completed' },
        ),
      ];
      timeline.render(sessionState);
      const runs = document.querySelectorAll('.assistant-run');
      expect(runs).toHaveLength(2);
      expect(runs[0].textContent).toContain('First answer.');
      expect(runs[1].textContent).toContain('Second answer.');
      expect(document.querySelector('.run-boundary-sep')).toBeNull();
    },
  );

  it.each(['live', 'history'])(
    'gives steered User messages the ordinary message appearance in %s',
    (source) => {
      appendAppStyles();
      const sessionState = timelineSession();
      const user = {
        id: 'original',
        role: 'user',
        content: 'Please inspect the layout.',
      };
      sessionState.timelineItems = [
        messageItem(user),
        assistantRun(
          'steering',
          [
            outputItem('Inspecting the layout.'),
            {
              id: 'steered-text',
              type: 'user_message',
              content: 'Stop the current task.',
              message: {
                ...user,
                id: 'steered-text',
                content: 'Stop the current task.',
              },
            },
            {
              id: 'steered-blocks',
              type: 'user_message',
              content: [{ type: 'text', text: 'Inspect this instead.' }],
              message: {
                ...user,
                id: 'steered-blocks',
                content: [{ type: 'text', text: 'Inspect this instead.' }],
              },
            },
          ],
          { source },
        ),
      ];
      timeline.render(sessionState);
      const [ordinary, ...steered] = document.querySelectorAll('.msg.user');
      expect(steered).toHaveLength(2);
      const appearance = (message) =>
        ['.msg-user-text', '.msg-header'].map((selector) => {
          const style = getComputedStyle(message.querySelector(selector));
          return [
            style.padding,
            style.color,
            style.backgroundColor,
            style.border,
            style.maxWidth,
            style.lineHeight,
          ];
        });
      expect(appearance(ordinary)[0][0]).toBe('8px 14px');
      for (const correction of steered) {
        expect(correction.closest('.assistant-run')).toBeTruthy();
        expect(appearance(correction)).toEqual(appearance(ordinary));
      }
    },
  );

  it.each([false, true])(
    'renders a server error once with a User anchor: %s',
    (withUser) => {
      const sessionState = timelineSession();
      const message = {
        id: 'failure',
        role: 'error',
        content: 'Test provider failure',
        error_kind: 'provider',
      };
      sessionState.timelineItems = [
        ...(withUser
          ? [
              messageItem({
                id: 'user',
                role: 'user',
                content: 'User question',
              }),
            ]
          : []),
        messageItem(message),
      ];
      timeline.render(sessionState);
      expect(
        document.querySelectorAll('[data-timeline-item-id="failure"]'),
      ).toHaveLength(1);
      expect(occurrences(message.content)).toBe(1);
    },
  );

  it('renders persisted Run and Tool durations after History load', () => {
    const sessionState = timelineSession();
    const timing = {
      started_at: '2026-05-03T14:30:01+00:00',
      completed_at: '2026-05-03T14:30:02.250+00:00',
      duration_ms: 1250,
    };
    sessionState.timelineItems = [
      assistantRun(
        'run-one',
        [
          toolItem({
            id: 'call-one',
            name: 'read',
            result: { ok: true, error: null, data: {}, artifacts: [] },
            timing,
          }),
          outputItem('Done'),
        ],
        { status: 'completed', source: 'history', timing, durationMs: 1250 },
      ),
    ];
    timeline.render(sessionState);

    expect(document.querySelector('.run-footer').textContent).toContain('1.3s');
    expect(
      document.querySelector('.run-tool-event .te-time').textContent,
    ).toContain('1.3s');
  });

  it('shows live elapsed Run and Tool time and advances from absolute timestamps', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-05T18:00:05.000Z'));
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun(
        'run-live-elapsed',
        [
          toolItem({
            id: 'call-live-elapsed',
            name: 'read',
            arguments: { path: 'README.md' },
            timestamp: '2026-08-05T18:00:02.500Z',
            startedEvent: {
              type: 'tool_call_started',
              timestamp: '2026-08-05T18:00:02.500Z',
              payload: {
                tool_call: {
                  id: 'call-live-elapsed',
                  name: 'read',
                  arguments: { path: 'README.md' },
                },
              },
            },
          }),
        ],
        { startTimestamp: '2026-08-05T18:00:00.000Z' },
      ),
    ];
    timeline.render(sessionState);

    const footer = document.querySelector('.run-footer');
    const toolTime = document.querySelector('.tool-event-line .te-time');
    expect(footer.textContent).toContain(t('chat.runStatus.running'));
    expect(footer.textContent).toContain('5.0s');
    expect(toolTime.textContent).toBe('2.5s');

    await vi.advanceTimersByTimeAsync(500);
    flushSync();

    expect(footer.textContent).toContain('5.5s');
    expect(toolTime.textContent).toBe('3.0s');
  });

  it('shows no duration while a streamed Tool call is still preparing', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-05T18:00:05.000Z'));
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun('run-preparing', [
        toolItem({
          id: 'call-preparing',
          name: 'read',
          status: 'preparing',
          streaming: true,
          partialArgumentsText: '{"path":"README.md"}',
          previewArguments: { path: 'README.md' },
          startedEvent: null,
        }),
      ]),
    ];
    timeline.render(sessionState);

    const toolLine = document.querySelector('.tool-event-line');
    expect(toolLine.querySelector('.te-dot.preparing')).not.toBeNull();
    expect(toolLine.querySelector('.te-time')).toBeNull();
  });

  it('shows cancelled status for a Tool that was active when the Run was cancelled', () => {
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun(
        'run-cancelled-tool',
        [
          toolItem({
            id: 'call-bash',
            name: 'bash',
            arguments: { command: 'sleep 30' },
            status: 'cancelled',
          }),
        ],
        { status: 'cancelled' },
      ),
    ];
    timeline.render(sessionState);

    const toolLine = document.querySelector('.tool-event-line');
    expect(toolLine.querySelector('.te-fn').textContent).toBe('bash');
    expect(toolLine.textContent).toContain(t('chat.toolCancelled'));
    expect(toolLine.querySelector('.te-dot.running')).toBeNull();
    expect(toolLine.querySelector('.te-dot.cancelled')).not.toBeNull();
  });

  it.each(['live', 'history'])(
    'renders a model fallback notice once inside its Assistant Run in %s',
    (mode) => {
      const sessionState = timelineSession();
      const models = {
        from_model: 'openai/gpt-5',
        to_model: 'openrouter/anthropic/claude-sonnet-4',
      };
      sessionState.timelineItems = [
        assistantRun(
          'fallback',
          [
            {
              id: 'fallback-note',
              type: 'model_fallback',
              content: models.to_model,
              ...models,
            },
            outputItem('Recovered'),
          ],
          {
            source: mode,
            status: mode === 'history' ? 'completed' : 'running',
          },
        ),
      ];
      timeline.render(sessionState);

      const notices = document.querySelectorAll(
        '.assistant-run .run-inline-banner.banner--info',
      );
      expect(notices).toHaveLength(1);
      expect(notices[0].textContent.trim()).toBe(
        t('chat.modelFallbackFrom', {
          from: 'openai/gpt-5',
          to: 'openrouter/anthropic/claude-sonnet-4',
        }),
      );
      const run = notices[0].closest('.assistant-run');
      expect(run.textContent).toContain('Recovered');
      expect(run.textContent.includes(t('chat.runStatus.running'))).toBe(
        mode !== 'history',
      );
    },
  );

  it('renders goal changes as status rows inside a Run and between Runs', () => {
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun(
        'goal-turn',
        [
          {
            id: 'goal-n1',
            type: 'goal_event',
            event: 'turn',
            turn: 2,
            max_turns: 30,
          },
          outputItem('Next step done'),
        ],
        { source: 'history', status: 'completed' },
      ),
      {
        id: 'goal-n2',
        type: 'goal_event',
        timestamp: '2026-10-17T10:00:00Z',
        message: {
          id: 'n2',
          role: 'goal_event',
          event: 'paused',
          reason: 'stop',
        },
      },
    ];
    timeline.render(sessionState);

    const rows = [...document.querySelectorAll('.goal-sep')];
    expect(rows.map((row) => row.textContent.trim())).toEqual([
      t('chat.goalRow.turn', { turn: 2, max: 30 }),
      t('chat.goalRow.paused.stop'),
    ]);
    expect(rows[0].closest('.assistant-run')).not.toBeNull();
    expect(rows[1].closest('.assistant-run')).toBeNull();
  });

  it('shows the live output in place of an Output detail block', () => {
    const sessionState = timelineSession();
    const display = {
      version: 1,
      hidden_argument_keys: [],
      primary: [],
      facts: [],
      details: [
        { type: 'text', label: 'command', text: 'printf hello' },
        {
          type: 'text',
          label: 'output',
          source: { from: 'result', path: ['data', 'output'] },
        },
      ],
    };
    sessionState.timelineItems = [
      assistantRun('run-tool-output', [
        toolItem({
          id: 'call-one',
          name: 'bash',
          arguments: { command: 'printf hello' },
          output: '$ printf hello\nhello',
          result: { ok: true, data: { exit_code: 0, output: 'hello' } },
          display,
        }),
      ]),
    ];
    timeline.render(sessionState);

    openDisclosures('.tool-event');
    expect(detailText('chat.toolDetailLabel.command')).toBe('printf hello');
    const outputRows = Array.from(document.querySelectorAll('.teb-row')).filter(
      (row) =>
        row.querySelector('.teb-label')?.textContent ===
        t('chat.toolDetailLabel.output'),
    );
    expect(outputRows).toHaveLength(1);
    expect(outputRows[0].textContent).toContain('$ printf hello');
  });

  it('keeps interrupted Assistant output without a recovery marker', () => {
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun(
        'run-interrupted',
        [outputItem('The first half of the answer', { interrupted: true })],
        { status: 'interrupted' },
      ),
    ];
    timeline.render(sessionState);

    expect(
      document.querySelector('.run-inline-banner.banner--warn'),
    ).toBeNull();
    expect(document.body.textContent).toContain('The first half of the answer');
  });

  it('renders live Tool output apart from the Result', () => {
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun('run-tool-output', [
        toolItem({
          id: 'call-one',
          name: 'bash',
          arguments: { command: 'printf hello' },
          output: 'hello\nwarn',
          result: { ok: true, data: { exit_code: 0, output: 'hello\nwarn' } },
        }),
      ]),
    ];
    timeline.render(sessionState);

    openDisclosures('.tool-event');
    expect(detailRow('chat.toolDetailLabel.output').textContent).toContain(
      'hello',
    );
    expect(detailText('chat.toolResultLabel')).toContain('exit_code: 0');
    expect(
      detailRow('chat.toolResultLabel').querySelector('.teb-code').textContent,
    ).not.toMatch(/hello|warn/);
  });

  it('renders the bash Result output when no streamed output is available', () => {
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun('run-bash-result-output', [
        toolItem({
          id: 'call-one',
          name: 'bash',
          arguments: { command: 'printf hello' },
          result: {
            ok: true,
            data: { exit_code: 0, output: 'hello from history\n' },
          },
        }),
      ]),
    ];
    timeline.render(sessionState);
    openDisclosures('.tool-event');

    expect(
      detailRow('chat.toolResultLabel').querySelector('.teb-code').textContent,
    ).toContain('hello from history');
  });

  it('updates a mounted Run through growing deltas and Tool completion while retaining earlier rows', () => {
    const props = reactiveProps({
      sessionState: timelineSession(),
      agentName: 'Alpha',
    });
    const { sessionState } = props;
    applyTimelineEvent(sessionState, 'timeline_snapshot', {
      run_id: 'run-stream',
      sequence: 3,
      items: [
        assistantRun('run-stream', [
          toolItem({
            id: 'read',
            name: 'read',
            arguments: { path: 'notes' },
            result: { ok: true, data: { content: 'saved' } },
          }),
        ]),
      ],
    });
    timeline.mount(props);
    openDisclosures('.tool-event');
    const firstTool = document.querySelector('.tool-event');
    const firstBody = firstTool.querySelector('.tool-event-body');
    applyTimelineEvent(sessionState, 'timeline_update', {
      run_id: 'run-stream',
      sequence: 4,
      base_sequence: 3,
      run: { id: 'assistant-run-run-stream' },
      children: [outputItem('First', { streaming: true })],
    });
    flushSync();
    expect(document.querySelector('.msg-markdown').textContent).toContain(
      'First',
    );
    applyTimelineEvent(sessionState, 'timeline_update', {
      run_id: 'run-stream',
      sequence: 6,
      base_sequence: 4,
      run: { id: 'assistant-run-run-stream' },
      child_updates: [{ id: 'output-one', append: { content: ' answer' } }],
      children: [
        toolItem({
          id: 'bash',
          name: 'bash',
          arguments: { command: 'echo done' },
        }),
      ],
    });
    flushSync();
    expect(document.querySelector('.msg-markdown').textContent).toContain(
      'First answer',
    );
    openDisclosures('.tool-event');
    const lastTool = document.querySelectorAll('.tool-event')[1];
    expect(
      lastTool.querySelector('.te-dot').classList.contains('running'),
    ).toBe(true);
    applyTimelineEvent(sessionState, 'timeline_update', {
      run_id: 'run-stream',
      sequence: 7,
      base_sequence: 6,
      run: { id: 'assistant-run-run-stream' },
      children: [
        toolItem({
          id: 'bash',
          name: 'bash',
          arguments: { command: 'echo done' },
          result: { ok: true, data: { output: 'finished output' } },
        }),
      ],
    });
    flushSync();
    expect(lastTool.querySelector('.te-dot').classList.contains('done')).toBe(
      true,
    );
    expect(lastTool.textContent).toContain('finished output');
    expect(document.querySelector('.tool-event')).toBe(firstTool);
    expect(firstTool.querySelector('.tool-event-body')).toBe(firstBody);
    expect(firstTool.open).toBe(true);
  });

  it('renders a stable-sized Thinking chevron and only rotates it when expanded', () => {
    appendAppStyles();
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun('run-thinking-chevron', [reasoningItem('Trace the issue')]),
    ];
    timeline.render(sessionState);

    const reasoningBlock = document.querySelector(
      '.assistant-run .reasoning-block',
    );
    const chevron = reasoningBlock.querySelector('.r-chevron');
    expect(reasoningBlock.open).toBe(false);
    expect(getComputedStyle(chevron).transform || 'none').toBe('none');

    reasoningBlock.open = true;
    reasoningBlock.dispatchEvent(new Event('toggle'));
    flushSync();

    for (const size of ['width', 'height']) {
      expect(chevron.getAttribute(size)).toBe('10');
    }
    expect(getComputedStyle(chevron).transform).toBe('rotate(180deg)');
  });
});
