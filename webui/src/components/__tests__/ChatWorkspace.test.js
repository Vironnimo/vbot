// @vitest-environment jsdom
import { afterEach, beforeEach, vi } from 'vitest';
import {
  describe,
  expect,
  it,
  applyConnectionSnapshotMock,
  rpcMock,
  createChatRpcMock,
  createAgent,
  listSessionsMock,
  setInputValue,
  flushSync,
  waitForCondition,
  setupChatViewTestSuite,
  testChatStateRefs,
} from './ChatView.support.js';
import { unmount } from 'svelte';
import { resetComposerMemory } from '../../lib/composerMemory.js';
import ChatWorkspace from '../ChatWorkspace.svelte';
import { reactiveProps } from './reactiveProps.svelte.js';

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
    await waitForCondition(() => pane(0)?.textContent.includes('Hello'), 100);
    if (!split) return;
    action(0, 'Split view');
    await waitForCondition(() => button(pane(1), 'Sessions'), 100);
    action(1, 'Sessions');
    await waitForCondition(
      () => pane(1)?.textContent.includes('Second topic'),
      100,
    );
    Array.from(pane(1).querySelectorAll('button'))
      .find((el) => el.textContent.includes('Second topic'))
      .click();
    await waitForCondition(
      () => pane(1)?.textContent.includes('Second conversation sentinel'),
      100,
    );
  }

  it.each([0, 1])(
    'keeps the Session list open after selecting a Session in area %i',
    async (index) => {
      await start(false);
      if (index === 1) {
        action(0, 'Split view');
        await waitForCondition(() => button(pane(1), 'Sessions'), 100);
      }
      action(index, 'Sessions');
      await waitForCondition(
        () => pane(index).querySelector('.session-row__select'),
        100,
      );
      const drawer = pane(index).querySelector('.session-drawer');
      drawer.querySelector('.session-row__select').click();
      await waitForCondition(
        () => pane(index).textContent.includes('Second conversation sentinel'),
        100,
      );
      expect(pane(index).querySelector('.session-drawer')).toBe(drawer);
      const selectedRow = drawer.querySelector('.session-row__select--active');
      expect(selectedRow).not.toBeNull();
      selectedRow.click();
      flushSync();
      expect(pane(index).querySelector('.session-drawer')).toBe(drawer);
      action(index, 'Sessions');
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
        await waitForCondition(() => pane(1)?.querySelector('iframe'), 100);
        expect(testChatStateRefs).toHaveLength(1);
      }
      action(0, 'Sessions');
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
      await waitForCondition(() => button(pane(1), 'Sessions'), 100);
      expect(pane(1).querySelector('.session-drawer')).toBeNull();
      expect(pane(0).querySelector('.session-drawer')).toBe(firstDrawer);

      listSessionsMock.mockClear();
      action(1, 'Sessions');
      await waitForCondition(() => listSessionsMock.mock.calls.length > 0, 100);
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
      action(1, 'Sessions');
      action(1, 'Close area');
      action(0, 'Split view');
      expect(pane(1).querySelector('.session-drawer')).toBeNull();
      action(1, 'Sessions');
      pane(1).querySelector('.session-drawer__filter-trigger').click();
      flushSync();
      expect(switches()[0].getAttribute('aria-checked')).toBe('false');
      action(1, 'Sessions');
      action(0, 'Sessions');
      action(0, 'Sessions');
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
      action(index, 'Sessions');
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
    action(0, 'Sessions');
    action(0, 'Split view');
    await waitForCondition(() => button(pane(1), 'Sessions'), 100);
    openFilters(1);
    button(pane(1), 'All agents').click();
    flushSync();
    for (const toggle of switches()) {
      toggle.click();
      flushSync();
    }

    await unmount(harness.mountedComponent);
    await start(false);
    listSessionsMock.mockClear();
    openFilters(0);
    expect(switches()).toHaveLength(5);
    expect(
      switches().map((toggle) => toggle.getAttribute('aria-checked')),
    ).toEqual(Array(5).fill('true'));
    await waitForCondition(() => listSessionsMock.mock.calls.length > 0, 100);
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
    action(0, 'Sessions');
    action(0, 'Split view');
    await waitForCondition(() => button(pane(1), 'Sessions'), 100);
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
    action(1, 'Close area');
    flushSync();
    expect(pane(1).hidden).toBe(true);
    expect(pane(0).querySelector('.msg-input')).toBe(leftInput);
    action(0, 'Split view');
    flushSync();
    expect(pane(1).querySelector('.msg-input')).toBe(rightInput);
    expect(leftInput.value).toBe('Left draft sentinel');
    expect(rightInput.value).toBe('Right draft sentinel');
    expect(testChatStateRefs).toHaveLength(2);
    // A new Session in the second pane cannot re-aim the first pane's landing.
    button(pane(1), 'New session').click();
    await waitForCondition(
      () => rpcMock.mock.calls.some(([method]) => method === 'session.create'),
      100,
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
      await waitForCondition(
        () =>
          rpcMock.mock.calls.some(
            ([method, params]) =>
              method === 'chat.stream' && params.content === content,
          ),
        100,
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
    await waitForCondition(() => pane(0)?.querySelector('.msg-input'), 100);
    const firstInput = pane(0).querySelector('.msg-input');
    setInputValue(firstInput, 'Shared Session draft');
    flushSync();
    action(0, 'Split view');
    await waitForCondition(() => testChatStateRefs.length === 2, 100);
    expect(pane(1).querySelector('.msg-input')).toBeNull();
    action(0, 'Close area');
    await waitForCondition(() => pane(1).querySelector('.msg-input'), 100);
    const secondInput = pane(1).querySelector('.msg-input');
    expect(secondInput.value).toBe('Shared Session draft');
    setInputValue(secondInput, 'Continued in the right');
    flushSync();
    action(1, 'Split view');
    await waitForCondition(() => pane(0).querySelector('.msg-input'), 100);
    expect(pane(0).querySelector('.msg-input').value).toBe(
      'Continued in the right',
    );
    expect(pane(1).querySelector('.msg-input')).toBeNull();
  });

  it('can create a second Session even when the first Session is empty', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: { 'session-1': [], 'created-alpha': [] },
      }),
    );
    harness.mount({ target: document.body }, ChatWorkspace);
    await waitForCondition(() => pane(0)?.querySelector('.msg-input'), 100);
    action(0, 'Split view');
    await waitForCondition(() => pane(1)?.querySelector('.chat-view'), 100);
    await waitForCondition(() => button(pane(1), 'New session'), 100);
    button(pane(1), 'New session').click();
    await waitForCondition(() => pane(1).querySelector('.msg-input'), 100);
    expect(testChatStateRefs[0].agents[0].current_session_id).toBe('session-1');
    expect(testChatStateRefs[1].agents[0].current_session_id).toBe(
      'created-alpha',
    );
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
    const tabLabels = (index) =>
      Array.from(pane(index).querySelectorAll('.chat-header .agent-tab')).map(
        (tab) => tab.getAttribute('aria-label'),
      );
    await waitForCondition(() => tabLabels(0).includes('Beta: Running'), 100);

    // Beta's Run ends; later traffic pushes its terminal event out of App's
    // bounded window, and Gamma's Run starts outside the retained window.
    props.runServerEvents = [lifecycle('run_completed', 'R1', 'beta', 'b-1')];
    props.activeRuns = [];
    await waitForCondition(() => !tabLabels(0).includes('Beta: Running'), 100);
    props.runServerEvents = [];
    props.activeRuns = [
      { run_id: 'R2', agent_id: 'gamma', session_id: 'g-1', status: 'running' },
    ];

    action(0, 'Split view');
    await waitForCondition(() => tabLabels(1).includes('Gamma: Running'), 100);

    expect(tabLabels(1)).not.toContain('Beta: Running');
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

    function mountDeletableWorkspace() {
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
          return { ...params, next_session_id: 'session-2' };
        }
        if (method === 'agent.list') {
          return {
            agents: [
              createAgent({
                current_session_id: deleted ? 'session-2' : 'session-1',
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
        sessions: deleted
          ? [rows['session-2']]
          : [rows['session-1'], rows['session-2']],
      }));
      const props = reactiveProps({
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        agentsRefreshToken: 0,
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
          },
        },
        ChatWorkspace,
      );
      return props;
    }

    async function deleteFromDrawer(index, title) {
      action(index, 'Sessions');
      await waitForCondition(
        () =>
          Array.from(pane(index).querySelectorAll('.session-row')).some((row) =>
            row.textContent.includes(title),
          ),
        100,
      );
      Array.from(pane(index).querySelectorAll('.session-row'))
        .find((row) => row.textContent.includes(title))
        .querySelector('.session-row__menu-trigger')
        .click();
      flushSync();
      document.querySelector('.session-row__menu-item--danger').click();
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

    it('lands on the server landing and never reopens the deleted Session', async () => {
      const props = mountDeletableWorkspace();
      await waitForCondition(() => deletedHistoryReads() > 0, 100);
      await deleteFromDrawer(0, 'First topic');
      await waitForCondition(
        () => pane(0).textContent.includes('Second conversation sentinel'),
        100,
      );
      const readsAtDeletion = deletedHistoryReads();

      // App publishes rosters (refreshed and still-stale) and bumps the token.
      props.sharedAgents = [createAgent({ current_session_id: 'session-2' })];
      props.agentsRefreshToken = 1;
      flushSync();
      await waitForCondition(
        () =>
          rpcMock.mock.calls.filter(([method]) => method === 'agent.list')
            .length >= 2,
        100,
      );
      props.sharedAgents = [createAgent()];
      flushSync();

      // Selecting the same Agent returns to its current Session.
      pane(0).querySelector('.agent-tab').click();
      flushSync();
      await settle();

      expect(testChatStateRefs[0].agents[0].current_session_id).toBe(
        'session-2',
      );
      expect(deletedHistoryReads()).toBe(readsAtDeletion);
      expect(pane(0).textContent).toContain('Second conversation sentinel');
    });

    it('releases the deleted Session in the other Chat area too', async () => {
      mountDeletableWorkspace();
      await waitForCondition(() => deletedHistoryReads() > 0, 100);
      action(0, 'Split view');
      await waitForCondition(() => testChatStateRefs.length === 2, 100);
      await waitForCondition(() => deletedHistoryReads() > 1, 100);

      await deleteFromDrawer(0, 'First topic');
      await waitForCondition(
        () =>
          pane(0).textContent.includes('Second conversation sentinel') &&
          testChatStateRefs[1].agents[0].current_session_id === 'session-2',
        100,
      );
      const readsAtDeletion = deletedHistoryReads();

      pane(1).querySelector('.agent-tab').click();
      flushSync();
      await settle();

      expect(deletedHistoryReads()).toBe(readsAtDeletion);
      expect(pane(1).textContent).toContain('Second conversation sentinel');
    });
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

  it('keeps area actions inside existing controls and preserves Chat through manual Preview switching', async () => {
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
        button(pane(index), 'Close area').closest(
          '.chat-view__session-bar, .session-drawer__controls',
        ),
      ).not.toBeNull();
    }
    mockPreviewOpening();
    pane(0).querySelector('.msg-markdown a').click();
    await waitForCondition(() => pane(1).querySelector('iframe'), 100);
    expect(chat.hidden).toBe(true);
    expect(
      button(pane(1), 'Back to chat').closest('.html-preview__toolbar'),
    ).not.toBeNull();
    action(1, 'Back to chat');
    expect(chat.hidden).toBe(false);
    expect(chat.querySelector('.msg-input')).toBe(input);
    expect(input.value).toBe('Draft survives the content menu');
    action(1, 'Show preview');
    expect(chat.hidden).toBe(true);
    expect(
      pane(1).querySelector('.html-preview input, .html-preview form'),
    ).toBeNull();
  });

  it('restores focus on the remaining area after closing one', async () => {
    await start();
    button(pane(0), 'Close area').click();
    flushSync();
    expect(pane(0).hidden).toBe(true);
    await waitForCondition(
      () => document.activeElement === button(pane(1), 'Split view'),
      100,
    );
  });

  it('opens an HTML output in the other area without replacing its Chat', async () => {
    await start();
    mockPreviewOpening();
    const anchor = pane(0).querySelector('.msg-markdown a');
    anchor.click();
    await waitForCondition(() => pane(1).querySelector('iframe'), 100);
    expect(rpcMock).toHaveBeenCalledWith('file.preview_open', {
      source: '/api/files/file-token',
    });
    expect(pane(1).querySelector('iframe').getAttribute('sandbox')).toBe(
      'allow-scripts allow-downloads',
    );
    const rightChat = pane(1).querySelector('.chat-view');
    action(1, 'Back to chat');
    flushSync();
    expect(pane(1).querySelector('.chat-view')).toBe(rightChat);
    expect(rightChat.textContent).toContain('Second conversation sentinel');
    expect(testChatStateRefs).toHaveLength(2);
  });

  it('opens a rendered Agent file output directly from one Chat without a manual Preview entry', async () => {
    await start(false);
    expect(button(pane(0), 'Split view')).toBeTruthy();
    expect(document.querySelector('[role="menu"]')).toBeNull();
    mockPreviewOpening();
    pane(0).querySelector('.msg-markdown a').click();
    await waitForCondition(() => pane(1)?.querySelector('iframe'), 100);
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
    await waitForCondition(() => !pane(1).hidden, 100);
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
    await waitForCondition(() => document.querySelector('[role="menu"]'), 100);
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
        100,
      );
      expect(event.defaultPrevented).toBe(true);
      const items = [...document.querySelectorAll('[role="menuitem"]')];
      expect(items.map((item) => item.textContent.trim())).toEqual([
        'Copy file path',
        'Open in browser',
        'Download',
      ]);
      items[0].click();
      await waitForCondition(() => onToast.mock.calls.length > 0, 100);
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
      100,
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
    await waitForCondition(() => document.querySelector('[role="menu"]'), 100);
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
    await waitForCondition(() => pane(1)?.querySelector('iframe'), 100);
    expect(document.querySelector('[role="menu"]')).toBeNull();
    expect(rpcMock).toHaveBeenCalledWith('file.preview_open', {
      source: '/api/files/file-token',
    });
  });
});
