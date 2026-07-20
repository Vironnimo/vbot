// @vitest-environment jsdom

import {
  describe,
  activeAgentTab,
  createAgent,
  createChatRpcMock,
  expect,
  flushSync,
  it,
  listQueueMock,
  listSessionsMock,
  rpcMock,
  sendComposerMessage,
  setupChatViewTestSuite,
  showProjectMock,
  vi,
  waitForCondition,
} from './ChatView.support.js';

describe('ChatView', () => {
  const chatViewTest = setupChatViewTestSuite();

  it('renders the project dropdown with No project default and identity chat unchanged', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: '',
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const dropdown = document.querySelector('.chat-header__project-dropdown');
    expect(dropdown).toBeTruthy();
    // The shared Dropdown trigger reflects the current selection's label.
    const dropdownLabel = dropdown.querySelector(
      '.dropdown-primitive__trigger-label',
    );
    expect(dropdownLabel?.textContent?.trim()).toBe('Personal — no project');
    // No project chosen → no second bar, no project.show call.
    expect(document.querySelector('.chat-view__project-team')).toBeNull();
    expect(showProjectMock).not.toHaveBeenCalled();
    // Identity history call is byte-identical to today (bare agent id).
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'session-1',
      limit: 100,
    });
  });

  it('REGRESSION: a Personal identity send is byte-identical to today (bare agent id, no @projekt)', async () => {
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamHandler: (params) => {
          streamCalls.push(params);
          return {
            run_id: 'run-personal',
            sse_url: '/api/runs/run-personal/events',
            status: 'running',
            events: [],
          };
        },
      }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        // A project exists in the dropdown but is NOT selected.
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: '',
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Personal hello');

    await waitForCondition(() => streamCalls.length === 1, 100);

    // The chat.stream payload carries the bare id — no `@projekt`, exactly
    // today's identity behavior.
    expect(streamCalls[0]).toEqual({
      agent_id: 'alpha',
      session_id: 'session-1',
      content: 'Personal hello',
    });
    expect(streamCalls[0].agent_id).not.toContain('@');
  });

  it('choosing a project loads its team and jumps to the default agent', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [
          { agent_id: 'reviewer', display_name: 'Reviewer', model: 'm' },
          { agent_id: 'builder', display_name: 'Builder', model: 'm' },
        ],
        report: { clean: true, findings: [] },
      },
    });
    // Project agent has an existing session listed (trap 1: newest wins).
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

    // Second bar shows the scanned team.
    const teamBar = document.querySelector('.chat-view__project-team');
    expect(teamBar).toBeTruthy();
    expect(teamBar.textContent).toContain('Builder');
    expect(teamBar.textContent).toContain('Reviewer');

    // Jumped to the default agent (builder), not the first team member.
    const activeProjectTab = teamBar.querySelector('.agent-tab.active');
    expect(activeProjectTab?.textContent).toContain('Builder');

    // session.list and chat.history for the project agent use the FULL
    // address (trap 2).
    expect(listSessionsMock).toHaveBeenCalledWith('builder@vbot');
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'builder@vbot',
      session_id: 'builder-session',
      limit: 100,
    });
  });

  it('keeps only one agent selected across both bars when switching between identity and project agents', async () => {
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
      () =>
        document
          .querySelector('.chat-view__project-team .agent-tab.active')
          ?.textContent?.includes('Builder'),
      100,
    );

    // Selecting the project agent must deselect the identity tab: exactly one
    // active tab across both bars (regression — the identity tab used to stay
    // highlighted because chatState.selectedAgentId is untouched).
    let activeTabs = document.querySelectorAll('.agent-tab.active');
    expect(activeTabs).toHaveLength(1);
    expect(activeTabs[0].textContent).toContain('Builder');
    expect(document.querySelector('.chat-header .agent-tab.active')).toBeNull();

    // Switching back to the identity agent moves the single selection up to the
    // header bar (the project team bar stays rendered but with no active tab).
    document.querySelector('.chat-header .agent-tab').click();
    await waitForCondition(
      () =>
        document
          .querySelector('.chat-header .agent-tab.active')
          ?.textContent?.includes('Alpha'),
      100,
    );
    activeTabs = document.querySelectorAll('.agent-tab.active');
    expect(activeTabs).toHaveLength(1);
    expect(activeTabs[0].textContent).toContain('Alpha');
    expect(
      document.querySelector('.chat-view__project-team .agent-tab.active'),
    ).toBeNull();
  });

  it('jumps to the first team member when the project has no default agent', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: '' },
      scan: {
        team: [
          { agent_id: 'first', display_name: 'First', model: 'm' },
          { agent_id: 'second', display_name: 'Second', model: 'm' },
        ],
        report: { clean: true, findings: [] },
      },
    });
    listSessionsMock.mockResolvedValue({ sessions: [] });
    // No sessions → session.create (default mock) returns
    // `created-first@vbot`, whose history is the new empty session.
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'created-first@vbot': [],
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
      () =>
        document
          .querySelector('.chat-view__project-team .agent-tab.active')
          ?.textContent?.includes('First'),
      100,
    );

    // Trap 1: no current_session_id, no listed session → session.create with
    // the full address and NO make_current.
    expect(rpcMock).toHaveBeenCalledWith('session.create', {
      agent_id: 'first@vbot',
    });
    const createCall = rpcMock.mock.calls.find(
      ([method]) => method === 'session.create',
    );
    expect(createCall[1]).not.toHaveProperty('make_current');
  });

  it('restores the remembered project agent on reload instead of the default', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [
          { agent_id: 'reviewer', display_name: 'Reviewer', model: 'm' },
          { agent_id: 'builder', display_name: 'Builder', model: 'm' },
        ],
        report: { clean: true, findings: [] },
      },
    });
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'reviewer-session',
          created_at: '2026-06-01T00:00:00+00:00',
          last_active_at: '2026-06-10T00:00:00+00:00',
        },
      ],
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'reviewer-session': [
            {
              id: 'reviewer-assistant-one',
              role: 'assistant',
              content: 'Reviewer project reply',
            },
          ],
        },
      }),
    );

    // A reload where the Reviewer team member was active: App restores the
    // project AND the remembered project agent.
    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
        sharedSelectedProjectAgentId: 'reviewer',
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Reviewer project reply'),
      100,
    );

    // Restored the remembered agent (Reviewer), NOT the project default
    // (Builder) — exactly one active tab, in the project bar.
    const activeTabs = document.querySelectorAll('.agent-tab.active');
    expect(activeTabs).toHaveLength(1);
    expect(activeTabs[0].textContent).toContain('Reviewer');
    expect(
      document.querySelector('.chat-view__project-team .agent-tab.active')
        ?.textContent,
    ).toContain('Reviewer');
    // Session/history resolution used the restored agent's full address. The
    // background activity refresh may list every team member, but it must not
    // navigate to Builder's history.
    expect(listSessionsMock).toHaveBeenCalledWith('reviewer@vbot');
    expect(rpcMock).not.toHaveBeenCalledWith(
      'chat.history',
      expect.objectContaining({ agent_id: 'builder@vbot' }),
    );
  });

  it('restores the identity agent on reload when it was active alongside the project', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [{ agent_id: 'builder', display_name: 'Builder', model: 'm' }],
        report: { clean: true, findings: [] },
      },
    });
    rpcMock.mockImplementation(createChatRpcMock());

    // A reload where an identity agent was active despite the open project:
    // App restores the project but the remembered project agent is '' (none).
    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
        sharedSelectedProjectAgentId: '',
      },
    });
    flushSync();

    // The identity agent's history loads and stays active.
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    // The project team bar renders, but no project agent is opened — the
    // single active tab is the identity agent in the header bar.
    expect(document.querySelector('.chat-view__project-team')).toBeTruthy();
    const activeTabs = document.querySelectorAll('.agent-tab.active');
    expect(activeTabs).toHaveLength(1);
    expect(
      document.querySelector('.chat-header .agent-tab.active')?.textContent,
    ).toContain('Alpha');
    expect(
      document.querySelector('.chat-view__project-team .agent-tab.active'),
    ).toBeNull();
    // No project agent was opened. Its Sessions may be listed for activity,
    // but project-agent history is not loaded.
    expect(rpcMock).not.toHaveBeenCalledWith(
      'chat.history',
      expect.objectContaining({ agent_id: 'builder@vbot' }),
    );
  });

  it('falls back to the project default when the remembered agent left the team', async () => {
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
      }),
    );

    // The remembered agent ('ghost') is no longer scanned — restore must heal
    // to the project default rather than leave the chat agent-less.
    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
        sharedSelectedProjectAgentId: 'ghost',
      },
    });
    flushSync();

    await waitForCondition(
      () =>
        document
          .querySelector('.chat-view__project-team .agent-tab.active')
          ?.textContent?.includes('Builder'),
      100,
    );
    expect(listSessionsMock).toHaveBeenCalledWith('builder@vbot');
  });

  it('jumps to the default on a genuine project switch and reports it up for persistence', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [
          { agent_id: 'reviewer', display_name: 'Reviewer', model: 'm' },
          { agent_id: 'builder', display_name: 'Builder', model: 'm' },
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
      }),
    );

    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const parentHarness = createChatViewParentHarness();

    // Start with no project (Personal), mirroring App's persisted state.
    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
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

    // The user picks a project from the dropdown (a genuine switch, not a
    // reload): it must jump to the default agent.
    parentHarness.setSelectedProjectId('vbot');
    flushSync();

    await waitForCondition(
      () =>
        document
          .querySelector('.chat-view__project-team .agent-tab.active')
          ?.textContent?.includes('Builder'),
      100,
    );

    // The chosen agent is reported up so App can persist it for the next reload.
    expect(parentHarness.selectedProjectAgentId).toBe('builder');
  });

  it('reports the identity agent up as the active project agent when switching back to it', async () => {
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
        sessionMessages: { 'builder-session': [] },
      }),
    );

    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const parentHarness = createChatViewParentHarness();
    // A reload restoring the Builder project agent.
    parentHarness.setSelectedProjectId('vbot');
    parentHarness.setSelectedProjectAgentId('builder');

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
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
      () =>
        document
          .querySelector('.chat-view__project-team .agent-tab.active')
          ?.textContent?.includes('Builder'),
      100,
    );

    // Stepping back up to the identity agent persists '' (identity active),
    // distinct from null/"nothing remembered" — this is what a reload restores
    // to keep the chat on the identity agent.
    document.querySelector('.chat-header .agent-tab').click();
    await waitForCondition(
      () =>
        document
          .querySelector('.chat-header .agent-tab.active')
          ?.textContent?.includes('Alpha'),
      100,
    );
    expect(parentHarness.selectedProjectAgentId).toBe('');
  });

  it('renders an empty second bar without error for an empty project team', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'empty', default_agent: '' },
      scan: { team: [], report: { clean: true, findings: [] } },
    });
    rpcMock.mockImplementation(createChatRpcMock());

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'empty', display_name: 'Empty' }],
        selectedProjectId: 'empty',
      },
    });
    flushSync();

    await waitForCondition(
      () => Boolean(document.querySelector('.chat-view__project-team')),
      100,
    );

    const teamBar = document.querySelector('.chat-view__project-team');
    expect(teamBar.querySelector('.agent-tab')).toBeNull();
    expect(teamBar.textContent).toContain('no agents');
    // No project agent selected, no error notice.
    expect(document.querySelector('.chat-view__error')).toBeNull();
    // The identity agent above stays active and chattable.
    expect(activeAgentTab()?.textContent).toContain('Alpha');
  });

  it('sends a project-agent message and syncs the queue with the full address (trap 2)', async () => {
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
    const streamCalls = [];
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
        streamHandler: (params) => {
          streamCalls.push(params);
          return {
            run_id: 'run-proj',
            sse_url: '/api/runs/run-proj/events',
            status: 'running',
            events: [],
          };
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

    // The queue key carries the project anchor, so the queue sync that ran
    // during history load used the FULL address (trap 2).
    expect(listQueueMock).toHaveBeenCalledWith(
      'builder@vbot',
      'builder-session',
    );
    // It must NEVER strip the address down to the bare id.
    expect(listQueueMock).not.toHaveBeenCalledWith(
      'builder',
      'builder-session',
    );

    sendComposerMessage('Hello project agent');

    await waitForCondition(() => streamCalls.length === 1, 100);

    // chat.stream parses an address → FULL address (trap 2).
    expect(streamCalls[0]).toEqual({
      agent_id: 'builder@vbot',
      session_id: 'builder-session',
      content: 'Hello project agent',
    });
  });

  it('shows the scan banner for an unclean project and links into the Projects tab', async () => {
    const onNavigateToProjects = vi.fn();
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [{ agent_id: 'builder', display_name: 'Builder', model: 'm' }],
        report: {
          clean: false,
          findings: [
            {
              type: 'bad_model',
              detail: 'unknown model',
              agent_id: 'builder',
            },
          ],
        },
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
        sessionMessages: { 'builder-session': [] },
      }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
        onNavigateToProjects,
      },
    });
    flushSync();

    await waitForCondition(
      () => Boolean(document.querySelector('.project-scan-banner')),
      100,
    );

    const link = document.querySelector('.project-scan-banner__link');
    expect(link).toBeTruthy();
    link.click();
    flushSync();
    expect(onNavigateToProjects).toHaveBeenCalledTimes(1);
  });

  it('does not show the scan banner for a clean project', async () => {
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
      createChatRpcMock({ sessionMessages: { 'builder-session': [] } }),
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
      () => Boolean(document.querySelector('.chat-view__project-team')),
      100,
    );

    expect(document.querySelector('.project-scan-banner')).toBeNull();
  });
});
