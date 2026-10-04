import { reconnectBackoffDelay } from '../backoff.js';
import { createPtyFrameSanitizer } from './protocol.js';
import { clampTerminalGrid } from './state.js';

const RECONNECT_INITIAL_DELAY_MS = 500;

const RECONNECT_MAX_DELAY_MS = 8_000;

const RESIZE_DEBOUNCE_MS = 100;

const INPUT_FLUSH_DELAY_MS = 24;

const INPUT_CHUNK_CHARS = 32_768;

// Input typed while the socket reconnects waits for it, up to the server's
// per-message limit; anything beyond is dropped rather than replayed late.
const INPUT_BACKLOG_CHARS = 65_536;

// A socket that still sits in WS_CONNECTING past this budget is treated as
// wedged (half-open after a sleep/radio/network handoff) and force-closed so
// the ordinary close path schedules a reconnect; such sockets never fire
// open or close on their own.
const CONNECT_TIMEOUT_MS = 8_000;

const WS_CONNECTING = 0;

const WS_OPEN = 1;

/**
 * The live connection of one Terminal Session: its WebSocket, the ordered
 * event stream, and the operator's input and resize requests, which travel
 * over the same socket in the order the operator made them.
 *
 * Server events: `terminal_ready` (a full snapshot that restarts the
 * sequence), then `terminal_output`, `terminal_snapshot` and `terminal_state`
 * with consecutive sequences. A gap reconnects for a fresh snapshot. Replies
 * to requests carry no sequence: `input_failed`, `resize_done` and
 * `resize_failed`.
 */
export function createTerminalConnection({
  terminalId,
  subscribe,
  canConnect,
  onStatus,
  onReady,
  onOutput,
  onSnapshot,
  onState,
  onInputFailed,
  onResized,
  onResizeFailed,
  onResizeSettled,
  setTimeoutFn = globalThis.setTimeout,
  clearTimeoutFn = globalThis.clearTimeout,
}) {
  let connection = null;
  let ready = false;
  let closed = false;
  let ended = false;
  let lastSequence = 0;
  let reconnectTimer = null;
  let reconnectAttempt = 0;
  let connectTimer = null;
  let sanitizer = createPtyFrameSanitizer();
  let inputTimer = null;
  let inputBuffer = '';
  let resizeTimer = null;
  let pendingResize = null;
  let resizeInFlight = null;
  let resizeRequest = 0;

  function connect() {
    if (closed || ended || !canConnect()) {
      return;
    }
    clearReconnectTimer();
    onStatus(reconnectAttempt > 0 ? 'reconnecting' : 'connecting');
    try {
      connection = subscribe(terminalId, {
        onEvent: handleEvent,
        onError: (error) => {
          if (!closed) onStatus('error', error);
        },
        onClose: handleClose,
      });
      const socket = connection?.socket;
      if (socket && socket.readyState === WS_CONNECTING) {
        connectTimer = setTimeoutFn(() => {
          connectTimer = null;
          const current = connection?.socket;
          if (closed || !current || current.readyState === WS_OPEN || ended) {
            return;
          }
          // Still connecting past the budget: treat as wedged. The socket
          // never opened, so nothing is lost.
          dropSocket();
          scheduleReconnect(0);
        }, CONNECT_TIMEOUT_MS);
      }
    } catch (error) {
      onStatus('error', error);
      scheduleReconnect();
    }
  }

  function handleEvent(event) {
    if (closed || !event || typeof event !== 'object') {
      return;
    }
    switch (event.type) {
      case 'input_failed':
        if (!ended) onInputFailed(String(event.message ?? ''));
        return;
      case 'resize_done':
      case 'resize_failed':
        settleResize(event);
        return;
      default:
        break;
    }
    const sequence = Number.isInteger(event.sequence) ? event.sequence : null;
    if (event.type === 'terminal_ready') {
      if (sequence === null || typeof event.ansi !== 'string') {
        reconnectForGap();
        return;
      }
      // Fresh stream: any partially-held frame tail from the previous socket
      // is dead and must not bleed into the rebuilt buffer.
      sanitizer = createPtyFrameSanitizer();
      clearConnectTimer();
      lastSequence = sequence;
      reconnectAttempt = 0;
      ready = true;
      onReady(event.ansi, event.terminal);
      if (!ended) {
        flushInput();
        if (pendingResize && !resizeInFlight) flushResize();
      }
      return;
    }
    if (sequence === null || sequence <= lastSequence) {
      return;
    }
    if (sequence !== lastSequence + 1) {
      reconnectForGap();
      return;
    }
    lastSequence = sequence;
    if (event.type === 'terminal_output' && typeof event.data === 'string') {
      const sanitized = sanitizer.next(event.data);
      if (sanitized) onOutput(sanitized);
    } else if (
      event.type === 'terminal_snapshot' &&
      typeof event.ansi === 'string'
    ) {
      onSnapshot(event.ansi, event.terminal);
    } else if (event.type === 'terminal_state') {
      onState(event.terminal);
    }
  }

  function handleClose() {
    if (closed) {
      return;
    }
    connection = null;
    ready = false;
    requeueResizeInFlight();
    if (ended || !canConnect() || reconnectTimer !== null) {
      return;
    }
    onStatus('reconnecting');
    scheduleReconnect();
  }

  function reconnectForGap() {
    onStatus('reconnecting', null, 'gap');
    dropSocket('terminals-view-gap');
    scheduleReconnect(0);
  }

  function dropSocket(reason) {
    const current = connection;
    connection = null;
    ready = false;
    requeueResizeInFlight();
    if (reason) current?.close(1000, reason);
    else current?.close();
  }

  function scheduleReconnect(explicitDelay) {
    if (closed || ended || !canConnect()) {
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
      connect();
    }, delay);
  }

  function send(message) {
    if (!ready || !connection?.send) {
      return false;
    }
    return connection.send(message) === true;
  }

  // -- input --------------------------------------------------------------

  function queueInput(data, { immediate = false } = {}) {
    if (ended || closed || typeof data !== 'string' || !data) {
      return;
    }
    inputBuffer = (inputBuffer + data).slice(0, INPUT_BACKLOG_CHARS);
    if (immediate || inputBuffer.length >= INPUT_CHUNK_CHARS) {
      flushInput();
      return;
    }
    if (inputTimer === null) {
      inputTimer = setTimeoutFn(flushInput, INPUT_FLUSH_DELAY_MS);
    }
  }

  function flushInput() {
    clearInputTimer();
    while (inputBuffer && !ended) {
      const data = inputBuffer.slice(0, INPUT_CHUNK_CHARS);
      if (!send({ type: 'input', data })) {
        // Not connected: the backlog goes out once the stream is ready.
        return;
      }
      inputBuffer = inputBuffer.slice(data.length);
    }
  }

  // -- resize -------------------------------------------------------------

  // `current` is the terminal's authoritative grid.
  function resize(columns, rows, { current, immediate = false } = {}) {
    if (
      ended ||
      closed ||
      !Number.isInteger(columns) ||
      !Number.isInteger(rows)
    ) {
      return;
    }
    // The server bounds are enforced here, at the single place every fitted
    // grid passes through; the viewer can never emit a rejected size.
    const fitted = clampTerminalGrid(columns, rows);
    // Skip resizes that only repeat the terminal's authoritative dimensions.
    // Without this, every tab revisit re-sends the same size on its fresh
    // stream and makes the foreground program repaint for nothing.
    if (
      !resizeInFlight &&
      current?.columns === fitted.columns &&
      current?.rows === fitted.rows
    ) {
      clearPendingResize();
      return;
    }
    pendingResize = fitted;
    if (
      resizeInFlight?.columns === fitted.columns &&
      resizeInFlight.rows === fitted.rows
    ) {
      // The latest intent now matches the active request, replacing any
      // intermediate size that was waiting behind it.
      clearPendingResize();
      return;
    }
    clearResizeTimer();
    if (resizeInFlight) {
      return;
    }
    if (immediate) {
      flushResize();
      return;
    }
    // The debounce absorbs remount transients: a tab revisit measures its
    // settling layout several times within this window, and only the final
    // size reaches the PTY.
    resizeTimer = setTimeoutFn(() => {
      resizeTimer = null;
      flushResize();
    }, RESIZE_DEBOUNCE_MS);
  }

  function flushResize() {
    clearResizeTimer();
    const request = pendingResize;
    if (!request || ended || resizeInFlight) {
      return;
    }
    resizeRequest += 1;
    const inFlight = { ...request, request: resizeRequest };
    if (
      !send({
        type: 'resize',
        request: inFlight.request,
        columns: inFlight.columns,
        rows: inFlight.rows,
      })
    ) {
      // Not connected: the size is requested once the stream is ready.
      return;
    }
    pendingResize = null;
    resizeInFlight = inFlight;
  }

  function settleResize(event) {
    if (!resizeInFlight || event.request !== resizeInFlight.request) {
      return;
    }
    resizeInFlight = null;
    if (ended) {
      return;
    }
    if (event.type === 'resize_done') {
      onResized(event.terminal);
    } else {
      onResizeFailed(String(event.message ?? ''));
    }
    if (pendingResize) {
      flushResize();
    } else {
      // Report the settled grid: a rejected request leaves the PTY at its
      // old size, which the viewer must mirror again.
      onResizeSettled();
    }
  }

  function requeueResizeInFlight() {
    // A request whose socket closed may not have arrived; a newer intent
    // replaces it, otherwise it goes out again once the stream is ready.
    if (resizeInFlight && !pendingResize) {
      pendingResize = {
        columns: resizeInFlight.columns,
        rows: resizeInFlight.rows,
      };
    }
    resizeInFlight = null;
  }

  // True while this viewer's own resize has not reached the PTY yet.
  function resizeSettling() {
    return Boolean(
      resizeTimer !== null || pendingResize !== null || resizeInFlight !== null,
    );
  }

  // -- lifecycle ----------------------------------------------------------

  // The process ended: keep the socket's last snapshot, drop what was not
  // sent, and never reconnect.
  function finish() {
    ended = true;
    clearInputTimer();
    inputBuffer = '';
    clearPendingResize();
    resizeInFlight = null;
    clearReconnectTimer();
  }

  function close() {
    closed = true;
    finish();
    const current = connection;
    connection = null;
    ready = false;
    current?.close(1000, 'terminals-view-close');
  }

  function clearPendingResize() {
    clearResizeTimer();
    pendingResize = null;
  }

  function clearResizeTimer() {
    if (resizeTimer !== null) {
      clearTimeoutFn(resizeTimer);
      resizeTimer = null;
    }
  }

  function clearInputTimer() {
    if (inputTimer !== null) {
      clearTimeoutFn(inputTimer);
      inputTimer = null;
    }
  }

  function clearReconnectTimer() {
    if (reconnectTimer !== null) {
      clearTimeoutFn(reconnectTimer);
      reconnectTimer = null;
    }
    clearConnectTimer();
  }

  function clearConnectTimer() {
    if (connectTimer !== null) {
      clearTimeoutFn(connectTimer);
      connectTimer = null;
    }
  }

  return {
    terminalId,
    connect,
    close,
    finish,
    queueInput,
    resize,
    resizeSettling,
    get ended() {
      return ended;
    },
  };
}
