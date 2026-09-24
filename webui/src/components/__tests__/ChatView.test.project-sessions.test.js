// @vitest-environment jsdom

import {
  describe,
  createAgent,
  createChatRpcMock,
  expect,
  findButtonByText,
  flushSync,
  getSessionMock,
  it,
  listQueueMock,
  listSessionActivityMock,
  listSessionsMock,
  rpcMock,
  selectAgentFromPicker,
  selectedPersonalAgentName,
  sendComposerMessage,
  setupChatViewTestSuite,
  showProjectMock,
  subscribeRunEventsMock,
  vi,
  waitForCondition,
} from './ChatView.support.js';

// Serve `session.get` point reads from `sessions` keyed `address::sessionId`.
function serveSessions(sessions) {
  getSessionMock.mockImplementation(async (agentAddress, sessionId) => ({
    session: sessions[`${agentAddress}::${sessionId}`] ?? null,
  }));
}

// A two-member Team (Orchestrator is the project default) whose Session
// activity is served from `unreadResults` (address -> { sessionId, runId }),
// so a test can reveal a finished result later and signal it (a scoped Session
// invalidation or a full Sessions refresh). `session.mark_read` acknowledges
// the result the way the server does.
async function mountTeamWithUnreadResults(chatViewTest, unreadResults) {
  showProjectMock.mockResolvedValue({
    project: { project_id: 'vbot', default_agent: 'orchestrator' },
    scan: {
      team: [
        { agent_id: 'orchestrator', display_name: 'Orchestrator', model: 'm' },
        { agent_id: 'explorer', display_name: 'Explorer', model: 'm' },
      ],
      report: { clean: true, findings: [] },
    },
  });
  const landingSessions = {
    'orchestrator@vbot': 'orch-session',
    'explorer@vbot': 'explorer-held',
  };
  listSessionsMock.mockImplementation(async (agentAddress) => ({
    sessions: landingSessions[agentAddress]
      ? [{ id: landingSessions[agentAddress] }]
      : [],
  }));
  const readRunIds = new Set();
  const baseRpc = createChatRpcMock({
    sessionMessages: {
      'orch-session': [
        { id: 'orch-reply', role: 'assistant', content: 'Orchestrator chat' },
      ],
      'orch-unread': [
        {
          id: 'orch-result',
          role: 'assistant',
          content: 'Orchestrator unread result',
        },
        {
          id: 'orch-summary',
          role: 'run_summary',
          run_id: 'run-orch',
          status: 'completed',
        },
      ],
      'explorer-held': [
        {
          id: 'explorer-reply',
          role: 'assistant',
          content: 'Explorer earlier',
        },
      ],
      'explorer-unread': [
        {
          id: 'explorer-result',
          role: 'assistant',
          content: 'Explorer unread result',
        },
        {
          id: 'explorer-summary',
          role: 'run_summary',
          run_id: 'run-explorer',
          status: 'completed',
        },
      ],
    },
  });
  rpcMock.mockImplementation(async (method, params) => {
    if (method === 'session.mark_read') {
      readRunIds.add(params.run_id);
    }
    return baseRpc(method, params);
  });
  listSessionActivityMock.mockImplementation(async (addresses) => ({
    agents: addresses.map((address) => {
      const [agentId, projectId] = address.split('@');
      const result = unreadResults[address];
      const read = result ? readRunIds.has(result.runId) : false;
      return {
        agent_id: agentId,
        project_id: projectId ?? null,
        sessions: result
          ? [
              {
                id: result.sessionId,
                latest_completion_run_id: result.runId,
                has_unread_completion: !read,
                unread_run_id: read ? null : result.runId,
                unread_run_status: read ? null : 'completed',
                unread_run_at: read ? null : '2026-09-01T10:00:00+00:00',
              },
            ]
          : [],
      };
    }),
  }));
  const { createChatViewParentHarness } =
    await import('./chatViewParentHarness.svelte.js');
  const parentHarness = createChatViewParentHarness();

  chatViewTest.mount({
    target: document.body,
    props: {
      sharedAgents: [createAgent()],
      sharedSelectedAgentId: 'alpha',
      projects: [{ project_id: 'vbot', display_name: 'vBot' }],
      selectedProjectId: 'vbot',
      get sessionsRefreshToken() {
        return parentHarness.sessionsRefreshToken;
      },
      get sessionInvalidations() {
        return parentHarness.sessionInvalidations;
      },
    },
  });
  flushSync();
  await waitForCondition(
    () => document.body.textContent.includes('Orchestrator chat'),
    100,
  );
  return parentHarness;
}

function teamTab(name) {
  return Array.from(
    document.querySelectorAll('.chat-view__project-team .agent-tab'),
  ).find((tab) => tab.textContent.includes(name));
}

function teamTabIsUnread(name) {
  return Boolean(teamTab(name)?.querySelector('.tab-indicator--unread'));
}

function markedRead(agentId, sessionId, runId) {
  return rpcMock.mock.calls.some(
    ([method, params]) =>
      method === 'session.mark_read' &&
      params?.agent_id === agentId &&
      params?.session_id === sessionId &&
      params?.run_id === runId,
  );
}

describe('ChatView', () => {
  const chatViewTest = setupChatViewTestSuite();

  it('waits before showing initial History feedback', async () => {
    const HISTORY_LOADING_FEEDBACK_DELAY_MS = 300;
    let resolveHistory;
    const defaultRpc = createChatRpcMock();
    rpcMock.mockImplementation((method, params) => {
      if (method === 'chat.history') {
        return new Promise((resolve) => {
          resolveHistory = resolve;
        });
      }
      return defaultRpc(method, params);
    });
    vi.useFakeTimers();

    chatViewTest.mount({ target: document.body });
    await vi.advanceTimersByTimeAsync(0);
    flushSync();

    expect(document.body.textContent).not.toContain('Loading chat history');

    await vi.advanceTimersByTimeAsync(HISTORY_LOADING_FEEDBACK_DELAY_MS - 1);
    flushSync();
    expect(document.body.textContent).not.toContain('Loading chat history');

    await vi.advanceTimersByTimeAsync(1);
    flushSync();
    expect(document.body.textContent).toContain('Loading chat history');

    resolveHistory({
      active_run: null,
      has_more: false,
      messages: [],
      session_id: 'session-1',
    });
    await vi.advanceTimersByTimeAsync(0);
    flushSync();

    expect(document.body.textContent).not.toContain('Loading chat history');
  });

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
      () =>
        Boolean(
          document.querySelector('button[aria-label="Open Sub-Agent Session"]'),
        ),
      100,
    );

    document
      .querySelector('button[aria-label="Open Sub-Agent Session"]')
      .click();
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
          title: 'Older builder topic',
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
        hasConnectedProvider: false,
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Builder project reply'),
      100,
    );

    findButtonByText('Sessions')?.click();
    flushSync();

    await waitForCondition(
      () => document.querySelectorAll('.session-row__select').length === 2,
      100,
    );
    // The drawer lists the PROJECT agent's sessions through the full address,
    // not the bare team-member id (which would hit the wrong world).
    expect(listSessionsMock).toHaveBeenCalledWith(
      'builder@vbot',
      expect.objectContaining({ limit: 1, includeSubagents: false }),
    );

    const oldRow = Array.from(
      document.querySelectorAll('.session-row__select'),
    ).find((button) => button.textContent.includes('Older builder topic'));
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
    // Session selection is ordinary navigation. The backend's default-session
    // pointer does not make this usable Session "past" or require a return
    // warning.
    expect(document.body.textContent).not.toContain('Viewing a past session');
    expect(findButtonByText('Return to current session')).toBeFalsy();
    expect(document.body.textContent).toContain('Connect a provider to start');
  });

  it('releases a deleted Project Agent Session so reopening the Agent lands elsewhere', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [{ agent_id: 'builder', display_name: 'Builder', model: 'm' }],
        report: { clean: true, findings: [] },
      },
    });
    const newest = {
      id: 'builder-session',
      title: 'Newest builder topic',
      created_at: '2026-06-05T00:00:00+00:00',
      last_active_at: '2026-06-10T00:00:00+00:00',
    };
    const older = {
      id: 'builder-old',
      title: 'Older builder topic',
      created_at: '2026-06-01T00:00:00+00:00',
      last_active_at: '2026-06-02T00:00:00+00:00',
    };
    let deleted = false;
    listSessionsMock.mockImplementation(async () => ({
      sessions: deleted ? [older] : [newest, older],
    }));
    const baseRpc = createChatRpcMock({
      sessionMessages: {
        'builder-session': [
          { id: 'newest-reply', role: 'assistant', content: 'Newest reply' },
        ],
        'builder-old': [
          { id: 'older-reply', role: 'assistant', content: 'Older reply' },
        ],
      },
    });
    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'session.delete') {
        deleted = true;
        return { ...params, next_session_id: 'builder-old' };
      }
      if (
        deleted &&
        method === 'chat.history' &&
        params.session_id === 'builder-session'
      ) {
        throw new Error('Session not found: builder-session');
      }
      return baseRpc(method, params);
    });
    const historyReads = (sessionId) =>
      rpcMock.mock.calls.filter(
        ([method, params]) =>
          method === 'chat.history' && params.session_id === sessionId,
      ).length;

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
      },
    });
    await waitForCondition(
      () => document.body.textContent.includes('Newest reply'),
      100,
    );

    findButtonByText('Sessions')?.click();
    await waitForCondition(
      () => document.querySelectorAll('.session-row').length === 2,
      100,
    );
    Array.from(document.querySelectorAll('.session-row'))
      .find((row) => row.textContent.includes('Newest builder topic'))
      .querySelector('.session-row__menu-trigger')
      .click();
    flushSync();
    document.querySelector('.session-row__menu-item--danger').click();
    flushSync();
    findButtonByText('Delete')?.click();
    await waitForCondition(
      () => document.body.textContent.includes('Older reply'),
      100,
    );
    const deletedReads = historyReads('builder-session');

    // Leave for the Identity Agent, then reopen the Project Agent.
    await selectAgentFromPicker('Alpha');
    await waitForCondition(() => selectedPersonalAgentName() === 'Alpha', 100);
    const landingReads = historyReads('builder-old');
    document.querySelector('.chat-view__project-team .agent-tab').click();
    await waitForCondition(
      () =>
        historyReads('builder-old') > landingReads ||
        historyReads('builder-session') > deletedReads,
      100,
    );

    expect(historyReads('builder-session')).toBe(deletedReads);
    expect(document.body.textContent).toContain('Older reply');
  });

  it('opens the unread Session of a Team member that holds an older Session and marks it read', async () => {
    const unreadResults = {};
    const parentHarness = await mountTeamWithUnreadResults(
      chatViewTest,
      unreadResults,
    );

    // Explorer lands on (and holds) its earlier Session, then the user
    // returns to the Orchestrator.
    teamTab('Explorer').click();
    await waitForCondition(
      () => document.body.textContent.includes('Explorer earlier'),
      100,
    );
    teamTab('Orchestrator').click();
    await waitForCondition(
      () => document.body.textContent.includes('Orchestrator chat'),
      100,
    );

    // A result finishes in another Explorer Session.
    unreadResults['explorer@vbot'] = {
      sessionId: 'explorer-unread',
      runId: 'run-explorer',
    };
    parentHarness.bumpSessionsRefreshToken();
    await waitForCondition(() => teamTabIsUnread('Explorer'), 100);

    teamTab('Explorer').click();
    await waitForCondition(
      () => document.body.textContent.includes('Explorer unread result'),
      100,
    );
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'explorer@vbot',
      session_id: 'explorer-unread',
      limit: 100,
    });
    expect(document.body.textContent).not.toContain('Explorer earlier');
    expect(teamTab('Explorer').classList.contains('active')).toBe(true);
    await waitForCondition(
      () => markedRead('explorer@vbot', 'explorer-unread', 'run-explorer'),
      100,
    );

    // The acknowledged result stays read once another Agent is displayed.
    teamTab('Orchestrator').click();
    await waitForCondition(
      () => document.body.textContent.includes('Orchestrator chat'),
      100,
    );
    expect(teamTabIsUnread('Explorer')).toBe(false);
  });

  it('opens the unread Session of a never-opened Team member', async () => {
    await mountTeamWithUnreadResults(chatViewTest, {
      'explorer@vbot': { sessionId: 'explorer-unread', runId: 'run-explorer' },
    });
    await waitForCondition(() => teamTabIsUnread('Explorer'), 100);

    teamTab('Explorer').click();
    await waitForCondition(
      () => document.body.textContent.includes('Explorer unread result'),
      100,
    );
    expect(teamTab('Explorer').classList.contains('active')).toBe(true);
    await waitForCondition(
      () => markedRead('explorer@vbot', 'explorer-unread', 'run-explorer'),
      100,
    );
    expect(teamTabIsUnread('Explorer')).toBe(false);
  });

  it('switches the selected Team member to its unread Session when clicked again', async () => {
    const unreadResults = {};
    const parentHarness = await mountTeamWithUnreadResults(
      chatViewTest,
      unreadResults,
    );

    await waitForCondition(
      () =>
        listSessionActivityMock.mock.calls.some(([addresses]) =>
          addresses.includes('explorer@vbot'),
        ),
      100,
    );
    listSessionActivityMock.mockClear();
    // A result finishes in another Session of the displayed Orchestrator.
    // This window holds no event of that Run, so only the named Agent's
    // activity is read again.
    unreadResults['orchestrator@vbot'] = {
      sessionId: 'orch-unread',
      runId: 'run-orch',
    };
    parentHarness.pushSessionInvalidation({
      project_id: 'vbot',
      agent_id: 'orchestrator',
      session_id: 'orch-unread',
      run_id: 'run-orch',
    });
    await waitForCondition(() => teamTabIsUnread('Orchestrator'), 100);
    expect(listSessionActivityMock.mock.calls).toEqual([
      [['orchestrator@vbot']],
    ]);

    teamTab('Orchestrator').click();
    await waitForCondition(
      () => document.body.textContent.includes('Orchestrator unread result'),
      100,
    );
    expect(document.body.textContent).not.toContain('Orchestrator chat');
    await waitForCondition(
      () => markedRead('orchestrator@vbot', 'orch-unread', 'run-orch'),
      100,
    );
    expect(teamTabIsUnread('Orchestrator')).toBe(false);
  });

  it('returns from a sub-agent session to its parent session (item 4)', async () => {
    serveSessions({
      'alpha::sub-session-1': {
        id: 'sub-session-1',
        subagent_parent: { agent_id: 'alpha', session_id: 'session-parent' },
      },
      'alpha::session-parent': {
        id: 'session-parent',
        title: 'Parent planning',
      },
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

    document.querySelector('.chat-activity__rail').click();
    flushSync();
    await waitForCondition(
      () =>
        document
          .querySelector('.chat-activity__parent-link')
          ?.textContent.trim() === 'Parent planning',
      100,
    );
    document.querySelector('.chat-activity__parent-link').click();
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
    // The parent opens as an ordinary Session. Only the cross-owner sub-agent
    // view needs a contextual return banner.
    expect(document.body.textContent).not.toContain(
      'Viewing a sub-agent session',
    );
    expect(findButtonByText('Return to current session')).toBeFalsy();
  });

  it('keeps parent navigation and Agent selection aligned through nested sub-agent sessions', async () => {
    const agents = [
      createAgent({
        id: 'alpha',
        name: 'Alpha',
        current_session_id: 'root-session',
      }),
      createAgent({
        id: 'beta',
        name: 'Beta',
        current_session_id: 'beta-current',
      }),
      createAgent({
        id: 'gamma',
        name: 'Gamma',
        current_session_id: 'gamma-current',
      }),
    ];
    serveSessions({
      'gamma::grandchild-session': {
        id: 'grandchild-session',
        is_subagent_session: true,
        subagent_parent: { agent_id: 'beta', session_id: 'child-session' },
      },
      'beta::child-session': {
        id: 'child-session',
        is_subagent_session: true,
        subagent_parent: { agent_id: 'alpha', session_id: 'root-session' },
      },
      'alpha::root-session': { id: 'root-session' },
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents,
        sessionMessages: {
          'root-session': [
            {
              id: 'root-reply',
              role: 'assistant',
              content: 'Root history',
            },
          ],
          'child-session': [
            {
              id: 'child-reply',
              role: 'assistant',
              content: 'Child history',
            },
          ],
          'grandchild-session': [
            {
              id: 'grandchild-reply',
              role: 'assistant',
              content: 'Grandchild history',
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
          agentId: 'gamma',
          sessionId: 'grandchild-session',
          subAgent: true,
        },
      },
    });
    flushSync();

    await waitForCondition(
      () =>
        document.body.textContent.includes('Grandchild history') &&
        Boolean(findButtonByText('Return to parent session')),
      100,
    );
    expect(selectedPersonalAgentName()).toBe('Gamma');

    findButtonByText('Return to parent session').click();
    flushSync();

    await waitForCondition(
      () =>
        document.body.textContent.includes('Child history') &&
        Boolean(findButtonByText('Return to parent session')),
      100,
    );
    expect(document.body.textContent).toContain('Viewing a sub-agent session');
    expect(selectedPersonalAgentName()).toBe('Beta');

    findButtonByText('Return to parent session').click();
    flushSync();

    await waitForCondition(
      () =>
        document.body.textContent.includes('Root history') &&
        !document.body.textContent.includes('Viewing a sub-agent session'),
      100,
    );
    expect(findButtonByText('Return to parent session')).toBeFalsy();
    expect(selectedPersonalAgentName()).toBe('Alpha');
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

  it('lands a Session moved into a Team member on that Session, not its unread one', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [
          { agent_id: 'builder', display_name: 'Builder', model: 'm' },
          { agent_id: 'reviewer', display_name: 'Reviewer', model: 'm' },
        ],
        report: { clean: true, findings: [] },
      },
    });
    listSessionsMock.mockResolvedValue({
      sessions: [{ id: 'builder-session' }],
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'builder-session': [
            { id: 'b1', role: 'assistant', content: 'Builder project reply' },
          ],
          'reviewer-unread': [
            { id: 'r1', role: 'assistant', content: 'Reviewer unread result' },
            {
              id: 'r2',
              role: 'run_summary',
              run_id: 'run-r',
              status: 'completed',
            },
          ],
        },
        streamHandler: ({ content }) => {
          if (content === '/agent reviewer@vbot') {
            return {
              command_handled: true,
              reply: 'Moved to reviewer@vbot.',
              output: 'action',
              data: {
                command: 'agent',
                session_id: 'builder-session',
                agent_id: 'reviewer@vbot',
              },
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );
    listSessionActivityMock.mockImplementation(async (ids) => ({
      agents: ids.map((address) => {
        const [agentId, projectId] = address.split('@');
        return {
          agent_id: agentId,
          project_id: projectId ?? null,
          sessions:
            address === 'reviewer@vbot'
              ? [
                  {
                    id: 'reviewer-unread',
                    latest_completion_run_id: 'run-r',
                    has_unread_completion: true,
                    unread_run_id: 'run-r',
                    unread_run_status: 'completed',
                    unread_run_at: '2026-08-05T18:41:24+00:00',
                  },
                ]
              : [],
        };
      }),
    }));
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
    await waitForCondition(
      () =>
        Boolean(
          document.querySelector(
            '.chat-view__project-team .tab-indicator--unread',
          ),
        ),
      100,
    );
    rpcMock.mockClear();

    sendComposerMessage('/agent reviewer@vbot');

    const historyCalls = () =>
      rpcMock.mock.calls
        .filter(([method]) => method === 'chat.history')
        .map(([, params]) => `${params.agent_id}::${params.session_id}`);
    await waitForCondition(
      () => historyCalls().includes('reviewer@vbot::builder-session'),
      100,
    );
    expect(historyCalls()).not.toContain('reviewer@vbot::reviewer-unread');
  });
});
