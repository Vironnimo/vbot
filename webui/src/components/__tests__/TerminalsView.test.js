// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const listTerminalsMock = vi.fn();
const startTerminalMock = vi.fn();
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
  startTerminal: (...args) => startTerminalMock(...args),
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
      this.refresh = vi.fn();
      this.resize = vi.fn((columns, rows) => {
        this.cols = columns;
        this.rows = rows;
      });
      this.write = vi.fn((_data, callback) => callback?.());
      this.dispose = vi.fn();
      this.focus = vi.fn();
      this.scrollToBottom = vi.fn(() => {
        this.buffer.active.viewportY = this.buffer.active.baseY;
      });
      this.buffer = { active: { viewportY: 0, baseY: 0 } };
      terminalInstances.push(this);
    }

    loadAddon() {}
    open() {}
    onData(callback) {
      this.onDataCallback = callback;
      return { dispose: vi.fn() };
    }
    onScroll(callback) {
      this.onScrollCallback = callback;
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
    startTerminalMock.mockReset().mockResolvedValue({});
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
        title: 'Auth refactor · Codex',
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

    expect(document.body.textContent).toContain('Auth refactor · Codex');
    expect(document.body.textContent).toContain('python');
    expect(document.body.textContent).toContain('PTY');
    expect(document.body.textContent).toContain('main@vbot');
    expect(document.querySelector('button[role="switch"]')).toBeTruthy();
    expect(document.body.textContent).not.toContain(
      'Quiet is not a semantic prompt.',
    );
    expect(terminalInstances[0].resize).toHaveBeenCalledWith(100, 28);
    expect(terminalInstances[0].write).toHaveBeenCalledWith(
      '\u001b[2JDemo TUI ready',
      expect.any(Function),
    );
    expect(terminalInstances[0].refresh).toHaveBeenCalledWith(0, 27);

    await unmount(mountedComponent);
    mountedComponent = null;
    expect(streams[0].connection.close).toHaveBeenCalledWith(
      1000,
      'terminals-view-close',
    );
    expect(killTerminalMock).not.toHaveBeenCalled();
  });

  it('uses the shared secondary sidebar header, action, list, and selection contract', async () => {
    listTerminalsMock.mockResolvedValue({ terminals: [terminal()] });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1);

    const pane = document.querySelector('.terminals-view__list-pane');
    const header = pane.querySelector('.secondary-pane__header');
    const list = pane.querySelector('.secondary-pane__scroll.secondary-list');
    const item = list.querySelector('.secondary-list__item');

    expect(header.querySelector('#terminals-list-title')).toBeTruthy();
    expect(header.querySelector('button').textContent.trim()).toBe('Add');
    expect(item.classList.contains('active')).toBe(true);
    expect(item.getAttribute('aria-current')).toBe('true');
  });

  it('renders the shared empty state when no Terminal Session is active', async () => {
    listTerminalsMock.mockResolvedValue({ terminals: [] });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() =>
      document.querySelector('.terminals-view__list .empty-state'),
    );

    expect(
      document.querySelector('.terminals-view__detail > .empty-state'),
    ).toBeTruthy();
    expect(subscribeTerminalEventsMock).not.toHaveBeenCalled();
  });

  it('humanizes a shell command while no program has announced a title', async () => {
    listTerminalsMock.mockResolvedValue({
      terminals: [terminal({ command: 'pwsh.exe', title: '' })],
    });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1);

    expect(document.body.textContent).toContain('PowerShell');
    expect(document.body.textContent).toContain('pwsh.exe');
  });

  it('rebuilds retained scrollback when the Terminals tab is mounted again', async () => {
    listTerminalsMock.mockResolvedValue({ terminals: [terminal()] });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    streams[0].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 1,
      terminal: terminal(),
      ansi: '\u001b[2Jfirst visit',
    });
    await unmount(mountedComponent);
    mountedComponent = null;

    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);
    const retainedSnapshot =
      '\u001b[2J\u001b[Hhistory-0\r\nhistory-1\r\ncurrent screen';
    streams[1].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 4,
      terminal: terminal(),
      ansi: retainedSnapshot,
    });

    expect(terminalInstances[1].write).toHaveBeenCalledWith(
      retainedSnapshot,
      expect.any(Function),
    );
  });

  it('keeps a finished Terminal Session available as read-only history', async () => {
    listTerminalsMock.mockResolvedValue({
      terminals: [terminal({ state: 'exited' })],
    });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    streams[0].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 6,
      terminal: terminal({ state: 'exited' }),
      ansi: '\u001b[2Jretained final answer',
    });
    flushSync();

    expect(
      document.querySelector('.terminals-view__state-dot--exited'),
    ).toBeTruthy();
    expect(
      document.querySelector('.terminals-view__terminal-mode'),
    ).toBeTruthy();
    expect(
      [...document.querySelectorAll('button')].some(
        (button) => button.textContent.trim() === 'Stop terminal',
      ),
    ).toBe(false);
    expect(document.querySelector('.terminals-view__controls')).toBeNull();
  });

  it('starts a manual terminal from the modal and enables direct control', async () => {
    listTerminalsMock.mockResolvedValue({ terminals: [] });
    startTerminalMock.mockResolvedValue({
      terminal: terminal({
        terminal_id: 'manual-1',
        command: 'codex',
        owner: null,
      }),
    });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() =>
      document.querySelector('.terminals-view__detail > .empty-state'),
    );

    findButton('Add').click();
    flushSync();
    setField('#terminal-start-command', 'codex');
    setField('#terminal-start-arguments', '--profile\nwork space');
    setField('#terminal-start-workdir', 'C:\\repo');
    document
      .querySelector('#terminal-start-form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    expect(startTerminalMock).toHaveBeenCalledWith({
      command: 'codex',
      args: ['--profile', 'work space'],
      workdir: 'C:\\repo',
    });
    expect(
      document.querySelector(
        '.terminals-view__terminal-shell[data-control="enabled"]',
      ),
    ).toBeTruthy();
    expect(terminalInstances[0].options.disableStdin).toBe(false);
    expect(terminalInstances[0].focus).toHaveBeenCalled();
  });

  it('prefills the last launch and can select an older persistent setup', async () => {
    listTerminalsMock.mockResolvedValue({
      terminals: [],
      launch_history: [
        launchHistory({
          id: 'recent',
          command: 'codex',
          args: ['--profile', 'daily'],
          workdir: 'C:\\Development\\vBot',
        }),
        launchHistory({
          id: 'older',
          command: 'python',
          args: ['-m', 'http.server', '8080'],
          workdir: 'C:\\Sites\\docs',
        }),
      ],
    });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() =>
      document.querySelector('.terminals-view__detail > .empty-state'),
    );

    findButton('Add').click();
    flushSync();
    expect(document.querySelector('#terminal-start-command').value).toBe(
      'codex',
    );
    expect(document.querySelector('#terminal-start-arguments').value).toBe(
      '--profile\ndaily',
    );
    expect(document.querySelector('#terminal-start-workdir').value).toBe(
      'C:\\Development\\vBot',
    );

    document.querySelector('#terminal-start-history').click();
    await waitFor(() =>
      [...document.querySelectorAll('button')].some((button) =>
        button.textContent.includes('python -m http.server 8080'),
      ),
    );
    [...document.querySelectorAll('button')]
      .find((button) =>
        button.textContent.includes('python -m http.server 8080'),
      )
      .click();
    flushSync();
    expect(document.querySelector('#terminal-start-command').value).toBe(
      'python',
    );
    expect(document.querySelector('#terminal-start-arguments').value).toBe(
      '-m\nhttp.server\n8080',
    );
    expect(document.querySelector('#terminal-start-workdir').value).toBe(
      'C:\\Sites\\docs',
    );
  });

  it('takes control on terminal click, forwards native keys, and exposes scrollback recovery', async () => {
    listTerminalsMock.mockResolvedValue({ terminals: [terminal()] });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);

    expect(terminalInstances[0].options.disableStdin).toBe(true);
    expect(
      document.querySelector(
        '.terminals-view__terminal-shell[data-control="observe"]',
      ),
    ).toBeTruthy();
    document.querySelector('.terminals-view__terminal-host').dispatchEvent(
      new MouseEvent('pointerdown', {
        bubbles: true,
        button: 0,
      }),
    );
    flushSync();
    expect(
      document.querySelector(
        '.terminals-view__terminal-shell[data-control="enabled"]',
      ),
    ).toBeTruthy();
    expect(terminalInstances[0].options.disableStdin).toBe(false);
    expect(terminalInstances[0].focus).toHaveBeenCalled();

    terminalInstances[0].onDataCallback('\u001b[A');
    terminalInstances[0].onDataCallback('\r');
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(sendTerminalInputMock).toHaveBeenCalledWith('term-1', '\u001b[A\r');

    terminalInstances[0].buffer.active = { viewportY: 3, baseY: 12 };
    terminalInstances[0].onScrollCallback();
    flushSync();
    findButton('Jump to latest').click();
    expect(terminalInstances[0].scrollToBottom).toHaveBeenCalledTimes(1);
  });
});

function terminal(changes = {}) {
  return {
    terminal_id: 'term-1',
    state: 'working',
    command: 'python',
    title: '',
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

function launchHistory(changes = {}) {
  return {
    id: 'launch-1',
    command: null,
    args: [],
    workdir: null,
    used_at: '2026-08-08T10:00:00+00:00',
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

function findButton(label) {
  const button = [...document.querySelectorAll('button')].find(
    (item) => item.textContent.trim() === label,
  );
  if (!button) {
    throw new Error(`button not found: ${label}`);
  }
  return button;
}

function setField(selector, value) {
  const field = document.querySelector(selector);
  field.value = value;
  field.dispatchEvent(new Event('input', { bubbles: true }));
}
