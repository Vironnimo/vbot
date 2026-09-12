// @vitest-environment jsdom
import {
  describe,
  activeAgentTab,
  createAgent,
  createChatRpcMock,
  expect,
  findButtonByText,
  flushSync,
  it,
  listSessionActivityMock,
  rpcMock,
  setupChatViewTestSuite,
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
});
