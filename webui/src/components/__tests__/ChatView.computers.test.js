// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import {
  createAgent,
  createChatRpcMock,
  findNewSessionButton,
  flushSync,
  getSessionMock,
  rpcCalls,
  rpcMock,
  runningRun,
  sendComposerMessage,
  setupChatViewTestSuite,
  waitForCondition,
} from './ChatView.support.js';

const { default: ChatView } = await import('../ChatView.svelte');
const { default: ComputerContextHost } =
  await import('./ComputerContextHost.support.svelte');

const computers = [
  { id: 'server', name: 'Server', online: true, capabilities: ['terminal'] },
  {
    id: 'pc_laptop',
    name: 'Work laptop',
    online: true,
    capabilities: ['terminal', 'computer_use'],
  },
  {
    id: 'pc_office',
    name: 'Office PC',
    online: true,
    capabilities: ['computer_use'],
  },
];
const execution = {
  computer_id: 'pc_laptop',
  allowed_computer_ids: ['pc_laptop'],
};
const openComputers = () => {
  document
    .querySelector('button[aria-label="Computers for this conversation"]')
    .click();
  flushSync();
};
const defaultPicker = () =>
  document.querySelector('button[aria-label="Default computer"]');
async function chooseComputer(name) {
  defaultPicker().click();
  flushSync();
  await waitForCondition(() => document.querySelector('[role="option"]'));
  [...document.querySelectorAll('[role="option"]')]
    .find((item) => item.textContent.includes(name))
    .click();
  flushSync();
}
function closeComputers() {
  document.querySelector('.modal-footer button').click();
  flushSync();
}

describe('ChatView computer selection', () => {
  const chat = setupChatViewTestSuite();

  it.each(['pending', 'failed', 'missing'])(
    'does not invent a default for an existing Session while its metadata read is %s',
    async (failure) => {
      let resolveOld;
      getSessionMock.mockImplementation(() => {
        if (failure === 'pending')
          return new Promise((resolve) => {
            resolveOld = resolve;
          });
        if (failure === 'failed') return Promise.reject(new Error('offline'));
        return { session: null };
      });
      let stored = execution;
      const base = createChatRpcMock();
      rpcMock.mockImplementation((method, params) => {
        if (method === 'session.set_execution') {
          stored = params.execution;
          return { execution: stored };
        }
        return base(method, params);
      });
      chat.mount(
        {
          target: document.body,
          props: {
            component: ChatView,
            readComputers: async () => ({ computers }),
          },
        },
        ComputerContextHost,
      );
      flushSync();
      await waitForCondition(
        () =>
          getSessionMock.mock.calls.length > 0 &&
          document
            .querySelector(
              'button[aria-label="Computers for this conversation"]',
            )
            ?.textContent.includes('Computer not loaded'),
      );
      openComputers();
      expect(defaultPicker()).toBeNull();
      expect(
        document.querySelector('.modal-body [role="checkbox"]'),
      ).toBeNull();
      expect(rpcCalls('session.set_execution')).toEqual([]);

      getSessionMock.mockImplementation(async () => ({
        session: { id: 'session-1', execution: stored },
      }));
      [...document.querySelectorAll('.modal-body button')]
        .find((button) => button.textContent.trim() === 'Retry')
        .click();
      flushSync();
      await waitForCondition(() =>
        defaultPicker()?.textContent.includes('Work laptop'),
      );
      expect(defaultPicker().disabled).toBe(false);
      resolveOld?.({ session: { id: 'session-1' } });
      await Promise.resolve();
      flushSync();
      expect(defaultPicker().textContent).toContain('Work laptop');

      document
        .querySelector('[role="checkbox"][aria-label="Office PC"]')
        .click();
      flushSync();
      await waitForCondition(
        () => rpcCalls('session.set_execution').length === 1,
      );
      expect(rpcCalls('session.set_execution')[0].execution).toEqual({
        computer_id: 'pc_laptop',
        allowed_computer_ids: ['pc_laptop', 'pc_office'],
      });
    },
  );

  it('sends explicit computer grants with a draft first message without changing Agent defaults', async () => {
    rpcMock.mockImplementation(
      createChatRpcMock({
        agents: [createAgent({ current_session_id: '' })],
        streamHandler: () => ({
          ...runningRun('run-computer'),
          session_id: 'created',
        }),
      }),
    );
    chat.mount(
      {
        target: document.body,
        props: {
          component: ChatView,
          readComputers: async () => ({ computers }),
        },
      },
      ComputerContextHost,
    );
    flushSync();
    await waitForCondition(() =>
      document.querySelector(
        'button[aria-label="Computers for this conversation"]',
      ),
    );
    openComputers();
    await chooseComputer('Work laptop');
    const serverGrant = document.querySelector(
      '[role="checkbox"][aria-label="Server"]',
    );
    expect(serverGrant.getAttribute('aria-checked')).toBe('false');
    serverGrant.click();
    flushSync();
    closeComputers();
    sendComposerMessage('Use my laptop and check the server too.');
    await waitForCondition(() => rpcCalls('chat.stream').length === 1);
    expect(rpcCalls('chat.stream')[0].new_session.execution).toEqual({
      computer_id: 'pc_laptop',
      allowed_computer_ids: ['pc_laptop', 'server'],
    });
    expect(rpcCalls('agent.update')).toEqual([]);
    expect(rpcCalls('session.set_execution')).toEqual([]);
    findNewSessionButton().click();
    flushSync();
    await waitForCondition(() =>
      document
        .querySelector('button[aria-label="Computers for this conversation"]')
        ?.textContent.includes('Server'),
    );
  });

  it('keeps an offline or missing Session computer selected and saves an explicit replacement', async () => {
    let stored = execution;
    getSessionMock.mockImplementation(async () => ({
      session: { id: 'session-1', execution: stored },
    }));
    const base = createChatRpcMock();
    rpcMock.mockImplementation((method, params) => {
      if (method === 'session.set_execution') {
        stored = params.execution;
        return { execution: stored };
      }
      return base(method, params);
    });
    let catalog;
    let listed = computers.map((computer) => ({
      ...computer,
      online: computer.id !== 'pc_laptop',
    }));
    chat.mount(
      {
        target: document.body,
        props: {
          component: ChatView,
          readComputers: async () => ({ computers: listed }),
          onCatalog: (value) => {
            catalog = value;
          },
        },
      },
      ComputerContextHost,
    );
    flushSync();
    await waitForCondition(() =>
      document
        .querySelector('button[aria-label="Computers for this conversation"]')
        ?.textContent.includes('Offline'),
    );
    openComputers();
    expect(defaultPicker().textContent).toContain('Work laptop');
    listed = computers.filter((computer) => computer.id !== 'pc_laptop');
    await catalog.refresh();
    flushSync();
    expect(defaultPicker().textContent).toContain('pc_laptop');
    expect(rpcCalls('session.set_execution')).toEqual([]);
    await chooseComputer('Server');
    await waitForCondition(
      () => rpcCalls('session.set_execution').length === 1,
    );
    expect(rpcCalls('session.set_execution')[0]).toEqual({
      agent_id: 'alpha',
      session_id: 'session-1',
      execution: { computer_id: 'server', allowed_computer_ids: ['server'] },
    });
  });

  it('shows the selected computer but locks access changes during an active Run', async () => {
    getSessionMock.mockResolvedValue({
      session: { id: 'session-1', execution },
    });
    rpcMock.mockImplementation(
      createChatRpcMock({
        activeRuns: { 'session-1': runningRun('run-active') },
      }),
    );
    chat.mount(
      {
        target: document.body,
        props: {
          component: ChatView,
          readComputers: async () => ({ computers }),
        },
      },
      ComputerContextHost,
    );
    flushSync();
    await waitForCondition(() =>
      document
        .querySelector('button[aria-label="Computers for this conversation"]')
        ?.textContent.includes('Work laptop'),
    );
    openComputers();
    await waitForCondition(() => defaultPicker()?.disabled);
    expect(
      [...document.querySelectorAll('[role="checkbox"]')].every(
        (item) => item.disabled,
      ),
    ).toBe(true);
    expect(rpcCalls('session.set_execution')).toEqual([]);
  });
});
