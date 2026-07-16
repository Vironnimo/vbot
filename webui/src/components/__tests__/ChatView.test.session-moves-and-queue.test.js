// @vitest-environment jsdom

import {
  describe,
  activeAgentTab,
  createAgent,
  createChatRpcMock,
  expect,
  findCancelRunButton,
  flushSync,
  it,
  listSessionsMock,
  rpcMock,
  sendComposerMessage,
  setupChatViewTestSuite,
  showProjectMock,
  subscribeRunEventsMock,
  waitForCondition,
} from './ChatView.support.js';

describe('ChatView', () => {
  const chatViewTest = setupChatViewTestSuite();

  it('switches to a different agent and its new session for a cross-agent /handoff command', async () => {
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
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
          'session-handoff-cross': [
            {
              id: 'handoff-cross-assistant-one',
              role: 'assistant',
              content: 'Handoff target reply (cross agent)',
            },
          ],
        },
        streamHandler: ({ content }) => {
          if (content === '/handoff beta') {
            return {
              command_handled: true,
              reply: 'Handoff sent to beta. Opening new session.',
              data: {
                command: 'handoff',
                session_id: 'session-handoff-cross',
                agent_id: 'beta',
              },
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    // Mirror App's behavior: `onAgentSelected` updates a reactive selected
    // id that flows back as `sharedSelectedAgentId`, so the agent-sync effect
    // observes the new selection and short-circuits.
    const parentHarness = createChatViewParentHarness();
    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: agents,
        get sharedSelectedAgentId() {
          return parentHarness.selectedAgentId;
        },
        onAgentSelected: (agentId) => {
          parentHarness.setSelectedAgentId(agentId);
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Parent main response'),
      100,
    );

    sendComposerMessage('/handoff beta');

    await waitForCondition(
      () =>
        document.body.textContent.includes(
          'Handoff target reply (cross agent)',
        ),
      100,
    );

    // Cross-agent `/handoff` is an action command: no toast or transient card.
    expect(document.body.querySelector('.chat-view__command-toast')).toBeNull();
    expect(document.body.querySelector('.transient-card')).toBeNull();
    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'parent-session',
      content: '/handoff beta',
    });
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'beta',
      session_id: 'session-handoff-cross',
      limit: 100,
    });
    expect(subscribeRunEventsMock).not.toHaveBeenCalled();
    expect(activeAgentTab()?.textContent).toContain('Beta');
  });

  // --- /agent move-action routing: all four directions --------------------
  //
  // `/agent <addr>` MOVES the current session (same session id) to another
  // agent. The target's address decides the world: a bare id is identity, an
  // `agent@projekt` address is a project team agent. These four tests cross
  // every direction and assert the SAME session id is opened under the target.

  it('moves the current session to another identity agent (identity → identity)', async () => {
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const agents = [
      createAgent({
        id: 'alpha',
        name: 'Alpha',
        current_session_id: 'shared-session',
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
          'shared-session': [
            {
              id: 'shared-assistant-one',
              role: 'assistant',
              content: 'Shared session reply',
            },
          ],
        },
        streamHandler: ({ content }) => {
          if (content === '/agent beta') {
            return {
              command_handled: true,
              reply: 'Moved to beta.',
              output: 'action',
              data: {
                command: 'agent',
                session_id: 'shared-session',
                agent_id: 'beta',
              },
            };
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
      () => document.body.textContent.includes('Shared session reply'),
      100,
    );

    sendComposerMessage('/agent beta');

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' &&
            params?.agent_id === 'beta' &&
            params?.session_id === 'shared-session',
        ),
      100,
    );

    // The move is an action: no toast, no transient card.
    expect(document.body.querySelector('.chat-view__command-toast')).toBeNull();
    expect(document.body.querySelector('.transient-card')).toBeNull();
    // The SAME session id is opened under the target identity agent.
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'beta',
      session_id: 'shared-session',
      limit: 100,
    });
    expect(activeAgentTab()?.textContent).toContain('Beta');
  });

  it('moves the current session to a project team agent (identity → project)', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [{ agent_id: 'builder', display_name: 'Builder', model: 'm' }],
        report: { clean: true, findings: [] },
      },
    });
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents: [createAgent({ current_session_id: 'shared-session' })],
        sessionMessages: {
          // The moved session keeps its id (`shared-session`); history is keyed
          // by session id, so the same content is served under the target.
          'shared-session': [
            {
              id: 'shared-assistant-one',
              role: 'assistant',
              content: 'Identity session reply',
            },
          ],
        },
        streamHandler: (params) => {
          streamCalls.push(params);
          if (params.content === '/agent builder@vbot') {
            return {
              command_handled: true,
              reply: 'Moved to builder@vbot.',
              output: 'action',
              data: {
                command: 'agent',
                session_id: 'shared-session',
                agent_id: 'builder@vbot',
              },
            };
          }
          throw new Error(`Unexpected stream content: ${params.content}`);
        },
      }),
    );

    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const parentHarness = createChatViewParentHarness();
    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent({ current_session_id: 'shared-session' })],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        get selectedProjectId() {
          return parentHarness.selectedProjectId;
        },
        onProjectSelected: (id) => parentHarness.setSelectedProjectId(id),
        get sharedSelectedProjectAgentId() {
          return parentHarness.selectedProjectAgentId;
        },
        onProjectAgentSelected: (id) =>
          parentHarness.setSelectedProjectAgentId(id),
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Identity session reply'),
      100,
    );

    sendComposerMessage('/agent builder@vbot');

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' &&
            params?.agent_id === 'builder@vbot' &&
            params?.session_id === 'shared-session',
        ),
      100,
    );

    // The move was issued from the identity bar, so the source send used the
    // bare identity address (no `@projekt`).
    expect(streamCalls[0]).toEqual({
      agent_id: 'alpha',
      session_id: 'shared-session',
      content: '/agent builder@vbot',
    });
    // The history loaded under the FULL project address with the SAME session.
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'builder@vbot',
      session_id: 'shared-session',
      limit: 100,
    });
    // The session was pre-seeded for the target, so the accessor never had to
    // list or create a session (the move keeps the existing one).
    expect(listSessionsMock).not.toHaveBeenCalledWith('builder@vbot');
    expect(rpcMock).not.toHaveBeenCalledWith(
      'session.create',
      expect.objectContaining({ agent_id: 'builder@vbot' }),
    );
    // The project bar now shows Builder as the active team agent, and App was
    // told to persist the project + agent.
    expect(
      document
        .querySelector('.chat-view__project-team .agent-tab.active')
        ?.textContent?.includes('Builder'),
    ).toBe(true);
    expect(parentHarness.selectedProjectId).toBe('vbot');
    expect(parentHarness.selectedProjectAgentId).toBe('builder');
  });

  it('moves a project-agent session back to an identity agent (project → identity)', async () => {
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
              id: 'builder-assistant-one',
              role: 'assistant',
              content: 'Builder project reply',
            },
          ],
        },
        streamHandler: ({ content }) => {
          if (content === '/agent alpha') {
            return {
              command_handled: true,
              reply: 'Moved to alpha.',
              output: 'action',
              data: {
                command: 'agent',
                session_id: 'builder-session',
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
        sharedAgents: [createAgent({ current_session_id: 'alpha-current' })],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
      },
    });
    flushSync();

    // Wait for the project agent (Builder) to become the active chat target.
    await waitForCondition(
      () => document.body.textContent.includes('Builder project reply'),
      100,
    );

    sendComposerMessage('/agent alpha');

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' &&
            params?.agent_id === 'alpha' &&
            params?.session_id === 'builder-session',
        ),
      100,
    );

    // The move was issued from the project bar; the stream send used the FULL
    // source address (trap 2).
    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'builder@vbot',
      session_id: 'builder-session',
      content: '/agent alpha',
    });
    // The SAME session id is now opened under the identity agent (bare id).
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'builder-session',
      limit: 100,
    });
    // The identity bar is active again.
    expect(
      document
        .querySelector('.chat-header .agent-tab.active')
        ?.textContent?.includes('Alpha'),
    ).toBe(true);
  });

  it('moves a project-agent session to another project agent (project → project)', async () => {
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
              id: 'builder-assistant-one',
              role: 'assistant',
              content: 'Builder project reply',
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

    sendComposerMessage('/agent reviewer@vbot');

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' &&
            params?.agent_id === 'reviewer@vbot' &&
            params?.session_id === 'builder-session',
        ),
      100,
    );

    // The SAME session id is opened under the new project agent's full address.
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'reviewer@vbot',
      session_id: 'builder-session',
      limit: 100,
    });
    // Reviewer is now the active project team agent.
    expect(
      document
        .querySelector('.chat-view__project-team .agent-tab.active')
        ?.textContent?.includes('Reviewer'),
    ).toBe(true);
  });

  it('subscribes to the run returned by a /continue command', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamHandler: ({ content }) => {
          if (content === '/continue') {
            return {
              run_id: 'run-continue-1',
              sse_url: '/api/runs/run-continue-1/events',
              status: 'running',
              events: [],
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

    sendComposerMessage('/continue');

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'session-1',
      content: '/continue',
    });
    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/run-continue-1/events',
      expect.any(Object),
      { afterSequence: 0 },
    );
  });

  it('keeps slash skill triggers queued while allowing built-in /stop to bypass during an active run', async () => {
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamHandler: ({ content }) => {
          streamCalls.push(content);
          if (content === 'Start a long run') {
            return {
              run_id: 'run-1',
              sse_url: '/api/runs/run-1/events',
              status: 'running',
              events: [],
            };
          }
          if (content === '/debugging investigate this run') {
            return {
              queued: true,
              item: {
                id: 'queued-skill-1',
                content: '/debugging investigate this run',
                created_at: '2026-05-22T10:00:00+00:00',
              },
            };
          }
          if (content === '/stop') {
            return {
              command_handled: true,
              reply: 'Run cancelled.',
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

    sendComposerMessage('Start a long run');

    await waitForCondition(() => Boolean(findCancelRunButton()), 100);

    sendComposerMessage('/debugging investigate this run');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.queued-messages__content')
          ?.textContent?.includes('/debugging investigate this run'),
      100,
    );

    sendComposerMessage('/stop');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.chat-view__command-toast')
          ?.textContent?.trim() === 'Run cancelled.',
      100,
    );

    expect(streamCalls).toEqual([
      'Start a long run',
      '/debugging investigate this run',
      '/stop',
    ]);
    expect(
      document.body
        .querySelector('.queued-messages__content')
        ?.textContent?.includes('/debugging investigate this run'),
    ).toBe(true);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('uses local /stop fallback when command metadata cannot be loaded', async () => {
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        commandsError: true,
        streamHandler: ({ content }) => {
          streamCalls.push(content);
          if (content === 'Start a long run') {
            return {
              run_id: 'run-fallback-stop-1',
              sse_url: '/api/runs/run-fallback-stop-1/events',
              status: 'running',
              events: [],
            };
          }
          if (content === '/debugging investigate this run') {
            return {
              queued: true,
              item: {
                id: 'queued-skill-fallback-1',
                content: '/debugging investigate this run',
                created_at: '2026-05-22T10:01:00+00:00',
              },
            };
          }
          if (content === '/stop') {
            return {
              command_handled: true,
              reply: 'Run cancelled.',
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

    sendComposerMessage('Start a long run');

    await waitForCondition(() => Boolean(findCancelRunButton()), 100);

    sendComposerMessage('/debugging investigate this run');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.queued-messages__content')
          ?.textContent?.includes('/debugging investigate this run'),
      100,
    );

    sendComposerMessage('/stop');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.chat-view__command-toast')
          ?.textContent?.trim() === 'Run cancelled.',
      100,
    );

    expect(streamCalls).toEqual([
      'Start a long run',
      '/debugging investigate this run',
      '/stop',
    ]);
    expect(
      document.body
        .querySelector('.queued-messages__content')
        ?.textContent?.includes('/debugging investigate this run'),
    ).toBe(true);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('recognizes /compact when command metadata includes a leading slash', async () => {
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        commandItems: [
          {
            name: '/compact',
            description: 'Compact the current session context.',
            type: 'command',
          },
          {
            name: 'debugging',
            description: 'Investigate unclear bugs.',
            type: 'skill',
          },
        ],
        streamHandler: ({ content }) => {
          streamCalls.push(content);
          if (content === 'Start a long run') {
            return {
              run_id: 'run-compact-1',
              sse_url: '/api/runs/run-compact-1/events',
              status: 'running',
              events: [],
            };
          }
          if (content === '/compact') {
            return {
              command_handled: true,
              reply: 'Context compacted.',
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

    sendComposerMessage('Start a long run');

    await waitForCondition(() => Boolean(findCancelRunButton()), 100);

    sendComposerMessage('/compact');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.chat-view__command-toast')
          ?.textContent?.trim() === 'Context compacted.',
      100,
    );

    expect(streamCalls).toEqual(['Start a long run', '/compact']);
    const queuedContent =
      document.body.querySelector('.queued-messages__content')?.textContent ??
      '';
    expect(queuedContent.includes('/compact')).toBe(false);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('reloads history after a /compact that carries an instruction', async () => {
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        commandItems: [
          {
            name: '/compact',
            description: 'Compact the current session context.',
            type: 'command',
            argument: 'optional',
            output: 'toast',
          },
        ],
        streamHandler: ({ content }) => {
          streamCalls.push(content);
          if (content === '/compact focus on the auth work') {
            return {
              command_handled: true,
              reply: 'Context compacted.',
              output: 'toast',
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

    // The reload is what surfaces the new compaction separator. The argument
    // form must trigger it just like the bare `/compact` does — regression
    // guard for `isCompactCommand` matching only the leading token.
    const historyReloadCount = () =>
      rpcMock.mock.calls.filter(
        ([method, params]) =>
          method === 'chat.history' && params?.session_id === 'session-1',
      ).length;
    const reloadsBefore = historyReloadCount();

    sendComposerMessage('/compact focus on the auth work');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.chat-view__command-toast')
          ?.textContent?.trim() === 'Context compacted.',
      100,
    );
    await waitForCondition(() => historyReloadCount() > reloadsBefore, 100);

    expect(streamCalls).toEqual(['/compact focus on the auth work']);
    expect(historyReloadCount()).toBeGreaterThan(reloadsBefore);
  });

  it('queues non-command messages while a run is active', async () => {
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamHandler: ({ content }) => {
          streamCalls.push(content);
          if (content === 'Start a long run') {
            return {
              run_id: 'run-2',
              sse_url: '/api/runs/run-2/events',
              status: 'running',
              events: [],
            };
          }
          if (content === 'Queue this while running') {
            return {
              queued: true,
              item: {
                id: 'queued-message-1',
                content: 'Queue this while running',
                created_at: '2026-05-22T10:02:00+00:00',
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

    sendComposerMessage('Start a long run');

    await waitForCondition(() => Boolean(findCancelRunButton()), 100);

    sendComposerMessage('Queue this while running');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.queued-messages__content')
          ?.textContent?.trim() === 'Queue this while running',
      100,
    );

    expect(streamCalls).toEqual([
      'Start a long run',
      'Queue this while running',
    ]);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('explains when a server restart discards a locally shown queued message', async () => {
    const { createChatViewConnectionSnapshotHarness } =
      await import('./chatViewConnectionSnapshotHarness.svelte.js');
    const harness = createChatViewConnectionSnapshotHarness();
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamHandler: ({ content }) => {
          if (content === 'Start before restart') {
            return {
              run_id: 'run-before-restart',
              sse_url: '/api/runs/run-before-restart/events',
              status: 'running',
              events: [],
            };
          }
          if (content === 'Queue before restart') {
            return {
              queued: true,
              item: {
                id: 'queued-before-restart',
                content: 'Queue before restart',
                created_at: '2026-07-16T12:00:00+00:00',
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
        get connectionSnapshot() {
          return harness.connectionSnapshot;
        },
      },
    });
    flushSync();
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start before restart');
    await waitForCondition(() => Boolean(findCancelRunButton()), 100);
    sendComposerMessage('Queue before restart');
    await waitForCondition(
      () =>
        document.body
          .querySelector('.queued-messages__content')
          ?.textContent?.includes('Queue before restart'),
      100,
    );

    harness.setConnectionSnapshot({
      type: 'connection_ready',
      epoch: 'epoch-after-restart',
      replay_status: 'epoch_changed',
      active_runs: [],
      queues: [],
    });
    flushSync();

    await waitForCondition(
      () =>
        document.body
          .querySelector('.chat-view__command-toast')
          ?.textContent?.trim() ===
        '1 queued message was discarded because the server restarted.',
      100,
    );
    expect(document.body.querySelector('.queued-messages__content')).toBeNull();
  });
});
