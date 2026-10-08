// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  applyConnectionSnapshotMock,
  rpcMock,
  createChatRpcMock,
  createAgent,
  getSessionMock,
  listSessionsMock,
  message,
  selectAgentFromPicker,
  setInputValue,
  flushSync,
  waitForCondition,
  setupChatViewTestSuite,
  testChatStateRefs,
} from './ChatView.support.js';
import {
  renameComposerAgent,
  resetComposerMemory,
} from '../../lib/composerMemory.js';
import ChatWorkspace from '../ChatWorkspace.svelte';
import { reactiveProps } from './reactiveProps.support.svelte.js';

describe('ChatWorkspace', () => {
  const harness = setupChatViewTestSuite();
  beforeEach(() => {
    localStorage.clear();
    resetComposerMemory();
  });
  afterEach(() => {
    window.history.replaceState({}, '', '/');
    delete window.pywebview;
  });

  const pane = (index) =>
    document.querySelectorAll('.chat-workspace__pane')[index];
  const button = (root, label) =>
    Array.from(
      root.querySelectorAll('.chat-workspace__body:not([hidden]) button'),
    ).find(
      (element) =>
        (element.getAttribute('aria-label') || element.textContent.trim()) ===
        label,
    );

  function action(index, label) {
    button(pane(index), label).click();
    flushSync();
  }

  function mockPreviewOpening() {
    const baseRpc = rpcMock.getMockImplementation();
    rpcMock.mockImplementation((method, params) =>
      method === 'file.preview_open'
        ? Promise.resolve({
            token: 'site-token',
            url: '/api/preview-assets/site-token/index.html',
            source: '/site/index.html',
            root: '/site',
            filename: 'index.html',
            revision: 'one',
          })
        : baseRpc(method, params),
    );
  }

  async function start(
    split = true,
    content = 'Hello! Your website: [index.html](/api/files/file-token)',
    onToast = () => {},
  ) {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-1': [
            {
              id: 'website-answer',
              role: 'assistant',
              content,
            },
          ],
          'session-2': [
            {
              id: 'second-answer',
              role: 'assistant',
              content: 'Second conversation sentinel',
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
      ],
    });
    harness.mount(
      {
        target: document.body,
        props: {
          sharedAgents: [createAgent()],
          sharedSelectedAgentId: 'alpha',
          onToast,
        },
      },
      ChatWorkspace,
    );
    await waitForCondition(() => pane(0)?.textContent.includes('Hello'));
    if (!split) return;
    action(0, 'Split view');
    await waitForCondition(() => button(pane(1), 'Session list'));
    action(1, 'Session list');
    await waitForCondition(() => pane(1)?.textContent.includes('Second topic'));
    Array.from(pane(1).querySelectorAll('button'))
      .find((el) => el.textContent.includes('Second topic'))
      .click();
    await waitForCondition(() =>
      pane(1)?.textContent.includes('Second conversation sentinel'),
    );
  }

  it.each([0, 1])(
    'keeps the Session list open after selecting a Session in area %i',
    async (index) => {
      await start(false);
      if (index === 1) {
        action(0, 'Split view');
        await waitForCondition(() => button(pane(1), 'Session list'));
      }
      action(index, 'Session list');
      await waitForCondition(() =>
        pane(index).querySelector('.session-row__select'),
      );
      const drawer = pane(index).querySelector('.session-drawer');
      drawer.querySelector('.session-row__select').click();
      await waitForCondition(() =>
        pane(index).textContent.includes('Second conversation sentinel'),
      );
      expect(pane(index).querySelector('.session-drawer')).toBe(drawer);
      const selectedRow = drawer.querySelector('.session-row__select--active');
      expect(selectedRow).not.toBeNull();
      selectedRow.click();
      flushSync();
      expect(pane(index).querySelector('.session-drawer')).toBe(drawer);
      action(index, 'Session list');
      expect(pane(index).querySelector('.session-drawer')).toBeNull();
    },
  );

  it.each(['split', 'preview'])(
    'starts a new Chat with its list closed and copies Session filters through %s',
    async (route) => {
      await start(false);
      if (route === 'preview') {
        mockPreviewOpening();
        pane(0).querySelector('.msg-markdown a').click();
        await waitForCondition(() => pane(1)?.querySelector('iframe'));
        expect(testChatStateRefs).toHaveLength(1);
      }
      action(0, 'Session list');
      const firstDrawer = pane(0).querySelector('.session-drawer');
      firstDrawer.querySelector('button[aria-label="All agents"]').click();
      flushSync();
      firstDrawer.querySelector('.session-drawer__filter-trigger').click();
      flushSync();
      const switches = () => [
        ...document.querySelectorAll(
          '.session-drawer__filter-menu [role="switch"]',
        ),
      ];
      expect(switches()).toHaveLength(5);
      for (const toggle of switches()) {
        toggle.click();
        flushSync();
      }
      firstDrawer.querySelector('.session-drawer__filter-trigger').click();
      flushSync();
      if (route === 'split') action(0, 'Split view');
      else action(1, 'Back to chat');
      await waitForCondition(() => button(pane(1), 'Session list'));
      expect(pane(1).querySelector('.session-drawer')).toBeNull();
      expect(pane(0).querySelector('.session-drawer')).toBe(firstDrawer);

      listSessionsMock.mockClear();
      action(1, 'Session list');
      await waitForCondition(() => listSessionsMock.mock.calls.length > 0);
      expect(listSessionsMock).toHaveBeenLastCalledWith(
        ['alpha'],
        expect.objectContaining({
          includeSubagents: true,
          includeMemoryReflections: true,
          includeSkillReflections: true,
          includeCron: true,
          includeChannels: true,
        }),
      );
      const secondDrawer = pane(1).querySelector('.session-drawer');
      secondDrawer.querySelector('.session-drawer__filter-trigger').click();
      flushSync();
      expect(
        switches().map((toggle) => toggle.getAttribute('aria-checked')),
      ).toEqual(Array(5).fill('true'));
      // Subsequent choices remain local and survive list/area close and reopen.
      switches()[0].click();
      flushSync();
      action(1, 'Session list');
      action(1, 'Close area');
      action(0, 'Split view');
      expect(pane(1).querySelector('.session-drawer')).toBeNull();
      action(1, 'Session list');
      pane(1).querySelector('.session-drawer__filter-trigger').click();
      flushSync();
      expect(switches()[0].getAttribute('aria-checked')).toBe('false');
      action(1, 'Session list');
      action(0, 'Session list');
      action(0, 'Session list');
      pane(0).querySelector('.session-drawer__filter-trigger').click();
      flushSync();
      expect(
        switches().map((toggle) => toggle.getAttribute('aria-checked')),
      ).toEqual(Array(5).fill('true'));
    },
  );

  it('restores all Session filters independently after a fresh workspace mount', async () => {
    const switches = () => [
      ...document.querySelectorAll(
        '.session-drawer__filter-menu [role="switch"]',
      ),
    ];
    const openFilters = (index) => {
      action(index, 'Session list');
      pane(index).querySelector('.session-drawer__filter-trigger').click();
      flushSync();
    };
    await start(false);
    openFilters(0);
    button(pane(0), 'All agents').click();
    flushSync();
    for (const toggle of switches()) {
      toggle.click();
      flushSync();
    }
    action(0, 'Session list');
    action(0, 'Split view');
    await waitForCondition(() => button(pane(1), 'Session list'));
    openFilters(1);
    button(pane(1), 'All agents').click();
    flushSync();
    for (const toggle of switches()) {
      toggle.click();
      flushSync();
    }

    await harness.unmount();
    await start(false);
    listSessionsMock.mockClear();
    openFilters(0);
    expect(switches()).toHaveLength(5);
    expect(
      switches().map((toggle) => toggle.getAttribute('aria-checked')),
    ).toEqual(Array(5).fill('true'));
    await waitForCondition(() => listSessionsMock.mock.calls.length > 0);
    expect(listSessionsMock).toHaveBeenLastCalledWith(
      ['alpha'],
      expect.objectContaining({
        includeSubagents: true,
        includeMemoryReflections: true,
        includeSkillReflections: true,
        includeCron: true,
        includeChannels: true,
      }),
    );
    action(0, 'Session list');
    action(0, 'Split view');
    await waitForCondition(() => button(pane(1), 'Session list'));
    openFilters(1);
    expect(
      switches().map((toggle) => toggle.getAttribute('aria-checked')),
    ).toEqual(Array(5).fill('false'));
  });

  it('keeps two real Chat owners independent, retains drafts, and restores closed areas', async () => {
    await start();
    expect(pane(0).textContent).toContain('Hello');
    expect(pane(0).textContent).not.toContain('Second conversation sentinel');
    expect(pane(1).querySelector('.chat-workspace__toolbar')).toBeNull();
    expect(pane(1).querySelector('[role=tablist]')).toBeNull();
    const leftInput = pane(0).querySelector('.msg-input');
    const rightInput = pane(1).querySelector('.msg-input');
    setInputValue(leftInput, 'Left draft sentinel');
    setInputValue(rightInput, 'Right draft sentinel');
    flushSync();
    action(0, 'Close area');
    expect(pane(0).hidden).toBe(true);
    expect(pane(1).querySelector('.msg-input')).toBe(rightInput);
    // Focus moves to the remaining area.
    await waitForCondition(
      () => document.activeElement === button(pane(1), 'Split view'),
    );
    action(1, 'Split view');
    expect(pane(0).querySelector('.msg-input')).toBe(leftInput);
    expect(leftInput.value).toBe('Left draft sentinel');
    expect(rightInput.value).toBe('Right draft sentinel');
    expect(testChatStateRefs).toHaveLength(2);
    // A new Session in the second pane cannot re-aim the first pane's landing.
    button(pane(1), 'New session').click();
    await waitForCondition(
      () => testChatStateRefs[1].agents[0].current_session_id === '',
    );
    expect(testChatStateRefs[0].agents[0].current_session_id).toBe('session-1');
    expect(leftInput.value).toBe('Left draft sentinel');
  });

  it('routes messages to the concrete Session in each area', async () => {
    await start();
    for (const [index, content] of [
      [0, 'Left request'],
      [1, 'Right request'],
    ]) {
      setInputValue(pane(index).querySelector('.msg-input'), content);
      flushSync();
      pane(index).querySelector('.btn-primary.btn-icon').click();
      await waitForCondition(() =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.stream' && params.content === content,
        ),
      );
    }
    const requests = rpcMock.mock.calls
      .filter(([method]) => method === 'chat.stream')
      .map(([, params]) => params);
    expect(requests).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          session_id: 'session-1',
          content: 'Left request',
        }),
        expect.objectContaining({
          session_id: 'session-2',
          content: 'Right request',
        }),
      ]),
    );
  });

  it('keeps one composer for a duplicated Session and transfers its draft on close', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    harness.mount({ target: document.body }, ChatWorkspace);
    await waitForCondition(() => pane(0)?.querySelector('.msg-input'));
    const firstInput = pane(0).querySelector('.msg-input');
    setInputValue(firstInput, 'Shared Session draft');
    flushSync();
    action(0, 'Split view');
    await waitForCondition(() => testChatStateRefs.length === 2);
    expect(pane(1).querySelector('.msg-input')).toBeNull();
    action(0, 'Close area');
    await waitForCondition(() => pane(1).querySelector('.msg-input'));
    const secondInput = pane(1).querySelector('.msg-input');
    expect(secondInput.value).toBe('Shared Session draft');
    setInputValue(secondInput, 'Continued in the right');
    flushSync();
    action(1, 'Split view');
    await waitForCondition(() => pane(0).querySelector('.msg-input'));
    expect(pane(0).querySelector('.msg-input').value).toBe(
      'Continued in the right',
    );
    expect(pane(1).querySelector('.msg-input')).toBeNull();
  });

  it("keeps each area's draft of the same Agent apart and across navigation", async () => {
    await start();
    action(1, 'Session list');
    button(pane(1), 'New session').click();
    action(0, 'New session');
    await waitForCondition(() =>
      [0, 1].every(
        (index) =>
          testChatStateRefs[index].agents[0].current_session_id === '' &&
          pane(index).querySelector('.msg-input'),
      ),
    );
    const leftInput = pane(0).querySelector('.msg-input');
    const rightInput = pane(1).querySelector('.msg-input');
    setInputValue(leftInput, 'Left draft sentinel');
    setInputValue(rightInput, 'Right draft sentinel');
    flushSync();

    // The second area leaves its draft for a Session and comes back to it.
    action(1, 'Session list');
    await waitForCondition(() => pane(1)?.textContent.includes('Second topic'));
    Array.from(pane(1).querySelectorAll('button'))
      .find((el) => el.textContent.includes('Second topic'))
      .click();
    await waitForCondition(() => rightInput.value === '');
    button(pane(1), 'New session').click();
    await waitForCondition(() => rightInput.value === 'Right draft sentinel');

    expect(leftInput.value).toBe('Left draft sentinel');
    expect(
      rpcMock.mock.calls.filter(([method]) => method === 'chat.stream'),
    ).toEqual([]);
  });

  it('keeps both areas on their Agents, Sessions and drafts through Agent renames', async () => {
    let agents = [
      createAgent(),
      createAgent({
        id: 'beta',
        name: 'Beta',
        current_session_id: 'session-beta',
      }),
    ];
    const baseRpc = createChatRpcMock({
      sessionMessages: {
        'session-beta': [
          { id: 'beta-answer', role: 'assistant', content: 'Beta sentinel' },
        ],
      },
    });
    rpcMock.mockImplementation(async (method, params) =>
      method === 'agent.list' ? { agents } : baseRpc(method, params),
    );
    const renameListeners = [];
    const props = reactiveProps({
      sharedAgents: agents,
      sharedSelectedAgentId: 'alpha',
      agentsRefreshToken: 0,
      subscribeAgentRenames: (listener) => {
        renameListeners.push(listener);
        return () =>
          renameListeners.splice(renameListeners.indexOf(listener), 1);
      },
    });
    // What App does for a rename another window or the CLI made.
    function renameAgent(oldId, newId) {
      renameComposerAgent(oldId, newId);
      for (const listener of [...renameListeners]) listener(oldId, newId);
      if (props.sharedSelectedAgentId === oldId) {
        props.sharedSelectedAgentId = newId;
      }
      agents = agents.map((agent) =>
        agent.id === oldId ? { ...agent, id: newId } : agent,
      );
      props.sharedAgents = agents;
      props.agentsRefreshToken += 1;
      flushSync();
    }
    harness.mount({ target: document.body, props }, ChatWorkspace);
    await waitForCondition(() => pane(0)?.textContent.includes('Hello'));
    action(0, 'Split view');
    await selectAgentFromPicker('Beta', pane(1));
    await waitForCondition(() => pane(1).textContent.includes('Beta sentinel'));
    const inputs = [0, 1].map((index) =>
      pane(index).querySelector('.msg-input'),
    );
    setInputValue(inputs[0], 'Alpha draft sentinel');
    setInputValue(inputs[1], 'Beta draft sentinel');
    flushSync();

    renameAgent('beta', 'delta');
    renameAgent('alpha', 'gamma');
    await waitForCondition(() =>
      testChatStateRefs.every((state) =>
        state.agents.some((agent) => agent.id === 'gamma'),
      ),
    );

    expect(testChatStateRefs.map((state) => state.selectedAgentId)).toEqual([
      'gamma',
      'delta',
    ]);
    expect(pane(0).textContent).toContain('Hello');
    expect(pane(1).textContent).toContain('Beta sentinel');
    expect(
      [0, 1].map((index) => pane(index).querySelector('.msg-input').value),
    ).toEqual(['Alpha draft sentinel', 'Beta draft sentinel']);
    for (const state of testChatStateRefs) {
      expect(
        Object.keys(state.sessions).filter((key) =>
          /^(alpha|beta)::/.test(key),
        ),
      ).toEqual([]);
    }
  });

  it('starts a newly opened Chat area from the current Run state, not stale App buffers', async () => {
    const agents = [
      createAgent(),
      createAgent({ id: 'beta', name: 'Beta', current_session_id: 'b-1' }),
      createAgent({ id: 'gamma', name: 'Gamma', current_session_id: 'g-1' }),
    ];
    rpcMock.mockImplementation(createChatRpcMock({ agents }));
    applyConnectionSnapshotMock.mockImplementation(function (snapshot) {
      return this.applyConnectionSnapshot(snapshot);
    });
    const lifecycle = (type, runId, agentId, sessionId) => ({
      type,
      payload: {
        run_id: runId,
        agent_id: agentId,
        session_id: sessionId,
        run_event_type: type,
        run_event_sequence: 9,
        run_event_timestamp: '2026-09-22T10:00:00+00:00',
        ...(type === 'run_completed' ? { status: 'completed' } : {}),
      },
    });
    const betaRun = { run_id: 'R1', agent_id: 'beta', session_id: 'b-1' };
    const props = reactiveProps({
      // App connected while Beta's Run was active.
      connectionSnapshot: {
        type: 'connection_ready',
        replay_status: 'fresh',
        active_runs: [betaRun],
        queues: [],
      },
      activeRuns: [betaRun],
      runServerEvents: [],
    });
    harness.mount(
      {
        target: document.body,
        props: {
          sharedAgents: agents,
          sharedSelectedAgentId: 'alpha',
          get connectionSnapshot() {
            return props.connectionSnapshot;
          },
          get activeRuns() {
            return props.activeRuns;
          },
          get runServerEvents() {
            return props.runServerEvents;
          },
        },
      },
      ChatWorkspace,
    );
    // Each area's Agent bar shows the Agents' activity.
    const pillLabels = (index) =>
      Array.from(pane(index).querySelectorAll('button.agent-pill')).map(
        (pill) => pill.getAttribute('aria-label'),
      );
    await waitForCondition(() => pillLabels(0).includes('Beta: Running'));

    // Beta's Run ends; later traffic pushes its terminal event out of App's
    // bounded window, and Gamma's Run starts outside the retained window.
    props.runServerEvents = [lifecycle('run_completed', 'R1', 'beta', 'b-1')];
    props.activeRuns = [];
    await waitForCondition(() => !pillLabels(0).includes('Beta: Running'));
    props.runServerEvents = [];
    props.activeRuns = [
      { run_id: 'R2', agent_id: 'gamma', session_id: 'g-1', status: 'running' },
    ];

    action(0, 'Split view');
    await waitForCondition(() => pillLabels(1).includes('Gamma: Running'));

    expect(pillLabels(1)).not.toContain('Beta: Running');
  });

  describe('deleting the displayed current Session', () => {
    const rows = {
      'session-1': {
        id: 'session-1',
        title: 'First topic',
        created_at: '2026-05-10T00:00:00+00:00',
        last_active_at: '2026-05-10T02:00:00+00:00',
      },
      'session-2': {
        id: 'session-2',
        title: 'Second topic',
        created_at: '2026-05-10T00:00:00+00:00',
        last_active_at: '2026-05-10T01:00:00+00:00',
      },
    };
    let deleted;
    const serverDeletion = () => ({
      agentAddress: 'alpha',
      deletedSessionId: 'session-1',
      nextSessionId: 'session-2',
    });

    function mountDeletableWorkspace({
      beforeDeleteResponse,
      landing = 'session-2',
    } = {}) {
      deleted = false;
      const baseRpc = createChatRpcMock({
        sessionMessages: {
          'session-2': [
            {
              id: 'second-answer',
              role: 'assistant',
              content: 'Second conversation sentinel',
            },
          ],
        },
      });
      rpcMock.mockImplementation(async (method, params) => {
        if (method === 'session.delete') {
          deleted = true;
          await beforeDeleteResponse?.();
          return { ...params, next_session_id: landing };
        }
        if (method === 'agent.list') {
          return {
            agents: [
              createAgent({
                current_session_id: deleted ? (landing ?? '') : 'session-1',
              }),
            ],
          };
        }
        if (
          deleted &&
          method === 'chat.history' &&
          params.session_id === 'session-1'
        ) {
          throw new Error('Session not found: session-1');
        }
        return baseRpc(method, params);
      });
      listSessionsMock.mockImplementation(async () => ({
        sessions:
          deleted && !landing
            ? []
            : deleted
              ? [rows['session-2']]
              : [rows['session-1'], rows['session-2']],
      }));
      const props = reactiveProps({
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        agentsRefreshToken: 0,
        sessionDeletion: null,
        onSessionNavigation: vi.fn(),
      });
      harness.mount(
        {
          target: document.body,
          props: {
            get sharedAgents() {
              return props.sharedAgents;
            },
            get sharedSelectedAgentId() {
              return props.sharedSelectedAgentId;
            },
            get agentsRefreshToken() {
              return props.agentsRefreshToken;
            },
            get sessionDeletion() {
              return props.sessionDeletion;
            },
            get onSessionNavigation() {
              return props.onSessionNavigation;
            },
          },
        },
        ChatWorkspace,
      );
      return props;
    }

    async function deleteFromDrawer(index, title) {
      action(index, 'Session list');
      await waitForCondition(() =>
        Array.from(pane(index).querySelectorAll('.session-row')).some((row) =>
          row.textContent.includes(title),
        ),
      );
      Array.from(pane(index).querySelectorAll('.session-row'))
        .find((row) => row.textContent.includes(title))
        .querySelector('.session-row__menu-trigger')
        .click();
      flushSync();
      document.querySelector('.context-menu__item--danger').click();
      flushSync();
      Array.from(document.querySelectorAll('.modal-footer button'))
        .find((element) => element.textContent.trim() === 'Delete')
        .click();
      flushSync();
    }

    // Lets any (unwanted) History load triggered by the last action start.
    async function settle(ticks = 10) {
      for (let tick = 0; tick < ticks; tick += 1) {
        await new Promise((resolve) => setTimeout(resolve, 0));
        flushSync();
      }
    }

    const deletedHistoryReads = () =>
      rpcMock.mock.calls.filter(
        ([method, params]) =>
          method === 'chat.history' && params.session_id === 'session-1',
      ).length;

    it.each([
      ['the server landing', 'session-2'],
      ['a draft without a landing', null],
    ])(
      'lands on %s and never reopens the deleted Session',
      async (_case, landing) => {
        const props = mountDeletableWorkspace({ landing });
        const landed = () =>
          landing
            ? pane(0).textContent.includes('Second conversation sentinel')
            : testChatStateRefs[0].agents[0].current_session_id === '' &&
              !pane(0).textContent.includes('Hello');
        await waitForCondition(() => deletedHistoryReads() > 0);
        await deleteFromDrawer(0, 'First topic');
        await waitForCondition(landed);
        const readsAtDeletion = deletedHistoryReads();

        // App publishes rosters (refreshed and still-stale) and bumps the
        // token.
        props.sharedAgents = [
          createAgent({ current_session_id: landing ?? '' }),
        ];
        props.agentsRefreshToken = 1;
        flushSync();
        await waitForCondition(
          () =>
            rpcMock.mock.calls.filter(([method]) => method === 'agent.list')
              .length >= 2,
        );
        props.sharedAgents = [createAgent()];
        flushSync();

        // Selecting the same Agent returns to its current Session.
        await selectAgentFromPicker('Alpha', pane(0));
        await settle();

        expect(testChatStateRefs[0].agents[0].current_session_id).toBe(
          landing ?? '',
        );
        expect(deletedHistoryReads()).toBe(readsAtDeletion);
        expect(landed()).toBe(true);
        expect(pane(0).querySelector('.msg-input').disabled).toBe(false);
      },
    );

    it('releases the deleted Session in the other Chat area too', async () => {
      mountDeletableWorkspace();
      await waitForCondition(() => deletedHistoryReads() > 0);
      action(0, 'Split view');
      await waitForCondition(() => testChatStateRefs.length === 2);
      await waitForCondition(() => deletedHistoryReads() > 1);

      await deleteFromDrawer(0, 'First topic');
      await waitForCondition(
        () =>
          pane(0).textContent.includes('Second conversation sentinel') &&
          testChatStateRefs[1].agents[0].current_session_id === 'session-2',
      );
      const readsAtDeletion = deletedHistoryReads();

      await selectAgentFromPicker('Alpha', pane(1));
      await settle();

      expect(deletedHistoryReads()).toBe(readsAtDeletion);
      expect(pane(1).textContent).toContain('Second conversation sentinel');
    });

    it('follows a Session another window deleted in every area', async () => {
      const props = mountDeletableWorkspace();
      await waitForCondition(() => deletedHistoryReads() > 0);
      action(0, 'Split view');
      await waitForCondition(() => testChatStateRefs.length === 2);
      await waitForCondition(() => deletedHistoryReads() > 1);
      const navigationReports = props.onSessionNavigation.mock.calls.length;

      // Another window archived the Session both areas display.
      deleted = true;
      props.sessionDeletion = serverDeletion();
      flushSync();

      await waitForCondition(() =>
        [0, 1].every(
          (index) =>
            pane(index).textContent.includes('Second conversation sentinel') &&
            testChatStateRefs[index].agents[0].current_session_id ===
              'session-2',
        ),
      );
      const readsAtDeletion = deletedHistoryReads();
      await settle();

      expect(deletedHistoryReads()).toBe(readsAtDeletion);
      // A passive follow corrects the first area's place; another window's
      // act is no new history step.
      expect(
        props.onSessionNavigation.mock.calls.slice(navigationReports),
      ).toEqual([
        [
          expect.objectContaining({ sessionId: 'session-2' }),
          { replace: true },
        ],
      ]);
      expect(
        rpcMock.mock.calls.some(([method]) => method === 'session.delete'),
      ).toBe(false);
    });

    it('ignores a deletion reported before the workspace mounted', async () => {
      const props = reactiveProps({ sessionDeletion: serverDeletion() });
      rpcMock.mockImplementation(createChatRpcMock());
      listSessionsMock.mockResolvedValue({ sessions: [rows['session-1']] });
      const navigation = vi.fn();
      harness.mount(
        {
          target: document.body,
          props: {
            sharedAgents: [createAgent()],
            sharedSelectedAgentId: 'alpha',
            onSessionNavigation: navigation,
            get sessionDeletion() {
              return props.sessionDeletion;
            },
          },
        },
        ChatWorkspace,
      );
      await waitForCondition(() => deletedHistoryReads() > 0);
      await settle();

      expect(testChatStateRefs[0].agents[0].current_session_id).toBe(
        'session-1',
      );
      expect(
        rpcMock.mock.calls.filter(
          ([method, params]) =>
            method === 'chat.history' && params.session_id === 'session-2',
        ),
      ).toEqual([]);
    });

    it.each([
      { eventFirst: false, laterFocus: false },
      { eventFirst: true, laterFocus: false },
      { eventFirst: false, laterFocus: true },
      { eventFirst: true, laterFocus: true },
    ])(
      'completes its own deletion (event first: $eventFirst, later focus: $laterFocus)',
      async ({ eventFirst, laterFocus }) => {
        const deletion = Promise.withResolvers();
        let props;
        props = mountDeletableWorkspace({
          beforeDeleteResponse: async () => {
            if (eventFirst) {
              // The WebSocket echo overtakes the delete response.
              props.sessionDeletion = serverDeletion();
              flushSync();
              await waitForCondition(() =>
                pane(0).textContent.includes('Second conversation sentinel'),
              );
            }
            await deletion.promise;
          },
        });
        await waitForCondition(() => deletedHistoryReads() > 0);
        const navigationReports = props.onSessionNavigation.mock.calls.length;

        await deleteFromDrawer(0, 'First topic');
        await waitForCondition(() =>
          rpcMock.mock.calls.some(([method]) => method === 'session.delete'),
        );
        const laterControl = document.createElement('button');
        document.body.append(laterControl);
        if (laterFocus) laterControl.focus();
        deletion.resolve();
        await waitForCondition(
          () =>
            props.onSessionNavigation.mock.calls.length > navigationReports &&
            (laterFocus
              ? document.activeElement === laterControl
              : document.activeElement?.tagName === 'TEXTAREA' &&
                pane(0).contains(document.activeElement)),
        );
        const readsAtDeletion = deletedHistoryReads();
        await settle();

        expect(document.activeElement).toBe(
          laterFocus ? laterControl : pane(0).querySelector('.msg-input'),
        );

        expect(deletedHistoryReads()).toBe(readsAtDeletion);
        expect(testChatStateRefs[0].agents[0].current_session_id).toBe(
          'session-2',
        );
        expect(pane(0).textContent).toContain('Second conversation sentinel');
      },
    );
  });

  it('opens a Session row or an Agent tab in the other area through its context menu', async () => {
    const agents = [
      createAgent(),
      createAgent({
        id: 'beta',
        name: 'Beta',
        current_session_id: 'session-beta',
      }),
    ];
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents,
        sessionMessages: {
          'session-2': [
            message('second-answer', 'Second conversation sentinel'),
          ],
          'session-beta': [message('beta-answer', 'Beta sentinel')],
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
      ],
    });
    const onSessionNavigation = vi.fn(() => true);
    harness.mount(
      {
        target: document.body,
        props: {
          sharedAgents: agents,
          sharedSelectedAgentId: 'alpha',
          onSessionNavigation,
        },
      },
      ChatWorkspace,
    );
    await waitForCondition(() => pane(0)?.textContent.includes('Hello'));
    const openFromMenu = async (element, label) => {
      element.dispatchEvent(
        new MouseEvent('contextmenu', {
          bubbles: true,
          cancelable: true,
          clientX: 40,
          clientY: 40,
        }),
      );
      flushSync();
      const item = [...document.querySelectorAll('[role="menuitem"]')].find(
        (entry) => entry.textContent.trim() === label,
      );
      expect(item).toBeTruthy();
      item.click();
      flushSync();
    };
    const sessionRow = (index) =>
      pane(index)
        .querySelector('.session-row__select')
        ?.closest('.session-row');
    const agentTab = (index, name) =>
      [...pane(index).querySelectorAll('button.agent-pill')].find(
        (pill) => pill.textContent.trim() === name,
      );

    // A row of the only area opens the split view and shows its Session
    // there; the first area keeps its own Session.
    action(0, 'Session list');
    await waitForCondition(() => sessionRow(0));
    await openFromMenu(sessionRow(0), 'Open in split view');
    await waitForCondition(() =>
      pane(1)?.textContent.includes('Second conversation sentinel'),
    );
    expect(pane(1).hidden).toBe(false);
    expect(pane(0).textContent).toContain('Hello');
    expect(pane(0).textContent).not.toContain('Second conversation sentinel');

    // Once split, a tab offers the other area and shows its Agent there.
    await waitForCondition(() => agentTab(0, 'Beta')?.disabled === false);
    await openFromMenu(agentTab(0, 'Beta'), 'Open in other area');
    await waitForCondition(() => pane(1).textContent.includes('Beta sentinel'));
    expect(pane(0).textContent).toContain('Hello');

    // The second area sends a Session to the first, as a history step of
    // the first area's place.
    onSessionNavigation.mockClear();
    action(1, 'Session list');
    await waitForCondition(() => sessionRow(1));
    await openFromMenu(sessionRow(1), 'Open in other area');
    await waitForCondition(() =>
      pane(0).textContent.includes('Second conversation sentinel'),
    );
    expect(pane(1).textContent).toContain('Beta sentinel');
    expect(onSessionNavigation).toHaveBeenCalledWith(
      expect.objectContaining({ agentId: 'beta', sessionId: 'session-2' }),
      { replace: false },
    );
  });

  it('opens a Sub-Agent Session and returns to its parent within the second area', async () => {
    const agents = [createAgent(), createAgent({ id: 'beta', name: 'Beta' })];
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents,
        sessionMessages: {
          'session-2': [
            {
              id: 'delegation',
              role: 'assistant',
              content: null,
              tool_calls: [
                {
                  id: 'child-call',
                  name: 'subagent',
                  arguments: {
                    action: 'run',
                    agent_id: 'beta',
                    content: 'Review the work',
                  },
                },
              ],
            },
            {
              id: 'delegation-result',
              role: 'tool',
              tool_call_id: 'child-call',
              name: 'subagent',
              content: JSON.stringify({
                ok: true,
                data: {
                  agent_id: 'beta',
                  session_id: 'child-session',
                  status: 'completed',
                },
              }),
            },
            message('parent-answer', 'Second-area parent history'),
          ],
          'child-session': [message('child-answer', 'Child history sentinel')],
        },
      }),
    );
    listSessionsMock.mockResolvedValue({
      sessions: [{ id: 'session-2', title: 'Parent topic' }],
    });
    getSessionMock.mockImplementation(async (agentId, sessionId) => ({
      session:
        agentId === 'beta' && sessionId === 'child-session'
          ? {
              id: sessionId,
              is_subagent_session: true,
              subagent_parent: { agent_id: 'alpha', session_id: 'session-2' },
            }
          : { id: sessionId, title: 'Parent topic' },
    }));
    const onSessionNavigation = vi.fn();
    harness.mount(
      {
        target: document.body,
        props: {
          sharedAgents: agents,
          sharedSelectedAgentId: 'alpha',
          onSessionNavigation,
        },
      },
      ChatWorkspace,
    );
    await waitForCondition(() => pane(0)?.textContent.includes('Hello'));
    action(0, 'Split view');
    await waitForCondition(() => button(pane(1), 'Session list'));
    action(1, 'Session list');
    await waitForCondition(() => pane(1).querySelector('.session-row__select'));
    pane(1).querySelector('.session-row__select').click();
    await waitForCondition(() => button(pane(1), 'Open Sub-Agent Session'));
    onSessionNavigation.mockClear();

    action(1, 'Open Sub-Agent Session');

    await waitForCondition(() =>
      pane(1).textContent.includes('Child history sentinel'),
    );
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'beta',
      session_id: 'child-session',
      limit: 100,
    });
    expect(pane(0).textContent).toContain('Hello');
    expect(pane(0).textContent).not.toContain('Child history sentinel');
    await waitForCondition(() => button(pane(1), 'Return to parent session'));
    action(1, 'Return to parent session');
    await waitForCondition(() =>
      pane(1).textContent.includes('Second-area parent history'),
    );
    expect(pane(1).textContent).not.toContain('Child history sentinel');
    expect(pane(0).textContent).toContain('Hello');
    action(1, 'New session');
    await waitForCondition(
      () => testChatStateRefs[1].agents[0].current_session_id === '',
    );
    expect(testChatStateRefs[1].selectedAgentId).toBe('alpha');
    expect(pane(0).textContent).toContain('Hello');
    expect(onSessionNavigation).not.toHaveBeenCalled();
  });

  it('resizes with keyboard, clamps widths and restores equal sizes', async () => {
    await start();
    const divider = document.querySelector('[role="separator"]');
    const key = (value) => {
      divider.dispatchEvent(
        new KeyboardEvent('keydown', { key: value, bubbles: true }),
      );
      flushSync();
    };
    key('ArrowRight');
    expect(divider.getAttribute('aria-valuenow')).toBe('52');
    key('End');
    expect(divider.getAttribute('aria-valuenow')).toBe(
      divider.getAttribute('aria-valuemax'),
    );
    key('Home');
    expect(divider.getAttribute('aria-valuenow')).toBe(
      divider.getAttribute('aria-valuemin'),
    );
    key('Enter');
    expect(divider.getAttribute('aria-valuenow')).toBe('50');
    expect(localStorage.getItem('vbot.chat.splitRatio')).toBe('50');
  });

  it('keeps area actions inside existing controls and opens HTML output in the other area without replacing its Chat', async () => {
    await start();
    const chat = pane(1).querySelector('.chat-view');
    const input = chat.querySelector('.msg-input');
    setInputValue(input, 'Draft survives the content menu');
    flushSync();
    for (const index of [0, 1]) {
      expect(
        pane(index).firstElementChild.classList.contains(
          'chat-workspace__body',
        ),
      ).toBe(true);
      expect(pane(index).querySelector('.chat-workspace__toolbar')).toBeNull();
      expect(pane(index).querySelector('[role="tablist"]')).toBeNull();
      expect(
        button(pane(index), 'Close area').closest('.chat-header'),
      ).not.toBeNull();
    }
    mockPreviewOpening();
    pane(0).querySelector('.msg-markdown a').click();
    await waitForCondition(() => pane(1).querySelector('iframe'));
    expect(rpcMock).toHaveBeenCalledWith('file.preview_open', {
      source: '/api/files/file-token',
    });
    expect(pane(1).querySelector('iframe').getAttribute('sandbox')).toBe(
      'allow-scripts allow-downloads',
    );
    expect(chat.hidden).toBe(true);
    expect(
      button(pane(1), 'Back to chat').closest('.html-preview__toolbar'),
    ).not.toBeNull();
    action(1, 'Back to chat');
    expect(chat.hidden).toBe(false);
    expect(pane(1).querySelector('.chat-view')).toBe(chat);
    expect(chat.textContent).toContain('Second conversation sentinel');
    expect(testChatStateRefs).toHaveLength(2);
    expect(chat.querySelector('.msg-input')).toBe(input);
    expect(input.value).toBe('Draft survives the content menu');
    action(1, 'Show preview');
    expect(chat.hidden).toBe(true);
    expect(
      pane(1).querySelector('.html-preview input, .html-preview form'),
    ).toBeNull();
  });

  it('opens a rendered Agent file output directly from one Chat without a manual Preview entry', async () => {
    await start(false);
    expect(button(pane(0), 'Split view')).toBeTruthy();
    expect(document.querySelector('[role="menu"]')).toBeNull();
    mockPreviewOpening();
    pane(0).querySelector('.msg-markdown a').click();
    await waitForCondition(() => pane(1)?.querySelector('iframe'));
    expect(pane(0).querySelector('.chat-view').hidden).toBe(false);
    expect(
      pane(1).querySelector('.html-preview input, .html-preview form'),
    ).toBeNull();
    expect(testChatStateRefs).toHaveLength(1);
    expect(rpcMock).toHaveBeenCalledWith('file.preview_open', {
      source: '/api/files/file-token',
    });
    action(1, 'Close area');
    pane(0).querySelector('.msg-markdown a').click();
    await waitForCondition(() => !pane(1).hidden);
    expect(testChatStateRefs).toHaveLength(1);
  });

  it('opens external and download actions through the Desktop browser bridge without splitting Chat', async () => {
    await start(false);
    window.history.replaceState({}, '', '/?accessor=desktop');
    const openExternalUrl = vi.fn().mockResolvedValue(undefined);
    window.pywebview = { api: { openExternalUrl } };
    pane(0).querySelector('[data-file-external]').click();
    expect(openExternalUrl).toHaveBeenLastCalledWith(
      new URL('/api/files/file-token', window.location.href).href,
    );
    const trigger = pane(0).querySelector(
      'a[data-preview-file]:not([data-file-external])',
    );
    trigger.dispatchEvent(
      new MouseEvent('contextmenu', {
        bubbles: true,
        cancelable: true,
        clientX: 90,
        clientY: 120,
      }),
    );
    await waitForCondition(() => document.querySelector('[role="menu"]'));
    const items = [...document.querySelectorAll('[role="menuitem"]')];
    expect(items).toHaveLength(3);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    const download = items.find((item) => item.hasAttribute('download'));
    expect(download.getAttribute('href')).toBe(
      '/api/files/file-token?download=true',
    );
    expect(download.getAttribute('download')).toBe('index.html');
    download.click();
    flushSync();
    expect(openExternalUrl).toHaveBeenLastCalledWith(
      new URL('/api/files/file-token?download=true', window.location.href).href,
    );
    expect(document.querySelector('[role="menu"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(document.querySelector('[role="separator"]')).toBeNull();
    expect(
      rpcMock.mock.calls.some(([method]) => method === 'file.preview_open'),
    ).toBe(false);
  });

  it.each(['browser', 'desktop', 'failure'])(
    'copies a report path through the %s clipboard and restores focus',
    async (accessor) => {
      const path = String.raw`C:\Users\Viro\Berichte\Übersicht (final).md`;
      const title = path.replaceAll('\\', '\\\\');
      const onToast = vi.fn();
      await start(
        false,
        `Hello! [report.md](/api/files/report-token "${title}")`,
        onToast,
      );
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText },
        configurable: true,
      });
      const setClipboardText = vi.fn().mockResolvedValue(undefined);
      if (accessor === 'desktop') {
        window.history.replaceState({}, '', '/?accessor=desktop');
        window.pywebview = { api: { setClipboardText } };
      }
      if (accessor === 'failure')
        writeText.mockRejectedValue(new Error('denied'));
      const link = pane(0).querySelector('.msg-markdown a');
      const event =
        accessor === 'desktop'
          ? new KeyboardEvent('keydown', {
              key: 'F10',
              shiftKey: true,
              bubbles: true,
              cancelable: true,
            })
          : new MouseEvent('contextmenu', {
              bubbles: true,
              cancelable: true,
              clientX: 90,
              clientY: 120,
            });
      link.dispatchEvent(event);
      await waitForCondition(
        () => document.activeElement?.getAttribute('role') === 'menuitem',
      );
      expect(event.defaultPrevented).toBe(true);
      const items = [...document.querySelectorAll('[role="menuitem"]')];
      expect(items.map((item) => item.textContent.trim())).toEqual([
        'Copy file path',
        'Open in browser',
        'Download',
      ]);
      items[0].click();
      await waitForCondition(() => onToast.mock.calls.length > 0);
      expect(
        accessor === 'desktop' ? setClipboardText : writeText,
      ).toHaveBeenCalledWith(path);
      if (accessor === 'desktop') expect(writeText).not.toHaveBeenCalled();
      expect(onToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: accessor === 'failure' ? 'error' : 'success',
        }),
      );
      expect(document.querySelector('[role="menu"]')).toBeNull();
      expect(document.activeElement).toBe(link);
      expect(document.querySelector('[role="separator"]')).toBeNull();
      expect(
        rpcMock.mock.calls.some(([method]) => method === 'file.preview_open'),
      ).toBe(false);
    },
  );

  it('supports file menu keyboard navigation across buttons and links, and its preview action', async () => {
    await start(false);
    mockPreviewOpening();
    const trigger = pane(0).querySelector(
      'a[data-preview-file]:not([data-file-external])',
    );
    trigger.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'F10',
        shiftKey: true,
        bubbles: true,
      }),
    );
    await waitForCondition(
      () => document.activeElement?.getAttribute('role') === 'menuitem',
    );
    const items = [...document.querySelectorAll('[role="menuitem"]')];
    expect(document.activeElement).toBe(items[0]);
    items[0].dispatchEvent(
      new KeyboardEvent('keydown', { key: 'End', bubbles: true }),
    );
    expect(document.activeElement).toBe(items[2]);
    items[2].dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    flushSync();
    expect(document.activeElement).toBe(trigger);
    trigger.dispatchEvent(
      new MouseEvent('contextmenu', {
        bubbles: true,
        cancelable: true,
        clientX: 90,
        clientY: 120,
      }),
    );
    await waitForCondition(() => document.querySelector('[role="menu"]'));
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }));
    flushSync();
    expect(document.querySelector('[role="menu"]')).toBeNull();
    trigger.dispatchEvent(
      new MouseEvent('contextmenu', {
        bubbles: true,
        cancelable: true,
        clientX: 90,
        clientY: 120,
      }),
    );
    flushSync();
    document.querySelector('[role="menuitem"]').click();
    await waitForCondition(() => pane(1)?.querySelector('iframe'));
    expect(document.querySelector('[role="menu"]')).toBeNull();
    expect(rpcMock).toHaveBeenCalledWith('file.preview_open', {
      source: '/api/files/file-token',
    });
  });
});
