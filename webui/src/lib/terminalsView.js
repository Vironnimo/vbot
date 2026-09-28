// Public Terminal surface. Internal modules separate stream coordination,
// speech input, catalog management, state and PTY protocol helpers.
export {
  parseTerminalCommandLine,
  formatTerminalCommandLine,
} from './terminalsView/protocol.js';
export {
  TERMINAL_STREAM_IDLE,
  TERMINAL_STREAM_CONNECTED,
  TERMINAL_STREAM_ERROR,
  TERMINAL_MAX_COLUMNS,
  TERMINAL_MAX_ROWS,
  clampTerminalGrid,
  layoutForCount,
  createTerminalsViewState,
  visibleTerminals,
  terminalIsFinished,
} from './terminalsView/state.js';
export { createTerminalsController } from './terminalsView/controller.js';
