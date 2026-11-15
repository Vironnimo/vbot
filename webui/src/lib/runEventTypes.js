export const TERMINAL_RUN_EVENTS = new Set([
  'run_completed',
  'run_cancelled',
  'run_failed',
  'run_interrupted',
]);

// Protocol classifications shared by stream and WebSocket consumers. State
// transitions remain owned by each consumer.
const RUN_SERVER_EVENT_TYPES = new Set([
  'run_started',
  'run_output',
  ...TERMINAL_RUN_EVENTS,
]);

export function isTerminalRunEvent(type) {
  return TERMINAL_RUN_EVENTS.has(type);
}

export function isRunServerEvent(type) {
  return RUN_SERVER_EVENT_TYPES.has(type);
}
