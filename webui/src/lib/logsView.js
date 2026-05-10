export const LOGS_STREAM_STATUS_IDLE = 'idle';
export const LOGS_STREAM_STATUS_CONNECTING = 'connecting';
export const LOGS_STREAM_STATUS_CONNECTED = 'connected';
export const LOGS_STREAM_STATUS_RECONNECTING = 'reconnecting';
export const LOGS_STREAM_STATUS_ERROR = 'error';

const ALL_LEVELS_FILTER = 'all';
const SEARCHABLE_ENTRY_FIELDS = [
  'timestamp',
  'level',
  'logger_name',
  'message',
];

export function createLogsViewState() {
  return {
    files: [],
    selectedFile: '',
    entries: [],
    levelFilter: ALL_LEVELS_FILTER,
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

  if (state.selectedFile && files.includes(state.selectedFile)) {
    return state.selectedFile;
  }

  state.selectedFile = defaultFile || files[0] || '';
  return state.selectedFile;
}

export function selectLogFile(state, file) {
  state.selectedFile = typeof file === 'string' ? file : '';
  return state.selectedFile;
}

export function replaceLogEntries(state, result) {
  state.selectedFile =
    typeof result?.file === 'string' ? result.file : state.selectedFile;
  state.entries = Array.isArray(result?.entries) ? result.entries : [];
  state.readError = '';
  return state.entries;
}

export function mergeLogStreamEvent(state, event) {
  if (event?.file && event.file !== state.selectedFile) {
    return state.entries;
  }

  const nextEntries = Array.isArray(event?.entries) ? event.entries : [];

  if (event?.type === 'reset') {
    state.entries = nextEntries;
    return state.entries;
  }

  if (event?.type === 'append' && nextEntries.length > 0) {
    state.entries = [...state.entries, ...nextEntries];
  }

  return state.entries;
}

export function setLevelFilter(state, level) {
  state.levelFilter =
    typeof level === 'string' && level ? level : ALL_LEVELS_FILTER;
  return state.levelFilter;
}

export function setSearchText(state, value) {
  state.searchText = typeof value === 'string' ? value : '';
  return state.searchText;
}

export function deriveLevelOptions(entries) {
  const levels = new Set();

  for (const entry of Array.isArray(entries) ? entries : []) {
    if (typeof entry?.level === 'string' && entry.level) {
      levels.add(entry.level);
    }
  }

  return [ALL_LEVELS_FILTER, ...Array.from(levels).sort()];
}

export function filterLogEntries(entries, filters = {}) {
  const levelFilter =
    typeof filters.levelFilter === 'string'
      ? filters.levelFilter
      : ALL_LEVELS_FILTER;
  const searchNeedle = normalizeSearchText(filters.searchText);

  return (Array.isArray(entries) ? entries : []).filter((entry) => {
    if (levelFilter !== ALL_LEVELS_FILTER && entry?.level !== levelFilter) {
      return false;
    }

    if (!searchNeedle) {
      return true;
    }

    return buildSearchHaystack(entry).includes(searchNeedle);
  });
}

export function visibleLogEntries(state) {
  return filterLogEntries(state?.entries, {
    levelFilter: state?.levelFilter,
    searchText: state?.searchText,
  });
}

export function levelOptionValue() {
  return ALL_LEVELS_FILTER;
}

function normalizeSearchText(value) {
  return typeof value === 'string' ? value.trim().toLowerCase() : '';
}

function buildSearchHaystack(entry) {
  const parts = [];

  for (const key of SEARCHABLE_ENTRY_FIELDS) {
    if (typeof entry?.[key] === 'string' && entry[key]) {
      parts.push(entry[key]);
    }
  }

  if (typeof entry?.continuation === 'string' && entry.continuation) {
    parts.push(entry.continuation);
  }

  return parts.join(' ').toLowerCase();
}
