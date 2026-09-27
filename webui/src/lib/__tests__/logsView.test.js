import { describe, expect, it } from 'vitest';

import {
  LOGS_SORT_ORDER_NEWEST,
  LOGS_SORT_ORDER_OLDEST,
  LOGS_STREAM_STATUS_IDLE,
  applyLogCatalog,
  changedFilterSelectionCount,
  createLogsViewState,
  deriveLevelOptions,
  deriveSortOptions,
  levelOptionValue,
  mergeLogStreamEvent,
  normalizeLevelFilter,
  replaceLogEntries,
  selectLogFile,
  setLevelFilter,
  setSortOrder,
  setSearchText,
  visibleLogEntries,
} from '../logsView.js';

describe('logsView state', () => {
  it('creates default logs view state', () => {
    expect(createLogsViewState()).toEqual({
      files: [],
      defaultFile: '',
      selectedFile: '',
      entries: [],
      levelFilter: 'all',
      sortOrder: 'newest',
      searchText: '',
      loadingCatalog: false,
      loadingEntries: false,
      catalogError: '',
      readError: '',
      streamError: '',
      streamStatus: LOGS_STREAM_STATUS_IDLE,
    });
  });

  it('applies the log catalog and keeps a valid current selection', () => {
    const state = createLogsViewState();

    expect(
      applyLogCatalog(state, {
        files: ['2026-05-11', '2026-05-10'],
        default_file: '2026-05-11',
      }),
    ).toBe('2026-05-11');

    selectLogFile(state, '2026-05-10');
    expect(
      applyLogCatalog(state, {
        files: ['2026-05-11', '2026-05-10', '2026-05-09'],
        default_file: '2026-05-11',
      }),
    ).toBe('2026-05-10');

    // Without a default file the newest listed file is the default.
    expect(applyLogCatalog(state, { files: ['2026-05-12'] })).toBe(
      '2026-05-12',
    );
    expect(state.defaultFile).toBe('2026-05-12');
  });

  it('counts file, level, and order choices that differ from the default view', () => {
    const state = createLogsViewState();
    applyLogCatalog(state, {
      files: ['2026-05-11', '2026-05-10'],
      default_file: '2026-05-11',
    });
    replaceLogEntries(state, {
      file: '2026-05-11',
      entries: [entry({ level: 'info' }), entry({ level: 'error' })],
    });
    expect(changedFilterSelectionCount(state)).toBe(0);

    setSearchText(state, 'boot');
    expect(changedFilterSelectionCount(state)).toBe(0);

    setLevelFilter(state, 'error');
    setSortOrder(state, LOGS_SORT_ORDER_OLDEST);
    selectLogFile(state, '2026-05-10');
    expect(changedFilterSelectionCount(state)).toBe(3);

    // A newer daily file becomes the default; the kept older selection still
    // counts as changed.
    applyLogCatalog(state, {
      files: ['2026-05-12', '2026-05-11', '2026-05-10'],
      default_file: '2026-05-12',
    });
    setLevelFilter(state, 'all');
    setSortOrder(state, LOGS_SORT_ORDER_NEWEST);
    expect(changedFilterSelectionCount(state)).toBe(1);
  });

  it('replaces entries and merges append and reset stream events for the selected file only', () => {
    const state = createLogsViewState();
    replaceLogEntries(state, {
      file: '2026-05-11',
      entries: [entry({ message: 'Ready' })],
    });

    mergeLogStreamEvent(state, {
      type: 'append',
      file: '2026-05-11',
      entries: [entry({ message: 'Failed', level: 'error' })],
    });
    mergeLogStreamEvent(state, {
      type: 'append',
      file: '2026-05-10',
      entries: [entry({ message: 'Other file' })],
    });
    expect(state.entries.map((item) => item.message)).toEqual([
      'Ready',
      'Failed',
    ]);

    mergeLogStreamEvent(state, {
      type: 'reset',
      file: '2026-05-11',
      entries: [entry({ message: 'Reset', level: 'warn' })],
    });
    expect(state.entries.map((item) => item.message)).toEqual(['Reset']);
  });

  it('keeps the level filter only while the loaded entries include that level', () => {
    const state = createLogsViewState();
    state.entries = [entry({ level: 'info' }), entry({ level: 'warn' })];
    setLevelFilter(state, 'warn');
    expect(normalizeLevelFilter(state)).toBe('warn');

    replaceLogEntries(state, {
      file: '2026-05-11',
      entries: [entry({ level: 'info' })],
    });
    expect(state.levelFilter).toBe('all');

    setLevelFilter(state, 'error');
    mergeLogStreamEvent(state, {
      type: 'reset',
      file: '2026-05-11',
      entries: [entry({ level: 'warn' })],
    });
    expect(state.levelFilter).toBe('all');
  });

  it('offers all plus the distinct parsed levels in sorted order', () => {
    expect(
      deriveLevelOptions([
        entry({ level: 'warn' }),
        entry({ level: 'warn', message: 'Second warning' }),
        entry({ level: 'info' }),
        entry({ level: 'error' }),
        entry({ level: '' }),
        entry({ level: null }),
      ]),
    ).toEqual([levelOptionValue(), 'error', 'info', 'warn']);
  });
});

describe('logsView visible entries', () => {
  it('filters by level and by search text across every entry field', () => {
    const state = createLogsViewState();
    state.entries = [
      entry({ level: 'info', message: 'Ready' }),
      entry({
        timestamp: '2026-05-11 09:00:01',
        level: 'error',
        message: 'Failed',
        continuation: 'Traceback line',
      }),
      entry({
        level: 'warn',
        logger_name: 'vbot.core.worker',
        message: 'Retry soon',
      }),
    ];
    const visibleMessages = () =>
      visibleLogEntries(state).map((item) => item.message);

    setLevelFilter(state, 'error');
    expect(visibleMessages()).toEqual(['Failed']);

    setLevelFilter(state, levelOptionValue());
    setSearchText(state, '  WORKER retry ');
    expect(visibleMessages()).toEqual(['Retry soon']);

    for (const needle of ['09:00:01', 'error', 'failed', 'traceback']) {
      setSearchText(state, needle);
      expect(visibleMessages()).toEqual(['Failed']);
    }
    setSearchText(state, 'server.app');
    expect(visibleMessages()).toEqual(['Failed', 'Ready']);
  });

  it('orders entries newest first unless oldest first is chosen', () => {
    const state = createLogsViewState();
    state.entries = ['First', 'Second', 'Third'].map((message) =>
      entry({ message }),
    );
    const visibleMessages = () =>
      visibleLogEntries(state).map((item) => item.message);

    expect(deriveSortOptions()).toEqual([
      LOGS_SORT_ORDER_NEWEST,
      LOGS_SORT_ORDER_OLDEST,
    ]);
    expect(visibleMessages()).toEqual(['Third', 'Second', 'First']);

    setSortOrder(state, LOGS_SORT_ORDER_OLDEST);
    expect(visibleMessages()).toEqual(['First', 'Second', 'Third']);

    expect(setSortOrder(state, 'sideways')).toBe(LOGS_SORT_ORDER_NEWEST);
    expect(visibleMessages()).toEqual(['Third', 'Second', 'First']);
  });
});

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
