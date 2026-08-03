// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const listTerminalsMock = vi.fn();
const sendTerminalInputMock = vi.fn();
const resizeTerminalMock = vi.fn();
const killTerminalMock = vi.fn();
const subscribeTerminalEventsMock = vi.fn();
const streams = [];
const terminalInstances = [];

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  listTerminals: (...args) => listTerminalsMock(...args),
  sendTerminalInput: (...args) => sendTerminalInputMock(...args),
  resizeTerminal: (...args) => resizeTerminalMock(...args),
  killTerminal: (...args) => killTerminalMock(...args),
  subscribeTerminalEvents: (...args) => subscribeTerminalEventsMock(...args),
}));

vi.mock('@xterm/xterm', () => ({
  Terminal: class MockTerminal {
    constructor(options) {
      this.options = options;
      this.cols = 120;
      this.rows = 32;
      this.reset = vi.fn();
      this.resize = vi.fn((columns, rows) => {
        this.cols = columns;
        this.rows = rows;
      });
      this.write = vi.fn((_data, callback) => callback?.());
      this.dispose = vi.fn();
      this.focus = vi.fn();
      terminalInstances.push(this);
    }

    loadAddon() {}
    open() {}
    onData(callback) {
      this.onDataCallback = callback;
      return { dispose: vi.fn() };
    }
  },
}));

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class MockFitAddon {
    fit() {}
  },
}));

const { default: TerminalsView } = await import('../TerminalsView.svelte');

describe('TerminalsView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    streams.length = 0;
    terminalInstances.length = 0;
    listTerminalsMock.mockReset();
    sendTerminalInputMock.mockReset().mockResolvedValue({});
    resizeTerminalMock.mockReset().mockResolvedValue({});
    killTerminalMock.mockReset().mockResolvedValue({});
    subscribeTerminalEventsMock
      .mockReset()
      .mockImplementation((_id, handlers) => {
        const connection = { close: vi.fn() };
        streams.push({ handlers, connection });
        return connection;
      });
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('shows active ownership and renders the live ANSI snapshot without owning lifetime', async () => {
    listTerminalsMock.mockResolvedValue({ terminals: [terminal()] });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);

    streams[0].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 2,
      terminal: terminal({
        columns: 100,
        rows: 28,
        attention: {
          revision: 1,
          kind: 'output_settled',
          summary: 'Quiet is not a semantic prompt.',
        },
      }),
      ansi: '\u001b[2JDemo TUI ready',
    });
    flushSync();

    expect(document.body.textContent).toContain('python');
    expect(document.body.textContent).toContain('PTY');
    expect(document.body.textContent).toContain('main@vbot');
    expect(document.body.textContent).toContain('Take control');
    expect(document.body.textContent).not.toContain(
      'Quiet is not a semantic prompt.',
    );
    expect(terminalInstances[0].resize).toHaveBeenCalledWith(100, 28);
    expect(terminalInstances[0].write).toHaveBeenCalledWith(
      '\u001b[2JDemo TUI ready',
      expect.any(Function),
    );

    await unmount(mountedComponent);
    mountedComponent = null;
    expect(streams[0].connection.close).toHaveBeenCalledWith(
      1000,
      'terminals-view-close',
    );
    expect(killTerminalMock).not.toHaveBeenCalled();
  });

  it('renders the shared empty state when no Terminal Session is active', async () => {
    listTerminalsMock.mockResolvedValue({ terminals: [] });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() =>
      document.body.textContent.includes('No active terminals'),
    );

    expect(document.body.textContent).toContain('Nothing to monitor yet');
    expect(subscribeTerminalEventsMock).not.toHaveBeenCalled();
  });
});

function terminal(changes = {}) {
  return {
    terminal_id: 'term-1',
    state: 'working',
    command: 'python',
    workdir: 'C:\\Development\\vBot',
    pid: 4321,
    started_at: '2026-08-03T12:00:00+00:00',
    columns: 120,
    rows: 32,
    owner: {
      project_id: 'vbot',
      agent_id: 'main',
      session_id: 'session-one',
    },
    attention: null,
    ...changes,
  };
}

async function waitFor(predicate, attempts = 50) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    flushSync();
    if (predicate()) {
      return;
    }
  }
  throw new Error('condition was not reached');
}
