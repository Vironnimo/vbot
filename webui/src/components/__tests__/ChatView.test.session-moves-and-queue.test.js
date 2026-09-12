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

  it('does not apply stale command navigation after the user selects another Agent', async () => {
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    let resolveMove;
    const moveResponse = new Promise((resolve) => {
      resolveMove = resolve;
    });
    const agents = [
      createAgent({ current_session_id: 'shared-session' }),
      createAgent({
        id: 'beta',
        name: 'Beta',
        current_session_id: 'beta-session',
      }),
      createAgent({
        id: 'gamma',
        name: 'Gamma',
        current_session_id: 'gamma-session',
      }),
    ];
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents,
        sessionMessages: {
          'shared-session': [
            { id: 'alpha-reply', role: 'assistant', content: 'Alpha reply' },
          ],
          'gamma-session': [
            { id: 'gamma-reply', role: 'assistant', content: 'Gamma reply' },
          ],
        },
        streamHandler: ({ content }) => {
          if (content === '/agent beta') {
            return moveResponse;
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
      () => document.body.textContent.includes('Alpha reply'),
      100,
    );

    sendComposerMessage('/agent beta');
    findButtonByText('Gamma').click();
    await waitForCondition(
      () => document.body.textContent.includes('Gamma reply'),
      100,
    );

    resolveMove({
      command_handled: true,
      reply: 'Moved to beta.',
      output: 'action',
      data: {
        command: 'agent',
        session_id: 'shared-session',
        agent_id: 'beta',
      },
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();

    expect(activeAgentTab()?.textContent).toContain('Gamma');
    expect(rpcMock).not.toHaveBeenCalledWith('chat.history', {
      agent_id: 'beta',
      session_id: 'shared-session',
      limit: 100,
    });
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
    // create a session (the background activity refresh may still list it).
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
});
