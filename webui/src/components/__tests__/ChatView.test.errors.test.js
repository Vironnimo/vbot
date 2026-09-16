// @vitest-environment jsdom
import {
  describe,
  createChatRpcMock,
  expect,
  flushSync,
  it,
  rpcMock,
  sendComposerMessage,
  setupChatViewTestSuite,
  subscribeRunEventsMock,
  testChatStateRefs,
  waitForCondition,
} from './ChatView.support.js';

describe('ChatView error placement', () => {
  const suite = setupChatViewTestSuite();

  it.each([
    ['global', 'historyError'],
    ['global', 'actionError'],
    ['global', 'commandsError'],
    ['session', 'actionError'],
    ['session', 'streamError'],
  ])('keeps %s %s beside the composer until cleared', async (scope, field) => {
    rpcMock.mockImplementation(createChatRpcMock());
    suite.mount({ target: document.body });
    flushSync();
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );
    const state = testChatStateRefs[0];
    const owner = scope === 'global' ? state : Object.values(state.sessions)[0];
    owner[field] = `test-${scope}-${field}`;
    flushSync();
    const notice = document.querySelector('.chat-view__composer-feedback');
    expect(notice.closest('.chat-view__footer-stack')).not.toBeNull();
    expect(notice.textContent).toContain(owner[field]);
    expect(document.querySelector('.chat-view__notice-stack')).toBeNull();
    owner[field] = '';
    flushSync();
    expect(document.querySelector('.chat-view__composer-feedback')).toBeNull();
  });

  it('keeps a rejected send visible beside the unchanged draft', async () => {
    const failure = 'test-admission-failure';
    rpcMock.mockImplementation(
      createChatRpcMock({
        streamHandler: () => {
          throw new Error(failure);
        },
      }),
    );
    suite.mount({ target: document.body });
    flushSync();
    await waitForCondition(
      () => document.body.textContent.includes('Hello'),
      100,
    );
    sendComposerMessage('test-unsent-draft');
    await waitForCondition(
      () => document.body.textContent.includes(failure),
      100,
    );
    expect(
      document.querySelector('.chat-view__composer-feedback').textContent,
    ).toContain(failure);
    expect(document.querySelector('textarea').value).toBe('test-unsent-draft');
    expect(document.querySelector('.msg--error')).toBeNull();
  });

  it.each([false, true])(
    'shows a Run failure once in the timeline, persisted error: %s',
    async (persisted) => {
      const body = {
        error: {
          message: 'test-provider-failure',
          metadata: { remedy_hint: 'test-remedy' },
        },
      };
      const error = `Rate limited: 429 ${JSON.stringify(body)}`;
      rpcMock.mockImplementation(
        createChatRpcMock({
          sessionMessages: { 'session-1': [] },
          streamResponse: {
            run_id: 'failed-run',
            status: 'running',
            events: [],
            sse_url: '/test-events',
          },
        }),
      );
      suite.mount({ target: document.body });
      flushSync();
      await waitForCondition(
        () =>
          document.querySelector('textarea') &&
          !document.querySelector('textarea').disabled,
        100,
      );
      sendComposerMessage('test-failing-request');
      await waitForCondition(
        () => subscribeRunEventsMock.mock.calls.length === 1,
        100,
      );
      const handlers = subscribeRunEventsMock.mock.calls[0][1];
      let sequence = 0;
      if (persisted)
        handlers.onEvent({
          data: {
            type: 'error_message_persisted',
            run_id: 'failed-run',
            sequence: ++sequence,
            payload: {
              message: { id: 'test-error', role: 'error', content: error },
            },
          },
        });
      handlers.onEvent({
        data: {
          type: 'run_failed',
          run_id: 'failed-run',
          sequence: sequence + 1,
          payload: { status: 'failed', error },
        },
      });
      await waitForCondition(
        () => document.querySelector('.error-details'),
        100,
      );
      expect(document.querySelectorAll('.error-details')).toHaveLength(1);
      const details = document.querySelector('.error-details');
      expect(details.closest('.chat-view__timeline-shell')).not.toBeNull();
      details.querySelector('summary').click();
      expect(details.open).toBe(true);
      expect(JSON.parse(details.querySelector('pre').textContent)).toEqual(
        body,
      );
      expect(document.querySelector('.chat-view__notice-stack')).toBeNull();
      expect(
        document.querySelector('.chat-view__composer-feedback'),
      ).toBeNull();
    },
  );
});
