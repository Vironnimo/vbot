// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import { t } from '../../lib/i18n.js';
import {
  activeTeamTabName,
  agentPickerTrigger,
  createAgent,
  createChatRpcMock,
  findButtonByText,
  flushSync,
  handledCommand,
  historyReads,
  hoveredTooltipText,
  listQueueMock,
  listSessionsMock,
  listedSessions,
  message,
  projectChatProps,
  rpcCalls,
  rpcMock,
  runningRun,
  selectAgentFromPicker,
  selectedPersonalAgentName,
  sendComposerMessage,
  serveProject,
  settle,
  setupChatViewTestSuite,
  showProjectMock,
  streamResponses,
  teamTab,
  waitForCondition,
  waitForText,
} from './ChatView.support.js';
import { createChatViewParentHarness } from './ChatView.parent.support.svelte.js';

const projects = [{ project_id: 'vbot', display_name: 'vBot' }];
const identityProps = () => ({
  sharedAgents: [createAgent()],
  sharedSelectedAgentId: 'alpha',
  projects,
});
const builderSession = {
  id: 'builder-session',
  created_at: '2026-06-01T00:00:00+00:00',
  last_active_at: '2026-06-10T00:00:00+00:00',
};
const builderReply = {
  'builder-session': [message('builder-reply', 'Builder project reply')],
};

// Project History Agent addresses (`agent@project`) Chat has read.
const projectHistoryAgents = () => [
  ...new Set(
    rpcCalls('chat.history')
      .map((params) => params.agent_id)
      .filter((agentId) => agentId.includes('@')),
  ),
];
const activeTabCount = () =>
  document.querySelectorAll('.agent-tab.active').length;

describe('ChatView Projects', () => {
  const chat = setupChatViewTestSuite();

  describe('Project selection', () => {
    it('keeps the Identity chat and its bare addresses while no Project is selected', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({ streamResponse: runningRun('run-personal') }),
      );
      await chat.mountChat({ ...identityProps(), selectedProjectId: '' });

      // The shared Dropdown trigger reflects the current selection's label.
      expect(
        document
          .querySelector(
            '.chat-header__project-dropdown .dropdown-primitive__trigger-label',
          )
          .textContent.trim(),
      ).toBe(t('chat.project.none'));
      expect(document.querySelector('.chat-view__project-team')).toBeNull();
      expect(showProjectMock).not.toHaveBeenCalled();
      expect(rpcMock).toHaveBeenCalledWith('chat.history', {
        agent_id: 'alpha',
        session_id: 'session-1',
        limit: 100,
      });

      sendComposerMessage('Personal hello');
      await waitForCondition(() => rpcCalls('chat.stream').length === 1);
      expect(rpcCalls('chat.stream')).toEqual([
        {
          agent_id: 'alpha',
          session_id: 'session-1',
          content: 'Personal hello',
        },
      ]);
    });

    it('loads the Team and opens the Project default Agent by its full address', async () => {
      serveProject({
        team: [
          ['reviewer', 'Reviewer'],
          ['builder', 'Builder'],
        ],
      });
      // The newest listed Session of the Project Agent is its landing.
      listedSessions(builderSession);
      rpcMock.mockImplementation(
        createChatRpcMock({
          sessionMessages: builderReply,
          streamResponse: runningRun('run-proj'),
        }),
      );
      await chat.mountChat(projectChatProps(), {
        ready: 'Builder project reply',
      });

      // The second bar shows the scanned Team; the default Agent is active,
      // not the first member.
      const teamBar = document.querySelector('.chat-view__project-team');
      expect(teamBar.textContent).toContain('Reviewer');
      expect(activeTeamTabName()).toContain('Builder');
      // Session list, History, Queue and sends use the full address.
      expect(listSessionsMock).toHaveBeenCalledWith(
        'builder@vbot',
        expect.objectContaining({ limit: 1, includeSubagents: false }),
      );
      expect(rpcMock).toHaveBeenCalledWith('chat.history', {
        agent_id: 'builder@vbot',
        session_id: 'builder-session',
        limit: 100,
      });
      expect(listQueueMock).toHaveBeenCalledWith(
        'builder@vbot',
        'builder-session',
      );
      expect(listQueueMock).not.toHaveBeenCalledWith(
        'builder',
        'builder-session',
      );

      sendComposerMessage('Hello project agent');
      await waitForCondition(() => rpcCalls('chat.stream').length === 1);
      expect(rpcCalls('chat.stream')).toEqual([
        {
          agent_id: 'builder@vbot',
          session_id: 'builder-session',
          content: 'Hello project agent',
        },
      ]);
    });

    it('creates a Session for the first Team member when the Project has no default Agent', async () => {
      serveProject({
        defaultAgent: '',
        team: [
          ['first', 'First'],
          ['second', 'Second'],
        ],
      });
      // No listed Session: `session.create` returns `created-first@vbot`.
      rpcMock.mockImplementation(
        createChatRpcMock({ sessionMessages: { 'created-first@vbot': [] } }),
      );
      await chat.mountChat(projectChatProps(), { ready: null });

      await waitForCondition(() => activeTeamTabName().includes('First'));
      // Created with the full address and without `make_current`.
      expect(rpcCalls('session.create')).toEqual([{ agent_id: 'first@vbot' }]);
    });

    it('renders an empty Team bar without an error and keeps the Identity Agent', async () => {
      showProjectMock.mockResolvedValue({
        project: { project_id: 'empty', default_agent: '' },
        scan: { team: [], report: { clean: true, findings: [] } },
      });
      rpcMock.mockImplementation(createChatRpcMock());
      await chat.mountChat({
        ...identityProps(),
        projects: [{ project_id: 'empty', display_name: 'Empty' }],
        selectedProjectId: 'empty',
      });

      await waitForCondition(() =>
        document.querySelector('.chat-view__project-team'),
      );
      const teamBar = document.querySelector('.chat-view__project-team');
      expect(teamBar.querySelector('.agent-tab')).toBeNull();
      expect(teamBar.textContent).toContain(t('chat.project.teamEmpty'));
      expect(document.querySelector('.chat-view__error')).toBeNull();
      expect(selectedPersonalAgentName()).toBe('Alpha');
    });

    it.each([
      [
        'an unclean',
        [{ type: 'bad_model', detail: 'unknown model', agent_id: 'builder' }],
        1,
      ],
      ['a clean', [], 0],
    ])(
      'shows the scan banner for %s Project and links into Projects',
      async (_case, findings, banners) => {
        const onNavigateToProjects = vi.fn();
        serveProject({ findings });
        listedSessions(builderSession);
        rpcMock.mockImplementation(
          createChatRpcMock({ sessionMessages: { 'builder-session': [] } }),
        );
        await chat.mountChat(projectChatProps({ onNavigateToProjects }), {
          ready: null,
        });
        await waitForCondition(() => activeTeamTabName().includes('Builder'));

        expect(document.querySelectorAll('.project-scan-banner')).toHaveLength(
          banners,
        );
        document.querySelector('.project-scan-banner__link')?.click();
        expect(onNavigateToProjects).toHaveBeenCalledTimes(banners);
      },
    );

    it('shows each Agent effective provider/model and thinking effort in the activity hover', async () => {
      rpcMock.mockImplementation(createChatRpcMock());
      showProjectMock.mockResolvedValue({
        project: { project_id: 'vbot', default_agent: 'builder' },
        scan: {
          team: [
            {
              agent_id: 'builder',
              display_name: 'Builder Bot',
              model: 'anthropic/claude-sonnet-4',
              effective: {
                model: {
                  value: 'openai/gpt-5.2::subscription',
                  source: 'override',
                },
                thinking_effort: { value: 'medium', source: 'agent' },
              },
            },
          ],
          report: { clean: true, findings: [] },
        },
      });
      await chat.mountChat(
        projectChatProps({
          sharedAgents: [
            createAgent({
              model: 'openrouter/anthropic/claude-sonnet-4::api-key:work',
            }),
          ],
        }),
        { ready: null },
      );
      // The Project default is active; step up to the Identity Agent so the
      // picker trigger carries its tooltip.
      await waitForCondition(() => activeTeamTabName().includes('Builder'));
      await selectAgentFromPicker('Alpha');
      await waitForCondition(() => selectedPersonalAgentName() === 'Alpha');

      vi.useFakeTimers();
      expect(await hoveredTooltipText(agentPickerTrigger())).toBe(
        'Alpha\nActivity: Idle\nModel: openrouter/anthropic/claude-sonnet-4\nThinking effort: Provider default',
      );
      // An id the name does not already say follows as its own row.
      expect(await hoveredTooltipText(teamTab('Builder'))).toBe(
        'Builder Bot\nActivity: Idle\nModel: openai/gpt-5.2\nThinking effort: medium\nAgent ID: builder',
      );
    });

    it.each([
      ['response', false],
      ['error', false],
      ['response', true],
      ['error', true],
    ])(
      'keeps the newest Project history navigation after an older Team %s (revisit=%s)',
      async (outcome, revisit) => {
        let resolveOld;
        let rejectOld;
        const oldTeam = new Promise((resolve, reject) => {
          resolveOld = resolve;
          rejectOld = reject;
        });
        const team = (projectId, agentId, name) => ({
          project: { project_id: projectId, default_agent: agentId },
          scan: {
            team: [{ agent_id: agentId, display_name: name, model: 'm' }],
            report: { clean: true, findings: [] },
          },
        });
        showProjectMock
          .mockReturnValueOnce(oldTeam)
          .mockImplementation((id) =>
            Promise.resolve(team(id, 'new-agent', 'New team')),
          );
        rpcMock.mockImplementation(
          createChatRpcMock({
            sessionMessages: {
              'old-session': [
                message('old-message', 'Obsolete project transcript'),
              ],
              'new-session': [
                message('new-message', 'Current project transcript'),
              ],
            },
          }),
        );
        const parent = createChatViewParentHarness();
        await chat.mountChat(
          parent.props(['project', 'navigation'], {
            ...identityProps(),
            projects: [
              { project_id: 'old-project', display_name: 'Old project' },
              { project_id: 'new-project', display_name: 'New project' },
            ],
          }),
        );
        const navigate = (projectId, agentId, sessionId, requestId) => {
          parent.setPendingSessionNavigation({
            agentId: `${agentId}@${projectId}`,
            sessionId,
            requestId,
            selection: { agentId: 'alpha', projectId, projectAgentId: agentId },
          });
          flushSync();
        };
        navigate('old-project', 'old-agent', 'old-session', 1);
        await waitForCondition(() =>
          showProjectMock.mock.calls.some(([id]) => id === 'old-project'),
        );
        navigate('new-project', 'new-agent', 'new-session', 2);
        await waitForText('Current project transcript');
        if (revisit) {
          navigate('old-project', 'new-agent', 'new-session', 3);
          await waitForCondition(
            () =>
              showProjectMock.mock.calls.length === 3 &&
              parent.selectedProjectAgentId === 'new-agent',
          );
        }
        if (outcome === 'error') rejectOld(new Error('obsolete failure'));
        else resolveOld(team('old-project', 'old-agent', 'Old team'));
        await oldTeam.catch(() => {});
        await settle(2);

        expect(parent.selectedProjectAgentId).toBe('new-agent');
        expect(document.body.textContent).toContain(
          'Current project transcript',
        );
        expect(document.body.textContent).toContain('New team');
        expect(document.body.textContent).not.toContain('Old team');
        expect(document.body.textContent).not.toContain('obsolete failure');
        expect(historyReads('old-session')).toBe(0);
      },
    );
  });

  describe('Project Agent selection', () => {
    it('keeps one selection across both bars and reports an Identity Agent as empty', async () => {
      serveProject();
      listedSessions(builderSession);
      rpcMock.mockImplementation(
        createChatRpcMock({ sessionMessages: builderReply }),
      );
      const parent = createChatViewParentHarness();
      parent.setSelectedProjectId('vbot');
      await chat.mountChat(parent.props(['project'], identityProps()), {
        ready: 'Builder project reply',
      });

      // The Project Agent deselects the Identity Agent: exactly one
      // selection across both bars.
      expect(activeTabCount()).toBe(1);
      expect(activeTeamTabName()).toContain('Builder');
      expect(selectedPersonalAgentName()).toBe('');

      // Back on the Identity Agent the Team bar keeps no active tab, and App
      // persists '' (Identity active), distinct from null (nothing
      // remembered).
      await selectAgentFromPicker('Alpha');
      await waitForCondition(() => selectedPersonalAgentName() === 'Alpha');
      expect(activeTabCount()).toBe(0);
      expect(parent.selectedProjectAgentId).toBe('');
    });

    it.each([
      [
        'the remembered Team member',
        'reviewer',
        'Reviewer project reply',
        'reviewer@vbot',
      ],
      ['the Identity Agent active beside the Project', '', 'Hello', null],
      [
        'the Project default when the remembered member left',
        'ghost',
        'Builder project reply',
        'builder@vbot',
      ],
    ])(
      'restores %s on reload',
      async (_case, remembered, ready, openedAddress) => {
        serveProject({
          team: [
            ['reviewer', 'Reviewer'],
            ['builder', 'Builder'],
          ],
        });
        const landings = {
          'reviewer@vbot': 'reviewer-session',
          'builder@vbot': 'builder-session',
        };
        listSessionsMock.mockImplementation(async (address) => ({
          sessions: landings[address] ? [{ id: landings[address] }] : [],
        }));
        rpcMock.mockImplementation(
          createChatRpcMock({
            sessionMessages: {
              ...builderReply,
              'reviewer-session': [
                message('reviewer-reply', 'Reviewer project reply'),
              ],
            },
          }),
        );
        await chat.mountChat(
          projectChatProps({ sharedSelectedProjectAgentId: remembered }),
          { ready },
        );
        await waitForCondition(() =>
          document.querySelector('.chat-view__project-team .agent-tab'),
        );

        // Exactly one selection: a Team tab or the Identity Agent.
        expect(activeTabCount()).toBe(openedAddress ? 1 : 0);
        expect(selectedPersonalAgentName()).toBe(openedAddress ? '' : 'Alpha');
        // The background activity refresh may list every Team member, but
        // only the restored Agent's History is read.
        expect(projectHistoryAgents()).toEqual(
          openedAddress ? [openedAddress] : [],
        );
      },
    );

    it('opens the Project default on a genuine Project switch and reports it up', async () => {
      serveProject({
        team: [
          ['reviewer', 'Reviewer'],
          ['builder', 'Builder'],
        ],
      });
      listedSessions(builderSession);
      rpcMock.mockImplementation(
        createChatRpcMock({ sessionMessages: builderReply }),
      );
      const parent = createChatViewParentHarness();
      await chat.mountChat(parent.props(['project'], identityProps()));

      // The user picks a Project from the dropdown (not a reload restore).
      parent.setSelectedProjectId('vbot');
      flushSync();
      await waitForCondition(() => activeTeamTabName().includes('Builder'));

      // App persists the chosen Agent for the next reload.
      expect(parent.selectedProjectAgentId).toBe('builder');
    });
  });

  describe('Project Sessions', () => {
    it('displays a Session override over the active Project Agent and loads it by address', async () => {
      serveProject();
      listedSessions(builderSession);
      rpcMock.mockImplementation(
        createChatRpcMock({
          sessionMessages: {
            ...builderReply,
            'worker-session': [message('worker-reply', 'Worker child reply')],
          },
        }),
      );
      await chat.mountChat(
        projectChatProps({
          pendingSessionNavigation: {
            agentId: 'worker@vbot',
            sessionId: 'worker-session',
            subAgent: true,
          },
        }),
        { ready: 'Worker child reply' },
      );

      // The override wins the display although a Project Agent is active.
      expect(rpcMock).toHaveBeenCalledWith('chat.history', {
        agent_id: 'worker@vbot',
        session_id: 'worker-session',
        limit: 100,
      });
      expect(document.body.textContent).toContain(
        t('chat.subagentSessionNotice'),
      );
    });

    it('qualifies a spawn-row Session link with the displayed Project', async () => {
      serveProject();
      listedSessions(builderSession);
      rpcMock.mockImplementation(
        createChatRpcMock({
          sessionMessages: {
            'builder-session': [
              message('builder-user', 'Spawn a worker', 'user'),
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
          },
        }),
      );
      const navigateToSubAgent = vi.fn();
      await chat.mountChat(projectChatProps({ navigateToSubAgent }), {
        ready: null,
      });
      const openLabel = t('chat.subagent.openSession');
      await waitForCondition(() =>
        document.querySelector(`button[aria-label="${openLabel}"]`),
      );

      document.querySelector(`button[aria-label="${openLabel}"]`).click();
      flushSync();

      // The persisted descriptor carries the bare child id; the navigation
      // carries the parent Project's qualified address.
      expect(navigateToSubAgent).toHaveBeenCalledWith(
        expect.objectContaining({
          agentId: 'worker@vbot',
          sessionId: 'worker-session',
        }),
      );
    });

    it('lists and opens drawer Sessions of a Project Agent by full address', async () => {
      serveProject();
      listedSessions(builderSession, {
        id: 'builder-old',
        title: 'Older builder topic',
        created_at: '2026-06-01T00:00:00+00:00',
        last_active_at: '2026-06-02T00:00:00+00:00',
      });
      rpcMock.mockImplementation(
        createChatRpcMock({
          sessionMessages: {
            ...builderReply,
            'builder-old': [
              message('builder-old-reply', 'Older builder reply'),
            ],
          },
        }),
      );
      await chat.mountChat(projectChatProps({ hasConnectedProvider: false }), {
        ready: 'Builder project reply',
      });

      findButtonByText(t('sessions.title')).click();
      await waitForCondition(
        () => document.querySelectorAll('.session-row__select').length === 2,
      );
      // The drawer lists the Project Agent's Sessions through the full
      // address, not the bare Team member id.
      expect(listSessionsMock).toHaveBeenCalledWith(
        'builder@vbot',
        expect.objectContaining({ limit: 1, includeSubagents: false }),
      );
      findButtonByText('Older builder topic').click();

      await waitForText('Older builder reply');
      expect(rpcMock).toHaveBeenCalledWith('chat.history', {
        agent_id: 'builder@vbot',
        session_id: 'builder-old',
        limit: 100,
      });
      // Session selection is ordinary navigation: no return banner.
      expect(findButtonByText(t('chat.returnToCurrentSession'))).toBeFalsy();
      expect(document.body.textContent).toContain(t('chat.noProvider.title'));
    });

    it('releases a deleted Project Agent Session so reopening the Agent lands elsewhere', async () => {
      serveProject();
      const newest = { ...builderSession, title: 'Newest builder topic' };
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
          'builder-session': [message('newest-reply', 'Newest reply')],
          'builder-old': [message('older-reply', 'Older reply')],
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
      await chat.mountChat(projectChatProps(), { ready: 'Newest reply' });

      findButtonByText(t('sessions.title')).click();
      await waitForCondition(
        () => document.querySelectorAll('.session-row').length === 2,
      );
      Array.from(document.querySelectorAll('.session-row'))
        .find((row) => row.textContent.includes('Newest builder topic'))
        .querySelector('.session-row__menu-trigger')
        .click();
      flushSync();
      document.querySelector('.session-row__menu-item--danger').click();
      flushSync();
      findButtonByText(t('common.delete')).click();
      await waitForText('Older reply');
      const deletedReads = historyReads('builder-session');

      // Leave for the Identity Agent, then reopen the Project Agent.
      await selectAgentFromPicker('Alpha');
      await waitForCondition(() => selectedPersonalAgentName() === 'Alpha');
      const landingReads = historyReads('builder-old');
      teamTab('Builder').click();
      await waitForCondition(
        () =>
          historyReads('builder-old') > landingReads ||
          historyReads('builder-session') > deletedReads,
      );

      expect(historyReads('builder-session')).toBe(deletedReads);
      expect(document.body.textContent).toContain('Older reply');
    });

    it('moves the current Identity Session to a Team member with /agent', async () => {
      serveProject();
      const alpha = createAgent({ current_session_id: 'shared-session' });
      rpcMock.mockImplementation(
        createChatRpcMock({
          agents: [alpha],
          // The moved Session keeps its id; History is keyed by Session id.
          sessionMessages: {
            'shared-session': [
              message('shared-reply', 'Identity session reply'),
            ],
          },
          streamHandler: streamResponses({
            '/agent builder@vbot': handledCommand('Moved to builder@vbot.', {
              output: 'action',
              data: {
                command: 'agent',
                session_id: 'shared-session',
                agent_id: 'builder@vbot',
              },
            }),
          }),
        }),
      );
      const parent = createChatViewParentHarness();
      await chat.mountChat(
        parent.props(['project'], {
          ...identityProps(),
          sharedAgents: [alpha],
        }),
        { ready: 'Identity session reply' },
      );

      sendComposerMessage('/agent builder@vbot');
      await waitForCondition(
        () => historyReads('shared-session', 'builder@vbot') > 0,
      );

      // Sent from the Identity bar with the bare address; the same Session
      // opens under the full Project address without creating one.
      expect(rpcCalls('chat.stream')).toEqual([
        {
          agent_id: 'alpha',
          session_id: 'shared-session',
          content: '/agent builder@vbot',
        },
      ]);
      expect(rpcCalls('session.create')).toEqual([]);
      await waitForCondition(() => activeTeamTabName().includes('Builder'));
      expect(parent.selectedProjectId).toBe('vbot');
      expect(parent.selectedProjectAgentId).toBe('builder');
    });

    it('moves a Project Agent Session back to an Identity Agent with /agent', async () => {
      serveProject();
      listedSessions(builderSession);
      rpcMock.mockImplementation(
        createChatRpcMock({
          sessionMessages: builderReply,
          streamHandler: streamResponses({
            '/agent alpha': handledCommand('Moved to alpha.', {
              output: 'action',
              data: {
                command: 'agent',
                session_id: 'builder-session',
                agent_id: 'alpha',
              },
            }),
          }),
        }),
      );
      await chat.mountChat(
        projectChatProps({
          sharedAgents: [createAgent({ current_session_id: 'alpha-current' })],
        }),
        { ready: 'Builder project reply' },
      );

      sendComposerMessage('/agent alpha');
      await waitForCondition(
        () => historyReads('builder-session', 'alpha') > 0,
      );

      // Sent from the Project bar with the full source address.
      expect(rpcCalls('chat.stream')).toEqual([
        {
          agent_id: 'builder@vbot',
          session_id: 'builder-session',
          content: '/agent alpha',
        },
      ]);
      await waitForCondition(() => selectedPersonalAgentName() === 'Alpha');
    });
  });
});
