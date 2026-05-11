import { describe, expect, it } from 'vitest';

import {
  LOGS_SORT_ORDER_NEWEST,
  LOGS_SORT_ORDER_OLDEST,
  LOGS_STREAM_STATUS_IDLE,
  applyLogCatalog,
  createLogsViewState,
  deriveLevelOptions,
  deriveSortOptions,
  filterLogEntries,
  levelOptionValue,
  mergeLogStreamEvent,
  replaceLogEntries,
  selectLogFile,
  setLevelFilter,
  setSortOrder,
  setSearchText,
  sortLogEntries,
  visibleLogEntries,
} from '../logsView.js';

describe('logsView helpers', () => {
  it('creates default logs view state', () => {
    expect(createLogsViewState()).toEqual({
      files: [],
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

  it('applies log catalog and keeps a valid current selection', () => {
    const state = createLogsViewState();

    expect(
      applyLogCatalog(state, {
        files: ['2026-05-11', '2026-05-10'],
        default_file: '2026-05-11',
      }),
    ).toBe('2026-05-11');

    state.selectedFile = '2026-05-10';

    expect(
      applyLogCatalog(state, {
        files: ['2026-05-11', '2026-05-10', '2026-05-09'],
        default_file: '2026-05-11',
      }),
    ).toBe('2026-05-10');
  });

  it('replaces entries and merges append and reset stream events', () => {
    const state = createLogsViewState();
    replaceLogEntries(state, {
      file: '2026-05-11',
      entries: [entry({ message: 'Ready', level: 'info' })],
    });

    mergeLogStreamEvent(state, {
      type: 'append',
      file: '2026-05-11',
      entries: [entry({ message: 'Failed', level: 'error' })],
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

  it('ignores stream events for a different file', () => {
    const state = createLogsViewState();
    replaceLogEntries(state, {
      file: '2026-05-11',
      entries: [entry({ message: 'Ready' })],
    });

    mergeLogStreamEvent(state, {
      type: 'append',
      file: '2026-05-10',
      entries: [entry({ message: 'Ignored' })],
    });

    expect(state.entries.map((item) => item.message)).toEqual(['Ready']);
  });

  it('derives level options and filters by level and text search', () => {
    const entries = [
      entry({
        level: 'info',
        logger_name: 'vbot.server.app',
        message: 'Ready',
      }),
      entry({
        level: 'error',
        logger_name: 'vbot.server.app',
        message: 'Failed',
      }),
      entry({
        level: 'warn',
        logger_name: 'vbot.core.worker',
        message: 'Retry soon',
      }),
    ];

    expect(deriveLevelOptions(entries)).toEqual([
      'all',
      'error',
      'info',
      'warn',
    ]);
    expect(
      filterLogEntries(entries, { levelFilter: 'error', searchText: '' }).map(
        (item) => item.message,
      ),
    ).toEqual(['Failed']);
    expect(
      filterLogEntries(entries, {
        levelFilter: levelOptionValue(),
        searchText: 'worker retry',
      }).map((item) => item.message),
    ).toEqual(['Retry soon']);
    expect(deriveSortOptions()).toEqual(['newest', 'oldest']);
  });

  it('keeps all plus distinct parsed levels only', () => {
    const entries = [
      entry({ level: 'warn' }),
      entry({ level: 'warn', message: 'Second warning' }),
      entry({ level: 'error' }),
      entry({ level: '' }),
      entry({ level: null }),
    ];

    expect(deriveLevelOptions(entries)).toEqual(['all', 'error', 'warn']);
  });

  it('searches timestamp, level, logger, message, and continuation text', () => {
    const entries = [
      entry({
        timestamp: '2026-05-11 09:00:00',
        level: 'error',
        logger_name: 'vbot.server.app',
        message: 'Failed',
        continuation: 'Traceback line',
      }),
    ];

    expect(filterLogEntries(entries, { searchText: '09:00:00' })).toHaveLength(
      1,
    );
    expect(filterLogEntries(entries, { searchText: 'error' })).toHaveLength(1);
    expect(
      filterLogEntries(entries, { searchText: 'server.app' }),
    ).toHaveLength(1);
    expect(filterLogEntries(entries, { searchText: 'failed' })).toHaveLength(1);
    expect(filterLogEntries(entries, { searchText: 'traceback' })).toHaveLength(
      1,
    );
  });

  it('computes visible entries from state filters', () => {
    const state = createLogsViewState();
    state.entries = [
      entry({
        timestamp: '2026-05-11 09:00:00',
        level: 'info',
        message: 'Ready',
      }),
      entry({
        timestamp: '2026-05-11 09:00:01',
        level: 'error',
        message: 'Failed',
      }),
    ];

    selectLogFile(state, '2026-05-11');
    setLevelFilter(state, 'error');
    setSearchText(state, 'failed');

    expect(visibleLogEntries(state).map((item) => item.message)).toEqual([
      'Failed',
    ]);
  });

  it('defaults to newest-first and supports oldest-first ordering', () => {
    const entries = [
      entry({ timestamp: '2026-05-11 09:00:00', message: 'First' }),
      entry({ timestamp: '2026-05-11 09:00:01', message: 'Second' }),
      entry({ timestamp: '2026-05-11 09:00:02', message: 'Third' }),
    ];

    expect(
      sortLogEntries(entries, LOGS_SORT_ORDER_NEWEST).map(
        (item) => item.message,
      ),
    ).toEqual(['Third', 'Second', 'First']);
    expect(
      sortLogEntries(entries, LOGS_SORT_ORDER_OLDEST).map(
        (item) => item.message,
      ),
    ).toEqual(['First', 'Second', 'Third']);
  });

  it('applies visible ordering from state sort selection', () => {
    const state = createLogsViewState();
    state.entries = [
      entry({ timestamp: '2026-05-11 09:00:00', message: 'First' }),
      entry({ timestamp: '2026-05-11 09:00:01', message: 'Second' }),
      entry({ timestamp: '2026-05-11 09:00:02', message: 'Third' }),
    ];

    expect(visibleLogEntries(state).map((item) => item.message)).toEqual([
      'Third',
      'Second',
      'First',
    ]);

    setSortOrder(state, LOGS_SORT_ORDER_OLDEST);

    expect(visibleLogEntries(state).map((item) => item.message)).toEqual([
      'First',
      'Second',
      'Third',
    ]);
  });

  it('falls back to newest sort order for invalid values', () => {
    const state = createLogsViewState();

    expect(setSortOrder(state, 'sideways')).toBe(LOGS_SORT_ORDER_NEWEST);
    expect(state.sortOrder).toBe(LOGS_SORT_ORDER_NEWEST);
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
