import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  TERMINAL_STREAM_CONNECTED,
  TERMINAL_STREAM_RECONNECTING,
  createTerminalsController,
  createTerminalsViewState,
  reconcileTerminalList,
  selectedTerminal,
} from '../terminalsView.js';

afterEach(() => {
  vi.useRealTimers();
});

describe('terminal list projection', () => {
  it('keeps a valid selection and excludes finished terminals', () => {
    const state = createTerminalsViewState();
    state.selectedTerminalId = 'term-2';

    reconcileTerminalList(state, {
      terminals: [
        terminal('term-1'),
        terminal('term-2', { state: 'ready' }),
        terminal('term-3', { state: 'exited' }),
      ],
    });

    expect(state.selectedTerminalId).toBe('term-2');
    expect(state.terminals.map((item) => item.terminal_id)).toEqual([
      'term-1',
      'term-2',
    ]);
    expect(selectedTerminal(state)?.state).toBe('ready');
  });
});

describe('terminal live controller', () => {
  it('applies an authoritative snapshot, ordered output, and reconnects on a gap', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const snapshots = [];
    const output = [];
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({
      state,
      api,
      onSnapshot: (ansi) => snapshots.push(ansi),
      onOutput: (data) => output.push(data),
    });

    await controller.start();
    expect(state.selectedTerminalId).toBe('term-1');
    expect(streams).toHaveLength(1);

    streams[0].emit({
      type: 'terminal_ready',
      sequence: 4,
      terminal: terminal('term-1'),
      ansi: '\u001b[2Jready',
    });
    streams[0].emit({ type: 'terminal_output', sequence: 5, data: 'next' });
    streams[0].emit({
      type: 'terminal_state',
      sequence: 6,
      terminal: terminal('term-1', { title: 'Codex tests' }),
    });

    expect(state.streamStatus).toBe(TERMINAL_STREAM_CONNECTED);
    expect(snapshots).toEqual(['\u001b[2Jready']);
    expect(output).toEqual(['next']);
    expect(selectedTerminal(state)?.title).toBe('Codex tests');

    streams[0].emit({ type: 'terminal_output', sequence: 8, data: 'gap' });
    expect(state.streamStatus).toBe(TERMINAL_STREAM_RECONNECTING);
    expect(streams[0].connection.close).toHaveBeenCalled();
    await vi.runAllTimersAsync();
    expect(streams).toHaveLength(2);
    controller.destroy();
  });

  it('does not reconnect when the authoritative snapshot is already finished', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    api.listTerminals.mockResolvedValueOnce({ terminals: [] });
    streams[0].emit({
      type: 'terminal_ready',
      sequence: 5,
      terminal: terminal('term-1', { state: 'exited' }),
      ansi: '\u001b[2Jdone',
    });
    streams[0].close();
    await vi.runAllTimersAsync();

    expect(streams).toHaveLength(1);
    expect(state.terminals).toEqual([]);
    expect(state.selectedTerminalId).toBe('');
    controller.destroy();
  });

  it('batches input, debounces resize, and removes an explicitly killed terminal', async () => {
    vi.useFakeTimers();
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    controller.queueInput('hello');
    controller.queueInput('\r', { immediate: true });
    controller.resize(100, 30);
    controller.resize(110, 32);
    await vi.runAllTimersAsync();

    expect(api.sendTerminalInput).toHaveBeenCalledWith('term-1', 'hello\r');
    expect(api.resizeTerminal).toHaveBeenCalledWith('term-1', 110, 32);

    api.listTerminals.mockResolvedValueOnce({ terminals: [] });
    await expect(controller.killSelected()).resolves.toBe(true);
    expect(api.killTerminal).toHaveBeenCalledWith('term-1');
    expect(state.terminals).toEqual([]);
    expect(state.selectedTerminalId).toBe('');
    controller.destroy();
  });

  it('starts, selects, and connects one manual terminal', async () => {
    const state = createTerminalsViewState();
    const streams = [];
    const api = fakeApi({ streams });
    const controller = createTerminalsController({ state, api });

    await controller.start();
    api.startTerminal.mockResolvedValueOnce({
      terminal: terminal('manual-1', { command: 'codex', owner: null }),
    });

    const started = await controller.startManualTerminal({ command: 'codex' });

    expect(api.startTerminal).toHaveBeenCalledWith({ command: 'codex' });
    expect(started).toMatchObject({ terminal_id: 'manual-1', owner: null });
    expect(state.selectedTerminalId).toBe('manual-1');
    expect(state.startError).toBe('');
    expect(streams).toHaveLength(2);
    expect(streams[0].connection.close).toHaveBeenCalledWith(
      1000,
      'terminals-view-close',
    );
    controller.destroy();
  });
});

function fakeApi({ streams }) {
  return {
    listTerminals: vi
      .fn()
      .mockResolvedValue({ terminals: [terminal('term-1')] }),
    sendTerminalInput: vi.fn().mockResolvedValue({}),
    startTerminal: vi.fn().mockResolvedValue({}),
    resizeTerminal: vi.fn().mockResolvedValue({}),
    killTerminal: vi.fn().mockResolvedValue({}),
    subscribeTerminalEvents: vi.fn((_terminalId, handlers) => {
      const connection = { close: vi.fn() };
      streams.push({
        connection,
        emit: (event) => handlers.onEvent(event),
        close: () => handlers.onClose(),
      });
      return connection;
    }),
  };
}

function terminal(terminalId, changes = {}) {
  return {
    terminal_id: terminalId,
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
