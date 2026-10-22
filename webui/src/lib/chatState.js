// Public Chat state/controller surface. Internal files retain one controller
// lifecycle and own Session, History, Run-event and streaming projections.
// Compatibility export; protocol membership belongs to the API owner.
export { TERMINAL_RUN_EVENTS } from './api.js';
export {
  createChatState,
  setAgents,
  selectAgent,
  selectedAgent,
  ensureSessionState,
  renameAgentInKey,
  renameAgentInKeys,
  agentActivityStatus,
  agentUnreadResults,
  newestUnreadSessionForAgent,
  sessionHasTerminalRun,
  currentSessionState,
  removeQueuedMessage,
  isSessionEmpty,
  isRunActive,
  contextCompactionState,
  resetStaleRun,
} from './chatState/sessionState.js';
export { createChatController } from './chatState/controller.js';
export { loadHistory, prependHistory } from './chatState/history.js';
export {
  startRun,
  appendRunEvent,
  applyRunControls,
  isReleasedRun,
  releaseFinishedRunEvents,
} from './chatState/runEvents.js';
export {
  isProjectSelected,
  resolveAgentAddressing,
  pickProjectAgentSessionId,
} from './chatState/addressing.js';
export { visibleTimelineItemsForRender } from './chatTimeline.js';
