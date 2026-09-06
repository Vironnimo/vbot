// @vitest-environment jsdom

import {
  describe,
  activeAgentTab,
  createAgent,
  createChatRpcMock,
  expect,
  findButtonByText,
  findNewSessionButton,
  flushSync,
  hoveredContextRingTooltip,
  it,
  listSessionActivityMock,
  rpcMock,
  sendComposerMessage,
  setInputValue,
  setupChatViewTestSuite,
  subscribeRunEventsMock,
  testChatStateRefs,
  testRunStreamRefs,
  waitForCondition,
} from './ChatView.support.js';
import { reactiveProps } from './reactiveProps.svelte.js';

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

  it('hides presentation while inactive without recreating Chat state or DOM', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    const props = reactiveProps({
      active: true,
      sharedAgents: [createAgent()],
      sharedSelectedAgentId: 'alpha',
    });

    chatViewTest.mount({ target: document.body, props });
    flushSync();
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );
    const agentLoads = rpcMock.mock.calls.filter(
      ([method]) => method === 'agent.list',
    ).length;
    const historyLoads = rpcMock.mock.calls.filter(
      ([method]) => method === 'chat.history',
    ).length;
    const activityLoads = listSessionActivityMock.mock.calls.length;
    const chatView = document.querySelector('.chat-view');
    const timeline = document.querySelector('.messages');
    timeline.scrollTop = 93;
    expect(testChatStateRefs).toHaveLength(1);

    props.active = false;
    flushSync();
    expect(document.querySelector('.chat-view')).toBe(chatView);
    expect(chatView.hidden).toBe(true);
    expect(testChatStateRefs).toHaveLength(1);

    props.active = true;
    flushSync();
    expect(document.querySelector('.chat-view')).toBe(chatView);
    expect(chatView.hidden).toBe(false);
    expect(document.querySelector('.messages')).toBe(timeline);
    expect(timeline.scrollTop).toBe(93);
    expect(document.body.textContent).toContain('Hello');
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'agent.list'),
    ).toHaveLength(agentLoads);
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'chat.history'),
    ).toHaveLength(historyLoads);
    expect(listSessionActivityMock).toHaveBeenCalledTimes(activityLoads);
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

  it('shows background Agent activity as orange without selecting that Agent', async () => {
    const beta = createAgent({
      id: 'beta',
      name: 'Beta',
      current_session_id: 'session-beta',
    });
    rpcMock.mockImplementation(
      createChatRpcMock({ agents: [createAgent(), beta] }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent(), beta],
      },
    });
    flushSync();

    await waitForCondition(() => testRunStreamRefs.length === 1, 100);
    testRunStreamRefs[0].handleServerEvents({
      type: 'run_started',
      payload: {
        run_id: 'run-beta',
        agent_id: 'beta',
        project_id: null,
        session_id: 'session-beta',
        run_event_type: 'run_started',
        run_event_sequence: 1,
        output: { status: 'running' },
      },
    });
    flushSync();

    const betaTab = findButtonByText('Beta');
    expect(betaTab?.classList.contains('active')).toBe(false);
    expect(betaTab?.querySelector('.tab-indicator--running')).toBeTruthy();
  });

  it('keeps an inactive Agent blue until its exact result is opened', async () => {
    const beta = createAgent({
      id: 'beta',
      name: 'Beta',
      current_session_id: 'session-beta',
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents: [createAgent(), beta],
        sessionMessages: {
          'session-beta': [
            {
              id: 'summary-beta',
              role: 'run_summary',
              run_id: 'run-beta',
              status: 'completed',
              timestamp: '2026-07-20T10:00:00+00:00',
            },
          ],
        },
      }),
    );
    listSessionActivityMock.mockImplementation(async (agentIds) => ({
      agents: agentIds.map((agentId) => ({
        agent_id: agentId,
        project_id: null,
        sessions:
          agentId === 'beta'
            ? [
                {
                  id: 'session-beta',
                  has_unread_completion: true,
                  unread_run_id: 'run-beta',
                  unread_run_status: 'completed',
                  unread_run_at: '2026-07-20T10:00:00+00:00',
                },
              ]
            : [],
      })),
    }));

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent(), beta],
      },
    });
    flushSync();

    await waitForCondition(
      () =>
        Boolean(
          findButtonByText('Beta')?.querySelector('.tab-indicator--unread'),
        ),
      100,
    );
    const betaTab = findButtonByText('Beta');
    await waitForCondition(() => betaTab.disabled === false, 100);
    betaTab.click();

    await waitForCondition(
      () => findButtonByText('Beta')?.classList.contains('active') === true,
      100,
    );
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'beta',
      session_id: 'session-beta',
      limit: 100,
    });

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'session.mark_read' &&
            params?.agent_id === 'beta' &&
            params?.session_id === 'session-beta' &&
            params?.run_id === 'run-beta',
        ),
      100,
    );
    flushSync();

    const selectedBetaTab = findButtonByText('Beta');
    expect(selectedBetaTab.classList.contains('active')).toBe(true);
    expect(selectedBetaTab.querySelector('.tab-indicator--unread')).toBeNull();
  });

  it('does not resurrect a read result when retained events replay after remount', async () => {
    const beta = createAgent({
      id: 'beta',
      name: 'Beta',
      current_session_id: 'session-beta',
    });
    rpcMock.mockImplementation(
      createChatRpcMock({ agents: [createAgent(), beta] }),
    );
    listSessionActivityMock.mockImplementation(async (agentIds) => ({
      agents: agentIds.map((agentId) => ({
        agent_id: agentId,
        project_id: null,
        sessions:
          agentId === 'beta'
            ? [
                {
                  id: 'session-beta',
                  latest_completion_run_id: 'run-beta',
                  has_unread_completion: false,
                  unread_run_id: null,
                  unread_run_status: null,
                  unread_run_at: null,
                },
              ]
            : [],
      })),
    }));

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent(), beta],
        runServerEvents: [
          {
            type: 'run_completed',
            payload: {
              run_id: 'run-beta',
              agent_id: 'beta',
              project_id: null,
              session_id: 'session-beta',
              run_event_type: 'run_completed',
              run_event_sequence: 2,
              run_event_timestamp: '2026-07-20T10:00:00+00:00',
              status: 'completed',
            },
          },
        ],
      },
    });
    flushSync();

    await waitForCondition(
      () =>
        testChatStateRefs[0]?.sessions['beta::session-beta']
          ?.latestCompletionRunId === 'run-beta',
      100,
    );
    flushSync();

    expect(
      testChatStateRefs[0].sessions['beta::session-beta'].hasUnreadCompletion,
    ).toBe(false);
    expect(
      findButtonByText('Beta')?.querySelector('.tab-indicator--unread'),
    ).toBeNull();
  });

  it('clears a delivered child result and lands on the Agent user session', async () => {
    const alpha = createAgent({
      current_session_id: 'parent-session',
    });
    const beta = createAgent({
      id: 'beta',
      name: 'Beta',
      current_session_id: 'beta-user-session',
    });
    let childDelivered = false;
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents: [alpha, beta],
        sessionMessages: {
          'parent-session': [],
          'beta-user-session': [
            {
              id: 'beta-user-message',
              role: 'assistant',
              content: 'Beta user conversation',
            },
          ],
          'beta-child-session': [
            {
              id: 'beta-child-summary',
              role: 'run_summary',
              run_id: 'beta-child-run',
              status: 'completed',
            },
          ],
        },
      }),
    );
    listSessionActivityMock.mockImplementation(async (agentIds) => ({
      agents: agentIds.map((agentId) => ({
        agent_id: agentId,
        project_id: null,
        sessions:
          agentId === 'beta'
            ? [
                {
                  id: 'beta-user-session',
                  latest_completion_run_id: null,
                  has_unread_completion: false,
                },
                {
                  id: 'beta-child-session',
                  latest_completion_run_id: 'beta-child-run',
                  has_unread_completion: !childDelivered,
                  unread_run_id: childDelivered ? null : 'beta-child-run',
                  unread_run_status: childDelivered ? null : 'completed',
                  unread_run_at: childDelivered
                    ? null
                    : '2026-07-20T10:00:00+00:00',
                },
              ]
            : [],
      })),
    }));
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const parentHarness = createChatViewParentHarness();

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [alpha, beta],
        get sharedSelectedAgentId() {
          return parentHarness.selectedAgentId;
        },
        onAgentSelected: (agentId) => parentHarness.setSelectedAgentId(agentId),
        get sessionsRefreshToken() {
          return parentHarness.sessionsRefreshToken;
        },
      },
    });
    flushSync();

    await waitForCondition(
      () =>
        Boolean(
          findButtonByText('Beta')?.querySelector('.tab-indicator--unread'),
        ),
      100,
    );

    childDelivered = true;
    parentHarness.bumpSessionsRefreshToken();
    flushSync();
    await waitForCondition(
      () => !findButtonByText('Beta')?.querySelector('.tab-indicator--unread'),
      100,
    );

    findButtonByText('Beta').click();
    await waitForCondition(
      () =>
        findButtonByText('Beta')?.classList.contains('active') &&
        document.body.textContent.includes('Beta user conversation'),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'beta',
      session_id: 'beta-user-session',
      limit: 100,
    });
    expect(rpcMock).not.toHaveBeenCalledWith('chat.history', {
      agent_id: 'beta',
      session_id: 'beta-child-session',
      limit: 100,
    });
  });

  it('keeps a displayed terminal result idle before read acknowledgement returns', async () => {
    let resolveMarkRead;
    const defaultRpc = createChatRpcMock();
    rpcMock.mockImplementation((method, params) => {
      if (method === 'session.mark_read') {
        return new Promise((resolve) => {
          resolveMarkRead = () =>
            resolve({
              agent_id: params.agent_id,
              session_id: params.session_id,
              latest_completion_run_id: params.run_id,
              has_unread_completion: false,
              unread_run_id: null,
              unread_run_status: null,
              unread_run_at: null,
              marked_read: true,
            });
        });
      }
      return defaultRpc(method, params);
    });

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    await waitForCondition(() => testRunStreamRefs.length === 1, 100);
    testRunStreamRefs[0].handleServerEvents({
      type: 'run_started',
      payload: {
        run_id: 'run-visible',
        agent_id: 'alpha',
        project_id: null,
        session_id: 'session-1',
        run_event_type: 'run_started',
        run_event_sequence: 1,
        run_event_timestamp: '2026-07-20T10:00:00+00:00',
        output: { status: 'running' },
      },
    });
    testRunStreamRefs[0].handleServerEvents({
      type: 'run_completed',
      payload: {
        run_id: 'run-visible',
        agent_id: 'alpha',
        project_id: null,
        session_id: 'session-1',
        run_event_type: 'run_completed',
        run_event_sequence: 2,
        run_event_timestamp: '2026-07-20T10:00:01+00:00',
        status: 'completed',
      },
    });
    flushSync();

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'session.mark_read' &&
            params?.agent_id === 'alpha' &&
            params?.session_id === 'session-1' &&
            params?.run_id === 'run-visible',
        ),
      100,
    );
    flushSync();

    const alphaTab = activeAgentTab();
    expect(alphaTab?.querySelector('.tab-indicator--unread')).toBeNull();

    resolveMarkRead();
    await Promise.resolve();
  });

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

    const expectedTooltip = [
      '3,978 / 262,144',
      '',
      'Last turn',
      'Input: 3,886 tok',
      '  · read from cache: 3,000 (77%)',
      '  · newly written to cache: 200',
      '  · uncached: 686',
      'Output: 92 tok',
      '',
      'Session (12 fully measured turns)',
      'Input: 40,000 tok',
      '  · read from cache: 32,000 (80%)',
      'Output: 1,500 tok',
      'Avg cache read per turn: 2,667 tok',
    ].join('\n');

    expect(await hoveredContextRingTooltip(expectedTooltip)).toBe(
      expectedTooltip,
    );
  });

  it('omits cache lines from the context ring tooltip without cache usage', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92 },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    const expectedTooltip = [
      '3,978 / 262,144',
      '',
      'Last turn',
      'Input: 3,886 tok',
      'Output: 92 tok',
    ].join('\n');

    expect(await hoveredContextRingTooltip(expectedTooltip)).toBe(
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
    findButtonByText('Beta').click();
    await waitForCondition(
      () => document.body.textContent.includes('Beta reply'),
      100,
    );
    findButtonByText('Alpha').click();
    await waitForCondition(
      () => activeAgentTab()?.textContent?.includes('Alpha'),
      100,
    );

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

    expect(activeAgentTab()?.textContent).toContain('Alpha');
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
    expect(activeAgentTab()?.textContent).toContain('Alpha');
  });
});
