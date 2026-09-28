// Public Chat state/controller surface. Internal files retain one controller
// lifecycle and own Session, History, Run-event and streaming projections.
export {
  CHAT_STATUS_IDLE,
  CHAT_STATUS_RUNNING,
  CHAT_STATUS_COMPLETED,
  CHAT_STATUS_FAILED,
  CHAT_STATUS_CANCELLED,
  CHAT_STATUS_INTERRUPTED,
  AGENT_ACTIVITY_IDLE,
  AGENT_ACTIVITY_RUNNING,
  AGENT_ACTIVITY_UNREAD,
  TERMINAL_RUN_EVENTS,
  createChatState,
  setAgents,
  selectAgent,
  selectedAgent,
  ensureSessionState,
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
export { loadHistory } from './chatState/history.js';
export {
  startRun,
  appendRunEvent,
  applyRunControls,
  isReleasedRun,
  releaseFinishedRunEvents,
} from './chatState/runEvents.js';
export { highestContiguousRunEventSequence } from './chatState/streamingEvents.js';
export {
  isProjectSelected,
  resolveAgentAddressing,
  pickProjectAgentSessionId,
} from './chatState/addressing.js';
export {
  assistantRunChildProgressKey,
  visibleTimelineItemsForRender,
} from './chatTimeline.js';
