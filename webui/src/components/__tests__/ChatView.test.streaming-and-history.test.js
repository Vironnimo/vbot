// @vitest-environment jsdom
import {
  describe,
  createChatRpcMock,
  expect,
  flushSync,
  it,
  rpcMock,
  sendComposerMessage,
  setInputValue,
  setupChatViewTestSuite,
  subscribeRunEventsMock,
  vi,
  waitForCondition,
} from './ChatView.support.js';

describe('ChatView', () => {
  const chatViewTest = setupChatViewTestSuite();

  it('settles a Background Bash completion without a reactive update loop', async () => {
    rpcMock.mockImplementation(createChatRpcMock());
    chatViewTest.mount({
      target: document.body,
      props: {
        backgroundBashStatusEvents: [
          {
            payload: {
              process_id: 'proc-test',
              status: 'completed',
              started_at: '2026-09-08T12:00:00Z',
              finished_at: '2026-09-08T12:00:10Z',
              output: 'finished',
              exit_code: 0,
            },
          },
        ],
      },
    });
    flushSync();
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );
    const input = document.querySelector('textarea');
    setInputValue(input, 'Continue after completion');
    flushSync();
    expect(input.value).toBe('Continue after completion');
    expect(document.querySelector('[aria-label="Send message"]').disabled).toBe(
      false,
    );
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

    chatViewTest.mount({ target: document.body });
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

    chatViewTest.mount({ target: document.body });
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
              background: true,
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

    chatViewTest.mount({ target: document.body });
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

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const composerInput = document.querySelector('.msg-input');
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

    chatViewTest.mount({ target: document.body });
    flushSync();

    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );

    const composerInput = document.querySelector('.msg-input');
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
});
