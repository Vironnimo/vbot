// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import { t } from '../../lib/i18n.js';
import {
  allAgentsTrigger,
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
  selectedAgentName,
  sendComposerMessage,
  serveProject,
  setInputValue,
  settle,
  setupChatViewTestSuite,
  subscribeRunEventsMock,
  testChatStateRefs,
  tick,
  waitForCondition,
  waitForText,
  sessionListButton,
} from './ChatView.support.js';
import { createChatViewParentHarness } from './ChatView.parent.support.svelte.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const SUB_AGENT_NOTICE = () => t('chat.subagentSessionNotice');
const REFLECTION_NOTICE = () => t('chat.reflectionSessionNotice');
const returnToCurrentButton = () =>
  findButtonByText(t('chat.returnToCurrentSession'));
const returnToParentButton = () =>
  findButtonByText(t('chat.returnToParentSession'));
const composerInput = () => document.querySelector('.msg-input');
const sessionsButton = () => sessionListButton();

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

async function openSessionInfoParent(name) {
  document.querySelector('.chat-header__info-toggle').click();
  flushSync();
  const link = () => document.querySelector('.chat-activity__parent-link');
  await waitForCondition(
    () =>
      link()
        ?.querySelector('.chat-activity__parent-name')
        .textContent.trim() === name,
  );
  link().click();
  flushSync();
}

describe('ChatView Sessions', () => {
  const chat = setupChatViewTestSuite();

  it('enables a draft immediately while the departed Agent History is still pending', async () => {
    let release;
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    const agents = [
      createAgent(),
      createAgent({ id: 'beta', name: 'Beta', current_session_id: '' }),
    ];
    const fallback = createChatRpcMock({ agents });
    rpcMock.mockImplementation((method, params) =>
      method === 'chat.timeline' ? pending : fallback(method, params),
    );
    const parent = createChatViewParentHarness();
    await chat.mountChat(parent.props(['agent'], { sharedAgents: agents }), {
      ready: null,
    });
    await waitForCondition(() => historyReads('session-1') > 0);
    expect(composerInput().disabled).toBe(true);
    await selectAgentFromPicker('Beta');
    await waitForCondition(() => selectedAgentName() === 'Beta');
    expect(composerInput().disabled).toBe(false);
    expect(findNewSessionButton().disabled).toBe(false);
    release({ messages: [] });
    await settle();
    expect(composerInput().disabled).toBe(false);
  });

  it.each(['alpha', 'beta@project'])(
    'keeps Beta/main when a deletion belongs to %s/main',
    async (owner) => {
      const beta = createAgent({
        id: 'beta',
        name: 'Beta',
        current_session_id: 'main',
      });
      const alpha = createAgent({ current_session_id: 'main' });
      rpcMock.mockImplementation(
        createChatRpcMock({
          agents: [alpha, beta],
          sessionMessages: { main: [message('hello', 'Hello')] },
        }),
      );
      const props = reactiveProps({ siblingSessionDeletion: null });
      await chat.mountChat({
        sharedAgents: [alpha, beta],
        sharedSelectedAgentId: 'beta',
        get siblingSessionDeletion() {
          return props.siblingSessionDeletion;
        },
      });
      props.siblingSessionDeletion = {
        agentAddress: owner,
        deletedSessionId: 'main',
        nextSessionId: 'landing',
        requestId: 1,
      };
      await settle();
      expect(selectedAgentName()).toBe('Beta');
      expect(historyReads('landing')).toBe(0);
      await sendComposerMessage('Still Beta');
      expect(rpcCalls('chat.stream').at(-1)).toMatchObject({
        agent_id: 'beta',
        session_id: 'main',
      });
    },
  );

  describe('Session information', () => {
    it('reads the change statistics and usage of the Session and its Sub-Agents while it is open', async () => {
      const answers = [
        { files: 1, added: 3, removed: 1, file_stats: [] },
        { files: 2, added: 5, removed: 1, file_stats: [] },
      ];
      const fallback = createChatRpcMock();
      const totals = (input_tokens) => ({ input_tokens, output_tokens: 10 });
      rpcMock.mockImplementation(async (method, params) => {
        if (method === 'session.change_stats') {
          return {
            change_stats:
              answers[rpcCalls('session.change_stats').length - 1] ?? null,
            subagents: 1,
          };
        }
        if (method === 'statistics.session_usage') {
          return {
            session: totals(4000),
            subagents: totals(3000),
            total: totals(7000),
            subagent_count: 1,
          };
        }
        if (method === 'subagent.tree') {
          return { subagents: [] };
        }
        return fallback(method, params);
      });
      const props = reactiveProps({ sessionsRefreshToken: 0 });
      await chat.mountChat(props);
      const statsText = () =>
        document.querySelector(
          '[data-card="changes"] .chat-activity__card-summary',
        )?.textContent ?? '';

      // Closed, the panel asks for nothing.
      expect(rpcCalls('session.change_stats')).toEqual([]);
      document.querySelector('.chat-header__info-toggle').click();
      flushSync();
      await waitForCondition(() => statsText().includes('+3'));
      const session = { agent_id: 'alpha', session_id: 'session-1' };
      expect(rpcCalls('session.change_stats')).toEqual([
        { ...session, include_subagents: true },
      ]);
      expect(rpcCalls('statistics.session_usage')).toEqual([session]);
      expect(rpcCalls('subagent.tree')).toEqual([session]);
      await waitForCondition(() =>
        document
          .querySelector('[data-card="usage"] .chat-activity__facts')
          ?.textContent.includes('7K'),
      );

      // A Sessions refresh (a Run ended somewhere) reads them again.
      props.sessionsRefreshToken = 1;
      flushSync();
      await waitForCondition(() => statsText().includes('+5'));
      expect(rpcCalls('session.change_stats')).toHaveLength(2);
    });
  });

  describe('New session', () => {
    const alphaSessionId = () =>
      testChatStateRefs[0].agents.find((agent) => agent.id === 'alpha')
        .current_session_id;

    it.each([
      ['keeps an already empty Session', { 'session-1': [] }, false],
      ['shows a draft in place of a Session', {}, true],
      ['shows a Project Agent draft in place of its Session', {}, true, true],
    ])(
      '%s on repeated clicks without a request and focuses its composer',
      async (_case, sessionMessages, draft, project) => {
        if (project) {
          serveProject();
          listedSessions('session-1');
        }
        rpcMock.mockImplementation(createChatRpcMock({ sessionMessages }));
        await chat.mountChat(project ? projectChatProps() : {}, {
          ready: null,
        });
        await waitForCondition(() => historyReads('session-1') > 0);
        await settle(2);
        const requests = rpcMock.mock.calls.length;

        findNewSessionButton().focus();
        findNewSessionButton().click();
        findNewSessionButton().click();

        await waitForCondition(
          () => document.activeElement === composerInput(),
        );
        await settle();
        // At most the draft's commands and Skills are read again: a draft
        // offers those of the Agent's default Project, not the Session's.
        expect(
          rpcMock.mock.calls
            .slice(requests)
            .filter(([method]) => method !== 'chat.commands'),
        ).toEqual([]);
        expect(composerInput().disabled).toBe(false);
        expect(document.body.textContent).not.toContain('Hello');
        if (!project) {
          expect(alphaSessionId()).toBe(draft ? '' : 'session-1');
        }
      },
    );

    it('creates the Session with the first draft send and continues there', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({
          streamHandler: (params) => ({
            ...runningRun(params.new_session ? 'run-one' : 'run-two'),
            session_id: params.session_id ?? 'created-alpha',
          }),
        }),
      );
      const parent = createChatViewParentHarness();
      const onSessionNavigation = vi.fn();
      const onAgentsChanged = vi.fn();
      await chat.mountChat(
        parent.props(['navigation'], { onSessionNavigation, onAgentsChanged }),
      );

      findNewSessionButton().click();
      await waitForCondition(() => alphaSessionId() === '');
      expect(onSessionNavigation).toHaveBeenLastCalledWith(
        { agentId: 'alpha', sessionId: '', subAgent: false },
        { replace: false },
      );
      sendComposerMessage('First message');

      await waitForCondition(() => alphaSessionId() === 'created-alpha');
      expect(rpcCalls('chat.stream')).toEqual([
        {
          agent_id: 'alpha',
          new_session: {
            working_project_id: null,
            agent_overrides: {
              model: 'openrouter/anthropic/claude-sonnet-4',
              thinking_effort_default: true,
            },
          },
          content: 'First message',
        },
      ]);
      // The created Session replaces the draft's history entry.
      expect(onSessionNavigation).toHaveBeenLastCalledWith(
        { agentId: 'alpha', sessionId: 'created-alpha', subAgent: false },
        { replace: true },
      );
      expect(
        onAgentsChanged.mock.calls
          .at(-1)[0]
          .find((agent) => agent.id === 'alpha').current_session_id,
      ).toBe('created-alpha');
      expect(subscribeRunEventsMock).toHaveBeenCalledWith(
        '/api/runs/run-one/timeline?child_limit=100',
        expect.any(Object),
        {
          eventTypes: ['timeline_snapshot', 'timeline_update'],
          closeOnTerminal: false,
        },
      );
      await waitForCondition(() => composerInput().value === '');

      sendComposerMessage('Second message');
      await waitForCondition(() => rpcCalls('chat.stream').length === 2);
      expect(rpcCalls('chat.stream')[1]).toEqual({
        agent_id: 'alpha',
        session_id: 'created-alpha',
        content: 'Second message',
      });
    });

    it('starts a Session in a Project of the by-Project Session list without changing the default Project', async () => {
      const chatRpc = createChatRpcMock({
        streamHandler: (params) => ({
          ...runningRun('run-one'),
          session_id: params.session_id ?? 'created-alpha',
        }),
      });
      rpcMock.mockImplementation(async (method, params) =>
        method === 'session.project_groups'
          ? {
              groups: [
                {
                  project_id: 'vbot',
                  sessions: [],
                  next_cursor: null,
                  total_count: 0,
                },
                {
                  project_id: null,
                  sessions: [{ id: 'session-1', agent_address: 'alpha' }],
                  next_cursor: null,
                  total_count: 1,
                },
              ],
            }
          : chatRpc(method, params),
      );
      await chat.mountChat({
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        onSessionFiltersChange: vi.fn(),
      });

      sessionsButton().click();
      flushSync();
      document.querySelector('.session-drawer__by-project').click();
      flushSync();
      const newInVbot = () =>
        document.querySelector(
          `.session-group__new[aria-label="${t('sessions.projects.newSession', { project: 'vBot' })}"]`,
        );
      await waitForCondition(() => Boolean(newInVbot()));
      newInVbot().click();
      await waitForCondition(() => alphaSessionId() === '');
      sendComposerMessage('First message');

      await waitForCondition(() => alphaSessionId() === 'created-alpha');
      expect(rpcCalls('chat.stream')[0]).toMatchObject({
        agent_id: 'alpha',
        new_session: { working_project_id: 'vbot' },
      });
      expect(rpcCalls('agent.update')).toEqual([]);
    });

    it('keeps the composer text of a left empty Session and of the draft', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({ sessionMessages: { 'session-1': [] } }),
      );
      listedSessions({
        id: 'session-1',
        title: 'Draft home',
        created_at: '2026-05-09T00:00:00+00:00',
        last_active_at: '2026-05-09T00:00:00+00:00',
      });
      await chat.mountChat({}, { ready: null });
      await waitForCondition(() => historyReads('session-1') > 0);

      const input = composerInput();
      setInputValue(input, 'unfinished Session text');
      flushSync();
      findNewSessionButton().click();
      await waitForCondition(() => alphaSessionId() === '');
      expect(input.value).toBe('');
      setInputValue(input, 'unfinished draft text');
      flushSync();

      await openFromDrawer('Draft home');
      await waitForCondition(() => input.value === 'unfinished Session text');
      findNewSessionButton().click();
      await waitForCondition(() => input.value === 'unfinished draft text');
      expect(rpcCalls('chat.stream')).toEqual([]);
      setInputValue(input, '');
      flushSync();
    });

    it.each([
      ['an Agent switch', 'agent'],
      ['a same-Agent drawer Session', 'session'],
    ])(
      'adopts a delayed first draft send without replacing %s or its focus',
      async (_case, scenario) => {
        const agents = [
          createAgent(),
          createAgent({
            id: 'beta',
            name: 'Beta',
            current_session_id: 'session-beta',
          }),
        ];
        listedSessions({
          id: 'session-2',
          title: 'Older topic',
          created_at: '2026-05-09T00:00:00+00:00',
          last_active_at: '2026-05-09T00:00:00+00:00',
        });
        let resolveSend;
        rpcMock.mockImplementation(
          createChatRpcMock({
            agents,
            sessionMessages: {
              'session-beta': [message('beta-reply', 'Beta session reply')],
              'session-2': [message('older-reply', 'Older session reply')],
              'created-alpha': [
                message('created-reply', 'Created session reply'),
              ],
            },
            streamHandler: () =>
              new Promise((resolve) => {
                resolveSend = () =>
                  resolve({
                    ...runningRun('run-new'),
                    session_id: 'created-alpha',
                  });
              }),
          }),
        );
        const parent = createChatViewParentHarness();
        await chat.mountChat(parent.props(['agent'], { sharedAgents: agents }));

        findNewSessionButton().click();
        await waitForCondition(() => alphaSessionId() === '');
        sendComposerMessage('Delayed message');
        await waitForCondition(() => Boolean(resolveSend));
        const expectedText =
          scenario === 'agent' ? 'Beta session reply' : 'Older session reply';
        if (scenario === 'agent') {
          await selectAgentFromPicker('Beta');
        } else {
          await openFromDrawer('Older topic');
        }
        await waitForText(expectedText);
        const focusTarget = document.createElement('button');
        document.body.append(focusTarget);
        focusTarget.focus();

        resolveSend();
        await waitForCondition(() => alphaSessionId() === 'created-alpha');
        await settle(2);

        expect(document.body.textContent).toContain(expectedText);
        expect(document.activeElement).toBe(focusTarget);
        expect(historyReads('created-alpha')).toBe(0);
        expect(subscribeRunEventsMock).not.toHaveBeenCalled();

        // A later return to Alpha opens the created Session.
        await selectAgentFromPicker('Alpha');
        await waitForText('Created session reply');
      },
    );

    it('starts a Run in a new Session while the previous Session Run remains active', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({
          activeRuns: { 'session-1': runningRun('run-one') },
          streamHandler: () => ({
            ...runningRun('run-two'),
            session_id: 'created-alpha',
          }),
        }),
      );
      await chat.mountChat();
      await waitForCondition(() => Boolean(findCancelRunButton()));

      expect(findNewSessionButton().disabled).toBe(false);
      findNewSessionButton().click();
      await waitForCondition(() => !findCancelRunButton());
      sendComposerMessage('Run in parallel');
      await waitForCondition(() => alphaSessionId() === 'created-alpha');

      const sessions = testChatStateRefs[0].sessions;
      expect(sessions['alpha::session-1'].currentRun?.runId).toBe('run-one');
      expect(sessions['alpha::created-alpha'].currentRun?.runId).toBe(
        'run-two',
      );
      for (const key of ['alpha::session-1', 'alpha::created-alpha']) {
        expect(sessions[key].status).toBe('running');
      }
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
          expect(rpcMock).toHaveBeenCalledWith('chat.timeline', {
            agent_id: 'alpha',
            session_id: 'session-2',
            limit: 100,
            child_limit: 100,
          });

          findNewSessionButton().click();
          await waitForCondition(
            () =>
              document.activeElement === composerInput() &&
              !document.body.textContent.includes('Second session reply'),
          );
        } finally {
          window.matchMedia = originalMatchMedia;
        }
      },
    );

    it.each([
      'uninterrupted',
      'programmatic focus',
      'pointer interaction',
      'keyboard interaction',
      'area hidden and reopened',
      'area disabled and reenabled',
      'browser navigation away and back',
    ])('honors pending Session focus ownership after %s', async (scenario) => {
      const history = Promise.withResolvers();
      let pending = true;
      const base = createChatRpcMock({
        sessionMessages: {
          'session-2': [message('assistant-two', 'Second session reply')],
        },
      });
      rpcMock.mockImplementation(async (method, params) => {
        if (
          pending &&
          method === 'chat.timeline' &&
          params.session_id === 'session-2'
        ) {
          pending = false;
          await history.promise;
        }
        return base(method, params);
      });
      listedSessions({ id: 'session-2', title: 'Second topic' });
      const props = reactiveProps({ active: true, interactive: true });
      await chat.mountChat(props);
      await openFromDrawer('Second topic');
      await waitForCondition(() => historyReads('session-2') === 1);

      if (scenario === 'programmatic focus') {
        // A dialog may focus its control without a preceding pointer event.
        const control = document.createElement('button');
        document.body.append(control);
        control.focus();
      } else if (scenario === 'pointer interaction') {
        document.body.dispatchEvent(
          new Event('pointerdown', { bubbles: true }),
        );
      } else if (scenario === 'keyboard interaction') {
        document.body.dispatchEvent(
          new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }),
        );
      } else if (scenario.startsWith('area')) {
        const prop = scenario.includes('hidden') ? 'active' : 'interactive';
        props[prop] = false;
        flushSync();
        props[prop] = true;
        flushSync();
      } else if (scenario === 'browser navigation away and back') {
        // The final displayed key matches the still pending navigation, but
        // these two passive navigations have retired its deliberate focus.
        for (const sessionId of ['session-1', 'session-2']) {
          props.pendingSessionNavigation = {
            agentId: 'alpha',
            sessionId,
            subAgent: false,
            requestId: `restore-${sessionId}`,
          };
          flushSync();
          await waitForText(
            sessionId === 'session-1' ? 'Hello' : 'Second session reply',
          );
        }
      }
      const previousFocus = document.activeElement;
      history.resolve();
      await waitForText('Second session reply');
      await settle(2);

      expect(document.activeElement).toBe(
        scenario === 'uninterrupted' ? composerInput() : previousFocus,
      );
    });

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

    it.each(['click', 'keyboard'])(
      'focuses after a user Agent switch by %s but not after a passive Agent update',
      async (selection) => {
        const agents = [
          createAgent(),
          ...Array.from({ length: 4 }, (_, index) =>
            createAgent({ id: `other-${index}`, name: `Other ${index}` }),
          ),
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

        if (selection === 'keyboard') {
          allAgentsTrigger().click();
          await waitForCondition(() =>
            Boolean(document.querySelector('.s-dropdown-search input')),
          );
          const search = document.querySelector('.s-dropdown-search input');
          setInputValue(search, 'Beta');
          flushSync();
          search.dispatchEvent(
            new KeyboardEvent('keydown', {
              key: 'Enter',
              bubbles: true,
              cancelable: true,
            }),
          );
        } else {
          await selectAgentFromPicker('Beta');
        }
        await waitForText('Beta session reply');
        await waitForCondition(
          () => document.activeElement === composerInput(),
        );

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
      },
    );

    it('does not switch the viewed conversation on a Sessions refresh', async () => {
      rpcMock.mockImplementation(createChatRpcMock());
      const parent = createChatViewParentHarness();
      await chat.mountChat(
        parent.props(['sessions'], {
          sharedAgents: [createAgent()],
          sharedSelectedAgentId: 'alpha',
        }),
      );
      const historyCallsBefore = rpcCalls('chat.timeline').length;

      // A Sessions signal refreshes the Session list only ("stay put").
      parent.bumpSessionsRefreshToken();
      flushSync();

      expect(rpcCalls('chat.timeline')).toHaveLength(historyCallsBefore);
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
      expect(rpcCalls('chat.timeline')).toEqual([
        {
          agent_id: 'alpha',
          session_id: 'session-1',
          limit: 100,
          child_limit: 100,
        },
        {
          agent_id: 'alpha',
          session_id: 'session-1',
          limit: 50,
          child_limit: 100,
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
          method === 'chat.timeline' &&
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

        expect(rpcMock).toHaveBeenCalledWith('chat.timeline', {
          agent_id: override.agentId,
          session_id: override.sessionId,
          limit: 100,
          child_limit: 100,
        });
        expect(selectedAgentName()).toBe(scenario.childAgentName);
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
        expect(rpcMock).toHaveBeenCalledWith('chat.timeline', {
          agent_id: current.agentId,
          session_id: current.sessionId,
          limit: 100,
          child_limit: 100,
        });
        expect(historyReads('beta-current-session')).toBe(0);
        expect(selectedAgentName()).toBe('Alpha');
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
        expect(selectedAgentName()).toBe(scenario.childAgentName);
        await waitForCondition(
          () => subscribeRunEventsMock.mock.calls.length === 1,
        );
        expect(subscribeRunEventsMock).toHaveBeenCalledWith(
          '/api/runs/child-run-continue/timeline?child_limit=100',
          expect.any(Object),
          {
            eventTypes: ['timeline_snapshot', 'timeline_update'],
            closeOnTerminal: false,
          },
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
      expect(selectedAgentName()).toBe('Gamma');

      // The notice button returns to the parent sub-agent Session.
      returnToParentButton().click();
      flushSync();
      await waitForCondition(
        () =>
          document.body.textContent.includes('Child history') &&
          Boolean(returnToParentButton()),
      );
      expect(document.body.textContent).toContain(SUB_AGENT_NOTICE());
      expect(selectedAgentName()).toBe('Beta');

      // Session info links the parent by its title; it opens as an ordinary
      // Session without a return notice.
      await openSessionInfoParent('Root planning');
      await waitForCondition(
        () =>
          document.body.textContent.includes('Root history') &&
          !document.body.textContent.includes(SUB_AGENT_NOTICE()),
      );
      expect(rpcMock).toHaveBeenCalledWith('chat.timeline', {
        agent_id: 'alpha',
        session_id: 'root-session',
        limit: 100,
        child_limit: 100,
      });
      expect(returnToParentButton()).toBeFalsy();
      expect(returnToCurrentButton()).toBeFalsy();
      expect(selectedAgentName()).toBe('Alpha');
    });
  });

  describe('Librarian Session', () => {
    it('opens a Session of the hidden Librarian like any Session and continues it', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({
          contextUsage: { tokens: 32768, estimated: false },
          contextWindow: 131072,
          sessionMessages: {
            'lib-1': [message('lib-summary', 'Merged deploy notes')],
          },
          streamHandler: ({ agent_id: agentId, session_id: sessionId }) => {
            if (agentId === 'librarian' && sessionId === 'lib-1') {
              return runningRun('librarian-continue');
            }
            throw new Error(
              `Unexpected stream target: ${agentId}/${sessionId}`,
            );
          },
        }),
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

      // The Librarian is not in the Agent roster; the Agent bar names it.
      expect(rpcMock).toHaveBeenCalledWith('chat.timeline', {
        agent_id: 'librarian',
        session_id: 'lib-1',
        limit: 100,
        child_limit: 100,
      });
      expect(selectedAgentName()).toBe(t('librarian.name'));
      expect(composerInput().disabled).toBe(false);
      // The Session's Context names its window, so the ring shows without the
      // hidden Agent in the roster.
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

  describe('Live call Session', () => {
    it.each([
      ['live-voice', 'live.agent.voice'],
      ['live-backend', 'live.agent.backend'],
    ])(
      'shows a Session of %s for reading, without a composer',
      async (agentId, nameKey) => {
        rpcMock.mockImplementation(
          createChatRpcMock({
            sessionMessages: {
              'call-1': [message('call-summary', 'Started the deploy')],
            },
          }),
        );
        await chat.mountChat(
          {
            sharedAgents: [createAgent()],
            sharedSelectedAgentId: 'alpha',
            pendingSessionNavigation: {
              agentId,
              sessionId: 'call-1',
              subAgent: false,
            },
          },
          { ready: 'Started the deploy' },
        );

        expect(selectedAgentName()).toBe(t(nameKey));
        // Only the call writes to its Sessions.
        expect(composerInput()).toBeNull();
        expect(document.body.textContent).toContain(t('chat.liveCallNotice'));
      },
    );
  });

  describe('Session info', () => {
    it.each([
      [{}, 'alpha'],
      [{ agent_id: 'beta', project_id: null }, 'beta'],
      [{ agent_id: 'beta', project_id: 'project' }, 'beta@project'],
    ])(
      'opens a restored Reflection with owner %j at %s',
      async (owner, address) => {
        const baseRpc = createChatRpcMock({
          sessionMessages: {
            'review-session': [
              message('review-result', 'Restored review result'),
            ],
          },
        });
        rpcMock.mockImplementation(async (method, params) => {
          const result = await baseRpc(method, params);
          return method === 'chat.timeline' && params.session_id === 'session-1'
            ? {
                ...result,
                reflection_runs: [
                  {
                    run_id: 'review-run',
                    ...owner,
                    session_id: 'review-session',
                    run_kind: 'memory_reflection',
                    status: 'completed',
                    started_at: '2026-09-05T10:00:00Z',
                  },
                ],
              }
            : result;
        });
        await chat.mountChat({
          sharedAgents: [
            createAgent(),
            createAgent({ id: 'beta', name: 'Beta' }),
          ],
        });

        document.querySelector('.chat-header__info-toggle').click();
        // Finished work waits folded.
        await waitForCondition(() =>
          document.querySelector('.chat-activity__finished-toggle'),
        );
        document.querySelector('.chat-activity__finished-toggle').click();
        await waitForCondition(() =>
          document.querySelector('.chat-activity__task-link'),
        );
        const reviewLink = document.querySelector('.chat-activity__task-link');
        expect(reviewLink.getAttribute('aria-label')).toContain('Completed');
        reviewLink.click();
        await waitForText('Restored review result');
        expect(rpcMock).toHaveBeenCalledWith('chat.timeline', {
          agent_id: address,
          session_id: 'review-session',
          limit: 100,
          child_limit: 100,
        });
      },
    );

    it('returns from a Reflection Session to its source Session', async () => {
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

      await waitForText(REFLECTION_NOTICE());
      const returnButton = () =>
        findButtonByText(t('chat.returnToMainSession'));
      await waitForCondition(() => Boolean(returnButton()));
      returnButton().click();
      flushSync();
      await waitForText('Original Session history');
      await waitForCondition(
        () => !document.body.textContent.includes(REFLECTION_NOTICE()),
      );
      expect(rpcMock).toHaveBeenCalledWith('chat.timeline', {
        agent_id: 'alpha',
        session_id: 'source-session',
        limit: 100,
        child_limit: 100,
      });
    });
  });

  describe('Identity Agent rename', () => {
    it.each([
      ['the first Agent on an older Session', 'alpha', 'gamma', 'Older'],
      ['a later Agent on its current Session', 'beta', 'delta', 'Beta'],
    ])(
      'keeps %s with its transcript',
      async (_case, oldId, newId, shownText) => {
        let agents = [
          createAgent(),
          createAgent({
            id: 'beta',
            name: 'Beta',
            current_session_id: 'session-beta',
          }),
        ];
        listedSessions({
          id: 'session-2',
          title: 'Older topic',
          created_at: '2026-05-09T00:00:00+00:00',
          last_active_at: '2026-05-09T00:00:00+00:00',
        });
        const baseRpc = createChatRpcMock({
          sessionMessages: {
            'session-beta': [message('beta-reply', 'Beta session reply')],
            'session-2': [message('older-reply', 'Older session reply')],
          },
        });
        rpcMock.mockImplementation(async (method, params) =>
          method === 'agent.list' ? { agents } : baseRpc(method, params),
        );
        const renameListeners = [];
        const props = reactiveProps({
          sharedAgents: agents,
          sharedSelectedAgentId: oldId,
          agentsRefreshToken: 0,
          subscribeAgentRenames: (listener) => {
            renameListeners.push(listener);
            return () =>
              renameListeners.splice(renameListeners.indexOf(listener), 1);
          },
        });
        await chat.mountChat(props, { ready: null });
        if (oldId === 'alpha') {
          await waitForText('Hello');
          await openFromDrawer('Older topic');
        }
        await waitForText(`${shownText} session reply`);
        const chatState = testChatStateRefs[0];
        const shownSessionId = chatState.agents.find(
          (agent) => agent.id === oldId,
        ).current_session_id;
        const oldName = selectedAgentName();

        // App's order: the rename mapping reaches Chat synchronously, then the
        // shared selection follows and the roster reloads under the new id.
        for (const listener of renameListeners) listener(oldId, newId);
        agents = agents.map((agent) =>
          agent.id === oldId ? { ...agent, id: newId } : agent,
        );
        props.sharedSelectedAgentId = newId;
        props.sharedAgents = agents;
        props.agentsRefreshToken += 1;
        await settle(3);

        expect(document.body.textContent).toContain(
          `${shownText} session reply`,
        );
        expect(selectedAgentName()).toBe(oldName);
        expect(chatState.selectedAgentId).toBe(newId);
        expect(
          chatState.agents.find((agent) => agent.id === newId)
            .current_session_id,
        ).toBe(shownSessionId);
        const keys = Object.keys(chatState.sessions);
        expect(keys.filter((key) => key.startsWith(`${oldId}::`))).toEqual([]);
        expect(chatState.sessions[`${newId}::${shownSessionId}`].agentId).toBe(
          newId,
        );
      },
    );
  });
});
