export const LOGS_STREAM_STATUS_IDLE = 'idle';
export const LOGS_STREAM_STATUS_CONNECTING = 'connecting';
export const LOGS_STREAM_STATUS_CONNECTED = 'connected';
export const LOGS_STREAM_STATUS_RECONNECTING = 'reconnecting';
export const LOGS_STREAM_STATUS_ERROR = 'error';
const LOGS_SORT_ORDER_NEWEST = 'newest';
const LOGS_SORT_ORDER_OLDEST = 'oldest';

// The view holds at most this many entries of the shown file (ten server
// pages). Loading older entries stops here; live appends beyond it drop the
// oldest entries, in batches of LOGS_RENDER_STEP so an append rarely copies.
export const LOGS_WINDOW_MAX_ENTRIES = 5000;
// Rows mounted for a newly shown file, and added by each "load older" step.
export const LOGS_RENDER_STEP = 250;

// What the control past the oldest shown row offers.
export const LOGS_OLDER_REVEAL = 'reveal'; // more loaded rows to mount
export const LOGS_OLDER_LOAD = 'load'; // an older page to read from the server
export const LOGS_OLDER_FULL = 'full'; // older entries exist but the window is full
export const LOGS_OLDER_NONE = 'none'; // the file start is shown

const ALL_LEVELS_FILTER = 'all';
const SORT_ORDER_OPTIONS = [LOGS_SORT_ORDER_NEWEST, LOGS_SORT_ORDER_OLDEST];
const SEARCHABLE_ENTRY_FIELDS = [
  'timestamp',
  'level',
  'logger_name',
  'message',
];

const searchHaystacks = new WeakMap();

// The loaded entries of the shown file. A class instance is not deep-proxied
// by Svelte state, so thousands of entries cost no per-entry proxies and every
// change is an in-place update; each change bumps the state's `revision`, which
// is what readers depend on. Both arrays are in file order, oldest first, and
// every entry has a unique byte `offset` that increases with that order.
class LogEntryWindow {
  constructor() {
    this.entries = [];
    // The entries that match the level and search filters.
    this.visible = [];
    this.levelCounts = new Map();
  }
}

export function createLogsViewState() {
  return {
    files: [],
    defaultFile: '',
    selectedFile: '',
    entryWindow: new LogEntryWindow(),
    // Bumped by every change to entryWindow, whose contents are not reactive.
    revision: 0,
    // Bumped whenever the shown entries are replaced as a whole.
    generation: 0,
    // Byte offset older entries are read before, or null at the file start.
    nextBefore: null,
    // Visible entries at or after this byte offset are mounted.
    renderFrom: 0,
    loadingOlder: false,
    // Identifies the current older-entries load; replaced when it goes stale.
    olderRequest: 0,
    olderError: '',
    levelFilter: ALL_LEVELS_FILTER,
    sortOrder: LOGS_SORT_ORDER_NEWEST,
    searchText: '',
    loadingCatalog: false,
    loadingEntries: false,
    catalogError: '',
    readError: '',
    streamError: '',
    streamStatus: LOGS_STREAM_STATUS_IDLE,
  };
}

export function applyLogCatalog(state, result) {
  const files = Array.isArray(result?.files) ? result.files : [];
  const defaultFile =
    typeof result?.default_file === 'string' ? result.default_file : '';

  state.files = files;
  state.defaultFile = defaultFile || files[0] || '';

  if (state.selectedFile && files.includes(state.selectedFile)) {
    return state.selectedFile;
  }

  state.selectedFile = state.defaultFile;
  return state.selectedFile;
}

export function selectLogFile(state, file) {
  state.selectedFile = typeof file === 'string' ? file : '';
  return state.selectedFile;
}

// Shows a `log.read` result: the newest page of the file.
export function replaceLogEntries(state, result) {
  state.selectedFile =
    typeof result?.file === 'string' ? result.file : state.selectedFile;
  resetWindow(state, logEntriesOf(result), nextBeforeOf(result));
  state.readError = '';
}

export function clearLogEntries(state) {
  resetWindow(state, [], null);
}

// Applies a live `reset` or `append` event of the shown file. An append first
// drops the entries at or after its `from_offset`: the entry that was still
// being written when the previous event was sent.
export function mergeLogStreamEvent(state, event) {
  if (event?.file && event.file !== state.selectedFile) {
    return false;
  }

  if (event?.type === 'reset') {
    resetWindow(state, logEntriesOf(event), nextBeforeOf(event));
    return true;
  }

  if (event?.type === 'append') {
    appendEntries(state, event.from_offset, logEntriesOf(event));
    return true;
  }

  return false;
}

export function olderLogEntriesState(state) {
  if (firstRenderedIndex(state) > 0) {
    return LOGS_OLDER_REVEAL;
  }
  if (state.nextBefore === null) {
    return LOGS_OLDER_NONE;
  }
  return state.entryWindow.entries.length >= LOGS_WINDOW_MAX_ENTRIES
    ? LOGS_OLDER_FULL
    : LOGS_OLDER_LOAD;
}

// Mounts up to LOGS_RENDER_STEP more of the loaded visible entries.
export function revealOlderLogEntries(state) {
  const first = firstRenderedIndex(state);
  if (first === 0) {
    return false;
  }

  const visible = state.entryWindow.visible;
  state.renderFrom = visible[Math.max(0, first - LOGS_RENDER_STEP)].offset;
  return true;
}

// Starts loading the page before the oldest loaded entry. Returns the request
// to hand back with its result, or null when there is nothing to load.
export function startOlderLogEntriesLoad(state) {
  if (state.loadingOlder || olderLogEntriesState(state) !== LOGS_OLDER_LOAD) {
    return null;
  }

  state.olderRequest += 1;
  state.loadingOlder = true;
  state.olderError = '';
  return {
    id: state.olderRequest,
    file: state.selectedFile,
    before: state.nextBefore,
  };
}

// Prepends an older page and mounts its newest visible entries. A result for a
// request that went stale (other file, replaced entries, trimmed window) is
// ignored.
export function prependOlderLogEntries(state, request, result) {
  if (!isCurrentOlderRequest(state, request)) {
    return false;
  }
  state.loadingOlder = false;

  const win = state.entryWindow;
  const oldest = win.entries.length > 0 ? win.entries[0].offset : Infinity;
  let page = logEntriesOf(result).filter((entry) => entry.offset < oldest);
  let nextBefore = nextBeforeOf(result);
  const room = Math.max(0, LOGS_WINDOW_MAX_ENTRIES - win.entries.length);
  if (page.length > room) {
    page = page.slice(page.length - room);
    nextBefore = page.length > 0 ? page[0].offset : state.nextBefore;
  }

  const matches = entryFilter(state);
  for (const entry of page) {
    countLevel(win, entry, 1);
  }
  win.entries = page.concat(win.entries);
  win.visible = page.filter(matches).concat(win.visible);
  state.nextBefore = nextBefore;
  revealOlderLogEntries(state);
  state.revision += 1;
  return true;
}

export function failOlderLogEntriesLoad(state, request, message) {
  if (!isCurrentOlderRequest(state, request)) {
    return false;
  }

  state.loadingOlder = false;
  state.olderError = message;
  return true;
}

// The mounted rows in display order.
export function renderedLogEntries(state) {
  const rendered = state.entryWindow.visible.slice(firstRenderedIndex(state));
  return state.sortOrder === LOGS_SORT_ORDER_OLDEST
    ? rendered
    : rendered.reverse();
}

export function visibleLogEntryCount(state) {
  return state.entryWindow.visible.length;
}

export function logEntryCount(state) {
  return state.entryWindow.entries.length;
}

// Only a level the loaded entries have can be chosen; anything else shows all.
export function setLevelFilter(state, level) {
  const next =
    typeof level === 'string' && state.entryWindow.levelCounts.has(level)
      ? level
      : ALL_LEVELS_FILTER;
  if (next !== state.levelFilter) {
    state.levelFilter = next;
    refilter(state);
  }
  return state.levelFilter;
}

export function setSortOrder(state, value) {
  state.sortOrder = SORT_ORDER_OPTIONS.includes(value)
    ? value
    : LOGS_SORT_ORDER_NEWEST;
  return state.sortOrder;
}

export function setSearchText(state, value) {
  const next = typeof value === 'string' ? value : '';
  const changed =
    normalizeSearchText(next) !== normalizeSearchText(state.searchText);
  state.searchText = next;
  if (changed) {
    refilter(state);
  }
  return state.searchText;
}

export function deriveLevelOptions(state) {
  return [
    ALL_LEVELS_FILTER,
    ...Array.from(state.entryWindow.levelCounts.keys()).sort(),
  ];
}

// Counts the file, level, and order choices that differ from the default view
// (catalog default file, all levels, newest first). Search text is excluded:
// it is a free-text query rather than a choice among options.
export function changedFilterSelectionCount(state) {
  let count = 0;
  if (state?.selectedFile && state.selectedFile !== state.defaultFile) {
    count += 1;
  }
  if (state?.levelFilter && state.levelFilter !== ALL_LEVELS_FILTER) {
    count += 1;
  }
  if (state?.sortOrder && state.sortOrder !== LOGS_SORT_ORDER_NEWEST) {
    count += 1;
  }
  return count;
}

export function levelOptionValue() {
  return ALL_LEVELS_FILTER;
}

export function deriveSortOptions() {
  return SORT_ORDER_OPTIONS;
}

function resetWindow(state, entries, nextBefore) {
  const win = state.entryWindow;
  const kept =
    entries.length > LOGS_WINDOW_MAX_ENTRIES
      ? entries.slice(entries.length - LOGS_WINDOW_MAX_ENTRIES)
      : entries;
  win.entries = [];
  win.levelCounts = new Map();
  for (const entry of kept) {
    const last = win.entries.at(-1);
    if (last === undefined || entry.offset > last.offset) {
      win.entries.push(entry);
      countLevel(win, entry, 1);
    }
  }
  state.nextBefore =
    kept.length < entries.length ? win.entries[0].offset : nextBefore;
  state.generation += 1;
  invalidateOlderLoad(state);
  state.olderError = '';
  // A level the new entries lack would hide them all.
  if (!win.levelCounts.has(state.levelFilter)) {
    state.levelFilter = ALL_LEVELS_FILTER;
  }
  refilter(state);
}

function appendEntries(state, fromOffset, entries) {
  const win = state.entryWindow;
  if (Number.isInteger(fromOffset)) {
    while (win.entries.length > 0 && win.entries.at(-1).offset >= fromOffset) {
      const removed = win.entries.pop();
      countLevel(win, removed, -1);
      if (win.visible.at(-1) === removed) {
        win.visible.pop();
      }
    }
  }

  const matches = entryFilter(state);
  for (const entry of entries) {
    const last = win.entries.at(-1);
    if (last !== undefined && entry.offset <= last.offset) {
      continue;
    }
    win.entries.push(entry);
    countLevel(win, entry, 1);
    if (matches(entry)) {
      win.visible.push(entry);
    }
  }
  trimWindow(state);
  state.revision += 1;
}

function trimWindow(state) {
  const win = state.entryWindow;
  const excess = win.entries.length - LOGS_WINDOW_MAX_ENTRIES;
  if (excess <= LOGS_RENDER_STEP) {
    return;
  }

  for (const entry of win.entries.splice(0, excess)) {
    countLevel(win, entry, -1);
  }
  const oldest = win.entries[0].offset;
  win.visible.splice(0, lowerBound(win.visible, oldest));
  state.nextBefore = oldest;
  invalidateOlderLoad(state);
}

// Recomputes the visible entries and mounts the newest LOGS_RENDER_STEP.
function refilter(state) {
  const win = state.entryWindow;
  win.visible = win.entries.filter(entryFilter(state));
  const count = Math.min(LOGS_RENDER_STEP, win.visible.length);
  // Without a visible entry, entries from the newest one on still mount: the
  // newest entry may be replaced by a matching version of itself.
  state.renderFrom =
    count > 0
      ? win.visible[win.visible.length - count].offset
      : (win.entries.at(-1)?.offset ?? 0);
  state.revision += 1;
}

function invalidateOlderLoad(state) {
  state.olderRequest += 1;
  state.loadingOlder = false;
}

function isCurrentOlderRequest(state, request) {
  return (
    state.loadingOlder &&
    request?.id === state.olderRequest &&
    request.file === state.selectedFile &&
    request.before === state.nextBefore
  );
}

function firstRenderedIndex(state) {
  return lowerBound(state.entryWindow.visible, state.renderFrom);
}

// The index of the first entry whose offset is at least `offset`.
function lowerBound(entries, offset) {
  let low = 0;
  let high = entries.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (entries[middle].offset < offset) {
      low = middle + 1;
    } else {
      high = middle;
    }
  }
  return low;
}

function countLevel(win, entry, delta) {
  const level = entry.level;
  if (typeof level !== 'string' || !level) {
    return;
  }

  const count = (win.levelCounts.get(level) ?? 0) + delta;
  if (count > 0) {
    win.levelCounts.set(level, count);
  } else {
    win.levelCounts.delete(level);
  }
}

function entryFilter(state) {
  const levelFilter = state.levelFilter;
  const searchNeedle = normalizeSearchText(state.searchText);

  return (entry) => {
    if (levelFilter !== ALL_LEVELS_FILTER && entry.level !== levelFilter) {
      return false;
    }

    return !searchNeedle || searchHaystack(entry).includes(searchNeedle);
  };
}

function logEntriesOf(result) {
  return Array.isArray(result?.entries)
    ? result.entries.filter(
        (entry) =>
          entry !== null &&
          typeof entry === 'object' &&
          Number.isInteger(entry.offset) &&
          entry.offset >= 0,
      )
    : [];
}

function nextBeforeOf(result) {
  const value = result?.next_before;
  return Number.isInteger(value) && value > 0 ? value : null;
}

function normalizeSearchText(value) {
  return typeof value === 'string' ? value.trim().toLowerCase() : '';
}

function searchHaystack(entry) {
  let haystack = searchHaystacks.get(entry);
  if (haystack !== undefined) {
    return haystack;
  }

  const parts = [];
  for (const key of SEARCHABLE_ENTRY_FIELDS) {
    if (typeof entry?.[key] === 'string' && entry[key]) {
      parts.push(entry[key]);
    }
  }
  if (typeof entry?.continuation === 'string' && entry.continuation) {
    parts.push(entry.continuation);
  }

  haystack = parts.join(' ').toLowerCase();
  searchHaystacks.set(entry, haystack);
  return haystack;
}
