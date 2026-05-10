// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

const listLogsMock = vi.fn();
const readLogFileMock = vi.fn();
const subscribeLogEventsMock = vi.fn();
const streamConnections = [];

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  listLogs: (...args) => listLogsMock(...args),
  readLogFile: (...args) => readLogFileMock(...args),
  subscribeLogEvents: (...args) => subscribeLogEventsMock(...args),
}));

const { default: LogsView } = await import('../LogsView.svelte');

describe('LogsView', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    streamConnections.length = 0;

    listLogsMock.mockReset();
    readLogFileMock.mockReset();
    subscribeLogEventsMock.mockReset();
    subscribeLogEventsMock.mockImplementation((file, handlers = {}) => {
      const connection = createStreamConnection(file, handlers);
      streamConnections.push(connection);
      return connection;
    });
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it('loads the newest file by default and subscribes to its live stream', async () => {
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11', '2026-05-10'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11',
      entries: [entry({ message: 'Ready' })],
      cursor: 'cursor-initial',
    });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => subscribeLogEventsMock.mock.calls.length === 1,
    );

    expect(readLogFileMock).toHaveBeenCalledWith('2026-05-11');
    expect(subscribeLogEventsMock).toHaveBeenCalledWith(
      '2026-05-11',
      expect.any(Object),
      { cursor: 'cursor-initial' },
    );
    expect(document.body.textContent).toContain('Ready');
    expect(selectByLabel('File').value).toBe('2026-05-11');
  });

  it('filters entries by level and search text without extra backend calls', async () => {
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11',
      entries: [
        entry({ level: 'info', message: 'Ready' }),
        entry({ level: 'error', message: 'Failed to boot' }),
      ],
    });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();
    await waitForCondition(() =>
      document.body.textContent.includes('Failed to boot'),
    );

    const initialReadCalls = readLogFileMock.mock.calls.length;

    selectByLabel('Level').value = 'error';
    selectByLabel('Level').dispatchEvent(
      new Event('change', { bubbles: true }),
    );
    flushSync();

    await waitForCondition(() => !document.body.textContent.includes('Ready'));
    expect(document.body.textContent).toContain('Failed to boot');

    inputByLabel('Search').value = 'boot';
    inputByLabel('Search').dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    expect(readLogFileMock.mock.calls.length).toBe(initialReadCalls);
    expect(document.body.textContent).toContain('Failed to boot');
  });

  it('switches files and resubscribes to the selected file only', async () => {
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11', '2026-05-10'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockImplementation(async (file) => ({
      file,
      entries: [entry({ message: `Loaded ${file}` })],
      cursor: `cursor-${file}`,
    }));

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();
    await waitForCondition(
      () => subscribeLogEventsMock.mock.calls.length === 1,
    );

    const firstConnection = streamConnections[0];
    selectByLabel('File').value = '2026-05-10';
    selectByLabel('File').dispatchEvent(new Event('change', { bubbles: true }));
    flushSync();

    await waitForCondition(
      () => subscribeLogEventsMock.mock.calls.length === 2,
    );

    expect(firstConnection.close).toHaveBeenCalledWith(1000, 'logs-view-close');
    expect(readLogFileMock).toHaveBeenCalledWith('2026-05-10');
    expect(subscribeLogEventsMock).toHaveBeenLastCalledWith(
      '2026-05-10',
      expect.any(Object),
      { cursor: 'cursor-2026-05-10' },
    );
    expect(document.body.textContent).toContain('Loaded 2026-05-10');
  });

  it('uses the read cursor when opening the live log stream', async () => {
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11',
      entries: [entry({ message: 'Ready' })],
      cursor: 'cursor-live-handoff',
    });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => subscribeLogEventsMock.mock.calls.length === 1,
    );

    expect(subscribeLogEventsMock).toHaveBeenCalledWith(
      '2026-05-11',
      expect.any(Object),
      { cursor: 'cursor-live-handoff' },
    );
  });

  it('applies live append events and renders continuation lines', async () => {
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11',
      entries: [entry({ message: 'Ready' })],
      cursor: 'cursor-reconnect',
    });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();
    await waitForCondition(() => streamConnections.length === 1);

    streamConnections[0].emitOpen();
    streamConnections[0].emitEvent({
      type: 'append',
      file: '2026-05-11',
      entries: [
        entry({
          level: 'error',
          message: 'Failed',
          continuation: 'Traceback line',
        }),
      ],
    });
    flushSync();

    await waitForCondition(() =>
      document.body.textContent.includes('Traceback line'),
    );

    expect(document.body.textContent).toContain('Failed');
    expect(document.body.textContent).toContain('Traceback line');
    expect(document.body.textContent).toContain('Live');
  });

  it('cleans up the active stream on destroy', async () => {
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11',
      entries: [entry({ message: 'Ready' })],
    });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();
    await waitForCondition(() => streamConnections.length === 1);

    const connection = streamConnections[0];

    await unmount(mountedComponent);
    mountedComponent = null;

    expect(connection.close).toHaveBeenCalledWith(1000, 'logs-view-close');
  });

  it('reconnects after the live stream closes unexpectedly', async () => {
    vi.useFakeTimers();

    listLogsMock.mockResolvedValue({
      files: ['2026-05-11'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11',
      entries: [entry({ message: 'Ready' })],
      cursor: 'cursor-reconnect',
    });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();
    await waitForCondition(() => streamConnections.length === 1, 40, true);

    streamConnections[0].emitClose();
    flushSync();

    expect(document.body.textContent).toContain('Reconnecting…');

    await vi.advanceTimersByTimeAsync(1000);
    flushSync();

    await waitForCondition(
      () => subscribeLogEventsMock.mock.calls.length === 2,
      40,
      true,
    );
    expect(
      readLogFileMock.mock.calls.filter((call) => call[0] === '2026-05-11'),
    ).toHaveLength(2);
    expect(subscribeLogEventsMock).toHaveBeenLastCalledWith(
      '2026-05-11',
      expect.any(Object),
      { cursor: 'cursor-reconnect' },
    );
  });
});

function createStreamConnection(file, handlers) {
  return {
    file,
    handlers,
    close: vi.fn(() => {
      handlers.onClose?.();
    }),
    emitOpen() {
      handlers.onOpen?.();
    },
    emitEvent(event) {
      handlers.onEvent?.(event);
    },
    emitError(error) {
      handlers.onError?.(error);
    },
    emitClose() {
      handlers.onClose?.();
    },
  };
}

function entry(overrides = {}) {
  return {
    timestamp: '2026-05-11 09:00:00',
    level: 'info',
    logger_name: 'vbot.server.app',
    message: 'Ready',
    continuation: '',
    ...overrides,
  };
}

function selectByLabel(label) {
  const element = document.body.querySelector(`[aria-label="${label}"]`);
  expect(element).toBeTruthy();
  return element;
}

function inputByLabel(label) {
  const element = document.body.querySelector(`[aria-label="${label}"]`);
  expect(element).toBeTruthy();
  return element;
}

async function waitForCondition(check, attempts = 20, withTimers = false) {
  for (let index = 0; index < attempts; index += 1) {
    await Promise.resolve();
    if (withTimers) {
      await vi.advanceTimersByTimeAsync(0);
    } else {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    flushSync();

    if (check()) {
      return;
    }
  }

  throw new Error('Timed out waiting for condition.');
}
