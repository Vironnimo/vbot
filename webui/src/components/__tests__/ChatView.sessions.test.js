// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import { t } from '../../lib/i18n.js';
import {
  createAgent,
  createChatRpcMock,
  createHistoryMessages,
  findButtonByText,
  findCancelRunButton,
  findNewSessionButton,
  flushSync,
  getSessionMock,
  historyReads,
  listedSessions,
  message,
  rpcCalls,
  rpcMock,
  runningRun,
  projectChatProps,
  selectAgentFromPicker,
  selectedPersonalAgentName,
  sendComposerMessage,
  serveProject,
  setInputValue,
  settle,
  setupChatViewTestSuite,
  subscribeRunEventsMock,
  testChatStateRefs,
  teamTab,
  tick,
  waitForCondition,
  waitForText,
} from './ChatView.support.js';
import { createChatViewParentHarness } from './ChatView.parent.support.svelte.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const SUB_AGENT_NOTICE = () => t('chat.subagentSessionNotice');
const returnToCurrentButton = () =>
  findButtonByText(t('chat.returnToCurrentSession'));
const returnToParentButton = () =>
  findButtonByText(t('chat.returnToParentSession'));
const composerInput = () => document.querySelector('.msg-input');
const sessionsButton = () => findButtonByText(t('sessions.title'));

// Serves `session.get` point reads from `sessions` keyed `address::sessionId`.
function serveSessions(sessions) {
  getSessionMock.mockImplementation(async (agentAddress, sessionId) => ({
    session: sessions[`${agentAddress}::${sessionId}`] ?? null,
  }));
}

async function openFromDrawer(title) {
  sessionsButton().click();
  await waitForCondition(() => Boolean(findButtonByText(title)));
  const row = findButtonByText(title);
  row.focus();
  row.click();
}

async function openSessionInfoLink(selector, text) {
  document.querySelector('.chat-activity__rail').click();
  flushSync();
  await waitForCondition(
    () => document.querySelector(selector)?.textContent.trim() === text,
  );
  document.querySelector(selector).click();
  flushSync();
}

describe('ChatView Sessions', () => {
  const chat = setupChatViewTestSuite();

  describe('New session', () => {
    it.each([
      ['reuses an already empty Session', { 'session-1': [] }, 0],
      ['creates one Session from a non-empty Session', {}, 1],
      ['creates one Project Agent Session', {}, 1, true],
    ])(
      '%s for repeated clicks and focuses its composer',
      async (_case, sessionMessages, creates, project) => {
        const agentAddress = project ? 'builder@vbot' : 'alpha';
        const createdSessionId = `created-${agentAddress}`;
        if (project) {
          serveProject();
          listedSessions('session-1');
        }
        rpcMock.mockImplementation(
          createChatRpcMock({
            sessionMessages: { ...sessionMessages, [createdSessionId]: [] },
          }),
        );
        await chat.mountChat(project ? projectChatProps() : {}, {
          ready: null,
        });
        await waitForCondition(() => historyReads('session-1') > 0);

        findNewSessionButton().focus();
        findNewSessionButton().click();
        findNewSessionButton().click();

        await waitForCondition(
          () =>
            document.activeElement === composerInput() &&
            historyReads(createdSessionId) === creates,
        );
        expect(rpcCalls('session.create')).toHaveLength(creates);
        if (creates) {
          expect(rpcCalls('session.create')).toEqual([
            project
              ? { agent_id: agentAddress }
              : { agent_id: agentAddress, make_current: true },
          ]);
        }
      },
    );

    it('creates a new Session when the empty transcript has a draft and preserves that draft', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({
          sessionMessages: { 'session-1': [], 'created-alpha': [] },
        }),
      );
      listedSessions(
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
      );
      await chat.mountChat({}, { ready: null });
      await waitForCondition(() => historyReads('session-1') > 0);

      const input = composerInput();
      setInputValue(input, 'unfinished draft');
      flushSync();
      findNewSessionButton().click();
      await waitForCondition(() => historyReads('created-alpha') > 0);
      expect(input.value).toBe('');

      await openFromDrawer('Draft home');
      await waitForCondition(() => input.value === 'unfinished draft');
      expect(rpcCalls('session.create')).toHaveLength(1);
      setInputValue(input, '');
      flushSync();
    });

    it('blocks sending while New session is pending so no message reaches the Session being left', async () => {
      const baseRpc = createChatRpcMock({
        sessionMessages: { 'created-alpha': [] },
      });
      let resolveCreate = null;
      rpcMock.mockImplementation((method, params) =>
        method === 'session.create'
          ? new Promise((resolve) => {
              resolveCreate = () => resolve(baseRpc(method, params));
            })
          : baseRpc(method, params),
      );
      await chat.mountChat();
      expect(composerInput().disabled).toBe(false);

      findNewSessionButton().click();
      await waitForCondition(() => resolveCreate !== null);
      expect(composerInput().disabled).toBe(true);
      sendComposerMessage('Sent during the switch');
      composerInput().dispatchEvent(
        new KeyboardEvent('keydown', { bubbles: true, key: 'Enter' }),
      );
      flushSync();

      resolveCreate();
      await waitForCondition(
        () =>
          historyReads('created-alpha') > 0 &&
          composerInput().disabled === false,
      );
      expect(rpcCalls('chat.stream')).toHaveLength(0);
    });

    it.each([
      ['an Agent switch', 'agent'],
      ['a same-Agent drawer Session', 'session'],
      ['the same displayed Session chosen again', 'same-session'],
      ['a history restore of the displayed Session', 'restore'],
      ['leaving and revisiting the same Agent', 'revisit'],
      ['leaving Chat', 'hidden'],
      ['leaving and returning to Chat', 'shown-again'],
      ['leaving and revisiting a Project Agent', 'project-revisit'],
      ['a Project Agent drawer Session', 'project-session'],
      ['navigation during the created Session History load', 'history'],
    ])(
      'reconciles a delayed New session without replacing %s or its focus',
      async (_case, scenario) => {
        const project = scenario.startsWith('project');
        const agentAddress = project ? 'builder@vbot' : 'alpha';
        const createdSessionId = `created-${agentAddress}`;
        const agents = [
          createAgent(),
          createAgent({
            id: 'beta',
            name: 'Beta',
            current_session_id: 'session-beta',
          }),
        ];
        if (project) serveProject();
        listedSessions(
          {
            id: 'session-1',
            title: 'Current topic',
            created_at: '2026-05-10T00:00:00+00:00',
            last_active_at: '2026-05-10T00:00:00+00:00',
          },
          {
            id: 'session-2',
            title: 'Older topic',
            created_at: '2026-05-09T00:00:00+00:00',
            last_active_at: '2026-05-09T00:00:00+00:00',
          },
        );
        const baseRpc = createChatRpcMock({
          agents,
          sessionMessages: {
            'session-beta': [message('beta-reply', 'Beta session reply')],
            'session-2': [message('older-reply', 'Older session reply')],
            [createdSessionId]: [
              message('created-reply', 'Created session reply'),
            ],
          },
        });
        let resolveCreate;
        let resolveHistory;
        rpcMock.mockImplementation((method, params) => {
          if (method === 'session.create') {
            return new Promise((resolve) => {
              resolveCreate = () => resolve(baseRpc(method, params));
            });
          }
          if (
            scenario === 'history' &&
            method === 'chat.history' &&
            params.session_id === createdSessionId
          ) {
            return new Promise((resolve) => {
              resolveHistory = () => resolve(baseRpc(method, params));
            });
          }
          return baseRpc(method, params);
        });
        const parent = createChatViewParentHarness();
        if (project) parent.setSelectedProjectId('vbot');
        const visibility = reactiveProps({ active: true });
        const onAgentsChanged = vi.fn();
        const props = parent.props(['agent', 'project', 'navigation'], {
          sharedAgents: agents,
          projects: [{ project_id: 'vbot', display_name: 'vBot' }],
          onAgentsChanged,
        });
        Object.defineProperty(props, 'active', {
          get: () => visibility.active,
          enumerable: true,
        });
        await chat.mountChat(props);

        findNewSessionButton().click();
        await waitForCondition(() => Boolean(resolveCreate));
        if (scenario === 'history') {
          resolveCreate();
          await waitForCondition(() => Boolean(resolveHistory));
        }
        let expectedText = 'Hello';
        let expectedAgent = project ? '' : 'Alpha';
        if (scenario === 'agent' || scenario === 'history') {
          await selectAgentFromPicker('Beta');
          expectedText = 'Beta session reply';
          expectedAgent = 'Beta';
        } else if (scenario === 'same-session') {
          await openFromDrawer('Current topic');
        } else if (scenario === 'restore') {
          parent.setPendingSessionNavigation({
            agentId: agentAddress,
            sessionId: 'session-1',
            requestId: 1,
          });
          flushSync();
        } else if (scenario.endsWith('session')) {
          await openFromDrawer('Older topic');
          expectedText = 'Older session reply';
        } else if (scenario === 'revisit') {
          await selectAgentFromPicker('Beta');
          await waitForText('Beta session reply');
          await selectAgentFromPicker('Alpha');
        } else if (scenario === 'project-revisit') {
          await selectAgentFromPicker('Alpha');
          await waitForCondition(() => selectedPersonalAgentName() === 'Alpha');
          teamTab('Builder').click();
          flushSync();
        } else {
          visibility.active = false;
          flushSync();
          if (scenario === 'shown-again') {
            visibility.active = true;
            flushSync();
          }
        }
        await waitForText(expectedText);
        await settle(2);
        if (scenario !== 'hidden') {
          expect(composerInput().disabled).toBe(false);
        }
        const focusTarget = document.createElement('button');
        document.body.append(focusTarget);
        focusTarget.focus();

        if (scenario === 'history') resolveHistory();
        else resolveCreate();
        await waitForCondition(() => !findNewSessionButton().disabled);
        await settle();

        expect(document.body.textContent).toContain(expectedText);
        expect(document.body.textContent).not.toContain(
          'Created session reply',
        );
        expect(selectedPersonalAgentName()).toBe(expectedAgent);
        expect(parent.selectedAgentId).toBe(
          expectedAgent === 'Beta' ? 'beta' : 'alpha',
        );
        expect(document.activeElement).toBe(focusTarget);
        expect(historyReads(createdSessionId)).toBe(
          scenario === 'history' ? 1 : 0,
        );
        if (!project) {
          expect(
            onAgentsChanged.mock.calls
              .at(-1)[0]
              .find((agent) => agent.id === 'alpha').current_session_id,
          ).toBe(createdSessionId);
        }

        // A later deliberate return opens the new current Session; retaining
        // the superseding view must not trap subsequent navigation there.
        visibility.active = true;
        flushSync();
        await selectAgentFromPicker('Alpha');
        if (project) {
          teamTab('Builder').click();
          flushSync();
        }
        // The delayed History case gates each read, including this return.
        if (scenario === 'history') {
          await waitForCondition(() => historyReads(createdSessionId) > 1);
          resolveHistory();
        }
        await waitForText('Created session reply');
        expect(rpcCalls('session.create')).toHaveLength(1);
      },
    );

    it('starts a Run in a new Session while the previous Session Run remains active', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({
          sessionMessages: { 'created-alpha': [] },
          activeRuns: { 'session-1': runningRun('run-one') },
          streamHandler: (params) => ({
            ...runningRun('run-two'),
            session_id: params.session_id,
          }),
        }),
      );
      await chat.mountChat();
      await waitForCondition(() => Boolean(findCancelRunButton()));

      expect(findNewSessionButton().disabled).toBe(false);
      findNewSessionButton().click();
      await waitForCondition(() => historyReads('created-alpha') > 0);
      sendComposerMessage('Run in parallel');
      await waitForCondition(() =>
        rpcCalls('chat.stream').some(
          (params) => params.session_id === 'created-alpha',
        ),
      );

      const sessions = testChatStateRefs[0].sessions;
      expect(sessions['alpha::session-1'].currentRun?.runId).toBe('run-one');
      expect(sessions['alpha::created-alpha'].currentRun?.runId).toBe(
        'run-two',
      );
      for (const key of ['alpha::session-1', 'alpha::created-alpha']) {
        expect(sessions[key].status).toBe('running');
      }
    });

    it('blocks New session before the selected Agent has an active Session projection', async () => {
      const beta = createAgent({
        id: 'beta',
        name: 'Beta',
        current_session_id: 'session-beta',
      });
      rpcMock.mockImplementation(
        createChatRpcMock({ agents: [createAgent(), beta] }),
      );
      const props = reactiveProps({
        active: true,
        sharedAgents: [createAgent(), beta],
        sharedSelectedAgentId: 'alpha',
      });
      await chat.mountChat(props);

      props.sharedSelectedAgentId = 'beta';
      const chatState = testChatStateRefs.at(-1);
      chatState.selectedAgentId = 'beta';
      chatState.loadingHistory = false;
      flushSync();

      const newSessionButton = findNewSessionButton();
      expect(newSessionButton.disabled).toBe(true);
      // Exercise the handler guard as well as the rendered disabled state by
      // simulating a stale click that bypasses native disabling.
      newSessionButton.disabled = false;
      newSessionButton.click();
      await Promise.resolve();
      expect(rpcCalls('session.create')).toEqual([]);
    });
  });

  describe('Session navigation', () => {
    it.each([
      ['focuses the composer on desktop', false],
      ['keeps reading mode on mobile', true],
    ])(
      'opens a drawer Session and %s, while New session always focuses',
      async (_case, mobile) => {
        const originalMatchMedia = window.matchMedia;
        window.matchMedia = () => ({ matches: mobile });
        try {
          rpcMock.mockImplementation(
            createChatRpcMock({
              sessionMessages: {
                'session-2': [message('assistant-two', 'Second session reply')],
                'created-alpha': [],
              },
            }),
          );
          listedSessions({
            id: 'session-2',
            title: 'Second topic',
            created_at: '2026-05-10T00:00:00+00:00',
            last_active_at: '2026-05-10T01:00:00+00:00',
          });
          await chat.mountChat();

          await openFromDrawer('Second topic');
          await waitForText('Second session reply');
          await waitForCondition(
            () => (document.activeElement === composerInput()) === !mobile,
          );
          expect(rpcMock).toHaveBeenCalledWith('chat.history', {
            agent_id: 'alpha',
            session_id: 'session-2',
            limit: 100,
          });

          findNewSessionButton().click();
          await waitForCondition(() => historyReads('created-alpha') > 0);
          await waitForCondition(
            () => document.activeElement === composerInput(),
          );
        } finally {
          window.matchMedia = originalMatchMedia;
        }
      },
    );

    it('does not steal focus when browser history changes the displayed Session', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({
          sessionMessages: {
            'session-2': [
              message('assistant-two', 'History-restored session reply'),
            ],
          },
        }),
      );
      const parent = createChatViewParentHarness();
      await chat.mountChat(parent.props(['navigation']));

      const passiveFocusTarget = document.createElement('button');
      document.body.append(passiveFocusTarget);
      passiveFocusTarget.focus();
      parent.setPendingSessionNavigation({
        agentId: 'alpha',
        sessionId: 'session-2',
        subAgent: false,
        requestId: 'history-navigation-1',
      });
      flushSync();

      await waitForText('History-restored session reply');
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
            'session-2': [message('beta-assistant', 'Beta session reply')],
          },
        }),
      );
      const parent = createChatViewParentHarness();
      await chat.mountChat(parent.props(['agent'], { sharedAgents: agents }));

      await selectAgentFromPicker('Beta');
      await waitForText('Beta session reply');
      await waitForCondition(() => document.activeElement === composerInput());

      const passiveFocusTarget = document.createElement('button');
      document.body.append(passiveFocusTarget);
      passiveFocusTarget.focus();
      parent.setSelectedAgentId('alpha');
      flushSync();
      await waitForCondition(
        () =>
          document.body.textContent.includes('Hello') &&
          !document.body.textContent.includes('Beta session reply'),
      );
      expect(document.activeElement).toBe(passiveFocusTarget);
    });

    it('does not switch the viewed conversation on a Sessions refresh', async () => {
      rpcMock.mockImplementation(createChatRpcMock());
      const parent = createChatViewParentHarness();
      await chat.mountChat(
        parent.props(['sessions'], {
          sharedAgents: [createAgent()],
          sharedSelectedAgentId: 'alpha',
        }),
      );
      const historyCallsBefore = rpcCalls('chat.history').length;

      // A Sessions signal refreshes the Session list only ("stay put").
      parent.bumpSessionsRefreshToken();
      flushSync();

      expect(rpcCalls('chat.history')).toHaveLength(historyCallsBefore);
      expect(document.body.textContent).toContain('Hello');
    });

    it('loads the newest History first and prepends older messages on top scroll', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({
          sessionMessages: { 'session-1': createHistoryMessages(120) },
        }),
      );
      await chat.mountChat({}, { ready: 'History message 21' });
      expect(document.body.textContent).not.toContain('History message 20');

      const messages = document.querySelector('.messages');
      let scrollHeight = 1000;
      Object.defineProperty(messages, 'scrollHeight', {
        configurable: true,
        get: () => scrollHeight,
      });
      Object.defineProperty(messages, 'offsetHeight', {
        configurable: true,
        get: () => 500,
      });
      Object.defineProperty(messages, 'scrollTop', {
        configurable: true,
        writable: true,
        value: 0,
      });
      await tick();
      messages.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
      messages.dispatchEvent(new Event('scroll'));
      scrollHeight = 1400;

      await waitForCondition(
        () =>
          document.body.textContent.includes('History message 20') &&
          messages.scrollTop === 400,
      );
      expect(rpcCalls('chat.history')).toEqual([
        { agent_id: 'alpha', session_id: 'session-1', limit: 100 },
        {
          agent_id: 'alpha',
          session_id: 'session-1',
          limit: 50,
          before: 'message-021',
        },
      ]);
    });

    it('drops a stale History response for a Session that is no longer displayed', async () => {
      let releaseChildHistory;
      const childHistoryGate = new Promise((resolve) => {
        releaseChildHistory = resolve;
      });
      const baseMock = createChatRpcMock();
      rpcMock.mockImplementation(async (method, params) => {
        if (
          method === 'chat.history' &&
          params?.session_id === 'sub-session-1'
        ) {
          await childHistoryGate;
          return {
            session_id: 'sub-session-1',
            messages: [],
            active_run: runningRun('stale-run'),
          };
        }
        return baseMock(method, params);
      });
      const parent = createChatViewParentHarness();
      await chat.mountChat(
        parent.props(['navigation'], {
          sharedAgents: [createAgent()],
          sharedSelectedAgentId: 'alpha',
        }),
      );

      // Navigate into the child (its History hangs), then straight back to
      // the current Session before the child response arrives.
      parent.setPendingSessionNavigation({
        agentId: 'alpha',
        sessionId: 'sub-session-1',
        subAgent: true,
        requestId: 1,
      });
      flushSync();
      parent.setPendingSessionNavigation({
        returnToCurrent: true,
        requestId: 2,
      });
      flushSync();
      await waitForCondition(
        () =>
          document.body.textContent.includes('Hello') &&
          !document.body.textContent.includes(SUB_AGENT_NOTICE()),
      );

      // The late response neither re-opens a stream for the left Session nor
      // errors or locks the healthy view.
      releaseChildHistory();
      await waitForCondition(
        () => !document.body.textContent.includes(t('loading.history')),
      );
      expect(
        subscribeRunEventsMock.mock.calls.filter(
          ([sseUrl]) => sseUrl === '/api/runs/stale-run/events',
        ),
      ).toHaveLength(0);
      expect(document.querySelector('.chat-view__error')).toBeNull();
      expect(composerInput().disabled).toBe(false);
      expect(document.body.textContent).toContain('Hello');
    });
  });

  describe('sub-agent Session override', () => {
    const overrides = [
      [
        'same-Agent',
        {
          agents: [createAgent()],
          override: { agentId: 'alpha', sessionId: 'sub-session-1' },
          childText: 'Sub-agent response',
          childAgentName: 'Alpha',
          current: { agentId: 'alpha', sessionId: 'session-1', text: 'Hello' },
        },
      ],
      [
        'other-Agent',
        {
          agents: [
            createAgent({ current_session_id: 'parent-session' }),
            createAgent({
              id: 'beta',
              name: 'Beta',
              current_session_id: 'beta-current-session',
            }),
          ],
          override: { agentId: 'beta', sessionId: 'beta-sub-session' },
          childText: 'Beta sub-agent response',
          childAgentName: 'Beta',
          current: {
            agentId: 'alpha',
            sessionId: 'parent-session',
            text: 'Parent main response',
          },
        },
      ],
    ];
    const overrideMessages = {
      'parent-session': [message('parent-reply', 'Parent main response')],
      'beta-sub-session': [
        message('beta-sub-reply', 'Beta sub-agent response'),
      ],
      'beta-current-session': [
        message('beta-current', 'Beta current response'),
      ],
    };

    async function mountOverride({ agents, override, childText }, rpcOptions) {
      rpcMock.mockImplementation(
        createChatRpcMock({
          agents,
          sessionMessages: overrideMessages,
          ...rpcOptions,
        }),
      );
      await chat.mountChat(
        {
          sharedAgents: agents,
          sharedSelectedAgentId: 'alpha',
          pendingSessionNavigation: { ...override, subAgent: true },
        },
        { ready: childText },
      );
    }

    it.each(overrides)(
      'shows a writable notice for a %s override and returns to the parent Agent current Session',
      async (_case, scenario) => {
        await mountOverride(scenario);
        const { override, current } = scenario;

        expect(rpcMock).toHaveBeenCalledWith('chat.history', {
          agent_id: override.agentId,
          session_id: override.sessionId,
          limit: 100,
        });
        expect(selectedPersonalAgentName()).toBe(scenario.childAgentName);
        const notice = document.querySelector(
          '.chat-view__footer-stack .chat-view__footer-banner',
        );
        expect(notice.querySelector('strong').textContent).toContain(
          SUB_AGENT_NOTICE(),
        );
        expect(notice.querySelectorAll('button')).toHaveLength(1);
        expect(notice.querySelector('p')).toBeNull();
        expect(composerInput().disabled).toBe(false);
        // Without parent metadata the notice offers the current Session.
        expect(returnToParentButton()).toBeFalsy();

        returnToCurrentButton().click();
        await waitForCondition(
          () =>
            document.body.textContent.includes(current.text) &&
            !document.body.textContent.includes(SUB_AGENT_NOTICE()),
        );
        expect(rpcMock).toHaveBeenCalledWith('chat.history', {
          agent_id: current.agentId,
          session_id: current.sessionId,
          limit: 100,
        });
        expect(historyReads('beta-current-session')).toBe(0);
        expect(selectedPersonalAgentName()).toBe('Alpha');
        expect(composerInput().disabled).toBe(false);
      },
    );

    it.each(overrides)(
      'sends messages into a %s override Session',
      async (_case, scenario) => {
        const { override } = scenario;
        await mountOverride(scenario, {
          streamHandler: ({ agent_id: agentId, session_id: sessionId }) => {
            if (
              agentId === override.agentId &&
              sessionId === override.sessionId
            ) {
              return runningRun('child-run-continue');
            }
            throw new Error(
              `Unexpected stream target: ${agentId}/${sessionId}`,
            );
          },
        });

        sendComposerMessage('Continue child work');
        await waitForCondition(() => rpcCalls('chat.stream').length === 1);

        expect(rpcCalls('chat.stream')).toEqual([
          {
            agent_id: override.agentId,
            session_id: override.sessionId,
            content: 'Continue child work',
          },
        ]);
        expect(selectedPersonalAgentName()).toBe(scenario.childAgentName);
        await waitForCondition(
          () => subscribeRunEventsMock.mock.calls.length === 1,
        );
        expect(subscribeRunEventsMock).toHaveBeenCalledWith(
          '/api/runs/child-run-continue/events',
          expect.any(Object),
          { afterSequence: 0 },
        );
      },
    );

    it('keeps the displayed override Session and its stream on a roster refresh', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({
          activeRuns: { 'sub-session-1': runningRun('active-sub-run') },
        }),
      );
      const parent = createChatViewParentHarness();
      await chat.mountChat(
        parent.props(['agents'], {
          sharedAgents: [createAgent()],
          sharedSelectedAgentId: 'alpha',
          pendingSessionNavigation: {
            agentId: 'alpha',
            sessionId: 'sub-session-1',
            subAgent: true,
          },
        }),
        { ready: null },
      );
      await waitForCondition(
        () => subscribeRunEventsMock.mock.calls.length === 1,
      );
      const subscription = subscribeRunEventsMock.mock.results[0].value;
      const currentReads = historyReads('session-1');

      // An Agent roster refresh (any Agent CRUD anywhere) must not steal the
      // viewed Session's display, its live subscription, or the composer.
      parent.bumpAgentsRefreshToken();
      flushSync();
      await waitForCondition(() => rpcCalls('agent.list').length >= 2);

      expect(historyReads('session-1')).toBe(currentReads);
      expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
      expect(subscription.close).not.toHaveBeenCalled();
      expect(document.body.textContent).toContain(SUB_AGENT_NOTICE());
      expect(composerInput().disabled).toBe(false);
    });

    it('returns through nested sub-agent Sessions to their parents with the Agent selection aligned', async () => {
      const agents = [
        createAgent({ current_session_id: 'root-session' }),
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
        'alpha::root-session': { id: 'root-session', title: 'Root planning' },
      });
      rpcMock.mockImplementation(
        createChatRpcMock({
          agents,
          sessionMessages: {
            'root-session': [message('root-reply', 'Root history')],
            'child-session': [message('child-reply', 'Child history')],
            'grandchild-session': [
              message('grandchild-reply', 'Grandchild history'),
            ],
          },
        }),
      );
      await chat.mountChat(
        {
          sharedAgents: agents,
          sharedSelectedAgentId: 'alpha',
          pendingSessionNavigation: {
            agentId: 'gamma',
            sessionId: 'grandchild-session',
            subAgent: true,
          },
        },
        { ready: 'Grandchild history' },
      );
      await waitForCondition(() => Boolean(returnToParentButton()));
      expect(selectedPersonalAgentName()).toBe('Gamma');

      // The notice button returns to the parent sub-agent Session.
      returnToParentButton().click();
      flushSync();
      await waitForCondition(
        () =>
          document.body.textContent.includes('Child history') &&
          Boolean(returnToParentButton()),
      );
      expect(document.body.textContent).toContain(SUB_AGENT_NOTICE());
      expect(selectedPersonalAgentName()).toBe('Beta');

      // Session info links the parent by its title; it opens as an ordinary
      // Session without a return notice.
      await openSessionInfoLink('.chat-activity__parent-link', 'Root planning');
      await waitForCondition(
        () =>
          document.body.textContent.includes('Root history') &&
          !document.body.textContent.includes(SUB_AGENT_NOTICE()),
      );
      expect(rpcMock).toHaveBeenCalledWith('chat.history', {
        agent_id: 'alpha',
        session_id: 'root-session',
        limit: 100,
      });
      expect(returnToParentButton()).toBeFalsy();
      expect(returnToCurrentButton()).toBeFalsy();
      expect(selectedPersonalAgentName()).toBe('Alpha');
    });
  });

  describe('Librarian Session', () => {
    it('opens a Session of the hidden Librarian like any Agent and continues it', async () => {
      const baseRpc = createChatRpcMock({
        contextUsage: { tokens: 32768, estimated: false },
        sessionMessages: {
          'lib-1': [message('lib-summary', 'Merged deploy notes')],
        },
        streamHandler: ({ agent_id: agentId, session_id: sessionId }) => {
          if (agentId === 'librarian' && sessionId === 'lib-1') {
            return runningRun('librarian-continue');
          }
          throw new Error(`Unexpected stream target: ${agentId}/${sessionId}`);
        },
      });
      rpcMock.mockImplementation(async (method, params) =>
        method === 'agent.get' && params.id === 'librarian'
          ? createAgent({
              id: 'librarian',
              name: t('librarian.name'),
              builtin: 'librarian',
              context_window: 131072,
            })
          : baseRpc(method, params),
      );
      await chat.mountChat(
        {
          sharedAgents: [createAgent()],
          sharedSelectedAgentId: 'alpha',
          pendingSessionNavigation: {
            agentId: 'librarian',
            sessionId: 'lib-1',
            subAgent: false,
          },
        },
        { ready: 'Merged deploy notes' },
      );

      // The Librarian is not in the Agent roster; the picker names it.
      expect(rpcMock).toHaveBeenCalledWith('chat.history', {
        agent_id: 'librarian',
        session_id: 'lib-1',
        limit: 100,
      });
      expect(
        document.querySelector('.chat-header__agent-picker').textContent.trim(),
      ).toBe(t('librarian.name'));
      expect(composerInput().disabled).toBe(false);
      // Its own Agent payload gives the context window, so the context ring
      // shows the Session's fill like for a roster Agent.
      await waitForCondition(() =>
        Boolean(document.querySelector('.context-ring')),
      );
      const fill = document.querySelector('.context-ring__fill');
      expect(Number(fill.getAttribute('stroke-dashoffset'))).toBeCloseTo(
        2 * Math.PI * 6 * (1 - 32768 / 131072),
        1,
      );

      sendComposerMessage('Why did you merge deploy?');
      await waitForCondition(() => rpcCalls('chat.stream').length === 1);

      expect(rpcCalls('chat.stream')).toEqual([
        {
          agent_id: 'librarian',
          session_id: 'lib-1',
          content: 'Why did you merge deploy?',
        },
      ]);
    });
  });

  describe('Session info', () => {
    it('renders a restored Reflection and opens its Session', async () => {
      const baseRpc = createChatRpcMock({
        sessionMessages: {
          'review-session': [
            message('review-result', 'Restored review result'),
          ],
        },
      });
      rpcMock.mockImplementation(async (method, params) => {
        const result = await baseRpc(method, params);
        return method === 'chat.history' && params.session_id === 'session-1'
          ? {
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
            }
          : result;
      });
      await chat.mountChat();

      document.querySelector('.chat-activity__rail').click();
      await waitForCondition(() =>
        document.querySelector('.chat-activity__task-link'),
      );
      const reviewLink = document.querySelector('.chat-activity__task-link');
      expect(reviewLink.getAttribute('aria-label')).toContain('Completed');
      reviewLink.click();
      await waitForText('Restored review result');
    });

    it('opens the source Session from a Reflection Session link', async () => {
      serveSessions({
        'alpha::session-1': {
          id: 'session-1',
          run_kinds: ['reflection'],
          fork_source: {
            agent_id: 'alpha',
            session_id: 'source-session',
            project_id: null,
          },
        },
        'alpha::source-session': {
          id: 'source-session',
          title: 'Original research',
        },
      });
      rpcMock.mockImplementation(
        createChatRpcMock({
          sessionMessages: {
            'source-session': [
              message('source-reply', 'Original Session history'),
            ],
          },
        }),
      );
      await chat.mountChat();

      await openSessionInfoLink(
        '.chat-activity__parent-link',
        'Original research',
      );
      await waitForText('Original Session history');
      expect(rpcMock).toHaveBeenCalledWith('chat.history', {
        agent_id: 'alpha',
        session_id: 'source-session',
        limit: 100,
      });
    });
  });
});
