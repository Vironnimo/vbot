// @vitest-environment jsdom
import {
  createAgent,
  createChatRpcMock,
  expect,
  flushSync,
  sendComposerMessage,
  setupChatViewTestSuite,
  subscribeRunEventsMock,
  waitForCondition,
} from './ChatView.support.js';

function setupChatSessionNavigationSuite() {
  const chatViewTest = setupChatViewTestSuite();
  // Helper: render a single running sub-agent tool row in the parent
  // timeline (mirrors the fast-subagent test setup). The caller is
  // responsible for installing an `rpcMock.mockImplementation` first; this
  // helper does NOT overwrite it (the verify tests pass a custom mock that
  // must keep responding after the mount completes).
  async function mountChatViewWithRunningSubAgent() {
    chatViewTest.mount({
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

    sendComposerMessage('Spawn background sub-agent');

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    const handlers = subscribeRunEventsMock.mock.calls[0][1];
    handlers.onEvent({
      data: {
        type: 'tool_call_started',
        run_id: 'run-verify-1',
        sequence: 1,
        payload: {
          tool_call: {
            id: 'call-verify-1',
            index: 0,
            name: 'subagent',
            arguments: {
              agent_id: 'alpha',
              background: true,
              content: 'Inspect the project',
            },
          },
        },
      },
    });
    handlers.onEvent({
      data: {
        type: 'subagent_session_started',
        run_id: 'run-verify-1',
        sequence: 2,
        payload: {
          tool_call: {
            id: 'call-verify-1',
            index: 0,
            name: 'subagent',
          },
          data: {
            agent_id: 'alpha',
            session_id: 'sub-session-1',
            run_id: 'verify-run',
            status: 'running',
          },
        },
      },
    });
    handlers.onEvent({
      data: {
        type: 'tool_call_result',
        run_id: 'run-verify-1',
        sequence: 3,
        payload: {
          tool_call: {
            id: 'call-verify-1',
            index: 0,
            name: 'subagent',
          },
          result: JSON.stringify({
            ok: true,
            data: {
              agent_id: 'alpha',
              session_id: 'sub-session-1',
              run_id: 'verify-run',
              status: 'running',
            },
          }),
        },
      },
    });
    flushSync();

    // The sub-agent row is in the parent timeline, dot still "running"
    // because the frozen persisted descriptor says so.
    const runningRow = document.querySelector('.subagent-tool-event');
    expect(runningRow).not.toBeNull();
    expect(runningRow?.querySelector('.te-dot.running')).not.toBeNull();
    return runningRow;
  }
  // Custom RPC mock factory for the sub-agent verification tests. The
  // default `createChatRpcMock` returns plain assistant messages for
  // `sub-session-1`; the verify path needs to see a `run_summary` (or an
  // `active_run`) in the response. The test passes the response override
  // for `sub-session-1`; everything else falls through to the default
  // behaviour.
  function createVerifyRpcMock({ subSessionHistory }) {
    const fallback = createChatRpcMock({
      streamResponse: {
        run_id: 'run-verify-1',
        sse_url: '/api/runs/run-verify-1/events',
        status: 'running',
        events: [],
      },
    });
    return async (method, params) => {
      if (method === 'chat.history' && params?.session_id === 'sub-session-1') {
        return subSessionHistory;
      }
      return fallback(method, params);
    };
  }
  // Helper: render a single QUEUED sub-agent tool row in the parent timeline
  // (the busy-child-session spawn path): the frozen descriptor carries only a
  // queue_item_id — never a run id. The caller installs the `rpcMock`
  // implementation and the `listQueueMock` behaviour first; mounting fires
  // the row's automatic status verification, which consults both.
  async function mountChatViewWithQueuedSubAgent() {
    chatViewTest.mount({
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

    sendComposerMessage('Spawn queued sub-agent');

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    const handlers = subscribeRunEventsMock.mock.calls[0][1];
    handlers.onEvent({
      data: {
        type: 'tool_call_started',
        run_id: 'run-verify-1',
        sequence: 1,
        payload: {
          tool_call: {
            id: 'call-queued-1',
            index: 0,
            name: 'subagent',
            arguments: {
              agent_id: 'alpha',
              background: true,
              content: 'Inspect the project',
            },
          },
        },
      },
    });
    handlers.onEvent({
      data: {
        type: 'subagent_session_started',
        run_id: 'run-verify-1',
        sequence: 2,
        payload: {
          tool_call: { id: 'call-queued-1', index: 0, name: 'subagent' },
          data: {
            agent_id: 'alpha',
            session_id: 'sub-session-1',
            queue_item_id: 'queue-item-1',
            status: 'queued',
          },
        },
      },
    });
    handlers.onEvent({
      data: {
        type: 'tool_call_result',
        run_id: 'run-verify-1',
        sequence: 3,
        payload: {
          tool_call: { id: 'call-queued-1', index: 0, name: 'subagent' },
          result: JSON.stringify({
            ok: true,
            data: {
              agent_id: 'alpha',
              session_id: 'sub-session-1',
              queue_item_id: 'queue-item-1',
              status: 'queued',
            },
          }),
        },
      },
    });
    flushSync();

    const row = document.querySelector('.subagent-tool-event');
    expect(row).not.toBeNull();
    return row;
  }
  // The tool row object the timeline's cancel button hands to
  // `onCancelSubAgent` for a queued spawn (frozen descriptor, no run id).
  function queuedSpawnToolFixture() {
    return {
      name: 'subagent',
      status: 'success',
      arguments: { agent_id: 'alpha', content: 'Inspect the project' },
      result: JSON.stringify({
        ok: true,
        error: null,
        data: {
          agent_id: 'alpha',
          session_id: 'sub-session-1',
          queue_item_id: 'queue-item-1',
          status: 'queued',
        },
        artifacts: [],
      }),
    };
  }
  return {
    chatViewTest,
    mountChatViewWithRunningSubAgent,
    createVerifyRpcMock,
    mountChatViewWithQueuedSubAgent,
    queuedSpawnToolFixture,
  };
}

export { setupChatSessionNavigationSuite };
