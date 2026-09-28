// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';

import {
  appendEvents,
  assistantOutput,
  flushSync,
  setupChatTimelineSuite,
  timelineSession,
  userPersisted,
} from './ChatTimeline.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import { createChatState, loadHistory } from '../../lib/chatState.js';

// Canonical History rows of `turns` answered questions, as chat.history
// returns them.
function historyTurns(turns, { firstSequence = 0, prefix = 'old' } = {}) {
  return Array.from({ length: turns }, (_, turn) => {
    const sequence = firstSequence + turn * 2;
    const runId = `${prefix}-run-${turn}`;
    return [
      {
        id: `${prefix}-user-${turn}`,
        role: 'user',
        content: `Question ${turn}`,
        history_sequence: sequence,
        history_run_id: runId,
      },
      {
        id: `${prefix}-answer-${turn}`,
        role: 'assistant',
        content: `Earlier answer ${turn}`,
        history_sequence: sequence + 1,
        history_run_id: runId,
      },
    ];
  }).flat();
}

function region() {
  return document.querySelector('[role="status"][aria-live="polite"]');
}

function announced() {
  return region().textContent.trim();
}

describe('ChatTimeline announcements', () => {
  const timeline = setupChatTimelineSuite();

  function mountSession(chatState, sessionId, messages) {
    const sessionState = timelineSession(sessionId, chatState);
    loadHistory(sessionState, messages);
    const props = reactiveProps({ agentName: 'Alpha', sessionState });
    timeline.mount(props);
    return props;
  }

  it('announces only new content at the end of the displayed Session, once it is final', () => {
    const chatState = createChatState();
    const props = mountSession(chatState, 'session-news', []);
    // Mutations through the props bag reach the projection.
    const { sessionState } = props;

    expect(document.querySelector('.messages').hasAttribute('aria-live')).toBe(
      false,
    );
    // The displayed Session's History arriving is not news.
    loadHistory(sessionState, historyTurns(3, { firstSequence: 100 }));
    flushSync();
    expect(document.querySelectorAll('[data-timeline-item-id]')).toHaveLength(
      6,
    );
    expect(announced()).toBe('');

    appendEvents(sessionState, 'run-new', [
      userPersisted('user-new', 'What changed?'),
      { type: 'run_started', payload: { status: 'running' } },
      {
        type: 'assistant_output_delta',
        payload: { content_delta: 'The **timeline** now ' },
      },
    ]);
    flushSync();
    expect(announced()).toBe('');

    appendEvents(
      sessionState,
      'run-new',
      [
        assistantOutput('The **timeline** now mounts `visible` rows.'),
        { type: 'run_completed', payload: { status: 'completed' } },
      ],
      4,
    );
    flushSync();
    expect(announced()).toBe('The timeline now mounts visible rows.');

    props.transientCards = [
      { id: 'card-status', text: 'Model: fake', anchorId: 'gone' },
    ];
    flushSync();
    expect(announced()).toBe('Model: fake');

    // History takes over the announced Run: it is not announced again.
    loadHistory(
      sessionState,
      [
        ...historyTurns(3, { firstSequence: 100 }),
        {
          id: 'user-new',
          role: 'user',
          content: 'What changed?',
          history_sequence: 106,
          history_run_id: 'run-new',
        },
        {
          id: 'assistant-new',
          role: 'assistant',
          content: 'The **timeline** now mounts `visible` rows.',
          history_sequence: 107,
          history_run_id: 'run-new',
        },
      ],
      { runs: [{ run_id: 'run-new', status: 'completed', complete: true }] },
    );
    flushSync();
    expect(announced()).toBe('Model: fake');

    appendEvents(sessionState, 'run-failing', [
      {
        type: 'error_message_persisted',
        payload: {
          message: {
            id: 'error-new',
            role: 'error',
            content: 'Provider unavailable',
          },
        },
      },
    ]);
    flushSync();
    expect(announced()).toBe('Error: Provider unavailable');

    // Older History loading above the reader is not news.
    loadHistory(sessionState, historyTurns(2, { prefix: 'older' }), {
      incremental: true,
    });
    flushSync();
    expect(
      document.querySelector('[data-timeline-item-id="history-record-0"]'),
    ).not.toBeNull();
    expect(announced()).toBe('Error: Provider unavailable');

    // Switching Sessions clears the region and announces none of the other
    // Session's History.
    const other = timelineSession('session-other', chatState);
    loadHistory(other, historyTurns(4, { prefix: 'other' }));
    props.sessionState = other;
    flushSync();
    expect(announced()).toBe('');
  });
});
