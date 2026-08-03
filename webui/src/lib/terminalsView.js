import {
  killTerminal,
  listTerminals,
  resizeTerminal,
  sendTerminalInput,
  subscribeTerminalEvents,
} from './api.js';
import { reconnectBackoffDelay } from './backoff.js';

export const TERMINAL_STREAM_IDLE = 'idle';
export const TERMINAL_STREAM_CONNECTING = 'connecting';
export const TERMINAL_STREAM_CONNECTED = 'connected';
export const TERMINAL_STREAM_RECONNECTING = 'reconnecting';
export const TERMINAL_STREAM_ERROR = 'error';

const RECONNECT_INITIAL_DELAY_MS = 500;
const RECONNECT_MAX_DELAY_MS = 8_000;
const INPUT_FLUSH_DELAY_MS = 24;
const INPUT_CHUNK_CHARS = 32_768;
const RESIZE_DEBOUNCE_MS = 100;
const TERMINAL_STATES_FINISHED = new Set(['exited', 'error']);

export function createTerminalsViewState() {
  return {
    terminals: [],
    selectedTerminalId: '',
    loading: false,
    listError: '',
    actionError: '',
    streamError: '',
    streamErrorCode: '',
    streamStatus: TERMINAL_STREAM_IDLE,
    lastSequence: 0,
    killing: false,
  };
}

export function selectedTerminal(state) {
  return (
    state.terminals.find(
      (terminal) => terminal.terminal_id === state.selectedTerminalId,
    ) ?? null
  );
}

export function reconcileTerminalList(state, result) {
  const terminals = Array.isArray(result?.terminals) ? result.terminals : [];
  state.terminals = terminals.filter(
    (terminal) =>
      terminal &&
      typeof terminal.terminal_id === 'string' &&
      !TERMINAL_STATES_FINISHED.has(terminal.state),
  );
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

export function mergeTerminalSummary(state, terminal) {
  if (!terminal || typeof terminal.terminal_id !== 'string') {
    return null;
  }
  const index = state.terminals.findIndex(
    (item) => item.terminal_id === terminal.terminal_id,
  );
  if (TERMINAL_STATES_FINISHED.has(terminal.state)) {
    if (index >= 0) {
      state.terminals = state.terminals.filter(
        (item) => item.terminal_id !== terminal.terminal_id,
      );
    }
    return terminal;
  }
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
    killTerminal,
    listTerminals,
    resizeTerminal,
    sendTerminalInput,
    subscribeTerminalEvents,
  },
  setTimeoutFn = globalThis.setTimeout,
  clearTimeoutFn = globalThis.clearTimeout,
}) {
  let destroyed = false;
  let serverUnavailable = false;
  let listRequestId = 0;
  let currentStream = null;
  let reconnectTimer = null;
  let reconnectAttempt = 0;
  let inputTimer = null;
  let inputBuffer = '';
  let inputChain = Promise.resolve();
  let resizeTimer = null;
  let pendingResize = null;
  let lastResize = null;
  let resizeChain = Promise.resolve();

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
      const previousSelection = state.selectedTerminalId;
      const nextSelection = reconcileTerminalList(state, result);
      if (nextSelection !== previousSelection) {
        switchStream(nextSelection);
      } else if (nextSelection && !currentStream && !serverUnavailable) {
        connectStream(nextSelection);
      }
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
    switchStream(terminalId);
  }

  function switchStream(terminalId) {
    clearPendingInput();
    clearPendingResize();
    closeCurrentStream();
    state.lastSequence = 0;
    state.streamError = '';
    state.streamErrorCode = '';
    state.actionError = '';
    lastResize = null;
    onClear();
    if (!terminalId || serverUnavailable) {
      state.streamStatus = TERMINAL_STREAM_IDLE;
      return;
    }
    connectStream(terminalId);
  }

  function connectStream(terminalId) {
    clearReconnectTimer();
    state.streamStatus =
      reconnectAttempt > 0
        ? TERMINAL_STREAM_RECONNECTING
        : TERMINAL_STREAM_CONNECTING;
    const stream = {
      terminalId,
      connection: null,
      shouldReconnect: true,
      terminalEnded: false,
    };
    try {
      stream.connection = api.subscribeTerminalEvents(terminalId, {
        onEvent: (event) => handleStreamEvent(stream, event),
        onError: (error) => {
          if (currentStream !== stream) {
            return;
          }
          state.streamError = errorMessage(error);
          state.streamErrorCode = '';
          state.streamStatus = TERMINAL_STREAM_ERROR;
        },
        onClose: () => handleStreamClose(stream),
      });
      currentStream = stream;
    } catch (error) {
      state.streamError = errorMessage(error);
      state.streamErrorCode = '';
      state.streamStatus = TERMINAL_STREAM_ERROR;
      scheduleReconnect(terminalId);
    }
  }

  function handleStreamEvent(stream, event) {
    if (currentStream !== stream || !event || typeof event !== 'object') {
      return;
    }
    const sequence = Number.isInteger(event.sequence) ? event.sequence : null;
    if (event.type === 'terminal_ready') {
      if (sequence === null || typeof event.ansi !== 'string') {
        reconnectForGap(stream);
        return;
      }
      state.lastSequence = sequence;
      state.streamError = '';
      state.streamErrorCode = '';
      state.streamStatus = TERMINAL_STREAM_CONNECTED;
      reconnectAttempt = 0;
      const terminal = mergeTerminalSummary(state, event.terminal);
      onSnapshot(event.ansi, terminal);
      if (terminal && TERMINAL_STATES_FINISHED.has(terminal.state)) {
        stream.terminalEnded = true;
        stream.shouldReconnect = false;
        void loadTerminals({ silent: true });
      }
      return;
    }
    if (sequence === null || sequence <= state.lastSequence) {
      return;
    }
    if (sequence !== state.lastSequence + 1) {
      reconnectForGap(stream);
      return;
    }
    state.lastSequence = sequence;
    if (event.type === 'terminal_output' && typeof event.data === 'string') {
      onOutput(event.data);
      return;
    }
    if (event.type === 'terminal_state') {
      const terminal = mergeTerminalSummary(state, event.terminal);
      if (terminal && TERMINAL_STATES_FINISHED.has(terminal.state)) {
        stream.terminalEnded = true;
        stream.shouldReconnect = false;
        void loadTerminals({ silent: true });
      }
    }
  }

  function reconnectForGap(stream) {
    if (currentStream !== stream) {
      return;
    }
    state.streamStatus = TERMINAL_STREAM_RECONNECTING;
    state.streamError = '';
    state.streamErrorCode = 'gap';
    const terminalId = stream.terminalId;
    closeCurrentStream();
    scheduleReconnect(terminalId, 0);
  }

  function handleStreamClose(stream) {
    if (currentStream !== stream) {
      return;
    }
    currentStream = null;
    if (
      destroyed ||
      serverUnavailable ||
      stream.terminalEnded ||
      !stream.shouldReconnect ||
      state.selectedTerminalId !== stream.terminalId
    ) {
      return;
    }
    state.streamStatus = TERMINAL_STREAM_RECONNECTING;
    scheduleReconnect(stream.terminalId);
  }

  function scheduleReconnect(terminalId, explicitDelay) {
    if (
      destroyed ||
      serverUnavailable ||
      state.selectedTerminalId !== terminalId
    ) {
      return;
    }
    clearReconnectTimer();
    const delay =
      explicitDelay ??
      reconnectBackoffDelay(reconnectAttempt, {
        initialDelayMs: RECONNECT_INITIAL_DELAY_MS,
        maxDelayMs: RECONNECT_MAX_DELAY_MS,
      });
    reconnectAttempt += 1;
    reconnectTimer = setTimeoutFn(() => {
      reconnectTimer = null;
      if (
        !destroyed &&
        !serverUnavailable &&
        state.selectedTerminalId === terminalId
      ) {
        connectStream(terminalId);
      }
    }, delay);
  }

  function queueInput(data, { immediate = false } = {}) {
    if (
      typeof data !== 'string' ||
      !data ||
      !state.selectedTerminalId ||
      serverUnavailable
    ) {
      return;
    }
    inputBuffer += data;
    if (immediate || inputBuffer.length >= INPUT_CHUNK_CHARS) {
      flushInput();
      return;
    }
    if (inputTimer === null) {
      inputTimer = setTimeoutFn(flushInput, INPUT_FLUSH_DELAY_MS);
    }
  }

  function flushInput() {
    if (inputTimer !== null) {
      clearTimeoutFn(inputTimer);
      inputTimer = null;
    }
    if (!inputBuffer || !state.selectedTerminalId) {
      return;
    }
    const terminalId = state.selectedTerminalId;
    const data = inputBuffer.slice(0, INPUT_CHUNK_CHARS);
    inputBuffer = inputBuffer.slice(data.length);
    inputChain = inputChain
      .catch(() => undefined)
      .then(async () => {
        try {
          await api.sendTerminalInput(terminalId, data);
          if (!destroyed && state.selectedTerminalId === terminalId) {
            state.actionError = '';
          }
        } catch (error) {
          if (!destroyed && state.selectedTerminalId === terminalId) {
            state.actionError = errorMessage(error);
          }
        }
      });
    if (inputBuffer) {
      inputTimer = setTimeoutFn(flushInput, 0);
    }
  }

  function resize(columns, rows) {
    if (
      !Number.isInteger(columns) ||
      !Number.isInteger(rows) ||
      !state.selectedTerminalId ||
      serverUnavailable
    ) {
      return;
    }
    pendingResize = { terminalId: state.selectedTerminalId, columns, rows };
    if (
      lastResize?.terminalId === pendingResize.terminalId &&
      lastResize.columns === columns &&
      lastResize.rows === rows
    ) {
      return;
    }
    if (resizeTimer !== null) {
      clearTimeoutFn(resizeTimer);
    }
    resizeTimer = setTimeoutFn(flushResize, RESIZE_DEBOUNCE_MS);
  }

  function flushResize() {
    resizeTimer = null;
    const request = pendingResize;
    pendingResize = null;
    if (!request || state.selectedTerminalId !== request.terminalId) {
      return;
    }
    lastResize = request;
    resizeChain = resizeChain
      .catch(() => undefined)
      .then(async () => {
        try {
          await api.resizeTerminal(
            request.terminalId,
            request.columns,
            request.rows,
          );
        } catch (error) {
          if (!destroyed && state.selectedTerminalId === request.terminalId) {
            state.actionError = errorMessage(error);
          }
        }
      });
  }

  async function killSelected() {
    const terminalId = state.selectedTerminalId;
    if (!terminalId || state.killing) {
      return false;
    }
    state.killing = true;
    state.actionError = '';
    try {
      await api.killTerminal(terminalId);
      await loadTerminals({ silent: true });
      return true;
    } catch (error) {
      state.actionError = errorMessage(error);
      return false;
    } finally {
      state.killing = false;
    }
  }

  function setServerUnavailable(unavailable) {
    const next = unavailable === true;
    if (next === serverUnavailable) {
      return;
    }
    serverUnavailable = next;
    if (next) {
      closeCurrentStream();
      state.streamStatus = TERMINAL_STREAM_IDLE;
      return;
    }
    void loadTerminals({ silent: true });
  }

  function clearPendingInput() {
    if (inputTimer !== null) {
      clearTimeoutFn(inputTimer);
      inputTimer = null;
    }
    inputBuffer = '';
  }

  function clearPendingResize() {
    if (resizeTimer !== null) {
      clearTimeoutFn(resizeTimer);
      resizeTimer = null;
    }
    pendingResize = null;
  }

  function clearReconnectTimer() {
    if (reconnectTimer !== null) {
      clearTimeoutFn(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function closeCurrentStream() {
    clearReconnectTimer();
    if (!currentStream) {
      return;
    }
    const stream = currentStream;
    currentStream = null;
    stream.shouldReconnect = false;
    stream.connection?.close(1000, 'terminals-view-close');
  }

  function destroy() {
    destroyed = true;
    clearPendingInput();
    clearPendingResize();
    closeCurrentStream();
  }

  return {
    destroy,
    killSelected,
    loadTerminals,
    queueInput,
    resize,
    selectTerminal,
    setServerUnavailable,
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
