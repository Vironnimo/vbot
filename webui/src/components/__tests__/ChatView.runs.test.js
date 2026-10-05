// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import {
  applyConnectionSnapshotMock,
  cancelRunMock,
  closeSubscriptionForMock,
  createAgent,
  createChatRpcMock,
  findButtonByText,
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
} from './ChatView.support.js';
import { createChatViewParentHarness } from './ChatView.parent.support.svelte.js';
import { t } from '../../lib/i18n.js';

const alphaProps = () => ({
  sharedAgents: [createAgent()],
  sharedSelectedAgentId: 'alpha',
});

const subAgentRow = () => document.querySelector('.subagent-tool-event');
const subAgentDot = (status) =>
  subAgentRow()?.querySelector(`.te-dot.${status}`) ?? null;

async function reopenCurrentSessionFromDrawer() {
  findButtonByText('Sessions').click();
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
  return runEventSource(runId);
}

// Emits a `subagent` run into the running parent Run: the tool call, its
// child Session start and the run result, all carrying `spawn`.
function emitSubAgentSpawn(source, callId, spawn, { afterStart } = {}) {
  const toolCall = { id: callId, index: 0, name: 'subagent' };
  source.emit('tool_call_started', {
    tool_call: {
      ...toolCall,
      arguments: {
        action: 'run',
        agent_id: 'alpha',
        content: 'Inspect the project',
      },
    },
  });
  source.emit('subagent_session_started', { tool_call: toolCall, data: spawn });
  flushSync();
  afterStart?.();
  source.emit('tool_call_result', {
    tool_call: toolCall,
    result: JSON.stringify({ ok: true, data: spawn }),
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
    if (method !== 'chat.history' || params?.session_id !== 'sub-session-1') {
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
  rpcCalls('chat.history').filter(
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
    source.emit('tool_call_started', {
      tool_call: {
        id: callId,
        index: 0,
        name: 'read',
        arguments: { path: 'notes.md' },
      },
    });
    flushSync();
  }

  const clickSubAgentCancel = () =>
    subAgentRow().querySelector('[data-cancel="subagent"]').click();

  describe('run stream', () => {
    it('applies a live command status without a reactive update loop', async () => {
      rpcMock.mockImplementation(createChatRpcMock());
      await chat.mountChat({ commandStatuses: { term_test: 'completed' } });

      const input = document.querySelector('textarea');
      setInputValue(input, 'Continue after completion');
      flushSync();
      expect(input.value).toBe('Continue after completion');
      expect(
        document.querySelector('[aria-label="Send message"]').disabled,
      ).toBe(false);
    });

    it('batches run SSE deltas before updating the rendered timeline', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({ streamResponse: runningRun('run-batched') }),
      );
      await chat.mountChat({}, { ready: null });
      const source = await startRun('run-batched', 'Start batched stream');

      source.emit('reasoning_delta', { reasoning_delta: 'Think ' });
      source.emit('reasoning_delta', { reasoning_delta: 'fast' });
      flushSync();
      expect(document.body.textContent).not.toContain('Think fast');

      await waitForText('Think fast');
    });

    it('forwards a live bash row background action to the Run control RPC', async () => {
      rpcMock.mockImplementation(
        createChatRpcMock({ streamResponse: runningRun('run-bash') }),
      );
      await chat.mountChat({}, { ready: null });
      const source = await startRun('run-bash', 'Run a long command');

      source.emit('tool_call_started', {
        tool_call: {
          id: 'call-bash',
          index: 0,
          name: 'bash',
          arguments: { command: 'sleep 60' },
        },
      });
      source.emit('run_controls_changed', {
        compaction: 'unavailable',
        background_tool_call_ids: ['call-bash'],
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
      rpcMock.mockImplementation(
        createChatRpcMock({ streamResponse: runningRun('run-reconnect') }),
      );
      await chat.mountChat({}, { ready: null });
      const { handlers } = await startRun(
        'run-reconnect',
        'Start reconnecting stream',
      );

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
    it.each([
      [
        'resumes after a complete retained prefix',
        [
          { type: 'run_started', sequence: 1, payload: { status: 'running' } },
          {
            type: 'assistant_output_delta',
            sequence: 2,
            payload: { content_delta: 'Retained ' },
          },
        ],
        2,
      ],
      [
        'replays from the start when the replay prefix was evicted',
        [
          {
            type: 'assistant_output_delta',
            sequence: 5_000,
            payload: { content_delta: 'Retained ' },
          },
        ],
        0,
      ],
    ])(
      'keeps an opened Session live and %s',
      async (_case, retainedEvents, afterSequence) => {
        const events = retainedEvents.map((event) => ({
          ...event,
          run_id: 'active-sub-run',
          agent_id: 'alpha',
          session_id: 'sub-session-1',
        }));
        rpcMock.mockImplementation(
          createChatRpcMock({
            activeRuns: {
              'sub-session-1': runningRun('active-sub-run', events),
            },
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
          '/api/runs/active-sub-run/events',
          expect.any(Object),
          { afterSequence },
        );

        runEventSource('active-sub-run').emit(
          'assistant_output_delta',
          { content_delta: 'live' },
          retainedEvents.at(-1).sequence + 1,
        );
        await waitForText('Retained live');
      },
    );

    it('merges retained active-run events when reloading the same displayed Session', async () => {
      const retained = (sequence, type, payload) => ({
        type,
        run_id: 'active-parent-run',
        agent_id: 'alpha',
        session_id: 'session-1',
        sequence,
        payload,
      });
      const activeRuns = {
        'session-1': runningRun('active-parent-run', [
          retained(1, 'run_started', { status: 'running' }),
        ]),
      };
      rpcMock.mockImplementation(createChatRpcMock({ activeRuns }));
      listedSessions('session-1');
      await chat.mountChat(alphaProps());
      await waitForCondition(
        () => subscribeRunEventsMock.mock.calls.length === 1,
      );

      activeRuns['session-1'] = runningRun('active-parent-run', [
        ...activeRuns['session-1'].events,
        retained(2, 'assistant_output_delta', { content_delta: 'Recovered ' }),
        retained(3, 'assistant_output_delta', { content_delta: 'draft' }),
      ]);
      await reopenCurrentSessionFromDrawer();

      await waitForText('Recovered draft');
      expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
    });

    it('attaches to SSE when a Run starts for the displayed Session', async () => {
      rpcMock.mockImplementation(createChatRpcMock());
      await chat.mountChat(
        {
          ...alphaProps(),
          runServerEvent: runServerEvent('run_started', {
            sequence: 1,
            run_id: 'pushed-run',
            agent_id: 'alpha',
            session_id: 'session-1',
            status: 'running',
          }),
        },
        { ready: null },
      );

      await waitForCondition(
        () => subscribeRunEventsMock.mock.calls.length === 1,
      );
      expect(subscribeRunEventsMock).toHaveBeenCalledWith(
        '/api/runs/pushed-run/events',
        expect.any(Object),
        { afterSequence: 1 },
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
        method === 'chat.history'
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

    it.each([
      ['offers', subSessionHistory({ active_run: runningRun('child-run') })],
      [
        'does not offer',
        subSessionHistory({
          messages: [
            {
              id: 'sub-run-summary',
              role: 'run_summary',
              run_id: 'child-run',
              status: 'completed',
            },
          ],
        }),
      ],
    ])(
      '%s Stop all without a Run by whether a Sub-Agent of the Session runs',
      async (offers, childHistory) => {
        const fallback = createChatRpcMock({
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
        rpcMock.mockImplementation(async (method, params) =>
          method === 'chat.history' && params?.session_id === 'sub-session-1'
            ? childHistory
            : fallback(method, params),
        );
        await chat.mountChat(alphaProps());
        await waitForCondition(() => statusChecks() === 1);
        await settle();

        expect(findCancelRunButton()).toBeUndefined();
        if (offers === 'offers') {
          expect(subAgentDot('running')).not.toBeNull();
          labelledButton(t('chat.stopAll')).click();
          await waitForCondition(() => rpcCalls('chat.stop_all').length === 1);
        } else {
          expect(subAgentDot('done')).not.toBeNull();
          expect(labelledButton(t('chat.stopAll'))).toBeNull();
        }
      },
    );
  });
});
