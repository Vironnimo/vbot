import {
  createTerminalGroup,
  deleteTerminalGroup,
  forgetTerminal,
  killTerminal,
  listTerminals,
  prepareSpeechTranscription,
  renameTerminalGroup,
  setTerminalGroupOrder,
  startTerminal,
  subscribeTerminalEvents,
  transcribeSpeech,
} from '../api.js';
import { createAudioRecorder } from '../audioRecorder.js';
import { createTerminalConnection } from './connection.js';
import {
  TERMINAL_STREAM_IDLE,
  TERMINAL_STREAM_CONNECTED,
  TERMINAL_STREAM_SNAPSHOT,
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

export function createTerminalsController({
  state,
  onSnapshot = () => {},
  onOutput = () => {},
  onGeometry = () => {},
  onClear = () => {},
  onTranscript = () => {},
  onSpeechError = () => {},
  createRecorder = createAudioRecorder,
  transcribe = transcribeSpeech,
  prepareTranscription = prepareSpeechTranscription,
  api = {
    createTerminalGroup,
    deleteTerminalGroup,
    forgetTerminal,
    killTerminal,
    listTerminals,
    renameTerminalGroup,
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
  // One live connection per visible Terminal Session.
  const connections = new Map();
  const { cancelSpeech, toggleSpeech } = createTerminalSpeech({
    state,
    onTranscript,
    onSpeechError,
    createRecorder,
    transcribe,
    prepareTranscription,
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
    for (const connection of [...connections.values()]) {
      if (!listedIds.has(connection.terminalId)) {
        closeStream(connection);
      }
    }
    if (serverUnavailable) {
      return;
    }
    for (const terminal of visibleTerminals(state)) {
      if (terminalIsFinished(terminal)) cancelSpeech(terminal.terminal_id);
      if (!connections.has(terminal.terminal_id)) {
        connectStream(terminal.terminal_id);
      }
    }
  }

  function connectStream(terminalId) {
    if (destroyed || serverUnavailable) {
      return;
    }
    const connection = createTerminalConnection({
      terminalId,
      subscribe: api.subscribeTerminalEvents,
      canConnect: () => !destroyed && !serverUnavailable,
      setTimeoutFn,
      clearTimeoutFn,
      onStatus: (status, error = null, errorCode = '') =>
        setStreamView(terminalId, {
          status,
          error: error ? errorMessage(error) : '',
          errorCode,
        }),
      onReady: (ansi, summary) => {
        const terminal = mergeTerminalSummary(state, summary);
        const finished = terminalIsFinished(terminal);
        setStreamView(terminalId, {
          status: finished
            ? TERMINAL_STREAM_SNAPSHOT
            : TERMINAL_STREAM_CONNECTED,
          error: '',
          errorCode: '',
        });
        onSnapshot(terminalId, ansi, terminal);
        if (finished) {
          markStreamFinished(connection);
          void loadTerminals({ silent: true });
        }
      },
      onOutput: (data) => onOutput(terminalId, data),
      onSnapshot: (ansi, summary) =>
        onSnapshot(terminalId, ansi, mergeTerminalSummary(state, summary)),
      onState: (summary) => {
        const terminal = mergeTerminalSummary(state, summary);
        if (terminalIsFinished(terminal)) {
          markStreamFinished(connection);
          void loadTerminals({ silent: true });
        } else if (terminal) {
          onGeometry(terminalId, terminal);
        }
      },
      onInputFailed: (message) => reportError(terminalId, message),
      onResized: (summary) => {
        // The reply carries the authoritative summary of the resized PTY.
        const index = state.terminals.findIndex(
          (item) => item.terminal_id === terminalId,
        );
        if (index >= 0 && summary) {
          state.terminals[index] = {
            ...state.terminals[index],
            columns: summary.columns,
            rows: summary.rows,
          };
        }
        if (state.selectedTerminalId === terminalId) {
          state.actionError = '';
        }
      },
      onResizeFailed: (message) => reportError(terminalId, message),
      onResizeSettled: () => {
        const item = state.terminals.find(
          (terminal) => terminal.terminal_id === terminalId,
        );
        if (item && !destroyed) {
          onGeometry(terminalId, item);
        }
      },
    });
    connections.set(terminalId, connection);
    connection.connect();
  }

  function reportError(terminalId, message) {
    if (!destroyed && state.selectedTerminalId === terminalId) {
      state.actionError = message;
    }
  }

  function markStreamFinished(connection) {
    cancelSpeech(connection.terminalId);
    connection.finish();
    setStreamView(connection.terminalId, {
      status: TERMINAL_STREAM_SNAPSHOT,
    });
    if (state.selectedTerminalId === connection.terminalId) {
      state.actionError = '';
    }
  }

  function queueInput(
    data,
    { immediate = false, terminalId = state.selectedTerminalId } = {},
  ) {
    const item = state.terminals.find(
      (terminal) => terminal.terminal_id === terminalId,
    );
    if (!item || terminalIsFinished(item) || serverUnavailable) {
      return;
    }
    connections.get(terminalId)?.queueInput(data, { immediate });
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
    if (!item || terminalIsFinished(item) || serverUnavailable) {
      return;
    }
    connections
      .get(terminalId)
      ?.resize(columns, rows, { current: item, immediate });
  }

  // True while this viewer's own resize has not reached the PTY yet.
  function resizeSettling(terminalId) {
    return connections.get(terminalId)?.resizeSettling() ?? false;
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
  // the selection to a surviving terminal. Used by the optimistic close
  // path, where the tile must disappear before the server-side stop
  // completes.
  function removeTerminalFromState(terminalId) {
    const item = state.terminals.find(
      (terminal) => terminal.terminal_id === terminalId,
    );
    if (!item) {
      return null;
    }
    const connection = connections.get(terminalId);
    if (connection) {
      closeStream(connection);
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

  function closeStream(connection) {
    cancelSpeech(connection.terminalId);
    connections.delete(connection.terminalId);
    removeStreamView(connection.terminalId);
    connection.close();
    onClear(connection.terminalId);
  }

  function closeAllStreams() {
    for (const connection of [...connections.values()]) {
      closeStream(connection);
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
    loadTerminals,
    queueInput,
    renameGroup,
    reorderGroup,
    resize,
    resizeSettling,
    selectGroup,
    selectTerminal,
    setServerUnavailable,
    startManualTerminal,
    start,
  };
}
