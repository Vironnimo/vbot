// @vitest-environment jsdom

import {
  describe,
  applyConnectionSnapshotMock,
  cancelRunMock,
  closeSubscriptionForMock,
  createAgent,
  createChatRpcMock,
  expect,
  findButtonByText,
  findCancelRunButton,
  flushSync,
  it,
  listQueueMock,
  listSessionsMock,
  removeFromQueueMock,
  rpcMock,
  sendComposerMessage,
  setInputValue,
  setupChatViewTestSuite,
  subscribeRunEventsMock,
  testChatStateRefs,
  waitForCondition,
} from './ChatView.support.js';

describe('ChatView', () => {
  const chatViewTest = setupChatViewTestSuite();

  it('reuses an already empty session and focuses its composer', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({ sessionMessages: { 'session-1': [] } }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' && params?.session_id === 'session-1',
        ),
      100,
    );

    const newSessionButton = findButtonByText('New session');
    const composerInput = document.querySelector('#chat-composer-input');
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
          created_at: '2026-05-09T00:00:00+00:00',
          last_active_at: '2026-05-09T00:00:00+00:00',
        },
      ],
    });

    chatViewTest.mount({ target: document.body });
    flushSync();
    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' && params?.session_id === 'session-1',
        ),
      100,
    );

    const composerInput = document.querySelector('#chat-composer-input');
    setInputValue(composerInput, 'unfinished draft');
    flushSync();
    findButtonByText('New session').click();

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
    await waitForCondition(() => Boolean(findButtonByText('session-1')), 100);
    findButtonByText('session-1').click();
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

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const newSessionButton = findButtonByText('New session');
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
    const composerInput = document.querySelector('#chat-composer-input');
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

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(() => Boolean(findCancelRunButton()), 100);

    const newSessionButton = findButtonByText('New session');
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

    chatViewTest.mount({ target: document.body });
    flushSync();
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    findButtonByText('Sessions').click();
    await waitForCondition(() => Boolean(findButtonByText('session-2')), 100);
    findButtonByText('session-2').click();

    await waitForCondition(
      () => document.body.textContent.includes('Second session reply'),
      100,
    );
    const composerInput = document.querySelector('#chat-composer-input');
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

    chatViewTest.mount({
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

    findButtonByText('Beta').click();
    await waitForCondition(
      () => document.body.textContent.includes('Beta session reply'),
      100,
    );
    const composerInput = document.querySelector('#chat-composer-input');
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
            created_at: '2026-05-10T00:00:00+00:00',
            last_active_at: '2026-05-10T01:00:00+00:00',
          },
        ],
      });

      chatViewTest.mount({ target: document.body });
      flushSync();
      await waitForCondition(
        () => document.body.textContent.includes('Hello'),
        100,
      );

      findButtonByText('Sessions').click();
      await waitForCondition(() => Boolean(findButtonByText('session-2')), 100);
      const sessionButton = findButtonByText('session-2');
      sessionButton.focus();
      sessionButton.click();
      await waitForCondition(
        () => document.body.textContent.includes('Mobile session reply'),
        100,
      );
      const composerInput = document.querySelector('#chat-composer-input');
      expect(document.activeElement).not.toBe(composerInput);

      findButtonByText('New session').click();
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

    chatViewTest.mount({ target: document.body });
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

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const sessionsButton = findButtonByText('Sessions');
    expect(sessionsButton).toBeTruthy();
    sessionsButton.click();

    await waitForCondition(
      () => document.body.textContent.includes('session-legacy'),
      100,
    );

    expect(findButtonByText('session-legacy')).toBeTruthy();
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

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const sessionsButton = findButtonByText('Sessions');
    expect(sessionsButton).toBeTruthy();
    sessionsButton.click();

    await waitForCondition(
      () => document.body.textContent.includes('child-session'),
      100,
    );

    expect(document.body.textContent).toContain('Sub-agent');
    expect(document.body.textContent).toContain('Parent:');
    expect(document.body.textContent).toContain('orchestrator/parent-session');
  });

  it('applies a non-null connectionSnapshot prop to the run stream', async () => {
    const { createChatViewConnectionSnapshotHarness } =
      await import('./chatViewConnectionSnapshotHarness.svelte.js');
    const harness = createChatViewConnectionSnapshotHarness();
    const snapshot = {
      type: 'connection_ready',
      epoch: 'bus-epoch-1',
      last_sequence: 42,
      active_runs: [
        {
          run_id: 'run-snapshot-1',
          agent_id: 'alpha',
          session_id: 'session-1',
          status: 'running',
          sse_url: '/api/runs/run-snapshot-1/events',
        },
      ],
    };
    harness.setConnectionSnapshot(snapshot);

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        get connectionSnapshot() {
          return harness.connectionSnapshot;
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => applyConnectionSnapshotMock.mock.calls.length === 1,
      100,
    );

    expect(applyConnectionSnapshotMock).toHaveBeenCalledTimes(1);
    expect(applyConnectionSnapshotMock).toHaveBeenCalledWith(snapshot);
  });

  it('does not re-apply the same connectionSnapshot reference (dedup)', async () => {
    const { createChatViewConnectionSnapshotHarness } =
      await import('./chatViewConnectionSnapshotHarness.svelte.js');
    const harness = createChatViewConnectionSnapshotHarness();
    const snapshot = {
      type: 'connection_ready',
      epoch: 'bus-epoch-1',
      last_sequence: 42,
      active_runs: [],
    };
    harness.setConnectionSnapshot(snapshot);

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        get connectionSnapshot() {
          return harness.connectionSnapshot;
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => applyConnectionSnapshotMock.mock.calls.length === 1,
      100,
    );

    // Re-assign the harness to the same snapshot object. Svelte 5's `$state`
    // setter no-ops for the same reference, but the test still documents the
    // dedup contract: even if the effect re-runs for the same reference, the
    // call must not happen again.
    harness.setConnectionSnapshot(snapshot);
    flushSync();

    expect(applyConnectionSnapshotMock).toHaveBeenCalledTimes(1);
  });

  it('reconciles a stuck running session when chat.history reports no active_run (B3 regression)', async () => {
    const activeRuns = {
      'session-1': {
        run_id: 'run-stuck',
        sse_url: '/api/runs/run-stuck/events',
        status: 'running',
        events: [],
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

    // Initial mount attaches the SSE stream for the active run from history.
    await waitForCondition(() => Boolean(findCancelRunButton()), 100);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);

    // The server has lost the run (terminal event missed, bus buffer rolled,
    // or server restarted and the run is gone) — clear `activeRuns` so the
    // next `chat.history` response no longer carries an `active_run`.
    delete activeRuns['session-1'];

    // Trigger a second `loadHistoryForSession` via the sessions drawer.
    findButtonByText('Sessions')?.click();
    await waitForCondition(
      () => document.body.textContent.includes('session-1'),
      100,
    );
    findButtonByText('session-1')?.click();

    // Reconcile: the "Cancel run" button disappears and the run stream's
    // `closeSubscriptionFor` was called for this session key.
    await waitForCondition(() => findCancelRunButton() === undefined, 100);

    expect(findCancelRunButton()).toBeUndefined();
    expect(findButtonByText('New session')?.disabled).toBe(false);
    expect(closeSubscriptionForMock).toHaveBeenCalledWith('alpha::session-1');
    // No new SSE attach — the dead run is gone, not replaced.
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('keeps the session running when chat.history still reports the same active_run', async () => {
    const activeRuns = {
      'session-1': {
        run_id: 'run-stuck',
        sse_url: '/api/runs/run-stuck/events',
        status: 'running',
        events: [],
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

    await waitForCondition(() => Boolean(findCancelRunButton()), 100);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);

    // `chat.history` still reports the same active run — the active_run is
    // present on the second call, so no reconcile must fire.
    findButtonByText('Sessions')?.click();
    await waitForCondition(
      () => document.body.textContent.includes('session-1'),
      100,
    );
    findButtonByText('session-1')?.click();

    // `attachRunStream` runs again via `runStream.attachRunStream(...)` for
    // the second history load. The run is still the same id, so the
    // `alreadySubscribed` dedup inside `attachRunStream` prevents a
    // redundant SSE attach — `subscribeRunEvents` count stays at 1.
    await waitForCondition(
      () =>
        rpcMock.mock.calls.filter(
          ([method, params]) =>
            method === 'chat.history' && params?.session_id === 'session-1',
        ).length >= 2,
      100,
    );

    expect(findCancelRunButton()).toBeTruthy();
    expect(findButtonByText('New session')?.disabled).toBe(false);
    expect(closeSubscriptionForMock).not.toHaveBeenCalled();
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('does not reset the session when currentRun.runId changes during the chat.history await', async () => {
    // First history call: returns the running run so the session mounts in a
    // running state with `currentRun.runId = 'run-stuck'`.
    const initialActiveRuns = {
      'session-1': {
        run_id: 'run-stuck',
        sse_url: '/api/runs/run-stuck/events',
        status: 'running',
        events: [],
      },
    };

    // Second history call: returns no `active_run`. Held on a deferred so
    // the test can mutate `currentRun.runId` between the request and the
    // response — the exact race the `staleRunId` guard exists for.
    let resolveSecondHistory;
    const secondHistoryDeferred = new Promise((resolve) => {
      resolveSecondHistory = resolve;
    });
    let chatHistoryCallCount = 0;

    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'agent.list') {
        return { agents: [createAgent()] };
      }
      if (method === 'chat.history') {
        chatHistoryCallCount += 1;
        if (chatHistoryCallCount === 1) {
          return {
            session_id: params.session_id,
            messages: [
              {
                id: 'assistant-one',
                role: 'assistant',
                content: 'Hello',
              },
            ],
            has_more: false,
            active_run: initialActiveRuns[params.session_id],
          };
        }
        return secondHistoryDeferred;
      }
      if (method === 'chat.commands') {
        return { items: [] };
      }
      throw new Error(`Unexpected RPC method: ${method}`);
    });
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

    // Wait for the first `chat.history` response to land and the SSE stream
    // to be attached for the initial active run.
    await waitForCondition(() => Boolean(findCancelRunButton()), 100);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);

    // Trigger a second `loadHistoryForSession`. This call is held on the
    // deferred so we can race in a state mutation before it resolves.
    findButtonByText('Sessions')?.click();
    await waitForCondition(
      () => document.body.textContent.includes('session-1'),
      100,
    );
    findButtonByText('session-1')?.click();

    // Wait for the second `chat.history` call to be in flight.
    await waitForCondition(() => chatHistoryCallCount >= 2, 100);

    // Race: a *new* run legitimately starts before the deferred response
    // resolves. Simulate by mutating the live session state to a different
    // `runId` than the one the loader captured as `staleRunId`.
    const chatState = testChatStateRefs.at(-1);
    const sessionState = chatState.sessions['alpha::session-1'];
    expect(sessionState.currentRun?.runId).toBe('run-stuck');
    sessionState.currentRun = {
      runId: 'run-replacement',
      sseUrl: '/api/runs/run-replacement/events',
      status: 'running',
    };
    flushSync();

    // Resolve the deferred with no `active_run` — history is unaware of the
    // brand-new run (it started after the request was sent).
    resolveSecondHistory({
      session_id: 'session-1',
      messages: [
        {
          id: 'assistant-one',
          role: 'assistant',
          content: 'Hello',
        },
      ],
      has_more: false,
    });
    // The await resumes and runs the reconcile branch. `listQueue` is mocked
    // separately in `api.js` and does not hit `rpcMock`, so the total
    // `rpcMock` call count is `agent.list + chat.history × 2 + chat.commands`
    // = 4. Wait for the second history response to land by waiting for the
    // await chain inside `loadHistoryForSession` to reach the `await
    // syncSessionQueue(sessionState)` call.
    await waitForCondition(
      () => chatHistoryCallCount >= 2 && sessionState.currentRun !== null,
      100,
    );
    flushSync();

    // Guard fired: `staleRunId === 'run-stuck'` and the live
    // `currentRun.runId === 'run-replacement'`, so the reset branch did
    // not run. The session is still in the running state and the
    // `closeSubscriptionFor` reconcile hook was not called.
    expect(sessionState.status).toBe('running');
    expect(sessionState.currentRun?.runId).toBe('run-replacement');
    expect(closeSubscriptionForMock).not.toHaveBeenCalled();
    expect(findCancelRunButton()).toBeTruthy();
    expect(findButtonByText('New session')?.disabled).toBe(false);
  });

  // Helper: render a single running sub-agent tool row in the parent
  // timeline (mirrors the fast-subagent test setup). The caller is
  // responsible for installing an `rpcMock.mockImplementation` first; this
  // helper does NOT overwrite it (the verify tests pass a custom mock that
  // must keep responding after the mount completes).
  async function mountChatViewWithRunningSubAgent() {
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

    sendComposerMessage('Spawn background sub-agent');

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    const handlers = subscribeRunEventsMock.mock.calls[0][1];
    handlers.onEvent({
      data: {
        type: 'tool_call_started',
        run_id: 'run-verify-1',
        sequence: 1,
        payload: {
          tool_call: {
            id: 'call-verify-1',
            index: 0,
            name: 'subagent',
            arguments: {
              agent_id: 'alpha',
              background: true,
              content: 'Inspect the project',
            },
          },
        },
      },
    });
    handlers.onEvent({
      data: {
        type: 'subagent_session_started',
        run_id: 'run-verify-1',
        sequence: 2,
        payload: {
          tool_call: {
            id: 'call-verify-1',
            index: 0,
            name: 'subagent',
          },
          data: {
            agent_id: 'alpha',
            session_id: 'sub-session-1',
            run_id: 'verify-run',
            status: 'running',
          },
        },
      },
    });
    handlers.onEvent({
      data: {
        type: 'tool_call_result',
        run_id: 'run-verify-1',
        sequence: 3,
        payload: {
          tool_call: {
            id: 'call-verify-1',
            index: 0,
            name: 'subagent',
          },
          result: JSON.stringify({
            ok: true,
            data: {
              agent_id: 'alpha',
              session_id: 'sub-session-1',
              run_id: 'verify-run',
              status: 'running',
            },
          }),
        },
      },
    });
    flushSync();

    // The sub-agent row is in the parent timeline, dot still "running"
    // because the frozen persisted descriptor says so.
    const runningRow = document.querySelector('.subagent-tool-event');
    expect(runningRow).not.toBeNull();
    expect(runningRow?.querySelector('.te-dot.running')).not.toBeNull();
    return runningRow;
  }

  // Custom RPC mock factory for the sub-agent verification tests. The
  // default `createChatRpcMock` returns plain assistant messages for
  // `sub-session-1`; the verify path needs to see a `run_summary` (or an
  // `active_run`) in the response. The test passes the response override
  // for `sub-session-1`; everything else falls through to the default
  // behaviour.
  function createVerifyRpcMock({ subSessionHistory }) {
    const fallback = createChatRpcMock({
      streamResponse: {
        run_id: 'run-verify-1',
        sse_url: '/api/runs/run-verify-1/events',
        status: 'running',
        events: [],
      },
    });
    return async (method, params) => {
      if (method === 'chat.history' && params?.session_id === 'sub-session-1') {
        return subSessionHistory;
      }
      return fallback(method, params);
    };
  }

  it('verifySubAgentStatus: settles a stuck running sub-agent dot from a run_summary in chat.history (B5 regression)', async () => {
    rpcMock.mockImplementation(
      createVerifyRpcMock({
        subSessionHistory: {
          session_id: 'sub-session-1',
          messages: [
            {
              id: 'sub-assistant-original',
              role: 'assistant',
              content: 'Sub-agent response',
            },
            {
              id: 'sub-run-summary-1',
              role: 'run_summary',
              run_id: 'verify-run',
              status: 'completed',
              timing: { duration_ms: 4200 },
            },
          ],
          has_more: false,
        },
      }),
    );

    await mountChatViewWithRunningSubAgent();

    // The verification call hits the public exported method (same one
    // the future `onVerifySubAgentStatus` callback chain will invoke).
    await chatViewTest.mountedComponent.verifySubAgentStatus(
      'alpha',
      'sub-session-1',
      'verify-run',
    );
    flushSync();

    // Dot settled to "done" (status "completed" → dot "success") and the
    // child duration rendered in the time label.
    const settledRow = document.querySelector('.subagent-tool-event');
    expect(settledRow).not.toBeNull();
    expect(settledRow?.querySelector('.te-dot.running')).toBeNull();
    expect(settledRow?.querySelector('.te-dot.done')).not.toBeNull();
    expect(settledRow?.querySelector('.te-time')?.textContent?.trim()).toBe(
      '4.2s',
    );

    // The verify path targeted the right RPC (at least one verify
    // round-trip; the row's settled "success" dot also triggers the
    // existing `requestSubAgentResult` lookup, so more than one call is
    // expected and acceptable).
    const verifyHistoryCalls = rpcMock.mock.calls.filter(
      ([method, params]) =>
        method === 'chat.history' &&
        params?.session_id === 'sub-session-1' &&
        params?.limit === 20,
    );
    expect(verifyHistoryCalls.length).toBeGreaterThanOrEqual(1);
  });

  it('verifySubAgentStatus: keeps the dot running when chat.history reports an active_run, with a once-per-key guard', async () => {
    rpcMock.mockImplementation(
      createVerifyRpcMock({
        subSessionHistory: {
          session_id: 'sub-session-1',
          messages: [],
          has_more: false,
          active_run: {
            run_id: 'verify-run',
            sse_url: '/api/runs/verify-run/events',
            status: 'running',
            events: [],
          },
        },
      }),
    );

    await mountChatViewWithRunningSubAgent();

    // First call: chat.history returns active_run → dot stays "running".
    await chatViewTest.mountedComponent.verifySubAgentStatus(
      'alpha',
      'sub-session-1',
      'verify-run',
    );
    flushSync();

    const stillRunningRow = document.querySelector('.subagent-tool-event');
    expect(stillRunningRow).not.toBeNull();
    expect(stillRunningRow?.querySelector('.te-dot.running')).not.toBeNull();
    expect(stillRunningRow?.querySelector('.te-dot.done')).toBeNull();

    const historyCallCountAfterFirst = rpcMock.mock.calls.filter(
      ([method, params]) =>
        method === 'chat.history' &&
        params?.session_id === 'sub-session-1' &&
        params?.limit === 20,
    ).length;

    // Second call with the same key: the once-per-key guard must short-
    // circuit and not issue a second `chat.history` round-trip.
    await chatViewTest.mountedComponent.verifySubAgentStatus(
      'alpha',
      'sub-session-1',
      'verify-run',
    );
    flushSync();

    const historyCallCountAfterSecond = rpcMock.mock.calls.filter(
      ([method, params]) =>
        method === 'chat.history' &&
        params?.session_id === 'sub-session-1' &&
        params?.limit === 20,
    ).length;

    expect(historyCallCountAfterSecond).toBe(historyCallCountAfterFirst);

    // The dot is still running — the verify path did not flip it to
    // "done".
    const finalRow = document.querySelector('.subagent-tool-event');
    expect(finalRow?.querySelector('.te-dot.running')).not.toBeNull();
    expect(finalRow?.querySelector('.te-dot.done')).toBeNull();
  });

  // Helper: render a single QUEUED sub-agent tool row in the parent timeline
  // (the busy-child-session spawn path): the frozen descriptor carries only a
  // queue_item_id — never a run id. The caller installs the `rpcMock`
  // implementation and the `listQueueMock` behaviour first; mounting fires
  // the row's automatic status verification, which consults both.
  async function mountChatViewWithQueuedSubAgent() {
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

    sendComposerMessage('Spawn queued sub-agent');

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    const handlers = subscribeRunEventsMock.mock.calls[0][1];
    handlers.onEvent({
      data: {
        type: 'tool_call_started',
        run_id: 'run-verify-1',
        sequence: 1,
        payload: {
          tool_call: {
            id: 'call-queued-1',
            index: 0,
            name: 'subagent',
            arguments: {
              agent_id: 'alpha',
              background: true,
              content: 'Inspect the project',
            },
          },
        },
      },
    });
    handlers.onEvent({
      data: {
        type: 'subagent_session_started',
        run_id: 'run-verify-1',
        sequence: 2,
        payload: {
          tool_call: { id: 'call-queued-1', index: 0, name: 'subagent' },
          data: {
            agent_id: 'alpha',
            session_id: 'sub-session-1',
            queue_item_id: 'queue-item-1',
            status: 'queued',
          },
        },
      },
    });
    handlers.onEvent({
      data: {
        type: 'tool_call_result',
        run_id: 'run-verify-1',
        sequence: 3,
        payload: {
          tool_call: { id: 'call-queued-1', index: 0, name: 'subagent' },
          result: JSON.stringify({
            ok: true,
            data: {
              agent_id: 'alpha',
              session_id: 'sub-session-1',
              queue_item_id: 'queue-item-1',
              status: 'queued',
            },
          }),
        },
      },
    });
    flushSync();

    const row = document.querySelector('.subagent-tool-event');
    expect(row).not.toBeNull();
    return row;
  }

  // The tool row object the timeline's cancel button hands to
  // `onCancelSubAgent` for a queued spawn (frozen descriptor, no run id).
  function queuedSpawnToolFixture() {
    return {
      name: 'subagent',
      status: 'success',
      arguments: { agent_id: 'alpha', content: 'Inspect the project' },
      result: JSON.stringify({
        ok: true,
        error: null,
        data: {
          agent_id: 'alpha',
          session_id: 'sub-session-1',
          queue_item_id: 'queue-item-1',
          status: 'queued',
        },
        artifacts: [],
      }),
    };
  }

  it('cancelSubAgent: cancels a directly started child run through chat.cancel with reason user', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

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

    await chatViewTest.mountedComponent.cancelSubAgent({
      name: 'subagent',
      status: 'success',
      arguments: { agent_id: 'alpha', content: 'Inspect the project' },
      result: JSON.stringify({
        ok: true,
        error: null,
        data: {
          agent_id: 'alpha',
          session_id: 'sub-session-1',
          run_id: 'child-run-7',
          status: 'running',
        },
        artifacts: [],
      }),
    });

    expect(cancelRunMock).toHaveBeenCalledWith('child-run-7', {
      reason: 'user',
    });
    expect(removeFromQueueMock).not.toHaveBeenCalled();
  });

  it('cancelSubAgent: removes a still-queued child and settles the dot to cancelled', async () => {
    rpcMock.mockImplementation(
      createVerifyRpcMock({
        subSessionHistory: {
          session_id: 'sub-session-1',
          messages: [],
          has_more: false,
        },
      }),
    );
    // The queue item still waits, so the automatic verification keeps the
    // row running instead of settling it.
    listQueueMock.mockImplementation(async (agentId, sessionId) =>
      sessionId === 'sub-session-1'
        ? {
            items: [
              { id: 'queue-item-1', content: 'Inspect', internal: false },
            ],
          }
        : { items: [] },
    );

    await mountChatViewWithQueuedSubAgent();
    await waitForCondition(
      () =>
        document.querySelector('.subagent-tool-event .te-dot.running') !== null,
      100,
    );

    await chatViewTest.mountedComponent.cancelSubAgent(
      queuedSpawnToolFixture(),
    );
    flushSync();

    // `chat.queue_remove` keys on the BARE child agent id (trap 2).
    expect(removeFromQueueMock).toHaveBeenCalledWith(
      'alpha',
      'sub-session-1',
      'queue-item-1',
    );
    expect(cancelRunMock).not.toHaveBeenCalled();
    // Nothing else will ever report the never-started child, so the cancel
    // settles the row immediately.
    const row = document.querySelector('.subagent-tool-event');
    expect(row?.querySelector('.te-dot.cancelled')).not.toBeNull();
  });

  it('cancelSubAgent: falls back to the child session active run when the queue item is already consumed', async () => {
    rpcMock.mockImplementation(
      createVerifyRpcMock({
        subSessionHistory: {
          session_id: 'sub-session-1',
          messages: [],
          has_more: false,
          active_run: {
            run_id: 'child-active-run',
            sse_url: '/api/runs/child-active-run/events',
            status: 'running',
            events: [],
          },
        },
      }),
    );
    // The formerly queued child started server-side; its queue item is gone
    // and (post-reload) no queueRun mapping survived in this tab.
    removeFromQueueMock.mockRejectedValue(
      Object.assign(new Error('queued item not found: queue-item-1'), {
        code: 'queue_item_not_found',
      }),
    );

    await mountChatViewWithQueuedSubAgent();
    await waitForCondition(
      () =>
        document.querySelector('.subagent-tool-event .te-dot.running') !== null,
      100,
    );

    await chatViewTest.mountedComponent.cancelSubAgent(
      queuedSpawnToolFixture(),
    );
    flushSync();

    expect(cancelRunMock).toHaveBeenCalledWith('child-active-run', {
      reason: 'user',
    });
    const row = document.querySelector('.subagent-tool-event');
    expect(row?.querySelector('.te-dot.cancelled')).not.toBeNull();
  });

  it('verifySubAgentStatus: keeps a queued spawn running while its queue item is still pending', async () => {
    rpcMock.mockImplementation(
      createVerifyRpcMock({
        subSessionHistory: {
          session_id: 'sub-session-1',
          messages: [],
          has_more: false,
        },
      }),
    );
    listQueueMock.mockImplementation(async (agentId, sessionId) =>
      sessionId === 'sub-session-1'
        ? {
            items: [
              { id: 'queue-item-1', content: 'Inspect', internal: false },
            ],
          }
        : { items: [] },
    );

    await mountChatViewWithQueuedSubAgent();

    // The automatic run-id-less verification consulted the child queue…
    await waitForCondition(
      () =>
        listQueueMock.mock.calls.some(
          ([agentId, sessionId]) =>
            agentId === 'alpha' && sessionId === 'sub-session-1',
        ),
      100,
    );
    flushSync();

    // …and kept the dot running instead of settling "no trace" as success.
    const row = document.querySelector('.subagent-tool-event');
    expect(row?.querySelector('.te-dot.running')).not.toBeNull();
    expect(row?.querySelector('.te-dot.done')).toBeNull();
    expect(row?.querySelector('.te-dot.cancelled')).toBeNull();
  });

  it('verifySubAgentStatus: settles a never-started queued spawn to cancelled once its queue item is gone', async () => {
    rpcMock.mockImplementation(
      createVerifyRpcMock({
        subSessionHistory: {
          session_id: 'sub-session-1',
          messages: [],
          has_more: false,
        },
      }),
    );
    // Default `listQueueMock`: empty queue — the item is gone and no run ever
    // started, so the spawn was cancelled before start (not "completed").

    await mountChatViewWithQueuedSubAgent();

    await waitForCondition(
      () =>
        document.querySelector('.subagent-tool-event .te-dot.cancelled') !==
        null,
      100,
    );
    const row = document.querySelector('.subagent-tool-event');
    expect(row?.querySelector('.te-dot.done')).toBeNull();
  });

  // --- Two-bar project chat (Phase 2) -------------------------------------
});
