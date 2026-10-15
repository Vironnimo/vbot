// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';

import {
  flushSync,
  setupChatTimelineSuite,
  timelineSession,
} from './ChatTimeline.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import { createChatState, loadHistory } from '../../lib/chatState.js';
import {
  assistantRun,
  messageItem,
  outputItem,
} from './timelineItems.support.js';
import { applyTimelineEvent } from '../../lib/chatTimeline.js';

// Canonical History rows of `turns` answered questions, as chat.history
// returns them.
function historyTurns(turns, { firstSequence = 0, prefix = 'old' } = {}) {
  return Array.from({ length: turns }, (_, turn) => [
    messageItem(
      {
        id: `${prefix}-user-${turn}`,
        role: 'user',
        content: `Question ${turn}`,
      },
      { id: `history-record-${firstSequence + turn * 2}` },
    ),
    assistantRun(
      `${prefix}-run-${turn}`,
      [outputItem(`Earlier answer ${turn}`)],
      { source: 'history', status: 'completed' },
    ),
  ]).flat();
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
    loadHistory(sessionState, [], { items: messages });
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
    loadHistory(sessionState, [], {
      items: historyTurns(3, { firstSequence: 100 }),
    });
    flushSync();
    expect(document.querySelectorAll('[data-timeline-item-id]')).toHaveLength(
      6,
    );
    expect(announced()).toBe('');

    applyTimelineEvent(sessionState, 'timeline_snapshot', {
      run_id: 'run-new',
      sequence: 3,
      items: [
        messageItem(
          { id: 'user-new', role: 'user', content: 'What changed?' },
          { runId: 'run-new' },
        ),
        assistantRun('run-new', [
          outputItem('The **timeline** now ', { streaming: true }),
        ]),
      ],
    });
    flushSync();
    expect(announced()).toBe('');

    applyTimelineEvent(sessionState, 'timeline_snapshot', {
      run_id: 'run-new',
      sequence: 5,
      items: [
        messageItem(
          { id: 'user-new', role: 'user', content: 'What changed?' },
          { runId: 'run-new' },
        ),
        assistantRun(
          'run-new',
          [outputItem('The **timeline** now mounts `visible` rows.')],
          { status: 'completed' },
        ),
      ],
    });
    flushSync();
    expect(announced()).toBe('The timeline now mounts visible rows.');

    props.transientCards = [
      { id: 'card-status', text: 'Model: fake', anchorId: 'gone' },
    ];
    flushSync();
    expect(announced()).toBe('Model: fake');

    // History takes over the announced Run: it is not announced again.
    loadHistory(sessionState, [], {
      items: JSON.parse(JSON.stringify(sessionState.timelineItems)),
      runs: [{ run_id: 'run-new', status: 'completed', complete: true }],
    });
    flushSync();
    expect(announced()).toBe('Model: fake');

    applyTimelineEvent(sessionState, 'timeline_snapshot', {
      run_id: 'run-failing',
      sequence: 1,
      items: [
        messageItem(
          { id: 'error-new', role: 'error', content: 'Provider unavailable' },
          { runId: 'run-failing' },
        ),
      ],
    });
    flushSync();
    expect(announced()).toBe('Error: Provider unavailable');

    // Older History loading above the reader is not news.
    loadHistory(sessionState, [], {
      items: historyTurns(2, { prefix: 'older' }),
      incremental: true,
      prepend: true,
    });
    flushSync();
    expect(
      document.querySelector('[data-timeline-item-id="history-record-0"]'),
    ).not.toBeNull();
    expect(announced()).toBe('Error: Provider unavailable');

    // Switching Sessions clears the region and announces none of the other
    // Session's History.
    const other = timelineSession('session-other', chatState);
    loadHistory(other, [], { items: historyTurns(4, { prefix: 'other' }) });
    props.sessionState = other;
    flushSync();
    expect(announced()).toBe('');
  });
});
