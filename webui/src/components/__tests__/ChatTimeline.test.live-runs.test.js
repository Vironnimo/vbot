// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
  loadHistory,
  startRun,
} from '../../lib/chatState.js';
import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: ChatTimeline } = await import('../ChatTimeline.svelte');
import {
  appendReportedLiveRunEvents,
  reportedMultiStepMessages,
} from './ChatTimeline.support.js';

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

  it('keeps thinking above later tool rows after subsequent reasoning updates', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-run-order',
    );

    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-order',
      sequence: 1,
      payload: {
        reasoning_delta: 'Thinking starts',
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-order',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-order',
          index: 0,
          name: 'read',
          arguments: { path: 'MEMORY.md' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'reasoning_delta',
      run_id: 'run-order',
      sequence: 3,
      payload: {
        reasoning_delta: ' and keeps going',
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-order',
      sequence: 4,
      payload: {
        message: { role: 'assistant', content: 'Done' },
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

    const runContent = document.querySelector('.assistant-run-content');
    const renderedChildren = Array.from(runContent.children);

    expect(renderedChildren).toHaveLength(3);
    expect(renderedChildren[0].classList.contains('reasoning-block')).toBe(
      true,
    );
    expect(renderedChildren[1].classList.contains('run-tool-event')).toBe(true);
    expect(renderedChildren[2].classList.contains('msg-markdown')).toBe(true);
    expect(renderedChildren[0].textContent).toContain(
      'Thinking starts and keeps going',
    );
    expect(renderedChildren[1].textContent).toContain('read');
    expect(renderedChildren[2].textContent).toContain('Done');
  });

  it('renders distinct assistant output phases around a tool row', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-output-tool-output',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-output-tool-output',
      sequence: 1,
      payload: {
        content_delta: 'First answer',
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-output-tool-output',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-output-tool-output',
          index: 0,
          name: 'read',
          arguments: { path: 'MEMORY.md' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-output-tool-output',
      sequence: 3,
      payload: {
        tool_call: {
          id: 'call-output-tool-output',
          index: 0,
          name: 'read',
        },
        result: {
          ok: true,
          data: { content: 'A' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-output-tool-output',
      sequence: 4,
      payload: {
        content_delta: 'Second answer',
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

    const runContent = document.querySelector('.assistant-run-content');
    const renderedChildren = Array.from(runContent.children);

    expect(renderedChildren).toHaveLength(3);
    expect(renderedChildren[0].classList.contains('msg-markdown')).toBe(true);
    expect(renderedChildren[0].textContent).toContain('First answer');
    expect(renderedChildren[1].classList.contains('run-tool-event')).toBe(true);
    expect(renderedChildren[1].textContent).toContain('read');
    expect(renderedChildren[2].classList.contains('msg-markdown')).toBe(true);
    expect(renderedChildren[2].textContent).toContain('Second answer');
  });

  it('renders reported persisted multi-step session as one assistant block', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reported-multistep',
    );

    loadHistory(sessionState, [
      {
        id: 'user-reported',
        role: 'user',
        content: 'Investigate the duplicated chat UI.',
      },
      {
        id: 'assistant-glob',
        role: 'assistant',
        reasoning: 'Find candidate files.',
        tool_calls: [
          {
            id: 'call-glob',
            name: 'glob',
            arguments: { pattern: 'webui/src/**/*.js' },
          },
        ],
      },
      {
        id: 'tool-glob',
        role: 'tool',
        tool_call_id: 'call-glob',
        name: 'glob',
        content: '{"ok":true,"data":{"content":"webui/src/lib/chatState.js"}}',
      },
      {
        id: 'assistant-read',
        role: 'assistant',
        content: 'I found the timeline helper; now I will read it.',
        reasoning: 'Read the selected file.',
        tool_calls: [
          {
            id: 'call-read',
            name: 'read',
            arguments: { path: 'webui/src/lib/chatState.js' },
          },
        ],
      },
      {
        id: 'tool-read',
        role: 'tool',
        tool_call_id: 'call-read',
        name: 'read',
        content: '{"ok":true,"data":{"content":"timeline code"}}',
      },
      {
        id: 'assistant-final',
        role: 'assistant',
        content: 'The timeline is in chatState.js.',
        reasoning: 'Summarize the result.',
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

    expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    expect(document.querySelectorAll('.run-tool-event')).toHaveLength(2);
    expect(document.querySelectorAll('.reasoning-block')).toHaveLength(3);
    expect(
      Array.from(document.querySelectorAll('.te-fn')).map(
        (element) => element.textContent,
      ),
    ).toEqual(['glob', 'read']);
    expect(document.body.textContent).not.toContain(
      'I will inspect the UI state helpers.',
    );
    expect(
      document.body.textContent.match(
        /I found the timeline helper; now I will read it\./g,
      ),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/The timeline is in chatState\.js\./g),
    ).toHaveLength(1);
  });

  it('renders reported live multi-step run content and thinking once', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-reported-live',
    );

    appendReportedLiveRunEvents(sessionState, 'run-reported-live');

    mountedComponent = mount(ChatTimeline, {
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
      Array.from(document.querySelectorAll('.te-fn')).map(
        (element) => element.textContent,
      ),
    ).toEqual(['glob', 'read']);
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

  it('renders final reasoning once before final assistant content', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-final-reasoning-draft',
    );

    appendRunEvent(sessionState, {
      type: 'reasoning',
      run_id: 'run-final-reasoning-draft',
      sequence: 1,
      payload: {
        message: {
          id: 'assistant-reasoning-draft',
          role: 'assistant',
          reasoning: 'Summarize the result.',
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output_delta',
      run_id: 'run-final-reasoning-draft',
      sequence: 2,
      payload: { content_delta: 'The timeline is in chatState.js.' },
    });
    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-final-reasoning-draft',
      sequence: 3,
      payload: {
        message: {
          id: 'assistant-final',
          role: 'assistant',
          reasoning: 'Summarize the result.',
          content: 'The timeline is in chatState.js.',
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

    const runContent = document.querySelector('.assistant-run-content');
    const renderedChildren = Array.from(runContent.children);

    expect(document.querySelectorAll('.reasoning-block')).toHaveLength(1);
    expect(renderedChildren).toHaveLength(2);
    expect(renderedChildren[0].classList.contains('reasoning-block')).toBe(
      true,
    );
    expect(renderedChildren[1].classList.contains('msg-markdown')).toBe(true);
    expect(
      document.body.textContent.match(/Summarize the result\./g),
    ).toHaveLength(1);
    expect(
      document.body.textContent.match(/The timeline is in chatState\.js\./g),
    ).toHaveLength(1);
  });

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

    mountedComponent = mount(ChatTimeline, {
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

    mountedComponent = mount(ChatTimeline, {
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

    mountedComponent = mount(ChatTimeline, {
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

    mountedComponent = mount(ChatTimeline, {
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

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();

    expect(document.querySelector('.msg-header').textContent).toContain(
      'Running',
    );
    expect(document.querySelector('.msg-header').textContent).toContain('5.0s');
    expect(
      document.querySelector('.tool-event-line .te-time').textContent,
    ).toBe('2.5s');

    await vi.advanceTimersByTimeAsync(500);
    flushSync();

    expect(document.querySelector('.msg-header').textContent).toContain('5.5s');
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

    mountedComponent = mount(ChatTimeline, {
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

    mountedComponent = mount(ChatTimeline, {
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

    mountedComponent = mount(ChatTimeline, {
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

    mountedComponent = mount(ChatTimeline, {
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

  it('renders model fallback notices inside assistant runs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-model-fallback',
    );

    appendRunEvent(sessionState, {
      type: 'model_fallback_activated',
      run_id: 'run-model-fallback',
      sequence: 1,
      payload: {
        from_model: 'openai/gpt-5',
        to_model: 'openrouter/anthropic/claude-sonnet-4',
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

    const fallbackNotice = document.querySelector(
      '.run-inline-banner.banner--info',
    );
    expect(fallbackNotice).toBeTruthy();
    expect(fallbackNotice.textContent).toContain(
      'Switched to openrouter/anthropic/claude-sonnet-4',
    );
  });

  it('keeps interrupted assistant output without a recovery marker', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-interrupted',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-interrupted',
      sequence: 1,
      payload: {
        message: {
          role: 'assistant',
          content: 'The first half of the answer',
          interrupted: true,
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

    const notice = document.querySelector('.run-inline-banner.banner--warn');
    expect(notice).toBeNull();
    expect(document.body.textContent).toContain('The first half of the answer');
  });

  it('renders no interrupted marker for a normal assistant turn', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-not-interrupted',
    );

    appendRunEvent(sessionState, {
      type: 'assistant_output',
      run_id: 'run-not-interrupted',
      sequence: 1,
      payload: {
        message: {
          role: 'assistant',
          content: 'A complete answer',
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

    expect(
      document.querySelector('.run-inline-banner.banner--warn'),
    ).toBeNull();
  });

  it('renders streamed tool stdout and stderr inside assistant runs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-tool-output',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-tool-output',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'bash',
          arguments: { command: 'printf hello' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_stdout',
      run_id: 'run-tool-output',
      sequence: 2,
      payload: { tool_call_id: 'call-one', data: 'hello\n' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_stderr',
      run_id: 'run-tool-output',
      sequence: 3,
      payload: { tool_call_id: 'call-one', data: 'warn\n' },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-tool-output',
      sequence: 4,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'bash',
        },
        result: {
          ok: true,
          data: {
            status: 'completed',
            exit_code: 0,
            output: 'hello\nwarn\n',
            truncated: false,
          },
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

    expect(document.body.textContent).toContain('Stdout');
    expect(document.body.textContent).toContain('hello');
    expect(document.body.textContent).toContain('Stderr');
    expect(document.body.textContent).toContain('warn');

    const resultRow = Array.from(document.querySelectorAll('.teb-row')).find(
      (el) => el.querySelector('.teb-label')?.textContent === 'Result',
    );
    const resultFields = Array.from(
      resultRow.querySelectorAll('.teb-field'),
    ).map((field) => ({
      key: field.querySelector('.teb-field-key')?.textContent,
      value: field.querySelector('.teb-field-value')?.textContent,
    }));
    expect(resultFields).toContainEqual({ key: 'status', value: 'completed' });
    expect(resultFields).toContainEqual({ key: 'exit_code', value: '0' });
    const resultText = resultRow.querySelector('.teb-code').textContent;
    expect(resultText).not.toContain('hello');
    expect(resultText).not.toContain('warn');
  });

  it('renders bash result output when no streamed output is available', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-bash-result-output',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-bash-result-output',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'bash',
          arguments: { command: 'printf hello' },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-bash-result-output',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-one',
          index: 0,
          name: 'bash',
        },
        result: {
          ok: true,
          data: {
            status: 'completed',
            exit_code: 0,
            output: 'hello from history\n',
            truncated: false,
          },
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

    const resultRow = Array.from(document.querySelectorAll('.teb-row')).find(
      (el) => el.querySelector('.teb-label')?.textContent === 'Result',
    );

    expect(resultRow.querySelector('.teb-code').textContent).toContain(
      'hello from history',
    );
  });

  it('renders a stable-sized thinking chevron and only rotates it when expanded', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-thinking-chevron',
    );

    appendRunEvent(sessionState, {
      type: 'reasoning',
      run_id: 'run-thinking-chevron',
      sequence: 1,
      payload: {
        message: { role: 'assistant', reasoning: 'Trace the issue' },
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

    const reasoningBlock = document.querySelector(
      '.assistant-run .reasoning-block',
    );
    const chevron = reasoningBlock.querySelector('.r-chevron');

    expect(reasoningBlock.open).toBe(false);
    expect(chevron.getAttribute('width')).toBe('10');
    expect(chevron.getAttribute('height')).toBe('10');
    expect(chevron.style.transform).toBe('none');

    reasoningBlock.open = true;
    reasoningBlock.dispatchEvent(new Event('toggle'));
    flushSync();

    expect(reasoningBlock.open).toBe(true);
    expect(chevron.getAttribute('width')).toBe('10');
    expect(chevron.getAttribute('height')).toBe('10');
    expect(chevron.style.transform).toBe('rotate(180deg)');
  });
});
