// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';

import { t } from '../../lib/i18n.js';
import {
  agentPill,
  agentShowsUnread,
  createAgent,
  createChatRpcMock,
  flushSync,
  handledCommand,
  historyReads,
  listSessionActivityMock,
  listSessionsMock,
  listedSessions,
  markedRead,
  message,
  projectAgentName,
  projectChatProps,
  rpcMock,
  runServerEvent,
  runSummary,
  selectAgentFromPicker,
  selectProjectAgentFromPicker,
  selectedAgentName,
  sendComposerMessage,
  serveProject,
  serveSessionActivity,
  setPageAttention,
  setupChatViewTestSuite,
  streamResponses,
  testChatStateRefs,
  testRunStreamRefs,
  unread,
  waitForCondition,
  waitForText,
} from './ChatView.support.js';
import { createChatViewParentHarness } from './ChatView.parent.support.svelte.js';

const beta = (currentSessionId) =>
  createAgent({
    id: 'beta',
    name: 'Beta',
    current_session_id: currentSessionId,
  });

const teamMemberIsUnread = (name) => agentShowsUnread(projectAgentName(name));

describe('ChatView Agent activity', () => {
  const chat = setupChatViewTestSuite();

  describe('personal Agents', () => {
    it('shows background Agent activity as running without selecting that Agent', async () => {
      const agents = [createAgent(), beta('session-beta')];
      rpcMock.mockImplementation(createChatRpcMock({ agents }));
      await chat.mountChat({ sharedAgents: agents });

      testRunStreamRefs[0].handleServerEvents(
        runServerEvent('run_started', {
          sequence: 1,
          run_id: 'run-beta',
          agent_id: 'beta',
          session_id: 'session-beta',
          output: { status: 'running' },
        }),
      );
      flushSync();

      expect(selectedAgentName()).toBe('Alpha');
      expect(
        agentPill('Beta')?.querySelector('.tab-indicator--running'),
      ).toBeTruthy();
    });

    it.each([
      [
        'its current Session',
        'session-beta',
        {
          'session-beta': [runSummary('summary-beta', 'run-beta')],
        },
        [unread('session-beta', 'run-beta')],
      ],
      [
        'another Session',
        'beta-current',
        {
          'beta-current': [
            message('beta-current-reply', 'Beta current conversation'),
          ],
          'session-beta': [
            message('beta-unread-reply', 'Beta unread result'),
            runSummary('beta-unread-summary', 'run-beta'),
          ],
        },
        [
          {
            id: 'beta-current',
            latest_completion_run_id: 'run-beta-current',
            has_unread_completion: false,
          },
          unread('session-beta', 'run-beta'),
        ],
      ],
    ])(
      'keeps an inactive Agent unread until its result in %s is opened',
      async (_case, currentSessionId, sessionMessages, activity) => {
        const agents = [createAgent(), beta(currentSessionId)];
        rpcMock.mockImplementation(
          createChatRpcMock({ agents, sessionMessages }),
        );
        serveSessionActivity({ beta: activity });
        await chat.mountChat({ sharedAgents: agents });

        await waitForCondition(() => agentShowsUnread('Beta'));
        const betaPill = agentPill('Beta');
        expect(betaPill.getAttribute('aria-label')).toBe(
          t('chat.agentActivity.unreadOne', {
            name: 'Beta',
          }),
        );
        await waitForCondition(() => betaPill.disabled === false);
        betaPill.click();

        await waitForCondition(() => selectedAgentName() === 'Beta');
        expect(rpcMock).toHaveBeenCalledWith('chat.history', {
          agent_id: 'beta',
          session_id: 'session-beta',
          limit: 100,
        });
        await waitForCondition(() =>
          markedRead('beta', 'session-beta', 'run-beta'),
        );
        flushSync();

        // The displayed Agent shows no unread result.
        expect(historyReads('beta-current')).toBe(0);
        expect(agentShowsUnread('Beta')).toBe(false);
      },
    );

    it('does not resurrect a read result when retained events replay after remount', async () => {
      const agents = [createAgent(), beta('session-beta')];
      rpcMock.mockImplementation(createChatRpcMock({ agents }));
      serveSessionActivity({
        beta: [
          {
            id: 'session-beta',
            latest_completion_run_id: 'run-beta',
            has_unread_completion: false,
            unread_run_id: null,
            unread_run_status: null,
            unread_run_at: null,
          },
        ],
      });
      await chat.mountChat({
        sharedAgents: agents,
        runServerEvents: [
          runServerEvent('run_completed', {
            sequence: 2,
            run_id: 'run-beta',
            agent_id: 'beta',
            session_id: 'session-beta',
            status: 'completed',
          }),
        ],
      });

      const betaSession = () =>
        testChatStateRefs[0].sessions['beta::session-beta'];
      await waitForCondition(
        () => betaSession()?.latestCompletionRunId === 'run-beta',
      );
      flushSync();

      expect(betaSession().hasUnreadCompletion).toBe(false);
      expect(agentShowsUnread('Beta')).toBe(false);
    });

    it('clears a delivered child result and lands on the Agent user Session', async () => {
      const agents = [
        createAgent({ current_session_id: 'parent-session' }),
        beta('beta-user-session'),
      ];
      let childDelivered = false;
      rpcMock.mockImplementation(
        createChatRpcMock({
          agents,
          sessionMessages: {
            'parent-session': [],
            'beta-user-session': [
              message('beta-user-message', 'Beta user conversation'),
            ],
            'beta-child-session': [
              runSummary('beta-child-summary', 'beta-child-run'),
            ],
          },
        }),
      );
      serveSessionActivity((address) =>
        address === 'beta'
          ? [
              {
                id: 'beta-user-session',
                latest_completion_run_id: null,
                has_unread_completion: false,
              },
              childDelivered
                ? {
                    id: 'beta-child-session',
                    latest_completion_run_id: 'beta-child-run',
                    has_unread_completion: false,
                  }
                : unread('beta-child-session', 'beta-child-run'),
            ]
          : [],
      );
      const parent = createChatViewParentHarness();
      await chat.mountChat(
        parent.props(['agent', 'sessions'], { sharedAgents: agents }),
        { ready: null },
      );
      await waitForCondition(() => agentShowsUnread('Beta'));

      childDelivered = true;
      parent.bumpSessionsRefreshToken();
      flushSync();
      await waitForCondition(() => !agentShowsUnread('Beta'));

      await selectAgentFromPicker('Beta');
      await waitForText('Beta user conversation');
      expect(selectedAgentName()).toBe('Beta');
      expect(historyReads('beta-user-session', 'beta')).toBeGreaterThan(0);
      expect(historyReads('beta-child-session')).toBe(0);
    });

    it('keeps a displayed terminal result idle before read acknowledgement returns', async () => {
      let resolveMarkRead;
      const defaultRpc = createChatRpcMock();
      rpcMock.mockImplementation((method, params) =>
        method === 'session.mark_read'
          ? new Promise((resolve) => {
              resolveMarkRead = () => resolve(defaultRpc(method, params));
            })
          : defaultRpc(method, params),
      );
      await chat.mountChat({
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      });

      const run = {
        run_id: 'run-visible',
        agent_id: 'alpha',
        session_id: 'session-1',
      };
      testRunStreamRefs[0].handleServerEvents(
        runServerEvent('run_started', {
          ...run,
          sequence: 1,
          output: { status: 'running' },
        }),
      );
      testRunStreamRefs[0].handleServerEvents(
        runServerEvent('run_completed', {
          ...run,
          sequence: 2,
          status: 'completed',
        }),
      );
      flushSync();
      await waitForCondition(() =>
        markedRead('alpha', 'session-1', 'run-visible'),
      );
      flushSync();

      expect(selectedAgentName()).toBe('Alpha');
      expect(agentShowsUnread('Alpha')).toBe(false);
      resolveMarkRead();
    });

    // The acknowledgement tells other apps (the vBot tray) the result was
    // seen, so a page that is not looked at must not send it.
    it.each([
      [
        'unfocused',
        { focused: false },
        () => window.dispatchEvent(new Event('focus')),
      ],
      [
        'hidden',
        { visible: false },
        () => document.dispatchEvent(new Event('visibilitychange')),
      ],
    ])(
      'acknowledges a displayed result only once the %s page is attended again',
      async (_case, unattended, regainAttention) => {
        rpcMock.mockImplementation(createChatRpcMock());
        await chat.mountChat({
          sharedAgents: [createAgent()],
          sharedSelectedAgentId: 'alpha',
        });
        setPageAttention(unattended);

        const run = {
          run_id: 'run-unseen',
          agent_id: 'alpha',
          session_id: 'session-1',
        };
        testRunStreamRefs[0].handleServerEvents(
          runServerEvent('run_started', {
            ...run,
            sequence: 1,
            output: { status: 'running' },
          }),
        );
        testRunStreamRefs[0].handleServerEvents(
          runServerEvent('run_completed', {
            ...run,
            sequence: 2,
            status: 'completed',
          }),
        );
        const sessionState = () =>
          testChatStateRefs[0].sessions['alpha::session-1'];
        await waitForCondition(
          () => sessionState()?.unreadRunId === 'run-unseen',
        );

        expect(markedRead('alpha', 'session-1', 'run-unseen')).toBe(false);

        setPageAttention();
        regainAttention();
        await waitForCondition(() =>
          markedRead('alpha', 'session-1', 'run-unseen'),
        );
      },
    );
  });

  describe('Project Team members', () => {
    // A two-member Team (Orchestrator is the Project default) whose Session
    // activity is served from `unreadResults` (address -> { sessionId, runId }),
    // so a test can reveal a finished result later and signal it.
    // `session.mark_read` acknowledges the result the way the server does.
    async function mountTeamWithUnreadResults(unreadResults) {
      serveProject({
        defaultAgent: 'orchestrator',
        team: [
          ['orchestrator', 'Orchestrator'],
          ['explorer', 'Explorer'],
        ],
      });
      const landingSessions = {
        'orchestrator@vbot': 'orch-session',
        'explorer@vbot': 'explorer-held',
      };
      const readRunIds = new Set();
      const baseRpc = createChatRpcMock({
        sessionMessages: {
          'orch-session': [message('orch-reply', 'Orchestrator chat')],
          'orch-unread': [
            message('orch-result', 'Orchestrator unread result'),
            runSummary('orch-summary', 'run-orch'),
          ],
          'explorer-held': [message('explorer-reply', 'Explorer earlier')],
          'explorer-unread': [
            message('explorer-result', 'Explorer unread result'),
            runSummary('explorer-summary', 'run-explorer'),
          ],
        },
      });
      rpcMock.mockImplementation(async (method, params) => {
        if (method === 'session.mark_read') readRunIds.add(params.run_id);
        return baseRpc(method, params);
      });
      listSessionsMock.mockImplementation(async (address) => ({
        sessions: landingSessions[address]
          ? [{ id: landingSessions[address] }]
          : [],
      }));
      serveSessionActivity((address) => {
        const result = unreadResults[address];
        if (!result) return [];
        return readRunIds.has(result.runId)
          ? [
              {
                id: result.sessionId,
                latest_completion_run_id: result.runId,
                has_unread_completion: false,
              },
            ]
          : [unread(result.sessionId, result.runId)];
      });
      const parent = createChatViewParentHarness();
      await chat.mountChat(parent.props(['sessions'], projectChatProps()), {
        ready: 'Orchestrator chat',
      });
      return parent;
    }

    async function openTeamMember(name, text) {
      await selectProjectAgentFromPicker(name);
      await waitForText(text);
    }

    const explorerResult = {
      sessionId: 'explorer-unread',
      runId: 'run-explorer',
    };

    it.each([
      ['holds an older Session', true],
      ['was never opened', false],
    ])(
      'opens the unread Session of a Team member that %s and marks it read',
      async (_case, holdsOlderSession) => {
        const unreadResults = holdsOlderSession
          ? {}
          : { 'explorer@vbot': explorerResult };
        const parent = await mountTeamWithUnreadResults(unreadResults);
        if (holdsOlderSession) {
          // Explorer lands on (and holds) its earlier Session, then a result
          // finishes in another Explorer Session.
          await openTeamMember('Explorer', 'Explorer earlier');
          await openTeamMember('Orchestrator', 'Orchestrator chat');
          unreadResults['explorer@vbot'] = explorerResult;
          parent.bumpSessionsRefreshToken();
        }
        await waitForCondition(() => teamMemberIsUnread('Explorer'));

        await openTeamMember('Explorer', 'Explorer unread result');
        expect(rpcMock).toHaveBeenCalledWith('chat.history', {
          agent_id: 'explorer@vbot',
          session_id: 'explorer-unread',
          limit: 100,
        });
        expect(document.body.textContent).not.toContain('Explorer earlier');
        expect(selectedAgentName()).toBe(projectAgentName('Explorer'));
        await waitForCondition(() =>
          markedRead('explorer@vbot', 'explorer-unread', 'run-explorer'),
        );

        // The acknowledged result stays read once another Agent is displayed.
        await openTeamMember('Orchestrator', 'Orchestrator chat');
        expect(teamMemberIsUnread('Explorer')).toBe(false);
      },
    );

    it('switches the selected Team member to its unread Session when clicked again', async () => {
      const unreadResults = {};
      const parent = await mountTeamWithUnreadResults(unreadResults);
      await waitForCondition(() =>
        listSessionActivityMock.mock.calls.some(([addresses]) =>
          addresses.includes('explorer@vbot'),
        ),
      );
      listSessionActivityMock.mockClear();

      // A result finishes in another Session of the displayed Orchestrator.
      // This window holds no event of that Run, so only the named Agent's
      // activity is read again.
      unreadResults['orchestrator@vbot'] = {
        sessionId: 'orch-unread',
        runId: 'run-orch',
      };
      parent.pushSessionInvalidation({
        project_id: 'vbot',
        agent_id: 'orchestrator',
        session_id: 'orch-unread',
        run_id: 'run-orch',
      });
      await waitForCondition(() => teamMemberIsUnread('Orchestrator'));
      expect(listSessionActivityMock.mock.calls).toEqual([
        [['orchestrator@vbot']],
      ]);

      await openTeamMember('Orchestrator', 'Orchestrator unread result');
      expect(document.body.textContent).not.toContain('Orchestrator chat');
      await waitForCondition(() =>
        markedRead('orchestrator@vbot', 'orch-unread', 'run-orch'),
      );
      expect(teamMemberIsUnread('Orchestrator')).toBe(false);
    });

    it('lands a Session moved into a Team member on that Session, not its unread one', async () => {
      serveProject({
        team: [
          ['builder', 'Builder'],
          ['reviewer', 'Reviewer'],
        ],
      });
      listedSessions('builder-session');
      rpcMock.mockImplementation(
        createChatRpcMock({
          sessionMessages: {
            'builder-session': [message('b1', 'Builder project reply')],
            'reviewer-unread': [
              message('r1', 'Reviewer unread result'),
              runSummary('r2', 'run-r'),
            ],
          },
          streamHandler: streamResponses({
            '/agent reviewer@vbot': handledCommand('Moved to reviewer@vbot.', {
              output: 'action',
              data: {
                command: 'agent',
                session_id: 'builder-session',
                agent_id: 'reviewer@vbot',
              },
            }),
          }),
        }),
      );
      serveSessionActivity({
        'reviewer@vbot': [unread('reviewer-unread', 'run-r')],
      });
      await chat.mountChat(projectChatProps(), {
        ready: 'Builder project reply',
      });
      await waitForCondition(() => teamMemberIsUnread('Reviewer'));

      sendComposerMessage('/agent reviewer@vbot');

      // The same Session id opens under the new Project Agent's address.
      await waitForCondition(
        () => historyReads('builder-session', 'reviewer@vbot') > 0,
      );
      expect(historyReads('reviewer-unread')).toBe(0);
      await waitForCondition(
        () => selectedAgentName() === projectAgentName('Reviewer'),
      );
    });
  });
});
