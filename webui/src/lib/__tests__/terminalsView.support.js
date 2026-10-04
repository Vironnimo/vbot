import { afterEach, vi } from 'vitest';

import {
  createTerminalsController,
  createTerminalsViewState,
} from '../terminalsView.js';

const controllers = [];

afterEach(() => {
  for (const controller of controllers.splice(0)) {
    controller.destroy();
  }
  vi.useRealTimers();
});

function fakeApi({
  streams,
  terminals = [terminal('term-1')],
  groups,
  socketReadyState = 1,
}) {
  const groupList = groups ?? [
    {
      ...manual(),
      terminal_count: terminals.length,
      live_count: terminals.length,
    },
  ];
  return {
    listTerminals: vi.fn().mockResolvedValue({
      groups: groupList.map((item) => ({ ...item })),
      terminals: terminals.map((item) => ({ ...item })),
    }),
    startTerminal: vi.fn().mockResolvedValue({}),
    killTerminal: vi.fn().mockResolvedValue({}),
    forgetTerminal: vi.fn().mockResolvedValue({}),
    createTerminalGroup: vi.fn().mockResolvedValue({}),
    renameTerminalGroup: vi.fn().mockResolvedValue({}),
    deleteTerminalGroup: vi.fn().mockResolvedValue({}),
    setTerminalGroupOrder: vi.fn().mockResolvedValue({}),
    subscribeTerminalEvents: vi.fn((_terminalId, handlers) => {
      // The requests sent over this socket, in order.
      const sent = [];
      const connection = {
        close: vi.fn(),
        send: vi.fn((message) => {
          if (connection.socket.readyState !== 1) return false;
          sent.push(message);
          return true;
        }),
        socket: { readyState: socketReadyState },
      };
      streams.push({
        connection,
        sent,
        emit: (event) => handlers.onEvent(event),
        close: () => handlers.onClose(),
      });
      return connection;
    }),
  };
}

// A controller over a fake API whose server lists `terminals` in `groups`.
// `streams` collects one entry per opened Terminal socket. The suite destroys
// every controller after the test.
function createTerminals({
  controller: controllerOptions,
  ...apiOptions
} = {}) {
  const state = createTerminalsViewState();
  const streams = [];
  const api = fakeApi({ streams, ...apiOptions });
  const controller = createTerminalsController({
    state,
    api,
    ...controllerOptions,
  });
  controllers.push(controller);
  return { state, streams, api, controller };
}

async function startTerminals(options) {
  const harness = createTerminals(options);
  await harness.controller.start();
  return harness;
}

function readyEvent(terminalId, sequence, ansi = '', changes = {}) {
  return {
    type: 'terminal_ready',
    sequence,
    ansi,
    terminal: terminal(terminalId, changes),
  };
}

function manual(overrides = {}) {
  return {
    group_id: 'auto:manual',
    name: 'Manual',
    kind: 'automatic',
    terminal_count: 0,
    live_count: 0,
    order: [],
    ...overrides,
  };
}

function userGroup(groupId, name, terminalCount) {
  return manual({
    group_id: groupId,
    name,
    kind: 'user',
    terminal_count: terminalCount,
    live_count: terminalCount,
  });
}

function terminal(terminalId, changes = {}) {
  return {
    terminal_id: terminalId,
    group_id: 'auto:manual',
    state: 'working',
    command: 'python',
    title: '',
    workdir: 'C:\\repo',
    pid: 123,
    started_at: '2026-08-03T12:00:00+00:00',
    columns: 120,
    rows: 32,
    owner: {
      project_id: null,
      agent_id: 'main',
      session_id: 'session-1',
    },
    attention: null,
    ...changes,
  };
}

function launchHistory(id, changes = {}) {
  return {
    id,
    command: null,
    args: [],
    workdir: null,
    used_at: '2026-08-08T10:00:00+00:00',
    ...changes,
  };
}

// Resolve later: returns [promise, resolve, reject].
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return [promise, resolve, reject];
}

export {
  startTerminals,
  readyEvent,
  manual,
  userGroup,
  terminal,
  launchHistory,
  deferred,
};
