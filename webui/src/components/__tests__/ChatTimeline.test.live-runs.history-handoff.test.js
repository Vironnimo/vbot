// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  ChatTimeline,
  setupTimelineRunSuite,
} from './ChatTimeline.live-runs.support.js';
import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
} from '../../lib/chatState.js';

import {
  appendReportedLiveRunEvents,
  reportedMultiStepMessages,
} from './ChatTimeline.support.js';

describe('ChatTimeline', () => {
  const suite = setupTimelineRunSuite();

  it('renders one assistant block when reported history overlaps live events', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reported-overlap',
    );
    startRun(sessionState, {
      run_id: 'run-reported-overlap',
      sse_url: '/api/runs/run-reported-overlap/events',
      status: 'running',
    });

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-reported-overlap',
      sequence: 1,
      payload: { message: reportedMultiStepMessages()[0] },
    });
    appendReportedLiveRunEvents(sessionState, 'run-reported-overlap', 2);
    loadHistory(sessionState, reportedMultiStepMessages());

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    expect(document.querySelectorAll('.run-tool-event')).toHaveLength(2);
    expect(
      document.body.textContent.match(
        /I found the timeline helper; now I will read it\./g,
      ),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/The timeline is in chatState\.js\./g),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/Find candidate files\./g),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/Read the selected file\./g),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/Summarize the result\./g),
    ).toHaveLength(1);
  });

  it('renders one visible assistant block when persisted history overlaps an active run', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-overlap',
    );

    sessionState.status = 'running';
    sessionState.currentRun = {
      runId: 'run-overlap',
      sseUrl: '/api/runs/run-overlap/events',
      status: 'running',
    };
    sessionState.messages = [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'The file says A.',
      },
    ];

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-overlap',
      sequence: 1,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Inspect the file',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-overlap',
      sequence: 2,
      payload: {
        reasoning_delta: 'Checking',
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-overlap',
      sequence: 3,
      payload: {
        message: {
          id: 'assistant-one',
          role: 'assistant',
          content: 'The file says A.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-overlap',
      sequence: 4,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read',
          arguments: { path: 'a.txt' },
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

    const assistantRuns = document.querySelectorAll('.assistant-run');
    expect(assistantRuns).toHaveLength(1);
    expect(document.querySelectorAll('.streaming-caret')).toHaveLength(0);
    expect(document.body.textContent).toContain('The file says A.');
    expect(document.body.textContent).toContain('Checking');
  });

  it('drops still-working indicators after the active run becomes terminal history', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-terminal-overlap',
    );

    sessionState.messages = [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'The file says A.',
      },
    ];

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-terminal-overlap',
      sequence: 1,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Inspect the file',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-terminal-overlap',
      sequence: 2,
      payload: {
        message: {
          id: 'assistant-one',
          role: 'assistant',
          content: 'The file says A.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-terminal-overlap',
      sequence: 3,
      payload: {
        status: 'completed',
      },
    });
    sessionState.currentRun = {
      runId: 'run-terminal-overlap',
      sseUrl: '/api/runs/run-terminal-overlap/events',
      status: 'completed',
    };

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    expect(document.querySelectorAll('.streaming-caret')).toHaveLength(0);
  });

  it('shows cancelled status for a tool that was active when the run was cancelled', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-cancelled-tool',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-cancelled-tool',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-bash',
          index: 0,
          name: 'bash',
          arguments: { command: 'sleep 30' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_cancelled',
      run_id: 'run-cancelled-tool',
      sequence: 2,
      payload: { status: 'cancelled' },
    });

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const toolLine = document.querySelector('.tool-event-line');

    expect(toolLine?.textContent).toContain('bash');
    expect(toolLine?.textContent).toContain('cancelled');
    expect(toolLine?.querySelector('.te-dot.running')).toBeNull();
    expect(toolLine?.querySelector('.te-dot.cancelled')).not.toBeNull();
  });

  it('shows live elapsed Run and Tool time and advances from absolute timestamps', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-05T18:00:05.000Z'));
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-live-elapsed',
    );
    startRun(sessionState, {
      run_id: 'run-live-elapsed',
      status: 'running',
      started_at: '2026-08-05T18:00:00.000Z',
      events: [],
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-live-elapsed',
      sequence: 1,
      timestamp: '2026-08-05T18:00:02.500Z',
      payload: {
        tool_call: {
          id: 'call-live-elapsed',
          index: 0,
          name: 'read',
          arguments: { path: 'README.md' },
        },
      },
    });

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();

    expect(document.querySelector('.run-footer').textContent).toContain(
      'Running',
    );
    expect(document.querySelector('.run-footer').textContent).toContain('5.0s');
    expect(
      document.querySelector('.tool-event-line .te-time').textContent,
    ).toBe('2.5s');

    await vi.advanceTimersByTimeAsync(500);
    flushSync();

    expect(document.querySelector('.run-footer').textContent).toContain('5.5s');
    expect(
      document.querySelector('.tool-event-line .te-time').textContent,
    ).toBe('3.0s');
  });

  it('shows no duration while a streamed Tool call is still preparing', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-05T18:00:05.000Z'));
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-preparing-no-time',
    );
    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-preparing-no-time',
      sequence: 1,
      timestamp: '2026-08-05T18:00:00.000Z',
      payload: {
        tool_call_id: 'call-preparing-no-time',
        name_delta: 'read',
        arguments_delta: '{"path":"README.md"}',
        preview_arguments: { path: 'README.md' },
      },
    });

    suite.mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();

    const toolLine = document.querySelector('.tool-event-line');
    expect(toolLine.querySelector('.te-dot.preparing')).not.toBeNull();
    expect(toolLine.querySelector('.te-time')).toBeNull();
  });

  it('renders one assistant block when terminal events arrive after overlapping history refresh', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-terminal-history-refresh',
    );

    sessionState.currentRun = {
      runId: 'run-terminal-history-refresh',
      sseUrl: '/api/runs/run-terminal-history-refresh/events',
      status: 'running',
    };
    sessionState.status = 'completed';
    sessionState.messages = [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'The file says A.',
      },
    ];

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-terminal-history-refresh',
      sequence: 1,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Inspect the file',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-terminal-history-refresh',
      sequence: 2,
      payload: {
        message: {
          id: 'assistant-one',
          role: 'assistant',
          content: 'The file says A.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-terminal-history-refresh',
      sequence: 3,
      payload: {
        status: 'completed',
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

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    expect(document.querySelectorAll('.streaming-caret')).toHaveLength(0);
    expect(document.body.textContent.match(/The file says A\./g)).toHaveLength(
      1,
    );
  });

  it('uses completed persisted history when a follow-up run starts with its own live events', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-history-ahead',
    );

    startRun(sessionState, {
      run_id: 'run-history-ahead',
      sse_url: '/api/runs/run-history-ahead/events',
      status: 'running',
    });

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'The file says A.',
      },
    ]);

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-history-ahead',
      sequence: 1,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Inspect the file',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-history-ahead',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'read',
          arguments: { path: 'a.txt' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-history-ahead',
      sequence: 3,
      payload: {
        status: 'completed',
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

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    expect(document.querySelectorAll('.streaming-caret')).toHaveLength(0);
    expect(document.body.textContent.match(/The file says A\./g)).toHaveLength(
      1,
    );
    expect(document.body.textContent).not.toContain('read');
  });

  it('uses persisted overlap suffix rows as the single assistant block during handoff', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-history-suffix',
    );

    startRun(sessionState, {
      run_id: 'run-history-suffix',
      sse_url: '/api/runs/run-history-suffix/events',
      status: 'running',
    });

    loadHistory(sessionState, [
      { id: 'user-one', role: 'user', content: 'Inspect the file' },
      {
        id: 'assistant-tools',
        role: 'assistant',
        reasoning: 'Need to read it.',
        tool_calls: [
          {
            id: 'call-one',
            name: 'read',
            arguments: { path: 'a.txt' },
          },
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
    ]);

    appendRunEvent(sessionState, {
      type: 'user_message_persisted',
      run_id: 'run-history-suffix',
      sequence: 1,
      payload: {
        message: {
          id: 'user-one',
          role: 'user',
          content: 'Inspect the file',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'run_completed',
      run_id: 'run-history-suffix',
      sequence: 2,
      payload: {
        status: 'completed',
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

    const assistantRuns = document.querySelectorAll('.assistant-run');
    expect(assistantRuns).toHaveLength(1);
    expect(document.body.textContent).toContain('Need to read it.');
    expect(document.body.textContent).toContain('The file says A.');
    expect(document.body.textContent).toContain('read');
    expect(document.body.textContent.match(/The file says A\./g)).toHaveLength(
      1,
    );
  });
});
