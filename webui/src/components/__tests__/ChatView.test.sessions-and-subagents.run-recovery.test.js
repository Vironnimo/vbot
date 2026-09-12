// @vitest-environment jsdom
import {
  describe,
  applyConnectionSnapshotMock,
  closeSubscriptionForMock,
  createAgent,
  createChatRpcMock,
  expect,
  findButtonByText,
  findCancelRunButton,
  findNewSessionButton,
  flushSync,
  it,
  listSessionsMock,
  rpcMock,
  subscribeRunEventsMock,
  testChatStateRefs,
  waitForCondition,
} from './ChatView.support.js';
import { setupChatSessionNavigationSuite } from './ChatView.sessions-and-subagents.support.js';

describe('ChatView', () => {
  const suite = setupChatSessionNavigationSuite();

  it('applies a non-null connectionSnapshot prop to the run stream', async () => {
    const { createChatViewConnectionSnapshotHarness } =
      await import('./chatViewConnectionSnapshotHarness.svelte.js');
    const harness = createChatViewConnectionSnapshotHarness();
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
    harness.setConnectionSnapshot(snapshot);

    suite.chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        get connectionSnapshot() {
          return harness.connectionSnapshot;
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => applyConnectionSnapshotMock.mock.calls.length === 1,
      100,
    );

    expect(applyConnectionSnapshotMock).toHaveBeenCalledTimes(1);
    expect(applyConnectionSnapshotMock).toHaveBeenCalledWith(snapshot);
  });

  it('does not re-apply the same connectionSnapshot reference (dedup)', async () => {
    const { createChatViewConnectionSnapshotHarness } =
      await import('./chatViewConnectionSnapshotHarness.svelte.js');
    const harness = createChatViewConnectionSnapshotHarness();
    const snapshot = {
      type: 'connection_ready',
      epoch: 'bus-epoch-1',
      last_sequence: 42,
      active_runs: [],
    };
    harness.setConnectionSnapshot(snapshot);

    suite.chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        get connectionSnapshot() {
          return harness.connectionSnapshot;
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => applyConnectionSnapshotMock.mock.calls.length === 1,
      100,
    );

    // Re-assign the harness to the same snapshot object. Svelte 5's `$state`
    // setter no-ops for the same reference, but the test still documents the
    // dedup contract: even if the effect re-runs for the same reference, the
    // call must not happen again.
    harness.setConnectionSnapshot(snapshot);
    flushSync();

    expect(applyConnectionSnapshotMock).toHaveBeenCalledTimes(1);
  });

  it('reconciles a stuck running session when chat.history reports no active_run (B3 regression)', async () => {
    const activeRuns = {
      'session-1': {
        run_id: 'run-stuck',
        sse_url: '/api/runs/run-stuck/events',
        status: 'running',
        events: [],
      },
    };
    rpcMock.mockImplementation(createChatRpcMock({ activeRuns }));
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'session-1',
          created_at: '2026-05-10T00:00:00+00:00',
          last_active_at: '2026-05-10T00:01:00+00:00',
        },
      ],
    });

    suite.chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    // Initial mount attaches the SSE stream for the active run from history.
    await waitForCondition(() => Boolean(findCancelRunButton()), 100);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);

    // The server has lost the run (terminal event missed, bus buffer rolled,
    // or server restarted and the run is gone) — clear `activeRuns` so the
    // next `chat.history` response no longer carries an `active_run`.
    delete activeRuns['session-1'];

    // Trigger a second `loadHistoryForSession` via the sessions drawer.
    findButtonByText('Sessions')?.click();
    await waitForCondition(
      () => Boolean(document.querySelector('.session-row__select')),
      100,
    );
    document.querySelector('.session-row__select')?.click();

    // Reconcile: the "Cancel run" button disappears and the run stream's
    // `closeSubscriptionFor` was called for this session key.
    await waitForCondition(() => findCancelRunButton() === undefined, 100);

    expect(findCancelRunButton()).toBeUndefined();
    expect(findNewSessionButton()?.disabled).toBe(false);
    expect(closeSubscriptionForMock).toHaveBeenCalledWith('alpha::session-1');
    // No new SSE attach — the dead run is gone, not replaced.
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('keeps the session running when chat.history still reports the same active_run', async () => {
    const activeRuns = {
      'session-1': {
        run_id: 'run-stuck',
        sse_url: '/api/runs/run-stuck/events',
        status: 'running',
        events: [],
      },
    };
    rpcMock.mockImplementation(createChatRpcMock({ activeRuns }));
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'session-1',
          created_at: '2026-05-10T00:00:00+00:00',
          last_active_at: '2026-05-10T00:01:00+00:00',
        },
      ],
    });

    suite.chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    await waitForCondition(() => Boolean(findCancelRunButton()), 100);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);

    // `chat.history` still reports the same active run — the active_run is
    // present on the second call, so no reconcile must fire.
    findButtonByText('Sessions')?.click();
    await waitForCondition(
      () => Boolean(document.querySelector('.session-row__select')),
      100,
    );
    document.querySelector('.session-row__select')?.click();

    // `attachRunStream` runs again via `runStream.attachRunStream(...)` for
    // the second history load. The run is still the same id, so the
    // `alreadySubscribed` dedup inside `attachRunStream` prevents a
    // redundant SSE attach — `subscribeRunEvents` count stays at 1.
    await waitForCondition(
      () =>
        rpcMock.mock.calls.filter(
          ([method, params]) =>
            method === 'chat.history' && params?.session_id === 'session-1',
        ).length >= 2,
      100,
    );

    expect(findCancelRunButton()).toBeTruthy();
    expect(findNewSessionButton()?.disabled).toBe(false);
    expect(closeSubscriptionForMock).not.toHaveBeenCalled();
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('does not reset the session when currentRun.runId changes during the chat.history await', async () => {
    // First history call: returns the running run so the session mounts in a
    // running state with `currentRun.runId = 'run-stuck'`.
    const initialActiveRuns = {
      'session-1': {
        run_id: 'run-stuck',
        sse_url: '/api/runs/run-stuck/events',
        status: 'running',
        events: [],
      },
    };

    // Second history call: returns no `active_run`. Held on a deferred so
    // the test can mutate `currentRun.runId` between the request and the
    // response — the exact race the `staleRunId` guard exists for.
    let resolveSecondHistory;
    const secondHistoryDeferred = new Promise((resolve) => {
      resolveSecondHistory = resolve;
    });
    let chatHistoryCallCount = 0;

    rpcMock.mockImplementation(async (method, params) => {
      if (method === 'agent.list') {
        return { agents: [createAgent()] };
      }
      if (method === 'chat.history') {
        chatHistoryCallCount += 1;
        if (chatHistoryCallCount === 1) {
          return {
            session_id: params.session_id,
            messages: [
              {
                id: 'assistant-one',
                role: 'assistant',
                content: 'Hello',
              },
            ],
            has_more: false,
            active_run: initialActiveRuns[params.session_id],
          };
        }
        if (chatHistoryCallCount === 2) {
          return secondHistoryDeferred;
        }
        return {
          session_id: params.session_id,
          messages: [
            { id: 'assistant-one', role: 'assistant', content: 'Hello' },
          ],
          has_more: false,
          active_run: {
            run_id: 'run-replacement',
            sse_url: '/api/runs/run-replacement/events',
            status: 'running',
          },
        };
      }
      if (method === 'chat.commands') {
        return { items: [] };
      }
      throw new Error(`Unexpected RPC method: ${method}`);
    });
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'session-1',
          created_at: '2026-05-10T00:00:00+00:00',
          last_active_at: '2026-05-10T00:01:00+00:00',
        },
      ],
    });

    suite.chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    // Wait for the first `chat.history` response to land and the SSE stream
    // to be attached for the initial active run.
    await waitForCondition(() => Boolean(findCancelRunButton()), 100);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);

    // Trigger a second `loadHistoryForSession`. This call is held on the
    // deferred so we can race in a state mutation before it resolves.
    findButtonByText('Sessions')?.click();
    await waitForCondition(
      () => Boolean(document.querySelector('.session-row__select')),
      100,
    );
    document.querySelector('.session-row__select')?.click();

    // Wait for the second `chat.history` call to be in flight.
    await waitForCondition(() => chatHistoryCallCount >= 2, 100);

    // Race: a *new* run legitimately starts before the deferred response
    // resolves. Simulate by mutating the live session state to a different
    // `runId` than the one the loader captured as `staleRunId`.
    const chatState = testChatStateRefs.at(-1);
    const sessionState = chatState.sessions['alpha::session-1'];
    expect(sessionState.currentRun?.runId).toBe('run-stuck');
    sessionState.currentRun = {
      runId: 'run-replacement',
      sseUrl: '/api/runs/run-replacement/events',
      status: 'running',
    };
    flushSync();

    // Resolve the deferred with no `active_run` — history is unaware of the
    // brand-new run (it started after the request was sent).
    resolveSecondHistory({
      session_id: 'session-1',
      messages: [
        {
          id: 'assistant-one',
          role: 'assistant',
          content: 'Hello',
        },
      ],
      has_more: false,
    });
    // The obsolete snapshot triggers a fresh read, which confirms the new
    // Run before releasing the composer and navigation controls.
    await waitForCondition(
      () => chatHistoryCallCount === 3 && !chatState.loadingHistory,
      100,
    );
    flushSync();

    // Guard fired: `staleRunId === 'run-stuck'` and the live
    // `currentRun.runId === 'run-replacement'`, so the reset branch did
    // not run. The session is still in the running state and the
    // `closeSubscriptionFor` reconcile hook was not called.
    expect(sessionState.status).toBe('running');
    expect(sessionState.currentRun?.runId).toBe('run-replacement');
    expect(closeSubscriptionForMock).not.toHaveBeenCalled();
    expect(findCancelRunButton()).toBeTruthy();
    expect(findNewSessionButton()?.disabled).toBe(false);
  });
});
