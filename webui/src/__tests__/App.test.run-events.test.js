// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import {
  activeAgentTab,
  App,
  cleanupAppHarness,
  createChatRpcMock,
  createRunningSubAgentRpcMock,
  resetAppHarness,
  rpcMock,
  runServerEvent,
  subscribeRunEventsMock,
  subscribeServerEventsMock,
  waitForAssertion,
} from './App.support.js';

vi.mock('svelte', async () => {
  return import('../../node_modules/svelte/src/index-client.js');
});

describe('App', () => {
  let mountedComponent;

  beforeEach(() => {
    resetAppHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupAppHarness(mountedComponent);
  });

  it('processes rapid run WebSocket events without dropping the assistant output', async () => {
    const agents = [
      {
        id: 'alpha',
        name: 'Alpha',
        current_session_id: 'session-parent',
      },
    ];
    rpcMock.mockImplementation(createChatRpcMock(agents));

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForAssertion(() => {
      expect(activeAgentTab()?.textContent).toContain('Alpha');
    });

    const [handlers] = subscribeServerEventsMock.mock.calls[0];
    await Promise.all([
      handlers.onEvent(
        runServerEvent('run_started', 'run-follow-up', 1, {
          run_event_type: 'run_started',
          status: 'running',
        }),
      ),
      handlers.onEvent(
        runServerEvent('run_output', 'run-follow-up', 2, {
          run_event_type: 'assistant_output',
          output: {
            message: {
              role: 'assistant',
              content: 'Background sub-agent finished.',
            },
          },
        }),
      ),
      handlers.onEvent(
        runServerEvent('run_completed', 'run-follow-up', 3, {
          run_event_type: 'run_completed',
          status: 'completed',
        }),
      ),
    ]);
    flushSync();

    await waitForAssertion(() => {
      expect(document.body.textContent).toContain(
        'Background sub-agent finished.',
      );
    });
    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/run-follow-up/events',
      expect.any(Object),
      { afterSequence: 1 },
    );
  });

  it('updates a background sub-agent row when the child completion event arrives rapidly', async () => {
    const agents = [
      {
        id: 'alpha',
        name: 'Alpha',
        current_session_id: 'session-parent',
      },
    ];
    rpcMock.mockImplementation(createRunningSubAgentRpcMock(agents));

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForAssertion(() => {
      expect(document.querySelector('.subagent-tool-event')).toBeTruthy();
      expect(
        document.querySelector('.subagent-tool-event .te-dot.running'),
      ).toBeTruthy();
    });

    const [handlers] = subscribeServerEventsMock.mock.calls[0];
    await handlers.onEvent(
      runServerEvent('run_completed', 'sub-run-running', 1, {
        agent_id: 'alpha',
        session_id: 'sub-session-running',
        run_event_type: 'run_completed',
        status: 'completed',
      }),
    );
    flushSync();

    await waitForAssertion(() => {
      expect(
        document.querySelector('.subagent-tool-event .te-dot.done'),
      ).toBeTruthy();
      expect(
        document.querySelector('.subagent-tool-event .te-dot.running'),
      ).toBeFalsy();
    });
  });

  it('routes the connection_ready hello frame into connectionSnapshot and skips the run-server-events path', async () => {
    const agents = [
      {
        id: 'alpha',
        name: 'Alpha',
        current_session_id: 'session-parent',
      },
    ];
    rpcMock.mockImplementation(createChatRpcMock(agents));

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForAssertion(() => {
      expect(activeAgentTab()?.textContent).toContain('Alpha');
    });

    const [handlers] = subscribeServerEventsMock.mock.calls[0];
    const subscribeCallsBefore = subscribeRunEventsMock.mock.calls.length;

    const helloFrame = {
      type: 'connection_ready',
      epoch: 'bus-epoch-7',
      last_sequence: 42,
      active_runs: [
        {
          run_id: 'run-snapshot-1',
          agent_id: 'alpha',
          session_id: 'session-parent',
          status: 'running',
          sse_url: '/api/runs/run-snapshot-1/events',
        },
        {
          run_id: 'run-snapshot-2',
          agent_id: 'alpha',
          session_id: 'session-other',
          status: 'running',
          sse_url: '/api/runs/run-snapshot-2/events',
        },
      ],
    };

    handlers.onEvent(helloFrame);
    flushSync();

    // The full frame (epoch, last_sequence, active_runs) lives in the
    // connectionSnapshot state — verify the export returns it untouched.
    expect(mountedComponent.getConnectionSnapshot()).toEqual(helloFrame);
    expect(mountedComponent.getConnectionSnapshot().active_runs).toHaveLength(
      2,
    );

    // The hello frame has no payload.run_id / run_event_sequence, so
    // it stays out of `runServerEvents`. However, the snapshot application
    // in ChatView (Phase 1.3) legitimately triggers one SSE subscription
    // for the displayed session's active run — this is the intended
    // snapshot path, not the replay path.
    expect(subscribeRunEventsMock.mock.calls.length).toBe(
      subscribeCallsBefore + 1,
    );
    expect(subscribeRunEventsMock).toHaveBeenLastCalledWith(
      '/api/runs/run-snapshot-1/events',
      expect.any(Object),
      expect.any(Object),
    );
  });

  it('keeps the run_server_events path working for normal run lifecycle events', async () => {
    const agents = [
      {
        id: 'alpha',
        name: 'Alpha',
        current_session_id: 'session-parent',
      },
    ];
    rpcMock.mockImplementation(createChatRpcMock(agents));

    mountedComponent = mount(App, { target: document.body });
    flushSync();

    await waitForAssertion(() => {
      expect(activeAgentTab()?.textContent).toContain('Alpha');
    });

    const [handlers] = subscribeServerEventsMock.mock.calls[0];
    const subscribeCallsBefore = subscribeRunEventsMock.mock.calls.length;

    handlers.onEvent(
      runServerEvent('run_started', 'run-plain', 11, {
        run_event_type: 'run_started',
        status: 'running',
      }),
    );
    flushSync();

    // A plain run_started still flows through `runServerEvents`, which
    // delegates to runStream.handleServerEvents → attachRunStream →
    // subscribeRunEvents. The connection_ready routing change must not
    // disturb that.
    expect(subscribeRunEventsMock.mock.calls.length).toBe(
      subscribeCallsBefore + 1,
    );
    expect(subscribeRunEventsMock).toHaveBeenLastCalledWith(
      '/api/runs/run-plain/events',
      expect.any(Object),
      expect.any(Object),
    );

    // And the connection snapshot is still null because no hello arrived.
    expect(mountedComponent.getConnectionSnapshot()).toBeNull();
  });
});
