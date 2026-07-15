// @vitest-environment jsdom

import {
  describe,
  activeAgentTab,
  createAgent,
  createChatRpcMock,
  expect,
  findButtonByText,
  flushSync,
  hoveredTokenBadgeTooltip,
  it,
  rpcMock,
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

  it('marks the chat view with the default comfortable chat-width', () => {
    rpcMock.mockImplementation(createChatRpcMock());

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    expect(
      document.querySelector('.chat-view')?.getAttribute('data-chat-width'),
    ).toBe('comfortable');
  });

  it('reflects the chatWidth prop on the chat view for the measure cap', () => {
    rpcMock.mockImplementation(createChatRpcMock());

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        chatWidth: 'full',
      },
    });
    flushSync();

    // `full` is the opt-out hook: the CSS sets `--chat-measure: none` on this
    // attribute, removing the reading-width cap.
    expect(
      document.querySelector('.chat-view')?.getAttribute('data-chat-width'),
    ).toBe('full');
  });

  it('requests command suggestions scoped to the active agent address', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    // chat.commands is fetched with the active agent's address so the server can
    // return that agent's effective (project-scoped) skills, not the global list.
    const calledWithAgent = () =>
      rpcMock.mock.calls.some(
        ([method, params]) =>
          method === 'chat.commands' && params?.agent_id === 'alpha',
      );
    await waitForCondition(calledWithAgent, 100);

    expect(calledWithAgent()).toBe(true);
  });

  it('shows the combined input and output usage in the token badge', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92 },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    const numberFormat = new Intl.NumberFormat('en');
    const expectedBadge = `${numberFormat.format(3978)} / ${numberFormat.format(262144)} tok`;

    await waitForCondition(
      () =>
        document.body.querySelector('.token-badge')?.textContent?.trim() ===
        expectedBadge,
      100,
    );

    expect(
      document.body.querySelector('.token-badge')?.textContent?.trim(),
    ).toBe(expectedBadge);
  });

  it('refreshes the unchanged token badge after a model step while the run continues', async () => {
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
        },
      },
    });
    flushSync();

    const numberFormat = new Intl.NumberFormat('en');
    const expectedBadge = `${numberFormat.format(3978)} / ${numberFormat.format(262144)} tok`;
    await waitForCondition(
      () =>
        document.body.querySelector('.token-badge')?.textContent?.trim() ===
        expectedBadge,
      100,
    );

    expect(
      document.body.querySelector('.token-badge')?.textContent?.trim(),
    ).toBe(expectedBadge);
    expect(testChatStateRefs[0].sessions['alpha::session-1'].status).toBe(
      'running',
    );
  });

  it('keeps the estimated marker when combined usage is estimated', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92, estimated: true },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    const numberFormat = new Intl.NumberFormat('en');
    const expectedBadge = `~${numberFormat.format(3978)} / ${numberFormat.format(262144)} tok`;

    await waitForCondition(
      () =>
        document.body.querySelector('.token-badge')?.textContent?.trim() ===
        expectedBadge,
      100,
    );

    expect(
      document.body.querySelector('.token-badge')?.textContent?.trim(),
    ).toBe(expectedBadge);
  });

  it('tolerates a null context window in the token badge', async () => {
    // A model whose context window is unknown sends context_window: null in the
    // agent payload. The badge must show just the tokens — never "/ NaN" or a
    // crash (Phase 6 honest-gap contract).
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92 },
        contextWindow: null,
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    const numberFormat = new Intl.NumberFormat('en');
    const expectedBadge = `${numberFormat.format(3978)} tok`;

    await waitForCondition(
      () =>
        document.body.querySelector('.token-badge')?.textContent?.trim() ===
        expectedBadge,
      100,
    );

    const badgeText = document.body
      .querySelector('.token-badge')
      ?.textContent?.trim();
    expect(badgeText).toBe(expectedBadge);
    expect(badgeText).not.toContain('NaN');
    expect(badgeText).not.toContain('/');
  });

  it('shows the last-turn and session usage breakdown in the token badge tooltip', async () => {
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

    const expectedTooltip = [
      'Last turn',
      'Input: 3,886 tok',
      '  · read from cache: 3,000 (77%)',
      '  · newly written to cache: 200',
      '  · uncached: 686',
      'Output: 92 tok',
      '',
      'Session (12 measured turns)',
      'Input: 40,000 tok',
      '  · read from cache: 32,000 (80%)',
      'Output: 1,500 tok',
      'Avg cache read per turn: 2,667 tok',
    ].join('\n');

    expect(await hoveredTokenBadgeTooltip(expectedTooltip)).toBe(
      expectedTooltip,
    );
  });

  it('omits cache lines from the token badge tooltip without cache usage', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92 },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    const expectedTooltip = [
      'Last turn',
      'Input: 3,886 tok',
      'Output: 92 tok',
    ].join('\n');

    expect(await hoveredTokenBadgeTooltip(expectedTooltip)).toBe(
      expectedTooltip,
    );
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
    expect(findButtonByText('New session')).toBeTruthy();
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

    const composerInput = document.querySelector('#chat-composer-input');
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
    expect(activeAgentTab()?.textContent).toContain('Alpha');
  });
});
