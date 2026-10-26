// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import {
  applyConnectionSnapshotMock,
  cancelRunMock,
  closeSubscriptionForMock,
  createAgent,
  createChatRpcMock,
  findCancelRunButton,
  findNewSessionButton,
  flushSync,
  historyReads,
  listQueueMock,
  listedSessions,
  message,
  rpcCalls,
  rpcMock,
  runEventSource,
  runServerEvent,
  runningRun,
  sendComposerMessage,
  setInputValue,
  settle,
  setupChatViewTestSuite,
  subscribeRunEventsMock,
  testChatStateRefs,
  waitForCondition,
  waitForText,
  sessionListButton,
} from './ChatView.support.js';
import { createChatViewParentHarness } from './ChatView.parent.support.svelte.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import { createRunEventFeed } from '../../lib/runEventFeed.svelte.js';
import { sessionActivityFromSnapshot } from '../../lib/sessionActivity.js';
import { t } from '../../lib/i18n.js';
import { assistantRun, outputItem, toolItem } from './timelineItems.support.js';

const alphaProps = () => ({
  sharedAgents: [createAgent()],
  sharedSelectedAgentId: 'alpha',
});

const subAgentRow = () => document.querySelector('.subagent-tool-event');
const subAgentDot = (status) =>
  subAgentRow()?.querySelector(`.te-dot.${status}`) ?? null;

async function reopenCurrentSessionFromDrawer() {
  sessionListButton().click();
  await waitForCondition(() =>
    Boolean(document.querySelector('.session-row__select')),
  );
  document.querySelector('.session-row__select').click();
}

// Starts a Run from the composer and returns its event source.
async function startRun(runId, content) {
  await waitForText('Hello');
  sendComposerMessage(content);
  await waitForCondition(() => subscribeRunEventsMock.mock.calls.length === 1);
  const source = runEventSource(runId);
  source.emit('timeline_snapshot', {
    items: [assistantRun(runId, [])],
    state: { status: 'running' },
  });
  return source;
}

// The server announces the child before its spawning Tool returns.
function emitSubAgentSpawn(source, callId, spawn, { afterStart } = {}) {
  const row = toolItem({
    id: callId,
    name: 'subagent',
    arguments: {
      action: 'run',
      agent_id: 'alpha',
      content: 'Inspect the project',
    },
    subAgentSession: spawn,
  });
  source.emit('timeline_update', {
    run: { id: `assistant-run-${source.runId}` },
    children: [row],
  });
  flushSync();
  afterStart?.();
  source.emit('timeline_update', {
    run: { id: `assistant-run-${source.runId}` },
    children: [
      toolItem({
        ...row,
        result: { ok: true, data: spawn },
        status: 'success',
      }),
    ],
  });
  flushSync();
}

// Serves `sub-session-1` History from `subSessionHistory` (or fails with it
// when it is an Error) for the sub-agent status checks; the parent Run starts
// as `run-parent`.
function serveSubAgentHistory(subSessionHistory) {
  const fallback = createChatRpcMock({
    streamResponse: runningRun('run-parent'),
  });
  rpcMock.mockImplementation(async (method, params) => {
    if (method !== 'chat.timeline' || params?.session_id !== 'sub-session-1') {
      return fallback(method, params);
    }
    if (subSessionHistory instanceof Error) throw subSessionHistory;
    return subSessionHistory;
  });
}

const subSessionHistory = (overrides = {}) => ({
  session_id: 'sub-session-1',
  messages: [],
  has_more: false,
  ...overrides,
});

const statusChecks = () =>
  rpcCalls('chat.timeline').filter(
    (params) => params.session_id === 'sub-session-1' && params.limit === 20,
  ).length;

describe('ChatView Runs', () => {
  const chat = setupChatViewTestSuite();

  // A background sub-agent row whose child Run `child-run` is running. The
  // dot is running as soon as the child Session starts, before the spawn
  // result arrives.
  async function mountRunningSubAgent() {
    await chat.mountChat(alphaProps(), { ready: null });
    const source = await startRun('run-parent', 'Spawn background sub-agent');
    emitSubAgentSpawn(
      source,
      'call-running',
      {
        agent_id: 'alpha',
        session_id: 'sub-session-1',
        run_id: 'child-run',
        status: 'running',
      },
      {
        afterStart: () => {
          expect(subAgentDot('running')).not.toBeNull();
          expect(subAgentDot('done')).toBeNull();
        },
      },
    );
    expect(subAgentDot('running')).not.toBeNull();
    expect(subAgentDot('done')).toBeNull();
    return source;
  }

  // A new parent-Run event re-renders the Timeline, which reconciles every
  // sub-agent row again.
  function rerenderTimeline(source, callId) {
    source.emit('timeline_update', {
      run: { id: `assistant-run-${source.runId}` },
      children: [
        toolItem({ id: callId, name: 'read', arguments: { path: 'notes.md' } }),
      ],
    });
    flushSync();
  }

  const clickSubAgentCancel = () =>
    subAgentRow().querySelector('[data-cancel="subagent"]').click();

  describe('run stream', () => {
    it('applies a live command status without a reactive update loop', async () => {
      rpcMock.mockImplementation(createChatRpcMock());
      await chat.mountChat({
        commandStatuses: {
          cmd_test: {
            status: 'completed',
            stillRunning: false,
            sessionId: 'session-one',
          },
        },
      });

      const input = document.querySelector('textarea');
      setInputValue(input, 'Continue after completion');
      flushSync();
      expect(input.value).toBe('Continue after completion');
      expect(
        document.querySelector('[aria-label="Send message"]').disabled,
      ).toBe(false);
    });

    it('renders server-batched text and applies its incremental append in the same row', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({ streamResponse: runningRun('run-batched') }),
      );
      await chat.mountChat({}, { ready: null });
      const source = await startRun('run-batched', 'Start batched stream');
      source.emit('timeline_update', {
        run: { id: 'assistant-run-run-batched' },
        children: [outputItem('Streamed ', { id: 'answer', streaming: true })],
      });
      flushSync();
      expect(document.body.textContent).toContain('Streamed');
      source.emit('timeline_update', {
        run: { id: 'assistant-run-run-batched' },
        child_updates: [{ id: 'answer', append: { content: 'answer' } }],
      });
      flushSync();
      expect(document.body.textContent).toContain('Streamed answer');
      expect(document.querySelectorAll('.assistant-run')).toHaveLength(1);
    });

    it('loads a live Bash raw result in a new Session before its first History read', async () => {
      const fullResult = {
        ok: true,
        data: { output: 'Complete Bash output from the raw call' },
      };
      const fallback = createChatRpcMock({
        streamResponse: {
          ...runningRun('run-bash'),
          session_id: 'created-alpha',
        },
      });
      rpcMock.mockImplementation((method, params) =>
        method === 'chat.tool_call'
          ? { arguments: { command: 'cat big.log' }, result: fullResult }
          : fallback(method, params),
      );
      const parent = createChatViewParentHarness();
      await chat.mountChat(parent.props(['navigation']));
      findNewSessionButton().click();
      await settle();
      sendComposerMessage('Read the log');
      await waitForCondition(
        () => subscribeRunEventsMock.mock.calls.length === 1,
      );

      const source = runEventSource('run-bash');
      source.emit('timeline_snapshot', {
        history_generation: 'created-generation',
        items: [
          assistantRun('run-bash', [
            toolItem({
              id: 'call-bash',
              name: 'bash',
              arguments: { command: 'cat big.log' },
              result: { ok: true, data: { output: 'Preview...' } },
              rawTruncated: true,
              display: {
                version: 1,
                hidden_argument_keys: [],
                primary: [],
                facts: [],
                details: [
                  { type: 'text', label: 'output', text: 'Preview...' },
                ],
              },
            }),
          ]),
        ],
        state: { status: 'running' },
      });
      // Later patches carry no generation; the initial snapshot owns it.
      source.emit('timeline_update', {
        run: { id: 'assistant-run-run-bash', iterationCount: 2 },
      });
      flushSync();
      for (const selector of [
        '.working-block',
        '.tool-event',
        '.tool-raw-call',
      ]) {
        for (const disclosure of document.querySelectorAll(selector)) {
          disclosure.open = true;
          disclosure.dispatchEvent(new Event('toggle'));
        }
        flushSync();
      }
      await waitForText(fullResult.data.output);
      expect(rpcCalls('chat.tool_call')).toEqual([
        {
          agent_id: 'alpha',
          session_id: 'created-alpha',
          run_id: 'run-bash',
          tool_id: 'call-bash',
          history_generation: 'created-generation',
        },
      ]);
      expect(historyReads('created-alpha')).toBe(0);
    });

    it('forwards a live bash row background action to the Run control RPC', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({ streamResponse: runningRun('run-bash') }),
      );
      await chat.mountChat({}, { ready: null });
      const source = await startRun('run-bash', 'Run a long command');

      source.emit('timeline_update', {
        run: { id: 'assistant-run-run-bash' },
        children: [
          toolItem({
            id: 'call-bash',
            name: 'bash',
            arguments: { command: 'sleep 60' },
          }),
        ],
        state: {
          controls: {
            compaction: 'unavailable',
            background_tool_call_ids: ['call-bash'],
          },
          controls_sequence: 2,
        },
      });
      flushSync();

      document.querySelector('[aria-label="Move to background"]').click();
      flushSync();
      await waitForCondition(() => rpcCalls('chat.control_run').length > 0);
      expect(rpcCalls('chat.control_run')).toEqual([
        {
          agent_id: 'alpha',
          session_id: 'session-1',
          run_id: 'run-bash',
          action: 'background_tool',
          tool_call_id: 'call-bash',
        },
      ]);
    });

    it('coalesces repeated run stream errors into one reconnect', async () => {
      const activeRuns = {};
      rpcMock.mockImplementation(
        createChatRpcMock({
          activeRuns,
          streamResponse: runningRun('run-reconnect'),
        }),
      );
      await chat.mountChat({}, { ready: null });
      const { handlers } = await startRun(
        'run-reconnect',
        'Start reconnecting stream',
      );

      activeRuns['session-1'] = runningRun('run-reconnect');
      vi.useFakeTimers();
      // Pin reconnect jitter to its midpoint so attempt 0 fires at exactly the
      // base 500ms delay this test advances by.
      const randomSpy = vi.spyOn(Math, 'random').mockReturnValue(0.5);
      try {
        handlers.onError(new Error('first disconnect'));
        handlers.onError(new Error('second disconnect'));
        await vi.advanceTimersByTimeAsync(500);
        flushSync();

        expect(subscribeRunEventsMock).toHaveBeenCalledTimes(2);
        const [first, second] = subscribeRunEventsMock.mock.results;
        expect(first.value.close).toHaveBeenCalledTimes(1);
        expect(second.value.close).not.toHaveBeenCalled();
      } finally {
        randomSpy.mockRestore();
      }
    });
  });

  describe('attaching to active Runs', () => {
    it.each([2, 5000])(
      'opens a Session from a complete timeline snapshot at sequence %i regardless of raw replay retention',
      async (sequence) => {
        rpcMock.mockImplementation(
          createChatRpcMock({
            activeRuns: { 'sub-session-1': runningRun('active-sub-run') },
          }),
        );
        await chat.mountChat(
          {
            ...alphaProps(),
            pendingSessionNavigation: {
              agentId: 'alpha',
              sessionId: 'sub-session-1',
              subAgent: true,
            },
          },
          { ready: null },
        );
        await waitForCondition(
          () => subscribeRunEventsMock.mock.calls.length === 1,
        );
        expect(subscribeRunEventsMock).toHaveBeenCalledWith(
          '/api/runs/active-sub-run/timeline?child_limit=100',
          expect.any(Object),
          {
            eventTypes: ['timeline_snapshot', 'timeline_update'],
            closeOnTerminal: false,
          },
        );
        const source = runEventSource('active-sub-run');
        source.emit(
          'timeline_snapshot',
          {
            items: [
              assistantRun('active-sub-run', [
                outputItem('Retained ', { id: 'answer', streaming: true }),
              ]),
            ],
            state: { status: 'running' },
          },
          sequence,
        );
        source.emit('timeline_update', {
          run: { id: 'assistant-run-active-sub-run' },
          child_updates: [{ id: 'answer', append: { content: 'live' } }],
        });
        await waitForText('Retained live');
      },
    );

    it('loads an authoritative timeline when reloading the same displayed Session without opening another stream', async () => {
      const activeRuns = { 'session-1': runningRun('active-parent-run') };
      const sessionItems = {};
      rpcMock.mockImplementation(
        createChatRpcMock({ activeRuns, sessionItems }),
      );
      listedSessions('session-1');
      await chat.mountChat(alphaProps());
      await waitForCondition(
        () => subscribeRunEventsMock.mock.calls.length === 1,
      );
      sessionItems['session-1'] = [
        assistantRun('active-parent-run', [
          outputItem('Recovered draft', { streaming: true }),
        ]),
      ];
      await reopenCurrentSessionFromDrawer();
      await waitForText('Recovered draft');
      expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
    });

    it('attaches to SSE when a Run starts for the displayed Session', async () => {
      rpcMock.mockImplementation(createChatRpcMock());
      const runEvents = createRunEventFeed();
      await chat.mountChat({ ...alphaProps(), runEvents });
      runEvents.append(
        runServerEvent('run_started', {
          sequence: 1,
          run_id: 'pushed-run',
          agent_id: 'alpha',
          session_id: 'session-1',
          status: 'running',
        }),
      );

      await waitForCondition(
        () => subscribeRunEventsMock.mock.calls.length === 1,
      );
      expect(subscribeRunEventsMock).toHaveBeenCalledWith(
        '/api/runs/pushed-run/timeline?child_limit=100',
        expect.any(Object),
        {
          eventTypes: ['timeline_snapshot', 'timeline_update'],
          closeOnTerminal: false,
        },
      );
    });

    it('forwards each new connection snapshot to the run stream once', async () => {
      rpcMock.mockImplementation(createChatRpcMock());
      const parent = createChatViewParentHarness();
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
      parent.setConnectionSnapshot(snapshot);
      await chat.mountChat(parent.props(['connection'], alphaProps()), {
        ready: null,
      });
      await waitForCondition(
        () => applyConnectionSnapshotMock.mock.calls.length === 1,
      );
      expect(applyConnectionSnapshotMock).toHaveBeenLastCalledWith(snapshot);

      parent.setConnectionSnapshot(snapshot);
      flushSync();
      expect(applyConnectionSnapshotMock).toHaveBeenCalledTimes(1);

      const reconnect = { ...snapshot, last_sequence: 43, active_runs: [] };
      parent.setConnectionSnapshot(reconnect);
      flushSync();
      expect(applyConnectionSnapshotMock).toHaveBeenCalledTimes(2);
      expect(applyConnectionSnapshotMock).toHaveBeenLastCalledWith(reconnect);
    });
  });

  describe('History reconciliation', () => {
    it.each([
      ['releases a stuck Run the History no longer reports', false],
      ['keeps a Run the History still reports', true],
    ])('%s', async (_case, stillActive) => {
      const activeRuns = { 'session-1': runningRun('run-stuck') };
      rpcMock.mockImplementation(createChatRpcMock({ activeRuns }));
      listedSessions('session-1');
      await chat.mountChat(alphaProps(), { ready: null });
      await waitForCondition(() => Boolean(findCancelRunButton()));
      expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);

      // The server lost the Run (missed terminal event, rolled bus buffer or
      // a restart) unless it is still active.
      if (!stillActive) delete activeRuns['session-1'];
      await reopenCurrentSessionFromDrawer();
      await waitForCondition(
        () =>
          historyReads('session-1') >= 2 &&
          Boolean(findCancelRunButton()) === stillActive,
      );

      expect(findNewSessionButton().disabled).toBe(false);
      if (stillActive) {
        expect(closeSubscriptionForMock).not.toHaveBeenCalled();
      } else {
        expect(closeSubscriptionForMock).toHaveBeenCalledWith(
          'alpha::session-1',
        );
      }
      // Neither case attaches a second stream.
      expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
    });

    it('does not reset the Session when a new Run starts during the History read', async () => {
      const history = (activeRun) => ({
        session_id: 'session-1',
        messages: [message('assistant-one', 'Hello')],
        has_more: false,
        ...(activeRun ? { active_run: activeRun } : {}),
      });
      let resolveSecondHistory;
      const responses = [
        () => history(runningRun('run-stuck')),
        () =>
          new Promise((resolve) => {
            resolveSecondHistory = resolve;
          }),
        () => history(runningRun('run-replacement')),
      ];
      const baseRpc = createChatRpcMock({ commandItems: [] });
      rpcMock.mockImplementation(async (method, params) =>
        method === 'chat.timeline'
          ? responses[historyReads('session-1') - 1]()
          : baseRpc(method, params),
      );
      listedSessions('session-1');
      await chat.mountChat(alphaProps(), { ready: null });
      await waitForCondition(() => Boolean(findCancelRunButton()));

      await reopenCurrentSessionFromDrawer();
      await waitForCondition(() => historyReads('session-1') >= 2);

      // A new Run legitimately starts before the held response resolves.
      const chatState = testChatStateRefs.at(-1);
      const sessionState = chatState.sessions['alpha::session-1'];
      expect(sessionState.currentRun?.runId).toBe('run-stuck');
      sessionState.currentRun = {
        runId: 'run-replacement',
        sseUrl: '/api/runs/run-replacement/events',
        status: 'running',
      };
      flushSync();

      // History from before the new Run triggers a fresh read, which confirms
      // the new Run before releasing the composer and navigation controls.
      resolveSecondHistory(history(null));
      await waitForCondition(
        () => historyReads('session-1') === 3 && !chatState.loadingHistory,
      );

      expect(sessionState.status).toBe('running');
      expect(sessionState.currentRun?.runId).toBe('run-replacement');
      expect(closeSubscriptionForMock).not.toHaveBeenCalled();
      expect(findCancelRunButton()).toBeTruthy();
      expect(findNewSessionButton().disabled).toBe(false);
    });

    it('re-syncs a held Session Queue on a matching Queue signal only', async () => {
      rpcMock.mockImplementation(createChatRpcMock());
      const parent = createChatViewParentHarness();
      await chat.mountChat(parent.props(['queue'], alphaProps()), {
        ready: null,
      });
      await waitForCondition(() =>
        listQueueMock.mock.calls.some(
          ([agentId, sessionId]) =>
            agentId === 'alpha' && sessionId === 'session-1',
        ),
      );
      const callsBefore = listQueueMock.mock.calls.length;

      parent.setQueueInvalidation({ agentId: 'alpha', sessionId: 'unheld' });
      flushSync();
      expect(listQueueMock).toHaveBeenCalledTimes(callsBefore);

      parent.setQueueInvalidation({ agentId: 'alpha', sessionId: 'session-1' });
      flushSync();
      expect(listQueueMock).toHaveBeenCalledTimes(callsBefore + 1);
      expect(listQueueMock).toHaveBeenLastCalledWith('alpha', 'session-1');
    });
  });

  describe('sub-agent rows', () => {
    it.each([
      [
        'settles a stuck running dot from the child Run summary',
        subSessionHistory({
          messages: [
            message('sub-assistant-original', 'Sub-agent response'),
            {
              id: 'sub-run-summary-1',
              role: 'run_summary',
              run_id: 'child-run',
              status: 'completed',
              timing: { duration_ms: 4200 },
            },
          ],
        }),
        'done',
        '4.2s',
      ],
      [
        'settles a running dot as done when the child History has no trace of the Run',
        subSessionHistory({
          messages: [message('sub-assistant-original', 'Sub-agent response')],
        }),
        'done',
        null,
      ],
      [
        'keeps the dot running while the child Run is active',
        subSessionHistory({ active_run: runningRun('child-run') }),
        'running',
        null,
      ],
    ])(
      '%s and checks each child Run once',
      async (_case, history, dot, time) => {
        serveSubAgentHistory(history);
        const source = await mountRunningSubAgent();

        await settle(3);
        expect(subAgentDot(dot)).not.toBeNull();
        expect(subAgentRow().querySelectorAll('.te-dot')).toHaveLength(1);
        expect(
          subAgentRow().querySelector('.te-time')?.textContent.trim() ?? null,
        ).toBe(time);
        const checks = statusChecks();
        expect(checks).toBeGreaterThanOrEqual(1);

        rerenderTimeline(source, 'call-read');
        await settle(3);
        expect(statusChecks()).toBe(checks);
      },
    );

    it('keeps the dot running after a failed status check and checks again', async () => {
      serveSubAgentHistory(new Error('History unavailable'));
      const source = await mountRunningSubAgent();
      // The row's own automatic check fails first.
      await settle();
      expect(statusChecks()).toBe(1);

      for (let attempt = 0; attempt < 2; attempt += 1) {
        const checks = statusChecks();
        rerenderTimeline(source, `call-read-${attempt}`);
        await settle();
        expect(statusChecks()).toBe(checks + 1);
        expect(subAgentDot('running')).not.toBeNull();
      }
    });

    it('cancels a started child Run through its row with reason user', async () => {
      serveSubAgentHistory(
        subSessionHistory({ active_run: runningRun('child-run') }),
      );
      await mountRunningSubAgent();

      clickSubAgentCancel();
      await waitForCondition(() => cancelRunMock.mock.calls.length === 1);
      expect(cancelRunMock).toHaveBeenCalledWith('child-run', {
        reason: 'user',
      });
    });
  });

  describe('Stop all', () => {
    const labelledButton = (label) =>
      document.querySelector(`button[aria-label="${label}"]`);
    const toastText = () =>
      document.querySelector('.chat-view__command-toast')?.textContent.trim();

    it.each([
      [0, 'chat.stopAllNothing'],
      [3, 'chat.stopAllDone'],
    ])(
      'stops all work of the Session from the Stop menu and reports %i stopped',
      async (stopped, notice) => {
        const fallback = createChatRpcMock({
          streamResponse: runningRun('run-parent'),
        });
        rpcMock.mockImplementation(async (method, params) =>
          method === 'chat.stop_all'
            ? { ok: true, stopped }
            : fallback(method, params),
        );
        await chat.mountChat(alphaProps(), { ready: null });
        await startRun('run-parent', 'Start a long run');
        await waitForCondition(() =>
          Boolean(labelledButton(t('chat.stopOptions'))),
        );

        labelledButton(t('chat.stopOptions')).click();
        flushSync();
        Array.from(document.querySelectorAll('[role="menuitem"]'))
          .find((item) => item.textContent.includes(t('chat.stopAll')))
          .click();

        await waitForCondition(() => toastText() === t(notice));
        expect(rpcCalls('chat.stop_all')).toEqual([
          { agent_id: 'alpha', session_id: 'session-1' },
        ]);
        expect(cancelRunMock).not.toHaveBeenCalled();
      },
    );

    it('offers Stop all for authoritative work absent from History and refreshes on lifecycle changes', async () => {
      const fallback = createChatRpcMock();
      let running = true;
      rpcMock.mockImplementation(async (method, params) => {
        if (method === 'chat.work')
          return { run_active: false, background_work_running: running };
        if (method === 'chat.stop_all') {
          running = false;
          return { stopped: 1 };
        }
        return fallback(method, params);
      });
      const feed = createRunEventFeed();
      const props = reactiveProps({
        ...alphaProps(),
        runEvents: feed,
        sessionActivity: null,
      });
      await chat.mountChat(props);
      await waitForCondition(() => Boolean(labelledButton(t('chat.stopAll'))));
      expect(findCancelRunButton()).toBeUndefined();
      labelledButton(t('chat.stopAll')).click();
      await waitForCondition(() => labelledButton(t('chat.stopAll')) === null);

      // An independently launched Terminal needs no Tool row in this History.
      running = true;
      props.terminalsRefreshToken = 1;
      await waitForCondition(() => Boolean(labelledButton(t('chat.stopAll'))));
      running = false;
      props.commandStatuses = {
        cmd_outside_page: { status: 'completed', sessionId: 'session-1' },
      };
      await waitForCondition(() => labelledButton(t('chat.stopAll')) === null);
      running = true;
      props.queueInvalidation = {
        agentId: 'worker',
        sessionId: 'child-outside-page',
      };
      await waitForCondition(() => Boolean(labelledButton(t('chat.stopAll'))));
      running = false;
      feed.append(
        runServerEvent('run_completed', {
          run_id: 'run-unloaded',
          agent_id: 'worker',
          session_id: 'child-outside-page',
          sequence: 2,
        }),
      );
      await waitForCondition(() => labelledButton(t('chat.stopAll')) === null);
      running = true;
      props.connectionSnapshot = {
        active_runs: [],
        epoch: 'new',
        last_sequence: 1,
      };
      await waitForCondition(() => Boolean(labelledButton(t('chat.stopAll'))));

      // A Session waiting for work that will wake it offers Stop all too.
      running = false;
      props.terminalsRefreshToken = 2;
      await waitForCondition(() => labelledButton(t('chat.stopAll')) === null);
      props.sessionActivity = sessionActivityFromSnapshot([
        {
          project_id: null,
          agent_id: 'alpha',
          session_id: 'session-1',
          state: 'waiting',
          work_started_at: '2026-05-09T00:00:00+00:00',
          waiting_for: { subagents: 0, commands: 0, results: 1 },
        },
      ]);
      await waitForCondition(() => Boolean(labelledButton(t('chat.stopAll'))));
      props.sessionActivity = {};
      await waitForCondition(() => labelledButton(t('chat.stopAll')) === null);
    });

    const finishedChild = subSessionHistory({
      messages: [
        {
          id: 'sub-run-summary',
          role: 'run_summary',
          run_id: 'child-run',
          status: 'completed',
        },
      ],
    });
    it.each([
      [
        'offers',
        'running',
        subSessionHistory({ active_run: runningRun('child-run') }),
        [],
      ],
      // A command the finished Sub-Agent started still runs.
      [
        'offers',
        'done',
        finishedChild,
        [
          {
            subagent_id: 'sub-worker',
            session_id: 'sub-session-1',
            command_id: 'cmd_dev',
            label: 'npm run dev',
          },
        ],
      ],
      ['does not offer', 'done', finishedChild, []],
    ])(
      '%s Stop all without a Run by whether work of a Sub-Agent of the Session runs (Run dot %s)',
      async (offers, dot, childHistory, subAgentCommands) => {
        const fallback = createChatRpcMock({
          sessionItems: {
            'session-1': [
              assistantRun(
                'delegation',
                [
                  toolItem({
                    id: 'call-worker',
                    name: 'subagent',
                    arguments: {
                      action: 'run',
                      agent_id: 'alpha',
                      content: 'Do the work',
                    },
                    result: {
                      ok: true,
                      data: {
                        agent_id: 'alpha',
                        session_id: 'sub-session-1',
                        run_id: 'child-run',
                        status: 'running',
                      },
                    },
                  }),
                  outputItem('Hello'),
                ],
                { source: 'history', status: 'completed' },
              ),
            ],
          },
          sessionMessages: {
            'session-1': [
              message('user-1', 'Delegate the work', 'user'),
              {
                id: 'assistant-spawn',
                role: 'assistant',
                content: null,
                tool_calls: [
                  {
                    id: 'call-worker',
                    name: 'subagent',
                    arguments: {
                      action: 'run',
                      agent_id: 'alpha',
                      content: 'Do the work',
                    },
                  },
                ],
              },
              {
                id: 'spawn-result',
                role: 'tool',
                tool_call_id: 'call-worker',
                name: 'subagent',
                content: JSON.stringify({
                  ok: true,
                  data: {
                    agent_id: 'alpha',
                    session_id: 'sub-session-1',
                    run_id: 'child-run',
                    status: 'running',
                  },
                }),
              },
              message('assistant-one', 'Hello'),
            ],
          },
        });
        rpcMock.mockImplementation(async (method, params) => {
          if (method === 'chat.work')
            return {
              run_active: false,
              background_work_running: offers === 'offers',
            };
          if (method !== 'chat.timeline') return fallback(method, params);
          if (params?.session_id === 'sub-session-1') return childHistory;
          return {
            ...(await fallback(method, params)),
            subagent_background_commands: subAgentCommands,
          };
        });
        await chat.mountChat(alphaProps());
        await waitForCondition(() => statusChecks() === 1);
        await settle();

        expect(findCancelRunButton()).toBeUndefined();
        expect(subAgentDot(dot)).not.toBeNull();
        if (offers === 'offers') {
          labelledButton(t('chat.stopAll')).click();
          await waitForCondition(() => rpcCalls('chat.stop_all').length === 1);
        } else {
          expect(labelledButton(t('chat.stopAll'))).toBeNull();
        }
      },
    );
  });
});
