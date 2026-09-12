// @vitest-environment jsdom
import {
  describe,
  createChatRpcMock,
  expect,
  findCancelRunButton,
  flushSync,
  it,
  rpcMock,
  sendComposerMessage,
  setupChatViewTestSuite,
  subscribeRunEventsMock,
  waitForCondition,
} from './ChatView.support.js';

describe('ChatView', () => {
  const chatViewTest = setupChatViewTestSuite();

  it('keeps slash skill triggers queued while allowing built-in /stop to bypass during an active run', async () => {
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamHandler: ({ content }) => {
          streamCalls.push(content);
          if (content === 'Start a long run') {
            return {
              run_id: 'run-1',
              sse_url: '/api/runs/run-1/events',
              status: 'running',
              events: [],
            };
          }
          if (content === '/debugging investigate this run') {
            return {
              queued: true,
              item: {
                id: 'queued-skill-1',
                content: '/debugging investigate this run',
                created_at: '2026-05-22T10:00:00+00:00',
              },
            };
          }
          if (content === '/stop') {
            return {
              command_handled: true,
              reply: 'Run cancelled.',
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start a long run');

    await waitForCondition(() => Boolean(findCancelRunButton()), 100);

    sendComposerMessage('/debugging investigate this run');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.queued-messages__content')
          ?.textContent?.includes('/debugging investigate this run'),
      100,
    );

    sendComposerMessage('/stop');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.chat-view__command-toast')
          ?.textContent?.trim() === 'Run cancelled.',
      100,
    );

    expect(streamCalls).toEqual([
      'Start a long run',
      '/debugging investigate this run',
      '/stop',
    ]);
    expect(
      document.body
        .querySelector('.queued-messages__content')
        ?.textContent?.includes('/debugging investigate this run'),
    ).toBe(true);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('uses local /stop fallback when command metadata cannot be loaded', async () => {
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        commandsError: true,
        streamHandler: ({ content }) => {
          streamCalls.push(content);
          if (content === 'Start a long run') {
            return {
              run_id: 'run-fallback-stop-1',
              sse_url: '/api/runs/run-fallback-stop-1/events',
              status: 'running',
              events: [],
            };
          }
          if (content === '/debugging investigate this run') {
            return {
              queued: true,
              item: {
                id: 'queued-skill-fallback-1',
                content: '/debugging investigate this run',
                created_at: '2026-05-22T10:01:00+00:00',
              },
            };
          }
          if (content === '/stop') {
            return {
              command_handled: true,
              reply: 'Run cancelled.',
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start a long run');

    await waitForCondition(() => Boolean(findCancelRunButton()), 100);

    sendComposerMessage('/debugging investigate this run');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.queued-messages__content')
          ?.textContent?.includes('/debugging investigate this run'),
      100,
    );

    sendComposerMessage('/stop');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.chat-view__command-toast')
          ?.textContent?.trim() === 'Run cancelled.',
      100,
    );

    expect(streamCalls).toEqual([
      'Start a long run',
      '/debugging investigate this run',
      '/stop',
    ]);
    expect(
      document.body
        .querySelector('.queued-messages__content')
        ?.textContent?.includes('/debugging investigate this run'),
    ).toBe(true);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('recognizes /compact when command metadata includes a leading slash', async () => {
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        commandItems: [
          {
            name: '/compact',
            description: 'Compact the current session context.',
            type: 'command',
          },
          {
            name: 'debugging',
            description: 'Investigate unclear bugs.',
            type: 'skill',
          },
        ],
        streamHandler: ({ content }) => {
          streamCalls.push(content);
          if (content === 'Start a long run') {
            return {
              run_id: 'run-compact-1',
              sse_url: '/api/runs/run-compact-1/events',
              status: 'running',
              events: [],
            };
          }
          if (content === '/compact') {
            return {
              command_handled: true,
              reply: 'Context compacted.',
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start a long run');

    await waitForCondition(() => Boolean(findCancelRunButton()), 100);

    sendComposerMessage('/compact');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.chat-view__command-toast')
          ?.textContent?.trim() === 'Context compacted.',
      100,
    );

    expect(streamCalls).toEqual(['Start a long run', '/compact']);
    const queuedContent =
      document.body.querySelector('.queued-messages__content')?.textContent ??
      '';
    expect(queuedContent.includes('/compact')).toBe(false);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('streams /compact through the same live checkpoint lifecycle as auto-compaction', async () => {
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        commandItems: [
          {
            name: '/compact',
            description: 'Compact the current session context.',
            type: 'command',
            argument: 'optional',
            output: 'toast',
          },
        ],
        streamHandler: ({ content }) => {
          streamCalls.push(content);
          if (content === '/compact focus on the auth work') {
            return {
              run_id: 'run-manual-compaction',
              sse_url: '/api/runs/run-manual-compaction/events',
              status: 'running',
              events: [
                {
                  type: 'run_started',
                  run_id: 'run-manual-compaction',
                  sequence: 1,
                  payload: { status: 'running' },
                },
                {
                  type: 'compaction_started',
                  run_id: 'run-manual-compaction',
                  sequence: 2,
                  payload: {},
                },
              ],
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    // Manual Compaction is a real Run, but it renders bare - the same bare
    // checkpoint divider as Auto-Compaction, from the same lifecycle events,
    // without a History-reload special case.
    const historyReloadCount = () =>
      rpcMock.mock.calls.filter(
        ([method, params]) =>
          method === 'chat.history' && params?.session_id === 'session-1',
      ).length;
    const reloadsBefore = historyReloadCount();

    sendComposerMessage('/compact focus on the auth work');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.compaction-sep--running')
          ?.textContent?.trim() === 'Compacting current conversation…',
      100,
    );

    expect(streamCalls).toEqual(['/compact focus on the auth work']);
    expect(historyReloadCount()).toBe(reloadsBefore);
    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/run-manual-compaction/events',
      expect.any(Object),
      { afterSequence: 0 },
    );

    const handlers = subscribeRunEventsMock.mock.calls[0][1];
    const checkpointMessage = {
      id: 'checkpoint-manual',
      role: 'compaction_checkpoint',
      content: 'Exact manual compaction context',
      usage: {
        context_tokens_before: 69_030,
        context_tokens_after: 26_835,
      },
    };
    handlers.onEvent({
      data: {
        type: 'compaction_completed',
        run_id: 'run-manual-compaction',
        sequence: 3,
        payload: {
          message: checkpointMessage,
          checkpoint: 1,
          checkpoint_id: 'checkpoint-manual',
          context_tokens_before: 69_030,
          context_tokens_after: 26_835,
        },
      },
    });
    handlers.onEvent({
      data: {
        type: 'run_completed',
        run_id: 'run-manual-compaction',
        sequence: 4,
        payload: { status: 'completed' },
      },
    });

    await waitForCondition(
      () =>
        document.body
          .querySelector('.compaction-sep')
          ?.textContent?.includes('~69k → ~27k'),
      100,
    );

    expect(document.body.querySelector('.compaction-sep--running')).toBeNull();
    const disclosure = document.body.querySelector('.compaction-disclosure');
    expect(disclosure).toBeTruthy();
    disclosure.open = true;
    flushSync();
    expect(
      disclosure.querySelector('.compaction-detail__text').textContent,
    ).toBe('Exact manual compaction context');
  });

  it('queues non-command messages while a run is active', async () => {
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamHandler: ({ content }) => {
          streamCalls.push(content);
          if (content === 'Start a long run') {
            return {
              run_id: 'run-2',
              sse_url: '/api/runs/run-2/events',
              status: 'running',
              events: [],
            };
          }
          if (content === 'Queue this while running') {
            return {
              queued: true,
              item: {
                id: 'queued-message-1',
                content: 'Queue this while running',
                created_at: '2026-05-22T10:02:00+00:00',
              },
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start a long run');

    await waitForCondition(() => Boolean(findCancelRunButton()), 100);

    sendComposerMessage('Queue this while running');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.queued-messages__content')
          ?.textContent?.trim() === 'Queue this while running',
      100,
    );

    expect(streamCalls).toEqual([
      'Start a long run',
      'Queue this while running',
    ]);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('explains when a server restart discards a locally shown queued message', async () => {
    const { createChatViewConnectionSnapshotHarness } =
      await import('./chatViewConnectionSnapshotHarness.svelte.js');
    const harness = createChatViewConnectionSnapshotHarness();
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamHandler: ({ content }) => {
          if (content === 'Start before restart') {
            return {
              run_id: 'run-before-restart',
              sse_url: '/api/runs/run-before-restart/events',
              status: 'running',
              events: [],
            };
          }
          if (content === 'Queue before restart') {
            return {
              queued: true,
              item: {
                id: 'queued-before-restart',
                content: 'Queue before restart',
                created_at: '2026-07-16T12:00:00+00:00',
              },
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    chatViewTest.mount({
      target: document.body,
      props: {
        get connectionSnapshot() {
          return harness.connectionSnapshot;
        },
      },
    });
    flushSync();
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start before restart');
    await waitForCondition(() => Boolean(findCancelRunButton()), 100);
    sendComposerMessage('Queue before restart');
    await waitForCondition(
      () =>
        document.body
          .querySelector('.queued-messages__content')
          ?.textContent?.includes('Queue before restart'),
      100,
    );

    harness.setConnectionSnapshot({
      type: 'connection_ready',
      epoch: 'epoch-after-restart',
      replay_status: 'epoch_changed',
      active_runs: [],
      queues: [],
    });
    flushSync();

    await waitForCondition(
      () =>
        document.body
          .querySelector('.chat-view__command-toast')
          ?.textContent?.trim() ===
        '1 queued message was discarded because the server restarted.',
      100,
    );
    expect(document.body.querySelector('.queued-messages__content')).toBeNull();
  });
});
