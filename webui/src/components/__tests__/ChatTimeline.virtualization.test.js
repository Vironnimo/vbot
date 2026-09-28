// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  appendEvents,
  flushSync,
  reportedMultiStepMessages,
  setupChatTimelineSuite,
  timelineSession,
  userPersisted,
} from './ChatTimeline.support.js';
import {
  installFakeLayout,
  mountedRow,
  mountedRowIds,
} from './ChatTimeline.layout.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import { createChatState } from '../../lib/chatState.js';
import { t } from '../../lib/i18n.js';

const VIEWPORT_HEIGHT = 500;

// Row heights that differ from the layout model's per-kind estimates and
// from each other, so no estimate is ever exact.
function rowHeight(element) {
  const id = element.dataset.timelineItemId;
  const number = Number.parseInt(id.match(/\d+$/)?.[0] ?? '0', 10);
  if (id.startsWith('assistant-run-')) {
    return 80 + element.textContent.length;
  }
  if (id.startsWith('history-run-')) {
    return 180 + (number % 7) * 45;
  }
  return 40 + (number % 5) * 25;
}

// `turns` History turns: a user message and an answering Run each.
function historyTurns(turns, prefix = '') {
  return Array.from({ length: turns }, (_, index) => [
    {
      id: `${prefix}user-${index}`,
      role: 'user',
      content: `Question ${index}`,
      timestamp: '2026-05-10T09:00:00',
    },
    {
      id: `${prefix}answer-${index}`,
      role: 'assistant',
      content: `Answer ${index}`,
      timestamp: '2026-05-10T09:00:30',
    },
  ]).flat();
}

function sessionWithHistory(sessionId, messages, chatState) {
  const sessionState = timelineSession(sessionId, chatState);
  sessionState.messages = messages;
  return sessionState;
}

function expectSameTop(top, expected) {
  expect(Math.abs(top - expected)).toBeLessThanOrEqual(1);
}

function spacers() {
  return Array.from(document.querySelectorAll('[data-timeline-spacer]'));
}

describe('ChatTimeline virtualization', () => {
  const timeline = setupChatTimelineSuite();
  let layout = null;

  afterEach(() => {
    layout?.uninstall();
    layout = null;
  });

  async function mountWithLayout(props) {
    layout = installFakeLayout({ viewportHeight: VIEWPORT_HEIGHT, rowHeight });
    const bag = reactiveProps({ agentName: 'Alpha', ...props });
    timeline.mount(bag);
    await layout.settle();
    return bag;
  }

  // Scrolls as the user in `step` px moves until `done()`, checking after
  // every move that the row the user saw at the viewport top stays there
  // once the window has caught up. Corrections skip sub-pixel shifts, and at
  // the very top nothing can stay above the first row, so a position clamped
  // there is exempt.
  async function scrollInSteps(step, done) {
    for (let moves = 0; moves < 100 && !done(); moves += 1) {
      layout.scrollTo(layout.scrollTop() + step);
      const anchorId = layout.firstVisibleRowId();
      const anchorTop = anchorId === null ? null : layout.rowTop(anchorId);
      await layout.settle(2);
      if (anchorId !== null && layout.scrollTop() > 0) {
        expectSameTop(layout.rowTop(anchorId), anchorTop);
      }
    }
    expect(done()).toBe(true);
  }

  it('mounts every row while the timeline has no layout', () => {
    timeline.render(sessionWithHistory('session-flat', historyTurns(20)));

    expect(mountedRowIds()).toHaveLength(40);
    expect(spacers()).toHaveLength(0);
  });

  it('mounts only the rows around the viewport and follows the streaming tail', async () => {
    const props = await mountWithLayout({
      sessionState: sessionWithHistory('session-long', historyTurns(50)),
    });
    // Mutations through the props bag reach the projection.
    const { sessionState } = props;

    const mounted = mountedRowIds();
    expect(mounted.length).toBeGreaterThan(3);
    expect(mounted.length).toBeLessThan(30);
    expect(mounted.at(-1)).toBe('history-run-answer-49');
    expect(mountedRow('user-0')).toBeNull();
    expect(spacers().length).toBeGreaterThan(0);
    expect(
      spacers().every(
        (spacer) => !spacer.hasAttribute('data-timeline-item-id'),
      ),
    ).toBe(true);
    expect(layout.scrollTop()).toBe(layout.maxScrollTop());

    appendEvents(sessionState, 'run-live', [
      userPersisted('user-live', 'Stream please'),
    ]);
    for (let delta = 0; delta < 5; delta += 1) {
      appendEvents(
        sessionState,
        'run-live',
        [
          {
            type: 'assistant_output_delta',
            payload: { content_delta: 'streamed text '.repeat(20) },
          },
        ],
        2 + delta,
      );
      flushSync();
      await layout.settle(2);
      expect(layout.scrollTop()).toBe(layout.maxScrollTop());
    }
    expect(mountedRowIds().at(-1)).toMatch(/^assistant-run-/);
    expect(mountedRowIds().length).toBeLessThan(30);
  });

  it('keeps the reading position while rows with unexpected heights mount above and below it', async () => {
    await mountWithLayout({
      sessionState: sessionWithHistory('session-read', historyTurns(20)),
    });

    await scrollInSteps(-440, () => layout.scrollTop() === 0);
    expect(mountedRow('user-0')).not.toBeNull();
    expect(mountedRow('history-run-answer-19')).toBeNull();

    await scrollInSteps(
      440,
      () => layout.scrollTop() === layout.maxScrollTop(),
    );
    expect(mountedRow('history-run-answer-19')).not.toBeNull();
    expect(mountedRow('user-0')).toBeNull();
  });

  it('restores a reading position whose rows were unmounted by a Session switch', async () => {
    const chatState = createChatState();
    const first = sessionWithHistory(
      'session-first',
      historyTurns(100),
      chatState,
    );
    const second = sessionWithHistory(
      'session-second',
      historyTurns(40, 'other-'),
      chatState,
    );
    const props = await mountWithLayout({ sessionState: first });
    layout.scrollTo(Math.round(layout.maxScrollTop() / 2));
    await layout.settle();
    const anchorId = layout.firstVisibleRowId();
    const anchorTop = layout.rowTop(anchorId);

    props.sessionState = second;
    await layout.settle();
    expect(mountedRow(anchorId)).toBeNull();

    props.sessionState = first;
    await layout.settle();
    expectSameTop(layout.rowTop(anchorId), anchorTop);
  });

  it('keeps the reading position when older History loads above it', async () => {
    let props = null;
    const onLoadOlder = vi.fn(async () => {
      props.sessionState.messages = [
        ...historyTurns(30, 'older-'),
        ...props.sessionState.messages,
      ];
      return true;
    });
    props = await mountWithLayout({
      sessionState: sessionWithHistory(
        'session-older',
        historyTurns(30, 'recent-'),
      ),
      onLoadOlder,
    });
    await scrollInSteps(-400, () => layout.scrollTop() === 0);
    const anchorId = layout.firstVisibleRowId();
    const anchorTop = layout.rowTop(anchorId);

    props.hasOlderHistory = true;
    flushSync();
    layout.scrollTo(0, { upward: true });
    await layout.settle();

    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    expect(anchorId).toBe('recent-user-0');
    expectSameTop(layout.rowTop(anchorId), anchorTop);
    expect(layout.scrollTop()).toBeGreaterThan(0);
  });

  it('keeps an expanded Tool row expanded after it leaves the window and returns', async () => {
    await mountWithLayout({
      sessionState: sessionWithHistory('session-disclosure', [
        ...reportedMultiStepMessages(),
        ...historyTurns(20),
      ]),
    });
    await scrollInSteps(-900, () => layout.scrollTop() === 0);
    const toolRow = () =>
      mountedRow('history-run-assistant-glob')?.querySelector(
        '.run-tool-event',
      );
    toolRow().open = true;
    toolRow().dispatchEvent(new Event('toggle'));
    flushSync();

    await scrollInSteps(
      900,
      () => layout.scrollTop() === layout.maxScrollTop(),
    );
    expect(toolRow()).toBeUndefined();
    await scrollInSteps(-900, () => layout.scrollTop() === 0);

    expect(toolRow().open).toBe(true);
  });

  describe('rows with ongoing interaction', () => {
    const speechUrl = '/api/speech/artifacts/aud_virtual';
    const speechMessages = [
      { id: 'speech-user', role: 'user', content: 'Read it aloud' },
      {
        id: 'speech-call',
        role: 'assistant',
        tool_calls: [
          {
            id: 'call-speech',
            name: 'text_to_speech',
            arguments: { text: 'test-owned speech' },
          },
        ],
      },
      {
        id: 'speech-result',
        role: 'tool',
        tool_call_id: 'call-speech',
        name: 'text_to_speech',
        content: JSON.stringify({
          ok: true,
          error: null,
          data: {
            artifact: { id: 'aud_virtual', kind: 'speech', url: speechUrl },
          },
          artifacts: [],
        }),
      },
      { id: 'speech-answer', role: 'assistant', content: 'Spoken.' },
    ];

    function editableHistory() {
      return [
        {
          id: 'edit-user',
          role: 'user',
          content: 'Original question',
          editable: true,
        },
        ...speechMessages,
        ...historyTurns(20),
      ];
    }

    function mockPlayback() {
      vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(
        function play() {
          Object.defineProperty(this, 'paused', {
            configurable: true,
            value: false,
          });
          this.dispatchEvent(new Event('play'));
          return Promise.resolve();
        },
      );
      vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(
        function pause() {
          Object.defineProperty(this, 'paused', {
            configurable: true,
            value: true,
          });
          this.dispatchEvent(new Event('pause'));
        },
      );
      vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
    }

    afterEach(() => {
      vi.restoreAllMocks();
    });

    function control(row, key) {
      return row.querySelector(`[aria-label="${t(key)}"]`);
    }

    it.each([
      [
        'an inline edit',
        'edit-user',
        (row) => {
          control(row, 'chat.editMessage').click();
          flushSync();
          document.activeElement?.blur();
          return () => {
            Array.from(row.querySelectorAll('button'))
              .find(
                (button) => button.textContent.trim() === t('common.cancel'),
              )
              .click();
          };
        },
      ],
      [
        'keyboard focus',
        'edit-user',
        (row) => {
          control(row, 'chat.editMessage').focus();
          return () => control(row, 'chat.editMessage').blur();
        },
      ],
      [
        'a playing speech result',
        'history-run-speech-call',
        (row) => {
          mockPlayback();
          control(row, 'audio.play').click();
          return () => control(row, 'audio.pause').click();
        },
      ],
    ])(
      'keeps a row with %s mounted outside the window until it ends',
      async (_case, rowId, begin) => {
        await mountWithLayout({
          sessionState: sessionWithHistory('session-held', editableHistory()),
        });
        await scrollInSteps(-900, () => layout.scrollTop() === 0);
        const row = mountedRow(rowId);
        const end = begin(row);
        flushSync();

        await scrollInSteps(
          900,
          () => layout.scrollTop() === layout.maxScrollTop(),
        );
        expect(mountedRow(rowId)).toBe(row);
        expect(mountedRow('user-0')).toBeNull();

        end();
        await layout.settle();
        expect(mountedRow(rowId)).toBeNull();
      },
    );
  });
});
