// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();
const subscribeRunEventsMock = vi.fn(() => ({ close: vi.fn(), source: null }));
const listSessionsMock = vi.fn(async () => ({ sessions: [] }));
const linkSessionToChannelMock = vi.fn(async () => ({ ok: true }));
const listQueueMock = vi.fn(async () => ({ items: [] }));
const removeFromQueueMock = vi.fn(async () => ({ ok: true }));
const updateQueueItemMock = vi.fn(async () => ({ ok: true }));

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  RUN_EVENT_ASSISTANT_OUTPUT_DELTA: 'assistant_output_delta',
  RUN_EVENT_REASONING_DELTA: 'reasoning_delta',
  RUN_EVENT_TOOL_CALL_DELTA: 'tool_call_delta',
  RUN_EVENT_TOOL_CALL_STDERR: 'tool_call_stderr',
  RUN_EVENT_TOOL_CALL_STDOUT: 'tool_call_stdout',
  rpc: (...args) => rpcMock(...args),
  subscribeRunEvents: (...args) => subscribeRunEventsMock(...args),
  listSessions: (...args) => listSessionsMock(...args),
  linkSessionToChannel: (...args) => linkSessionToChannelMock(...args),
  listQueue: (...args) => listQueueMock(...args),
  removeFromQueue: (...args) => removeFromQueueMock(...args),
  updateQueueItem: (...args) => updateQueueItemMock(...args),
}));

const { default: ChatView } = await import('../ChatView.svelte');

describe('ChatView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
    subscribeRunEventsMock.mockClear();
    listSessionsMock.mockReset();
    listSessionsMock.mockResolvedValue({ sessions: [] });
    linkSessionToChannelMock.mockReset();
    linkSessionToChannelMock.mockResolvedValue({ ok: true });
    listQueueMock.mockReset();
    listQueueMock.mockResolvedValue({ items: [] });
    removeFromQueueMock.mockReset();
    removeFromQueueMock.mockResolvedValue({ ok: true });
    updateQueueItemMock.mockReset();
    updateQueueItemMock.mockResolvedValue({ ok: true });
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
  });

  it('shows the combined input and output usage in the token badge', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92 },
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    const numberFormat = new Intl.NumberFormat();
    const expectedBadge = `${numberFormat.format(3978)} / ${numberFormat.format(262144)} tok`;

    await waitForCondition(
      () =>
        document.body.querySelector('.token-badge')?.textContent?.trim() ===
        expectedBadge,
      100,
    );

    expect(
      document.body.querySelector('.token-badge')?.textContent?.trim(),
    ).toBe(expectedBadge);
  });

  it('keeps the estimated marker when combined usage is estimated', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92, estimated: true },
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    const numberFormat = new Intl.NumberFormat();
    const expectedBadge = `~${numberFormat.format(3978)} / ${numberFormat.format(262144)} tok`;

    await waitForCondition(
      () =>
        document.body.querySelector('.token-badge')?.textContent?.trim() ===
        expectedBadge,
      100,
    );

    expect(
      document.body.querySelector('.token-badge')?.textContent?.trim(),
    ).toBe(expectedBadge);
  });

  it('does not render a refresh button in the chat header', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    expect(findButtonByText('Sessions')).toBeTruthy();
    expect(findButtonByText('New Session')).toBeTruthy();
    expect(findButtonByText('Refresh')).toBeFalsy();
    expect(document.body.querySelector('.chat-refresh')).toBeNull();
  });

  it('shows inline info and skips run subscription when a command is handled', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamResponse: {
          command_handled: true,
          reply: 'Run cancelled.',
        },
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const composerInput = document.querySelector('#chat-composer-input');
    expect(composerInput).toBeTruthy();
    setInputValue(composerInput, '/stop');
    flushSync();

    const sendButton = document.querySelector('.send-btn');
    expect(sendButton).toBeTruthy();
    sendButton.click();

    await waitForCondition(
      () =>
        document.body.querySelector('.chat-view__info')?.textContent?.trim() ===
        'Run cancelled.',
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'session-1',
      content: '/stop',
    });
    expect(subscribeRunEventsMock).not.toHaveBeenCalled();
  });

  it('shows inline info when /status command is handled', async () => {
    const statusReply =
      'Agent: Alpha\nModel: claude-sonnet-4\nSession started: 2026-05-19';

    rpcMock.mockImplementation(
      createChatRpcMock({
        commandItems: [
          {
            name: 'status',
            description: 'Show current agent and session status.',
            type: 'command',
          },
        ],
        streamHandler: ({ content }) => {
          if (content === '/status') {
            return {
              command_handled: true,
              reply: statusReply,
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('/status');

    await waitForCondition(
      () =>
        document.body
          .querySelector('.chat-view__info')
          ?.textContent?.includes('Agent: Alpha'),
      100,
    );

    const inlineInfo = document.body.querySelector('.chat-view__info');
    expect(inlineInfo?.textContent).toContain(
      'Agent: Alpha\nModel: claude-sonnet-4',
    );
    expect(inlineInfo?.textContent).toContain('Session started: 2026-05-19');
    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'session-1',
      content: '/status',
    });
    expect(subscribeRunEventsMock).not.toHaveBeenCalled();
  });

  it('switches to the session returned by a handled /new command', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-new': [],
        },
        streamHandler: ({ content }) => {
          if (content === '/new') {
            return {
              command_handled: true,
              reply: 'New session started: session-new',
              data: {
                command: 'new',
                session_id: 'session-new',
              },
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('/new');

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' && params?.session_id === 'session-new',
        ),
      100,
    );

    expect(document.body.querySelector('.chat-view__info')?.textContent).toBe(
      'New session started: session-new',
    );
    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'session-1',
      content: '/new',
    });
    expect(subscribeRunEventsMock).not.toHaveBeenCalled();
  });

  it('subscribes to the run returned by a /retry command', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamHandler: ({ content }) => {
          if (content === '/retry') {
            return {
              run_id: 'run-retry-1',
              sse_url: '/api/runs/run-retry-1/events',
              status: 'running',
              events: [],
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('/retry');

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'session-1',
      content: '/retry',
    });
    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/run-retry-1/events',
      expect.any(Object),
      { afterSequence: 0 },
    );
  });

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

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start a long run');

    await waitForCondition(() => Boolean(findButtonByText('Cancel run')), 100);

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
        document.body.querySelector('.chat-view__info')?.textContent?.trim() ===
        'Run cancelled.',
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

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start a long run');

    await waitForCondition(() => Boolean(findButtonByText('Cancel run')), 100);

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
        document.body.querySelector('.chat-view__info')?.textContent?.trim() ===
        'Run cancelled.',
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

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start a long run');

    await waitForCondition(() => Boolean(findButtonByText('Cancel run')), 100);

    sendComposerMessage('/compact');

    await waitForCondition(
      () =>
        document.body.querySelector('.chat-view__info')?.textContent?.trim() ===
        'Context compacted.',
      100,
    );

    expect(streamCalls).toEqual(['Start a long run', '/compact']);
    const queuedContent =
      document.body.querySelector('.queued-messages__content')?.textContent ??
      '';
    expect(queuedContent.includes('/compact')).toBe(false);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
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

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start a long run');

    await waitForCondition(() => Boolean(findButtonByText('Cancel run')), 100);

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

  it('batches run SSE deltas before updating the rendered timeline', async () => {
    const closeSubscription = vi.fn();
    subscribeRunEventsMock.mockReturnValue({
      close: closeSubscription,
      source: null,
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamResponse: {
          run_id: 'run-batched-deltas',
          sse_url: '/api/runs/run-batched-deltas/events',
          status: 'running',
          events: [],
        },
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start batched stream');

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    const handlers = subscribeRunEventsMock.mock.calls[0][1];
    handlers.onEvent({
      data: {
        type: 'reasoning_delta',
        run_id: 'run-batched-deltas',
        sequence: 1,
        payload: { reasoning_delta: 'Think ' },
      },
    });
    handlers.onEvent({
      data: {
        type: 'reasoning_delta',
        run_id: 'run-batched-deltas',
        sequence: 2,
        payload: { reasoning_delta: 'fast' },
      },
    });
    flushSync();

    expect(document.body.textContent).not.toContain('Think fast');

    await waitForCondition(
      () => document.body.textContent.includes('Think fast'),
      100,
    );
  });

  it('flushes stable run events immediately so a fast sub-agent starts as running', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamResponse: {
          run_id: 'run-fast-subagent',
          sse_url: '/api/runs/run-fast-subagent/events',
          status: 'running',
          events: [],
        },
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start fast sub-agent');

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    const handlers = subscribeRunEventsMock.mock.calls[0][1];
    handlers.onEvent({
      data: {
        type: 'tool_call_started',
        run_id: 'run-fast-subagent',
        sequence: 1,
        payload: {
          tool_call: {
            id: 'call-subagent',
            index: 0,
            name: 'subagent',
            arguments: {
              agent_id: 'beta',
              blocking: false,
              content: 'Inspect in the background',
            },
          },
        },
      },
    });
    handlers.onEvent({
      data: {
        type: 'subagent_session_started',
        run_id: 'run-fast-subagent',
        sequence: 2,
        payload: {
          tool_call: {
            id: 'call-subagent',
            index: 0,
            name: 'subagent',
          },
          data: {
            agent_id: 'beta',
            session_id: 'beta-session',
            run_id: 'beta-run',
            status: 'running',
          },
        },
      },
    });
    flushSync();

    const runningRow = document.querySelector('.subagent-tool-event');
    expect(runningRow).not.toBeNull();
    expect(runningRow?.querySelector('.te-dot.running')).not.toBeNull();
    expect(runningRow?.querySelector('.te-dot.done')).toBeNull();

    handlers.onEvent({
      data: {
        type: 'tool_call_result',
        run_id: 'run-fast-subagent',
        sequence: 3,
        payload: {
          tool_call: {
            id: 'call-subagent',
            index: 0,
            name: 'subagent',
          },
          result: JSON.stringify({
            ok: true,
            data: {
              agent_id: 'beta',
              session_id: 'beta-session',
              run_id: 'beta-run',
              status: 'running',
            },
          }),
        },
      },
    });
    flushSync();

    const spawnedRow = document.querySelector('.subagent-tool-event');
    expect(spawnedRow?.querySelector('.te-dot.running')).not.toBeNull();
    expect(spawnedRow?.querySelector('.te-dot.done')).toBeNull();
  });

  it('coalesces repeated run stream errors into one reconnect', async () => {
    const firstCloseSubscription = vi.fn();
    const secondCloseSubscription = vi.fn();
    subscribeRunEventsMock
      .mockReturnValueOnce({ close: firstCloseSubscription, source: null })
      .mockReturnValueOnce({ close: secondCloseSubscription, source: null });
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamResponse: {
          run_id: 'run-reconnect-once',
          sse_url: '/api/runs/run-reconnect-once/events',
          status: 'running',
          events: [],
        },
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Start reconnecting stream');

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    const handlers = subscribeRunEventsMock.mock.calls[0][1];

    vi.useFakeTimers();
    try {
      handlers.onError(new Error('first disconnect'));
      handlers.onError(new Error('second disconnect'));

      await vi.advanceTimersByTimeAsync(500);
      flushSync();

      expect(subscribeRunEventsMock).toHaveBeenCalledTimes(2);
      expect(firstCloseSubscription).toHaveBeenCalledTimes(1);
      expect(secondCloseSubscription).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows all command and skill suggestions for an empty slash query', async () => {
    const commandItems = [
      { name: 'stop', description: 'Cancel the active run.', type: 'command' },
      {
        name: 'help',
        description: 'Show available commands.',
        type: 'command',
      },
      { name: 'status', description: 'Show run status.', type: 'command' },
      { name: 'reset', description: 'Reset local UI state.', type: 'command' },
      { name: 'retry', description: 'Retry the last run.', type: 'command' },
      {
        name: 'debugging',
        description: 'Investigate unclear bugs.',
        type: 'skill',
      },
      {
        name: 'ctx7',
        description: 'Fetch current framework docs.',
        type: 'skill',
      },
      {
        name: 'refactoring',
        description: 'Refactor with strict scope.',
        type: 'skill',
      },
      {
        name: 'playwright-cli',
        description: 'Automate browser testing.',
        type: 'skill',
      },
      {
        name: 'frontend-design',
        description: 'Build intentional UI.',
        type: 'skill',
      },
      {
        name: 'glossary',
        description: 'Maintain glossary terms.',
        type: 'skill',
      },
      {
        name: 'debug',
        description: 'General debugging workflow.',
        type: 'skill',
      },
    ];

    rpcMock.mockImplementation(
      createChatRpcMock({
        commandItems,
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const composerInput = document.querySelector('#chat-composer-input');
    expect(composerInput).toBeTruthy();
    setInputValue(composerInput, '/');
    composerInput.setSelectionRange(1, 1);
    composerInput.dispatchEvent(new Event('keyup', { bubbles: true }));
    flushSync();

    await waitForCondition(
      () =>
        document.querySelectorAll('.skill-autocomplete__option').length ===
        commandItems.length,
      100,
    );

    expect(
      document.querySelectorAll('.skill-autocomplete__option'),
    ).toHaveLength(commandItems.length);
  });

  it('shows only skill suggestions for an inline dollar query', async () => {
    const commandItems = [
      { name: 'stop', description: 'Cancel the active run.', type: 'command' },
      { name: 'status', description: 'Show run status.', type: 'command' },
      {
        name: 'debugging',
        description: 'Investigate unclear bugs.',
        type: 'skill',
      },
      {
        name: 'ctx7',
        description: 'Fetch current framework docs.',
        type: 'skill',
      },
    ];

    rpcMock.mockImplementation(
      createChatRpcMock({
        commandItems,
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const composerInput = document.querySelector('#chat-composer-input');
    expect(composerInput).toBeTruthy();
    setInputValue(composerInput, 'Use $');
    composerInput.setSelectionRange(5, 5);
    composerInput.dispatchEvent(new Event('keyup', { bubbles: true }));
    flushSync();

    await waitForCondition(
      () =>
        document.querySelectorAll('.skill-autocomplete__option').length === 2,
      100,
    );

    const optionNames = Array.from(
      document.querySelectorAll('.skill-autocomplete__name'),
    ).map((element) => element.textContent.trim());
    expect(optionNames).toEqual(['debugging', 'ctx7']);
    expect(
      document.querySelector('.skill-autocomplete__eyebrow').textContent,
    ).toContain('skills');
  });

  it('loads a sub-agent session override as a writable session notice', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        pendingSubAgentNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Sub-agent response'),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'sub-session-1',
      limit: 100,
    });
    expect(document.body.textContent).toContain('Viewing a sub-agent session');
    expect(document.body.textContent).toContain('Return to current session');
    expect(
      document.querySelector(
        '.chat-view__footer-stack .chat-view__subagent-session-notice',
      ),
    ).toBeTruthy();
    expect(document.querySelector('textarea')?.disabled).toBe(false);
  });

  it('loads the newest history first and prepends older messages on top scroll', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-1': createHistoryMessages(120),
        },
      }),
    );

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    await waitForCondition(
      () =>
        document.body.textContent.includes('History message 21') &&
        !document.body.textContent.includes('History message 20'),
      100,
    );

    const messages = document.querySelector('.messages');
    let scrollHeight = 1000;
    Object.defineProperty(messages, 'scrollHeight', {
      configurable: true,
      get: () => scrollHeight,
    });
    Object.defineProperty(messages, 'scrollTop', {
      configurable: true,
      writable: true,
      value: 0,
    });

    messages.dispatchEvent(new Event('scroll'));
    scrollHeight = 1400;

    await waitForCondition(
      () =>
        document.body.textContent.includes('History message 20') &&
        messages.scrollTop === 400,
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'session-1',
      limit: 100,
    });
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'session-1',
      limit: 50,
      before: 'message-021',
    });
  });

  it('subscribes to an active run returned with opened session history', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        activeRuns: {
          'sub-session-1': {
            run_id: 'active-sub-run',
            sse_url: '/api/runs/active-sub-run/events',
            status: 'running',
            events: [
              {
                type: 'run_started',
                run_id: 'active-sub-run',
                agent_id: 'alpha',
                session_id: 'sub-session-1',
                sequence: 1,
                payload: { status: 'running' },
              },
            ],
          },
        },
      }),
    );

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        pendingSubAgentNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/active-sub-run/events',
      expect.any(Object),
      { afterSequence: 1 },
    );
  });

  it('merges retained active-run events when reloading the same displayed session', async () => {
    const activeRuns = {
      'session-1': {
        run_id: 'active-parent-run',
        sse_url: '/api/runs/active-parent-run/events',
        status: 'running',
        events: [
          {
            type: 'run_started',
            run_id: 'active-parent-run',
            agent_id: 'alpha',
            session_id: 'session-1',
            sequence: 1,
            payload: { status: 'running' },
          },
        ],
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

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    activeRuns['session-1'] = {
      ...activeRuns['session-1'],
      events: [
        ...activeRuns['session-1'].events,
        {
          type: 'assistant_output_delta',
          run_id: 'active-parent-run',
          agent_id: 'alpha',
          session_id: 'session-1',
          sequence: 2,
          payload: { content_delta: 'Recovered ' },
        },
        {
          type: 'assistant_output_delta',
          run_id: 'active-parent-run',
          agent_id: 'alpha',
          session_id: 'session-1',
          sequence: 3,
          payload: { content_delta: 'draft' },
        },
      ],
    };

    findButtonByText('Sessions')?.click();

    await waitForCondition(
      () => document.body.textContent.includes('session-1'),
      100,
    );

    findButtonByText('session-1')?.click();

    await waitForCondition(
      () => document.body.textContent.includes('Recovered draft'),
      100,
    );

    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);
  });

  it('attaches to SSE when a run starts for the displayed session', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        runServerEvent: {
          type: 'run_started',
          payload: {
            run_id: 'pushed-run',
            agent_id: 'alpha',
            session_id: 'session-1',
            run_event_type: 'run_started',
            run_event_sequence: 1,
            run_event_timestamp: '2026-05-26T00:00:00+00:00',
            status: 'running',
          },
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => subscribeRunEventsMock.mock.calls.length === 1,
      100,
    );

    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/pushed-run/events',
      expect.any(Object),
      { afterSequence: 1 },
    );
  });

  it('returns from a sub-agent session override to the current session', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        pendingSubAgentNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Sub-agent response'),
      100,
    );

    const returnButton = Array.from(document.querySelectorAll('button')).find(
      (button) => button.textContent.trim() === 'Return to current session',
    );

    expect(returnButton).toBeTruthy();
    returnButton.click();

    await waitForCondition(
      () =>
        document.body.textContent.includes('Hello') &&
        !document.body.textContent.includes('Viewing a sub-agent session'),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'session-1',
      limit: 100,
    });
    expect(document.querySelector('textarea')?.disabled).toBe(false);
  });

  it('returns from a different-agent sub-agent session to the parent current session', async () => {
    const agents = [
      createAgent({
        id: 'alpha',
        name: 'Alpha',
        current_session_id: 'parent-session',
      }),
      createAgent({
        id: 'beta',
        name: 'Beta',
        current_session_id: 'beta-current-session',
      }),
    ];
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents,
        sessionMessages: {
          'parent-session': [
            {
              id: 'parent-assistant-one',
              role: 'assistant',
              content: 'Parent main response',
            },
          ],
          'beta-sub-session': [
            {
              id: 'beta-sub-assistant-one',
              role: 'assistant',
              content: 'Beta sub-agent response',
            },
          ],
          'beta-current-session': [
            {
              id: 'beta-current-assistant-one',
              role: 'assistant',
              content: 'Beta current response',
            },
          ],
        },
      }),
    );

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: agents,
        sharedSelectedAgentId: 'alpha',
        pendingSubAgentNavigation: {
          agentId: 'beta',
          sessionId: 'beta-sub-session',
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Beta sub-agent response'),
      100,
    );

    const returnButton = Array.from(document.querySelectorAll('button')).find(
      (button) => button.textContent.trim() === 'Return to current session',
    );

    expect(returnButton).toBeTruthy();
    returnButton.click();

    await waitForCondition(
      () =>
        document.body.textContent.includes('Parent main response') &&
        !document.body.textContent.includes('Viewing a sub-agent session'),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'beta',
      session_id: 'beta-sub-session',
      limit: 100,
    });
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'parent-session',
      limit: 100,
    });
    expect(rpcMock).not.toHaveBeenCalledWith('chat.history', {
      agent_id: 'beta',
      session_id: 'beta-current-session',
      limit: 100,
    });
    expect(activeAgentTab()?.textContent).toContain('Alpha');
  });

  it('retries the sub-agent session when retry is requested from its override', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        retryRunResponse: {
          run_id: 'retry-run-1',
          sse_url: '/api/runs/retry-run-1/events',
          status: 'running',
          events: [],
        },
      }),
    );

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        pendingSubAgentNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Sub-agent response'),
      100,
    );

    expect(typeof mountedComponent.retryLastTurn).toBe('function');
    await mountedComponent.retryLastTurn();
    flushSync();

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.retry_last_turn' &&
            params?.agent_id === 'alpha' &&
            params?.session_id === 'sub-session-1',
        ),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.retry_last_turn', {
      agent_id: 'alpha',
      session_id: 'sub-session-1',
    });
    expect(rpcMock).not.toHaveBeenCalledWith('chat.retry_last_turn', {
      agent_id: 'alpha',
      session_id: 'session-1',
    });
  });

  it('sends messages from a sub-agent session override', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamResponse: {
          run_id: 'sub-run-continue',
          sse_url: '/api/runs/sub-run-continue/events',
          status: 'running',
          events: [],
        },
      }),
    );

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        pendingSubAgentNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Sub-agent response'),
      100,
    );

    sendComposerMessage('Continue child work');

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.stream' &&
            params?.agent_id === 'alpha' &&
            params?.session_id === 'sub-session-1' &&
            params?.content === 'Continue child work',
        ),
      100,
    );

    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/sub-run-continue/events',
      expect.any(Object),
      { afterSequence: 0 },
    );
  });

  it('sends messages to a different-agent sub-agent session override', async () => {
    const agents = [
      createAgent({
        id: 'alpha',
        name: 'Alpha',
        current_session_id: 'parent-session',
      }),
      createAgent({
        id: 'beta',
        name: 'Beta',
        current_session_id: 'beta-current-session',
      }),
    ];
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents,
        sessionMessages: {
          'parent-session': [
            {
              id: 'parent-assistant-one',
              role: 'assistant',
              content: 'Parent main response',
            },
          ],
          'beta-sub-session': [
            {
              id: 'beta-sub-assistant-one',
              role: 'assistant',
              content: 'Beta sub-agent response',
            },
          ],
        },
        streamHandler: ({ agent_id: agentId, session_id: sessionId }) => {
          if (agentId === 'beta' && sessionId === 'beta-sub-session') {
            return {
              run_id: 'beta-sub-run-continue',
              sse_url: '/api/runs/beta-sub-run-continue/events',
              status: 'running',
              events: [],
            };
          }
          throw new Error(`Unexpected stream target: ${agentId}/${sessionId}`);
        },
      }),
    );

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: agents,
        sharedSelectedAgentId: 'alpha',
        pendingSubAgentNavigation: {
          agentId: 'beta',
          sessionId: 'beta-sub-session',
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Beta sub-agent response'),
      100,
    );

    sendComposerMessage('Continue beta child work');

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.stream' &&
            params?.agent_id === 'beta' &&
            params?.session_id === 'beta-sub-session' &&
            params?.content === 'Continue beta child work',
        ),
      100,
    );

    expect(activeAgentTab()?.textContent).toContain('Alpha');
    expect(subscribeRunEventsMock).toHaveBeenCalledWith(
      '/api/runs/beta-sub-run-continue/events',
      expect.any(Object),
      { afterSequence: 0 },
    );
  });

  it('loads selected session history from the sessions drawer', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-1': [
            {
              id: 'assistant-one',
              role: 'assistant',
              content: 'Current session reply',
            },
          ],
          'ch-tg-assistant-12345': [
            {
              id: 'assistant-two',
              role: 'assistant',
              content: 'Telegram session reply',
            },
          ],
        },
      }),
    );
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'ch-tg-assistant-12345',
          created_at: '2026-05-10T11:00:00+00:00',
          last_active_at: '2026-05-11T09:30:00+00:00',
          source_channel_id: 'tg-assistant',
          platform: 'telegram',
          platform_conv_id: '12345',
        },
        {
          id: 'session-1',
          created_at: '2026-05-09T00:00:00+00:00',
          last_active_at: '2026-05-09T01:00:00+00:00',
        },
      ],
    });

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Current session reply'),
      100,
    );

    const sessionsButton = findButtonByText('Sessions');
    expect(sessionsButton).toBeTruthy();
    sessionsButton.click();

    await waitForCondition(
      () => document.body.textContent.includes('telegram/12345'),
      100,
    );

    const telegramSessionButton = findButtonByText('telegram/12345');
    expect(telegramSessionButton).toBeTruthy();
    telegramSessionButton.click();

    await waitForCondition(
      () => document.body.textContent.includes('Telegram session reply'),
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'ch-tg-assistant-12345',
      limit: 100,
    });
  });

  it('renders unlinked sessions as selection-only rows in the sessions drawer', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'session-legacy',
          created_at: '2026-05-09T00:00:00+00:00',
          last_active_at: '2026-05-09T01:00:00+00:00',
        },
      ],
    });

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const sessionsButton = findButtonByText('Sessions');
    expect(sessionsButton).toBeTruthy();
    sessionsButton.click();

    await waitForCondition(
      () => document.body.textContent.includes('session-legacy'),
      100,
    );

    expect(findButtonByText('session-legacy')).toBeTruthy();
    expect(findButtonByText('Link to channel')).toBeFalsy();
    expect(document.querySelector('input[name="channel-id"]')).toBeNull();
    expect(linkSessionToChannelMock).not.toHaveBeenCalled();
  });

  it('renders sub-agent session metadata in the sessions drawer', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'child-session',
          is_subagent_session: true,
          subagent_parent: {
            agent_id: 'orchestrator',
            session_id: 'parent-session',
          },
          last_active_at: '2026-05-09T01:00:00+00:00',
        },
      ],
    });

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const sessionsButton = findButtonByText('Sessions');
    expect(sessionsButton).toBeTruthy();
    sessionsButton.click();

    await waitForCondition(
      () => document.body.textContent.includes('child-session'),
      100,
    );

    expect(document.body.textContent).toContain('Sub-agent');
    expect(document.body.textContent).toContain('Parent:');
    expect(document.body.textContent).toContain('orchestrator/parent-session');
  });
});

function createChatRpcMock({
  usage,
  contextWindow = 262144,
  sessionMessages,
  activeRuns,
  retryRunResponse,
  streamResponse,
  streamHandler,
  commandsError = false,
  commandItems,
  agents,
} = {}) {
  const resolvedAgents = agents ?? [
    createAgent({ context_window: contextWindow }),
  ];
  const resolvedSessionMessages = {
    'session-1': [
      {
        id: 'assistant-one',
        role: 'assistant',
        content: 'Hello',
        usage,
      },
    ],
    'sub-session-1': [
      {
        id: 'sub-assistant-one',
        role: 'assistant',
        content: 'Sub-agent response',
      },
    ],
    ...(sessionMessages ?? {}),
  };

  return async (method, params) => {
    if (method === 'agent.list') {
      return { agents: resolvedAgents };
    }

    if (method === 'chat.history') {
      const messages = resolvedSessionMessages[params.session_id];
      if (messages) {
        const beforeIndex = params.before
          ? messages.findIndex((message) => message.id === params.before)
          : messages.length;
        if (beforeIndex < 0) {
          throw new Error(`Unexpected before message id: ${params.before}`);
        }
        const sourceMessages = messages.slice(0, beforeIndex);
        const pageMessages = params.limit
          ? sourceMessages.slice(-params.limit)
          : sourceMessages;
        const response = {
          session_id: params.session_id,
          messages: pageMessages,
          has_more: sourceMessages.length > pageMessages.length,
        };
        if (activeRuns?.[params.session_id]) {
          response.active_run = activeRuns[params.session_id];
        }
        return response;
      }

      throw new Error(`Unexpected session id: ${params.session_id}`);
    }

    if (method === 'chat.commands') {
      if (commandsError) {
        throw new Error('chat.commands unavailable');
      }
      return {
        items: commandItems ?? [
          {
            name: 'stop',
            description: 'Cancel the active run for this session.',
            type: 'command',
          },
          {
            name: 'debugging',
            description: 'Investigate unclear bugs.',
            type: 'skill',
          },
        ],
      };
    }

    if (method === 'chat.stream') {
      if (typeof streamHandler === 'function') {
        return streamHandler(params ?? {});
      }
      if (streamResponse) {
        return streamResponse;
      }
      throw new Error('Unexpected stream call');
    }

    if (method === 'chat.retry_last_turn') {
      if (retryRunResponse) {
        return retryRunResponse;
      }
      throw new Error('Unexpected retry call');
    }

    throw new Error(`Unexpected RPC method: ${method}`);
  };
}

function createHistoryMessages(count) {
  return Array.from({ length: count }, (_item, index) => {
    const number = index + 1;
    return {
      id: `message-${String(number).padStart(3, '0')}`,
      role: 'user',
      content: `History message ${number}`,
    };
  });
}

function createAgent(overrides = {}) {
  return {
    id: 'alpha',
    name: 'Alpha',
    model: 'openrouter/anthropic/claude-sonnet-4',
    fallback_model: '',
    workspace: 'C:/agents/alpha',
    temperature: '',
    thinking_effort: '',
    allowed_tools: ['*'],
    allowed_skills: ['*'],
    current_session_id: 'session-1',
    context_window: 262144,
    created_at: '2026-05-09T00:00:00+00:00',
    updated_at: '2026-05-09T00:00:00+00:00',
    ...overrides,
  };
}

function findButtonByText(text) {
  return Array.from(document.querySelectorAll('button')).find((button) =>
    button.textContent.includes(text),
  );
}

function activeAgentTab() {
  return document.querySelector('.agent-tab.active');
}

function setInputValue(input, value) {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

function sendComposerMessage(content) {
  const composerInput = document.querySelector('#chat-composer-input');
  expect(composerInput).toBeTruthy();
  setInputValue(composerInput, content);
  flushSync();

  const sendButton = document.querySelector('.send-btn');
  expect(sendButton).toBeTruthy();
  sendButton.click();
  flushSync();
}

async function waitForCondition(check, attempts = 20) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();

    if (check()) {
      return;
    }
  }

  throw new Error('Timed out waiting for condition.');
}
