import { afterEach, vi } from 'vitest';

afterEach(() => {
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
    sendTerminalInput: vi.fn().mockResolvedValue({}),
    resizeTerminal: vi.fn(async (_id, columns, rows) => ({
      terminal: { columns, rows },
    })),
    startTerminal: vi.fn().mockResolvedValue({}),
    killTerminal: vi.fn().mockResolvedValue({}),
    forgetTerminal: vi.fn().mockResolvedValue({}),
    createTerminalGroup: vi.fn().mockResolvedValue({}),
    renameTerminalGroup: vi.fn().mockResolvedValue({}),
    deleteTerminalGroup: vi.fn().mockResolvedValue({}),
    setTerminalGroupOrder: vi.fn().mockResolvedValue({}),
    subscribeTerminalEvents: vi.fn((_terminalId, handlers) => {
      const connection = {
        close: vi.fn(),
        socket: { readyState: socketReadyState },
      };
      streams.push({
        connection,
        emit: (event) => handlers.onEvent(event),
        close: () => handlers.onClose(),
      });
      return connection;
    }),
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

function span(row, column) {
  return { row, column, rowSpan: 1, columnSpan: 1 };
}

export { fakeApi, manual, terminal, launchHistory, span };
