// @vitest-environment jsdom

import {
  describe,
  createAgent,
  createChatRpcMock,
  expect,
  findButtonByText,
  flushSync,
  it,
  listQueueMock,
  listSessionsMock,
  rpcMock,
  setupChatViewTestSuite,
  showProjectMock,
  subscribeRunEventsMock,
  vi,
  waitForCondition,
} from './ChatView.support.js';

describe('ChatView', () => {
  const chatViewTest = setupChatViewTestSuite();

  it('re-syncs a held session queue on a matching queue resource_changed', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const parentHarness = createChatViewParentHarness();

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        get queueInvalidation() {
          return parentHarness.queueInvalidation;
        },
      },
    });
    flushSync();

    // The initial history load syncs the current session's queue once.
    await waitForCondition(
      () =>
        listQueueMock.mock.calls.some(
          ([agentId, sessionId]) =>
            agentId === 'alpha' && sessionId === 'session-1',
        ),
      100,
    );
    const callsBefore = listQueueMock.mock.calls.length;

    // A queue signal for a session this window does not hold is ignored.
    parentHarness.setQueueInvalidation({
      agentId: 'alpha',
      sessionId: 'unheld',
    });
    flushSync();
    expect(listQueueMock.mock.calls.length).toBe(callsBefore);

    // A queue signal for the held session re-syncs just that session's queue.
    parentHarness.setQueueInvalidation({
      agentId: 'alpha',
      sessionId: 'session-1',
    });
    flushSync();

    expect(listQueueMock.mock.calls.length).toBe(callsBefore + 1);
    expect(listQueueMock).toHaveBeenLastCalledWith('alpha', 'session-1');
  });

  it('does not switch the viewed conversation on a sessions resource_changed', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const parentHarness = createChatViewParentHarness();

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        get sessionsRefreshToken() {
          return parentHarness.sessionsRefreshToken;
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const historyCallsBefore = rpcMock.mock.calls.filter(
      ([method]) => method === 'chat.history',
    ).length;

    // A sessions signal refreshes the session list (drawer) only — it must not
    // reload the agent or switch the viewed conversation ("stay put").
    parentHarness.bumpSessionsRefreshToken();
    flushSync();

    const historyCallsAfter = rpcMock.mock.calls.filter(
      ([method]) => method === 'chat.history',
    ).length;
    expect(historyCallsAfter).toBe(historyCallsBefore);
    expect(document.body.textContent).toContain('Hello');
  });

  it('displays a session override over the active project agent and loads it by address (item 5)', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [{ agent_id: 'builder', display_name: 'Builder', model: 'm' }],
        report: { clean: true, findings: [] },
      },
    });
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'builder-session',
          created_at: '2026-06-01T00:00:00+00:00',
          last_active_at: '2026-06-10T00:00:00+00:00',
        },
      ],
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'builder-session': [
            {
              id: 'builder-reply',
              role: 'assistant',
              content: 'Builder project reply',
            },
          ],
          'worker-session': [
            {
              id: 'worker-reply',
              role: 'assistant',
              content: 'Worker child reply',
            },
          ],
        },
      }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
        pendingSessionNavigation: {
          agentId: 'worker@vbot',
          sessionId: 'worker-session',
          subAgent: true,
        },
      },
    });
    flushSync();

    // The override wins the display even though a project agent is active:
    // the child session renders, not the project session the second bar owns.
    await waitForCondition(
      () => document.body.textContent.includes('Worker child reply'),
      100,
    );
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'worker@vbot',
      session_id: 'worker-session',
      limit: 100,
    });
    expect(document.body.textContent).toContain('Viewing a sub-agent session');
  });

  it('qualifies a spawn-row view-session link with the displayed project (item 5)', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [{ agent_id: 'builder', display_name: 'Builder', model: 'm' }],
        report: { clean: true, findings: [] },
      },
    });
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'builder-session',
          created_at: '2026-06-01T00:00:00+00:00',
          last_active_at: '2026-06-10T00:00:00+00:00',
        },
      ],
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'builder-session': [
            { id: 'builder-user', role: 'user', content: 'Spawn a worker' },
            {
              id: 'builder-spawn',
              role: 'assistant',
              content: null,
              tool_calls: [
                {
                  id: 'call-worker',
                  name: 'subagent',
                  arguments: {
                    agent_id: 'worker',
                    background: true,
                    content: 'Do the work',
                  },
                },
              ],
            },
            {
              id: 'builder-spawn-result',
              role: 'tool',
              tool_call_id: 'call-worker',
              name: 'subagent',
              content: JSON.stringify({
                ok: true,
                data: {
                  agent_id: 'worker',
                  session_id: 'worker-session',
                  run_id: 'worker-run',
                  status: 'completed',
                },
              }),
            },
          ],
          'worker-session': [
            {
              id: 'worker-reply',
              role: 'assistant',
              content: 'Worker child reply',
            },
          ],
        },
      }),
    );
    const navigateToSubAgent = vi.fn();

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
        navigateToSubAgent,
      },
    });
    flushSync();

    await waitForCondition(
      () => Boolean(findButtonByText('view session')),
      100,
    );

    findButtonByText('view session').click();
    flushSync();

    // The persisted descriptor carries the bare child id; the navigation must
    // carry the parent project's qualified address (trap 2).
    expect(navigateToSubAgent).toHaveBeenCalledWith(
      expect.objectContaining({
        agentId: 'worker@vbot',
        sessionId: 'worker-session',
      }),
    );
  });

  it('lists and opens drawer sessions of a project agent by full address (item 5)', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [{ agent_id: 'builder', display_name: 'Builder', model: 'm' }],
        report: { clean: true, findings: [] },
      },
    });
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'builder-session',
          created_at: '2026-06-05T00:00:00+00:00',
          last_active_at: '2026-06-10T00:00:00+00:00',
        },
        {
          id: 'builder-old',
          created_at: '2026-06-01T00:00:00+00:00',
          last_active_at: '2026-06-02T00:00:00+00:00',
        },
      ],
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'builder-session': [
            {
              id: 'builder-reply',
              role: 'assistant',
              content: 'Builder project reply',
            },
          ],
          'builder-old': [
            {
              id: 'builder-old-reply',
              role: 'assistant',
              content: 'Older builder reply',
            },
          ],
        },
      }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Builder project reply'),
      100,
    );

    document.querySelector('.chat-sessions-toggle')?.click();
    flushSync();

    await waitForCondition(
      () => document.querySelectorAll('.session-row__select').length === 2,
      100,
    );
    // The drawer lists the PROJECT agent's sessions through the full address,
    // not the bare team-member id (which would hit the wrong world).
    expect(listSessionsMock).toHaveBeenCalledWith('builder@vbot');

    const oldRow = Array.from(
      document.querySelectorAll('.session-row__select'),
    ).find((button) => button.textContent.includes('builder-old'));
    expect(oldRow).toBeTruthy();
    oldRow.click();
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Older builder reply'),
      100,
    );
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'builder@vbot',
      session_id: 'builder-old',
      limit: 100,
    });
    // A same-agent older session is a past-session view, not a sub-agent view.
    expect(document.body.textContent).toContain('Viewing a past session');
  });

  it('returns from a sub-agent session to its parent session (item 4)', async () => {
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'sub-session-1',
          subagent_parent: {
            agent_id: 'alpha',
            session_id: 'session-parent',
          },
        },
      ],
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-parent': [
            {
              id: 'parent-reply',
              role: 'assistant',
              content: 'Parent history',
            },
          ],
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
    // With resolvable parent metadata the button targets the parent session.
    await waitForCondition(
      () => Boolean(findButtonByText('Return to parent session')),
      100,
    );

    findButtonByText('Return to parent session').click();
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Parent history'),
      100,
    );
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'session-parent',
      limit: 100,
    });
    // The parent is not the agent's current session, so the result is a
    // normal past-session view with its own banner — the desired chain.
    expect(document.body.textContent).toContain('Viewing a past session');
  });

  it('falls back to return-to-current when the child has no parent metadata (item 4)', async () => {
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

    // No subagent_parent metadata (old child session) → the button keeps the
    // return-to-current copy and behavior.
    expect(findButtonByText('Return to parent session')).toBeFalsy();
    const returnButton = findButtonByText('Return to current session');
    expect(returnButton).toBeTruthy();
    returnButton.click();
    flushSync();

    await waitForCondition(
      () =>
        document.body.textContent.includes('Hello') &&
        !document.body.textContent.includes('Viewing a sub-agent session'),
      100,
    );
  });

  it('keeps the displayed override session and its stream on a roster refresh (item 6)', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        activeRuns: {
          'sub-session-1': {
            run_id: 'active-sub-run',
            sse_url: '/api/runs/active-sub-run/events',
            status: 'running',
            events: [],
          },
        },
      }),
    );
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const parentHarness = createChatViewParentHarness();

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
        get agentsRefreshToken() {
          return parentHarness.agentsRefreshToken;
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );
    const subscription = subscribeRunEventsMock.mock.results[0].value;
    const currentHistoryCalls = () =>
      rpcMock.mock.calls.filter(
        ([method, params]) =>
          method === 'chat.history' && params?.session_id === 'session-1',
      ).length;
    const callsBefore = currentHistoryCalls();

    // An agent-roster refresh (any agent CRUD anywhere) must not steal the
    // viewed session's display, its live SSE subscription, or the composer.
    parentHarness.bumpAgentsRefreshToken();
    flushSync();
    await waitForCondition(
      () =>
        rpcMock.mock.calls.filter(([method]) => method === 'agent.list')
          .length >= 2,
      100,
    );

    expect(currentHistoryCalls()).toBe(callsBefore);
    expect(subscribeRunEventsMock.mock.calls.length).toBe(1);
    expect(subscription.close).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain('Viewing a sub-agent session');
    expect(document.querySelector('textarea')?.disabled).toBe(false);
  });

  it('drops a stale history response for a session that is no longer displayed (item 7)', async () => {
    let releaseChildHistory;
    const childHistoryGate = new Promise((resolve) => {
      releaseChildHistory = resolve;
    });
    const baseMock = createChatRpcMock();
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'chat.history' && params?.session_id === 'sub-session-1') {
        await childHistoryGate;
        return {
          session_id: 'sub-session-1',
          messages: [],
          active_run: {
            run_id: 'stale-run',
            sse_url: '/api/runs/stale-run/events',
            status: 'running',
            events: [],
          },
        };
      }
      return baseMock(method, params);
    });
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const parentHarness = createChatViewParentHarness();

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        get pendingSessionNavigation() {
          return parentHarness.pendingSessionNavigation;
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    // Navigate into the child (its history hangs), then straight back to the
    // current session before the child response arrives.
    parentHarness.setPendingSessionNavigation({
      agentId: 'alpha',
      sessionId: 'sub-session-1',
      subAgent: true,
      requestId: 1,
    });
    flushSync();
    parentHarness.setPendingSessionNavigation({
      returnToCurrent: true,
      requestId: 2,
    });
    flushSync();

    await waitForCondition(
      () =>
        document.body.textContent.includes('Hello') &&
        !document.body.textContent.includes('Viewing a sub-agent session'),
      100,
    );

    // The stale child response lands after the user already left the session:
    // it must not re-open an SSE subscription for the left session, must not
    // banner-error the healthy view, and must not lock the composer.
    releaseChildHistory();
    await waitForCondition(
      () => !document.body.textContent.includes('Loading chat history'),
      100,
    );

    expect(
      subscribeRunEventsMock.mock.calls.filter(
        ([sseUrl]) => sseUrl === '/api/runs/stale-run/events',
      ),
    ).toHaveLength(0);
    expect(document.querySelector('.chat-view__error')).toBeNull();
    expect(document.querySelector('textarea')?.disabled).toBe(false);
    expect(document.body.textContent).toContain('Hello');
  });

  it('prioritizes provider setup before model selection', async () => {
    const onConnectProvider = vi.fn();
    const onPickModel = vi.fn();
    rpcMock.mockImplementation(
      createChatRpcMock({ agents: [createAgent({ model: '' })] }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent({ model: '' })],
        sharedSelectedAgentId: 'alpha',
        hasConnectedProvider: false,
        onConnectProvider,
        onPickModel,
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Connect a provider to start'),
      100,
    );
    expect(document.body.textContent).not.toContain('Pick a model to start');

    findButtonByText('Connect a provider')?.click();
    flushSync();
    expect(onConnectProvider).toHaveBeenCalledTimes(1);
    expect(onPickModel).not.toHaveBeenCalled();
  });

  it('shows model selection once a provider is connected', async () => {
    const onPickModel = vi.fn();
    rpcMock.mockImplementation(
      createChatRpcMock({ agents: [createAgent({ model: '' })] }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent({ model: '' })],
        sharedSelectedAgentId: 'alpha',
        hasConnectedProvider: true,
        onPickModel,
      },
    });
    flushSync();

    await waitForCondition(
      () =>
        Array.from(document.querySelectorAll('.chat-view__footer-banner')).some(
          (element) => element.textContent.includes('Pick a model'),
        ),
      100,
    );
    expect(document.body.textContent).toContain('Pick a model to start');

    findButtonByText('Choose a model')?.click();
    flushSync();
    expect(onPickModel).toHaveBeenCalledTimes(1);
  });

  it('waits for provider state before showing a setup notice', () => {
    rpcMock.mockImplementation(
      createChatRpcMock({ agents: [createAgent({ model: '' })] }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent({ model: '' })],
        sharedSelectedAgentId: 'alpha',
        hasConnectedProvider: null,
      },
    });
    flushSync();

    expect(document.body.textContent).not.toContain(
      'Connect a provider to start',
    );
    expect(document.body.textContent).not.toContain('Pick a model to start');
  });

  it('hides the no-model notice when the current agent has a model', () => {
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
      Array.from(document.querySelectorAll('.chat-view__footer-banner')).some(
        (element) => element.textContent.includes('Pick a model'),
      ),
    ).toBe(false);
  });
});
