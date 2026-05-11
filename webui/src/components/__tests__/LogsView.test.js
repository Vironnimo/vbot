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
    expect(simpleTriggerLabel('logs-file')).toContain('2026-05-11');
  });

  it('filters and sorts entries locally through simple dropdown controls', async () => {
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11',
      entries: [
        entry({
          timestamp: '2026-05-11 09:00:00',
          level: 'info',
          message: 'Ready',
        }),
        entry({
          timestamp: '2026-05-11 09:01:00',
          level: 'warn',
          message: 'Config drift',
        }),
        entry({
          timestamp: '2026-05-11 09:02:00',
          level: 'error',
          message: 'Failed to boot',
        }),
      ],
    });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();
    await waitForCondition(() =>
      document.body.textContent.includes('Failed to boot'),
    );

    expect(logEntryMessages()).toEqual([
      'Failed to boot',
      'Config drift',
      'Ready',
    ]);

    const initialReadCalls = readLogFileMock.mock.calls.length;

    openSimpleDropdown('logs-level-filter');
    expect(simpleOptionLabels('logs-level-filter')).toEqual([
      'All levels',
      'ERROR',
      'INFO',
      'WARN',
    ]);
    selectSimpleOption('logs-level-filter', 'ERROR');

    await waitForCondition(() => !document.body.textContent.includes('Ready'));
    expect(document.body.textContent).toContain('Failed to boot');
    expect(document.body.textContent).not.toContain('Config drift');

    inputByLabel('Search').value = 'boot';
    inputByLabel('Search').dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    openSimpleDropdown('logs-level-filter');
    selectSimpleOption('logs-level-filter', 'All levels');
    inputByLabel('Search').value = '';
    inputByLabel('Search').dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    openSimpleDropdown('logs-sort-order');
    expect(simpleOptionLabels('logs-sort-order')).toEqual([
      'Newest first',
      'Oldest first',
    ]);
    selectSimpleOption('logs-sort-order', 'Oldest first');

    expect(readLogFileMock.mock.calls.length).toBe(initialReadCalls);
    expect(logEntryMessages()).toEqual([
      'Ready',
      'Config drift',
      'Failed to boot',
    ]);
    expect(simpleTriggerLabel('logs-sort-order')).toBe('Oldest first');
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
    openSimpleDropdown('logs-file');
    selectSimpleOption('logs-file', '2026-05-10');

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

  it('resets an invalid level selection when switching to a file with different levels', async () => {
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11', '2026-05-10'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockImplementation(async (file) => {
      if (file === '2026-05-11') {
        return {
          file,
          entries: [entry({ level: 'warn', message: 'Warn row' })],
          cursor: 'cursor-2026-05-11',
        };
      }

      return {
        file,
        entries: [entry({ level: 'info', message: 'Info row' })],
        cursor: 'cursor-2026-05-10',
      };
    });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();
    await waitForCondition(() =>
      document.body.textContent.includes('Warn row'),
    );

    openSimpleDropdown('logs-level-filter');
    selectSimpleOption('logs-level-filter', 'WARN');
    await waitForCondition(
      () => simpleTriggerLabel('logs-level-filter') === 'WARN',
    );

    openSimpleDropdown('logs-file');
    selectSimpleOption('logs-file', '2026-05-10');

    await waitForCondition(() =>
      document.body.textContent.includes('Info row'),
    );

    expect(simpleTriggerLabel('logs-level-filter')).toBe('All levels');

    openSimpleDropdown('logs-level-filter');
    expect(simpleOptionLabels('logs-level-filter')).toEqual([
      'All levels',
      'INFO',
    ]);
    expect(logEntryMessages()).toEqual(['Info row']);
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

  it('renders dense rows and applies live append events without extra reads', async () => {
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
    const initialReadCalls = readLogFileMock.mock.calls.length;

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

    const rows = Array.from(document.querySelectorAll('.logs-entry'));
    const errorRow = rows.find((row) =>
      row.classList.contains('logs-entry--error'),
    );

    expect(rows).toHaveLength(2);
    expect(rows[0].querySelectorAll('span')).toHaveLength(4);
    expect(errorRow).toBeTruthy();
    expect(errorRow?.textContent).toContain('Failed Traceback line');
    expect(errorRow?.getAttribute('title')).toBe('Failed\nTraceback line');
    expect(document.body.querySelector('select')).toBeNull();
    expect(document.body.textContent).toContain('Live');
    expect(readLogFileMock.mock.calls.length).toBe(initialReadCalls);
  });

  it('shows a fallback stream error message when the error event has no message', async () => {
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

    streamConnections[0].emitError(new Event('error'));
    flushSync();

    expect(document.body.textContent).toContain(
      'Live log updates failed. Connection closed unexpectedly.',
    );
    expect(document.body.textContent).not.toContain('undefined');
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

function inputByLabel(label) {
  const element = document.body.querySelector(`[aria-label="${label}"]`);
  expect(element).toBeTruthy();
  return element;
}

function logEntryMessages() {
  return Array.from(document.querySelectorAll('.logs-entry__message')).map(
    (element) => element.textContent.trim(),
  );
}

function openSimpleDropdown(id) {
  const trigger = getSimpleTrigger(id);
  trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

function selectSimpleOption(id, label) {
  const option = Array.from(
    getSimpleList(id)?.querySelectorAll('.dropdown-option') ?? [],
  ).find((item) => item.textContent.trim() === label);
  expect(option).toBeTruthy();
  option.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

function simpleOptionLabels(id) {
  return Array.from(
    getSimpleList(id)?.querySelectorAll('.dropdown-option') ?? [],
  ).map((option) => option.textContent.trim());
}

function simpleTriggerLabel(id) {
  return (
    getSimpleTrigger(id)
      .querySelector('.dropdown-primitive__trigger-label')
      ?.textContent?.trim() ?? ''
  );
}

function getSimpleRoot(id) {
  return getSimpleTrigger(id)?.closest('.dropdown-primitive');
}

function getSimpleTrigger(id) {
  const trigger = document.body.querySelector(`button#${id}`);
  expect(trigger).toBeTruthy();
  return trigger;
}

function getSimpleList(id) {
  return getSimpleRoot(id)?.querySelector('.dropdown-primitive__list');
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
