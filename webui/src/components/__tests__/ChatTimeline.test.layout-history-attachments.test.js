// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';

import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
} from '../../lib/chatState.js';
import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatTimeline } = await import('../ChatTimeline.svelte');
import { waitForCondition } from './ChatTimeline.support.js';

describe('ChatTimeline', () => {
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
    vi.useRealTimers();
  });

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

    mountedComponent = mount(ChatTimeline, {
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

    mountedComponent = mount(ChatTimeline, {
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

    mountedComponent = mount(ChatTimeline, {
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

    mountedComponent = mount(ChatTimeline, {
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

    mountedComponent = mount(ChatTimeline, {
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

    mountedComponent = mount(ChatTimeline, {
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

  it('anchors a transient card after the timeline item it followed', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-transient-anchor',
    );
    sessionState.messages = [
      {
        id: 'user-first',
        role: 'user',
        content: 'First question',
        timestamp: '2026-05-10T09:00:00',
      },
      {
        id: 'user-second',
        role: 'user',
        content: 'Second question',
        timestamp: '2026-05-10T09:05:00',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        transientCards: [
          {
            id: 'transient-1',
            text: 'Command output body',
            anchorId: 'user-first',
          },
        ],
      },
    });
    flushSync();

    const card = document.querySelector('.transient-card');
    const userMessages = Array.from(document.querySelectorAll('.msg.user'));
    const firstMessage = userMessages.find((element) =>
      element.textContent.includes('First question'),
    );
    const secondMessage = userMessages.find((element) =>
      element.textContent.includes('Second question'),
    );

    expect(card).toBeTruthy();
    expect(card.textContent).toContain('Command output body');
    // The card stays anchored to the first message it followed, so the later
    // message renders below it instead of the card being pushed to the bottom.
    expect(
      firstMessage.compareDocumentPosition(card) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      card.compareDocumentPosition(secondMessage) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('falls back to the end for a transient card whose anchor item is gone', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-transient-anchor-missing',
    );
    sessionState.messages = [
      {
        id: 'user-only',
        role: 'user',
        content: 'Only question',
        timestamp: '2026-05-10T09:00:00',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        transientCards: [
          {
            id: 'transient-orphan',
            text: 'Orphaned output',
            anchorId: 'item-that-no-longer-exists',
          },
        ],
      },
    });
    flushSync();

    const card = document.querySelector('.transient-card');
    const message = document.querySelector('.msg.user');
    expect(card).toBeTruthy();
    expect(card.textContent).toContain('Orphaned output');
    // A stale anchor keeps the card visible at the end rather than dropping it.
    expect(
      message.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('keeps a transient card whose anchor is gone at its chronological position', () => {
    // A history reload replaces live Run ids with history ids, so the card's
    // exact anchor disappears. With a creation time recorded, the card stays
    // between the messages that predate and postdate the command instead of
    // sinking below everything.
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-transient-anchor-reanchor',
    );
    sessionState.messages = [
      {
        id: 'user-early',
        role: 'user',
        content: 'Earlier question',
        timestamp: '2026-05-10T09:00:00',
      },
      {
        id: 'user-late',
        role: 'user',
        content: 'Later question',
        timestamp: '2026-05-10T09:10:00',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        transientCards: [
          {
            id: 'transient-mid',
            text: 'Status output',
            anchorId: 'run-live-that-vanished',
            // Same naive-local convention as the message timestamps above, so
            // the comparison is timezone-independent.
            createdAt: Date.parse('2026-05-10T09:05:00'),
          },
        ],
      },
    });
    flushSync();

    const earlyMessage = document.querySelector(
      '[data-timeline-item-id="user-early"]',
    );
    const lateMessage = document.querySelector(
      '[data-timeline-item-id="user-late"]',
    );
    const card = document.querySelector('.transient-card');
    expect(card.textContent).toContain('Status output');
    expect(
      earlyMessage.compareDocumentPosition(card) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      card.compareDocumentPosition(lateMessage) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('renders persisted run and tool durations after history load', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-render-timing',
    );
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

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.querySelector('.assistant-run').textContent).toContain(
      '1.3s',
    );
    expect(document.querySelector('.run-tool-event').textContent).toContain(
      '1.3s',
    );
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

    mountedComponent = mount(ChatTimeline, {
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

  it('scrolls the submitted user turn to the top when requested', async () => {
    const scrollToSpy = vi.fn();
    const originalScrollTo = Element.prototype.scrollTo;
    Element.prototype.scrollTo = scrollToSpy;

    try {
      const sessionState = ensureSessionState(
        createChatState(),
        'alpha',
        'session-submitted-turn-scroll',
      );
      appendRunEvent(sessionState, {
        type: 'user_message_persisted',
        run_id: 'run-submitted-turn-scroll',
        sequence: 1,
        payload: {
          message: {
            id: 'user-submitted-turn',
            role: 'user',
            content: 'Fresh turn should start the viewport',
            timestamp: '2026-05-11T08:00:00',
          },
        },
      });

      mountedComponent = mount(ChatTimeline, {
        target: document.body,
        props: {
          sessionState,
          agentName: 'Alpha',
          submittedTurnScrollKey: 1,
          submittedTurnScrollRunId: 'run-submitted-turn-scroll',
        },
      });
      flushSync();

      await waitForCondition(() => scrollToSpy.mock.calls.length > 0);

      expect(scrollToSpy).toHaveBeenCalledWith(
        expect.objectContaining({ behavior: 'smooth' }),
      );
      // Layout-less jsdom clamps every rect to zero, so the aligned position
      // resolves to the top of the content.
      expect(scrollToSpy.mock.calls[0][0].top).toBe(0);
      expect(
        document.querySelector('.submitted-turn-scroll-spacer'),
      ).toBeTruthy();
    } finally {
      if (originalScrollTo) {
        Element.prototype.scrollTo = originalScrollTo;
      } else {
        delete Element.prototype.scrollTo;
      }
    }
  });

  it('reserves the floating composer overlay height when aligning the submitted turn', async () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-submitted-turn-overlay',
    );
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-submitted-turn-overlay',
      sequence: 1,
      payload: {
        message: {
          id: 'user-submitted-turn-overlay',
          role: 'user',
          content: 'Turn aligned above the floating composer',
          timestamp: '2026-05-11T08:00:00',
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        submittedTurnScrollKey: 1,
        submittedTurnScrollRunId: 'run-submitted-turn-overlay',
        bottomOverlayHeight: 200,
      },
    });
    flushSync();

    // The composer overlays the bottom 200px of a 600px viewport, so only
    // 400px of unobstructed space count; the 300px of content needs a
    // 100px spacer for the top alignment to be reachable.
    const messages = document.querySelector('.messages');
    Object.defineProperty(messages, 'scrollHeight', {
      configurable: true,
      get: () => 300,
    });
    Object.defineProperty(messages, 'clientHeight', {
      configurable: true,
      get: () => 600,
    });
    Object.defineProperty(messages, 'scrollTop', {
      configurable: true,
      writable: true,
      value: 0,
    });

    await waitForCondition(
      () =>
        document.querySelector('.submitted-turn-scroll-spacer')?.style
          .height === '100px',
    );
  });

  it('waits for the submitted run user event instead of scrolling the previous user message', async () => {
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;

    try {
      const sessionState = ensureSessionState(
        createChatState(),
        'alpha',
        'session-submitted-turn-waits-for-run-user',
      );
      sessionState.messages = [
        {
          id: 'previous-user-message',
          role: 'user',
          content: 'Previous user message',
          timestamp: '2026-05-10T08:00:00',
        },
      ];

      mountedComponent = mount(ChatTimeline, {
        target: document.body,
        props: {
          sessionState,
          agentName: 'Alpha',
          submittedTurnScrollKey: 1,
          submittedTurnScrollRunId: 'run-new-turn-not-rendered-yet',
        },
      });
      flushSync();
      await tick();
      await tick();

      expect(scrollIntoView).not.toHaveBeenCalled();
      expect(
        document.querySelector('.submitted-turn-scroll-spacer'),
      ).toBeNull();
    } finally {
      if (originalScrollIntoView) {
        Element.prototype.scrollIntoView = originalScrollIntoView;
      } else {
        delete Element.prototype.scrollIntoView;
      }
    }
  });

  it('renders brace-free tool details and hides internal result fields', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-one',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-one',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read_file',
          arguments: { path: 'a.txt' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-one',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read_file',
        },
        result: {
          ok: true,
          data: {
            content: 'A',
            lines: 1,
          },
          artifacts: {
            stdout_path: '/tmp/internal.json',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 3,
      payload: {
        message: { role: 'assistant', content: 'Done' },
      },
    });

    expect(() => {
      mountedComponent = mount(ChatTimeline, {
        target: document.body,
        props: {
          sessionState,
          agentName: 'Alpha',
        },
      });
      flushSync();
    }).not.toThrow();

    expect(document.body.textContent).toContain('read_file');
    expect(document.body.textContent).toContain('Done');
    expect(document.body.textContent).toContain('path');
    expect(document.body.textContent).toContain('a.txt');
    expect(document.body.textContent).toContain('content');
    expect(document.body.textContent).toContain('A');
    expect(document.body.textContent).toContain('lines');
    expect(document.body.textContent).toContain('1');
    expect(document.body.textContent).not.toContain('artifacts');
    expect(document.body.textContent).not.toContain('stdout_path');

    const toolDetailRows = document.querySelectorAll(
      '.tool-event-body .teb-row',
    );
    expect(toolDetailRows).toHaveLength(2);
    expect(toolDetailRows[0].textContent).toContain('Args');
    expect(toolDetailRows[1].textContent).toContain('Result');

    const argsCode = toolDetailRows[0].querySelector('.teb-code').textContent;
    const resultCode = toolDetailRows[1].querySelector('.teb-code').textContent;
    expect(argsCode).not.toContain('{"path":"a.txt"}');
    expect(resultCode).not.toContain('{"content":"A","lines":1}');
    expect(resultCode.indexOf('content')).toBeLessThan(
      resultCode.indexOf('lines'),
    );
  });

  it('renders an automatic assistant follow-up as a separate assistant run', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-automatic-follow-up-boundary',
    );

    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-parent',
      sequence: 1,
      payload: { status: 'running' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-parent',
      sequence: 2,
      payload: {
        message: { role: 'assistant', content: 'Waiting on sub-agents.' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-parent',
      sequence: 3,
      payload: { status: 'completed' },
    });
    appendRunEvent(sessionState, {
      type: 'run_started',
      run_id: 'run-follow-up',
      sequence: 4,
      payload: { status: 'running' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-follow-up',
      sequence: 5,
      payload: { content_delta: 'Sub-agent batch finished.' },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const assistantRuns = document.querySelectorAll('.assistant-run');
    expect(assistantRuns).toHaveLength(2);
    expect(assistantRuns[0].textContent).toContain('Waiting on sub-agents.');
    expect(assistantRuns[1].textContent).toContain('Sub-agent batch finished.');

    expect(document.querySelector('.run-boundary-sep')).toBeNull();
  });

  it('renders consecutive assistant history runs as separate assistant runs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-history-follow-up-boundary',
    );

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
        content: 'Background sub-agent started.',
      },
      {
        id: 'assistant-follow-up',
        role: 'assistant',
        content: 'Background sub-agent finished.',
      },
    ]);

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const assistantRuns = document.querySelectorAll('.assistant-run');
    expect(assistantRuns).toHaveLength(2);
    expect(assistantRuns[0].textContent).toContain(
      'Background sub-agent started.',
    );
    expect(assistantRuns[1].textContent).toContain(
      'Background sub-agent finished.',
    );
    expect(document.querySelector('.run-boundary-sep')).toBeNull();
  });

  it('keeps normal user turns as separate assistant runs without a divider', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-normal-user-turns-no-boundary',
    );

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-one',
      sequence: 1,
      payload: {
        message: { id: 'user-one', role: 'user', content: 'First request' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-one',
      sequence: 2,
      payload: { message: { role: 'assistant', content: 'First answer.' } },
    });
    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-two',
      sequence: 3,
      payload: {
        message: { id: 'user-two', role: 'user', content: 'Second request' },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-two',
      sequence: 4,
      payload: { message: { role: 'assistant', content: 'Second answer.' } },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(2);
    expect(document.querySelector('.run-boundary-sep')).toBeNull();
  });

  it('renders error history messages with an error label and content', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-error-message',
    );
    sessionState.messages = [
      {
        id: 'error-one',
        role: 'error',
        error_kind: 'rate_limit',
        content: 'Provider rate limit exceeded',
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const errorMessage = document.querySelector('.msg.error');
    expect(errorMessage).toBeTruthy();
    expect(errorMessage.textContent).toContain('ERROR');
    expect(errorMessage.textContent).toContain('Provider rate limit exceeded');
  });

  it('renders image media blocks as inline images with attachment URLs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-media-block',
    );
    sessionState.messages = [
      {
        id: 'user-media-one',
        role: 'user',
        content: [
          {
            type: 'media',
            attachment_id: 'image-attachment-id',
            filename: 'diagram.png',
            media_type: 'image/png',
          },
        ],
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const image = document.querySelector('.attachment-thumb');
    expect(image).toBeTruthy();
    expect(image.getAttribute('src')).toBe(
      '/api/attachments/image-attachment-id',
    );
    expect(image.getAttribute('alt')).toBe('diagram.png');

    const imageLink = document.querySelector('.inline-attachment');
    expect(imageLink).toBeTruthy();
    expect(imageLink.getAttribute('href')).toBe(
      '/api/attachments/image-attachment-id',
    );
  });

  it('renders file blocks as attachment links without image previews', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-file-block',
    );
    sessionState.messages = [
      {
        id: 'user-file-one',
        role: 'user',
        content: [
          {
            type: 'file',
            attachment_id: 'file-attachment-id',
            filename: 'report.pdf',
            media_type: 'application/pdf',
          },
        ],
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const fileLink = document.querySelector('.inline-file-link');
    expect(fileLink).toBeTruthy();
    expect(fileLink.getAttribute('href')).toBe(
      '/api/attachments/file-attachment-id',
    );
    expect(fileLink.textContent).toContain('report.pdf');
    expect(document.querySelector('.attachment-thumb')).toBeNull();
  });

  it('renders text blocks inline instead of attachment links', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-text-block',
    );
    sessionState.messages = [
      {
        id: 'user-text-one',
        role: 'user',
        content: [
          {
            type: 'text',
            text: 'embedded text file content',
          },
        ],
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('embedded text file content');
    expect(document.querySelector('.inline-file-link')).toBeNull();
    expect(document.querySelector('.attachment-thumb')).toBeNull();
  });

  it('renders mixed text and media blocks in one user message', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-mixed-blocks',
    );
    sessionState.messages = [
      {
        id: 'user-mixed-one',
        role: 'user',
        content: [
          {
            type: 'text',
            text: 'note before image',
          },
          {
            type: 'media',
            attachment_id: 'mixed-image-id',
            filename: 'mixed.png',
            media_type: 'image/png',
          },
        ],
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('note before image');

    const image = document.querySelector('.attachment-thumb');
    expect(image).toBeTruthy();
    expect(image.getAttribute('src')).toBe('/api/attachments/mixed-image-id');
    expect(image.getAttribute('alt')).toBe('mixed.png');
  });

  it('keeps plain string user messages unchanged', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-plain-string',
    );
    sessionState.messages = [
      {
        id: 'user-plain-one',
        role: 'user',
        content: 'plain text message',
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.body.textContent).toContain('plain text message');
    expect(document.querySelector('.msg-body-blocks')).toBeNull();
    expect(document.querySelector('.inline-file-link')).toBeNull();
    expect(document.querySelector('.attachment-thumb')).toBeNull();
  });

  it('allows long unbroken user text to wrap inside the user bubble', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-user-long-token',
    );
    sessionState.messages = [
      {
        id: 'user-long-token',
        role: 'user',
        content: 'x'.repeat(240),
        timestamp: '2026-05-10T12:00:00Z',
      },
    ];

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const userBodyText = document.querySelector('.msg.user .msg-body-text');
    expect(userBodyText).toBeTruthy();
    expect(userBodyText.classList.contains('msg-body-text--user')).toBe(true);
  });
});
