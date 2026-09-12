// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import {
  flushSync,
  mount,
  ChatTimeline,
  setupTimelineLayoutSuite,
} from './ChatTimeline.layout-history-attachments.support.js';
import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
} from '../../lib/chatState.js';

describe('ChatTimeline', () => {
  const suite = setupTimelineLayoutSuite();

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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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
      suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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

    suite.mountedComponent = mount(ChatTimeline, {
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
});
