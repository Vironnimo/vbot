// Public Terminal surface. Internal modules separate stream coordination,
// speech input, catalog management, state and PTY protocol helpers.
export {
  parseTerminalCommandLine,
  formatTerminalCommandLine,
  createPtyFrameSanitizer,
} from './terminalsView/protocol.js';
export {
  TERMINAL_STREAM_IDLE,
  TERMINAL_STREAM_CONNECTING,
  TERMINAL_STREAM_CONNECTED,
  TERMINAL_STREAM_RECONNECTING,
  TERMINAL_STREAM_ERROR,
  TERMINAL_STREAM_SNAPSHOT,
  TERMINAL_MIN_COLUMNS,
  TERMINAL_MAX_COLUMNS,
  TERMINAL_MIN_ROWS,
  TERMINAL_MAX_ROWS,
  clampTerminalGrid,
  layoutForCount,
  createTerminalsViewState,
  visibleTerminals,
  selectedGroup,
  selectedTerminal,
  terminalIsFinished,
  reconcileTerminalList,
  reconcileTerminalGroups,
  reconcileTerminalLaunchHistory,
  mergeTerminalSummary,
  reconcileGroup,
  reconcileSingleGroup,
} from './terminalsView/state.js';
export { createTerminalsController } from './terminalsView/controller.js';
