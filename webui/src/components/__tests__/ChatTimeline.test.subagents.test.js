// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

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

  it('updates the spawn row while rendering subagent_result as an ordinary tool call', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-subagent-status',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-subagent-status',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
          arguments: {
            agent_id: 'beta',
            content: 'Inspect the logs',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-subagent-status',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
        },
        result: {
          ok: true,
          data: {
            agent_id: 'beta',
            session_id: 'sub-session-1',
            run_id: 'sub-run-1',
            status: 'running',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-subagent-status',
      sequence: 3,
      payload: {
        tool_call: {
          id: 'call-subagent-result',
          index: 1,
          name: 'subagent_result',
          arguments: {
            agent_id: 'beta',
            session_id: 'sub-session-1',
            run_id: 'sub-run-1',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-subagent-status',
      sequence: 4,
      payload: {
        tool_call: {
          id: 'call-subagent-result',
          index: 1,
          name: 'subagent_result',
        },
        result: {
          ok: true,
          data: {
            agent_id: 'beta',
            session_id: 'sub-session-1',
            run_id: 'sub-run-1',
            status: 'completed',
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

    const subagentRows = document.querySelectorAll(
      '.subagent-tool-event .subagent-line',
    );
    const ordinaryToolRows = Array.from(
      document.querySelectorAll(
        '.run-tool-event:not(.subagent-tool-event) .tool-event-line',
      ),
    );
    const resultRow = ordinaryToolRows.find(
      (row) => row.querySelector('.te-fn')?.textContent === 'subagent_result',
    );

    expect(subagentRows).toHaveLength(1);
    expect(subagentRows[0].textContent).not.toContain('Status: completed');
    expect(subagentRows[0].querySelector('.subagent-status')).toBeNull();
    expect(subagentRows[0].querySelector('.te-dot.done')).not.toBeNull();
    expect(subagentRows[0].querySelector('.te-dot.running')).toBeNull();
    expect(resultRow).toBeTruthy();
    expect(resultRow.textContent).toContain('beta · sub-session-1');
    expect(resultRow.textContent).not.toContain('Sub-agent');
    expect(resultRow.querySelector('.subagent-link')).toBeNull();
    expect(resultRow.querySelector('[data-cancel="subagent"]')).toBeNull();
  });

  it('keeps a spawned background sub-agent tool row running while the child runs', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-subagent-spawn-dot',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-subagent-spawn-dot',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
          arguments: {
            agent_id: 'beta',
            background: true,
            content: 'Inspect in the background',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-subagent-spawn-dot',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
        },
        result: {
          ok: true,
          data: {
            agent_id: 'beta',
            session_id: 'sub-session-running',
            status: 'running',
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

    const subagentLine = document.querySelector(
      '.subagent-tool-event .subagent-line',
    );

    expect(subagentLine?.textContent).toContain('view session');
    expect(subagentLine?.querySelector('.te-dot.running')).not.toBeNull();
    expect(subagentLine?.querySelector('.te-dot.done')).toBeNull();
  });

  it('marks a spawned background sub-agent row done after the child run completes', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-subagent-child-complete-dot',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-subagent-child-complete-dot',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
          arguments: {
            agent_id: 'beta',
            background: true,
            content: 'Inspect in the background',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-subagent-child-complete-dot',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
        },
        result: {
          ok: true,
          data: {
            agent_id: 'beta',
            session_id: 'sub-session-running',
            run_id: 'sub-run-completed',
            status: 'running',
          },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        subAgentStatuses: {
          'run:sub-run-completed': 'completed',
        },
      },
    });
    flushSync();

    const subagentLine = document.querySelector(
      '.subagent-tool-event .subagent-line',
    );

    expect(subagentLine?.querySelector('.te-dot.done')).not.toBeNull();
    expect(subagentLine?.querySelector('.te-dot.running')).toBeNull();
  });

  function mountCompletedNonBlockingSubAgent(props = {}) {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-subagent-result-fetch',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-subagent-result-fetch',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
          arguments: {
            agent_id: 'beta',
            background: true,
            content: 'Inspect in the background',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-subagent-result-fetch',
      sequence: 2,
      payload: {
        tool_call: { id: 'call-subagent', index: 0, name: 'subagent' },
        result: {
          ok: true,
          data: {
            agent_id: 'beta',
            session_id: 'sub-session-running',
            run_id: 'sub-run-completed',
            status: 'running',
          },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        subAgentStatuses: { 'run:sub-run-completed': 'completed' },
        ...props,
      },
    });
    flushSync();
  }

  it('requests the final result when a non-blocking sub-agent run completes', () => {
    const onRequestSubAgentResult = vi.fn();

    mountCompletedNonBlockingSubAgent({ onRequestSubAgentResult });

    expect(onRequestSubAgentResult).toHaveBeenCalledWith(
      'beta',
      'sub-session-running',
      'beta::sub-session-running::sub-run-completed',
      'sub-run-completed',
    );
  });

  it('renders a fetched non-blocking sub-agent result in the tool body', () => {
    mountCompletedNonBlockingSubAgent({
      subAgentResults: {
        'beta::sub-session-running::sub-run-completed': {
          loading: false,
          result: 'Investigation complete.',
        },
      },
    });

    const body = document.querySelector(
      '.subagent-tool-event .tool-event-body',
    );

    expect(body?.textContent).toContain('Investigation complete.');
  });

  it('shows the child run runtime on a completed non-blocking sub-agent', () => {
    mountCompletedNonBlockingSubAgent({
      subAgentStatuses: {
        'run:sub-run-completed': 'completed',
        'runDuration:sub-run-completed': 4200,
      },
    });

    const timeLabel = document.querySelector(
      '.subagent-tool-event .subagent-line .te-time',
    );

    expect(timeLabel?.textContent).toContain('4.2s');
  });

  it('shows no time on a completed non-blocking sub-agent without a tracked runtime', () => {
    mountCompletedNonBlockingSubAgent();

    const timeLabel = document.querySelector(
      '.subagent-tool-event .subagent-line .te-time',
    );

    expect(timeLabel).toBeNull();
  });

  it('calls the sub-agent navigation callback with a spawned session target', () => {
    const onNavigateToSubAgent = vi.fn();
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-subagent-link',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-subagent-link',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
          arguments: {
            agent_id: 'beta',
            content: 'Inspect the logs',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: 'run-subagent-link',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
        },
        result: {
          ok: true,
          data: {
            agent_id: 'beta',
            session_id: 'sub-session-1',
            status: 'running',
          },
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        onNavigateToSubAgent,
      },
    });
    flushSync();

    const subAgentDetails = document.querySelector('.subagent-tool-event');
    expect(subAgentDetails).toBeTruthy();
    expect(subAgentDetails.open).toBe(false);

    const viewSessionButton = document.querySelector('.subagent-link');
    expect(viewSessionButton).toBeTruthy();

    viewSessionButton.click();
    flushSync();

    expect(onNavigateToSubAgent).toHaveBeenCalledWith({
      agentId: 'beta',
      sessionId: 'sub-session-1',
    });
  });

  it('shows a sub-agent session link before the tool result arrives', () => {
    const onNavigateToSubAgent = vi.fn();
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-subagent-started-link',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-subagent-started-link',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
          arguments: {
            agent_id: 'beta',
            background: false,
            content: 'Inspect slowly',
          },
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'subagent_session_started',
      run_id: 'run-subagent-started-link',
      sequence: 2,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
        },
        data: {
          agent_id: 'beta',
          session_id: 'sub-session-running',
          run_id: 'sub-run-running',
          status: 'running',
        },
      },
    });

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
        onNavigateToSubAgent,
      },
    });
    flushSync();

    const viewSessionButton = document.querySelector('.subagent-link');
    expect(viewSessionButton).toBeTruthy();

    viewSessionButton.click();
    flushSync();

    expect(onNavigateToSubAgent).toHaveBeenCalledWith({
      agentId: 'beta',
      sessionId: 'sub-session-running',
    });
  });

  it('renders a blocking sub-agent row while the session target is starting', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-blocking-subagent-starting',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-blocking-subagent-starting',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
          arguments: {
            agent_id: 'beta',
            background: false,
            content: 'Inspect slowly',
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

    const subAgentRow = document.querySelector('.subagent-tool-event');
    expect(subAgentRow).toBeTruthy();
    expect(subAgentRow.textContent).toContain('starting');
    expect(document.querySelector('.subagent-link')).toBeNull();
  });

  it('renders a cancelled blocking sub-agent as cancelled after history reload', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-blocking-subagent-cancelled',
    );
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

    mountedComponent = mount(ChatTimeline, {
      target: document.body,
      props: {
        sessionState,
        agentName: 'Alpha',
      },
    });
    flushSync();

    const subAgentRow = document.querySelector('.subagent-tool-event');
    expect(subAgentRow).toBeTruthy();
    expect(subAgentRow.textContent).toContain('cancelled');
    expect(subAgentRow.textContent).not.toContain('starting');
    expect(subAgentRow.querySelector('.te-dot.cancelled')).not.toBeNull();
    expect(subAgentRow.querySelector('[data-cancel="subagent"]')).toBeNull();
  });

  it('does not render a non-blocking sub-agent row before a session target', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-nonblocking-subagent-without-target',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: 'run-nonblocking-subagent-without-target',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-subagent',
          index: 0,
          name: 'subagent',
          arguments: {
            agent_id: 'beta',
            background: true,
            content: 'Inspect in the background',
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

    expect(document.querySelector('.subagent-tool-event')).toBeNull();
    expect(document.querySelector('.subagent-link')).toBeNull();
  });

  it('does not render streaming tool preparation as a standalone card', () => {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      'session-streaming-tool-preparation',
    );

    appendRunEvent(sessionState, {
      type: 'tool_call_delta',
      run_id: 'run-streaming-tool-preparation',
      sequence: 1,
      payload: {
        tool_call_id: 'call-streaming-tool-preparation',
        name_delta: 'subagent',
        arguments_delta: '{"agent_id":"beta"',
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

    expect(document.querySelector('.streaming-tool-event')).toBeNull();
    expect(document.querySelector('.subagent-tool-event')).toBeNull();
    expect(document.body.textContent).not.toContain('PREPARING TOOL');
  });
});
