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
  transcribeSpeech,
} from '../api.js';
import { createAudioRecorder } from '../audioRecorder.js';
import { reconnectBackoffDelay } from '../backoff.js';
import { createPtyFrameSanitizer } from './protocol.js';
import {
  TERMINAL_STREAM_IDLE,
  TERMINAL_STREAM_CONNECTING,
  TERMINAL_STREAM_CONNECTED,
  TERMINAL_STREAM_RECONNECTING,
  TERMINAL_STREAM_ERROR,
  TERMINAL_STREAM_SNAPSHOT,
  clampTerminalGrid,
  visibleTerminals,
  terminalIsFinished,
  reconcileTerminalList,
  mergeTerminalSummary,
  errorMessage,
} from './state.js';
import {
  closingIds,
  closeFailureListeners,
  notifyCloseFailed,
} from './closeTracking.js';
import { createTerminalSpeech } from './speech.js';
import { createTerminalManagement } from './management.js';

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

export function createTerminalsController({
  state,
  onSnapshot = () => {},
  onOutput = () => {},
  onClear = () => {},
  onTranscript = () => {},
  onSpeechError = () => {},
  createRecorder = createAudioRecorder,
  transcribe = transcribeSpeech,
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
  const { cancelSpeech, toggleSpeech } = createTerminalSpeech({
    state,
    onTranscript,
    onSpeechError,
    createRecorder,
    transcribe,
    isDestroyed: () => destroyed,
    isUnavailable: () => serverUnavailable,
    streamView,
    selectTerminal,
  });
  const {
    createGroup,
    renameGroup,
    deleteGroup,
    reorderGroup,
    startManualTerminal,
  } = createTerminalManagement({
    state,
    api,
    isDestroyed: () => destroyed,
    isUnavailable: () => serverUnavailable,
    loadTerminals,
    reconcileStreams,
  });

  function streamView(terminalId) {
    return (
      state.streams[terminalId] ?? {
        status: TERMINAL_STREAM_IDLE,
        error: '',
        errorCode: '',
        gridPending: false,
      }
    );
  }

  function setStreamView(terminalId, patch) {
    if (patch.status && patch.status !== TERMINAL_STREAM_CONNECTED) {
      cancelSpeech(terminalId);
    }
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
    closeFailureListeners.add(handleCloseFailed);
    await loadTerminals();
  }

  // A close that failed after this controller was destroyed (the user left
  // the tab while the stop was in flight) is surfaced to the remounted
  // controller so the still-running session reappears instead of staying
  // hidden behind the closing filter.
  function handleCloseFailed(terminalId, message) {
    if (destroyed) {
      return;
    }
    state.actionError = message;
    void loadTerminals({ silent: true });
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
      reconcileTerminalList(state, filterClosingTerminals(result));
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
      if (terminalIsFinished(terminal)) cancelSpeech(terminal.terminal_id);
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
        resizeInFlight: null,
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
    cancelSpeech(stream.terminalId);
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
    // The server bounds are enforced here, at the single place every fitted
    // grid passes through; the viewer can never emit a rejected size.
    const fitted = clampTerminalGrid(columns, rows);
    columns = fitted.columns;
    rows = fitted.rows;
    // Skip resizes that only repeat the terminal's authoritative dimensions.
    // Without this, every tab revisit re-sends the same size on its fresh
    // stream and makes the foreground program repaint for nothing.
    if (
      !stream.resizeInFlight &&
      item.columns === columns &&
      item.rows === rows
    ) {
      clearPendingResize(stream);
      setStreamView(terminalId, { gridPending: false });
      return;
    }
    if (
      !stream.resizeInFlight &&
      stream.lastResize?.columns === columns &&
      stream.lastResize.rows === rows
    ) {
      // A completed request may have confirmed a different grid. Keep the
      // mismatch visible instead of starting a reactive resize loop.
      clearPendingResize(stream);
      setStreamView(terminalId, { gridPending: false });
      return;
    }
    stream.pendingResize = { terminalId, columns, rows };
    if (
      stream.resizeInFlight?.columns === columns &&
      stream.resizeInFlight.rows === rows
    ) {
      // The latest intent now matches the active request, replacing any
      // intermediate size that was waiting behind it.
      clearPendingResize(stream);
      return;
    }
    // From here a real correction enters the pipeline; until its response
    // lands, the divergence is expected and the diagnostics stay quiet.
    setStreamView(terminalId, { gridPending: true });
    if (stream.resizeTimer !== null) {
      clearTimeoutFn(stream.resizeTimer);
      stream.resizeTimer = null;
    }
    if (stream.resizeInFlight) {
      return;
    }
    if (immediate) {
      flushResize(stream);
      return;
    }
    // The debounce absorbs remount transients: a tab revisit measures its
    // settling layout several times within this window, and only the final
    // size reaches the PTY. A genuine one-shot layout change re-fires the
    // fit path afterwards with the corrected geometry.
    stream.resizeTimer = setTimeoutFn(
      () => flushResize(stream),
      RESIZE_DEBOUNCE_MS,
    );
  }

  async function flushResize(stream) {
    stream.resizeTimer = null;
    const request = stream.pendingResize;
    stream.pendingResize = null;
    if (
      !request ||
      streamRecords.get(request.terminalId) !== stream ||
      stream.terminalEnded
    ) {
      return;
    }
    stream.resizeInFlight = request;
    try {
      const result = await api.resizeTerminal(
        request.terminalId,
        request.columns,
        request.rows,
      );
      if (
        !destroyed &&
        streamRecords.get(request.terminalId) === stream &&
        !stream.terminalEnded
      ) {
        stream.lastResize = request;
        // The RPC returns the authoritative summary inside `terminal`.
        const index = state.terminals.findIndex(
          (item) => item.terminal_id === request.terminalId,
        );
        if (index >= 0) {
          state.terminals[index] = {
            ...state.terminals[index],
            columns: result.terminal.columns,
            rows: result.terminal.rows,
          };
        }
        setStreamView(request.terminalId, {
          gridPending: stream.pendingResize !== null,
        });
        if (state.selectedTerminalId === request.terminalId) {
          state.actionError = '';
        }
      }
    } catch (error) {
      stream.lastResize = null;
      // A failed correction remains visible and can be retried by a later fit.
      if (
        !destroyed &&
        streamRecords.get(request.terminalId) === stream &&
        !stream.terminalEnded &&
        state.selectedTerminalId === request.terminalId
      ) {
        state.actionError = errorMessage(error);
      }
    } finally {
      stream.resizeInFlight = null;
      if (
        streamRecords.get(request.terminalId) === stream &&
        stream.pendingResize
      ) {
        void flushResize(stream);
      }
    }
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

  // Strip closing Terminal Sessions out of a fresh server list and adjust
  // their groups so the sidebar does not show a phantom member while the
  // background stop and catalog removal are still settling.
  function filterClosingTerminals(result) {
    if (closingIds.size === 0) {
      return result;
    }
    const terminals = (result?.terminals ?? []).filter(
      (terminal) => !closingIds.has(terminal?.terminal_id),
    );
    const groups = (result?.groups ?? []).flatMap((group) => {
      const removed = (result?.terminals ?? []).filter(
        (terminal) =>
          closingIds.has(terminal?.terminal_id) &&
          terminal?.group_id === group.group_id,
      ).length;
      if (removed === 0) {
        return [group];
      }
      const nextCount = Math.max(0, Number(group.terminal_count) - removed);
      if (
        nextCount === 0 &&
        (group.kind === 'automatic' || group.kind === 'finished')
      ) {
        return [];
      }
      return [{ ...group, terminal_count: nextCount }];
    });
    return { ...result, terminals, groups };
  }

  // Remove one Terminal Session from the local projection: close its stream,
  // drop it from the list, keep the sidebar group count in sync, and move
  // the selection to a surviving terminal. Used by forget and by the
  // optimistic close path, where the tile must disappear before the
  // server-side stop completes.
  function removeTerminalFromState(terminalId) {
    const item = state.terminals.find(
      (terminal) => terminal.terminal_id === terminalId,
    );
    if (!item) {
      return null;
    }
    const stream = streamRecords.get(terminalId);
    if (stream) {
      closeStream(stream);
    }
    state.terminals = state.terminals.filter(
      (terminal) => terminal.terminal_id !== terminalId,
    );
    const groupIndex = state.groups.findIndex(
      (group) => group.group_id === item.group_id,
    );
    if (groupIndex >= 0) {
      const group = state.groups[groupIndex];
      const nextCount = Math.max(0, Number(group.terminal_count) - 1);
      if (
        nextCount === 0 &&
        (group.kind === 'automatic' || group.kind === 'finished')
      ) {
        state.groups = state.groups.filter(
          (candidate) => candidate.group_id !== group.group_id,
        );
        if (state.selectedGroupId === group.group_id) {
          state.selectedGroupId = state.groups[0]?.group_id ?? '';
        }
      } else {
        state.groups[groupIndex] = { ...group, terminal_count: nextCount };
      }
    }
    if (state.selectedTerminalId === terminalId) {
      state.selectedTerminalId = visibleTerminals(state)[0]?.terminal_id ?? '';
    }
    return item;
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
      removeTerminalFromState(terminalId);
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

  // Close one Terminal Session with a single click. The tile is removed from
  // the canvas immediately (optimistic); the server-side stop and catalog
  // removal continue in the background, so the UI never waits for the
  // process tree to die. A failed stop restores the session from the server
  // list and surfaces the error.
  async function closeTerminal(terminalId) {
    const item = state.terminals.find(
      (terminal) => terminal.terminal_id === terminalId,
    );
    if (!item || state.closing === terminalId) {
      return false;
    }
    state.closing = terminalId;
    state.actionError = '';
    closingIds.add(terminalId);
    removeTerminalFromState(terminalId);
    try {
      if (!terminalIsFinished(item)) {
        await api.killTerminal(terminalId);
      }
      await api.forgetTerminal(terminalId);
      return true;
    } catch (error) {
      closingIds.delete(terminalId);
      if (destroyed) {
        notifyCloseFailed(terminalId, errorMessage(error));
      } else {
        state.actionError = errorMessage(error);
        await loadTerminals({ silent: true });
      }
      return false;
    } finally {
      closingIds.delete(terminalId);
      if (state.closing === terminalId) {
        state.closing = '';
      }
    }
  }

  async function forgetSelected() {
    return forgetTerminal(state.selectedTerminalId);
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
    cancelSpeech(stream.terminalId);
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
    cancelSpeech();
    closeFailureListeners.delete(handleCloseFailed);
    closeAllStreams();
  }

  return {
    cancelSpeech,
    toggleSpeech,
    closeTerminal,
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
