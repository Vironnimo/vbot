// @vitest-environment jsdom
import {
  describe,
  createAgent,
  createChatRpcMock,
  expect,
  findButtonByText,
  findCancelRunButton,
  findNewSessionButton,
  flushSync,
  it,
  listSessionsMock,
  rpcMock,
  sendComposerMessage,
  setInputValue,
  testChatStateRefs,
  waitForCondition,
} from './ChatView.support.js';
import { setupChatSessionNavigationSuite } from './ChatView.sessions-and-subagents.support.js';

describe('ChatView', () => {
  const suite = setupChatSessionNavigationSuite();

  it('renders a restored Reflection in Session info and opens its Session', async () => {
    const baseRpc = createChatRpcMock({
      sessionMessages: {
        'review-session': [
          {
            id: 'review-result',
            role: 'assistant',
            content: 'Restored review result',
          },
        ],
      },
    });
    rpcMock.mockImplementation(async (method, params) => {
      const result = await baseRpc(method, params);
      if (method === 'chat.history' && params.session_id === 'session-1') {
        return {
          ...result,
          reflection_runs: [
            {
              run_id: 'review-run',
              session_id: 'review-session',
              run_kind: 'memory_reflection',
              status: 'completed',
              started_at: '2026-09-05T10:00:00Z',
            },
          ],
        };
      }
      return result;
    });
    suite.chatViewTest.mount({ target: document.body });
    flushSync();
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );
    document.querySelector('.chat-activity__rail').click();
    await waitForCondition(
      () => document.querySelector('.chat-activity__task-link'),
      100,
    );
    const reviewLink = document.querySelector('.chat-activity__task-link');
    expect(reviewLink.getAttribute('aria-label')).toContain('Completed');
    reviewLink.click();
    await waitForCondition(
      () => document.body.textContent.includes('Restored review result'),
      100,
    );
  });

  it('reuses an already empty session and focuses its composer', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({ sessionMessages: { 'session-1': [] } }),
    );

    suite.chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' && params?.session_id === 'session-1',
        ),
      100,
    );

    const newSessionButton = findNewSessionButton();
    const composerInput = document.querySelector('.msg-input');
    expect(newSessionButton).toBeTruthy();
    expect(composerInput).toBeTruthy();

    newSessionButton.focus();
    newSessionButton.click();
    newSessionButton.click();

    await waitForCondition(() => document.activeElement === composerInput, 100);
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'session.create'),
    ).toHaveLength(0);
  });

  it('creates a new session when the empty transcript has a draft and preserves that draft', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-1': [],
          'created-alpha': [],
        },
      }),
    );
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'created-alpha',
          created_at: '2026-05-10T00:00:00+00:00',
          last_active_at: '2026-05-10T00:00:00+00:00',
        },
        {
          id: 'session-1',
          title: 'Draft home',
          created_at: '2026-05-09T00:00:00+00:00',
          last_active_at: '2026-05-09T00:00:00+00:00',
        },
      ],
    });

    suite.chatViewTest.mount({ target: document.body });
    flushSync();
    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' && params?.session_id === 'session-1',
        ),
      100,
    );

    const composerInput = document.querySelector('.msg-input');
    setInputValue(composerInput, 'unfinished draft');
    flushSync();
    findNewSessionButton().click();

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' && params?.session_id === 'created-alpha',
        ),
      100,
    );
    expect(composerInput.value).toBe('');

    findButtonByText('Sessions').click();
    await waitForCondition(() => Boolean(findButtonByText('Draft home')), 100);
    findButtonByText('Draft home').click();
    await waitForCondition(
      () => composerInput.value === 'unfinished draft',
      100,
    );
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'session.create'),
    ).toHaveLength(1);
    setInputValue(composerInput, '');
    flushSync();
  });

  it('creates one session for repeated clicks from a non-empty session and focuses it', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({ sessionMessages: { 'created-alpha': [] } }),
    );

    suite.chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const newSessionButton = findNewSessionButton();
    expect(newSessionButton).toBeTruthy();
    newSessionButton.click();
    newSessionButton.click();

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' && params?.session_id === 'created-alpha',
        ),
      100,
    );
    const composerInput = document.querySelector('.msg-input');
    await waitForCondition(() => document.activeElement === composerInput, 100);

    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'session.create'),
    ).toHaveLength(1);
  });

  it('starts a Run in a new Session while the previous Session Run remains active', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: { 'created-alpha': [] },
        activeRuns: {
          'session-1': {
            run_id: 'run-one',
            sse_url: '/api/runs/run-one/events',
            status: 'running',
            events: [],
          },
        },
        streamHandler: (params) => ({
          run_id: 'run-two',
          session_id: params.session_id,
          sse_url: '/api/runs/run-two/events',
          status: 'running',
          events: [],
        }),
      }),
    );

    suite.chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(() => Boolean(findCancelRunButton()), 100);

    const newSessionButton = findNewSessionButton();
    expect(newSessionButton).toBeTruthy();
    expect(newSessionButton.disabled).toBe(false);
    newSessionButton.click();

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' && params?.session_id === 'created-alpha',
        ),
      100,
    );

    sendComposerMessage('Run in parallel');
    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.stream' && params?.session_id === 'created-alpha',
        ),
      100,
    );

    const sessionStates = testChatStateRefs[0].sessions;
    expect(sessionStates['alpha::session-1'].status).toBe('running');
    expect(sessionStates['alpha::session-1'].currentRun?.runId).toBe('run-one');
    expect(sessionStates['alpha::created-alpha'].status).toBe('running');
    expect(sessionStates['alpha::created-alpha'].currentRun?.runId).toBe(
      'run-two',
    );
  });

  it('focuses the composer after a user selects another session', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-2': [
            {
              id: 'assistant-two',
              role: 'assistant',
              content: 'Second session reply',
            },
          ],
        },
      }),
    );
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'session-2',
          title: 'Second topic',
          created_at: '2026-05-10T00:00:00+00:00',
          last_active_at: '2026-05-10T01:00:00+00:00',
        },
        {
          id: 'session-1',
          created_at: '2026-05-09T00:00:00+00:00',
          last_active_at: '2026-05-09T01:00:00+00:00',
        },
      ],
    });

    suite.chatViewTest.mount({ target: document.body });
    flushSync();
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    findButtonByText('Sessions').click();
    await waitForCondition(
      () => Boolean(findButtonByText('Second topic')),
      100,
    );
    findButtonByText('Second topic').click();

    await waitForCondition(
      () => document.body.textContent.includes('Second session reply'),
      100,
    );
    const composerInput = document.querySelector('.msg-input');
    await waitForCondition(() => document.activeElement === composerInput, 100);
  });

  it('does not steal focus when browser history changes the displayed session', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-2': [
            {
              id: 'assistant-two',
              role: 'assistant',
              content: 'History-restored session reply',
            },
          ],
        },
      }),
    );
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const parentHarness = createChatViewParentHarness();

    suite.chatViewTest.mount({
      target: document.body,
      props: {
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

    const passiveFocusTarget = document.createElement('button');
    document.body.append(passiveFocusTarget);
    passiveFocusTarget.focus();
    parentHarness.setPendingSessionNavigation({
      agentId: 'alpha',
      sessionId: 'session-2',
      subAgent: false,
      requestId: 'history-navigation-1',
    });
    flushSync();

    await waitForCondition(
      () =>
        document.body.textContent.includes('History-restored session reply'),
      100,
    );
    expect(document.activeElement).toBe(passiveFocusTarget);
  });

  it('focuses after a user Agent switch but not after a passive Agent update', async () => {
    const agents = [
      createAgent(),
      createAgent({
        id: 'beta',
        name: 'Beta',
        current_session_id: 'session-2',
      }),
    ];
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents,
        sessionMessages: {
          'session-2': [
            {
              id: 'beta-assistant',
              role: 'assistant',
              content: 'Beta session reply',
            },
          ],
        },
      }),
    );
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const parentHarness = createChatViewParentHarness();

    suite.chatViewTest.mount({
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

    findButtonByText('Beta').click();
    await waitForCondition(
      () => document.body.textContent.includes('Beta session reply'),
      100,
    );
    const composerInput = document.querySelector('.msg-input');
    await waitForCondition(() => document.activeElement === composerInput, 100);

    const passiveFocusTarget = document.createElement('button');
    document.body.append(passiveFocusTarget);
    passiveFocusTarget.focus();
    parentHarness.setSelectedAgentId('alpha');
    flushSync();
    await waitForCondition(
      () =>
        document.body.textContent.includes('Hello') &&
        !document.body.textContent.includes('Beta session reply'),
      100,
    );

    expect(document.activeElement).toBe(passiveFocusTarget);
  });

  it('keeps mobile Session navigation in reading mode but focuses New session', async () => {
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = () => ({ matches: true });
    try {
      rpcMock.mockImplementation(
        createChatRpcMock({
          sessionMessages: {
            'session-2': [
              {
                id: 'assistant-two',
                role: 'assistant',
                content: 'Mobile session reply',
              },
            ],
            'created-alpha': [],
          },
        }),
      );
      listSessionsMock.mockResolvedValue({
        sessions: [
          {
            id: 'session-2',
            title: 'Mobile topic',
            created_at: '2026-05-10T00:00:00+00:00',
            last_active_at: '2026-05-10T01:00:00+00:00',
          },
        ],
      });

      suite.chatViewTest.mount({ target: document.body });
      flushSync();
      await waitForCondition(
        () => document.body.textContent.includes('Hello'),
        100,
      );

      findButtonByText('Sessions').click();
      await waitForCondition(
        () => Boolean(findButtonByText('Mobile topic')),
        100,
      );
      const sessionButton = findButtonByText('Mobile topic');
      sessionButton.focus();
      sessionButton.click();
      await waitForCondition(
        () => document.body.textContent.includes('Mobile session reply'),
        100,
      );
      const composerInput = document.querySelector('.msg-input');
      expect(document.activeElement).not.toBe(composerInput);

      findNewSessionButton().click();
      await waitForCondition(
        () =>
          rpcMock.mock.calls.some(
            ([method, params]) =>
              method === 'chat.history' &&
              params?.session_id === 'created-alpha',
          ),
        100,
      );
      await waitForCondition(
        () => document.activeElement === composerInput,
        100,
      );
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });

  it('loads selected session history from the sessions drawer', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-1': [
            {
              id: 'assistant-one',
              role: 'assistant',
              content: 'Current session reply',
            },
          ],
          'ch-tg-assistant-12345': [
            {
              id: 'assistant-two',
              role: 'assistant',
              content: 'Telegram session reply',
            },
          ],
        },
      }),
    );
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'ch-tg-assistant-12345',
          created_at: '2026-05-10T11:00:00+00:00',
          last_active_at: '2026-05-11T09:30:00+00:00',
          source_channel_id: 'tg-assistant',
          platform: 'telegram',
          platform_conv_id: '12345',
        },
        {
          id: 'session-1',
          created_at: '2026-05-09T00:00:00+00:00',
          last_active_at: '2026-05-09T01:00:00+00:00',
        },
      ],
    });

    suite.chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Current session reply'),
      100,
    );

    const sessionsButton = findButtonByText('Sessions');
    expect(sessionsButton).toBeTruthy();
    sessionsButton.click();

    await waitForCondition(
      () => document.body.textContent.includes('telegram/12345'),
      100,
    );

    const telegramSessionButton = findButtonByText('telegram/12345');
    expect(telegramSessionButton).toBeTruthy();
    telegramSessionButton.click();

    await waitForCondition(
      () => document.body.textContent.includes('Telegram session reply'),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'ch-tg-assistant-12345',
      limit: 100,
    });
  });

  it('renders unlinked sessions as selection-only rows in the sessions drawer', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'session-legacy',
          created_at: '2026-05-09T00:00:00+00:00',
          last_active_at: '2026-05-09T01:00:00+00:00',
        },
      ],
    });

    suite.chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const sessionsButton = findButtonByText('Sessions');
    expect(sessionsButton).toBeTruthy();
    sessionsButton.click();

    await waitForCondition(
      () => Boolean(document.querySelector('.session-row__select')),
      100,
    );

    expect(document.querySelector('.session-row__select')).toBeTruthy();
    expect(findButtonByText('Link to channel')).toBeFalsy();
    expect(document.querySelector('input[name="channel-id"]')).toBeNull();
  });

  it('renders sub-agent session metadata in the sessions drawer', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'child-session',
          is_subagent_session: true,
          subagent_parent: {
            agent_id: 'orchestrator',
            session_id: 'parent-session',
          },
          last_active_at: '2026-05-09T01:00:00+00:00',
        },
      ],
    });

    suite.chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const sessionsButton = findButtonByText('Sessions');
    expect(sessionsButton).toBeTruthy();
    sessionsButton.click();

    await waitForCondition(
      () => document.querySelector('.session-drawer__filter-trigger'),
      100,
    );
    expect(document.body.textContent).not.toContain('child-session');

    document.querySelector('.session-drawer__filter-trigger').click();
    flushSync();
    const subagentSwitch = [
      ...document.querySelectorAll(
        '.session-drawer__filter-menu [role="switch"]',
      ),
    ].find(
      (candidate) => candidate.getAttribute('aria-label') === 'Subagent runs',
    );
    expect(subagentSwitch).toBeTruthy();
    subagentSwitch.click();
    flushSync();

    await waitForCondition(
      () => Boolean(document.querySelector('[data-session-marker="subagent"]')),
      100,
    );

    expect(
      document
        .querySelector('[data-session-marker="subagent"]')
        ?.getAttribute('aria-label'),
    ).toBe('Subagent');
    expect(document.body.textContent).not.toContain('Parent:');
    expect(document.body.textContent).not.toContain(
      'orchestrator/parent-session',
    );
  });

  it('opens the source Session from a Reflection Session info link', async () => {
    listSessionsMock.mockImplementation(async (_agentId, query = {}) => {
      const requiredSessionId = query.requiredSession?.sessionId;
      if (requiredSessionId === 'session-1') {
        return {
          sessions: [
            {
              id: 'session-1',
              run_kinds: ['reflection'],
              fork_source: {
                agent_id: 'alpha',
                session_id: 'source-session',
                project_id: null,
              },
            },
          ],
        };
      }
      if (requiredSessionId === 'source-session') {
        return {
          sessions: [
            {
              id: 'source-session',
              title: 'Original research',
            },
          ],
        };
      }
      return { sessions: [] };
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'source-session': [
            {
              id: 'source-reply',
              role: 'assistant',
              content: 'Original Session history',
            },
          ],
        },
      }),
    );

    suite.chatViewTest.mount({ target: document.body });
    flushSync();
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    document.querySelector('.chat-activity__rail').click();
    flushSync();
    await waitForCondition(
      () =>
        document
          .querySelector('.chat-activity__parent-link')
          ?.textContent.trim() === 'Original research',
      100,
    );

    document.querySelector('.chat-activity__parent-link').click();
    flushSync();
    await waitForCondition(
      () => document.body.textContent.includes('Original Session history'),
      100,
    );
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'source-session',
      limit: 100,
    });
  });
});
