import {
  createTerminalGroup,
  deleteTerminalGroup,
  forgetTerminal,
  killTerminal,
  listTerminals,
  renameTerminalGroup,
  resizeTerminal,
  sendTerminalInput,
  setTerminalGroupOrder,
  startTerminal,
  subscribeTerminalEvents,
} from './api.js';
import { reconnectBackoffDelay } from './backoff.js';

export function createPtyFrameSanitizer() {
  let pending = '';

  function next(chunk) {
    const combined = pending + chunk;
    if (combined === '') {
      pending = '';
      return '';
    }
    const lastEsc = combined.lastIndexOf('\x1b');
    if (lastEsc !== -1 && PARTIAL_ESC.test(combined.slice(lastEsc))) {
      pending = combined.slice(lastEsc);
      return combined
        .slice(0, lastEsc)
        .replace(BLANK_LINE_BURST, COLLAPSED_BURST);
    }
    const trailing = TRAILING_NEWLINES.exec(combined);
    if (trailing.index === 0) {
      pending = combined;
      return '';
    }
    pending = trailing[0];
    return combined
      .slice(0, trailing.index)
      .replace(BLANK_LINE_BURST, COLLAPSED_BURST);
  }

  function flush() {
    const last = pending;
    pending = '';
    if (last === '' || last.includes('\x1b')) {
      return '';
    }
    return last.replace(BLANK_LINE_BURST, COLLAPSED_BURST);
  }

  return { next, flush };
}

export const TERMINAL_STREAM_IDLE = 'idle';
export const TERMINAL_STREAM_CONNECTING = 'connecting';
export const TERMINAL_STREAM_CONNECTED = 'connected';
export const TERMINAL_STREAM_RECONNECTING = 'reconnecting';
export const TERMINAL_STREAM_ERROR = 'error';
export const TERMINAL_STREAM_SNAPSHOT = 'snapshot';

const RECONNECT_INITIAL_DELAY_MS = 500;
const RECONNECT_MAX_DELAY_MS = 8_000;
const RESIZE_DEBOUNCE_MS = 100;
const INPUT_FLUSH_DELAY_MS = 24;
const INPUT_CHUNK_CHARS = 32_768;
// A socket that still sits in WS_CONNECTING past this budget is treated as
// wedged (half-open after a sleep/radio/network handoff) and force-closed so
// the ordinary close path schedules a reconnect; such sockets never fire
// open or close on their own.
const CONNECT_TIMEOUT_MS = 8_000;
const WS_CONNECTING = 0;
const WS_OPEN = 1;
// A WS frame boundary can split an ANSI escape sequence, a UTF-8 code point,
// or a long blank-line run in two. Feeding xterm the torn halves corrupts its
// parser (it can swallow the output after a reconnect). This sanitizer holds
// back a trailing partial escape / newline run until the next frame completes
// it, mirrors Hermes' pty-resume sanitizer.
// A WS frame boundary can split an ANSI escape sequence, a UTF-8 code point,
// or a long blank-line run in two. Feeding xterm the torn halves corrupts its
// parser (it can swallow the output after a reconnect). This sanitizer holds
// back a trailing partial escape / newline run until the next frame completes
// it, mirrors Hermes' pty-resume sanitizer.
// eslint-disable-next-line no-control-regex -- intentional ESC byte in ANSI sequence parser
const PARTIAL_ESC = /^\x1b(?:\[\d*)?$/;
const TRAILING_NEWLINES = /(?:\r?\n)*\r?$/;
const BLANK_LINE_BURST = /(?:\r?\n){50,}/g;
const COLLAPSED_BURST = '\r\n\r\n';
const TERMINAL_STATES_FINISHED = new Set(['exited', 'error']);

/**
 * Fixed canvas grid layout for a terminal count.
 *
 * @param {number} count
 * @returns {{ rows: number, columns: number, spans: Array<{ row: number, column: number, rowSpan: number, columnSpan: number }> }}
 */
export function layoutForCount(count) {
  const total = Number.isInteger(count) && count > 0 ? count : 0;
  const columns =
    total === 0 ? 0 : total <= 2 ? total : total <= 4 ? 2 : total <= 6 ? 3 : 4;
  const rows = total === 0 ? 0 : Math.ceil(total / columns);
  const spans = [];
  for (let index = 0; index < total; index += 1) {
    let row;
    let column;
    if (total === 3) {
      row = index === 0 ? 0 : 1;
      column = index === 0 ? 0 : index - 1;
    } else {
      row = Math.floor(index / columns);
      column = index % columns;
    }
    const span = {
      row,
      column,
      rowSpan: 1,
      columnSpan: 1,
    };
    if (total === 3 && index === 0) {
      span.columnSpan = 2;
    }
    spans.push(span);
  }
  return { rows, columns, spans };
}

export function createTerminalsViewState() {
  return {
    terminals: [],
    groups: [],
    launchHistory: [],
    selectedGroupId: '',
    selectedTerminalId: '',
    loading: false,
    listError: '',
    actionError: '',
    streams: {},
    killing: '',
    forgetting: '',
    groupActionPending: false,
    startingTerminal: false,
    startError: '',
  };
}

export function visibleTerminals(state) {
  if (!state.selectedGroupId) {
    return [];
  }
  return state.terminals.filter(
    (terminal) => terminal?.group_id === state.selectedGroupId,
  );
}

export function selectedGroup(state) {
  return (
    state.groups.find((group) => group.group_id === state.selectedGroupId) ??
    null
  );
}

export function selectedTerminal(state) {
  return (
    state.terminals.find(
      (terminal) => terminal.terminal_id === state.selectedTerminalId,
    ) ?? null
  );
}

export function terminalIsFinished(terminal) {
  return TERMINAL_STATES_FINISHED.has(terminal?.state);
}

export function reconcileTerminalList(state, result) {
  const terminals = Array.isArray(result?.terminals) ? result.terminals : [];
  state.terminals = terminals.filter(
    (terminal) => terminal && typeof terminal.terminal_id === 'string',
  );
  reconcileTerminalGroups(state, result);
  reconcileTerminalLaunchHistory(state, result);
  if (
    state.selectedGroupId &&
    !state.groups.some((group) => group.group_id === state.selectedGroupId)
  ) {
    state.selectedGroupId = state.groups[0]?.group_id ?? '';
  }
  if (
    state.selectedTerminalId &&
    state.terminals.some(
      (terminal) => terminal.terminal_id === state.selectedTerminalId,
    )
  ) {
    return state.selectedTerminalId;
  }
  const firstVisible = visibleTerminals(state)[0];
  state.selectedTerminalId = firstVisible?.terminal_id ?? '';
  return state.selectedTerminalId;
}

export function reconcileTerminalGroups(state, result) {
  if (!Array.isArray(result?.groups)) {
    return state.groups;
  }
  state.groups = result.groups.filter(
    (group) =>
      group &&
      typeof group.group_id === 'string' &&
      group.group_id &&
      typeof group.name === 'string',
  );
  if (!state.selectedGroupId) {
    const firstOccupied =
      state.groups.find((group) => Number(group.terminal_count) > 0) ?? null;
    state.selectedGroupId = (firstOccupied ?? state.groups[0])?.group_id ?? '';
  }
  return state.groups;
}

export function reconcileTerminalLaunchHistory(state, result) {
  if (!Array.isArray(result?.launch_history)) {
    return state.launchHistory;
  }
  state.launchHistory = result.launch_history.filter(
    (entry) =>
      entry &&
      typeof entry.id === 'string' &&
      entry.id &&
      (entry.command === null || typeof entry.command === 'string') &&
      Array.isArray(entry.args) &&
      entry.args.every((argument) => typeof argument === 'string') &&
      (entry.workdir === null || typeof entry.workdir === 'string'),
  );
  return state.launchHistory;
}

export function mergeTerminalSummary(state, terminal) {
  if (!terminal || typeof terminal.terminal_id !== 'string') {
    return null;
  }
  const index = state.terminals.findIndex(
    (item) => item.terminal_id === terminal.terminal_id,
  );
  if (index < 0) {
    state.terminals = [terminal, ...state.terminals];
  } else {
    state.terminals[index] = { ...state.terminals[index], ...terminal };
  }
  return terminal;
}

export function reconcileGroup(state, group) {
  if (!group || typeof group.group_id !== 'string') {
    return null;
  }
  const index = state.groups.findIndex(
    (item) => item.group_id === group.group_id,
  );
  if (index < 0) {
    state.groups = [group, ...state.groups];
  } else {
    state.groups[index] = { ...state.groups[index], ...group };
  }
  return group;
}

export function reconcileSingleGroup(state, group) {
  const merged = reconcileGroup(state, group);
  if (merged) {
    reconcileTerminalList(state, {
      groups: state.groups,
      terminals: state.terminals,
    });
  }
  return merged;
}

export function createTerminalsController({
  state,
  onSnapshot = () => {},
  onOutput = () => {},
  onClear = () => {},
  api = {
    createTerminalGroup,
    deleteTerminalGroup,
    forgetTerminal,
    killTerminal,
    listTerminals,
    renameTerminalGroup,
    resizeTerminal,
    sendTerminalInput,
    setTerminalGroupOrder,
    startTerminal,
    subscribeTerminalEvents,
  },
  setTimeoutFn = globalThis.setTimeout,
  clearTimeoutFn = globalThis.clearTimeout,
}) {
  let destroyed = false;
  let serverUnavailable = false;
  let listRequestId = 0;
  const streamRecords = new Map();

  function streamView(terminalId) {
    return (
      state.streams[terminalId] ?? {
        status: TERMINAL_STREAM_IDLE,
        error: '',
        errorCode: '',
      }
    );
  }

  function setStreamView(terminalId, patch) {
    state.streams[terminalId] = { ...streamView(terminalId), ...patch };
  }

  function removeStreamView(terminalId) {
    if (!(terminalId in state.streams)) {
      return;
    }
    const next = { ...state.streams };
    delete next[terminalId];
    state.streams = next;
  }

  async function start() {
    await loadTerminals();
  }

  async function loadTerminals({ silent = false } = {}) {
    const requestId = ++listRequestId;
    if (!silent) {
      state.loading = true;
    }
    state.listError = '';
    try {
      const result = await api.listTerminals();
      if (destroyed || requestId !== listRequestId) {
        return;
      }
      reconcileTerminalList(state, result);
      reconcileStreams();
    } catch (error) {
      if (!destroyed && requestId === listRequestId && !serverUnavailable) {
        state.listError = errorMessage(error);
      }
    } finally {
      if (requestId === listRequestId) {
        state.loading = false;
      }
    }
  }

  function selectTerminal(terminalId) {
    if (
      terminalId === state.selectedTerminalId ||
      !state.terminals.some((item) => item.terminal_id === terminalId)
    ) {
      return;
    }
    state.selectedTerminalId = terminalId;
    state.actionError = '';
  }

  function selectGroup(groupId) {
    if (groupId === state.selectedGroupId) {
      return;
    }
    if (!state.groups.some((group) => group.group_id === groupId)) {
      return;
    }
    state.selectedGroupId = groupId;
    state.actionError = '';
    const firstVisible = visibleTerminals(state)[0];
    if (
      state.selectedTerminalId &&
      !state.terminals.some(
        (terminal) =>
          terminal.terminal_id === state.selectedTerminalId &&
          terminal.group_id === groupId,
      )
    ) {
      state.selectedTerminalId = firstVisible?.terminal_id ?? '';
    }
    reconcileStreams();
  }

  function reconcileStreams() {
    const listedIds = new Set(
      visibleTerminals(state).map((terminal) => terminal.terminal_id),
    );
    for (const stream of [...streamRecords.values()]) {
      if (!listedIds.has(stream.terminalId)) {
        closeStream(stream);
      }
    }
    if (serverUnavailable) {
      return;
    }
    for (const terminal of visibleTerminals(state)) {
      if (!streamRecords.has(terminal.terminal_id)) {
        connectStream(terminal.terminal_id);
      }
    }
  }

  function connectStream(terminalId) {
    if (destroyed || serverUnavailable) {
      return;
    }
    let stream = streamRecords.get(terminalId);
    if (!stream) {
      stream = {
        terminalId,
        connection: null,
        shouldReconnect: true,
        terminalEnded: false,
        lastSequence: 0,
        reconnectTimer: null,
        reconnectAttempt: 0,
        connectTimer: null,
        sanitizer: createPtyFrameSanitizer(),
        inputTimer: null,
        inputBuffer: '',
        inputChain: Promise.resolve(),
        resizeTimer: null,
        pendingResize: null,
        lastResize: null,
        resizeChain: Promise.resolve(),
      };
      streamRecords.set(terminalId, stream);
    }
    clearReconnectTimer(stream);
    setStreamView(terminalId, {
      status:
        stream.reconnectAttempt > 0
          ? TERMINAL_STREAM_RECONNECTING
          : TERMINAL_STREAM_CONNECTING,
      error: '',
      errorCode: '',
    });
    try {
      stream.connection = api.subscribeTerminalEvents(terminalId, {
        onEvent: (event) => handleStreamEvent(stream, event),
        onError: (error) => {
          if (streamRecords.get(terminalId) !== stream) {
            return;
          }
          setStreamView(terminalId, {
            error: errorMessage(error),
            errorCode: '',
            status: TERMINAL_STREAM_ERROR,
          });
        },
        onClose: () => handleStreamClose(stream),
      });
      const socket = stream.connection?.socket;
      if (socket && socket.readyState === WS_CONNECTING) {
        stream.connectTimer = setTimeoutFn(() => {
          stream.connectTimer = null;
          if (streamRecords.get(terminalId) !== stream) {
            return;
          }
          const current = stream.connection?.socket;
          if (
            !current ||
            current.readyState === WS_OPEN ||
            stream.terminalEnded
          ) {
            return;
          }
          // Still connecting past the budget: treat as wedged. Forcing the
          // close runs the ordinary reconnect path (which this handler's
          // onClose feeds); the socket never opened, so nothing is lost.
          stream.connection?.close();
          stream.connection = null;
          scheduleReconnect(terminalId, 0);
        }, CONNECT_TIMEOUT_MS);
      }
    } catch (error) {
      setStreamView(terminalId, {
        error: errorMessage(error),
        errorCode: '',
        status: TERMINAL_STREAM_ERROR,
      });
      scheduleReconnect(terminalId);
    }
  }

  function handleStreamEvent(stream, event) {
    if (streamRecords.get(stream.terminalId) !== stream) {
      return;
    }
    if (!event || typeof event !== 'object') {
      return;
    }
    const sequence = Number.isInteger(event.sequence) ? event.sequence : null;
    if (event.type === 'terminal_ready') {
      if (sequence === null || typeof event.ansi !== 'string') {
        reconnectForGap(stream);
        return;
      }
      // Fresh stream: any partially-held frame tail from the previous socket
      // is dead and must not bleed into the rebuilt buffer.
      stream.sanitizer = createPtyFrameSanitizer();
      clearConnectTimer(stream);
      stream.lastSequence = sequence;
      stream.reconnectAttempt = 0;
      const terminal = mergeTerminalSummary(state, event.terminal);
      const finished = terminalIsFinished(terminal);
      setStreamView(stream.terminalId, {
        status: finished ? TERMINAL_STREAM_SNAPSHOT : TERMINAL_STREAM_CONNECTED,
        error: '',
        errorCode: '',
      });
      onSnapshot(stream.terminalId, event.ansi, terminal);
      if (finished) {
        markStreamFinished(stream);
        void loadTerminals({ silent: true });
      }
      return;
    }
    if (sequence === null || sequence <= stream.lastSequence) {
      return;
    }
    if (sequence !== stream.lastSequence + 1) {
      reconnectForGap(stream);
      return;
    }
    stream.lastSequence = sequence;
    if (event.type === 'terminal_output' && typeof event.data === 'string') {
      const sanitized = stream.sanitizer.next(event.data);
      if (sanitized) {
        onOutput(stream.terminalId, sanitized);
      }
      return;
    }
    if (event.type === 'terminal_snapshot' && typeof event.ansi === 'string') {
      const terminal = mergeTerminalSummary(state, event.terminal);
      onSnapshot(stream.terminalId, event.ansi, terminal);
      return;
    }
    if (event.type === 'terminal_state') {
      const terminal = mergeTerminalSummary(state, event.terminal);
      if (terminalIsFinished(terminal)) {
        markStreamFinished(stream);
        void loadTerminals({ silent: true });
      }
    }
  }

  function markStreamFinished(stream) {
    stream.terminalEnded = true;
    stream.shouldReconnect = false;
    clearPendingInput(stream);
    clearPendingResize(stream);
    setStreamView(stream.terminalId, {
      status: TERMINAL_STREAM_SNAPSHOT,
    });
    if (state.selectedTerminalId === stream.terminalId) {
      state.actionError = '';
    }
  }

  function reconnectForGap(stream) {
    if (streamRecords.get(stream.terminalId) !== stream) {
      return;
    }
    setStreamView(stream.terminalId, {
      status: TERMINAL_STREAM_RECONNECTING,
      error: '',
      errorCode: 'gap',
    });
    const terminalId = stream.terminalId;
    stream.connection?.close(1000, 'terminals-view-gap');
    stream.connection = null;
    scheduleReconnect(terminalId, 0);
  }

  function handleStreamClose(stream) {
    if (streamRecords.get(stream.terminalId) !== stream) {
      return;
    }
    stream.connection = null;
    if (
      destroyed ||
      serverUnavailable ||
      stream.terminalEnded ||
      !stream.shouldReconnect
    ) {
      return;
    }
    if (stream.reconnectTimer !== null) {
      return;
    }
    setStreamView(stream.terminalId, {
      status: TERMINAL_STREAM_RECONNECTING,
      error: '',
      errorCode: '',
    });
    scheduleReconnect(stream.terminalId);
  }

  function scheduleReconnect(terminalId, explicitDelay) {
    if (destroyed || serverUnavailable) {
      return;
    }
    const stream = streamRecords.get(terminalId);
    if (!stream || stream.terminalEnded || !stream.shouldReconnect) {
      return;
    }
    clearReconnectTimer(stream);
    const delay =
      explicitDelay ??
      reconnectBackoffDelay(stream.reconnectAttempt, {
        initialDelayMs: RECONNECT_INITIAL_DELAY_MS,
        maxDelayMs: RECONNECT_MAX_DELAY_MS,
      });
    stream.reconnectAttempt += 1;
    stream.reconnectTimer = setTimeoutFn(() => {
      stream.reconnectTimer = null;
      if (!destroyed && !serverUnavailable && streamRecords.has(terminalId)) {
        connectStream(terminalId);
      }
    }, delay);
  }

  function queueInput(
    data,
    { immediate = false, terminalId = state.selectedTerminalId } = {},
  ) {
    const item = state.terminals.find(
      (terminal) => terminal.terminal_id === terminalId,
    );
    if (
      typeof data !== 'string' ||
      !data ||
      !terminalId ||
      !item ||
      terminalIsFinished(item) ||
      serverUnavailable
    ) {
      return;
    }
    const stream = streamRecords.get(terminalId);
    if (!stream || stream.terminalEnded) {
      return;
    }
    stream.inputBuffer += data;
    if (immediate || stream.inputBuffer.length >= INPUT_CHUNK_CHARS) {
      flushInput(stream);
      return;
    }
    if (stream.inputTimer === null) {
      stream.inputTimer = setTimeoutFn(
        () => flushInput(stream),
        INPUT_FLUSH_DELAY_MS,
      );
    }
  }

  function flushInput(stream) {
    if (stream.inputTimer !== null) {
      clearTimeoutFn(stream.inputTimer);
      stream.inputTimer = null;
    }
    if (!stream.inputBuffer) {
      return;
    }
    const terminalId = stream.terminalId;
    const data = stream.inputBuffer.slice(0, INPUT_CHUNK_CHARS);
    stream.inputBuffer = stream.inputBuffer.slice(data.length);
    stream.inputChain = stream.inputChain
      .catch(() => undefined)
      .then(async () => {
        if (stream.terminalEnded) {
          return;
        }
        try {
          await api.sendTerminalInput(terminalId, data);
          if (!destroyed && state.selectedTerminalId === terminalId) {
            state.actionError = '';
          }
        } catch (error) {
          if (
            !destroyed &&
            !stream.terminalEnded &&
            state.selectedTerminalId === terminalId
          ) {
            state.actionError = errorMessage(error);
          }
        }
      });
    if (stream.inputBuffer) {
      stream.inputTimer = setTimeoutFn(() => flushInput(stream), 0);
    }
  }

  function resize(
    columns,
    rows,
    terminalId = state.selectedTerminalId,
    immediate = false,
  ) {
    const item = state.terminals.find(
      (terminal) => terminal.terminal_id === terminalId,
    );
    if (
      !Number.isInteger(columns) ||
      !Number.isInteger(rows) ||
      !terminalId ||
      !item ||
      terminalIsFinished(item) ||
      serverUnavailable
    ) {
      return;
    }
    const stream = streamRecords.get(terminalId);
    if (!stream || stream.terminalEnded) {
      return;
    }
    // Skip resizes that only repeat the terminal's authoritative dimensions.
    // Without this, every tab revisit re-sends the same size on its fresh
    // stream and makes the foreground program repaint for nothing.
    if (item.columns === columns && item.rows === rows) {
      clearPendingResize(stream);
      return;
    }
    stream.pendingResize = { terminalId, columns, rows };
    if (
      stream.lastResize?.terminalId === stream.pendingResize.terminalId &&
      stream.lastResize.columns === columns &&
      stream.lastResize.rows === rows
    ) {
      return;
    }
    if (stream.resizeTimer !== null) {
      clearTimeoutFn(stream.resizeTimer);
      stream.resizeTimer = null;
    }
    if (immediate) {
      flushResize(stream);
      return;
    }
    stream.resizeTimer = setTimeoutFn(
      () => flushResize(stream),
      RESIZE_DEBOUNCE_MS,
    );
  }

  function flushResize(stream) {
    stream.resizeTimer = null;
    const request = stream.pendingResize;
    stream.pendingResize = null;
    if (!request || !streamRecords.has(request.terminalId)) {
      return;
    }
    stream.lastResize = request;
    stream.resizeChain = stream.resizeChain
      .catch(() => undefined)
      .then(async () => {
        if (stream.terminalEnded) {
          return;
        }
        try {
          await api.resizeTerminal(
            request.terminalId,
            request.columns,
            request.rows,
          );
        } catch (error) {
          if (
            !destroyed &&
            !stream.terminalEnded &&
            state.selectedTerminalId === request.terminalId
          ) {
            state.actionError = errorMessage(error);
          }
        }
      });
  }

  function clearPendingResize(stream) {
    if (stream.resizeTimer !== null) {
      clearTimeoutFn(stream.resizeTimer);
      stream.resizeTimer = null;
    }
    stream.pendingResize = null;
  }

  async function killTerminal(terminalId) {
    if (
      !terminalId ||
      state.killing === terminalId ||
      terminalIsFinished(
        state.terminals.find((item) => item.terminal_id === terminalId),
      )
    ) {
      return false;
    }
    state.killing = terminalId;
    state.actionError = '';
    try {
      await api.killTerminal(terminalId);
      await loadTerminals({ silent: true });
      return true;
    } catch (error) {
      state.actionError = errorMessage(error);
      return false;
    } finally {
      if (state.killing === terminalId) {
        state.killing = '';
      }
    }
  }

  async function killSelected() {
    return killTerminal(state.selectedTerminalId);
  }

  async function forgetTerminal(terminalId) {
    if (
      !terminalId ||
      state.forgetting === terminalId ||
      !terminalIsFinished(
        state.terminals.find((item) => item.terminal_id === terminalId),
      )
    ) {
      return false;
    }
    state.forgetting = terminalId;
    state.actionError = '';
    try {
      await api.forgetTerminal(terminalId);
      const stream = streamRecords.get(terminalId);
      if (stream) {
        closeStream(stream);
      }
      state.terminals = state.terminals.filter(
        (item) => item.terminal_id !== terminalId,
      );
      if (state.selectedTerminalId === terminalId) {
        state.selectedTerminalId =
          visibleTerminals(state)[0]?.terminal_id ?? '';
      }
      return true;
    } catch (error) {
      state.actionError = errorMessage(error);
      return false;
    } finally {
      if (state.forgetting === terminalId) {
        state.forgetting = '';
      }
    }
  }

  async function forgetSelected() {
    return forgetTerminal(state.selectedTerminalId);
  }

  async function createGroup(name) {
    if (state.groupActionPending || !name?.trim() || serverUnavailable) {
      return null;
    }
    state.groupActionPending = true;
    state.actionError = '';
    try {
      const result = await api.createTerminalGroup(name.trim());
      const group = reconcileSingleGroup(state, result?.group);
      if (group) {
        state.selectedGroupId = group.group_id;
      }
      return group;
    } catch (error) {
      state.actionError = errorMessage(error);
      return null;
    } finally {
      state.groupActionPending = false;
    }
  }

  async function renameGroup(groupId, name) {
    if (
      state.groupActionPending ||
      !groupId ||
      !name?.trim() ||
      serverUnavailable
    ) {
      return false;
    }
    state.groupActionPending = true;
    state.actionError = '';
    try {
      const group = await api.renameTerminalGroup(groupId, name.trim());
      reconcileGroup(state, group?.group);
      return true;
    } catch (error) {
      state.actionError = errorMessage(error);
      return false;
    } finally {
      state.groupActionPending = false;
    }
  }

  async function deleteGroup(groupId) {
    if (state.groupActionPending || !groupId || serverUnavailable) {
      return null;
    }
    state.groupActionPending = true;
    state.actionError = '';
    try {
      const result = await api.deleteTerminalGroup(groupId);
      await loadTerminals({ silent: true });
      return (
        result ?? {
          group_id: groupId,
          terminals_killed: 0,
        }
      );
    } catch (error) {
      state.actionError = errorMessage(error);
      return null;
    } finally {
      state.groupActionPending = false;
    }
  }

  function reorderGroup(groupId, order) {
    if (!groupId || !Array.isArray(order) || serverUnavailable) {
      return;
    }
    const current = visibleTerminals(state);
    const members = new Set(current.map((terminal) => terminal.terminal_id));
    const ordered = order.filter((terminalId) => members.has(terminalId));
    for (const terminal of current) {
      if (!ordered.includes(terminal.terminal_id)) {
        ordered.push(terminal.terminal_id);
      }
    }
    // Optimistic local order: rewrite the terminal list in the new order so
    // the canvas reflows immediately; the server order follows.
    state.terminals = state.terminals
      .filter((terminal) => terminal.group_id !== groupId)
      .concat(
        ordered.map((terminalId) =>
          state.terminals.find(
            (terminal) => terminal.terminal_id === terminalId,
          ),
        ),
      );
    void api.setTerminalGroupOrder(groupId, ordered).catch(() => {
      if (!destroyed) {
        state.actionError = errorMessage(
          new Error(
            'The terminal order could not be saved on the server. Reload the list to restore it.',
          ),
        );
      }
    });
  }

  async function startManualTerminal(params = {}, { groupId = null } = {}) {
    if (state.startingTerminal || serverUnavailable) {
      return null;
    }
    state.startingTerminal = true;
    state.startError = '';
    try {
      const request = { ...params };
      if (groupId) {
        request.group_id = groupId;
      }
      const result = await api.startTerminal(request);
      if (destroyed) {
        return null;
      }
      const terminal = mergeTerminalSummary(state, result?.terminal);
      if (!terminal) {
        throw new Error('The server returned an invalid terminal.');
      }
      reconcileTerminalLaunchHistory(state, result);
      if (groupId) {
        state.selectedGroupId = groupId;
      }
      state.selectedTerminalId = terminal.terminal_id;
      reconcileStreams();
      return terminal;
    } catch (error) {
      if (!destroyed) {
        state.startError = errorMessage(error);
      }
      return null;
    } finally {
      state.startingTerminal = false;
    }
  }

  function setServerUnavailable(unavailable) {
    const next = unavailable === true;
    if (next === serverUnavailable) {
      return;
    }
    serverUnavailable = next;
    if (next) {
      closeAllStreams();
      return;
    }
    void loadTerminals({ silent: true });
  }

  function clearPendingInput(stream) {
    if (stream.inputTimer !== null) {
      clearTimeoutFn(stream.inputTimer);
      stream.inputTimer = null;
    }
    stream.inputBuffer = '';
  }

  function clearReconnectTimer(stream) {
    if (stream.reconnectTimer !== null) {
      clearTimeoutFn(stream.reconnectTimer);
      stream.reconnectTimer = null;
    }
    clearConnectTimer(stream);
  }

  function clearConnectTimer(stream) {
    if (stream.connectTimer !== null) {
      clearTimeoutFn(stream.connectTimer);
      stream.connectTimer = null;
    }
  }

  function closeStream(stream) {
    streamRecords.delete(stream.terminalId);
    removeStreamView(stream.terminalId);
    clearReconnectTimer(stream);
    clearPendingInput(stream);
    clearPendingResize(stream);
    stream.shouldReconnect = false;
    const connection = stream.connection;
    stream.connection = null;
    connection?.close(1000, 'terminals-view-close');
    onClear(stream.terminalId);
  }

  function closeAllStreams() {
    for (const stream of [...streamRecords.values()]) {
      closeStream(stream);
    }
  }

  function destroy() {
    destroyed = true;
    closeAllStreams();
  }

  return {
    createGroup,
    deleteGroup,
    destroy,
    forgetSelected,
    forgetTerminal,
    killSelected,
    killTerminal,
    loadTerminals,
    queueInput,
    renameGroup,
    reorderGroup,
    resize,
    selectGroup,
    selectTerminal,
    setServerUnavailable,
    startManualTerminal,
    start,
  };
}

function errorMessage(error) {
  if (typeof error?.message === 'string' && error.message.trim()) {
    return error.message.trim();
  }
  if (typeof error === 'string' && error.trim()) {
    return error.trim();
  }
  return '';
}
