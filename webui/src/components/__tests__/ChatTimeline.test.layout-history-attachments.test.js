// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  tick,
  ChatTimeline,
  setupTimelineLayoutSuite,
} from './ChatTimeline.layout-history-attachments.support.js';
import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
} from '../../lib/chatState.js';

import {
  waitForCondition,
  mockScrollGeometry,
} from './ChatTimeline.support.js';

describe('ChatTimeline', () => {
  const suite = setupTimelineLayoutSuite();

  it('wraps messages in a capped, centered measure column', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-measure-wrapper',
    );
    sessionState.messages = [
      {
        id: 'user-one',
        role: 'user',
        content: 'Hello',
        timestamp: '2026-05-10T09:00:00',
      },
    ];

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    // `.messages` is the full-width scroll container; `.messages__content` is
    // the inner column capped/centered via `--chat-measure` (driven by
    // `data-chat-width` on `.chat-view`, see ChatView). The wrapper must exist
    // and hold the rendered messages.
    const scrollContainer = document.querySelector('.messages');
    const measureColumn = document.querySelector('.messages__content');
    expect(scrollContainer).toBeTruthy();
    expect(measureColumn).toBeTruthy();
    expect(scrollContainer.contains(measureColumn)).toBe(true);
    expect(measureColumn.querySelector('.msg.user')).toBeTruthy();
  });

  it('reports a scrollbar that appears after the timeline mounts', async () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-late-scrollbar',
    );
    const onScrollbarWidthChange = vi.fn();

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        onScrollbarWidthChange,
      },
    });
    flushSync();

    const scrollContainer = document.querySelector('.messages');
    Object.defineProperty(scrollContainer, 'offsetWidth', {
      configurable: true,
      get: () => 1000,
    });
    Object.defineProperty(scrollContainer, 'clientWidth', {
      configurable: true,
      get: () => 986,
    });

    suite.notifyContentResize();
    await tick();

    expect(onScrollbarWidthChange).toHaveBeenLastCalledWith(14);
  });

  it('does not show a date separator for a single-day history', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-single-day-history',
    );
    sessionState.messages = [
      {
        id: 'user-one',
        role: 'user',
        content: 'Morning note',
        timestamp: '2026-05-10T09:00:00',
      },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'Same day reply',
        timestamp: '2026-05-10T09:01:00',
      },
    ];

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.querySelector('.date-sep:not(.compaction-sep)')).toBeNull();
  });

  it('renders a live Compaction divider between its surrounding Run output', () => {
    const summaryText =
      '\n# Exact compaction summary\n\n<tag> stays text & *stars* stay literal\n';
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-live-compaction',
    );
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-compaction',
      sequence: 1,
      payload: {
        message: {
          id: 'assistant-before',
          role: 'assistant',
          content: 'Before checkpoint',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'compaction_started',
      run_id: 'run-compaction',
      sequence: 2,
      payload: {
        context_tokens_before: 250_000,
      },
    });
    appendRunEvent(sessionState, {
      type: 'compaction_completed',
      run_id: 'run-compaction',
      sequence: 3,
      payload: {
        context_tokens_before: 250_000,
        context_tokens_after: 30_000,
        message: {
          id: 'checkpoint-live',
          role: 'compaction_checkpoint',
          content: summaryText,
          timestamp: '2026-07-29T17:55:25Z',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-compaction',
      sequence: 4,
      payload: {
        message: {
          id: 'assistant-after',
          role: 'assistant',
          content: 'After checkpoint',
        },
      },
    });

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const divider = document.querySelector('.run-compaction-sep');
    const outputs = Array.from(document.querySelectorAll('.msg-markdown'));
    const before = outputs.find((element) =>
      element.textContent.includes('Before checkpoint'),
    );
    const after = outputs.find((element) =>
      element.textContent.includes('After checkpoint'),
    );

    expect(divider?.textContent.trim()).toBe(
      'Context compacted · ~250k → ~30k',
    );
    expect(divider?.classList.contains('compaction-sep--running')).toBe(false);
    const disclosure = document.querySelector('.compaction-disclosure--in-run');
    expect(disclosure?.open).toBe(false);
    expect(disclosure?.querySelector('summary')).toBe(divider);
    expect(
      disclosure?.querySelector('.compaction-detail__text').textContent,
    ).toBe(summaryText);
    divider.click();
    flushSync();
    expect(disclosure.open).toBe(true);
    expect(
      before.compareDocumentPosition(divider) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      divider.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('renders the running Compaction state before a checkpoint exists', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-running-compaction',
    );
    appendRunEvent(sessionState, {
      type: 'compaction_started',
      run_id: 'run-compaction',
      sequence: 1,
      payload: {
        context_tokens_before: 250_000,
      },
    });

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    // A run whose only child is the Compaction divider renders the divider
    // bare - no run header or footer around it.
    const divider = document.querySelector('.compaction-sep');
    expect(divider?.textContent.trim()).toBe(
      'Compacting current conversation…',
    );
    expect(divider?.classList.contains('compaction-sep--running')).toBe(true);
    expect(divider?.getAttribute('aria-busy')).toBe('true');
    expect(document.querySelector('.compaction-disclosure')).toBeNull();
    expect(document.querySelector('.assistant-run')).toBeNull();
  });

  it('keeps persisted Compaction token counts after History reload', () => {
    const summaryText =
      'Remember this exactly:\n\n- first fact\n- <literal tag>\n\nTrailing line.';
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-history-compaction',
    );
    loadHistory(sessionState, [
      {
        id: 'checkpoint-history',
        role: 'compaction_checkpoint',
        content: summaryText,
        timestamp: '2026-07-29T17:55:25Z',
        usage: {
          compacted_token_count: 220_000,
          context_tokens_before: 250_000,
          context_tokens_after: 30_000,
        },
      },
    ]);

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const divider = document.querySelector('.compaction-sep');
    expect(divider?.textContent.trim()).toBe(
      'Context compacted · ~250k → ~30k',
    );
    expect(divider?.tagName).toBe('SUMMARY');
    const disclosure = document.querySelector('.compaction-disclosure');
    expect(disclosure?.open).toBe(false);
    divider.click();
    flushSync();
    expect(disclosure.open).toBe(true);
    expect(
      disclosure.querySelector('.compaction-detail__text').textContent,
    ).toBe(summaryText);
  });

  it('groups multi-day history with Today for the current day', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-05-11T12:00:00'));

    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-multi-day-history',
    );
    sessionState.messages = [
      {
        id: 'user-yesterday',
        role: 'user',
        content: 'Yesterday question',
        timestamp: '2026-05-10T15:00:00',
      },
      {
        id: 'assistant-yesterday',
        role: 'assistant',
        content: 'Yesterday answer',
        timestamp: '2026-05-10T15:01:00',
      },
      {
        id: 'user-today',
        role: 'user',
        content: 'Continue today',
        timestamp: '2026-05-11T08:00:00',
      },
    ];

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const dateSeparators = Array.from(
      document.querySelectorAll('.date-sep:not(.compaction-sep)'),
    );

    expect(dateSeparators).toHaveLength(2);
    expect(dateSeparators[0].textContent.trim()).not.toBe('Today');
    expect(dateSeparators[1].textContent.trim()).toBe('Today');
  });

  it('loads older history at the top and preserves the scroll anchor', async () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-load-older-anchor',
    );
    sessionState.messages = [
      {
        id: 'message-older-boundary',
        role: 'user',
        content: 'Oldest loaded message',
        timestamp: '2026-05-10T09:00:00',
      },
    ];
    let scrollHeight = 1000;
    const onLoadOlder = vi.fn(async () => {
      scrollHeight = 1400;
      return true;
    });

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        hasOlderHistory: true,
        onLoadOlder,
      },
    });
    flushSync();

    const messages = document.querySelector('.messages');
    Object.defineProperty(messages, 'scrollHeight', {
      configurable: true,
      get: () => scrollHeight,
    });
    Object.defineProperty(messages, 'offsetHeight', {
      configurable: true,
      get: () => 500,
    });
    Object.defineProperty(messages, 'scrollTop', {
      configurable: true,
      writable: true,
      value: 0,
    });

    await tick();
    messages.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
    messages.dispatchEvent(new Event('scroll'));

    await waitForCondition(
      () => onLoadOlder.mock.calls.length === 1 && messages.scrollTop === 400,
    );
  });

  it('pins to the bottom when a turn is submitted and follows new content', async () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-submitted-turn-pin',
    );
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-submitted-turn-pin',
      sequence: 1,
      payload: {
        message: {
          id: 'user-submitted-turn-pin',
          role: 'user',
          content: 'Fresh turn',
          timestamp: '2026-05-11T08:00:00',
        },
      },
    });

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        submittedTurnScrollKey: 1,
      },
    });
    flushSync();

    const container = document.querySelector('.messages');
    const { currentScrollTop, setScrollHeight } = mockScrollGeometry(container);
    await waitForCondition(() => currentScrollTop() === 2000);

    // No spacer element — the old submitted-turn scroll mechanism is gone.
    expect(document.querySelector('.submitted-turn-scroll-spacer')).toBeNull();

    // Content grows (assistant response streaming in); the pinned viewport
    // follows the live tail.
    setScrollHeight(2400);
    suite.notifyContentResize();
    await waitForCondition(() => currentScrollTop() === 2400);
  });
});
