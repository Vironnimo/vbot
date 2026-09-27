// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';

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
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('loads the newest file by default, follows it live, and closes the stream on destroy', async () => {
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
    expect(buttonByText(t('common.refresh', 'Refresh'))).toBeNull();
    expect(document.querySelector('.logs-view.view-frame')).toBeTruthy();
    expect(document.querySelector('.logs-view .view-header')).toBeTruthy();
    const toolbar = document.querySelector('.logs-view .view-toolbar--stack');
    expect(toolbar).toBeTruthy();
    expect(toolbar.querySelector('.logs-view__filters')).toBeTruthy();
    expect(toolbar.querySelector('.logs-view__summary')).toBeTruthy();
    // Wide layouts show every filter inline, without a disclosure.
    expect(toolbar.querySelector('.logs-view__filters-toggle')).toBeNull();
    expect(document.getElementById('logs-filters').hidden).toBe(false);

    await unmount(mountedComponent);
    mountedComponent = null;
    expect(streamConnections[0].close).toHaveBeenCalledWith(
      1000,
      'logs-view-close',
    );
  });

  it('keeps search visible and folds the other filters behind a disclosure on phone width', async () => {
    const mediaQuery = stubMatchMedia(true);
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11', '2026-05-10'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11',
      entries: [
        entry({ timestamp: '2026-05-11 09:00:00', message: 'Ready' }),
        entry({
          timestamp: '2026-05-11 09:02:00',
          level: 'error',
          message: 'Failed to boot',
        }),
      ],
      cursor: 'cursor-compact',
    });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();
    await waitForCondition(() =>
      document.body.textContent.includes('Failed to boot'),
    );

    expect(window.matchMedia).toHaveBeenCalledWith('(max-width: 640px)');
    const toggle = document.querySelector('.logs-view__filters-toggle');
    const panel = document.getElementById('logs-filters');
    expect(toggle.textContent.trim()).toBe(t('logs.filters', 'Filters'));
    expect(toggle.getAttribute('aria-controls')).toBe('logs-filters');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.getAttribute('aria-label')).toBeNull();
    expect(panel.hidden).toBe(true);

    // Search stays outside the disclosure and is not counted as a change.
    const search = inputByLabel('Search');
    expect(panel.contains(search)).toBe(false);
    search.value = 'boot';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(logEntryMessages()).toEqual(['Failed to boot']);
    expect(toggle.querySelector('.logs-view__filters-count')).toBeNull();
    search.value = '';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    toggle.click();
    flushSync();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(panel.hidden).toBe(false);

    openSimpleDropdown('logs-level-filter');
    selectSimpleOption('logs-level-filter', t('logs.level.error', 'ERROR'));
    openSimpleDropdown('logs-sort-order');
    selectSimpleOption(
      'logs-sort-order',
      t('logs.sort.oldest', 'Oldest first'),
    );
    expect(logEntryMessages()).toEqual(['Failed to boot']);
    expect(toggle.getAttribute('aria-label')).toBe('Filters, 2 changed');
    expect(
      toggle.querySelector('.logs-view__filters-count').textContent.trim(),
    ).toBe('2');

    toggle.click();
    flushSync();
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(panel.hidden).toBe(true);
    expect(logEntryMessages()).toEqual(['Failed to boot']);

    // Leaving phone width restores the inline toolbar with the same choices.
    mediaQuery.change(false);
    flushSync();
    expect(document.querySelector('.logs-view__filters-toggle')).toBeNull();
    const inlinePanel = document.getElementById('logs-filters');
    expect(inlinePanel.hidden).toBe(false);
    expect(inlinePanel.contains(inputByLabel('Search'))).toBe(true);
    expect(simpleTriggerLabel('logs-level-filter')).toBe(
      t('logs.level.error', 'ERROR'),
    );
    expect(simpleTriggerLabel('logs-sort-order')).toBe(
      t('logs.sort.oldest', 'Oldest first'),
    );

    await unmount(mountedComponent);
    mountedComponent = null;
    expect(mediaQuery.removeEventListener).toHaveBeenCalledWith(
      'change',
      expect.any(Function),
    );
  });

  it('offers Retry only when the log catalog fails to load', async () => {
    listLogsMock
      .mockRejectedValueOnce(new Error('catalog unavailable'))
      .mockResolvedValueOnce({
        files: ['2026-05-11'],
        default_file: '2026-05-11',
      });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11',
      entries: [entry({ message: 'Recovered' })],
      cursor: 'cursor-recovered',
    });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();

    await waitForCondition(
      () => buttonByText(t('common.retry', 'Retry')) !== null,
    );
    expect(buttonByText(t('common.refresh', 'Refresh'))).toBeNull();

    buttonByText(t('common.retry', 'Retry')).click();
    flushSync();

    await waitForCondition(() =>
      document.body.textContent.includes('Recovered'),
    );
    expect(buttonByText(t('common.retry', 'Retry'))).toBeNull();
    expect(buttonByText(t('common.refresh', 'Refresh'))).toBeNull();
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
      t('logs.level.all', 'All levels'),
      t('logs.level.error', 'ERROR'),
      t('logs.level.info', 'INFO'),
      t('logs.level.warn', 'WARN'),
    ]);
    selectSimpleOption('logs-level-filter', t('logs.level.error', 'ERROR'));

    await waitForCondition(() => !document.body.textContent.includes('Ready'));
    expect(document.body.textContent).toContain('Failed to boot');
    expect(document.body.textContent).not.toContain('Config drift');

    inputByLabel('Search').value = 'boot';
    inputByLabel('Search').dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    openSimpleDropdown('logs-level-filter');
    selectSimpleOption('logs-level-filter', t('logs.level.all', 'All levels'));
    inputByLabel('Search').value = '';
    inputByLabel('Search').dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();

    openSimpleDropdown('logs-sort-order');
    expect(simpleOptionLabels('logs-sort-order')).toEqual([
      t('logs.sort.newest', 'Newest first'),
      t('logs.sort.oldest', 'Oldest first'),
    ]);
    selectSimpleOption(
      'logs-sort-order',
      t('logs.sort.oldest', 'Oldest first'),
    );

    expect(readLogFileMock.mock.calls.length).toBe(initialReadCalls);
    expect(logEntryMessages()).toEqual([
      'Ready',
      'Config drift',
      'Failed to boot',
    ]);
    expect(simpleTriggerLabel('logs-sort-order')).toBe(
      t('logs.sort.oldest', 'Oldest first'),
    );
  });

  it('switches files, resubscribes to the selected file only, and resets a level it lacks', async () => {
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11', '2026-05-10'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockImplementation(async (file) => ({
      file,
      entries: [
        entry({
          level: file === '2026-05-11' ? 'warn' : 'info',
          message: `Loaded ${file}`,
        }),
      ],
      cursor: `cursor-${file}`,
    }));

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();
    await waitForCondition(
      () => subscribeLogEventsMock.mock.calls.length === 1,
    );

    const warn = t('logs.level.warn', 'WARN');
    openSimpleDropdown('logs-level-filter');
    selectSimpleOption('logs-level-filter', warn);
    expect(simpleTriggerLabel('logs-level-filter')).toBe(warn);

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
    expect(logEntryMessages()).toEqual(['Loaded 2026-05-10']);

    const allLevels = t('logs.level.all', 'All levels');
    expect(simpleTriggerLabel('logs-level-filter')).toBe(allLevels);
    openSimpleDropdown('logs-level-filter');
    expect(simpleOptionLabels('logs-level-filter')).toEqual([
      allLevels,
      t('logs.level.info', 'INFO'),
    ]);
  });

  it('renders dense rows with level tones and applies live append events without extra reads', async () => {
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11',
      entries: [
        entry({ message: 'Ready' }),
        entry({ level: 'critical', message: 'Halted' }),
      ],
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

    await waitForCondition(() => logEntryMessages().includes('Failed'));

    const rows = Array.from(document.querySelectorAll('.logs-entry'));
    const errorRow = rows[0];

    // Critical entries share the error tone.
    expect(
      rows.map((row) =>
        ['error', 'warn', 'info', 'neutral'].find((tone) =>
          row.classList.contains(`logs-entry--${tone}`),
        ),
      ),
    ).toEqual(['error', 'error', 'info']);
    expect(
      [...rows[1].querySelectorAll('span[class^="logs-entry__"]')].map(
        (cell) => cell.className.split(' ')[0],
      ),
    ).toEqual([
      'logs-entry__timestamp',
      'logs-entry__level',
      'logs-entry__logger',
      'logs-entry__message',
      'logs-entry__summary',
    ]);
    // A multi-line entry shows its header message and a line count; the
    // continuation stays folded until the row is expanded.
    expect(
      errorRow.querySelector('.logs-entry__summary').textContent.trim(),
    ).toBe('Failed');
    expect(errorRow.querySelector('.logs-entry__more').textContent).toBe(
      t('logs.moreLinesOne', '+1 line'),
    );
    expect(document.body.textContent).not.toContain('Traceback line');
    expect(document.body.querySelector('select')).toBeNull();
    expect(document.body.textContent).toContain(
      t('logs.stream.connected', 'Live'),
    );
    expect(readLogFileMock.mock.calls.length).toBe(initialReadCalls);
  });

  it('expands an entry in place and keeps it expanded across live appends', async () => {
    const continuation =
      'Traceback (most recent call last):\n  File "app.py", line 3, in run';
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11',
      entries: [entry({ level: 'error', message: 'Run failed', continuation })],
      cursor: 'cursor-expand',
    });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();
    await waitForCondition(() => streamConnections.length === 1);

    const row = document.querySelector('.logs-entry');
    const toggle = row.querySelector('.logs-entry__toggle');
    expect(row.querySelector('.logs-entry__more').textContent).toBe(
      t('logs.moreLines', '+{count} lines', { count: 2 }),
    );
    expect(toggle.getAttribute('aria-label')).toBe('Entry details');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(row.querySelector('.logs-entry__detail')).toBeNull();

    toggle.click();
    flushSync();
    const detail = row.querySelector('.logs-entry__detail');
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(toggle.getAttribute('aria-controls')).toBe(detail.id);
    expect(detail.tagName).toBe('PRE');
    expect(detail.textContent).toBe(`Run failed\n${continuation}`);

    // Clicks inside the expanded text are for reading and selecting.
    detail.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    expect(row.querySelector('.logs-entry__detail')).toBeTruthy();

    // Newest first: a live append lands above without collapsing the row.
    streamConnections[0].emitEvent({
      type: 'append',
      file: '2026-05-11',
      entries: [entry({ message: 'Recovered' })],
    });
    flushSync();
    expect(logEntryMessages()).toEqual(['Recovered', 'Run failed']);
    const rows = document.querySelectorAll('.logs-entry');
    expect(rows[1]).toBe(row);
    expect(row.querySelector('.logs-entry__detail')).toBeTruthy();

    // A drag that selected text in the row is not a toggle.
    const timestamp = row.querySelector('.logs-entry__timestamp');
    vi.spyOn(window, 'getSelection').mockReturnValue({
      toString: () => '09:00',
    });
    timestamp.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    expect(row.querySelector('.logs-entry__detail')).toBeTruthy();

    // A plain click anywhere on the row toggles it.
    window.getSelection.mockReturnValue({ toString: () => '' });
    timestamp.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    expect(row.querySelector('.logs-entry__detail')).toBeNull();
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.hasAttribute('aria-controls')).toBe(false);

    rows[0]
      .querySelector('.logs-entry__message')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    expect(rows[0].querySelector('.logs-entry__detail').textContent).toBe(
      'Recovered',
    );
  });

  it('updates the file catalog live without changing a valid selection', async () => {
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11', '2026-05-10'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11',
      entries: [entry({ message: 'Ready' })],
      cursor: 'cursor-catalog',
    });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();
    await waitForCondition(() => streamConnections.length === 1);
    const initialReadCalls = readLogFileMock.mock.calls.length;

    streamConnections[0].emitEvent({
      type: 'catalog',
      file: '2026-05-11',
      files: ['2026-05-12', '2026-05-11', '2026-05-10'],
      default_file: '2026-05-12',
    });
    flushSync();

    expect(simpleTriggerLabel('logs-file')).toContain('2026-05-11');
    expect(readLogFileMock).toHaveBeenCalledTimes(initialReadCalls);

    openSimpleDropdown('logs-file');
    expect(simpleOptionLabels('logs-file')).toContain('2026-05-12');
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

    expect(
      document.querySelector('.logs-view .banner--warn[aria-live="polite"]'),
    ).toBeTruthy();
    expect(document.body.textContent).not.toContain('undefined');
  });

  it('copies the verbatim log line to the clipboard from the per-row copy button', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });

    const rawLine =
      '2026-05-11 09:00:00 [INFO] vbot.core - Ready - with - dashes';
    listLogsMock.mockResolvedValue({
      files: ['2026-05-11'],
      default_file: '2026-05-11',
    });
    readLogFileMock.mockResolvedValue({
      file: '2026-05-11',
      entries: [
        entry({
          logger_name: 'vbot.core',
          message: 'Ready - with - dashes',
          raw: rawLine,
        }),
      ],
      cursor: 'cursor-copy',
    });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();
    await waitForCondition(() =>
      document.body.textContent.includes('Ready - with - dashes'),
    );

    const copyButton = document.querySelector('.logs-entry .logs-entry__copy');
    expect(copyButton).toBeTruthy();
    expect(copyButton.getAttribute('aria-label')).toBe('Copy log line');

    copyButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await waitForCondition(() => writeText.mock.calls.length === 1);

    // The full source line is copied verbatim, not the reconstructed/truncated
    // preview shown in the row.
    expect(writeText).toHaveBeenCalledWith(rawLine);
    expect(document.querySelector('.logs-entry__detail')).toBeNull();
    await waitForCondition(
      () => copyButton.getAttribute('aria-label') === 'Copied',
    );
  });

  it('reconnects with backoff after the live stream closes, also across a failed attempt', async () => {
    vi.useFakeTimers();
    // Pin reconnect jitter to its midpoint so the attempts fire at exactly the
    // base delays of 1000ms and 2000ms this test advances by.
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
    const catalog = { files: ['2026-05-11'], default_file: '2026-05-11' };
    listLogsMock
      .mockResolvedValueOnce(catalog)
      .mockRejectedValueOnce(new Error('server unavailable'))
      .mockResolvedValue(catalog);
    readLogFileMock
      .mockResolvedValueOnce({
        file: '2026-05-11',
        entries: [entry({ message: 'Before restart' })],
        cursor: 'cursor-before-restart',
      })
      .mockResolvedValue({
        file: '2026-05-11',
        entries: [entry({ message: 'After restart' })],
        cursor: 'cursor-after-restart',
      });

    mountedComponent = mount(LogsView, { target: document.body });
    flushSync();
    await waitForCondition(() => streamConnections.length === 1, 40, true);

    const reconnecting = t('logs.stream.reconnecting', 'Reconnecting…');
    streamConnections[0].emitClose();
    flushSync();
    expect(document.body.textContent).toContain(reconnecting);

    await vi.advanceTimersByTimeAsync(1000);
    flushSync();
    expect(listLogsMock).toHaveBeenCalledTimes(2);
    expect(document.body.textContent).toContain(reconnecting);

    await vi.advanceTimersByTimeAsync(2000);
    await waitForCondition(() => streamConnections.length === 2, 40, true);
    expect(listLogsMock).toHaveBeenCalledTimes(3);
    expect(readLogFileMock).toHaveBeenCalledTimes(2);
    expect(document.body.textContent).toContain('After restart');
    expect(subscribeLogEventsMock).toHaveBeenLastCalledWith(
      '2026-05-11',
      expect.any(Object),
      { cursor: 'cursor-after-restart' },
    );
  });
});

function stubMatchMedia(initialMatches) {
  const listeners = new Set();
  const query = {
    matches: initialMatches,
    media: '(max-width: 640px)',
    addEventListener: vi.fn((type, listener) => listeners.add(listener)),
    removeEventListener: vi.fn((type, listener) => listeners.delete(listener)),
    change(matches) {
      query.matches = matches;
      for (const listener of listeners) {
        listener({ matches });
      }
    },
  };
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => query),
  );
  return query;
}

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
    raw: '2026-05-11 09:00:00 [INFO] vbot.server.app - Ready',
    ...overrides,
  };
}

function inputByLabel(label) {
  const element = document.body.querySelector(`[aria-label="${label}"]`);
  expect(element).toBeTruthy();
  return element;
}

function buttonByText(text) {
  return (
    [...document.body.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === text,
    ) ?? null
  );
}

function logEntryMessages() {
  return Array.from(document.querySelectorAll('.logs-entry__summary')).map(
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

function getSimpleTrigger(id) {
  const trigger = document.body.querySelector(`button#${id}`);
  expect(trigger).toBeTruthy();
  return trigger;
}

function getSimpleList() {
  // The list is portaled to <body>; only the open dropdown renders one.
  return document.body.querySelector('.dropdown-primitive__list');
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
