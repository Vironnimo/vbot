import { describe, expect, it } from 'vitest';

import {
  LOGS_STREAM_STATUS_IDLE,
  applyLogCatalog,
  createLogsViewState,
  deriveLevelOptions,
  filterLogEntries,
  levelOptionValue,
  mergeLogStreamEvent,
  replaceLogEntries,
  selectLogFile,
  setLevelFilter,
  setSearchText,
  visibleLogEntries,
} from '../logsView.js';

describe('logsView helpers', () => {
  it('creates default logs view state', () => {
    expect(createLogsViewState()).toEqual({
      files: [],
      selectedFile: '',
      entries: [],
      levelFilter: 'all',
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
      entry({ level: 'info', message: 'Ready' }),
      entry({ level: 'error', message: 'Failed' }),
    ];

    selectLogFile(state, '2026-05-11');
    setLevelFilter(state, 'error');
    setSearchText(state, 'failed');

    expect(visibleLogEntries(state).map((item) => item.message)).toEqual([
      'Failed',
    ]);
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
