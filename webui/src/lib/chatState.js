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
  sessionKey,
  ensureSessionState,
  syncAgentSessionActivity,
  applySessionCompletionActivity,
  agentActivityStatus,
  newestUnreadSessionForAgent,
  sessionHasTerminalRun,
  currentSessionState,
  updateSessionUsage,
  syncQueueFromServer,
  addServerQueuedMessage,
  updateQueuedMessageContent,
  removeQueuedMessage,
  isSessionEmpty,
  isRunActive,
  resetStaleRun,
} from './chatState/sessionState.js';
export {
  createChatController,
  normalizeBuiltInCommandName,
} from './chatState/controller.js';
export {
  loadHistory,
  prependHistory,
  truncateSessionForEdit,
} from './chatState/history.js';
export {
  startRun,
  appendRunEvent,
  applyRunControls,
  finishRun,
} from './chatState/runEvents.js';
export { highestContiguousRunEventSequence } from './chatState/streamingEvents.js';
export {
  isProjectSelected,
  resolveAgentAddressing,
  pickProjectAgentSessionId,
  resolveMoveActionFromResponse,
  resolveMoveTarget,
} from './chatState/addressing.js';
export {
  assistantRunChildProgressKey,
  visibleTimelineItemsForRender,
} from './chatTimeline.js';
