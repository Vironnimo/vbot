// @vitest-environment jsdom

import {
  describe,
  activeAgentTab,
  createAgent,
  createChatRpcMock,
  createHistoryMessages,
  expect,
  findButtonByText,
  flushSync,
  it,
  listSessionsMock,
  rpcMock,
  sendComposerMessage,
  setInputValue,
  setupChatViewTestSuite,
  subscribeRunEventsMock,
  vi,
  waitForCondition,
} from './ChatView.support.js';
import { tick } from 'svelte';

describe('ChatView', () => {
  const chatViewTest = setupChatViewTestSuite();

  it('batches run SSE deltas before updating the rendered timeline', async () => {
    const closeSubscription = vi.fn();
    subscribeRunEventsMock.mockReturnValue({
      close: closeSubscription,
      source: null,
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamResponse: {
          run_id: 'run-batched-deltas',
          sse_url: '/api/runs/run-batched-deltas/events',
          status: 'running',
          events: [],
        },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start batched stream');

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    const handlers = subscribeRunEventsMock.mock.calls[0][1];
    handlers.onEvent({
      data: {
        type: 'reasoning_delta',
        run_id: 'run-batched-deltas',
        sequence: 1,
        payload: { reasoning_delta: 'Think ' },
      },
    });
    handlers.onEvent({
      data: {
        type: 'reasoning_delta',
        run_id: 'run-batched-deltas',
        sequence: 2,
        payload: { reasoning_delta: 'fast' },
      },
    });
    flushSync();

    expect(document.body.textContent).not.toContain('Think fast');

    await waitForCondition(
      () => document.body.textContent.includes('Think fast'),
      100,
    );
  });

  it('flushes stable run events immediately so a fast sub-agent starts as running', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamResponse: {
          run_id: 'run-fast-subagent',
          sse_url: '/api/runs/run-fast-subagent/events',
          status: 'running',
          events: [],
        },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start fast sub-agent');

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    const handlers = subscribeRunEventsMock.mock.calls[0][1];
    handlers.onEvent({
      data: {
        type: 'tool_call_started',
        run_id: 'run-fast-subagent',
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
      },
    });
    handlers.onEvent({
      data: {
        type: 'subagent_session_started',
        run_id: 'run-fast-subagent',
        sequence: 2,
        payload: {
          tool_call: {
            id: 'call-subagent',
            index: 0,
            name: 'subagent',
          },
          data: {
            agent_id: 'beta',
            session_id: 'beta-session',
            run_id: 'beta-run',
            status: 'running',
          },
        },
      },
    });
    flushSync();

    const runningRow = document.querySelector('.subagent-tool-event');
    expect(runningRow).not.toBeNull();
    expect(runningRow?.querySelector('.te-dot.running')).not.toBeNull();
    expect(runningRow?.querySelector('.te-dot.done')).toBeNull();

    handlers.onEvent({
      data: {
        type: 'tool_call_result',
        run_id: 'run-fast-subagent',
        sequence: 3,
        payload: {
          tool_call: {
            id: 'call-subagent',
            index: 0,
            name: 'subagent',
          },
          result: JSON.stringify({
            ok: true,
            data: {
              agent_id: 'beta',
              session_id: 'beta-session',
              run_id: 'beta-run',
              status: 'running',
            },
          }),
        },
      },
    });
    flushSync();

    const spawnedRow = document.querySelector('.subagent-tool-event');
    expect(spawnedRow?.querySelector('.te-dot.running')).not.toBeNull();
    expect(spawnedRow?.querySelector('.te-dot.done')).toBeNull();
  });

  it('coalesces repeated run stream errors into one reconnect', async () => {
    const firstCloseSubscription = vi.fn();
    const secondCloseSubscription = vi.fn();
    subscribeRunEventsMock
      .mockReturnValueOnce({ close: firstCloseSubscription, source: null })
      .mockReturnValueOnce({ close: secondCloseSubscription, source: null });
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamResponse: {
          run_id: 'run-reconnect-once',
          sse_url: '/api/runs/run-reconnect-once/events',
          status: 'running',
          events: [],
        },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start reconnecting stream');

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    const handlers = subscribeRunEventsMock.mock.calls[0][1];

    vi.useFakeTimers();
    // Pin reconnect jitter to its midpoint so attempt 0 fires at exactly the
    // base 500ms delay this test advances by.
    const randomSpy = vi.spyOn(Math, 'random').mockReturnValue(0.5);
    try {
      handlers.onError(new Error('first disconnect'));
      handlers.onError(new Error('second disconnect'));

      await vi.advanceTimersByTimeAsync(500);
      flushSync();

      expect(subscribeRunEventsMock).toHaveBeenCalledTimes(2);
      expect(firstCloseSubscription).toHaveBeenCalledTimes(1);
      expect(secondCloseSubscription).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
      randomSpy.mockRestore();
    }
  });

  it('shows all command and skill suggestions for an empty slash query', async () => {
    const commandItems = [
      { name: 'stop', description: 'Cancel the active run.', type: 'command' },
      {
        name: 'help',
        description: 'Show available commands.',
        type: 'command',
      },
      { name: 'status', description: 'Show run status.', type: 'command' },
      { name: 'reset', description: 'Reset local UI state.', type: 'command' },
      { name: 'retry', description: 'Retry the last run.', type: 'command' },
      {
        name: 'debugging',
        description: 'Investigate unclear bugs.',
        type: 'skill',
      },
      {
        name: 'ctx7',
        description: 'Fetch current framework docs.',
        type: 'skill',
      },
      {
        name: 'refactoring',
        description: 'Refactor with strict scope.',
        type: 'skill',
      },
      {
        name: 'playwright-cli',
        description: 'Automate browser testing.',
        type: 'skill',
      },
      {
        name: 'frontend-design',
        description: 'Build intentional UI.',
        type: 'skill',
      },
      {
        name: 'glossary',
        description: 'Maintain glossary terms.',
        type: 'skill',
      },
      {
        name: 'debug',
        description: 'General debugging workflow.',
        type: 'skill',
      },
    ];

    rpcMock.mockImplementation(
      createChatRpcMock({
        commandItems,
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const composerInput = document.querySelector('.msg-input');
    expect(composerInput).toBeTruthy();
    setInputValue(composerInput, '/');
    composerInput.setSelectionRange(1, 1);
    composerInput.dispatchEvent(new Event('keyup', { bubbles: true }));
    flushSync();

    await waitForCondition(
      () =>
        document.querySelectorAll('.skill-autocomplete__option').length ===
        commandItems.length,
      100,
    );

    expect(
      document.querySelectorAll('.skill-autocomplete__option'),
    ).toHaveLength(commandItems.length);
  });

  it('shows only skill suggestions for an inline dollar query', async () => {
    const commandItems = [
      { name: 'stop', description: 'Cancel the active run.', type: 'command' },
      { name: 'status', description: 'Show run status.', type: 'command' },
      {
        name: 'debugging',
        description: 'Investigate unclear bugs.',
        type: 'skill',
      },
      {
        name: 'ctx7',
        description: 'Fetch current framework docs.',
        type: 'skill',
      },
    ];

    rpcMock.mockImplementation(
      createChatRpcMock({
        commandItems,
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const composerInput = document.querySelector('.msg-input');
    expect(composerInput).toBeTruthy();
    setInputValue(composerInput, 'Use $');
    composerInput.setSelectionRange(5, 5);
    composerInput.dispatchEvent(new Event('keyup', { bubbles: true }));
    flushSync();

    await waitForCondition(
      () =>
        document.querySelectorAll('.skill-autocomplete__option').length === 2,
      100,
    );

    const optionNames = Array.from(
      document.querySelectorAll('.skill-autocomplete__name'),
    ).map((element) => element.textContent.trim());
    expect(optionNames).toEqual(['debugging', 'ctx7']);
    expect(
      document.querySelector('.skill-autocomplete__eyebrow').textContent,
    ).toContain('skills');
  });

  it('loads a sub-agent session override as a writable session notice', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        pendingSessionNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
          subAgent: true,
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Sub-agent response'),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'sub-session-1',
      limit: 100,
    });
    expect(document.body.textContent).toContain('Viewing a sub-agent session');
    expect(document.body.textContent).toContain('Return to current session');
    expect(
      document.querySelector(
        '.chat-view__footer-stack .chat-view__footer-banner',
      ),
    ).toBeTruthy();
    expect(document.querySelector('textarea')?.disabled).toBe(false);
  });

  it('loads the newest history first and prepends older messages on top scroll', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-1': createHistoryMessages(120),
        },
      }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    await waitForCondition(
      () =>
        document.body.textContent.includes('History message 21') &&
        !document.body.textContent.includes('History message 20'),
      100,
    );

    const messages = document.querySelector('.messages');
    let scrollHeight = 1000;
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
    scrollHeight = 1400;

    await waitForCondition(
      () =>
        document.body.textContent.includes('History message 20') &&
        messages.scrollTop === 400,
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'session-1',
      limit: 100,
    });
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'session-1',
      limit: 50,
      before: 'message-021',
    });
  });

  it('subscribes to an active run returned with opened session history', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        activeRuns: {
          'sub-session-1': {
            run_id: 'active-sub-run',
            sse_url: '/api/runs/active-sub-run/events',
            status: 'running',
            events: [
              {
                type: 'run_started',
                run_id: 'active-sub-run',
                agent_id: 'alpha',
                session_id: 'sub-session-1',
                sequence: 1,
                payload: { status: 'running' },
              },
            ],
          },
        },
      }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        pendingSessionNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
          subAgent: true,
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/active-sub-run/events',
      expect.any(Object),
      { afterSequence: 1 },
    );
  });

  it('keeps an opened sub-agent session live after the Run replay prefix was evicted', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        activeRuns: {
          'sub-session-1': {
            run_id: 'long-sub-run',
            sse_url: '/api/runs/long-sub-run/events',
            status: 'running',
            events: [
              {
                type: 'assistant_output_delta',
                run_id: 'long-sub-run',
                agent_id: 'alpha',
                session_id: 'sub-session-1',
                sequence: 5_000,
                payload: { content_delta: 'Retained ' },
              },
            ],
          },
        },
      }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        pendingSessionNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
          subAgent: true,
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );
    const handlers = subscribeRunEventsMock.mock.calls[0][1];
    handlers.onEvent({
      data: {
        type: 'assistant_output_delta',
        run_id: 'long-sub-run',
        sequence: 5_001,
        payload: { content_delta: 'live' },
      },
    });

    await waitForCondition(
      () => document.body.textContent.includes('Retained live'),
      100,
    );
    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/long-sub-run/events',
      expect.any(Object),
      { afterSequence: 0 },
    );
  });

  it('merges retained active-run events when reloading the same displayed session', async () => {
    const activeRuns = {
      'session-1': {
        run_id: 'active-parent-run',
        sse_url: '/api/runs/active-parent-run/events',
        status: 'running',
        events: [
          {
            type: 'run_started',
            run_id: 'active-parent-run',
            agent_id: 'alpha',
            session_id: 'session-1',
            sequence: 1,
            payload: { status: 'running' },
          },
        ],
      },
    };
    rpcMock.mockImplementation(createChatRpcMock({ activeRuns }));
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'session-1',
          created_at: '2026-05-10T00:00:00+00:00',
          last_active_at: '2026-05-10T00:01:00+00:00',
        },
      ],
    });

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    activeRuns['session-1'] = {
      ...activeRuns['session-1'],
      events: [
        ...activeRuns['session-1'].events,
        {
          type: 'assistant_output_delta',
          run_id: 'active-parent-run',
          agent_id: 'alpha',
          session_id: 'session-1',
          sequence: 2,
          payload: { content_delta: 'Recovered ' },
        },
        {
          type: 'assistant_output_delta',
          run_id: 'active-parent-run',
          agent_id: 'alpha',
          session_id: 'session-1',
          sequence: 3,
          payload: { content_delta: 'draft' },
        },
      ],
    };

    findButtonByText('Sessions')?.click();

    await waitForCondition(
      () => Boolean(document.querySelector('.session-row__select')),
      100,
    );

    document.querySelector('.session-row__select')?.click();

    await waitForCondition(
      () => document.body.textContent.includes('Recovered draft'),
      100,
    );

    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('attaches to SSE when a run starts for the displayed session', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        runServerEvent: {
          type: 'run_started',
          payload: {
            run_id: 'pushed-run',
            agent_id: 'alpha',
            session_id: 'session-1',
            run_event_type: 'run_started',
            run_event_sequence: 1,
            run_event_timestamp: '2026-05-26T00:00:00+00:00',
            status: 'running',
          },
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/pushed-run/events',
      expect.any(Object),
      { afterSequence: 1 },
    );
  });

  it('returns from a sub-agent session override to the current session', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        pendingSessionNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
          subAgent: true,
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Sub-agent response'),
      100,
    );

    const returnButton = Array.from(document.querySelectorAll('button')).find(
      (button) => button.textContent.trim() === 'Return to current session',
    );

    expect(returnButton).toBeTruthy();
    returnButton.click();

    await waitForCondition(
      () =>
        document.body.textContent.includes('Hello') &&
        !document.body.textContent.includes('Viewing a sub-agent session'),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'session-1',
      limit: 100,
    });
    expect(document.querySelector('textarea')?.disabled).toBe(false);
  });

  it('returns from a different-agent sub-agent session to the parent current session', async () => {
    const agents = [
      createAgent({
        id: 'alpha',
        name: 'Alpha',
        current_session_id: 'parent-session',
      }),
      createAgent({
        id: 'beta',
        name: 'Beta',
        current_session_id: 'beta-current-session',
      }),
    ];
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents,
        sessionMessages: {
          'parent-session': [
            {
              id: 'parent-assistant-one',
              role: 'assistant',
              content: 'Parent main response',
            },
          ],
          'beta-sub-session': [
            {
              id: 'beta-sub-assistant-one',
              role: 'assistant',
              content: 'Beta sub-agent response',
            },
          ],
          'beta-current-session': [
            {
              id: 'beta-current-assistant-one',
              role: 'assistant',
              content: 'Beta current response',
            },
          ],
        },
      }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: agents,
        sharedSelectedAgentId: 'alpha',
        pendingSessionNavigation: {
          agentId: 'beta',
          sessionId: 'beta-sub-session',
          subAgent: true,
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Beta sub-agent response'),
      100,
    );
    expect(activeAgentTab()?.textContent).toContain('Beta');

    const returnButton = Array.from(document.querySelectorAll('button')).find(
      (button) => button.textContent.trim() === 'Return to current session',
    );

    expect(returnButton).toBeTruthy();
    returnButton.click();

    await waitForCondition(
      () =>
        document.body.textContent.includes('Parent main response') &&
        !document.body.textContent.includes('Viewing a sub-agent session'),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'beta',
      session_id: 'beta-sub-session',
      limit: 100,
    });
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'parent-session',
      limit: 100,
    });
    expect(rpcMock).not.toHaveBeenCalledWith('chat.history', {
      agent_id: 'beta',
      session_id: 'beta-current-session',
      limit: 100,
    });
    expect(activeAgentTab()?.textContent).toContain('Alpha');
  });

  it('sends messages from a sub-agent session override', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamResponse: {
          run_id: 'sub-run-continue',
          sse_url: '/api/runs/sub-run-continue/events',
          status: 'running',
          events: [],
        },
      }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        pendingSessionNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
          subAgent: true,
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Sub-agent response'),
      100,
    );

    sendComposerMessage('Continue child work');

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.stream' &&
            params?.agent_id === 'alpha' &&
            params?.session_id === 'sub-session-1' &&
            params?.content === 'Continue child work',
        ),
      100,
    );

    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/sub-run-continue/events',
      expect.any(Object),
      { afterSequence: 0 },
    );
  });

  it('sends messages to a different-agent sub-agent session override', async () => {
    const agents = [
      createAgent({
        id: 'alpha',
        name: 'Alpha',
        current_session_id: 'parent-session',
      }),
      createAgent({
        id: 'beta',
        name: 'Beta',
        current_session_id: 'beta-current-session',
      }),
    ];
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents,
        sessionMessages: {
          'parent-session': [
            {
              id: 'parent-assistant-one',
              role: 'assistant',
              content: 'Parent main response',
            },
          ],
          'beta-sub-session': [
            {
              id: 'beta-sub-assistant-one',
              role: 'assistant',
              content: 'Beta sub-agent response',
            },
          ],
        },
        streamHandler: ({ agent_id: agentId, session_id: sessionId }) => {
          if (agentId === 'beta' && sessionId === 'beta-sub-session') {
            return {
              run_id: 'beta-sub-run-continue',
              sse_url: '/api/runs/beta-sub-run-continue/events',
              status: 'running',
              events: [],
            };
          }
          throw new Error(`Unexpected stream target: ${agentId}/${sessionId}`);
        },
      }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: agents,
        sharedSelectedAgentId: 'alpha',
        pendingSessionNavigation: {
          agentId: 'beta',
          sessionId: 'beta-sub-session',
          subAgent: true,
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Beta sub-agent response'),
      100,
    );

    sendComposerMessage('Continue beta child work');

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.stream' &&
            params?.agent_id === 'beta' &&
            params?.session_id === 'beta-sub-session' &&
            params?.content === 'Continue beta child work',
        ),
      100,
    );

    expect(activeAgentTab()?.textContent).toContain('Beta');
    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/beta-sub-run-continue/events',
      expect.any(Object),
      { afterSequence: 0 },
    );
  });
});
