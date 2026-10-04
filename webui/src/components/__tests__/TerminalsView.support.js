const { mount, flushSync, unmount } = await import('svelte');
// @vitest-environment jsdom
import { afterEach, beforeEach, vi } from 'vitest';

import { init } from '../../lib/i18n.js';

const listTerminalsMock = vi.fn();

const startTerminalMock = vi.fn();

// What the Terminal sockets carried: input as (terminalId, data), resize
// requests as (terminalId, columns, rows) resolving to the server's answer.
const terminalInputMock = vi.fn();

const terminalResizeMock = vi.fn();

const killTerminalMock = vi.fn();

const forgetTerminalMock = vi.fn();

const createTerminalGroupMock = vi.fn();

const renameTerminalGroupMock = vi.fn();

const deleteTerminalGroupMock = vi.fn();

const setTerminalGroupOrderMock = vi.fn();

const subscribeTerminalEventsMock = vi.fn();

const createAudioRecorderMock = vi.fn();

const transcribeSpeechMock = vi.fn();

const prepareSpeechTranscriptionMock = vi.fn();

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
  transcribeSpeech: (...args) => transcribeSpeechMock(...args),
  prepareSpeechTranscription: (...args) =>
    prepareSpeechTranscriptionMock(...args),
  listTerminals: (...args) => listTerminalsMock(...args),
  startTerminal: (...args) => startTerminalMock(...args),
  killTerminal: (...args) => killTerminalMock(...args),
  forgetTerminal: (...args) => forgetTerminalMock(...args),
  createTerminalGroup: (...args) => createTerminalGroupMock(...args),
  renameTerminalGroup: (...args) => renameTerminalGroupMock(...args),
  deleteTerminalGroup: (...args) => deleteTerminalGroupMock(...args),
  setTerminalGroupOrder: (...args) => setTerminalGroupOrderMock(...args),
  subscribeTerminalEvents: (...args) => subscribeTerminalEventsMock(...args),
}));

vi.mock('$lib/audioRecorder.js', () => ({
  createAudioRecorder: (...args) => createAudioRecorderMock(...args),
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
      this.paste = vi.fn((text) => this.onDataCallback(text));
      this.scrollToBottom = vi.fn(() => {
        this.buffer.active.viewportY = this.buffer.active.baseY;
      });
      this.buffer = { active: { viewportY: 0, baseY: 0 } };
      this.parser = {
        registerCsiHandler: vi.fn(() => ({ dispose: vi.fn() })),
        registerDcsHandler: vi.fn(() => ({ dispose: vi.fn() })),
        registerOscHandler: vi.fn(() => ({ dispose: vi.fn() })),
      };
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
      // Cells measure 8x16 px at the 12 px base font and scale with it.
      this.proposeDimensions = vi.fn(() => {
        if (!this.terminal?.element?.parentElement) {
          return undefined;
        }
        const host = this.terminal.element.parentElement;
        const scale = this.terminal.options.fontSize / 12;
        return {
          cols: Math.max(1, Math.floor(host.clientWidth / (8 * scale))),
          rows: Math.max(1, Math.floor(host.clientHeight / (16 * scale))),
        };
      });
      this.fit = vi.fn(() => {
        const dimensions = this.proposeDimensions();
        if (dimensions) {
          this.terminal.resize(dimensions.cols, dimensions.rows);
        }
      });
      fitAddons.push(this);
    }
    activate(terminal) {
      this.terminal = terminal;
    }
    dispose() {}
  },
}));

// A real browser fires a ResizeObserver when the tile host is laid out;
// fire() reports a later layout change of the host.
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

function userGroup(groupId, name, terminalCount = 0) {
  return manualGroup({
    group_id: groupId,
    name,
    kind: 'user',
    terminal_count: terminalCount,
    live_count: terminalCount,
  });
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

// Deliver each open stream its authoritative ready snapshot.
function readyAll(terminals, ansi = '') {
  streams.forEach((stream, index) =>
    stream.handlers.onEvent({
      type: 'terminal_ready',
      sequence: 1,
      ansi,
      terminal: terminals[index],
    }),
  );
  flushSync();
}

function submitStartForm() {
  document
    .querySelector('#terminal-start-form')
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
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

const fixtureState = {
  get mockHostWidth() {
    return mockHostWidth;
  },
  set mockHostWidth(value) {
    mockHostWidth = value;
  },
  get mockHostHeight() {
    return mockHostHeight;
  },
  set mockHostHeight(value) {
    mockHostHeight = value;
  },
};

function setupTerminalsViewSuite() {
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
    createAudioRecorderMock.mockReset();
    transcribeSpeechMock.mockReset();
    prepareSpeechTranscriptionMock
      .mockReset()
      .mockResolvedValue({ state: 'loading' });
    listTerminalsMock.mockReset();
    startTerminalMock.mockReset().mockResolvedValue({});
    terminalInputMock.mockReset();
    terminalResizeMock
      .mockReset()
      .mockImplementation(async (_id, columns, rows) => ({
        terminal: { columns, rows },
      }));
    killTerminalMock.mockReset().mockResolvedValue({});
    forgetTerminalMock.mockReset().mockResolvedValue({});
    createTerminalGroupMock.mockReset().mockResolvedValue({});
    renameTerminalGroupMock.mockReset().mockResolvedValue({});
    deleteTerminalGroupMock.mockReset().mockResolvedValue({});
    setTerminalGroupOrderMock.mockReset().mockResolvedValue({});
    subscribeTerminalEventsMock
      .mockReset()
      .mockImplementation((terminalId, handlers) => {
        // The socket answers requests as the server does.
        const send = (message) => {
          if (message.type === 'input') {
            terminalInputMock(terminalId, message.data);
          } else if (message.type === 'resize') {
            const { request, columns, rows } = message;
            Promise.resolve(terminalResizeMock(terminalId, columns, rows)).then(
              (result) =>
                handlers.onEvent({
                  type: 'resize_done',
                  request,
                  terminal: result.terminal,
                }),
              (error) =>
                handlers.onEvent({
                  type: 'resize_failed',
                  request,
                  message: error.message,
                }),
            );
          }
          return true;
        };
        const connection = { close: vi.fn(), send };
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
  return {
    // Mount the view into the document; the suite unmounts it after the test.
    mount(props = {}) {
      mountedComponent = mount(TerminalsView, {
        target: document.body,
        props,
      });
      flushSync();
      return mountedComponent;
    },
    // Serve `terminals` (in `groups`) and wait until every tile has its xterm.
    async mountWith(terminals, groups) {
      listTerminalsMock.mockResolvedValue(
        terminalListResponse(terminals, groups),
      );
      const view = this.mount();
      await waitFor(
        () =>
          streams.length === terminals.length &&
          terminalInstances.length === terminals.length,
      );
      return view;
    },
    async unmount() {
      await unmount(mountedComponent);
      mountedComponent = null;
    },
  };
}

export {
  listTerminalsMock,
  startTerminalMock,
  terminalInputMock,
  terminalResizeMock,
  killTerminalMock,
  forgetTerminalMock,
  setTerminalGroupOrderMock,
  subscribeTerminalEventsMock,
  createAudioRecorderMock,
  transcribeSpeechMock,
  prepareSpeechTranscriptionMock,
  streams,
  terminalInstances,
  fitAddons,
  resizeObservers,
  terminal,
  launchHistory,
  manualGroup,
  userGroup,
  terminalListResponse,
  readyAll,
  submitStartForm,
  flushAnimationFrames,
  waitFor,
  findButton,
  findButtonByAriaLabel,
  setField,
  fixtureState,
  setupTerminalsViewSuite,
};

export { flushSync };
