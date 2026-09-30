import { describe, expect, it } from 'vitest';

import {
  LOGS_OLDER_FULL,
  LOGS_OLDER_LOAD,
  LOGS_OLDER_NONE,
  LOGS_OLDER_REVEAL,
  LOGS_RENDER_STEP,
  LOGS_STREAM_STATUS_IDLE,
  LOGS_WINDOW_MAX_ENTRIES,
  applyLogCatalog,
  changedFilterSelectionCount,
  clearLogEntries,
  createLogsViewState,
  deriveLevelOptions,
  deriveSortOptions,
  failOlderLogEntriesLoad,
  levelOptionValue,
  logEntryCount,
  mergeLogStreamEvent,
  olderLogEntriesState,
  prependOlderLogEntries,
  renderedLogEntries,
  replaceLogEntries,
  revealOlderLogEntries,
  selectLogFile,
  setLevelFilter,
  setSortOrder,
  setSearchText,
  startOlderLogEntriesLoad,
  visibleLogEntryCount,
} from '../logsView.js';

const FILE = '2026-05-11';

describe('logsView state', () => {
  it('creates default logs view state', () => {
    const state = createLogsViewState();

    expect(state).toMatchObject({
      files: [],
      defaultFile: '',
      selectedFile: '',
      nextBefore: null,
      loadingOlder: false,
      olderError: '',
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
    expect(logEntryCount(state)).toBe(0);
    expect(renderedLogEntries(state)).toEqual([]);
    expect(olderLogEntriesState(state)).toBe(LOGS_OLDER_NONE);
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
      file: FILE,
      entries: [entry(0, { level: 'info' }), entry(10, { level: 'error' })],
    });
    expect(changedFilterSelectionCount(state)).toBe(0);

    setSearchText(state, 'boot');
    expect(changedFilterSelectionCount(state)).toBe(0);

    setLevelFilter(state, 'error');
    setSortOrder(state, 'oldest');
    selectLogFile(state, '2026-05-10');
    expect(changedFilterSelectionCount(state)).toBe(3);

    // A newer daily file becomes the default; the kept older selection still
    // counts as changed.
    applyLogCatalog(state, {
      files: ['2026-05-12', '2026-05-11', '2026-05-10'],
      default_file: '2026-05-12',
    });
    setLevelFilter(state, 'all');
    setSortOrder(state, 'newest');
    expect(changedFilterSelectionCount(state)).toBe(1);
  });

  it('merges live events of the selected file, replacing the entry still being written', () => {
    const state = createLogsViewState();
    replaceLogEntries(state, {
      file: FILE,
      entries: [entry(0, { message: 'Ready' }), entry(20, { message: 'Fail' })],
      next_before: null,
    });
    const entries = state.entryWindow.entries;

    // The last entry grew (a partial line completed, a traceback line added):
    // the append replaces it from its offset on and adds what followed.
    mergeLogStreamEvent(state, {
      type: 'append',
      file: FILE,
      from_offset: 20,
      entries: [
        entry(20, { message: 'Failed', continuation: 'Traceback' }),
        entry(60, { message: 'Retry' }),
      ],
    });
    mergeLogStreamEvent(state, {
      type: 'append',
      file: '2026-05-10',
      from_offset: 0,
      entries: [entry(0, { message: 'Other file' })],
    });
    expect(messages(state)).toEqual(['Retry', 'Failed', 'Ready']);
    expect(state.entryWindow.entries.at(-2).continuation).toBe('Traceback');
    // Appends update the loaded entries in place instead of copying them.
    expect(state.entryWindow.entries).toBe(entries);

    mergeLogStreamEvent(state, {
      type: 'reset',
      file: FILE,
      entries: [entry(0, { message: 'Reset', level: 'warn' })],
      next_before: null,
    });
    expect(messages(state)).toEqual(['Reset']);

    clearLogEntries(state);
    expect(logEntryCount(state)).toBe(0);
  });

  it('keeps the level filter only while the loaded entries include that level', () => {
    const state = createLogsViewState();
    replaceLogEntries(state, {
      file: FILE,
      entries: [entry(0, { level: 'info' }), entry(10, { level: 'warn' })],
    });
    expect(setLevelFilter(state, 'warn')).toBe('warn');
    // A level the entries lack shows all levels.
    expect(setLevelFilter(state, 'error')).toBe('all');

    setLevelFilter(state, 'warn');
    replaceLogEntries(state, {
      file: FILE,
      entries: [entry(0, { level: 'info' })],
    });
    expect(state.levelFilter).toBe('all');

    setLevelFilter(state, 'info');
    mergeLogStreamEvent(state, {
      type: 'reset',
      file: FILE,
      entries: [entry(0, { level: 'warn' })],
    });
    expect(state.levelFilter).toBe('all');
  });

  it('offers all plus the distinct loaded levels in sorted order', () => {
    const state = createLogsViewState();
    replaceLogEntries(state, {
      file: FILE,
      entries: [
        entry(0, { level: 'warn' }),
        entry(10, { level: 'warn', message: 'Second warning' }),
        entry(20, { level: 'info' }),
        entry(30, { level: '' }),
        entry(40, { level: null }),
      ],
    });
    expect(deriveLevelOptions(state)).toEqual([
      levelOptionValue(),
      'info',
      'warn',
    ]);

    mergeLogStreamEvent(state, {
      type: 'append',
      file: FILE,
      from_offset: 50,
      entries: [entry(50, { level: 'error' })],
    });
    expect(deriveLevelOptions(state)).toEqual([
      levelOptionValue(),
      'error',
      'info',
      'warn',
    ]);
  });
});

describe('logsView visible entries', () => {
  it('filters by level and by search text across every entry field', () => {
    const state = createLogsViewState();
    replaceLogEntries(state, {
      file: FILE,
      entries: [
        entry(0, { level: 'info', message: 'Ready' }),
        entry(10, {
          timestamp: '2026-05-11 09:00:01',
          level: 'error',
          message: 'Failed',
          continuation: 'Traceback line',
        }),
        entry(20, {
          level: 'warn',
          logger_name: 'vbot.core.worker',
          message: 'Retry soon',
        }),
      ],
    });

    setLevelFilter(state, 'error');
    expect(messages(state)).toEqual(['Failed']);

    setLevelFilter(state, levelOptionValue());
    setSearchText(state, '  WORKER retry ');
    expect(messages(state)).toEqual(['Retry soon']);

    for (const needle of ['09:00:01', 'error', 'failed', 'traceback']) {
      setSearchText(state, needle);
      expect(messages(state)).toEqual(['Failed']);
    }
    setSearchText(state, 'server.app');
    expect(messages(state)).toEqual(['Failed', 'Ready']);

    // Live entries are filtered as they arrive.
    mergeLogStreamEvent(state, {
      type: 'append',
      file: FILE,
      from_offset: 30,
      entries: [
        entry(30, { message: 'Served' }),
        entry(40, { logger_name: 'vbot.core', message: 'Hidden' }),
      ],
    });
    expect(messages(state)).toEqual(['Served', 'Failed', 'Ready']);
    expect(visibleLogEntryCount(state)).toBe(3);
  });

  it('orders entries newest first unless oldest first is chosen', () => {
    const state = createLogsViewState();
    replaceLogEntries(state, {
      file: FILE,
      entries: ['First', 'Second', 'Third'].map((message, index) =>
        entry(index * 10, { message }),
      ),
    });

    expect(deriveSortOptions()).toEqual(['newest', 'oldest']);
    expect(messages(state)).toEqual(['Third', 'Second', 'First']);

    setSortOrder(state, 'oldest');
    expect(messages(state)).toEqual(['First', 'Second', 'Third']);

    expect(setSortOrder(state, 'sideways')).toBe('newest');
    expect(messages(state)).toEqual(['Third', 'Second', 'First']);
  });
});

describe('logsView older entries', () => {
  it('mounts the newest rows first and reveals the loaded rest step by step', () => {
    const state = createLogsViewState();
    const total = LOGS_RENDER_STEP * 2 + 10;
    replaceLogEntries(state, { file: FILE, entries: entries(0, total) });

    expect(renderedLogEntries(state)).toHaveLength(LOGS_RENDER_STEP);
    expect(renderedLogEntries(state)[0].offset).toBe((total - 1) * 10);
    expect(olderLogEntriesState(state)).toBe(LOGS_OLDER_REVEAL);

    // Live entries join the mounted rows without unmounting older ones.
    mergeLogStreamEvent(state, {
      type: 'append',
      file: FILE,
      from_offset: total * 10,
      entries: [entry(total * 10)],
    });
    expect(renderedLogEntries(state)).toHaveLength(LOGS_RENDER_STEP + 1);

    expect(revealOlderLogEntries(state)).toBe(true);
    expect(revealOlderLogEntries(state)).toBe(true);
    expect(renderedLogEntries(state)).toHaveLength(total + 1);
    expect(revealOlderLogEntries(state)).toBe(false);
    // The whole file is loaded.
    expect(olderLogEntriesState(state)).toBe(LOGS_OLDER_NONE);
  });

  it('loads the page before the oldest entry and ignores stale results', () => {
    const state = createLogsViewState();
    replaceLogEntries(state, {
      file: FILE,
      entries: entries(100, 2),
      next_before: 1000,
    });
    expect(olderLogEntriesState(state)).toBe(LOGS_OLDER_LOAD);

    const request = startOlderLogEntriesLoad(state);
    expect(request).toMatchObject({ file: FILE, before: 1000 });
    expect(state.loadingOlder).toBe(true);
    // One load at a time.
    expect(startOlderLogEntriesLoad(state)).toBeNull();

    expect(
      prependOlderLogEntries(state, request, {
        file: FILE,
        entries: entries(90, 2),
        next_before: 900,
      }),
    ).toBe(true);
    expect(renderedLogEntries(state).map((item) => item.offset)).toEqual([
      1010, 1000, 910, 900,
    ]);
    expect(state.nextBefore).toBe(900);
    expect(state.loadingOlder).toBe(false);

    // A failed load reports its error until the next attempt.
    const failing = startOlderLogEntriesLoad(state);
    expect(failOlderLogEntriesLoad(state, failing, 'Offline')).toBe(true);
    expect(state.olderError).toBe('Offline');
    const retry = startOlderLogEntriesLoad(state);
    expect(state.olderError).toBe('');

    // The file was replaced meanwhile: the late page belongs to other entries.
    mergeLogStreamEvent(state, {
      type: 'reset',
      file: FILE,
      entries: entries(0, 1),
      next_before: null,
    });
    expect(
      prependOlderLogEntries(state, retry, {
        file: FILE,
        entries: entries(80, 2),
        next_before: null,
      }),
    ).toBe(false);
    expect(renderedLogEntries(state).map((item) => item.offset)).toEqual([0]);
    expect(state.loadingOlder).toBe(false);
  });

  it('stops loading older entries once the window holds its maximum', () => {
    const state = createLogsViewState();
    const loaded = LOGS_WINDOW_MAX_ENTRIES - 5;
    replaceLogEntries(state, {
      file: FILE,
      entries: entries(100, loaded),
      next_before: 1000,
    });
    // Loading starts once every loaded entry is mounted.
    expect(startOlderLogEntriesLoad(state)).toBeNull();
    while (revealOlderLogEntries(state));
    const request = startOlderLogEntriesLoad(state);

    prependOlderLogEntries(state, request, {
      file: FILE,
      entries: entries(90, 10),
      next_before: 900,
    });

    // Only the newest five of the page fit; paging would continue there.
    expect(logEntryCount(state)).toBe(LOGS_WINDOW_MAX_ENTRIES);
    expect(state.entryWindow.entries[0].offset).toBe(950);
    expect(state.nextBefore).toBe(950);
    while (revealOlderLogEntries(state));
    expect(olderLogEntriesState(state)).toBe(LOGS_OLDER_FULL);
    expect(startOlderLogEntriesLoad(state)).toBeNull();
  });

  it('drops the oldest entries in batches when live appends overfill the window', () => {
    const state = createLogsViewState();
    replaceLogEntries(state, {
      file: FILE,
      entries: [
        entry(0, { level: 'debug' }),
        ...entries(1, LOGS_WINDOW_MAX_ENTRIES - 1),
      ],
      next_before: null,
    });
    const append = (count) => {
      const start = state.entryWindow.entries.at(-1).offset / 10 + 1;
      mergeLogStreamEvent(state, {
        type: 'append',
        file: FILE,
        from_offset: start * 10,
        entries: entries(start, count),
      });
    };

    // Up to one render step over the maximum stays loaded.
    append(LOGS_RENDER_STEP);
    expect(logEntryCount(state)).toBe(
      LOGS_WINDOW_MAX_ENTRIES + LOGS_RENDER_STEP,
    );
    expect(state.nextBefore).toBeNull();

    append(1);
    expect(logEntryCount(state)).toBe(LOGS_WINDOW_MAX_ENTRIES);
    const oldest = state.entryWindow.entries[0].offset;
    expect(oldest).toBe((LOGS_RENDER_STEP + 1) * 10);
    expect(state.entryWindow.visible[0].offset).toBe(oldest);
    // The dropped entries are older entries again, and their level is gone.
    expect(state.nextBefore).toBe(oldest);
    expect(deriveLevelOptions(state)).toEqual([levelOptionValue(), 'info']);
  });
});

function messages(state) {
  return renderedLogEntries(state).map((item) => item.message);
}

// `count` consecutive entries whose offsets are 10 apart, starting at
// `first * 10`.
function entries(first, count) {
  return Array.from({ length: count }, (_, index) =>
    entry((first + index) * 10, { message: `Entry ${first + index}` }),
  );
}

function entry(offset, overrides = {}) {
  return {
    offset,
    timestamp: '2026-05-11 09:00:00',
    level: 'info',
    logger_name: 'vbot.server.app',
    message: 'Ready',
    continuation: '',
    ...overrides,
  };
}
