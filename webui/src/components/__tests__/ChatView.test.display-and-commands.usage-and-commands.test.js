// @vitest-environment jsdom
import {
  describe,
  createAgent,
  createChatRpcMock,
  expect,
  findButtonByText,
  findNewSessionButton,
  flushSync,
  contextCompactionButton,
  hoveredContextRingCard,
  it,
  rpcMock,
  selectAgentFromPicker,
  selectedPersonalAgentName,
  sendComposerMessage,
  setInputValue,
  setupChatViewTestSuite,
  subscribeRunEventsMock,
  testChatStateRefs,
  testRunStreamRefs,
  waitForCondition,
} from './ChatView.support.js';

describe('ChatView', () => {
  const chatViewTest = setupChatViewTestSuite();

  it('renders the context ring with the correct fill ratio', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92 },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    // The ring is rendered only when both token count and context window are
    // known. Its fill arc's stroke-dashoffset encodes the fill ratio.
    const circumference = 2 * Math.PI * 6;
    const expectedRatio = 3978 / 262144;
    const expectedOffset = circumference * (1 - expectedRatio);

    await waitForCondition(
      () =>
        document.body.querySelector('.context-ring .context-ring__fill') !==
        null,
      100,
    );

    const fillArc = document.body.querySelector(
      '.context-ring .context-ring__fill',
    );
    const offset = Number(fillArc.getAttribute('stroke-dashoffset'));
    expect(offset).toBeCloseTo(expectedOffset, 1);
  });

  it('refreshes Context Usage after a model step while the run continues', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 1000, output_tokens: 50 },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(() => testRunStreamRefs.length === 1, 100);
    testRunStreamRefs[0].handleServerEvents({
      type: 'run_started',
      payload: {
        run_id: 'run-one',
        agent_id: 'alpha',
        project_id: null,
        session_id: 'session-1',
        run_event_type: 'run_started',
        run_event_sequence: 1,
        output: { status: 'running' },
      },
    });
    testRunStreamRefs[0].handleServerEvents({
      type: 'run_output',
      payload: {
        run_id: 'run-one',
        agent_id: 'alpha',
        project_id: null,
        session_id: 'session-1',
        run_event_type: 'model_step_usage',
        run_event_sequence: 2,
        output: {
          usage: { input_tokens: 3886, output_tokens: 92 },
          session_usage: {
            measured_turns: 2,
            estimated_turns: 0,
            input_tokens: 4886,
            output_tokens: 142,
          },
          context_usage: {
            tokens: 4050,
            estimated: true,
            provider_input_tokens: 3886,
            provider_output_tokens: 92,
            estimated_delta_tokens: 72,
          },
        },
      },
    });
    flushSync();

    // The ring should update its fill arc to reflect the new token count.
    const circumference = 2 * Math.PI * 6;
    const expectedRatio = 4050 / 262144;
    const expectedOffset = circumference * (1 - expectedRatio);

    await waitForCondition(() => {
      const fillArc = document.body.querySelector(
        '.context-ring .context-ring__fill',
      );
      if (!fillArc) return false;
      return (
        Math.abs(
          Number(fillArc.getAttribute('stroke-dashoffset')) - expectedOffset,
        ) < 0.5
      );
    }, 100);

    expect(testChatStateRefs[0].sessions['alpha::session-1'].status).toBe(
      'running',
    );
  });

  it('renders the context ring when Context Usage is estimated', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92, estimated: true },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    // The ring renders regardless of the estimated flag — the flag only
    // affects the tooltip text (prefixes the token count with ~).
    await waitForCondition(
      () => document.body.querySelector('.context-ring') !== null,
      100,
    );

    const circumference = 2 * Math.PI * 6;
    const expectedRatio = 3978 / 262144;
    const expectedOffset = circumference * (1 - expectedRatio);
    const fillArc = document.body.querySelector(
      '.context-ring .context-ring__fill',
    );
    const offset = Number(fillArc.getAttribute('stroke-dashoffset'));
    expect(offset).toBeCloseTo(expectedOffset, 1);
  });

  it('does not render the context ring when the context window is null', async () => {
    // A model whose context window is unknown sends context_window: null in
    // the agent payload. Without a denominator the fill ratio is undefined,
    // so the ring must not render — never a crash or a NaN arc.
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92 },
        contextWindow: null,
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    // Give the component a moment to settle, then confirm no ring.
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    expect(document.body.querySelector('.context-ring')).toBeNull();
  });

  it('shows the last-turn and session usage breakdown in the context ring tooltip', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: {
          input_tokens: 3886,
          output_tokens: 92,
          cache_read_tokens: 3000,
          cache_write_tokens: 200,
        },
        sessionUsage: {
          measured_turns: 12,
          estimated_turns: 0,
          cache_turns: 12,
          input_tokens: 40000,
          output_tokens: 1500,
          cache_read_tokens: 32000,
          cache_write_tokens: 900,
        },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    expect(await hoveredContextRingCard()).toEqual({
      summary: '3,978 / 262,144',
      sections: [
        {
          title: 'Last turn',
          meta: '',
          rows: [
            'Input: 3,886',
            '· Read from cache: 3,000 (77%)',
            '· Written to cache: 200',
            '· Uncached: 686',
            'Output: 92',
          ],
        },
        {
          title: 'Session',
          meta: '12 measured turns',
          rows: [
            'Input: 40,000',
            '· Read from cache: 32,000 (80%)',
            'Output: 1,500',
            'Avg cache read per turn: 2,667',
          ],
        },
      ],
    });
  });

  it('omits cache lines from the context ring tooltip without cache usage', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92 },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    expect(await hoveredContextRingCard()).toEqual({
      summary: '3,978 / 262,144',
      sections: [
        { title: 'Last turn', meta: '', rows: ['Input: 3,886', 'Output: 92'] },
      ],
    });
  });

  it('starts a manual Compaction Run from the context card while no Run is active', async () => {
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92 },
        streamHandler: ({ content }) => {
          streamCalls.push(content);
          if (content === '/compact') {
            return {
              run_id: 'run-card-compaction',
              sse_url: '/api/runs/run-card-compaction/events',
              status: 'running',
              events: [
                {
                  type: 'run_started',
                  run_id: 'run-card-compaction',
                  sequence: 1,
                  payload: { status: 'running' },
                },
                {
                  type: 'compaction_started',
                  run_id: 'run-card-compaction',
                  sequence: 2,
                  payload: {},
                },
              ],
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();
    await waitForCondition(
      () => document.body.querySelector('.context-ring-trigger') !== null,
      100,
    );

    const action = contextCompactionButton();
    expect(action.textContent.trim()).toBe('Compact now');
    expect(action.disabled).toBe(false);
    action.click();

    await waitForCondition(
      () => action.textContent.trim() === 'Compacting…',
      100,
    );
    expect(streamCalls).toEqual(['/compact']);
    expect(action.disabled).toBe(true);
    expect(
      rpcMock.mock.calls.some(([method]) => method === 'chat.control_run'),
    ).toBe(false);
  });

  it('requests Compaction from the active Run through the context card', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92 },
        streamResponse: {
          run_id: 'run-card-control',
          sse_url: '/api/runs/run-card-control/events',
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
    sendComposerMessage('Start a long run');
    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );
    const handlers = subscribeRunEventsMock.mock.calls[0][1];
    const controls = (compaction, sequence) =>
      handlers.onEvent({
        data: {
          type: 'run_controls_changed',
          run_id: 'run-card-control',
          sequence,
          payload: { compaction, background_tool_call_ids: [] },
        },
      });

    controls('unavailable', 1);
    flushSync();
    const action = contextCompactionButton();
    expect(action.disabled).toBe(true);

    controls('idle', 2);
    flushSync();
    expect(action.textContent.trim()).toBe('Compact now');
    expect(action.disabled).toBe(false);
    action.click();
    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(([method]) => method === 'chat.control_run'),
      100,
    );
    expect(rpcMock).toHaveBeenCalledWith('chat.control_run', {
      agent_id: 'alpha',
      session_id: 'session-1',
      run_id: 'run-card-control',
      action: 'compact',
    });

    controls('pending', 3);
    flushSync();
    expect(action.textContent.trim()).toBe('Compaction requested…');
    expect(action.disabled).toBe(true);

    controls('running', 4);
    flushSync();
    expect(action.textContent.trim()).toBe('Compacting…');
    expect(action.disabled).toBe(true);
  });

  it('does not render a refresh button in the chat header', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    expect(findButtonByText('Sessions')).toBeTruthy();
    expect(findNewSessionButton()).toBeTruthy();
    expect(findButtonByText('Refresh')).toBeFalsy();
    expect(document.body.querySelector('.chat-refresh')).toBeNull();
  });

  it('shows a bottom toast and skips run subscription when a toast command is handled', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamResponse: {
          command_handled: true,
          reply: 'Run cancelled.',
          output: 'toast',
        },
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
    setInputValue(composerInput, '/stop');
    flushSync();

    const sendButton = document.querySelector('.btn-primary.btn-icon');
    expect(sendButton).toBeTruthy();
    sendButton.click();

    await waitForCondition(
      () =>
        document.body
          .querySelector('.chat-view__command-toast')
          ?.textContent?.trim() === 'Run cancelled.',
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'session-1',
      content: '/stop',
    });
    expect(subscribeRunEventsMock).not.toHaveBeenCalled();
  });

  it('renders a transient card when a transient command is handled', async () => {
    const statusReply =
      'Agent: Alpha\nModel: claude-sonnet-4\nSession started: 2026-05-19';

    rpcMock.mockImplementation(
      createChatRpcMock({
        commandItems: [
          {
            name: 'status',
            description: 'Show current agent and session status.',
            type: 'command',
            argument: 'none',
            output: 'transient',
          },
        ],
        streamHandler: ({ content }) => {
          if (content === '/status') {
            return {
              command_handled: true,
              reply: statusReply,
              output: 'transient',
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('/status');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.transient-card')
          ?.textContent?.includes('Agent: Alpha'),
      100,
    );

    const card = document.body.querySelector('.transient-card__body');
    expect(card?.textContent).toContain('Agent: Alpha\nModel: claude-sonnet-4');
    expect(card?.textContent).toContain('Session started: 2026-05-19');
    // Transient output is never echoed into the bottom toast.
    expect(document.body.querySelector('.chat-view__command-toast')).toBeNull();
    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'session-1',
      content: '/status',
    });
    expect(subscribeRunEventsMock).not.toHaveBeenCalled();
  });

  it('drops a stale transient command result after navigating away and back', async () => {
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    let resolveStatus;
    const statusResponse = new Promise((resolve) => {
      resolveStatus = resolve;
    });
    const agents = [
      createAgent(),
      createAgent({
        id: 'beta',
        name: 'Beta',
        current_session_id: 'session-beta',
      }),
    ];
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents,
        sessionMessages: {
          'session-beta': [
            { id: 'beta-reply', role: 'assistant', content: 'Beta reply' },
          ],
        },
        commandItems: [
          {
            name: 'status',
            description: 'Show status.',
            type: 'command',
            argument: 'none',
            output: 'transient',
          },
        ],
        streamHandler: ({ content }) => {
          if (content === '/status') {
            return statusResponse;
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );
    const parentHarness = createChatViewParentHarness();
    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: agents,
        get sharedSelectedAgentId() {
          return parentHarness.selectedAgentId;
        },
        onAgentSelected: (agentId) => parentHarness.setSelectedAgentId(agentId),
      },
    });
    flushSync();
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('/status');
    await selectAgentFromPicker('Beta');
    await waitForCondition(
      () => document.body.textContent.includes('Beta reply'),
      100,
    );
    await selectAgentFromPicker('Alpha');
    await waitForCondition(() => selectedPersonalAgentName() === 'Alpha', 100);

    resolveStatus({
      command_handled: true,
      reply: 'Stale Alpha status',
      output: 'transient',
    });
    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.stream' && params?.content === '/status',
        ),
      100,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();

    expect(selectedPersonalAgentName()).toBe('Alpha');
    expect(document.body.querySelector('.transient-card')).toBeNull();
  });

  it('stacks transient cards so successive snapshots can be compared', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamHandler: ({ content }) => {
          if (content === '/status') {
            return {
              command_handled: true,
              reply: 'Agent: Alpha',
              output: 'transient',
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('/status');
    await waitForCondition(
      () => document.body.querySelectorAll('.transient-card').length === 1,
      100,
    );

    sendComposerMessage('/status');
    await waitForCondition(
      () => document.body.querySelectorAll('.transient-card').length === 2,
      100,
    );

    expect(document.body.querySelectorAll('.transient-card')).toHaveLength(2);
  });

  it('switches to the session returned by a handled /new command', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-new': [],
        },
        streamHandler: ({ content }) => {
          if (content === '/new') {
            return {
              command_handled: true,
              reply: 'New session started: session-new',
              data: {
                command: 'new',
                session_id: 'session-new',
              },
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('/new');

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' && params?.session_id === 'session-new',
        ),
      100,
    );

    // `/new` is an action command: it switches the session rather than showing
    // a toast or transient card.
    expect(document.body.querySelector('.chat-view__command-toast')).toBeNull();
    expect(document.body.querySelector('.transient-card')).toBeNull();
    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'session-1',
      content: '/new',
    });
    expect(subscribeRunEventsMock).not.toHaveBeenCalled();
  });

  it('switches to the new session returned by a same-agent /handoff command', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-handoff-same': [
            {
              id: 'handoff-same-assistant-one',
              role: 'assistant',
              content: 'Handoff target reply (same agent)',
            },
          ],
        },
        streamHandler: ({ content }) => {
          if (content === '/handoff') {
            return {
              command_handled: true,
              reply: 'Handoff sent to alpha. Opening new session.',
              data: {
                command: 'handoff',
                session_id: 'session-handoff-same',
                agent_id: 'alpha',
              },
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
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
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('/handoff');

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' &&
            params?.session_id === 'session-handoff-same',
        ),
      100,
    );

    // `/handoff` is an action command: no toast or transient card.
    expect(document.body.querySelector('.chat-view__command-toast')).toBeNull();
    expect(document.body.querySelector('.transient-card')).toBeNull();
    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'session-1',
      content: '/handoff',
    });
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'session-handoff-same',
      limit: 100,
    });
    expect(subscribeRunEventsMock).not.toHaveBeenCalled();
    expect(selectedPersonalAgentName()).toBe('Alpha');
  });
});
