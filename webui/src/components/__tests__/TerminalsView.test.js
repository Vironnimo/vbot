// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const listTerminalsMock = vi.fn();
const startTerminalMock = vi.fn();
const sendTerminalInputMock = vi.fn();
const resizeTerminalMock = vi.fn();
const killTerminalMock = vi.fn();
const forgetTerminalMock = vi.fn();
const createTerminalGroupMock = vi.fn();
const renameTerminalGroupMock = vi.fn();
const deleteTerminalGroupMock = vi.fn();
const setTerminalGroupOrderMock = vi.fn();
const subscribeTerminalEventsMock = vi.fn();
const streams = [];
const terminalInstances = [];
const fitAddons = [];
const resizeObservers = [];
let mockHostWidth = 800;
let mockHostHeight = 512;

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  listTerminals: (...args) => listTerminalsMock(...args),
  startTerminal: (...args) => startTerminalMock(...args),
  sendTerminalInput: (...args) => sendTerminalInputMock(...args),
  resizeTerminal: (...args) => resizeTerminalMock(...args),
  killTerminal: (...args) => killTerminalMock(...args),
  forgetTerminal: (...args) => forgetTerminalMock(...args),
  createTerminalGroup: (...args) => createTerminalGroupMock(...args),
  renameTerminalGroup: (...args) => renameTerminalGroupMock(...args),
  deleteTerminalGroup: (...args) => deleteTerminalGroupMock(...args),
  setTerminalGroupOrder: (...args) => setTerminalGroupOrderMock(...args),
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

    loadAddon = vi.fn((addon) => {
      if (addon && typeof addon.activate === 'function') {
        addon.activate(this);
      }
    });
    open(host) {
      Object.defineProperties(host, {
        clientWidth: { get: () => mockHostWidth },
        clientHeight: { get: () => mockHostHeight },
      });
      const element = document.createElement('div');
      const screen = document.createElement('div');
      element.className = 'xterm';
      screen.className = 'xterm-screen';
      screen.getBoundingClientRect = () => ({
        left: 10,
        top: 20,
        width: this.cols * 8,
        height: this.rows * 16,
      });
      Object.defineProperties(screen, {
        offsetWidth: { get: () => this.cols * 8 },
        offsetHeight: { get: () => this.rows * 16 },
      });
      element.append(screen);
      host.append(element);
      this.element = element;
    }
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
    constructor() {
      this.terminal = null;
      this.fit = vi.fn(() => {
        if (!this.terminal?.element?.parentElement) {
          return;
        }
        const host = this.terminal.element.parentElement;
        const cellWidth = 8;
        const cellHeight = 16;
        const cols = Math.max(1, Math.floor(host.clientWidth / cellWidth));
        const rows = Math.max(1, Math.floor(host.clientHeight / cellHeight));
        this.terminal.resize(cols, rows);
      });
      fitAddons.push(this);
    }
    activate(terminal) {
      this.terminal = terminal;
    }
    dispose() {}
  },
}));

// A real browser fires a ResizeObserver when the tile host is laid out,
// which drives scheduleFit. Same-turn observe + scheduleFit must not
// confirm the grid: the follow-up measurement happens on the next
// animation frame.
class MockResizeObserver {
  constructor(callback) {
    this.callback = callback;
    resizeObservers.push(this);
  }
  observe() {
    this.callback();
  }
  disconnect() {}
  fire() {
    this.callback();
  }
}
vi.stubGlobal('ResizeObserver', MockResizeObserver);

const { default: TerminalsView } = await import('../TerminalsView.svelte');

describe('TerminalsView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    streams.length = 0;
    terminalInstances.length = 0;
    fitAddons.length = 0;
    resizeObservers.length = 0;
    mockHostWidth = 800;
    mockHostHeight = 512;
    listTerminalsMock.mockReset();
    startTerminalMock.mockReset().mockResolvedValue({});
    sendTerminalInputMock.mockReset().mockResolvedValue({});
    resizeTerminalMock.mockReset().mockResolvedValue({});
    killTerminalMock.mockReset().mockResolvedValue({});
    forgetTerminalMock.mockReset().mockResolvedValue({});
    createTerminalGroupMock.mockReset().mockResolvedValue({});
    renameTerminalGroupMock.mockReset().mockResolvedValue({});
    deleteTerminalGroupMock.mockReset().mockResolvedValue({});
    setTerminalGroupOrderMock.mockReset().mockResolvedValue({});
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
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
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
    expect(document.body.textContent).not.toContain('python');
    expect(document.body.textContent).not.toContain('PID 4321');
    expect(document.body.textContent).toContain('main@vbot');
    expect(document.querySelector('.terminals-view__tile-bar-meta')).toBeNull();
    expect(terminalInstances[0].options.theme.background).toBe('#0E0D0B');
    expect(terminalInstances[0].loadAddon).toHaveBeenCalledWith(fitAddons[0]);
    await flushAnimationFrames(2);
    expect(document.querySelector('button[role="switch"]')).toBeNull();
    expect(document.body.textContent).not.toContain(
      'Quiet is not a semantic prompt.',
    );
    expect(terminalInstances[0].resize).toHaveBeenCalled();
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

  it('loads the FitAddon, fits the host grid, and never loads an accelerated renderer', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();

    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    await flushAnimationFrames(2);

    expect(fitAddons).toHaveLength(1);
    expect(terminalInstances[0].loadAddon).toHaveBeenCalledTimes(1);
    expect(terminalInstances[0].loadAddon).toHaveBeenCalledWith(fitAddons[0]);
    expect(fitAddons[0].fit).toHaveBeenCalled();
    expect(terminalInstances[0].cols).toBe(100);
    expect(terminalInstances[0].rows).toBe(32);
  });

  it('shows a diagnostics hint when a tile keeps rendering at a size the session never confirmed', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    // The tile fits 100x32 while the session still runs at its start size:
    // while the correction is in flight the hint stays quiet.
    await flushAnimationFrames(2);
    expect(document.querySelector('.terminals-view__grid-mismatch')).toBeNull();

    // The server confirms a different size than the fitted tile: the
    // pipeline closed without reconciling the two grids.
    resizeTerminalMock.mockResolvedValue({ columns: 120, rows: 40 });
    mockHostHeight = 660;
    resizeObservers[0].fire();
    await flushAnimationFrames(2);
    await new Promise((resolve) => setTimeout(resolve, 150));
    await waitFor(() =>
      Boolean(document.querySelector('.terminals-view__grid-mismatch')),
    );
    const hint = document.querySelector('.terminals-view__grid-mismatch');
    expect(hint.textContent).toContain('Session');
  });

  it('uses the compact top group bar and keeps both terminal actions available', async () => {
    listTerminalsMock.mockResolvedValue({
      groups: [manualGroup({ terminal_count: 1, live_count: 1 })],
      terminals: [terminal()],
    });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1);

    const toolbar = document.querySelector('.terminals-view__toolbar');
    const tabs = toolbar.querySelector('.terminals-view__group-tabs');
    const item = tabs.querySelector('.terminals-view__group-tab');

    expect(document.querySelector('.terminals-view__list-pane')).toBeNull();
    expect(
      [...toolbar.querySelectorAll('button')].some(
        (button) => button.textContent.trim() === 'Add group',
      ),
    ).toBe(true);
    expect(item.classList.contains('active')).toBe(true);
    expect(item.getAttribute('aria-current')).toBe('true');
    expect(
      [...toolbar.querySelectorAll('button')].some(
        (button) => button.textContent.trim() === 'New terminal',
      ),
    ).toBe(true);
    expect(document.querySelector('.terminals-view__header')).toBeNull();
  });

  it('switches the visible terminal group from the top group bar', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse(
        [terminal()],
        [
          manualGroup({ terminal_count: 1, live_count: 1 }),
          {
            group_id: 'group-work',
            name: 'Work',
            kind: 'user',
            terminal_count: 0,
            live_count: 0,
            order: [],
          },
        ],
      ),
    );
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1);

    [...document.querySelectorAll('.terminals-view__group-tab')]
      .find((button) => button.textContent.includes('Work'))
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(
      [...document.querySelectorAll('.terminals-view__group-tab')]
        .find((button) => button.textContent.includes('Work'))
        .getAttribute('aria-current'),
    ).toBe('true');
    expect(
      document.querySelector('.terminals-view__detail > .empty-state'),
    ).toBeTruthy();
  });

  it('reveals a single group action menu instead of two inline buttons', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse(
        [terminal({ group_id: 'group-work' })],
        [
          {
            group_id: 'group-work',
            name: 'Work',
            kind: 'user',
            terminal_count: 1,
            live_count: 1,
            order: [],
          },
        ],
      ),
    );
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1);

    const trigger = document.querySelector(
      '.terminals-view__group-action-menu-trigger',
    );
    expect(trigger).toBeTruthy();
    expect(trigger.getAttribute('aria-haspopup')).toBe('menu');

    // The menu is closed until the trigger is engaged.
    expect(document.querySelector('.terminals-view__group-menu')).toBeNull();

    trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    await waitFor(() => document.querySelector('.terminals-view__group-menu'));

    const items = [
      ...document.querySelectorAll('.terminals-view__group-menu-item'),
    ];
    expect(
      items.some((item) => item.textContent.trim() === 'Rename group'),
    ).toBe(true);
    expect(
      items.some((item) => item.textContent.trim() === 'Delete group'),
    ).toBe(true);
    const deleteItem = items.find(
      (item) => item.textContent.trim() === 'Delete group',
    );
    expect(deleteItem.classList).toContain(
      'terminals-view__group-menu-item--danger',
    );
  });

  it('renders the shared empty state when no Terminal Session is active', async () => {
    listTerminalsMock.mockResolvedValue({ groups: [], terminals: [] });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() =>
      document.querySelector('.terminals-view__group-status'),
    );

    expect(
      document.querySelector('.terminals-view__detail > .empty-state'),
    ).toBeTruthy();
    expect(subscribeTerminalEventsMock).not.toHaveBeenCalled();
  });

  it('humanizes a shell command while no program has announced a title', async () => {
    listTerminalsMock.mockResolvedValue({
      groups: [manualGroup({ terminal_count: 1, live_count: 1 })],
      terminals: [terminal({ command: 'pwsh.exe', title: '' })],
    });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1);

    expect(document.body.textContent).toContain('PowerShell');
    expect(document.body.textContent).not.toContain('pwsh.exe');
  });

  it('rebuilds retained scrollback when the Terminals tab is mounted again', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
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
      groups: [
        manualGroup(),
        {
          group_id: 'finished',
          name: 'Finished',
          kind: 'finished',
          terminal_count: 1,
          live_count: 0,
          order: [],
        },
      ],
      terminals: [terminal({ group_id: 'finished', state: 'exited' })],
    });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    streams[0].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 6,
      terminal: terminal({ group_id: 'finished', state: 'exited' }),
      ansi: '\u001b[2Jretained final answer',
    });
    flushSync();

    expect(
      [...document.querySelectorAll('.terminals-view__group-tab')].some(
        (element) => element.textContent.includes('Finished'),
      ),
    ).toBe(true);
    expect(
      [...document.querySelectorAll('button')].some(
        (button) => button.getAttribute('aria-label') === 'Close terminal',
      ),
    ).toBe(true);
    expect(terminalInstances[0].options.disableStdin).toBe(true);
  });

  it('closes a finished Terminal Session and removes it from the list', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([terminal({ state: 'exited', exit_code: 0 })]),
    );
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    streams[0].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 6,
      terminal: terminal({ state: 'exited', exit_code: 0 }),
      ansi: '\\u001b[2Jretained final answer',
    });
    flushSync();

    expect(document.querySelector('.terminals-view__exit-code')).toBeNull();

    findButtonByAriaLabel('Close terminal').click();
    flushSync();
    // The tile disappears immediately; the catalog removal runs in the
    // background.
    expect(document.querySelectorAll('.terminals-view__tile')).toHaveLength(0);
    await waitFor(() => forgetTerminalMock.mock.calls.length > 0);
    expect(forgetTerminalMock).toHaveBeenCalledWith('term-1');
    expect(killTerminalMock).not.toHaveBeenCalled();
  });

  it('closes a running Terminal Session with one click: stop, then remove', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);

    findButtonByAriaLabel('Close terminal').click();
    flushSync();
    // The tile disappears immediately; the server-side stop and catalog
    // removal continue in the background — no confirmation dialog in
    // between.
    expect(document.querySelectorAll('.terminals-view__tile')).toHaveLength(0);
    await waitFor(() => killTerminalMock.mock.calls.length > 0);
    await waitFor(() => forgetTerminalMock.mock.calls.length > 0);
    expect(killTerminalMock).toHaveBeenCalledWith('term-1');
    expect(forgetTerminalMock).toHaveBeenCalledWith('term-1');
    expect(
      document.querySelector('[role="dialog"][aria-modal="true"]'),
    ).toBeNull();
  });

  it('starts a terminal from the modal and focuses it for direct input', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([]));
    startTerminalMock.mockResolvedValue({
      terminal: terminal({
        terminal_id: 'manual-1',
        command: 'codex',
        owner: null,
        group_id: 'auto:manual',
      }),
    });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() =>
      document.querySelector('.terminals-view__detail > .empty-state'),
    );

    findButton('New terminal').click();
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
    expect(terminalInstances[0].options.disableStdin).toBe(false);
    expect(terminalInstances[0].focus).toHaveBeenCalled();
    expect(
      document.querySelector('button[aria-label="Take control"]'),
    ).toBeNull();
    expect(
      document.querySelector('button[aria-label="Release control"]'),
    ).toBeNull();
  });

  it('prefills the last launch and can select an older persistent setup', async () => {
    listTerminalsMock.mockResolvedValue({
      groups: [],
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

    findButton('New terminal').click();
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

  it('focuses on terminal click, forwards native keys, and exposes scrollback recovery', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);

    expect(terminalInstances[0].options.disableStdin).toBe(false);
    document.querySelector('.terminals-view__tile-host').dispatchEvent(
      new MouseEvent('pointerdown', {
        bubbles: true,
        button: 0,
      }),
    );
    flushSync();
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

  it('renders one tile per listed terminal with title, owner, and compact actions', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([
        terminal({ terminal_id: 'term-1', title: 'First terminal' }),
        terminal({ terminal_id: 'term-2', title: 'Second terminal' }),
      ]),
    );
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2);
    expect(streams).toHaveLength(2);
    await waitFor(() => terminalInstances.length === 2);
    expect(terminalInstances).toHaveLength(2);

    const tiles = document.querySelectorAll('.terminals-view__tile');
    expect(tiles).toHaveLength(2);
    const firstBar = tiles[0].querySelector('.terminals-view__tile-bar');
    expect(firstBar.textContent).toContain('First terminal');
    expect(firstBar.textContent).toContain('main@vbot');
    expect(
      firstBar.querySelectorAll('.terminals-view__tile-action'),
    ).toHaveLength(2);
    expect(
      firstBar
        .querySelector('.terminals-view__tile-action svg')
        .getAttribute('width'),
    ).toBe('14');
    expect(
      document.querySelector('button[aria-label="Take control"]'),
    ).toBeNull();
    expect(
      document.querySelector('button[aria-label="Release control"]'),
    ).toBeNull();
  });

  it('maximizes a tile and resizes the PTY immediately from the fitted dimensions', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    await waitFor(() => fitAddons[0].fit.mock.calls.length > 0);

    expect(terminalInstances[0].cols).toBe(100);
    expect(terminalInstances[0].rows).toBe(32);
    // Maximize is a deterministic user action: its fit resizes the PTY
    // immediately, without waiting for a second measurement.
    document
      .querySelector('button[aria-label="Maximize"]')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await waitFor(() => resizeTerminalMock.mock.calls.length > 0);
    expect(resizeTerminalMock).toHaveBeenCalledWith('term-1', 100, 32);
  });

  it('follows a one-shot tile shrink with a second fit so the PTY resizes', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(resizeObservers).toHaveLength(1);
    resizeTerminalMock.mockClear();

    mockHostWidth = 400;
    resizeObservers[0].fire();
    await Promise.resolve();
    flushSync();
    expect(resizeTerminalMock).not.toHaveBeenCalled();

    await flushAnimationFrames(1);
    // A settled size is still debounced before it reaches the PTY.
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(resizeTerminalMock).toHaveBeenCalledWith('term-1', 50, 32);
  });

  it('re-fits the first tile when a second terminal joins the canvas', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    startTerminalMock.mockResolvedValue({
      terminal: terminal({
        terminal_id: 'term-2',
        title: 'Second terminal',
        command: 'opencode',
      }),
    });
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    const fitsBefore = fitAddons[0].fit.mock.calls.length;

    findButton('New terminal').click();
    flushSync();
    document
      .querySelector('#terminal-start-form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await waitFor(() => terminalInstances.length === 2);
    await Promise.resolve();
    flushSync();
    await flushAnimationFrames(1);

    expect(fitAddons[0].fit.mock.calls.length).toBeGreaterThan(fitsBefore);
  });

  it('rebuilds both tiles after the Terminals tab is mounted again', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([
        terminal({ terminal_id: 'term-1', title: 'First terminal' }),
        terminal({ terminal_id: 'term-2', title: 'Second terminal' }),
      ]),
    );
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);

    await unmount(mountedComponent);
    mountedComponent = null;

    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 4 && terminalInstances.length === 4);
    streams[2].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 2,
      terminal: terminal({
        terminal_id: 'term-1',
        title: 'First terminal',
        columns: 100,
        rows: 32,
      }),
      ansi: '\u001b[2Jfirst remount snapshot',
    });
    streams[3].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 2,
      terminal: terminal({
        terminal_id: 'term-2',
        title: 'Second terminal',
        columns: 100,
        rows: 32,
      }),
      ansi: '\u001b[2Jsecond remount snapshot',
    });
    flushSync();
    expect(terminalInstances[2].write).toHaveBeenCalledWith(
      '\u001b[2Jfirst remount snapshot',
      expect.any(Function),
    );
    expect(terminalInstances[3].write).toHaveBeenCalledWith(
      '\u001b[2Jsecond remount snapshot',
      expect.any(Function),
    );
  });

  it('focuses the first tile and switches focus via a tile bar click', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([
        terminal({ terminal_id: 'term-1', title: 'First terminal' }),
        terminal({ terminal_id: 'term-2', title: 'Second terminal' }),
      ]),
    );
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);

    const tiles = document.querySelectorAll('.terminals-view__tile');
    expect(tiles[0].classList.contains('terminals-view__tile--focused')).toBe(
      true,
    );
    expect(tiles[1].classList.contains('terminals-view__tile--focused')).toBe(
      false,
    );

    document
      .querySelectorAll('.terminals-view__tile-bar')[1]
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(
      document
        .querySelectorAll('.terminals-view__tile')[0]
        .classList.contains('terminals-view__tile--focused'),
    ).toBe(false);
    expect(
      document
        .querySelectorAll('.terminals-view__tile')[1]
        .classList.contains('terminals-view__tile--focused'),
    ).toBe(true);
    expect(terminalInstances[1].focus).toHaveBeenCalled();
  });

  it('maximizes a tile to fill the canvas and restores the grid', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([
        terminal({ terminal_id: 'term-1', title: 'First terminal' }),
        terminal({ terminal_id: 'term-2', title: 'Second terminal' }),
      ]),
    );
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);

    const canvas = document.querySelector('.terminals-view__canvas');
    expect(canvas.getAttribute('style')).toContain('repeat(2, minmax(0, 1fr))');

    document
      .querySelector('button[aria-label="Maximize"]')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(canvas.getAttribute('style')).toContain('repeat(1, minmax(0, 1fr))');
    expect(canvas.classList.contains('terminals-view__canvas--maximized')).toBe(
      true,
    );
    const tiles = document.querySelectorAll('.terminals-view__tile');
    expect(tiles[0].classList.contains('terminals-view__tile--maximized')).toBe(
      true,
    );
    expect(tiles[1].classList.contains('terminals-view__tile--hidden')).toBe(
      true,
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(resizeTerminalMock).toHaveBeenCalledWith('term-1', 100, 32);

    document
      .querySelector('button[aria-label="Restore"]')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(canvas.getAttribute('style')).toContain('repeat(2, minmax(0, 1fr))');
    expect(canvas.classList.contains('terminals-view__canvas--maximized')).toBe(
      false,
    );
    expect(
      document.querySelectorAll('.terminals-view__tile--hidden'),
    ).toHaveLength(0);
    expect(
      document.querySelectorAll('.terminals-view__tile--maximized'),
    ).toHaveLength(0);
  });

  it('activates the clicked tile and routes its input without ownership modes', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([
        terminal({ terminal_id: 'term-1', title: 'First terminal' }),
        terminal({ terminal_id: 'term-2', title: 'Second terminal' }),
      ]),
    );
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);

    document.querySelectorAll('.terminals-view__tile-host')[1].dispatchEvent(
      new MouseEvent('pointerdown', {
        bubbles: true,
        button: 0,
      }),
    );
    flushSync();

    const tiles = document.querySelectorAll('.terminals-view__tile');
    expect(tiles[0].classList.contains('terminals-view__tile--focused')).toBe(
      false,
    );
    expect(tiles[1].classList.contains('terminals-view__tile--focused')).toBe(
      true,
    );
    expect(terminalInstances[1].options.disableStdin).toBe(false);
    expect(terminalInstances[1].focus).toHaveBeenCalled();
    expect(terminalInstances[0].options.disableStdin).toBe(false);

    terminalInstances[1].onDataCallback('ls');
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(sendTerminalInputMock).toHaveBeenCalledWith('term-2', 'ls');
  });

  it('keeps maximized output streaming and restores without re-initializing tiles', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([
        terminal({ terminal_id: 'term-1', title: 'First terminal' }),
        terminal({ terminal_id: 'term-2', title: 'Second terminal' }),
      ]),
    );
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);

    document
      .querySelector('button[aria-label="Maximize"]')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    streams[0].handlers.onEvent({
      type: 'terminal_output',
      sequence: 1,
      data: 'more output',
    });
    expect(terminalInstances[0].write).toHaveBeenCalledWith('more output');

    document
      .querySelector('button[aria-label="Restore"]')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    expect(terminalInstances).toHaveLength(2);
    expect(
      document.querySelectorAll('.terminals-view__tile-host'),
    ).toHaveLength(2);
  });

  it('reorders terminals in a user group by drag and drop on the tile bar', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse(
        [
          terminal({
            terminal_id: 'term-1',
            title: 'First terminal',
            group_id: 'group-1',
          }),
          terminal({
            terminal_id: 'term-2',
            title: 'Second terminal',
            group_id: 'group-1',
          }),
        ],
        [
          {
            group_id: 'group-1',
            name: 'Work',
            kind: 'user',
            terminal_count: 2,
            live_count: 2,
            order: [],
          },
        ],
      ),
    );
    mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);

    const bars = document.querySelectorAll('.terminals-view__tile-bar');
    expect(bars[0].getAttribute('draggable')).toBe('true');

    function dragEvent(type) {
      const event = new MouseEvent(type, {
        bubbles: true,
        cancelable: true,
      });
      let payload = '';
      const dataTransfer = {
        effectAllowed: '',
        dropEffect: '',
        setData: (_kind, value) => {
          payload = value;
        },
        getData: () => payload,
      };
      Object.defineProperty(event, 'dataTransfer', { value: dataTransfer });
      return event;
    }

    bars[0].dispatchEvent(dragEvent('dragstart'));
    bars[1].dispatchEvent(dragEvent('dragover'));
    bars[1].dispatchEvent(dragEvent('drop'));
    flushSync();

    expect(setTerminalGroupOrderMock).toHaveBeenCalledWith('group-1', [
      'term-2',
      'term-1',
    ]);
    const tiles = document.querySelectorAll('.terminals-view__tile');
    expect(tiles[0].getAttribute('data-terminal-id')).toBe('term-2');
  });
});

function terminal(changes = {}) {
  return {
    terminal_id: 'term-1',
    group_id: 'auto:manual',
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

function manualGroup(overrides = {}) {
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

function terminalListResponse(terminals, groups) {
  const groupList = groups ?? [
    manualGroup({
      terminal_count: terminals.length,
      live_count: terminals.length,
    }),
  ];
  return { groups: groupList, terminals };
}

function flushAnimationFrames(count = 1) {
  let wait = Promise.resolve();
  for (let index = 0; index < count; index += 1) {
    wait = wait.then(
      () => new Promise((resolve) => requestAnimationFrame(() => resolve())),
    );
  }
  return wait;
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

function findButtonByAriaLabel(ariaLabel) {
  const button = [...document.querySelectorAll('button')].find(
    (item) => item.getAttribute('aria-label') === ariaLabel,
  );
  if (!button) {
    throw new Error(`button not found by aria-label: ${ariaLabel}`);
  }
  return button;
}

function setField(selector, value) {
  const field = document.querySelector(selector);
  field.value = value;
  field.dispatchEvent(new Event('input', { bubbles: true }));
}
