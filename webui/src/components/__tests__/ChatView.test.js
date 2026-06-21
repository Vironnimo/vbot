// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const rpcMock = vi.fn();
const subscribeRunEventsMock = vi.fn(() => ({ close: vi.fn(), source: null }));
const listSessionsMock = vi.fn(async () => ({ sessions: [] }));
const listQueueMock = vi.fn(async () => ({ items: [] }));
const removeFromQueueMock = vi.fn(async () => ({ ok: true }));
const updateQueueItemMock = vi.fn(async () => ({ ok: true }));
const showProjectMock = vi.fn(async () => ({ project: {}, scan: {} }));
const applyConnectionSnapshotMock = vi.fn();
const closeSubscriptionForMock = vi.fn();
// Per-mount references to the real chatState and runStream created inside
// ChatView. The reconcile tests use these to introspect live session state
// (and, for the staleRunId-guard test, to mutate `currentRun.runId` while
// a `chat.history` request is in flight).
const testChatStateRefs = [];
const testRunStreamRefs = [];

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
  listQueue: (...args) => listQueueMock(...args),
  removeFromQueue: (...args) => removeFromQueueMock(...args),
  updateQueueItem: (...args) => updateQueueItemMock(...args),
  showProject: (...args) => showProjectMock(...args),
}));

// Wrap the real run-stream factory so the wiring test can observe calls to
// `applyConnectionSnapshot` independently of whatever side effects the real
// implementation triggers (sub-agent status updates, `subscribeRunEvents`
// attach, etc.). The wiring assertion is purely "the effect called the run
// stream's `applyConnectionSnapshot` with the snapshot prop", which the spy
// captures cleanly while the real `chatRunStream.js` runs untouched.
//
// The reconcile tests need two more hooks: (1) a `closeSubscriptionFor` spy
// that records the session key the reconcile path passed in, and (2) access
// to the live `chatState` and `runStream` references created inside ChatView
// (so the staleRunId-guard test can mutate `currentRun.runId` while a
// `chat.history` request is in flight).
vi.mock('../../lib/chatRunStream.js', async () => {
  const actual = await vi.importActual('../../lib/chatRunStream.js');
  return {
    ...actual,
    createChatRunStream: (options) => {
      const stream = actual.createChatRunStream(options);
      testChatStateRefs.push(options.chatState);
      testRunStreamRefs.push(stream);
      return {
        ...stream,
        applyConnectionSnapshot: applyConnectionSnapshotMock,
        closeSubscriptionFor: (sessionKey) => {
          closeSubscriptionForMock(sessionKey);
          return stream.closeSubscriptionFor(sessionKey);
        },
      };
    },
  };
});

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
    listQueueMock.mockReset();
    listQueueMock.mockResolvedValue({ items: [] });
    removeFromQueueMock.mockReset();
    removeFromQueueMock.mockResolvedValue({ ok: true });
    updateQueueItemMock.mockReset();
    updateQueueItemMock.mockResolvedValue({ ok: true });
    showProjectMock.mockReset();
    showProjectMock.mockResolvedValue({ project: {}, scan: {} });
    applyConnectionSnapshotMock.mockReset();
    closeSubscriptionForMock.mockReset();
    testChatStateRefs.length = 0;
    testRunStreamRefs.length = 0;
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
  });

  it('marks the chat view with the default comfortable chat-width', () => {
    rpcMock.mockImplementation(createChatRpcMock());

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    expect(
      document.querySelector('.chat-view')?.getAttribute('data-chat-width'),
    ).toBe('comfortable');
  });

  it('reflects the chatWidth prop on the chat view for the measure cap', () => {
    rpcMock.mockImplementation(createChatRpcMock());

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        chatWidth: 'full',
      },
    });
    flushSync();

    // `full` is the opt-out hook: the CSS sets `--chat-measure: none` on this
    // attribute, removing the reading-width cap.
    expect(
      document.querySelector('.chat-view')?.getAttribute('data-chat-width'),
    ).toBe('full');
  });

  it('requests command suggestions scoped to the active agent address', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    // chat.commands is fetched with the active agent's address so the server can
    // return that agent's effective (project-scoped) skills, not the global list.
    const calledWithAgent = () =>
      rpcMock.mock.calls.some(
        ([method, params]) =>
          method === 'chat.commands' && params?.agent_id === 'alpha',
      );
    await waitForCondition(calledWithAgent, 100);

    expect(calledWithAgent()).toBe(true);
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

  it('tolerates a null context window in the token badge', async () => {
    // A model whose context window is unknown sends context_window: null in the
    // agent payload. The badge must show just the tokens — never "/ NaN" or a
    // crash (Phase 6 honest-gap contract).
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92 },
        contextWindow: null,
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    const numberFormat = new Intl.NumberFormat();
    const expectedBadge = `${numberFormat.format(3978)} tok`;

    await waitForCondition(
      () =>
        document.body.querySelector('.token-badge')?.textContent?.trim() ===
        expectedBadge,
      100,
    );

    const badgeText = document.body
      .querySelector('.token-badge')
      ?.textContent?.trim();
    expect(badgeText).toBe(expectedBadge);
    expect(badgeText).not.toContain('NaN');
    expect(badgeText).not.toContain('/');
  });

  it('shows the usage breakdown with cache tokens in the token badge tooltip', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: {
          input_tokens: 3886,
          output_tokens: 92,
          cache_read_tokens: 3000,
          cache_write_tokens: 200,
        },
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    const numberFormat = new Intl.NumberFormat();
    const expectedTooltip = [
      `Input: ${numberFormat.format(3886)} tok`,
      `Cache read: ${numberFormat.format(3000)} tok`,
      `Cache write: ${numberFormat.format(200)} tok`,
      `Output: ${numberFormat.format(92)} tok`,
    ].join('\n');

    await waitForCondition(
      () =>
        document.body.querySelector('.token-badge')?.getAttribute('title') ===
        expectedTooltip,
      100,
    );

    expect(
      document.body.querySelector('.token-badge')?.getAttribute('title'),
    ).toBe(expectedTooltip);
  });

  it('omits cache lines from the token badge tooltip without cache usage', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        usage: { input_tokens: 3886, output_tokens: 92 },
      }),
    );

    mountedComponent = mount(ChatView, { target: document.body });
    flushSync();

    const numberFormat = new Intl.NumberFormat();
    const expectedTooltip = [
      `Input: ${numberFormat.format(3886)} tok`,
      `Output: ${numberFormat.format(92)} tok`,
    ].join('\n');

    await waitForCondition(
      () =>
        document.body.querySelector('.token-badge')?.getAttribute('title') ===
        expectedTooltip,
      100,
    );

    expect(
      document.body.querySelector('.token-badge')?.getAttribute('title'),
    ).toBe(expectedTooltip);
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
    expect(findButtonByText('New session')).toBeTruthy();
    expect(findButtonByText('Refresh')).toBeFalsy();
    expect(document.body.querySelector('.chat-refresh')).toBeNull();
  });

  it('shows a bottom toast and skips run subscription when a toast command is handled', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamResponse: {
          command_handled: true,
          reply: 'Run cancelled.',
          output: 'toast',
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

    const sendButton = document.querySelector('.btn-primary.btn-icon');
    expect(sendButton).toBeTruthy();
    sendButton.click();

    await waitForCondition(
      () =>
        document.body
          .querySelector('.chat-view__command-toast')
          ?.textContent?.trim() === 'Run cancelled.',
      100,
    );

    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'session-1',
      content: '/stop',
    });
    expect(subscribeRunEventsMock).not.toHaveBeenCalled();
  });

  it('renders a transient card when a transient command is handled', async () => {
    const statusReply =
      'Agent: Alpha\nModel: claude-sonnet-4\nSession started: 2026-05-19';

    rpcMock.mockImplementation(
      createChatRpcMock({
        commandItems: [
          {
            name: 'status',
            description: 'Show current agent and session status.',
            type: 'command',
            argument: 'none',
            output: 'transient',
          },
        ],
        streamHandler: ({ content }) => {
          if (content === '/status') {
            return {
              command_handled: true,
              reply: statusReply,
              output: 'transient',
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
          .querySelector('.transient-card')
          ?.textContent?.includes('Agent: Alpha'),
      100,
    );

    const card = document.body.querySelector('.transient-card__body');
    expect(card?.textContent).toContain('Agent: Alpha\nModel: claude-sonnet-4');
    expect(card?.textContent).toContain('Session started: 2026-05-19');
    // Transient output is never echoed into the bottom toast.
    expect(document.body.querySelector('.chat-view__command-toast')).toBeNull();
    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'session-1',
      content: '/status',
    });
    expect(subscribeRunEventsMock).not.toHaveBeenCalled();
  });

  it('stacks transient cards so successive snapshots can be compared', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamHandler: ({ content }) => {
          if (content === '/status') {
            return {
              command_handled: true,
              reply: 'Agent: Alpha',
              output: 'transient',
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
      () => document.body.querySelectorAll('.transient-card').length === 1,
      100,
    );

    sendComposerMessage('/status');
    await waitForCondition(
      () => document.body.querySelectorAll('.transient-card').length === 2,
      100,
    );

    expect(document.body.querySelectorAll('.transient-card')).toHaveLength(2);
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

    // `/new` is an action command: it switches the session rather than showing
    // a toast or transient card.
    expect(document.body.querySelector('.chat-view__command-toast')).toBeNull();
    expect(document.body.querySelector('.transient-card')).toBeNull();
    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'session-1',
      content: '/new',
    });
    expect(subscribeRunEventsMock).not.toHaveBeenCalled();
  });

  it('switches to the new session returned by a same-agent /handoff command', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'session-handoff-same': [
            {
              id: 'handoff-same-assistant-one',
              role: 'assistant',
              content: 'Handoff target reply (same agent)',
            },
          ],
        },
        streamHandler: ({ content }) => {
          if (content === '/handoff') {
            return {
              command_handled: true,
              reply: 'Handoff sent to alpha. Opening new session.',
              data: {
                command: 'handoff',
                session_id: 'session-handoff-same',
                agent_id: 'alpha',
              },
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
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
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('/handoff');

    await waitForCondition(
      () =>
        rpcMock.mock.calls.some(
          ([method, params]) =>
            method === 'chat.history' &&
            params?.session_id === 'session-handoff-same',
        ),
      100,
    );

    // `/handoff` is an action command: no toast or transient card.
    expect(document.body.querySelector('.chat-view__command-toast')).toBeNull();
    expect(document.body.querySelector('.transient-card')).toBeNull();
    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'session-1',
      content: '/handoff',
    });
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'session-handoff-same',
      limit: 100,
    });
    expect(subscribeRunEventsMock).not.toHaveBeenCalled();
    expect(activeAgentTab()?.textContent).toContain('Alpha');
  });

  it('switches to a different agent and its new session for a cross-agent /handoff command', async () => {
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
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
          'session-handoff-cross': [
            {
              id: 'handoff-cross-assistant-one',
              role: 'assistant',
              content: 'Handoff target reply (cross agent)',
            },
          ],
        },
        streamHandler: ({ content }) => {
          if (content === '/handoff beta') {
            return {
              command_handled: true,
              reply: 'Handoff sent to beta. Opening new session.',
              data: {
                command: 'handoff',
                session_id: 'session-handoff-cross',
                agent_id: 'beta',
              },
            };
          }
          throw new Error(`Unexpected stream content: ${content}`);
        },
      }),
    );

    // Mirror App's behavior: `onAgentSelected` updates a reactive selected
    // id that flows back as `sharedSelectedAgentId`, so the agent-sync effect
    // observes the new selection and short-circuits.
    const parentHarness = createChatViewParentHarness();
    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: agents,
        get sharedSelectedAgentId() {
          return parentHarness.selectedAgentId;
        },
        onAgentSelected: (agentId) => {
          parentHarness.setSelectedAgentId(agentId);
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Parent main response'),
      100,
    );

    sendComposerMessage('/handoff beta');

    await waitForCondition(
      () =>
        document.body.textContent.includes(
          'Handoff target reply (cross agent)',
        ),
      100,
    );

    // Cross-agent `/handoff` is an action command: no toast or transient card.
    expect(document.body.querySelector('.chat-view__command-toast')).toBeNull();
    expect(document.body.querySelector('.transient-card')).toBeNull();
    expect(rpcMock).toHaveBeenCalledWith('chat.stream', {
      agent_id: 'alpha',
      session_id: 'parent-session',
      content: '/handoff beta',
    });
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'beta',
      session_id: 'session-handoff-cross',
      limit: 100,
    });
    expect(subscribeRunEventsMock).not.toHaveBeenCalled();
    expect(activeAgentTab()?.textContent).toContain('Beta');
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

  it('reloads history after a /compact that carries an instruction', async () => {
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
              command_handled: true,
              reply: 'Context compacted.',
              output: 'toast',
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

    // The reload is what surfaces the new compaction separator. The argument
    // form must trigger it just like the bare `/compact` does — regression
    // guard for `isCompactCommand` matching only the leading token.
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
          .querySelector('.chat-view__command-toast')
          ?.textContent?.trim() === 'Context compacted.',
      100,
    );
    await waitForCondition(() => historyReloadCount() > reloadsBefore, 100);

    expect(streamCalls).toEqual(['/compact focus on the auth work']);
    expect(historyReloadCount()).toBeGreaterThan(reloadsBefore);
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
    // Pin reconnect jitter to its midpoint so attempt 0 fires at exactly the
    // base 500ms delay this test advances by.
    const randomSpy = vi.spyOn(Math, 'random').mockReturnValue(0.5);
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
      randomSpy.mockRestore();
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
        pendingSessionNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
          subAgent: true,
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
        pendingSessionNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
          subAgent: true,
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
        pendingSessionNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
          subAgent: true,
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
        pendingSessionNavigation: {
          agentId: 'beta',
          sessionId: 'beta-sub-session',
          subAgent: true,
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
        pendingSessionNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
          subAgent: true,
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
        pendingSessionNavigation: {
          agentId: 'alpha',
          sessionId: 'sub-session-1',
          subAgent: true,
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
        pendingSessionNavigation: {
          agentId: 'beta',
          sessionId: 'beta-sub-session',
          subAgent: true,
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

    mountedComponent = mount(ChatView, {
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

    mountedComponent = mount(ChatView, {
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

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    // Initial mount attaches the SSE stream for the active run from history.
    await waitForCondition(() => Boolean(findButtonByText('Cancel run')), 100);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);

    // The server has lost the run (terminal event missed, bus buffer rolled,
    // or server restarted and the run is gone) — clear `activeRuns` so the
    // next `chat.history` response no longer carries an `active_run`.
    delete activeRuns['session-1'];

    // Trigger a second `loadHistoryForSession` via the sessions drawer.
    findButtonByText('Sessions')?.click();
    await waitForCondition(
      () => document.body.textContent.includes('session-1'),
      100,
    );
    findButtonByText('session-1')?.click();

    // Reconcile: the "Cancel run" button disappears, "New session" is no
    // longer disabled (so `canCreateNewSession(...)` is now true), and the
    // run stream's `closeSubscriptionFor` was called for this session key.
    await waitForCondition(
      () => findButtonByText('Cancel run') === undefined,
      100,
    );

    expect(findButtonByText('Cancel run')).toBeUndefined();
    expect(findButtonByText('New session')?.disabled).toBe(false);
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

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    await waitForCondition(() => Boolean(findButtonByText('Cancel run')), 100);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);

    // `chat.history` still reports the same active run — the active_run is
    // present on the second call, so no reconcile must fire.
    findButtonByText('Sessions')?.click();
    await waitForCondition(
      () => document.body.textContent.includes('session-1'),
      100,
    );
    findButtonByText('session-1')?.click();

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

    expect(findButtonByText('Cancel run')).toBeTruthy();
    expect(findButtonByText('New session')?.disabled).toBe(true);
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
        return secondHistoryDeferred;
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

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
      },
    });
    flushSync();

    // Wait for the first `chat.history` response to land and the SSE stream
    // to be attached for the initial active run.
    await waitForCondition(() => Boolean(findButtonByText('Cancel run')), 100);
    expect(subscribeRunEventsMock).toHaveBeenCalledTimes(1);

    // Trigger a second `loadHistoryForSession`. This call is held on the
    // deferred so we can race in a state mutation before it resolves.
    findButtonByText('Sessions')?.click();
    await waitForCondition(
      () => document.body.textContent.includes('session-1'),
      100,
    );
    findButtonByText('session-1')?.click();

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
    // The await resumes and runs the reconcile branch. `listQueue` is mocked
    // separately in `api.js` and does not hit `rpcMock`, so the total
    // `rpcMock` call count is `agent.list + chat.history × 2 + chat.commands`
    // = 4. Wait for the second history response to land by waiting for the
    // await chain inside `loadHistoryForSession` to reach the `await
    // syncSessionQueue(sessionState)` call.
    await waitForCondition(
      () => chatHistoryCallCount >= 2 && sessionState.currentRun !== null,
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
    expect(findButtonByText('Cancel run')).toBeTruthy();
    expect(findButtonByText('New session')?.disabled).toBe(true);
  });

  // Helper: render a single running sub-agent tool row in the parent
  // timeline (mirrors the fast-subagent test setup). The caller is
  // responsible for installing an `rpcMock.mockImplementation` first; this
  // helper does NOT overwrite it (the verify tests pass a custom mock that
  // must keep responding after the mount completes).
  async function mountChatViewWithRunningSubAgent() {
    mountedComponent = mount(ChatView, {
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
              blocking: false,
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

  it('verifySubAgentStatus: settles a stuck running sub-agent dot from a run_summary in chat.history (B5 regression)', async () => {
    rpcMock.mockImplementation(
      createVerifyRpcMock({
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

    await mountChatViewWithRunningSubAgent();

    // The verification call hits the public exported method (same one
    // the future `onVerifySubAgentStatus` callback chain will invoke).
    await mountedComponent.verifySubAgentStatus(
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
      createVerifyRpcMock({
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

    await mountChatViewWithRunningSubAgent();

    // First call: chat.history returns active_run → dot stays "running".
    await mountedComponent.verifySubAgentStatus(
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
    await mountedComponent.verifySubAgentStatus(
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

  // --- Two-bar project chat (Phase 2) -------------------------------------

  it('renders the project dropdown with No project default and identity chat unchanged', async () => {
    rpcMock.mockImplementation(createChatRpcMock());

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: '',
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const dropdown = document.querySelector('.chat-header__project-dropdown');
    expect(dropdown).toBeTruthy();
    // The shared Dropdown trigger reflects the current selection's label.
    const dropdownLabel = dropdown.querySelector(
      '.dropdown-primitive__trigger-label',
    );
    expect(dropdownLabel?.textContent?.trim()).toBe('No project');
    // No project chosen → no second bar, no project.show call.
    expect(document.querySelector('.chat-view__project-team')).toBeNull();
    expect(showProjectMock).not.toHaveBeenCalled();
    // Identity history call is byte-identical to today (bare agent id).
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'alpha',
      session_id: 'session-1',
      limit: 100,
    });
  });

  it('REGRESSION: a Personal identity send is byte-identical to today (bare agent id, no @projekt)', async () => {
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamHandler: (params) => {
          streamCalls.push(params);
          return {
            run_id: 'run-personal',
            sse_url: '/api/runs/run-personal/events',
            status: 'running',
            events: [],
          };
        },
      }),
    );

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        // A project exists in the dropdown but is NOT selected.
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: '',
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    sendComposerMessage('Personal hello');

    await waitForCondition(() => streamCalls.length === 1, 100);

    // The chat.stream payload carries the bare id — no `@projekt`, exactly
    // today's identity behavior.
    expect(streamCalls[0]).toEqual({
      agent_id: 'alpha',
      session_id: 'session-1',
      content: 'Personal hello',
    });
    expect(streamCalls[0].agent_id).not.toContain('@');
  });

  it('choosing a project loads its team and jumps to the default agent', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [
          { agent_id: 'reviewer', display_name: 'Reviewer', model: 'm' },
          { agent_id: 'builder', display_name: 'Builder', model: 'm' },
        ],
        report: { clean: true, findings: [] },
      },
    });
    // Project agent has an existing session listed (trap 1: newest wins).
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'builder-session',
          created_at: '2026-06-01T00:00:00+00:00',
          last_active_at: '2026-06-10T00:00:00+00:00',
        },
      ],
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'builder-session': [
            {
              id: 'builder-assistant-one',
              role: 'assistant',
              content: 'Builder project reply',
            },
          ],
        },
      }),
    );

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Builder project reply'),
      100,
    );

    // Second bar shows the scanned team.
    const teamBar = document.querySelector('.chat-view__project-team');
    expect(teamBar).toBeTruthy();
    expect(teamBar.textContent).toContain('Builder');
    expect(teamBar.textContent).toContain('Reviewer');

    // Jumped to the default agent (builder), not the first team member.
    const activeProjectTab = teamBar.querySelector('.agent-tab.active');
    expect(activeProjectTab?.textContent).toContain('Builder');

    // session.list and chat.history for the project agent use the FULL
    // address (trap 2).
    expect(listSessionsMock).toHaveBeenCalledWith('builder@vbot');
    expect(rpcMock).toHaveBeenCalledWith('chat.history', {
      agent_id: 'builder@vbot',
      session_id: 'builder-session',
      limit: 100,
    });
  });

  it('keeps only one agent selected across both bars when switching between identity and project agents', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [{ agent_id: 'builder', display_name: 'Builder', model: 'm' }],
        report: { clean: true, findings: [] },
      },
    });
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'builder-session',
          created_at: '2026-06-01T00:00:00+00:00',
          last_active_at: '2026-06-10T00:00:00+00:00',
        },
      ],
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'builder-session': [
            {
              id: 'builder-assistant-one',
              role: 'assistant',
              content: 'Builder project reply',
            },
          ],
        },
      }),
    );

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
      },
    });
    flushSync();

    await waitForCondition(
      () =>
        document
          .querySelector('.chat-view__project-team .agent-tab.active')
          ?.textContent?.includes('Builder'),
      100,
    );

    // Selecting the project agent must deselect the identity tab: exactly one
    // active tab across both bars (regression — the identity tab used to stay
    // highlighted because chatState.selectedAgentId is untouched).
    let activeTabs = document.querySelectorAll('.agent-tab.active');
    expect(activeTabs).toHaveLength(1);
    expect(activeTabs[0].textContent).toContain('Builder');
    expect(document.querySelector('.chat-header .agent-tab.active')).toBeNull();

    // Switching back to the identity agent moves the single selection up to the
    // header bar (the project team bar stays rendered but with no active tab).
    document.querySelector('.chat-header .agent-tab').click();
    await waitForCondition(
      () =>
        document
          .querySelector('.chat-header .agent-tab.active')
          ?.textContent?.includes('Alpha'),
      100,
    );
    activeTabs = document.querySelectorAll('.agent-tab.active');
    expect(activeTabs).toHaveLength(1);
    expect(activeTabs[0].textContent).toContain('Alpha');
    expect(
      document.querySelector('.chat-view__project-team .agent-tab.active'),
    ).toBeNull();
  });

  it('jumps to the first team member when the project has no default agent', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: '' },
      scan: {
        team: [
          { agent_id: 'first', display_name: 'First', model: 'm' },
          { agent_id: 'second', display_name: 'Second', model: 'm' },
        ],
        report: { clean: true, findings: [] },
      },
    });
    listSessionsMock.mockResolvedValue({ sessions: [] });
    // No sessions → session.create (default mock) returns
    // `created-first@vbot`, whose history is the new empty session.
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'created-first@vbot': [],
        },
      }),
    );

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
      },
    });
    flushSync();

    await waitForCondition(
      () =>
        document
          .querySelector('.chat-view__project-team .agent-tab.active')
          ?.textContent?.includes('First'),
      100,
    );

    // Trap 1: no current_session_id, no listed session → session.create with
    // the full address and NO make_current.
    expect(rpcMock).toHaveBeenCalledWith('session.create', {
      agent_id: 'first@vbot',
    });
    const createCall = rpcMock.mock.calls.find(
      ([method]) => method === 'session.create',
    );
    expect(createCall[1]).not.toHaveProperty('make_current');
  });

  it('renders an empty second bar without error for an empty project team', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'empty', default_agent: '' },
      scan: { team: [], report: { clean: true, findings: [] } },
    });
    rpcMock.mockImplementation(createChatRpcMock());

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'empty', display_name: 'Empty' }],
        selectedProjectId: 'empty',
      },
    });
    flushSync();

    await waitForCondition(
      () => Boolean(document.querySelector('.chat-view__project-team')),
      100,
    );

    const teamBar = document.querySelector('.chat-view__project-team');
    expect(teamBar.querySelector('.agent-tab')).toBeNull();
    expect(teamBar.textContent).toContain('no agents');
    // No project agent selected, no error notice.
    expect(document.querySelector('.chat-view__error')).toBeNull();
    // The identity agent above stays active and chattable.
    expect(activeAgentTab()?.textContent).toContain('Alpha');
  });

  it('sends a project-agent message with the full address and syncs the queue with the bare id (trap 2)', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [{ agent_id: 'builder', display_name: 'Builder', model: 'm' }],
        report: { clean: true, findings: [] },
      },
    });
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'builder-session',
          created_at: '2026-06-01T00:00:00+00:00',
          last_active_at: '2026-06-10T00:00:00+00:00',
        },
      ],
    });
    const streamCalls = [];
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: {
          'builder-session': [
            {
              id: 'builder-assistant-one',
              role: 'assistant',
              content: 'Builder project reply',
            },
          ],
        },
        streamHandler: (params) => {
          streamCalls.push(params);
          return {
            run_id: 'run-proj',
            sse_url: '/api/runs/run-proj/events',
            status: 'running',
            events: [],
          };
        },
      }),
    );

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Builder project reply'),
      100,
    );

    // The queue sync that ran during history load used the BARE id (trap 2).
    expect(listQueueMock).toHaveBeenCalledWith('builder', 'builder-session');
    // It must NEVER be called with the full address.
    expect(listQueueMock).not.toHaveBeenCalledWith(
      'builder@vbot',
      'builder-session',
    );

    sendComposerMessage('Hello project agent');

    await waitForCondition(() => streamCalls.length === 1, 100);

    // chat.stream parses an address → FULL address (trap 2).
    expect(streamCalls[0]).toEqual({
      agent_id: 'builder@vbot',
      session_id: 'builder-session',
      content: 'Hello project agent',
    });
  });

  it('shows the scan banner for an unclean project and links into the Projects tab', async () => {
    const onNavigateToProjects = vi.fn();
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [{ agent_id: 'builder', display_name: 'Builder', model: 'm' }],
        report: {
          clean: false,
          findings: [
            {
              type: 'bad_model',
              detail: 'unknown model',
              agent_id: 'builder',
            },
          ],
        },
      },
    });
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'builder-session',
          created_at: '2026-06-01T00:00:00+00:00',
          last_active_at: '2026-06-10T00:00:00+00:00',
        },
      ],
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        sessionMessages: { 'builder-session': [] },
      }),
    );

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
        onNavigateToProjects,
      },
    });
    flushSync();

    await waitForCondition(
      () => Boolean(document.querySelector('.project-scan-banner')),
      100,
    );

    const link = document.querySelector('.project-scan-banner__link');
    expect(link).toBeTruthy();
    link.click();
    flushSync();
    expect(onNavigateToProjects).toHaveBeenCalledTimes(1);
  });

  it('does not show the scan banner for a clean project', async () => {
    showProjectMock.mockResolvedValue({
      project: { project_id: 'vbot', default_agent: 'builder' },
      scan: {
        team: [{ agent_id: 'builder', display_name: 'Builder', model: 'm' }],
        report: { clean: true, findings: [] },
      },
    });
    listSessionsMock.mockResolvedValue({
      sessions: [
        {
          id: 'builder-session',
          created_at: '2026-06-01T00:00:00+00:00',
          last_active_at: '2026-06-10T00:00:00+00:00',
        },
      ],
    });
    rpcMock.mockImplementation(
      createChatRpcMock({ sessionMessages: { 'builder-session': [] } }),
    );

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        projects: [{ project_id: 'vbot', display_name: 'vBot' }],
        selectedProjectId: 'vbot',
      },
    });
    flushSync();

    await waitForCondition(
      () => Boolean(document.querySelector('.chat-view__project-team')),
      100,
    );

    expect(document.querySelector('.project-scan-banner')).toBeNull();
  });

  it('re-syncs a held session queue on a matching queue resource_changed', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const parentHarness = createChatViewParentHarness();

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        get queueInvalidation() {
          return parentHarness.queueInvalidation;
        },
      },
    });
    flushSync();

    // The initial history load syncs the current session's queue once.
    await waitForCondition(
      () =>
        listQueueMock.mock.calls.some(
          ([agentId, sessionId]) =>
            agentId === 'alpha' && sessionId === 'session-1',
        ),
      100,
    );
    const callsBefore = listQueueMock.mock.calls.length;

    // A queue signal for a session this window does not hold is ignored.
    parentHarness.setQueueInvalidation({
      agentId: 'alpha',
      sessionId: 'unheld',
    });
    flushSync();
    expect(listQueueMock.mock.calls.length).toBe(callsBefore);

    // A queue signal for the held session re-syncs just that session's queue.
    parentHarness.setQueueInvalidation({
      agentId: 'alpha',
      sessionId: 'session-1',
    });
    flushSync();

    expect(listQueueMock.mock.calls.length).toBe(callsBefore + 1);
    expect(listQueueMock).toHaveBeenLastCalledWith('alpha', 'session-1');
  });

  it('does not switch the viewed conversation on a sessions resource_changed', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    const { createChatViewParentHarness } =
      await import('./chatViewParentHarness.svelte.js');
    const parentHarness = createChatViewParentHarness();

    mountedComponent = mount(ChatView, {
      target: document.body,
      props: {
        sharedAgents: [createAgent()],
        sharedSelectedAgentId: 'alpha',
        get sessionsRefreshToken() {
          return parentHarness.sessionsRefreshToken;
        },
      },
    });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const historyCallsBefore = rpcMock.mock.calls.filter(
      ([method]) => method === 'chat.history',
    ).length;

    // A sessions signal refreshes the session list (drawer) only — it must not
    // reload the agent or switch the viewed conversation ("stay put").
    parentHarness.bumpSessionsRefreshToken();
    flushSync();

    const historyCallsAfter = rpcMock.mock.calls.filter(
      ([method]) => method === 'chat.history',
    ).length;
    expect(historyCallsAfter).toBe(historyCallsBefore);
    expect(document.body.textContent).toContain('Hello');
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

    if (method === 'session.create') {
      // Deterministic session id derived from the address so project-agent
      // session-create tests can assert against it. `builder@vbot` →
      // `created-builder@vbot`.
      const agentId =
        typeof params?.agent_id === 'string' ? params.agent_id : '';
      return { agent_id: agentId, session_id: `created-${agentId}` };
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

  const sendButton = document.querySelector('.btn-primary.btn-icon');
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
