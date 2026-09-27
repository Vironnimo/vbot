// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import {
  appendEvents,
  flushSync,
  setupChatTimelineSuite,
  timelineSession,
  toolResult,
  toolStarted,
} from './ChatTimeline.support.js';
import { loadHistory } from '../../lib/chatState.js';
import { t } from '../../lib/i18n.js';

const BACKGROUND_ARGUMENTS = {
  agent_id: 'beta',
  background: true,
  content: 'Inspect in the background',
};

// A background sub-agent whose spawn result names Child Run `sub-run`.
function backgroundSubAgentSession() {
  const sessionState = timelineSession();
  appendEvents(sessionState, 'run-parent', [
    toolStarted('call-subagent', 'subagent', BACKGROUND_ARGUMENTS),
    toolResult('call-subagent', 'subagent', {
      ok: true,
      data: {
        agent_id: 'beta',
        session_id: 'sub-session',
        run_id: 'sub-run',
        status: 'running',
      },
    }),
  ]);
  return sessionState;
}

function subAgentLine() {
  return document.querySelector('.subagent-tool-event .subagent-line');
}

describe('ChatTimeline sub-agent rows', () => {
  const timeline = setupChatTimelineSuite();

  it('renders a sub-agent status action as an ordinary Tool call', () => {
    const sessionState = timelineSession();
    appendEvents(sessionState, 'run-subagent-status', [
      toolStarted('call-subagent', 'subagent', {
        action: 'run',
        agent_id: 'beta',
        content: 'Inspect the logs',
      }),
      toolResult('call-subagent', 'subagent', {
        ok: true,
        data: {
          id: 'sub_work_1',
          agent_id: 'beta',
          session_id: 'sub-session-1',
          status: 'running',
          delivery: 'automatic',
        },
      }),
      toolStarted('call-subagent-status', 'subagent', {
        action: 'status',
        id: 'sub_work_1',
      }),
      toolResult('call-subagent-status', 'subagent', {
        ok: true,
        data: {
          id: 'sub_work_1',
          agent_id: 'beta',
          session_id: 'sub-session-1',
          status: 'completed',
        },
      }),
    ]);
    timeline.render(sessionState);

    const subagentRows = document.querySelectorAll(
      '.subagent-tool-event .subagent-line',
    );
    expect(subagentRows).toHaveLength(1);
    expect(subagentRows[0].querySelector('.subagent-status')).toBeNull();
    expect(subagentRows[0].querySelector('.te-dot.running')).not.toBeNull();
    const statusRow = Array.from(
      document.querySelectorAll(
        '.run-tool-event:not(.subagent-tool-event) .tool-event-line',
      ),
    ).find((row) => row.querySelector('.te-fn')?.textContent === 'subagent');
    expect(statusRow.textContent).toContain('status · sub_work_1');
    expect(statusRow.textContent).not.toContain(t('chat.subagent.label'));
    expect(
      statusRow.querySelector('.subagent-link, [data-cancel="subagent"]'),
    ).toBeNull();
  });

  it('keeps a spawned background sub-agent row running while the Child Run runs', () => {
    timeline.render(backgroundSubAgentSession());

    expect(
      subAgentLine()
        .querySelector('.subagent-session-action')
        .getAttribute('aria-label'),
    ).toBe(t('chat.subagent.openSession'));
    expect(subAgentLine().querySelector('.te-dot.running')).not.toBeNull();
    expect(subAgentLine().querySelector('.te-dot.done')).toBeNull();
  });

  it('settles a completed background sub-agent without starting result recovery', () => {
    const onRequestSubAgentResult = vi.fn();
    timeline.render(backgroundSubAgentSession(), {
      subAgentStatuses: { 'run:sub-run': 'completed' },
      onRequestSubAgentResult,
    });

    expect(subAgentLine().querySelector('.te-dot.done')).not.toBeNull();
    expect(subAgentLine().querySelector('.te-dot.running')).toBeNull();
    // Without a tracked runtime the row shows no time.
    expect(subAgentLine().querySelector('.te-time')).toBeNull();
    // Recovery is the owner's job, never a presentation side effect.
    expect(onRequestSubAgentResult).not.toHaveBeenCalled();
  });

  it.each([
    [
      'the Child Run runtime',
      {
        subAgentStatuses: {
          'run:sub-run': 'completed',
          'runDuration:sub-run': 4200,
        },
      },
      '.subagent-line .te-time',
      '4.2s',
    ],
    [
      'a fetched result in the Tool body',
      {
        subAgentStatuses: { 'run:sub-run': 'completed' },
        subAgentResults: {
          'beta::sub-session::sub-run': {
            loading: false,
            result: 'Investigation complete.',
          },
        },
      },
      '.tool-event-body',
      'Investigation complete.',
    ],
  ])(
    'shows %s on a completed background sub-agent',
    (_case, props, selector, text) => {
      timeline.render(backgroundSubAgentSession(), props);

      expect(
        document.querySelector(`.subagent-tool-event ${selector}`).textContent,
      ).toContain(text);
    },
  );

  it('shows the Child Run live runtime instead of the spawn Tool duration', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-05T18:00:06.000Z'));
    const sessionState = timelineSession();
    appendEvents(sessionState, 'run-parent', [
      {
        ...toolStarted('call-subagent', 'subagent', {
          action: 'run',
          ...BACKGROUND_ARGUMENTS,
        }),
        timestamp: '2026-08-05T18:00:00.000Z',
      },
      {
        ...toolResult(
          'call-subagent',
          'subagent',
          {
            ok: true,
            data: {
              agent_id: 'beta',
              session_id: 'sub-session',
              run_id: 'sub-run',
              status: 'running',
              delivery: 'automatic',
            },
          },
          { timing: { duration_ms: 50 } },
        ),
        timestamp: '2026-08-05T18:00:00.050Z',
      },
    ]);
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
      toolResult('call-subagent', 'subagent', {
        ok: true,
        data: {
          agent_id: 'beta',
          session_id: 'sub-session-1',
          status: 'running',
        },
      }),
      'sub-session-1',
    ],
    [
      'the Session start before the Tool result',
      {
        type: 'subagent_session_started',
        payload: {
          tool_call: { id: 'call-subagent', index: 0, name: 'subagent' },
          data: {
            agent_id: 'beta',
            session_id: 'sub-session-running',
            run_id: 'sub-run-running',
            status: 'running',
          },
        },
      },
      'sub-session-running',
    ],
  ])(
    'navigates to the sub-agent Session named by %s',
    (_case, targetEvent, sessionId) => {
      const onNavigateToSubAgent = vi.fn();
      const sessionState = timelineSession();
      appendEvents(sessionState, 'run-subagent-link', [
        toolStarted('call-subagent', 'subagent', {
          agent_id: 'beta',
          background: false,
          content: 'Inspect the logs',
        }),
        targetEvent,
      ]);
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

  it('renders a blocking sub-agent row while its Session is starting', () => {
    const sessionState = timelineSession();
    appendEvents(sessionState, 'run-blocking', [
      toolStarted('call-subagent', 'subagent', {
        agent_id: 'beta',
        background: false,
        content: 'Inspect slowly',
      }),
    ]);
    timeline.render(sessionState);

    expect(
      document.querySelector('.subagent-tool-event').textContent,
    ).toContain(t('chat.subagent.starting'));
    expect(document.querySelector('.subagent-link')).toBeNull();
  });

  it('renders a cancelled blocking sub-agent as cancelled after History reload', () => {
    const sessionState = timelineSession();
    loadHistory(sessionState, [
      { id: 'user-1', role: 'user', content: 'Research the APIs' },
      {
        id: 'assistant-subagent',
        role: 'assistant',
        content: null,
        tool_calls: [
          {
            id: 'call-subagent',
            name: 'subagent',
            arguments: {
              agent_id: 'researcher',
              background: false,
              content: 'Research the APIs',
            },
          },
        ],
      },
      {
        id: 'summary-1',
        role: 'run_summary',
        run_id: 'run-parent',
        status: 'cancelled',
        timestamp: '2026-07-27T09:14:23Z',
      },
    ]);
    timeline.render(sessionState);

    const subAgentRow = document.querySelector('.subagent-tool-event');
    expect(subAgentRow.textContent).toContain(t('chat.toolCancelled'));
    expect(subAgentRow.textContent).not.toContain(t('chat.subagent.starting'));
    expect(subAgentRow.querySelector('.te-dot.cancelled')).not.toBeNull();
    expect(subAgentRow.querySelector('[data-cancel="subagent"]')).toBeNull();
  });

  it.each([
    [
      'a background sub-agent before its Session target',
      toolStarted('call-subagent', 'subagent', BACKGROUND_ARGUMENTS),
    ],
    [
      'a streamed sub-agent call that is still preparing',
      {
        type: 'tool_call_delta',
        payload: {
          tool_call_id: 'call-subagent',
          name_delta: 'subagent',
          arguments_delta: '{"agent_id":"beta"',
        },
      },
    ],
  ])('renders no sub-agent card for %s', (_case, event) => {
    const sessionState = timelineSession();
    appendEvents(sessionState, 'run-no-card', [event]);
    timeline.render(sessionState);

    expect(
      document.querySelector(
        '.subagent-tool-event, .subagent-link, .streaming-tool-event',
      ),
    ).toBeNull();
  });
});
