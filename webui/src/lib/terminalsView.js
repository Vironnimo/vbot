import {
  forgetTerminal,
  killTerminal,
  listTerminals,
  sendTerminalInput,
  startTerminal,
  subscribeTerminalEvents,
} from './api.js';
import { reconnectBackoffDelay } from './backoff.js';

export const TERMINAL_STREAM_IDLE = 'idle';
export const TERMINAL_STREAM_CONNECTING = 'connecting';
export const TERMINAL_STREAM_CONNECTED = 'connected';
export const TERMINAL_STREAM_RECONNECTING = 'reconnecting';
export const TERMINAL_STREAM_ERROR = 'error';
export const TERMINAL_STREAM_SNAPSHOT = 'snapshot';

const DEFAULT_TERMINAL_COLUMNS = 120;
const DEFAULT_TERMINAL_ROWS = 32;
const RECONNECT_INITIAL_DELAY_MS = 500;
const RECONNECT_MAX_DELAY_MS = 8_000;
const INPUT_FLUSH_DELAY_MS = 24;
const INPUT_CHUNK_CHARS = 32_768;
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

/**
 * Keep the viewer on the PTY's canonical grid while enlarging its cells to
 * consume the available browser surface. At least one spacing axis remains at
 * its natural value, so glyphs never overlap or become narrower than the font.
 *
 * @param {{ availableWidth: number, availableHeight: number, columns: number, rows: number, baseFontSize: number, baseCellWidth: number, baseCellHeight: number }} measurements
 * @returns {{ columns: number, rows: number, fontSize: number, letterSpacing: number, lineHeight: number } | null}
 */
export function terminalViewerGeometry(measurements) {
  const availableWidth = positiveNumber(measurements?.availableWidth);
  const availableHeight = positiveNumber(measurements?.availableHeight);
  const baseFontSize = positiveNumber(measurements?.baseFontSize);
  const baseCellWidth = positiveNumber(measurements?.baseCellWidth);
  const baseCellHeight = positiveNumber(measurements?.baseCellHeight);
  if (
    availableWidth === null ||
    availableHeight === null ||
    baseFontSize === null ||
    baseCellWidth === null ||
    baseCellHeight === null
  ) {
    return null;
  }
  const columns = terminalGridDimension(
    measurements?.columns,
    DEFAULT_TERMINAL_COLUMNS,
  );
  const rows = terminalGridDimension(measurements?.rows, DEFAULT_TERMINAL_ROWS);
  const targetCellWidth = availableWidth / columns;
  const targetCellHeight = availableHeight / rows;
  const scale = Math.min(
    targetCellWidth / baseCellWidth,
    targetCellHeight / baseCellHeight,
  );
  const scaledCellWidth = baseCellWidth * scale;
  const scaledCellHeight = baseCellHeight * scale;
  return {
    columns,
    rows,
    fontSize: baseFontSize * scale,
    letterSpacing: Math.max(0, targetCellWidth - scaledCellWidth),
    lineHeight: Math.max(1, targetCellHeight / scaledCellHeight),
  };
}

export function terminalViewerPointer(event, bounds, scale) {
  const scaleX = positiveNumber(scale?.x) ?? 1;
  const scaleY = positiveNumber(scale?.y) ?? 1;
  return {
    clientX: bounds.left + (event.clientX - bounds.left) / scaleX,
    clientY: bounds.top + (event.clientY - bounds.top) / scaleY,
  };
}

function terminalGridDimension(value, fallback) {
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

function positiveNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? value
    : null;
}

export function createTerminalsViewState() {
  return {
    terminals: [],
    launchHistory: [],
    selectedTerminalId: '',
    loading: false,
    listError: '',
    actionError: '',
    streams: {},
    killing: '',
    forgetting: '',
    startingTerminal: false,
    startError: '',
  };
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
  reconcileTerminalLaunchHistory(state, result);
  if (
    state.selectedTerminalId &&
    state.terminals.some(
      (terminal) => terminal.terminal_id === state.selectedTerminalId,
    )
  ) {
    return state.selectedTerminalId;
  }
  state.selectedTerminalId = state.terminals[0]?.terminal_id ?? '';
  return state.selectedTerminalId;
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

export function createTerminalsController({
  state,
  onSnapshot = () => {},
  onOutput = () => {},
  onClear = () => {},
  api = {
    forgetTerminal,
    killTerminal,
    listTerminals,
    sendTerminalInput,
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

  function reconcileStreams() {
    const listedIds = new Set(
      state.terminals.map((terminal) => terminal.terminal_id),
    );
    for (const stream of [...streamRecords.values()]) {
      if (!listedIds.has(stream.terminalId)) {
        closeStream(stream);
      }
    }
    if (serverUnavailable) {
      return;
    }
    for (const terminal of state.terminals) {
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
        inputTimer: null,
        inputBuffer: '',
        inputChain: Promise.resolve(),
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
      onOutput(stream.terminalId, event.data);
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
        state.selectedTerminalId = state.terminals[0]?.terminal_id ?? '';
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

  async function startManualTerminal(params = {}) {
    if (state.startingTerminal || serverUnavailable) {
      return null;
    }
    state.startingTerminal = true;
    state.startError = '';
    try {
      const result = await api.startTerminal(params);
      if (destroyed) {
        return null;
      }
      const terminal = mergeTerminalSummary(state, result?.terminal);
      if (!terminal) {
        throw new Error('The server returned an invalid terminal.');
      }
      reconcileTerminalLaunchHistory(state, result);
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
  }

  function closeStream(stream) {
    streamRecords.delete(stream.terminalId);
    removeStreamView(stream.terminalId);
    clearReconnectTimer(stream);
    clearPendingInput(stream);
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
    destroy,
    forgetSelected,
    forgetTerminal,
    killSelected,
    killTerminal,
    loadTerminals,
    queueInput,
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
