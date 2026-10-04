// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { fileURLToPath } from 'node:url';

import { readStyleSheet } from '../../__tests__/styles.support.js';
import {
  appendEvents,
  appendReportedLiveRunEvents,
  assistantOutput,
  detailRow,
  detailText,
  flushSync,
  occurrences,
  reportedMultiStepMessages,
  runChildren,
  setupChatTimelineSuite,
  timelineSession,
  toolResult,
  toolStarted,
  userPersisted,
} from './ChatTimeline.support.js';
import { loadHistory, startRun } from '../../lib/chatState.js';
import { t } from '../../lib/i18n.js';

// Match the canonical identity supplied by chat.history, including sparse
// replay.
function historyRows(runId, messages) {
  return messages.map((message, history_sequence) => ({
    ...message,
    history_sequence,
    history_run_id: runId,
  }));
}

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

  it.each([
    [
      'keeps Thinking above a later Tool row after further reasoning',
      [
        {
          type: 'reasoning_delta',
          payload: { reasoning_delta: 'Thinking starts' },
        },
        toolStarted('call-order', 'read', { path: 'MEMORY.md' }),
        {
          type: 'reasoning_delta',
          payload: { reasoning_delta: ' and keeps going' },
        },
        assistantOutput('Done'),
      ],
      [
        ['reasoning-block', 'Thinking starts and keeps going'],
        ['run-tool-event', 'read'],
        ['msg-markdown', 'Done'],
      ],
    ],
    [
      'keeps distinct output phases around a Tool row',
      [
        {
          type: 'assistant_output_delta',
          payload: { content_delta: 'First answer' },
        },
        toolStarted('call-output', 'read', { path: 'MEMORY.md' }),
        toolResult('call-output', 'read', { ok: true, data: { content: 'A' } }),
        {
          type: 'assistant_output_delta',
          payload: { content_delta: 'Second answer' },
        },
      ],
      [
        ['msg-markdown', 'First answer'],
        ['run-tool-event', 'read'],
        ['msg-markdown', 'Second answer'],
      ],
    ],
    [
      'renders final reasoning once before the final content',
      [
        {
          type: 'reasoning',
          payload: {
            message: {
              id: 'assistant-reasoning-draft',
              role: 'assistant',
              reasoning: 'Summarize the result.',
            },
          },
        },
        {
          type: 'assistant_output_delta',
          payload: { content_delta: 'The timeline is in chatState.js.' },
        },
        assistantOutput('The timeline is in chatState.js.', {
          id: 'assistant-final',
          reasoning: 'Summarize the result.',
        }),
      ],
      [
        ['reasoning-block', 'Summarize the result.'],
        ['msg-markdown', 'The timeline is in chatState.js.'],
      ],
    ],
  ])('%s', (_case, events, expected) => {
    const sessionState = timelineSession();
    appendEvents(sessionState, 'run-order', events);
    timeline.render(sessionState);

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
    for (const [className, text] of expected) {
      if (className !== 'run-tool-event') expect(occurrences(text)).toBe(1);
    }
  });

  it.each([
    [
      'persisted History',
      (sessionState) => loadHistory(sessionState, reportedMultiStepMessages()),
    ],
    [
      'live events',
      (sessionState) =>
        appendReportedLiveRunEvents(sessionState, 'run-reported'),
    ],
    [
      'History overlapping live events',
      (sessionState) => {
        startRun(sessionState, {
          run_id: 'run-reported',
          sse_url: '/api/runs/run-reported/events',
          status: 'running',
        });
        appendEvents(sessionState, 'run-reported', [
          {
            type: 'user_message_persisted',
            payload: { message: reportedMultiStepMessages()[0] },
          },
        ]);
        appendReportedLiveRunEvents(sessionState, 'run-reported', 2);
        loadHistory(
          sessionState,
          historyRows('run-reported', reportedMultiStepMessages()),
        );
      },
    ],
  ])(
    'renders a reported multi-step Run from %s as one block',
    (_case, fill) => {
      const sessionState = timelineSession();
      fill(sessionState);
      timeline.render(sessionState);

      expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
      expect(document.querySelectorAll('.reasoning-block')).toHaveLength(3);
      expect(
        Array.from(document.querySelectorAll('.te-fn')).map(
          (element) => element.textContent,
        ),
      ).toEqual(['glob', 'read']);
      for (const text of REPORTED_TEXTS) expect(occurrences(text)).toBe(1);
    },
  );

  it.each([
    [
      'an active Run',
      { status: 'running' },
      'running',
      [
        { type: 'reasoning_delta', payload: { reasoning_delta: 'Checking' } },
        assistantOutput('The file says A.', { id: 'assistant-one' }),
        toolStarted('call-one', 'read', { path: 'a.txt' }),
      ],
    ],
    [
      'a Run that became terminal History',
      { status: 'completed' },
      undefined,
      [
        assistantOutput('The file says A.', { id: 'assistant-one' }),
        { type: 'run_completed', payload: { status: 'completed' } },
      ],
    ],
    [
      'terminal events after a History refresh',
      { status: 'running' },
      'completed',
      [
        assistantOutput('The file says A.', { id: 'assistant-one' }),
        { type: 'run_completed', payload: { status: 'completed' } },
      ],
    ],
  ])(
    'renders one block without working indicators when History overlaps %s',
    (_case, currentRun, sessionStatus, events) => {
      const sessionState = timelineSession();
      sessionState.messages = historyRows('run-overlap', [
        { id: 'user-one', role: 'user', content: 'Inspect the file' },
        { id: 'assistant-one', role: 'assistant', content: 'The file says A.' },
      ]);
      appendEvents(sessionState, 'run-overlap', [
        userPersisted('user-one', 'Inspect the file'),
        ...events,
      ]);
      sessionState.currentRun = {
        runId: 'run-overlap',
        sseUrl: '/api/runs/run-overlap/events',
        ...currentRun,
      };
      if (sessionStatus) sessionState.status = sessionStatus;
      timeline.render(sessionState);

      expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
      expect(document.querySelectorAll('.streaming-caret')).toHaveLength(0);
      expect(occurrences('The file says A.')).toBe(1);
      // Live-only Thinking still shows next to the persisted answer.
      for (const event of events) {
        if (event.type !== 'reasoning_delta') continue;
        expect(document.body.textContent).toContain(
          event.payload.reasoning_delta,
        );
      }
    },
  );

  it('uses completed persisted History when a follow-up Run starts with its own live events', () => {
    const sessionState = timelineSession();
    startRun(sessionState, {
      run_id: 'run-history-ahead',
      sse_url: '/api/runs/run-history-ahead/events',
      status: 'running',
    });
    loadHistory(
      sessionState,
      historyRows('run-history-ahead', [
        { id: 'user-one', role: 'user', content: 'Inspect the file' },
        { id: 'assistant-one', role: 'assistant', content: 'The file says A.' },
        {
          id: 'summary-one',
          role: 'run_summary',
          run_id: 'run-history-ahead',
          status: 'completed',
          iteration_count: 1,
        },
      ]),
      {
        runs: [
          { run_id: 'run-history-ahead', status: 'completed', complete: true },
        ],
      },
    );
    appendEvents(sessionState, 'run-history-ahead', [
      userPersisted('user-one', 'Inspect the file'),
      toolStarted('call-one', 'read', { path: 'a.txt' }),
      { type: 'run_completed', payload: { status: 'completed' } },
    ]);
    timeline.render(sessionState);

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    expect(document.querySelectorAll('.streaming-caret')).toHaveLength(0);
    expect(occurrences('The file says A.')).toBe(1);
    expect(document.body.textContent).not.toContain('read');
  });

  it('uses persisted overlap suffix rows as the single block during handoff', () => {
    const sessionState = timelineSession();
    startRun(sessionState, {
      run_id: 'run-history-suffix',
      sse_url: '/api/runs/run-history-suffix/events',
      status: 'running',
    });
    loadHistory(
      sessionState,
      historyRows('run-history-suffix', [
        { id: 'user-one', role: 'user', content: 'Inspect the file' },
        {
          id: 'assistant-tools',
          role: 'assistant',
          reasoning: 'Need to read it.',
          tool_calls: [
            { id: 'call-one', name: 'read', arguments: { path: 'a.txt' } },
          ],
        },
        {
          id: 'tool-one',
          role: 'tool',
          tool_call_id: 'call-one',
          name: 'read',
          content: '{"ok": true, "content": "A"}',
        },
        {
          id: 'assistant-final',
          role: 'assistant',
          content: 'The file says A.',
        },
      ]),
    );
    appendEvents(sessionState, 'run-history-suffix', [
      userPersisted('user-one', 'Inspect the file'),
      { type: 'run_completed', payload: { status: 'completed' } },
    ]);
    timeline.render(sessionState);

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    expect(document.body.textContent).toContain('Need to read it.');
    expect(document.querySelector('.te-fn').textContent).toBe('read');
    expect(occurrences('The file says A.')).toBe(1);
  });

  it.each([
    [
      'an automatic follow-up Run',
      (sessionState) =>
        appendEvents(sessionState, 'run-parent', [
          { type: 'run_started', payload: { status: 'running' } },
          assistantOutput('First answer.'),
          { type: 'run_completed', payload: { status: 'completed' } },
          {
            type: 'run_started',
            run_id: 'run-follow-up',
            payload: { status: 'running' },
          },
          {
            type: 'assistant_output_delta',
            run_id: 'run-follow-up',
            payload: { content_delta: 'Second answer.' },
          },
        ]),
    ],
    [
      'consecutive Assistant History Runs',
      (sessionState) =>
        loadHistory(sessionState, [
          { id: 'user-one', role: 'user', content: 'Start background work' },
          {
            id: 'assistant-tool-call',
            role: 'assistant',
            content: null,
            tool_calls: [
              {
                id: 'call-subagent',
                name: 'subagent',
                arguments: { agent_id: 'tester', background: true },
              },
            ],
          },
          {
            id: 'tool-subagent',
            role: 'tool',
            tool_call_id: 'call-subagent',
            name: 'subagent',
            content: '{"ok":true}',
          },
          {
            id: 'assistant-started',
            role: 'assistant',
            content: 'First answer.',
          },
          {
            id: 'assistant-follow-up',
            role: 'assistant',
            content: 'Second answer.',
          },
        ]),
    ],
    [
      'ordinary User turns',
      (sessionState) =>
        appendEvents(sessionState, 'run-one', [
          userPersisted('user-one', 'First request'),
          assistantOutput('First answer.'),
          { ...userPersisted('user-two', 'Second request'), run_id: 'run-two' },
          { ...assistantOutput('Second answer.'), run_id: 'run-two' },
        ]),
    ],
  ])(
    'renders %s as separate Assistant Runs without a divider',
    (_case, fill) => {
      const sessionState = timelineSession();
      fill(sessionState);
      timeline.render(sessionState);

      const assistantRuns = document.querySelectorAll('.assistant-run');
      expect(assistantRuns).toHaveLength(2);
      expect(assistantRuns[0].textContent).toContain('First answer.');
      expect(assistantRuns[1].textContent).toContain('Second answer.');
      expect(document.querySelector('.run-boundary-sep')).toBeNull();
    },
  );

  it.each(['live', 'mixed', 'history'])(
    'gives steered User messages the same appearance as ordinary messages in %s',
    (mode) => {
      appendAppStyles();
      const sessionState = timelineSession();
      const user = {
        id: 'original',
        role: 'user',
        content: 'Please inspect the layout.',
        history_run_id: 'run-steering',
      };
      const messages = [
        user,
        {
          id: 'before-steering',
          role: 'assistant',
          content: 'Inspecting the layout.',
          history_run_id: 'run-steering',
        },
        { ...user, id: 'steered-text', content: 'Stop the current task.' },
        {
          ...user,
          id: 'steered-blocks',
          content: [{ type: 'text', text: 'Inspect this instead.' }],
        },
      ];
      if (mode !== 'history') {
        startRun(sessionState, { run_id: 'run-steering' });
        appendEvents(
          sessionState,
          'run-steering',
          messages.map((message) => ({
            type:
              message.role === 'user'
                ? 'user_message_persisted'
                : 'assistant_output',
            payload: { message },
          })),
        );
      }
      if (mode !== 'live') loadHistory(sessionState, messages);
      timeline.render(sessionState);

      const [ordinary, ...steered] = document.querySelectorAll('.msg.user');
      expect(steered).toHaveLength(2);
      const appearance = (message) =>
        ['.msg-body-text', '.msg-avatar', '.msg-author'].map((selector) => {
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
      expect(appearance(ordinary)[0][0]).toBe('10px 16px');
      for (const correction of steered) {
        expect(correction.closest('.assistant-run')).toBeTruthy();
        expect(appearance(correction)).toEqual(appearance(ordinary));
      }
    },
  );

  it.each([false, true])(
    'renders a persisted Run error once with a User anchor: %s',
    (withUser) => {
      const sessionState = timelineSession();
      const message = {
        id: 'failure',
        role: 'error',
        content: 'Test provider failure',
        error_kind: 'provider',
      };
      const user = { id: 'user', role: 'user', content: 'User question' };
      startRun(sessionState, {
        run_id: 'automatic',
        events: [{ run_id: 'automatic', sequence: 1, type: 'run_started' }],
      });
      appendEvents(
        sessionState,
        'automatic',
        [
          { type: 'error_message_persisted', payload: { message } },
          ...(withUser
            ? [{ type: 'user_message_persisted', payload: { message: user } }]
            : []),
        ],
        2,
      );
      loadHistory(sessionState, withUser ? [user, message] : [message]);
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
    loadHistory(sessionState, [
      {
        id: 'user-one',
        role: 'user',
        content: 'Run tool',
        timestamp: '2026-05-03T14:30:00+00:00',
      },
      {
        id: 'assistant-tool',
        role: 'assistant',
        content: null,
        timestamp: '2026-05-03T14:30:00+00:00',
        tool_calls: [{ id: 'call-one', name: 'read', arguments: {} }],
      },
      {
        id: 'tool-one',
        role: 'tool',
        tool_call_id: 'call-one',
        name: 'read',
        content: '{"ok":true,"error":null,"data":{},"artifacts":[]}',
        timestamp: '2026-05-03T14:30:02+00:00',
        timing,
      },
      {
        id: 'assistant-final',
        role: 'assistant',
        content: 'Done',
        timestamp: '2026-05-03T14:30:03+00:00',
      },
      {
        id: 'summary-one',
        role: 'run_summary',
        run_id: 'run-one',
        status: 'completed',
        timestamp: '2026-05-03T14:30:03+00:00',
        timing,
      },
    ]);
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
    startRun(sessionState, {
      run_id: 'run-live-elapsed',
      status: 'running',
      started_at: '2026-08-05T18:00:00.000Z',
      events: [],
    });
    appendEvents(sessionState, 'run-live-elapsed', [
      {
        ...toolStarted('call-live-elapsed', 'read', { path: 'README.md' }),
        timestamp: '2026-08-05T18:00:02.500Z',
      },
    ]);
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
    appendEvents(sessionState, 'run-preparing', [
      {
        type: 'tool_call_delta',
        timestamp: '2026-08-05T18:00:00.000Z',
        payload: {
          tool_call_id: 'call-preparing',
          name_delta: 'read',
          arguments_delta: '{"path":"README.md"}',
          preview_arguments: { path: 'README.md' },
        },
      },
    ]);
    timeline.render(sessionState);

    const toolLine = document.querySelector('.tool-event-line');
    expect(toolLine.querySelector('.te-dot.preparing')).not.toBeNull();
    expect(toolLine.querySelector('.te-time')).toBeNull();
  });

  it('shows cancelled status for a Tool that was active when the Run was cancelled', () => {
    const sessionState = timelineSession();
    appendEvents(sessionState, 'run-cancelled-tool', [
      toolStarted('call-bash', 'bash', { command: 'sleep 30' }),
      { type: 'run_cancelled', payload: { status: 'cancelled' } },
    ]);
    timeline.render(sessionState);

    const toolLine = document.querySelector('.tool-event-line');
    expect(toolLine.querySelector('.te-fn').textContent).toBe('bash');
    expect(toolLine.textContent).toContain(t('chat.toolCancelled'));
    expect(toolLine.querySelector('.te-dot.running')).toBeNull();
    expect(toolLine.querySelector('.te-dot.cancelled')).not.toBeNull();
  });

  it.each(['live', 'mixed', 'history'])(
    'renders a model fallback notice once inside its Assistant Run in %s',
    (mode) => {
      const sessionState = timelineSession();
      const models = {
        from_model: 'openai/gpt-5',
        to_model: 'openrouter/anthropic/claude-sonnet-4',
      };
      const answer = { id: 'answer', role: 'assistant', content: 'Recovered' };
      if (mode !== 'history') {
        startRun(sessionState, { run_id: 'run-model-fallback' });
        appendEvents(sessionState, 'run-model-fallback', [
          { type: 'model_fallback_activated', payload: models },
          assistantOutput(answer.content, answer),
        ]);
      }
      // History keeps the notice after the Run and across a reload.
      if (mode !== 'live')
        loadHistory(
          sessionState,
          historyRows('run-model-fallback', [
            { id: 'user', role: 'user', content: 'Hi' },
            { id: 'fallback-note', role: 'model_fallback', ...models },
            answer,
            ...(mode === 'history'
              ? [
                  {
                    id: 'summary',
                    role: 'run_summary',
                    run_id: 'run-model-fallback',
                    status: 'completed',
                  },
                ]
              : []),
          ]),
        );
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

  it('shows the live output screen in place of an Output detail block', () => {
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
    appendEvents(sessionState, 'run-tool-output', [
      toolStarted('call-one', 'bash', { command: 'printf hello' }),
      {
        type: 'tool_call_output',
        payload: {
          tool_call_id: 'call-one',
          terminal_id: 'term_one',
          screen: '$ printf hello\nhello',
        },
      },
      toolResult(
        'call-one',
        'bash',
        { ok: true, data: { exit_code: 0, output: 'hello' } },
        { display },
      ),
    ]);
    timeline.render(sessionState);

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
    appendEvents(sessionState, 'run-interrupted', [
      assistantOutput('The first half of the answer', { interrupted: true }),
    ]);
    timeline.render(sessionState);

    expect(
      document.querySelector('.run-inline-banner.banner--warn'),
    ).toBeNull();
    expect(document.body.textContent).toContain('The first half of the answer');
  });

  it('renders live Tool output apart from the Result', () => {
    const sessionState = timelineSession();
    appendEvents(sessionState, 'run-tool-output', [
      toolStarted('call-one', 'bash', { command: 'printf hello' }),
      {
        type: 'tool_call_output',
        payload: {
          tool_call_id: 'call-one',
          terminal_id: 'term_one',
          screen: 'hello\nwarn',
        },
      },
      toolResult('call-one', 'bash', {
        ok: true,
        data: { exit_code: 0, output: 'hello\nwarn' },
      }),
    ]);
    timeline.render(sessionState);

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
    appendEvents(sessionState, 'run-bash-result-output', [
      toolStarted('call-one', 'bash', { command: 'printf hello' }),
      toolResult('call-one', 'bash', {
        ok: true,
        data: { exit_code: 0, output: 'hello from history\n' },
      }),
    ]);
    timeline.render(sessionState);

    expect(
      detailRow('chat.toolResultLabel').querySelector('.teb-code').textContent,
    ).toContain('hello from history');
  });

  it('renders a stable-sized Thinking chevron and only rotates it when expanded', () => {
    appendAppStyles();
    const sessionState = timelineSession();
    appendEvents(sessionState, 'run-thinking-chevron', [
      {
        type: 'reasoning',
        payload: {
          message: { role: 'assistant', reasoning: 'Trace the issue' },
        },
      },
    ]);
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
