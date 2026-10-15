// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import {
  flushSync,
  setupChatTimelineSuite,
  timelineSession,
} from './ChatTimeline.support.js';
import { assistantRun, toolItem } from './timelineItems.support.js';
import { t } from '../../lib/i18n.js';

const RUN_ARGUMENTS = {
  action: 'run',
  agent_id: 'beta',
  content: 'Inspect in the background',
};

// A Sub-Agent whose start names Child Run `sub-run`.
function subAgentSession() {
  const sessionState = timelineSession();
  sessionState.timelineItems = [
    assistantRun('run-parent', [
      toolItem({
        id: 'call-subagent',
        name: 'subagent',
        arguments: RUN_ARGUMENTS,
        result: {
          ok: true,
          data: {
            agent_id: 'beta',
            session_id: 'sub-session',
            run_id: 'sub-run',
            status: 'running',
          },
        },
      }),
    ]),
  ];
  return sessionState;
}

function subAgentLine() {
  return document.querySelector('.subagent-tool-event .subagent-line');
}

describe('ChatTimeline sub-agent rows', () => {
  const timeline = setupChatTimelineSuite();

  it.each([
    [
      'list',
      { action: 'list' },
      {
        subagents: [
          {
            id: 'sub_work_1',
            agent_id: 'beta',
            session_id: 'sub-session-1',
            state: 'running',
          },
        ],
      },
      'list',
    ],
    [
      'former status',
      { action: 'status', id: 'sub_work_1' },
      { subagents: [] },
      'list · sub_work_1',
    ],
    [
      'cancel',
      { action: 'cancel', id: 'sub_work_1' },
      {
        id: 'sub_work_1',
        agent_id: 'beta',
        session_id: 'sub-session-1',
        status: 'cancelled',
      },
      'cancel · sub_work_1',
    ],
  ])(
    'renders a %s call as an ordinary Tool call',
    (_action, args, data, label) => {
      const sessionState = timelineSession();
      sessionState.timelineItems = [
        assistantRun('run-subagent-call', [
          toolItem({
            id: 'call-subagent',
            name: 'subagent',
            arguments: RUN_ARGUMENTS,
            result: {
              ok: true,
              data: {
                id: 'sub_work_1',
                agent_id: 'beta',
                session_id: 'sub-session-1',
                status: 'running',
              },
            },
          }),
          toolItem({
            id: 'call-subagent-other',
            name: 'subagent',
            arguments: args,
            result: { ok: true, data },
          }),
        ]),
      ];
      timeline.render(sessionState);

      const subagentRows = document.querySelectorAll(
        '.subagent-tool-event .subagent-line',
      );
      expect(subagentRows).toHaveLength(1);
      expect(subagentRows[0].querySelector('.te-dot.running')).not.toBeNull();
      const callRow = Array.from(
        document.querySelectorAll(
          '.run-tool-event:not(.subagent-tool-event) .tool-event-line',
        ),
      ).find((row) => row.querySelector('.te-fn')?.textContent === 'subagent');
      expect(callRow.textContent).toContain(label);
      expect(callRow.textContent).not.toContain(t('chat.subagent.label'));
      expect(
        callRow.querySelector('.subagent-link, [data-cancel="subagent"]'),
      ).toBeNull();
    },
  );

  it('renders a message to an existing Sub-Agent with a link to its Session and no cancel', () => {
    const onNavigateToSubAgent = vi.fn();
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun('run-subagent-send', [
        toolItem({
          id: 'call-send',
          name: 'subagent',
          arguments: {
            action: 'send',
            id: 'sub_work_1',
            content: 'Also check the tests',
          },
          subAgentSession: {
            id: 'sub_work_1',
            agent_id: 'beta',
            session_id: 'sub-session-1',
            status: 'running',
          },
          result: {
            ok: true,
            data: {
              id: 'sub_work_1',
              agent_id: 'beta',
              session_id: 'sub-session-1',
              status: 'steered',
            },
          },
        }),
      ]),
    ];
    // The Sub-Agent's own work shows on the row that started it; the send row
    // reports its delivery.
    timeline.render(sessionState, {
      onNavigateToSubAgent,
      subAgentStatuses: { 'session:beta::sub-session-1': 'running' },
    });

    expect(subAgentLine().querySelector('.te-fn').textContent.trim()).toBe(
      t('chat.subagent.sendLabel'),
    );
    expect(
      subAgentLine().querySelector('.subagent-agent').textContent,
    ).toContain('beta');
    expect(
      subAgentLine().querySelector('.subagent-preview').textContent,
    ).toContain('Also check the tests');
    expect(subAgentLine().querySelector('.te-dot.done')).not.toBeNull();
    expect(subAgentLine().querySelector('[data-cancel="subagent"]')).toBeNull();

    subAgentLine().querySelector('.subagent-link').click();
    flushSync();
    expect(onNavigateToSubAgent).toHaveBeenCalledWith({
      agentId: 'beta',
      sessionId: 'sub-session-1',
    });
  });

  it('keeps a Sub-Agent row running while its Child Run runs', () => {
    timeline.render(subAgentSession());

    expect(
      subAgentLine()
        .querySelector('.subagent-session-action')
        .getAttribute('aria-label'),
    ).toBe(t('chat.subagent.openSession'));
    expect(subAgentLine().querySelector('.te-dot.running')).not.toBeNull();
    expect(subAgentLine().querySelector('.te-dot.done')).toBeNull();
  });

  it.each([
    ['no time without a tracked runtime', {}, null],
    ['the Child Run runtime', { 'runDuration:sub-run': 4200 }, '4.2s'],
  ])(
    'settles a Sub-Agent row when its Run completes, showing %s',
    (_case, timing, time) => {
      timeline.render(subAgentSession(), {
        subAgentStatuses: { 'run:sub-run': 'completed', ...timing },
      });

      expect(subAgentLine().querySelector('.te-dot.done')).not.toBeNull();
      expect(subAgentLine().querySelector('.te-dot.running')).toBeNull();
      expect(
        subAgentLine().querySelector('.te-time')?.textContent.trim() ?? null,
      ).toBe(time);
    },
  );

  it('shows the Child Run live runtime instead of the spawn Tool duration', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-05T18:00:06.000Z'));
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun('run-parent', [
        toolItem({
          id: 'call-subagent',
          name: 'subagent',
          arguments: RUN_ARGUMENTS,
          result: {
            ok: true,
            data: {
              agent_id: 'beta',
              session_id: 'sub-session',
              run_id: 'sub-run',
              status: 'running',
            },
          },
          timing: { duration_ms: 50 },
        }),
      ]),
    ];
    timeline.render(sessionState, {
      subAgentStatuses: {
        'run:sub-run': 'running',
        'runStarted:sub-run': '2026-08-05T18:00:01.000Z',
      },
    });

    const timeLabel = subAgentLine().querySelector('.te-time');
    expect(timeLabel.textContent).toBe('5.0s');

    await vi.advanceTimersByTimeAsync(500);
    flushSync();
    expect(timeLabel.textContent).toBe('5.5s');
  });

  it.each([
    [
      'its spawn result',
      {
        result: {
          ok: true,
          data: {
            agent_id: 'beta',
            session_id: 'sub-session-1',
            status: 'running',
          },
        },
      },
      'sub-session-1',
    ],
    [
      'the Session start before the Tool result',
      {
        subAgentSession: {
          agent_id: 'beta',
          session_id: 'sub-session-running',
          run_id: 'sub-run-running',
          status: 'running',
        },
      },
      'sub-session-running',
    ],
  ])(
    'navigates to the sub-agent Session named by %s',
    (_case, target, sessionId) => {
      const onNavigateToSubAgent = vi.fn();
      const sessionState = timelineSession();
      sessionState.timelineItems = [
        assistantRun('run-subagent-link', [
          toolItem({
            id: 'call-subagent',
            name: 'subagent',
            arguments: RUN_ARGUMENTS,
            ...target,
          }),
        ]),
      ];
      timeline.render(sessionState, { onNavigateToSubAgent });

      expect(document.querySelector('.subagent-tool-event').open).toBe(false);
      document.querySelector('.subagent-link').click();
      flushSync();

      expect(onNavigateToSubAgent).toHaveBeenCalledWith({
        agentId: 'beta',
        sessionId,
      });
    },
  );

  it('renders a Sub-Agent call cancelled before its result as cancelled after History reload', () => {
    const sessionState = timelineSession();
    sessionState.timelineItems = [
      assistantRun(
        'run-parent',
        [
          toolItem({
            id: 'call-subagent',
            name: 'subagent',
            arguments: {
              action: 'run',
              agent_id: 'researcher',
              content: 'Research the APIs',
            },
            status: 'cancelled',
          }),
        ],
        { status: 'cancelled', source: 'history' },
      ),
    ];
    timeline.render(sessionState);

    const subAgentRow = document.querySelector('.subagent-tool-event');
    expect(subAgentRow.textContent).toContain(t('chat.toolCancelled'));
    expect(subAgentRow.querySelector('.te-dot.cancelled')).not.toBeNull();
    expect(subAgentRow.querySelector('.subagent-link')).toBeNull();
    expect(subAgentRow.querySelector('[data-cancel="subagent"]')).toBeNull();
  });

  it.each([
    [
      'a sub-agent before its Session target',
      toolItem({
        id: 'call-subagent',
        name: 'subagent',
        arguments: RUN_ARGUMENTS,
      }),
    ],
    [
      'a streamed sub-agent call that is still preparing',
      toolItem({
        id: 'call-subagent',
        name: 'subagent',
        status: 'preparing',
        streaming: true,
        partialArgumentsText: '{"agent_id":"beta"',
        startedEvent: null,
      }),
    ],
  ])('renders no sub-agent card for %s', (_case, event) => {
    const sessionState = timelineSession();
    sessionState.timelineItems = [assistantRun('run-no-card', [event])];
    timeline.render(sessionState);

    expect(
      document.querySelector(
        '.subagent-tool-event, .subagent-link, .streaming-tool-event',
      ),
    ).toBeNull();
  });
});
