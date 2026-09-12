// @vitest-environment jsdom
import {
  describe,
  cancelRunMock,
  createAgent,
  createChatRpcMock,
  expect,
  flushSync,
  it,
  listQueueMock,
  removeFromQueueMock,
  rpcMock,
  waitForCondition,
} from './ChatView.support.js';
import { setupChatSessionNavigationSuite } from './ChatView.sessions-and-subagents.support.js';

describe('ChatView', () => {
  const suite = setupChatSessionNavigationSuite();

  it('verifySubAgentStatus: settles a stuck running sub-agent dot from a run_summary in chat.history (B5 regression)', async () => {
    rpcMock.mockImplementation(
      suite.createVerifyRpcMock({
        subSessionHistory: {
          session_id: 'sub-session-1',
          messages: [
            {
              id: 'sub-assistant-original',
              role: 'assistant',
              content: 'Sub-agent response',
            },
            {
              id: 'sub-run-summary-1',
              role: 'run_summary',
              run_id: 'verify-run',
              status: 'completed',
              timing: { duration_ms: 4200 },
            },
          ],
          has_more: false,
        },
      }),
    );

    await suite.mountChatViewWithRunningSubAgent();

    // The verification call hits the public exported method (same one
    // the future `onVerifySubAgentStatus` callback chain will invoke).
    await suite.chatViewTest.mountedComponent.verifySubAgentStatus(
      'alpha',
      'sub-session-1',
      'verify-run',
    );
    flushSync();

    // Dot settled to "done" (status "completed" → dot "success") and the
    // child duration rendered in the time label.
    const settledRow = document.querySelector('.subagent-tool-event');
    expect(settledRow).not.toBeNull();
    expect(settledRow?.querySelector('.te-dot.running')).toBeNull();
    expect(settledRow?.querySelector('.te-dot.done')).not.toBeNull();
    expect(settledRow?.querySelector('.te-time')?.textContent?.trim()).toBe(
      '4.2s',
    );

    // The verify path targeted the right RPC (at least one verify
    // round-trip; the row's settled "success" dot also triggers the
    // existing `requestSubAgentResult` lookup, so more than one call is
    // expected and acceptable).
    const verifyHistoryCalls = rpcMock.mock.calls.filter(
      ([method, params]) =>
        method === 'chat.history' &&
        params?.session_id === 'sub-session-1' &&
        params?.limit === 20,
    );
    expect(verifyHistoryCalls.length).toBeGreaterThanOrEqual(1);
  });

  it('verifySubAgentStatus: keeps the dot running when chat.history reports an active_run, with a once-per-key guard', async () => {
    rpcMock.mockImplementation(
      suite.createVerifyRpcMock({
        subSessionHistory: {
          session_id: 'sub-session-1',
          messages: [],
          has_more: false,
          active_run: {
            run_id: 'verify-run',
            sse_url: '/api/runs/verify-run/events',
            status: 'running',
            events: [],
          },
        },
      }),
    );

    await suite.mountChatViewWithRunningSubAgent();

    // First call: chat.history returns active_run → dot stays "running".
    await suite.chatViewTest.mountedComponent.verifySubAgentStatus(
      'alpha',
      'sub-session-1',
      'verify-run',
    );
    flushSync();

    const stillRunningRow = document.querySelector('.subagent-tool-event');
    expect(stillRunningRow).not.toBeNull();
    expect(stillRunningRow?.querySelector('.te-dot.running')).not.toBeNull();
    expect(stillRunningRow?.querySelector('.te-dot.done')).toBeNull();

    const historyCallCountAfterFirst = rpcMock.mock.calls.filter(
      ([method, params]) =>
        method === 'chat.history' &&
        params?.session_id === 'sub-session-1' &&
        params?.limit === 20,
    ).length;

    // Second call with the same key: the once-per-key guard must short-
    // circuit and not issue a second `chat.history` round-trip.
    await suite.chatViewTest.mountedComponent.verifySubAgentStatus(
      'alpha',
      'sub-session-1',
      'verify-run',
    );
    flushSync();

    const historyCallCountAfterSecond = rpcMock.mock.calls.filter(
      ([method, params]) =>
        method === 'chat.history' &&
        params?.session_id === 'sub-session-1' &&
        params?.limit === 20,
    ).length;

    expect(historyCallCountAfterSecond).toBe(historyCallCountAfterFirst);

    // The dot is still running — the verify path did not flip it to
    // "done".
    const finalRow = document.querySelector('.subagent-tool-event');
    expect(finalRow?.querySelector('.te-dot.running')).not.toBeNull();
    expect(finalRow?.querySelector('.te-dot.done')).toBeNull();
  });

  it('cancelSubAgent: cancels a directly started child run through chat.cancel with reason user', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    suite.chatViewTest.mount({
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    await suite.chatViewTest.mountedComponent.cancelSubAgent({
      name: 'subagent',
      status: 'success',
      arguments: { agent_id: 'alpha', content: 'Inspect the project' },
      result: JSON.stringify({
        ok: true,
        error: null,
        data: {
          agent_id: 'alpha',
          session_id: 'sub-session-1',
          run_id: 'child-run-7',
          status: 'running',
        },
        artifacts: [],
      }),
    });

    expect(cancelRunMock).toHaveBeenCalledWith('child-run-7', {
      reason: 'user',
    });
    expect(removeFromQueueMock).not.toHaveBeenCalled();
  });

  it('cancelSubAgent: removes a still-queued child and settles the dot to cancelled', async () => {
    rpcMock.mockImplementation(
      suite.createVerifyRpcMock({
        subSessionHistory: {
          session_id: 'sub-session-1',
          messages: [],
          has_more: false,
        },
      }),
    );
    // The queue item still waits, so the automatic verification keeps the
    // row running instead of settling it.
    listQueueMock.mockImplementation(async (agentId, sessionId) =>
      sessionId === 'sub-session-1'
        ? {
            items: [
              { id: 'queue-item-1', content: 'Inspect', internal: false },
            ],
          }
        : { items: [] },
    );

    await suite.mountChatViewWithQueuedSubAgent();
    await waitForCondition(
      () =>
        document.querySelector('.subagent-tool-event .te-dot.running') !== null,
      100,
    );

    await suite.chatViewTest.mountedComponent.cancelSubAgent(
      suite.queuedSpawnToolFixture(),
    );
    flushSync();

    // `chat.queue_remove` keys on the BARE child agent id (trap 2).
    expect(removeFromQueueMock).toHaveBeenCalledWith(
      'alpha',
      'sub-session-1',
      'queue-item-1',
    );
    expect(cancelRunMock).not.toHaveBeenCalled();
    // Nothing else will ever report the never-started child, so the cancel
    // settles the row immediately.
    const row = document.querySelector('.subagent-tool-event');
    expect(row?.querySelector('.te-dot.cancelled')).not.toBeNull();
  });

  it('cancelSubAgent: falls back to the child session active run when the queue item is already consumed', async () => {
    rpcMock.mockImplementation(
      suite.createVerifyRpcMock({
        subSessionHistory: {
          session_id: 'sub-session-1',
          messages: [],
          has_more: false,
          active_run: {
            run_id: 'child-active-run',
            sse_url: '/api/runs/child-active-run/events',
            status: 'running',
            events: [],
          },
        },
      }),
    );
    // The formerly queued child started server-side; its queue item is gone
    // and (post-reload) no queueRun mapping survived in this tab.
    removeFromQueueMock.mockRejectedValue(
      Object.assign(new Error('queued item not found: queue-item-1'), {
        code: 'queue_item_not_found',
      }),
    );

    await suite.mountChatViewWithQueuedSubAgent();
    await waitForCondition(
      () =>
        document.querySelector('.subagent-tool-event .te-dot.running') !== null,
      100,
    );

    await suite.chatViewTest.mountedComponent.cancelSubAgent(
      suite.queuedSpawnToolFixture(),
    );
    flushSync();

    expect(cancelRunMock).toHaveBeenCalledWith('child-active-run', {
      reason: 'user',
    });
    const row = document.querySelector('.subagent-tool-event');
    expect(row?.querySelector('.te-dot.cancelled')).not.toBeNull();
  });

  it('verifySubAgentStatus: keeps a queued spawn running while its queue item is still pending', async () => {
    rpcMock.mockImplementation(
      suite.createVerifyRpcMock({
        subSessionHistory: {
          session_id: 'sub-session-1',
          messages: [],
          has_more: false,
        },
      }),
    );
    listQueueMock.mockImplementation(async (agentId, sessionId) =>
      sessionId === 'sub-session-1'
        ? {
            items: [
              { id: 'queue-item-1', content: 'Inspect', internal: false },
            ],
          }
        : { items: [] },
    );

    await suite.mountChatViewWithQueuedSubAgent();

    // The automatic run-id-less verification consulted the child queue…
    await waitForCondition(
      () =>
        listQueueMock.mock.calls.some(
          ([agentId, sessionId]) =>
            agentId === 'alpha' && sessionId === 'sub-session-1',
        ),
      100,
    );
    flushSync();

    // …and kept the dot running instead of settling "no trace" as success.
    const row = document.querySelector('.subagent-tool-event');
    expect(row?.querySelector('.te-dot.running')).not.toBeNull();
    expect(row?.querySelector('.te-dot.done')).toBeNull();
    expect(row?.querySelector('.te-dot.cancelled')).toBeNull();
  });

  it('verifySubAgentStatus: settles a never-started queued spawn to cancelled once its queue item is gone', async () => {
    rpcMock.mockImplementation(
      suite.createVerifyRpcMock({
        subSessionHistory: {
          session_id: 'sub-session-1',
          messages: [],
          has_more: false,
        },
      }),
    );
    // Default `listQueueMock`: empty queue — the item is gone and no run ever
    // started, so the spawn was cancelled before start (not "completed").

    await suite.mountChatViewWithQueuedSubAgent();

    await waitForCondition(
      () =>
        document.querySelector('.subagent-tool-event .te-dot.cancelled') !==
        null,
      100,
    );
    const row = document.querySelector('.subagent-tool-event');
    expect(row?.querySelector('.te-dot.done')).toBeNull();
  });
});
